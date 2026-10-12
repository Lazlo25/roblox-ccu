#!/usr/bin/env python3
"""Hourly CCU collector for the Roblox Monetization Tracker (standard library only).

Each run:
  1. reads five Roblox charts anonymously (every page, All Devices, All Locations) from the explore API;
  2. adds games seen on any chart in the last 4 days, so CCU keeps being tracked after a game drops off;
  3. gets current CCU ("playing") and total visits for all of them from the games API, and their up/down votes from the
     votes API, 50 IDs per request;
  4. appends one row per game to data/hourly/<UTC date>.csv (utc, universe_id, ccu, charts), keeps each game's first and
     latest visit count of the Eastern day in data/visits/<Eastern date>.json and its latest votes in data/votes/;
  5. reads RoTerminal's total players on Roblox (data/platform/<UTC date>.csv), its players by genre across its top 600
     games (data/genres/<Eastern date>.json) and its daily platform averages (data/roterminal_daily.json);
  6. rewrites data/latest_24h.json with each game's name, avg_ccu, min_ccu, max_ccu and samples over the last 24 hours,
     and data/daily/<Eastern date>.json with the same stats since midnight Eastern (today's file and yesterday's), plus
     each game's session length and like %, the latest total-players reading and the day's genre shares.
Readings are grouped by hour, so two runs in one hour count as one sample (the schedule runs twice an hour in case
GitHub skips one).

Session length (minutes per visit) follows Little's law: average players ÷ visits per minute, using the day's first and
latest visit counts and the CCU readings between them. It needs at least SESSION_MIN_HOURS of readings.

A failed chart, games batch or RoTerminal read is retried, then skipped, and the run carries on with the rest.
"""
import csv, datetime as dt, json, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
HOURLY = os.path.join(HERE, 'data', 'hourly')
LATEST = os.path.join(HERE, 'data', 'latest_24h.json')
DAILY = os.path.join(HERE, 'data', 'daily')
VISITS = os.path.join(HERE, 'data', 'visits')
VOTES = os.path.join(HERE, 'data', 'votes')
PLATFORM = os.path.join(HERE, 'data', 'platform')
GENRES = os.path.join(HERE, 'data', 'genres')
RT_DAILY = os.path.join(HERE, 'data', 'roterminal_daily.json')
ET = ZoneInfo('America/New_York')
# The tracker's four charts plus Top Playing Now. device=all matches the tracker: device=computer charts left out
# about a fifth of the tracker's games in testing.
SORTS = {'te': 'top-earning', 'mp': 'most-popular', 'pn': 'top-playing-now', 'tt': 'top-trending', 'uc': 'up-and-coming'}
CHART_URL = 'https://apis.roblox.com/explore-api/v1/get-sort-content?sessionId={sid}&sortId={sort}&device=all&country=all'
GAMES_URL = 'https://games.roblox.com/v1/games?universeIds='
VOTES_URL = 'https://games.roblox.com/v1/games/votes?universeIds='
# RoTerminal (roterminal.co, "Crawlers welcome" in its robots.txt): live total players on Roblox and players by genre.
RT_URL = 'https://roterminal.co/api/'
KEEP_DAYS = 4        # keep tracking a game this long after it was last seen on a chart
WINDOW_HOURS = 24    # latest_24h.json covers this many hours
SESSION_MIN_HOURS = 6
RT_KEEP_DAYS = 400   # days of RoTerminal's daily platform averages kept in data/roterminal_daily.json
FIELDS = ['utc', 'universe_id', 'ccu', 'charts']


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get_json(url, tries=5):
    """GET a JSON body, retrying failures with backoff (and honoring Retry-After on 429)."""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (roblox-ccu)', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            if i == tries - 1:
                raise
            retry_after = e.headers.get('Retry-After') if isinstance(e, urllib.error.HTTPError) and e.code == 429 else None
            time.sleep(int(retry_after) if retry_after and retry_after.isdigit() else 3 * 2 ** i)


def chart(sort_id):
    """Universe IDs on one chart, in order, across every page."""
    url = CHART_URL.format(sid=uuid.uuid4(), sort=sort_id)
    ids, token = [], ''
    for _ in range(40):
        r = get_json(url + ('&pageToken=' + urllib.parse.quote(token) if token else ''))
        games = r.get('games') or []
        ids += [str(g['universeId']) for g in games if g.get('universeId')]
        token = r.get('nextPageToken')
        if not token or not games:
            break
    return ids


def details(ids, url=GAMES_URL):
    """{universe ID: API record} for every ID that came back; failed batches are skipped."""
    out, failed = {}, 0
    for i in range(0, len(ids), 50):
        try:
            for g in get_json(url + ','.join(ids[i:i + 50])).get('data') or []:
                out[str(g['id'])] = g
        except Exception as e:
            failed += 1
            log(f'warning: batch {i // 50 + 1} of {url.split("?")[0]} failed: {e}')
        time.sleep(0.5)
    return out, failed


def parse_utc(s):
    return dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)


def rows_since(since):
    """Recorded rows newer than `since`, read from the daily CSVs that can hold them."""
    day, rows = since.date(), []
    while day <= dt.datetime.now(dt.timezone.utc).date():
        path = os.path.join(HOURLY, f'{day}.csv')
        if os.path.exists(path):
            with open(path, newline='', encoding='utf-8') as f:
                rows += [r for r in csv.DictReader(f) if parse_utc(r['utc']) > since]
        day += dt.timedelta(days=1)
    return rows


def hourly_stats(rows, visits=None, votes=None):
    """Per game: one reading per hour (the mean of that hour's runs), then avg/min/max over those hours.
    With a day's visits and votes, also session length (minutes per visit) and like %."""
    hours, ccu_at = {}, {}
    for r in rows:
        hours.setdefault(r['universe_id'], {}).setdefault(r['utc'][:13], []).append(int(r['ccu']))
        ccu_at.setdefault(r['universe_id'], []).append((r['utc'], int(r['ccu'])))
    out = {}
    for u, hs in hours.items():
        vals = [sum(v) / len(v) for v in hs.values()]
        out[u] = {'avg_ccu': round(sum(vals) / len(vals), 1), 'min_ccu': round(min(vals)), 'max_ccu': round(max(vals)), 'samples': len(vals)}
        v = (visits or {}).get(u)
        if v:
            t0, v0, t1, v1 = v
            span_h = (parse_utc(t1) - parse_utc(t0)).total_seconds() / 3600
            between = [c for t, c in ccu_at[u] if t0 <= t <= t1]
            if span_h >= SESSION_MIN_HOURS and v1 > v0 and between:
                # Little's law: players on average = visits per minute × minutes per visit.
                out[u]['sess'] = round(sum(between) / len(between) * span_h * 60 / (v1 - v0), 2)
                out[u]['sess_h'] = round(span_h, 1)
        up, down = (votes or {}).get(u) or (0, 0)
        if up + down:
            out[u]['like'] = round(100 * up / (up + down), 2)
    return out


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def write_lines(path, obj):
    """A JSON object with one key per line, for small git diffs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = ',\n'.join(json.dumps(k, ensure_ascii=False) + ':' + json.dumps(v, ensure_ascii=False, separators=(',', ':')) for k, v in obj.items())
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        f.write('{\n' + body + '\n}\n')
    os.replace(path + '.tmp', path)


def record_visits_votes(now, det, votes):
    """Each game's first and latest visit count of the Eastern day ([first utc, visits, latest utc, visits]), and its
    latest up/down votes."""
    stamp, day = now.strftime('%Y-%m-%dT%H:%M:%SZ'), now.astimezone(ET).date()
    path = os.path.join(VISITS, f'{day}.json')
    seen = load_json(path, {})
    for u, g in det.items():
        v = g.get('visits')
        if isinstance(v, int) and v > 0:
            seen[u] = seen[u][:2] + [stamp, v] if u in seen else [stamp, v, stamp, v]
    write_lines(path, dict(sorted(seen.items(), key=lambda kv: int(kv[0]))))
    path = os.path.join(VOTES, f'{day}.json')
    kept = load_json(path, {})
    kept.update({u: [int(g.get('upVotes') or 0), int(g.get('downVotes') or 0)] for u, g in votes.items()})
    write_lines(path, dict(sorted(kept.items(), key=lambda kv: int(kv[0]))))


def roterminal(now):
    """RoTerminal's total players now, players by genre now, and its daily platform averages. Failures are logged."""
    stamp, day = now.strftime('%Y-%m-%dT%H:%M:%SZ'), now.astimezone(ET).date()
    ok = []
    try:
        t, players = get_json(RT_URL + 'board', tries=3)['platform']  # only the full board carries the platform total
        path = os.path.join(PLATFORM, f'{now.date()}.csv')
        os.makedirs(PLATFORM, exist_ok=True)
        new_file = not os.path.exists(path)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(('utc,players,rt_utc\n' if new_file else '') + f'{stamp},{int(players)},{dt.datetime.fromtimestamp(t, dt.timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n')
        ok.append('platform')
    except Exception as e:
        log(f'warning: RoTerminal platform read failed: {e}')
    try:
        g = get_json(RT_URL + 'genres', tries=3)
        path = os.path.join(GENRES, f'{day}.json')
        acc = load_json(path, {'hours': [], 'playing': {}})
        hour = stamp[:13]
        if hour not in acc['hours']:  # one reading per UTC hour, like the CCU rows
            acc['hours'].append(hour)
            for x in g['genres']:
                acc['playing'][x['name']] = acc['playing'].get(x['name'], 0) + int(x['playing'])
            write_lines(path, acc)
        ok.append('genres')
    except Exception as e:
        log(f'warning: RoTerminal genres read failed: {e}')
    try:
        days = get_json(RT_URL + 'daily', tries=3)['days'][-RT_KEEP_DAYS:]
        # [day start (midnight Eastern, Unix seconds), average players, peak players, 1 while the day is in progress]
        with open(RT_DAILY + '.tmp', 'w', encoding='utf-8') as f:
            f.write('{"fetched_utc":"%s","days":[\n%s\n]}\n' % (stamp, ',\n'.join(json.dumps(d) for d in days)))
        os.replace(RT_DAILY + '.tmp', RT_DAILY)
        ok.append('daily')
    except Exception as e:
        log(f'warning: RoTerminal daily read failed: {e}')
    return ok


def platform_now(day):
    """The latest total-players reading taken during an Eastern day: [utc, players], or None."""
    start = dt.datetime.combine(day, dt.time(0), ET).astimezone(dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    last = None
    for d in (start.date(), (end - dt.timedelta(seconds=1)).date()):
        path = os.path.join(PLATFORM, f'{d}.csv')
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                for r in csv.DictReader(f):
                    if start <= parse_utc(r['utc']) < end:
                        last = [r['utc'], int(r['players'])]
    return last


def genre_shares(day):
    """Each genre's share (%) of RoTerminal's top-600 players over the Eastern day's hourly readings, and the hour count."""
    acc = load_json(os.path.join(GENRES, f'{day}.json'), None)
    total = sum(acc['playing'].values()) if acc else 0
    if not total:
        return None, 0
    return {g: round(100 * p / total, 3) for g, p in sorted(acc['playing'].items(), key=lambda kv: -kv[1])}, len(acc['hours'])


def write_stats(path, head, stats, names, old_path=None):
    """One game per line, for small diffs. Names come from this run, else from the file being replaced."""
    old = {}
    if os.path.exists(old_path or path):
        try:
            with open(old_path or path, encoding='utf-8') as f:
                old = json.load(f).get('games') or {}
        except Exception:
            pass
    lines = [json.dumps(u) + ':' + json.dumps({'name': names.get(u) or (old.get(u) or {}).get('name', ''), **stats[u]},
                                               ensure_ascii=False, separators=(',', ':')) for u in sorted(stats, key=int)]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        f.write(json.dumps(head)[:-1] + ',"games":{\n' + ',\n'.join(lines) + '\n}}\n')
    os.replace(path + '.tmp', path)
    return len(lines)


def write_outputs(now, names):
    stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    games = write_stats(LATEST, {'generated_utc': stamp, 'window_hours': WINDOW_HOURS},
                        hourly_stats(rows_since(now - dt.timedelta(hours=WINDOW_HOURS))), names)
    local = now.astimezone(ET)
    for back in (1, 0):  # yesterday's file keeps updating until its last hour is in; today's grows through the day
        day = local.date() - dt.timedelta(days=back)
        start = dt.datetime.combine(day, dt.time(0), ET)
        end = start + dt.timedelta(days=1)
        rows = [r for r in rows_since(start.astimezone(dt.timezone.utc) - dt.timedelta(seconds=1))
                if parse_utc(r['utc']) < end.astimezone(dt.timezone.utc)]
        if rows:
            shares, genre_hours = genre_shares(day)
            write_stats(os.path.join(DAILY, f'{day}.json'),
                        {'date': str(day), 'timezone': 'America/New_York', 'generated_utc': stamp,
                         'through_et': min(local, end).strftime('%H:%M') if back == 0 else '24:00',
                         'platform_now': platform_now(day), 'genres': shares, 'genre_hours': genre_hours},
                        hourly_stats(rows, load_json(os.path.join(VISITS, f'{day}.json'), {}),
                                     load_json(os.path.join(VOTES, f'{day}.json'), {})), names)
    return games


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    # The schedule tries every few minutes because GitHub drops most scheduled runs; record each UTC hour once.
    if '--force' not in sys.argv and any(r['utc'][:13] == now.strftime('%Y-%m-%dT%H') for r in rows_since(now - dt.timedelta(hours=1))):
        print(f'SKIP {now:%Y-%m-%dT%H}: this hour is already recorded')
        return
    on, failed_charts = {}, []
    for code, sort_id in SORTS.items():
        try:
            ids = chart(sort_id)
            if not ids:
                raise ValueError('empty chart')
            for u in ids:
                on.setdefault(u, []).append(code)
        except Exception as e:
            failed_charts.append(sort_id)
            log(f'warning: chart {sort_id} failed: {e}')
    recent = []
    for r in rows_since(now - dt.timedelta(days=KEEP_DAYS)):
        if r['charts'] and r['universe_id'] not in on and r['universe_id'] not in recent:
            recent.append(r['universe_id'])
    ids = list(on) + recent
    det, failed_batches = details(ids)
    votes, failed_votes = details(list(det), VOTES_URL)
    record_visits_votes(now, det, votes)
    rt = roterminal(now)

    stamp = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    rows = [{'utc': stamp, 'universe_id': u, 'ccu': int(det[u].get('playing') or 0), 'charts': ' '.join(on.get(u, []))}
            for u in ids if u in det]
    os.makedirs(HOURLY, exist_ok=True)
    path = os.path.join(HOURLY, f'{now.date()}.csv')
    new_file = not os.path.exists(path)
    with open(path, 'a', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerows(rows)
    games = write_outputs(now, {u: (g.get('name') or '').strip() for u, g in det.items()})
    print(f'OK utc={stamp} charted={len(on)} recent={len(recent)} recorded={len(rows)} votes={len(votes)} '
          f'latest_24h_games={games} failed_charts={",".join(failed_charts) or "-"} failed_batches={failed_batches} '
          f'failed_vote_batches={failed_votes} roterminal={",".join(rt) or "failed"}')
    if not rows:
        sys.exit('nothing recorded this run')


if __name__ == '__main__':
    main()
