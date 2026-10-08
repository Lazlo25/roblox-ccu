#!/usr/bin/env python3
"""Hourly CCU collector for the Roblox Monetization Tracker (standard library only).

Each run:
  1. reads five Roblox charts anonymously (every page, All Devices, All Locations) from the explore API;
  2. adds games seen on any chart in the last 4 days, so CCU keeps being tracked after a game drops off,
     plus every universe ID listed in watchlist.txt;
  3. gets current CCU ("playing") for all of them from the games API, 50 IDs per request;
  4. appends one row per game to data/hourly/<UTC date>.csv (utc, universe_id, ccu, charts);
  5. rewrites data/latest_24h.json with each game's name, avg_ccu, min_ccu, max_ccu and samples over the last 24 hours.

A failed chart or games batch is retried, then skipped, and the run carries on with the rest.
"""
import csv, datetime as dt, json, os, sys, time, urllib.error, urllib.parse, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
HOURLY = os.path.join(HERE, 'data', 'hourly')
LATEST = os.path.join(HERE, 'data', 'latest_24h.json')
WATCHLIST = os.path.join(HERE, 'watchlist.txt')
# The tracker's four charts plus Top Playing Now. device=all matches the tracker: device=computer charts left out
# about a fifth of the tracker's games in testing.
SORTS = {'te': 'top-earning', 'mp': 'most-popular', 'pn': 'top-playing-now', 'tt': 'top-trending', 'uc': 'up-and-coming'}
CHART_URL = 'https://apis.roblox.com/explore-api/v1/get-sort-content?sessionId={sid}&sortId={sort}&device=all&country=all'
GAMES_URL = 'https://games.roblox.com/v1/games?universeIds='
KEEP_DAYS = 4        # keep tracking a game this long after it was last seen on a chart
WINDOW_HOURS = 24    # latest_24h.json covers this many hours
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


def details(ids):
    """{universe ID: games API record} for every ID that came back; failed batches are skipped."""
    out, failed = {}, 0
    for i in range(0, len(ids), 50):
        try:
            for g in get_json(GAMES_URL + ','.join(ids[i:i + 50])).get('data') or []:
                out[str(g['id'])] = g
        except Exception as e:
            failed += 1
            log(f'warning: games batch {i // 50 + 1} failed: {e}')
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


def watchlist():
    if not os.path.exists(WATCHLIST):
        return []
    with open(WATCHLIST, encoding='utf-8') as f:
        return [s for s in (line.split('#')[0].strip() for line in f) if s.isdigit()]


def write_latest(now, names):
    """data/latest_24h.json: per-game stats over the last 24 hours, one game per line for small diffs."""
    stats = {}
    for r in rows_since(now - dt.timedelta(hours=WINDOW_HOURS)):
        v = int(r['ccu'])
        s = stats.setdefault(r['universe_id'], {'sum': 0, 'samples': 0, 'min_ccu': v, 'max_ccu': v})
        s['sum'] += v
        s['samples'] += 1
        s['min_ccu'], s['max_ccu'] = min(s['min_ccu'], v), max(s['max_ccu'], v)
    old = {}
    if os.path.exists(LATEST):
        try:
            with open(LATEST, encoding='utf-8') as f:
                old = json.load(f).get('games') or {}
        except Exception:
            pass
    lines = []
    for u in sorted(stats, key=int):
        s = stats[u]
        entry = {'name': names.get(u) or (old.get(u) or {}).get('name', ''), 'avg_ccu': round(s['sum'] / s['samples'], 1),
                 'min_ccu': s['min_ccu'], 'max_ccu': s['max_ccu'], 'samples': s['samples']}
        lines.append(json.dumps(u) + ':' + json.dumps(entry, ensure_ascii=False, separators=(',', ':')))
    head = json.dumps({'generated_utc': now.strftime('%Y-%m-%dT%H:%M:%SZ'), 'window_hours': WINDOW_HOURS})[:-1]
    with open(LATEST + '.tmp', 'w', encoding='utf-8') as f:
        f.write(head + ',"games":{\n' + ',\n'.join(lines) + '\n}}\n')
    os.replace(LATEST + '.tmp', LATEST)
    return len(lines)


def main():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
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
    watch = [u for u in watchlist() if u not in on and u not in recent]
    ids = list(on) + recent + watch
    det, failed_batches = details(ids)

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
    games = write_latest(now, {u: (g.get('name') or '').strip() for u, g in det.items()})
    print(f'OK utc={stamp} charted={len(on)} recent={len(recent)} watchlist={len(watch)} recorded={len(rows)} '
          f'latest_24h_games={games} failed_charts={",".join(failed_charts) or "-"} failed_batches={failed_batches}')
    if not rows:
        sys.exit('nothing recorded this run')


if __name__ == '__main__':
    main()
