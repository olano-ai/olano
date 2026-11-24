"""Benchmark the home-config read path — how often ``config.yaml`` is parsed.

``<home>/config.yaml`` is read by ~17 loader functions (``load_home_config``
plus every ``system_settings.load_*_settings``) and by ~95 call sites across
~44 modules. They all go through one cached read path
(``config/home.py:read_home_config_doc``), which parses once per
``(path, mtime_ns, size, inode)`` and hands out a copy.

Before that cache existed, one ``GET /api/agents/{name}`` parsed the file **22
times** — 8 from ``internal_tools._cortex_on`` (once per ``cortex_*`` tool), 4
from ``runtime/auth.multi_user_enabled`` on *every* request in the product, 6
from the media probes and 4 from speech. With a realistic ~2 KB config that was
~110 ms of a 127 ms request, and the cost grew with the file: it is linear in
config size, and that file grows as a box is configured.

This script exists so that regression is visible the moment it comes back. The
number to watch is the **parse count per request**, not the milliseconds — a new
uncached reader shows up as a jump there long before anyone notices the latency,
and the report names the calling function so you know which one to fix.

    uv run python scripts/bench_home_config.py
    uv run python scripts/bench_home_config.py --agents 20 --repeat 15
    uv run python scripts/bench_home_config.py --compare   # vs. no cache

``--compare`` re-runs the sweep with the store's cache defeated. That isolates
the *caching* half only — the reads still use libyaml, where the original code
used PyYAML's pure-Python loader (~8x slower on the same document), so the real
before/after is wider than the ratio printed here. Everything runs against a
throwaway home seeded with a full config.yaml; nothing touches the real one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

# Every settings block the runtime knows, so the parse cost is realistic rather
# than the near-zero of an empty file. Handshake is off and multi_user disabled
# so the API answers without an auth gate.
FULL_CONFIG = """\
default_agent: bench00
preferred_model: anthropic:claude-sonnet-4-6
runtime_api_port: 8765
dashboard_port: 3200
multi_user_enabled: false
highlights_all_agents: false
extra_bind_roots: []
archetype_dirs: []
sandbox_tool_sets: [chinese]
checkpoint_backend: sqlite
store_backend: sqlite
mcp_relay_url: null
instance_id: bench-01
cognition:
  enabled: true
  autonomous: true
  auto_approve: true
  intensity: aggressive
  fast_model: olano:turbo
  deep_model: olano:pro
  features:
    self_learning: true
    brainstorming: true
    idea_generation: true
    prototyping: true
    skills_optimization: true
    workspace_optimization: true
    memory_optimization: true
    pulse: true
speech:
  provider: olano
  tts_model: gpt-4o-mini-tts
  stt_model: whisper-1
  voice: alloy
  audio_format: mp3
media:
  provider: olano
  image_model: gpt-image-1
web_search:
  provider: olano
playground:
  enabled: true
disk_monitor:
  enabled: true
  path: /
  check_interval_minutes: 15
  warn_percent: 80
  cleanup_percent: 90
  actions: [olano_caches, docker_image_prune, npm_cache, journald_vacuum]
  cooldown_minutes: 360
  force_days: 7
  vacuum_size: 200M
catalog_sync:
  enabled: true
  poll_minutes: 30
handshake:
  enabled: false
  session_hours: 12
issues:
  auto_process: true
  poll_minutes: 10
revisions:
  enabled: true
  debounce_seconds: 30
  max_file_kb: 256
prompt_refresh:
  enabled: true
  check_interval_minutes: 10
mcp_health:
  enabled: true
  check_interval_minutes: 5
  refresh_window_minutes: 20
rate_limits:
  messages:
    per_minute: 20
    per_hour: 400
    per_day: 4000
heartbeat:
  enabled: true
  interval_minutes: 5
credits:
  display: credits
"""


def _endpoints(agent: str) -> list[tuple[str, str]]:
    """A dashboard page load, plus the Cortex knobs' own reads."""
    return [
        ("GET /api/status", "/api/status"),
        ("GET /api/agents", "/api/agents"),
        ("GET /api/agents/{name}", f"/api/agents/{agent}"),
        ("GET /api/cognition/status", f"/api/cognition/status?agent={agent}"),
        ("GET /api/pulse", f"/api/pulse?lane=pulse&status=pending&agent={agent}"),
        ("GET /api/cortex/items", f"/api/cortex/items?agent={agent}"),
        ("GET /api/diagnostics/disk", "/api/diagnostics/disk"),
        ("GET /api/catalog/status", "/api/catalog/status"),
        ("GET /api/catalog/providers?fields=basic", "/api/catalog/providers?fields=basic"),
    ]


def _build_home(root: Path, n_agents: int) -> tuple[Path, list[str]]:
    home, ws = root / "home", root / "workspace"
    ws.mkdir(parents=True)
    (ws / "AGENTS.md").write_text("# agents\n")
    (ws / "MEMORY.md").write_text("# memory\n")
    (ws / "memories").mkdir()
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(FULL_CONFIG)

    names = [f"bench{i:02d}" for i in range(n_agents)]
    for nm in names:
        d = home / "agents" / nm
        d.mkdir(parents=True)
        (d / "agent.json5").write_text(
            json.dumps(
                {
                    "name": nm,
                    "model": "fake:instant",
                    "backend": {"type": "filesystem", "root_dir": str(ws)},
                    "workspace": str(ws),
                    "description": "bench agent",
                    "cognition": {},
                }
            )
        )
    return ws, names


def _build_transport(ws: Path, names: list[str]):
    from unittest.mock import AsyncMock, MagicMock

    import httpx

    from olano.admin.memory import MemoryMonitor
    from olano.config.agent_config import AgentConfig
    from olano.config.backend_config import FilesystemBackendConfig
    from olano.runtime.api import create_api

    runners = {}
    for nm in names:
        cfg = AgentConfig(
            name=nm,
            model="fake:instant",
            backend=FilesystemBackendConfig(root_dir=str(ws)),
            workspace=str(ws),
        )
        r = MagicMock()
        r.config = cfg
        r.ainvoke = AsyncMock(return_value="ok")
        runners[nm] = r

    gw = MagicMock()
    gw._runners = runners
    gw._channels = []
    gw.registry = {}
    gw._agents_booting = False
    gw.scheduler = MagicMock()
    gw.scheduler.list_jobs.return_value = []
    gw.get_runner.side_effect = lambda name: runners.get(name)
    gw._memory_monitor = MemoryMonitor()
    return httpx.ASGITransport(app=create_api(gw))


class _ParseWatch:
    """Count parses of the home config, attributed to the calling function.

    Wraps the loader ``read_home_config_doc`` actually calls, so it sees only
    real parses — a cache hit never reaches it.
    """

    def __init__(self, home_mod, attribute: bool):
        self._mod = home_mod
        self._attribute = attribute
        self._real = home_mod.yaml.load
        self.count = 0
        self.callers: Counter = Counter()
        self.active = False

    def install(self, monkey) -> None:
        def counting(*a, **kw):
            if self.active:
                self.count += 1
                if self._attribute:
                    self.callers[self._blame()] += 1
            return self._real(*a, **kw)

        monkey(self._mod.yaml, "load", counting)

    @staticmethod
    def _blame() -> str:
        """The nearest olano frames above the read, newest last — enough to name
        the loader AND the caller that asked for it. This script's own frames are
        dropped so the ``--compare`` shim doesn't sit in the middle of every row.
        """
        frames = [
            f
            for f in traceback.extract_stack()[:-3]
            if "/olano/" in f.filename and "bench_home_config" not in f.filename
        ]
        return " <- ".join(
            f"{Path(f.filename).parent.name}/{Path(f.filename).name}:{f.name}"
            for f in reversed(frames[-3:])
        )


async def _sweep(transport, endpoints, repeat: int, watch: _ParseWatch) -> list[tuple]:
    import httpx

    rows = []
    async with httpx.AsyncClient(transport=transport, base_url="http://bench", timeout=60) as c:
        for label, url in endpoints:
            try:
                for _ in range(3):  # warm: import + lazy caches, not what we measure
                    await c.get(url)
            except Exception:
                rows.append((label, None, 0.0))
                continue
            watch.count = 0
            watch.active = True
            times = []
            ok = True
            for _ in range(repeat):
                t0 = time.perf_counter()
                r = await c.get(url)
                times.append((time.perf_counter() - t0) * 1000)
                if r.status_code != 200:
                    ok = False
            watch.active = False
            rows.append(
                (label, statistics.median(times) if ok and times else None, watch.count / repeat)
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--agents", type=int, default=8, help="fleet size to serve (default 8)")
    ap.add_argument("--repeat", type=int, default=12, help="requests per endpoint (default 12)")
    ap.add_argument(
        "--compare",
        action="store_true",
        help="also sweep with the parse cache defeated (the pre-cache behaviour)",
    )
    args = ap.parse_args()

    root = Path(tempfile.mkdtemp(prefix="olano-benchcfg-"))
    ws, names = _build_home(root, args.agents)
    os.environ["LIONCLAW_HOME"] = str(root / "home")
    os.environ["LIONCLAW_WORKSPACE"] = str(ws)
    os.environ.setdefault("LIONCLAW_CHECKPOINT_BACKEND", "sqlite")
    os.environ.setdefault("LIONCLAW_STORE_BACKEND", "sqlite")

    import olano.config.home as home_mod

    transport = _build_transport(ws, names)
    endpoints = _endpoints(names[0])
    size = (root / "home" / "config.yaml").stat().st_size

    patches: list[tuple] = []

    def monkey(obj, name, value):
        patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    watch = _ParseWatch(home_mod, attribute=True)
    watch.install(monkey)

    print(f"\nconfig.yaml: {size} B · fleet: {args.agents} agents · {args.repeat} reqs/endpoint")

    # One cold parse vs one cached read, for scale.
    cold = statistics.median(
        [_timed(lambda: (home_mod.invalidate_home_config_doc(), home_mod.read_home_config_doc()))]
        + [
            _timed(lambda: (home_mod.invalidate_home_config_doc(), home_mod.read_home_config_doc()))
            for _ in range(20)
        ]
    )
    home_mod.read_home_config_doc()  # prime
    warm = statistics.median([_timed(home_mod.read_home_config_doc) for _ in range(200)])
    print(f"one parse: {cold:.2f} ms · one cached read: {warm * 1000:.0f} µs\n")

    rows = asyncio.run(_sweep(transport, endpoints, args.repeat, watch))

    before = None
    if args.compare:
        real_read = home_mod.read_home_config_doc

        def uncached(override=None):
            home_mod.invalidate_home_config_doc()
            return real_read(override)

        import olano.config.system_settings as ss_mod

        monkey(home_mod, "read_home_config_doc", uncached)
        monkey(ss_mod, "read_home_config_doc", uncached)
        before = asyncio.run(_sweep(transport, endpoints, args.repeat, watch))
        for obj, name, old in reversed(patches[-2:]):
            setattr(obj, name, old)

    width = max(len(r[0]) for r in rows)
    head = f"{'endpoint':{width}}  {'parses':>7} {'median':>10}"
    if before:
        head += f" {'uncached':>10} {'gain':>7}"
    print(head)
    print("-" * len(head))
    tot = tot_before = 0.0
    tot_parses = 0.0
    for i, (label, ms, parses) in enumerate(rows):
        if ms is None:
            print(f"{label:{width}}  {'—':>7} {'not served':>10}")
            continue
        tot += ms
        tot_parses += parses
        line = f"{label:{width}}  {parses:7.1f} {ms:9.1f}ms"
        if before and before[i][1] is not None:
            tot_before += before[i][1]
            line += f" {before[i][1]:9.1f}ms {before[i][1] / ms:6.1f}x"
        print(line)
    print("-" * len(head))
    total = f"{'PAGE LOAD (sum)':{width}}  {tot_parses:7.1f} {tot:9.1f}ms"
    if before:
        total += f" {tot_before:9.1f}ms {tot_before / tot:6.1f}x"
    print(total)

    if tot_parses == 0:
        print(
            "\n✓ Zero parses across the sweep — every read hit the snapshot. That is the"
            "\n  expected result while config.yaml is unchanged; a non-zero count here means"
            "\n  something re-reads the file per call and the list below names it."
        )
    if watch.callers:
        which = "would parse without the cache" if before else "PARSED"
        print(f"\ncallers that {which} (caller → loader), per {args.repeat}-request sweep:")
        for who, n in watch.callers.most_common(12):
            print(f"  {n:4d}x  {who}")

    for obj, name, old in reversed(patches):
        setattr(obj, name, old)
    return 0


def _timed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return (time.perf_counter() - t0) * 1000


if __name__ == "__main__":
    sys.exit(main())
