# roblox-ccu

Hourly concurrent-player (CCU) readings for the games on Roblox's charts, collected by a GitHub Actions job so the
Roblox Monetization Tracker can use 24-hour averages instead of one daily reading.

Once per hour (each workflow run stays alive for about 5h40m collecting hourly, then starts the next run, because GitHub drops most scheduled runs; `collect.py` skips hours already recorded), it (standard library only):

- reads Top Earning, Most Popular, Top Playing Now, Top Trending and Up-and-Coming anonymously (All Devices, All Locations);
- adds games seen on any chart in the last 4 days;
- gets each game's current CCU and total visits from the games API and its up/down votes from the votes API, and appends one row per game to `data/hourly/<UTC date>.csv`;
- keeps each game's first and latest visit count of the Eastern day in `data/visits/<Eastern date>.json` and its latest votes in `data/votes/<Eastern date>.json`;
- reads [RoTerminal](https://roterminal.co)'s total players on Roblox (`data/platform/<UTC date>.csv`), its players by genre across its top 600 games (`data/genres/<Eastern date>.json`) and its daily platform averages (`data/roterminal_daily.json`);
- rewrites `data/latest_24h.json`: `generated_utc`, plus each game's name, `avg_ccu`, `min_ccu`, `max_ccu` and `samples` over the last 24 hours;
- rewrites `data/daily/<Eastern date>.json` with the same stats since midnight Eastern (`through_et` says how far the day has got), plus each game's `sess` (minutes per visit: average CCU ÷ visits per minute, once there are 6+ hours of readings) and `like` (% of votes that are up), the latest `platform_now` reading and the day's `genres` shares.

Readings are grouped by hour, so `samples` counts hours with at least one reading.

## If the schedule stops

GitHub disables scheduled workflows after 60 days without repository activity. The job's own hourly commits count as
activity, so this shouldn't happen. If it does, open the repo's **Actions** tab, pick **Hourly CCU** and click
**Enable workflow**, then **Run workflow** once.
