"""``OLANO_*`` and ``LIONCLAW_*`` are two spellings of one environment variable.

The package was renamed from lionclaw to olano and its configuration variables
(about 170 of them) are still read under the ``LIONCLAW_`` prefix. Rather than
touching every reader, :func:`apply_env_aliases` mirrors each spelling onto the
other, in ``os.environ``, at process start:

* ``OLANO_<X>`` set, ``LIONCLAW_<X>`` unset: ``LIONCLAW_<X>`` is written, so
  every existing reader sees a value set under the new name.
* ``LIONCLAW_<X>`` set, ``OLANO_<X>`` unset: ``OLANO_<X>`` is written, so a
  new reader, a child process or a shell script can use the new name.
* Both set to the same value: nothing to do.
* Both set to different values: neither is touched and ``LIONCLAW_<X>`` wins,
  because every reader still reads it. An explicitly set legacy variable is
  never overwritten; the disagreement is logged once per name at DEBUG level.

"Set" means present and non-empty. An empty value counts as unset, the rule
``config/bootstrap_env.py`` applies and the one nearly every reader applies
through ``os.environ.get(name, "").strip() or default``, so an empty spelling
never shadows a set one and an empty value is never copied.

The function runs from ``olano/__init__.py``, so the CLI, the runtime and the
test suite all get it before any reader, and again from ``cli/main._load_env``
after the ``.env`` and bootstrap files are overlaid onto the environment. It
remembers the mirrors it wrote (``_written``): on a later pass a mirror still
holding the value this module gave it is refreshed from its source rather than
defended as an explicit setting, which is what lets a ``.env`` written as
``OLANO_<X>`` override a shell ``OLANO_<X>`` exactly as the legacy spelling
always could. Idempotent; one pass over the environment.

Excluded (:data:`EXCLUDED_SUFFIXES`):

* ``HOME``. ``config/home.py`` owns both ``OLANO_HOME`` and ``LIONCLAW_HOME``
  with its own documented precedence (the new name outranks the legacy one,
  pinned by ``tests/test_home.py``) and ``set_olano_home`` already writes both,
  so mirroring them here would add a second, contradictory rule to the one
  variable that already has one.
* The deployment layer's own names (:data:`DEPLOYMENT_SUFFIXES`). The compose
  file, the deploy scripts and the managed update agent write ``OLANO_*``
  variables into a box's ``.env`` that were never runtime settings: the image
  pins (``OLANO_BACKEND_IMAGE`` / ``OLANO_DASHBOARD_IMAGE`` /
  ``OLANO_SANDBOX_IMAGE``, a registry tag the update agent pins so a later
  ``docker compose up`` keeps the same images), the host-side home directory
  name (``OLANO_HOME_DIR``), the Postgres sidecar's role/database/volume name
  (``OLANO_PG_NAME``) and the bootstrap's pull flag (``OLANO_PULL_IMAGES``).
  They pre-date the alias scheme, and one of them has a legacy twin the runtime
  reads for something else: ``LIONCLAW_SANDBOX_IMAGE`` is the image the runtime
  spawns per agent, defaulting to the local ``olano-sandbox:latest`` tag that
  the update agent re-tags after every pull. Mirroring the pin onto it made the
  runtime spawn ``ghcr.io/...:<tag>`` instead: a different image name on every
  update, which recreates every persistent sandbox (the image is part of the
  container signature) and, between the stack restart and the agent's pull, a
  private image the runtime cannot fetch, so agents came up with no shell.
  ``tests/test_env_aliases.py`` scans ``deploy/`` for ``OLANO_*`` names whose
  legacy twin the code reads, so a new collision fails the suite instead of
  reaching a box.

Secrets: a mirror is a second copy of the same value under a name an
exact-name denylist does not know. :func:`spellings` returns every spelling of
a name, and ``security/shell_guard._ALWAYS_STRIP_ENV`` is built over it so an
agent shell can read neither copy.

Imported on every ``import olano``: stdlib only, no ``re``, and ``logging`` is
loaded lazily on the first conflict.
"""

from __future__ import annotations

import os

NEW_PREFIX = "OLANO_"
LEGACY_PREFIX = "LIONCLAW_"

#: ``OLANO_<X>`` names the deployment layer writes into a box's ``.env`` (image
#: pins, the host-side home directory name, the sidecar's Postgres name, the
#: bootstrap pull flag). Not runtime settings, so never mirrored: see the
#: module docstring for the sandbox-image collision that made this necessary.
#: ``BROWSER_IMAGE`` has the same shape: ``OLANO_BROWSER_IMAGE`` is the registry
#: reference the live browser is pulled from, ``LIONCLAW_BROWSER_IMAGE`` the
#: local tag the runtime runs (olano/browser/sidecar.py).
DEPLOYMENT_SUFFIXES: frozenset[str] = frozenset(
    {
        "BACKEND_IMAGE",
        "DASHBOARD_IMAGE",
        "SANDBOX_IMAGE",
        "BROWSER_IMAGE",
        "HOME_DIR",
        "PG_NAME",
        "PULL_IMAGES",
    }
)

#: Suffixes that are never mirrored (see the module docstring).
EXCLUDED_SUFFIXES: frozenset[str] = frozenset({"HOME"}) | DEPLOYMENT_SUFFIXES

_SUFFIX_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")

# Mirrors this module wrote, name -> value, so a later pass can tell its own
# copy from a value somebody set on purpose.
_written: dict[str, str] = {}
# Suffixes already reported as disagreeing, so the DEBUG line fires once.
_reported: set[str] = set()


def alias_suffix(name: str) -> str | None:
    """Return ``<X>`` for an aliasable ``OLANO_<X>`` / ``LIONCLAW_<X>`` name, else ``None``."""
    for prefix in (NEW_PREFIX, LEGACY_PREFIX):
        if name.startswith(prefix):
            suffix = name[len(prefix) :]
            break
    else:
        return None
    if not suffix or suffix[0] == "_" or suffix in EXCLUDED_SUFFIXES:
        return None
    if not _SUFFIX_CHARS.issuperset(suffix):
        return None
    return suffix


def spellings(name: str) -> tuple[str, ...]:
    """Every spelling of *name*: both prefixes for an aliasable name, else ``(name,)``."""
    suffix = alias_suffix(name)
    if suffix is None:
        return (name,)
    return (LEGACY_PREFIX + suffix, NEW_PREFIX + suffix)


def _explicit(name: str) -> str:
    """The value of *name* when it was set on purpose, else ``""``.

    Absent and empty both read as unset, and so does a mirror this module wrote
    that nothing has changed since.
    """
    value = os.environ.get(name, "")
    if not value or _written.get(name) == value:
        return ""
    return value


def _report_conflict(suffix: str, legacy_name: str, new_name: str) -> None:
    if suffix in _reported:
        return
    _reported.add(suffix)
    import logging

    logging.getLogger(__name__).debug(
        "%s and %s are both set and differ; %s wins (a legacy variable is never overwritten)",
        new_name,
        legacy_name,
        legacy_name,
    )


def apply_env_aliases() -> dict[str, str]:
    """Mirror ``OLANO_*`` and ``LIONCLAW_*`` onto each other in ``os.environ``.

    Returns the names written on this pass, each with the value it received.
    Safe to call any number of times.
    """
    written: dict[str, str] = {}
    seen: set[str] = set()
    for key in list(os.environ):
        suffix = alias_suffix(key)
        if suffix is None or suffix in seen:
            continue
        seen.add(suffix)
        legacy_name = LEGACY_PREFIX + suffix
        new_name = NEW_PREFIX + suffix
        legacy = _explicit(legacy_name)
        new = _explicit(new_name)
        if legacy and new:
            if legacy != new:
                _report_conflict(suffix, legacy_name, new_name)
            continue
        if legacy:
            mirror, value = new_name, legacy
        elif new:
            mirror, value = legacy_name, new
        else:
            continue
        if os.environ.get(mirror) != value:
            os.environ[mirror] = value
            written[mirror] = value
        _written[mirror] = value
    return written
