"""Where deployed-page state lives. Spelled once, here.

* **In the agent's workspace** (``<workspace>/.olano/pages/<slug>/``): the
  build log, the server log and the keeper's pidfile. The agent can read them
  with its own file tools, and the keeper writes them as the sandbox user.
* **In the home, outside every workspace**: static releases
  (``<home>/pages/releases/<slug>/<id>/`` plus a ``CURRENT`` pointer) and the
  supervisor's observed-state snapshot (``<home>/run/pages-services.json``).
  Releases are runtime-owned on purpose: what a viewer is served after a
  successful build cannot be changed by anything running in the sandbox.
"""

from __future__ import annotations

from pathlib import Path

from olano.config.home import get_olano_paths
from olano.config.workspace import WORKSPACE_STATE_DIRNAME


def page_state_dir(workspace_root: Path, slug: str) -> Path:
    return Path(workspace_root) / WORKSPACE_STATE_DIRNAME / "pages" / slug


def service_log_path(workspace_root: Path, slug: str) -> Path:
    return page_state_dir(workspace_root, slug) / "service.log"


def build_log_path(workspace_root: Path, slug: str) -> Path:
    return page_state_dir(workspace_root, slug) / "build.log"


def keeper_pidfile(workspace_root: Path, slug: str) -> Path:
    return page_state_dir(workspace_root, slug) / "keeper.pid"


def releases_root() -> Path:
    return get_olano_paths().pages_dir / "releases"


def page_releases_dir(slug: str) -> Path:
    return releases_root() / slug


def current_release_file(slug: str) -> Path:
    return page_releases_dir(slug) / "CURRENT"


def services_state_path() -> Path:
    return get_olano_paths().run_dir / "pages-services.json"
