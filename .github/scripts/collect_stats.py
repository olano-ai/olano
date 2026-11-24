#!/usr/bin/env python3
"""Record the public repositories' adoption numbers on the `stats` branch.

Run daily by .github/workflows/stats.yml. GitHub shows most of these numbers
only as a current value (stars, forks) or only for the last 14 days (views and
clones), so nothing remembers yesterday unless something writes it down. This
script writes it down:

  history.csv          one row per day: stars, forks and watchers of each
                       public repository, and release asset downloads
  traffic.csv          one row per day of olano-ai/olano views and clones,
                       merged so each run keeps the 14 days GitHub reports;
                       written only when the STATS_TOKEN secret is set, since
                       the traffic API needs a token with Administration read
  latest.json          the newest numbers in one document
  badges/stars.json    shields.io endpoint badge: stars across the repositories

Container image downloads are deliberately not recorded: the managed fleet's
own update pulls dominate them, so they say nothing about adoption.

A number that cannot be read on a given day is left blank, never guessed, and
never fails the run: a missing day is better than a wrong one.

Standard library only, so the workflow needs no install step.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

OWNER = "olano-ai"
REPOS = ("olano", "skills", "olano-agents")
BADGES = ("stars.json",)


STATS_README = """# Olano adoption numbers

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
"""


def _fetch(url: str, token: str | None = None, accept: str = "application/vnd.github+json") -> str:
    headers = {"Accept": accept, "User-Agent": "olano-stats"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def _api(path: str, token: str | None) -> object:
    return json.loads(_fetch(f"https://api.github.com{path}", token))


def _warn(message: str) -> None:
    print(f"::warning::{message}", file=sys.stderr)


def repo_numbers(token: str | None) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for repo in REPOS:
        try:
            data = _api(f"/repos/{OWNER}/{repo}", token)
            assert isinstance(data, dict)
            out[f"{repo}_stars"] = int(data["stargazers_count"])
            out[f"{repo}_forks"] = int(data["forks_count"])
            out[f"{repo}_watchers"] = int(data["subscribers_count"])
        except (urllib.error.URLError, KeyError, ValueError, AssertionError) as exc:
            _warn(f"{repo}: repository numbers unavailable ({exc})")
            for key in ("stars", "forks", "watchers"):
                out[f"{repo}_{key}"] = None
    return out


def release_downloads(token: str | None) -> int | None:
    total = 0
    try:
        page = 1
        while True:
            releases = _api(f"/repos/{OWNER}/olano/releases?per_page=100&page={page}", token)
            assert isinstance(releases, list)
            if not releases:
                return total
            total += sum(
                int(asset.get("download_count", 0))
                for release in releases
                for asset in release.get("assets", [])
            )
            page += 1
    except (urllib.error.URLError, ValueError, AssertionError) as exc:
        _warn(f"release downloads unavailable ({exc})")
        return None


def traffic_rows(token: str) -> dict[str, dict[str, int]]:
    """Per-day views and clones for the last 14 days, keyed by date."""
    rows: dict[str, dict[str, int]] = {}
    views = _api(f"/repos/{OWNER}/olano/traffic/views", token)
    clones = _api(f"/repos/{OWNER}/olano/traffic/clones", token)
    assert isinstance(views, dict) and isinstance(clones, dict)
    for day in views.get("views", []):
        row = rows.setdefault(day["timestamp"][:10], {})
        row["views"] = int(day["count"])
        row["unique_visitors"] = int(day["uniques"])
    for day in clones.get("clones", []):
        row = rows.setdefault(day["timestamp"][:10], {})
        row["clones"] = int(day["count"])
        row["unique_cloners"] = int(day["uniques"])
    return rows


def compact(number: int) -> str:
    for size, suffix in ((1_000_000, "M"), (1_000, "k")):
        if number >= size:
            text = f"{number / size:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(number)


def _read_csv(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    if not path.exists():
        return [], {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), {row["date"]: row for row in reader}


def _write_csv(path: Path, fields: list[str], rows: dict[str, dict[str, object]]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for date in sorted(rows):
            writer.writerow({k: ("" if v is None else v) for k, v in rows[date].items()})


def merge_daily(path: Path, new_rows: dict[str, dict[str, object]], fields: list[str]) -> None:
    """Upsert rows by date. The columns are exactly `fields`, so a number this
    script stops collecting leaves the file rather than lingering as blanks."""
    _, rows = _read_csv(path)
    merged: dict[str, dict[str, object]] = {date: dict(row) for date, row in rows.items()}
    for date, row in new_rows.items():
        merged.setdefault(date, {"date": date}).update(row)
    _write_csv(path, ["date"] + [f for f in fields if f != "date"], merged)


def write_badge(path: Path, label: str, value: int | None, logo: str) -> None:
    if value is None:
        return  # keep yesterday's badge rather than publish a wrong number
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "label": label,
                "message": compact(value),
                "color": "blue",
                "namedLogo": logo,
                "cacheSeconds": 3600,
            },
            indent=2,
        )
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="checkout of the stats branch")
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    token = os.environ.get("GITHUB_TOKEN") or None
    traffic_token = os.environ.get("STATS_TOKEN") or None
    today = dt.datetime.now(dt.UTC).date().isoformat()

    row: dict[str, object] = {"date": today}
    row.update(repo_numbers(token))
    row["release_downloads"] = release_downloads(token)
    stars = [row[f"{repo}_stars"] for repo in REPOS]
    known_stars = [s for s in stars if isinstance(s, int)]
    row["stars_total"] = sum(known_stars) if known_stars else None

    fields = list(row)
    merge_daily(out / "history.csv", {today: row}, fields)

    if traffic_token:
        try:
            days = traffic_rows(traffic_token)
            merge_daily(
                out / "traffic.csv",
                {date: {"date": date, **values} for date, values in days.items()},
                ["views", "unique_visitors", "clones", "unique_cloners"],
            )
        except (urllib.error.URLError, KeyError, ValueError, AssertionError) as exc:
            _warn(f"traffic unavailable ({exc}); check that STATS_TOKEN can read Administration")
    else:
        print("STATS_TOKEN not set: skipping views and clones.")

    (out / "README.md").write_text(STATS_README)
    (out / "latest.json").write_text(json.dumps(row, indent=2) + "\n")
    for stale in (out / "badges").glob("*.json"):
        if stale.name not in BADGES:
            stale.unlink()  # a badge this script no longer writes
    write_badge(out / "badges" / "stars.json", "stars", row["stars_total"], "github")  # type: ignore[arg-type]

    print(json.dumps(row, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
