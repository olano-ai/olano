#!/usr/bin/env python3
"""olano-browser: the in-container side of an agent's live browser.

Standard library only, so the image needs nothing but Chromium, Xvfb and a
Python interpreter. Three entry points:

    browserd.py           run the browser: a virtual display (Xvfb, when the
                          image has it) and Chromium on it, restarted if it dies
    browserd.py pipe      splice stdin/stdout onto Chromium's DevTools socket
    browserd.py version   print Chromium's /json/version (health + ws path)

Chromium's DevTools endpoint listens on 127.0.0.1 inside the container and is
never published. The runtime reaches it only through ``docker exec -i <ctr>
browserd.py pipe``, one exec per DevTools connection, the same way it already
reaches a sandbox for the Terminal. That is the whole access model: nothing
else on the docker network can drive this browser, so no token is needed, and
no network path from the runtime container to this one has to exist (on a
managed box the runtime sits on the compose network and this container on the
sandbox lane, which docker isolates from each other).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

CDP_PORT = int(os.environ.get("OLANO_BROWSER_CDP_PORT", "9222"))
PROFILE = os.environ.get("OLANO_BROWSER_PROFILE", "/profile")
WIDTH = int(os.environ.get("OLANO_BROWSER_WIDTH", "1280"))
HEIGHT = int(os.environ.get("OLANO_BROWSER_HEIGHT", "800"))
DISPLAY_NUM = os.environ.get("OLANO_BROWSER_DISPLAY_NUM", "99")
# "auto" (default): a real display under Xvfb when the image ships it, else
# Chromium's new headless mode. "headless" forces the latter.
DISPLAY_MODE = os.environ.get("OLANO_BROWSER_DISPLAY", "auto").strip().lower()

_CHROME_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "/ms-playwright/chromium-*/chrome-linux/chrome",
    "/opt/pw-browsers/chromium-*/chrome-linux/chrome",
)

# Profile lock files a previous container (or a crash) leaves behind. Chromium
# refuses to start on a profile whose lock names another host, and every
# container has a different hostname, so a restarted sidecar would otherwise
# never come back up on the agent's saved profile.
_STALE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def log(msg: str) -> None:
    print(f"[olano-browser] {msg}", file=sys.stderr, flush=True)


def find_chrome() -> str:
    explicit = os.environ.get("OLANO_BROWSER_BIN", "").strip()
    if explicit:
        return explicit
    for cand in _CHROME_CANDIDATES:
        if "*" in cand:
            hits = sorted(glob.glob(cand))
            if hits:
                return hits[-1]
            continue
        found = shutil.which(cand)
        if found:
            return found
    raise SystemExit("olano-browser: no Chromium binary found in this image")


METADATA_RESOLVER_RULES = (
    "MAP 169.254.* ~NOTFOUND, MAP 100.100.100.200 ~NOTFOUND, "
    "MAP metadata.google.internal ~NOTFOUND, MAP metadata ~NOTFOUND, "
    "MAP [fd00:ec2::254] ~NOTFOUND, MAP [::ffff:a9fe:a9fe] ~NOTFOUND"
)


def chrome_flags(headless: bool, user_agent: str | None, proxied: bool = False) -> list[str]:
    flags = [
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={PROFILE}",
        f"--window-size={WIDTH},{HEIGHT}",
        "--window-position=0,0",
        "--force-device-scale-factor=1",
        # The container is the sandbox: it runs as uid 1000 with every
        # capability dropped, and docker's default seccomp profile refuses the
        # user namespaces Chromium's own sandbox needs.
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--use-mock-keychain",
        # Chromium's own background traffic (component updates, field trials,
        # sync, safe-browsing list fetches, DNS prefetch) is egress nobody
        # asked for, and the per-agent allowlist would refuse it anyway.
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--disable-default-apps",
        "--disable-breakpad",
        "--metrics-recording-only",
        "--dns-prefetch-disable",
        "--no-pings",
        "--disable-domain-reliability",
        "--disable-client-side-phishing-detection",
        "--safebrowsing-disable-auto-update",
        "--disable-field-trial-config",
        "--disable-features=Translate,MediaRouter,OptimizationHints,"
        "AutofillServerCommunication,InterestFeedContentSuggestions,"
        "CertificateTransparencyComponentUpdater",
        # navigator.webdriver stays false while a client drives the browser.
        "--disable-blink-features=AutomationControlled",
        # A page asking for a location, a camera, a microphone or leave to
        # notify would get a prompt bubble on the virtual display, which no
        # viewer sees, and wait on it forever. Refused at once instead; the
        # live view tells the person what was asked (olano/browser/live.py,
        # arm_page_permissions).
        "--deny-permission-prompts",
        # The cloud metadata endpoints, refused inside Chromium on every hop
        # (a redirect never reaches the runtime's request guard). Same rules as
        # olano/browser/launch.py METADATA_RESOLVER_RULES.
        f"--host-resolver-rules={METADATA_RESOLVER_RULES}",
    ]
    if proxied:
        # The agent's contexts go through a proxy (OLANO_BROWSER_PROXIED, set
        # by the runtime): WebRTC must not reveal this machine's own address
        # over UDP. Only then: without a proxy the flag just breaks calls for
        # a person who takes control.
        flags.append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    if headless:
        flags.append("--headless=new")
        if user_agent:
            flags.append(f"--user-agent={user_agent}")
    flags.append("about:blank")
    return flags


def headless_user_agent(chrome: str) -> str | None:
    """New headless mode still announces ``HeadlessChrome`` in its UA, which a
    large share of sites answer with a block page. Present the version the
    binary really is, without the marker."""
    try:
        out = subprocess.run(
            [chrome, "--version"], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception:
        return None
    version = next((tok for tok in out.split() if tok[:1].isdigit()), "")
    if not version:
        return None
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def clear_stale_locks() -> None:
    os.makedirs(PROFILE, exist_ok=True)
    for name in _STALE_LOCKS:
        path = os.path.join(PROFILE, name)
        try:
            if os.path.islink(path) or os.path.exists(path):
                os.unlink(path)
        except OSError:
            pass


def start_display() -> subprocess.Popen | None:
    if DISPLAY_MODE == "headless" or not shutil.which("Xvfb"):
        return None
    display = f":{DISPLAY_NUM}"
    sock = f"/tmp/.X11-unix/X{DISPLAY_NUM}"
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", f"{WIDTH}x{HEIGHT}x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if os.path.exists(sock):
            os.environ["DISPLAY"] = display
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    log("Xvfb did not come up; falling back to headless")
    try:
        proc.kill()
    except Exception:
        pass
    return None


def run() -> int:
    chrome = find_chrome()
    stopping = threading.Event()
    children: list[subprocess.Popen] = []

    def _stop(signum, _frame):
        stopping.set()
        for child in children:
            try:
                child.terminate()
            except Exception:
                pass

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    display = start_display()
    if display is not None:
        children.append(display)
    headless = display is None
    user_agent = headless_user_agent(chrome) if headless else None
    proxied = os.environ.get("OLANO_BROWSER_PROXIED", "").strip() == "1"
    log(f"chromium={chrome} display={'headless' if headless else os.environ['DISPLAY']}")

    restarts: list[float] = []
    while not stopping.is_set():
        clear_stale_locks()
        proc = subprocess.Popen([chrome, *chrome_flags(headless, user_agent, proxied)])
        children.append(proc)
        code = proc.wait()
        children.remove(proc)
        if stopping.is_set():
            break
        if display is not None and display.poll() is not None:
            log("display server exited; stopping")
            return 1
        now = time.time()
        restarts = [t for t in restarts if now - t < 600] + [now]
        if len(restarts) > 10:
            log("chromium keeps exiting; giving up so the runtime can recreate the container")
            return 1
        delay = min(30, 2 ** (len(restarts) - 1))
        log(f"chromium exited ({code}); restarting in {delay}s")
        stopping.wait(delay)

    for child in children:
        try:
            child.wait(timeout=10)
        except Exception:
            child.kill()
    return 0


def _connect(deadline_s: float = 20.0) -> socket.socket:
    deadline = time.time() + deadline_s
    while True:
        try:
            return socket.create_connection(("127.0.0.1", CDP_PORT), timeout=5)
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.2)


def pipe() -> int:
    """Byte-for-byte relay between this exec's stdio and the DevTools socket."""
    try:
        sock = _connect()
    except OSError as exc:
        log(f"devtools endpoint unreachable: {exc}")
        return 2
    sock.settimeout(None)
    stdin = sys.stdin.buffer.raw if hasattr(sys.stdin.buffer, "raw") else sys.stdin.buffer
    stdout = sys.stdout.buffer
    done = threading.Event()

    def upstream() -> None:
        try:
            while True:
                chunk = stdin.read(65536)
                if not chunk:
                    break
                sock.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            done.set()

    def downstream() -> None:
        try:
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                stdout.write(chunk)
                stdout.flush()
        except OSError:
            pass
        finally:
            done.set()

    threading.Thread(target=upstream, daemon=True).start()
    reader = threading.Thread(target=downstream, daemon=True)
    reader.start()
    done.wait()
    reader.join(timeout=2)
    try:
        sock.close()
    except OSError:
        pass
    return 0


def version() -> int:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + float(os.environ.get("OLANO_BROWSER_VERSION_WAIT", "20"))
    while True:
        try:
            with opener.open(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            print(json.dumps(body))
            return 0
        except Exception as exc:
            if time.time() > deadline:
                log(f"devtools endpoint not ready: {exc}")
                return 1
            time.sleep(0.3)


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "run"
    if cmd == "run":
        return run()
    if cmd == "pipe":
        return pipe()
    if cmd == "version":
        return version()
    log(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
