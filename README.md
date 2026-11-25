# Olano adoption numbers

Written once a day by `.github/workflows/stats.yml` on `main`
(`.github/scripts/collect_stats.py`). Nothing else writes to this branch.

- `history.csv`: one row per day (UTC) with stars, forks and watchers of
  olano, skills and olano-agents, and release asset downloads. A blank cell
  means the number could not be read that day.
- `traffic.csv`: olano-ai/olano views and clones per day. GitHub keeps only 14
  days of these, so this file is the long-term record. Needs the STATS_TOKEN
  secret.
- `latest.json`: the newest row.
- `badges/*.json`: shields.io endpoint badges used by the README on `main`.
