# roblox-ccu

Hourly concurrent-player (CCU) readings for the games on Roblox's charts, collected by a GitHub Actions job so the
Roblox Monetization Tracker can use 24-hour averages instead of one daily reading.

Every hour at :17 UTC, `collect.py` (standard library only):

- reads Top Earning, Most Popular, Top Playing Now, Top Trending and Up-and-Coming anonymously (All Devices, All Locations);
- adds games seen on any chart in the last 4 days, and the IDs in `watchlist.txt` (18+ titles the anonymous charts leave out);
- gets each game's current CCU from the games API and appends one row per game to `data/hourly/<UTC date>.csv`;
- rewrites `data/latest_24h.json`: `generated_utc`, plus each game's name, `avg_ccu`, `min_ccu`, `max_ccu` and `samples` over the last 24 hours.

## If the schedule stops

GitHub disables scheduled workflows after 60 days without repository activity. The job's own hourly commits count as
activity, so this shouldn't happen. If it does, open the repo's **Actions** tab, pick **Hourly CCU** and click
**Enable workflow**, then **Run workflow** once.
