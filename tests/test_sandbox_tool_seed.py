"""Shared-tool-layer seeding (opt-in tool sets).

Pins: **no set is seeded by default** — `chinese` used to be on for everyone and
put ~740 MB into every deployment's home whether or not an agent ever called a
Chinese cloud API. Also pins that the seed fires once per configured set,
detached, with the set name sanitized (no shell injection); that it never breaks
a build; and that the seed script contains ONLY official-API packages — never a
ToS-violating / account-ban-risk automation library.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import olano.config.home as home_mod
import olano.core.builder as builder

SEED_SCRIPT = Path(__file__).resolve().parents[1] / "docker" / "olano-sandbox" / "seed-tools.sh"


class _FakeBackend:
    def __init__(self) -> None:
        self.cmds: list[str] = []

    def execute(self, command, **kw):
        self.cmds.append(command)
        return {"stdout": "", "stderr": "", "returncode": 0}


def _patch_sets(monkeypatch, sets):
    monkeypatch.setattr(
        home_mod, "load_home_config", lambda *a, **k: SimpleNamespace(sandbox_tool_sets=sets)
    )


def test_no_tool_set_is_seeded_by_default(lionclaw_home):
    """The dataclass default and the config loader must agree on empty — a
    deployment only pays for a tool set it asked for. They are two separate
    literals, so this pins both."""
    from olano.config.home import HomeConfig, invalidate_home_config_doc, load_home_config

    assert HomeConfig().sandbox_tool_sets == []

    cfg_path = lionclaw_home / "config.yaml"
    cfg_path.write_text("default_agent: merli\n", encoding="utf-8")
    invalidate_home_config_doc(lionclaw_home)
    assert load_home_config(lionclaw_home).sandbox_tool_sets == []

    # An explicit value still wins.
    cfg_path.write_text("sandbox_tool_sets:\n  - chinese\n", encoding="utf-8")
    invalidate_home_config_doc(lionclaw_home)
    assert load_home_config(lionclaw_home).sandbox_tool_sets == ["chinese"]


def test_seed_fires_detached_per_set(monkeypatch):
    _patch_sets(monkeypatch, ["chinese"])
    b = _FakeBackend()
    builder._seed_sandbox_tool_sets(b)
    assert len(b.cmds) == 1
    cmd = b.cmds[0]
    assert "olano-seed-tools chinese" in cmd
    assert cmd.rstrip().endswith("&")  # backgrounded — never blocks the build


def test_seed_set_name_sanitized_against_injection(monkeypatch):
    _patch_sets(monkeypatch, ["chinese; rm -rf / #"])
    b = _FakeBackend()
    builder._seed_sandbox_tool_sets(b)
    cmd = b.cmds[0]
    # Only [a-z0-9_-] survive → the injection collapses to a harmless arg.
    assert "olano-seed-tools chineserm-rf" in cmd
    assert "; rm" not in cmd and "rm -rf" not in cmd


def test_seed_noop_when_disabled(monkeypatch):
    _patch_sets(monkeypatch, [])
    b = _FakeBackend()
    builder._seed_sandbox_tool_sets(b)
    assert b.cmds == []


def test_seed_never_raises(monkeypatch):
    _patch_sets(monkeypatch, ["chinese"])

    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("docker gone")

    builder._seed_sandbox_tool_sets(_Boom())  # must not propagate


def test_seed_script_official_only_no_ban_risk():
    # Only the executable (non-comment) lines matter — the header comment names
    # the excluded libraries on purpose, so ignore comments.
    code = "\n".join(
        ln for ln in SEED_SCRIPT.read_text().splitlines() if not ln.strip().startswith("#")
    ).lower()
    for banned in ("wechaty", "wcferry", "itchat", "xiaohongshu", "xhs-", "redbook"):
        assert banned not in code, f"ToS/ban-risk package leaked into seed: {banned}"


def test_seed_script_has_official_packages():
    text = SEED_SCRIPT.read_text()
    for pkg in (
        "wechatpy",  # WeChat official (Official Account / WeCom / Pay)
        "tccli",  # Tencent Cloud CLI (pip)
        "tencentcloud-sdk-python",
        "aliyun-python-sdk-core",
        "oss2",  # Aliyun OSS
        "cos-python-sdk-v5",  # Tencent COS
        "qiniu",
        "bce-python-sdk",  # Baidu
        "bilibili-api-python",
        "yt-dlp",
    ):
        assert pkg in text, f"expected official package missing from seed: {pkg}"


def test_seed_script_offers_the_coding_set_and_lands_it_in_the_shared_layer():
    """The coding CLIs are 616 MB, so they left the image — this set is the
    supported way back, and it must install into the shared tool layer rather
    than the container's root-owned /usr/local."""
    text = SEED_SCRIPT.read_text()
    assert "  coding)" in text
    for pkg in ("@anthropic-ai/claude-code", "@openai/codex"):
        assert pkg in text, f"coding set no longer installs {pkg}"
    assert "npm_config_prefix" in text
    assert "npm cache clean --force" in text, (
        "the seed must not leave its download cache in the home"
    )


def test_sandbox_image_no_longer_bakes_the_coding_clis():
    """Guards the 616 MB: they belong in the opt-in set, not in an image every
    box re-pulls on every update."""
    dockerfile = (SEED_SCRIPT.parent / "Dockerfile").read_text()
    install_lines = [
        ln
        for ln in dockerfile.splitlines()
        if "npm install -g" in ln and not ln.strip().startswith("#")
    ]
    assert install_lines, "sandbox Dockerfile no longer installs any npm globals"
    joined = "\n".join(install_lines)
    assert "@anthropic-ai/claude-code" not in joined
    assert "@openai/codex" not in joined


def test_tool_layer_path_includes_seed_bin():
    from olano.backends.lionclaw_docker import _TOOL_LAYER_PATH

    assert "/opt/agent-tools/bin" in _TOOL_LAYER_PATH


def _code_lines(path) -> str:
    """A file's non-comment lines — so a mention in prose can't satisfy a pin."""
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.strip().startswith("#"))


def test_sandbox_image_bakes_the_python_alias_and_zstd():
    """A live agent session hit both gaps in one sitting: the bare `python` did
    not resolve (only python3 existed), and `tar --zstd -xf x.tar.zst` died with
    "zstd: Cannot exec". The sandbox agent is non-root with all caps dropped, so
    it cannot apt-get either at run time — both have to be in the image."""
    code = _code_lines(SEED_SCRIPT.parent / "Dockerfile")
    assert "python-is-python3" in code
    assert "zstd" in code


def test_runtime_image_and_install_sh_keep_zstd_parity():
    """The toolkit is meant to be the same however olano was installed (see
    install.sh step 6: "kept in lockstep with the exec-sandbox image"), so a
    command that works in a sandbox must not fail on a host install.

    Only zstd is pinned across all three surfaces: the runtime image's
    python:*-slim base already ships an unversioned `python`, and install.sh
    fixes the alias best-effort per package manager.
    """
    repo = SEED_SCRIPT.parents[2]
    for surface in (repo / "Dockerfile", repo / "install.sh"):
        assert "zstd" in _code_lines(surface), f"zstd missing from {surface.name}"


def test_document_toolchain_spans_the_sandbox_image_and_the_host_lane():
    """The same lockstep, for the tools the document skills reach for. The
    runtime image is deliberately NOT in this list: doc skills run in the
    sandbox (or on the host lane), and the runtime image dropped pandoc and
    poppler for the same reason (root Dockerfile's own comment)."""
    repo = SEED_SCRIPT.parents[2]
    sandbox = _code_lines(SEED_SCRIPT.parent / "Dockerfile")
    host = _code_lines(repo / "install.sh")
    for tool in ("libreoffice", "tesseract", "qpdf", "imagemagick"):
        assert tool in sandbox, f"{tool} missing from the sandbox image"
        assert tool in host, f"{tool} missing from install.sh"


def test_both_images_ship_an_editor_for_the_terminal():
    """The dashboard Terminal lands in the sandbox on a managed box and in the
    runtime container on a self-hosted one; a terminal with no editor cannot
    open a file, and the sandbox agent cannot apt-get one."""
    repo = SEED_SCRIPT.parents[2]
    sandbox = _code_lines(SEED_SCRIPT.parent / "Dockerfile")
    runtime = _code_lines(repo / "Dockerfile")
    assert "vim" in sandbox and "nano" in sandbox
    assert "vim-tiny" in runtime and "nano" in runtime


def test_interpreter_shims_are_appended_never_prepended():
    """Image alias / venv `python` must beat the runtime shim, so the shim dir
    is the LAST PATH entry the exec prefix sets — and the shims needed no
    signature bump of their own (same mount; PATH is per exec), so containers
    that already exist pick them up on their next command."""
    from olano.backends import interpreter_shims as shims
    from olano.backends.lionclaw_docker import _TOOL_LAYER_PATH

    line = shims.shell_path_export()
    assert line == f"export PATH={_TOOL_LAYER_PATH}:$PATH:/opt/agent-tools/shims"


def test_container_signature_schema_matches_the_baked_image_content():
    """The signature hashes the image TAG, so a re-pulled image never recreates
    a container on its own — a schema bump is the only thing that does. Schema 4
    is the document toolchain, schema 5 the everyday shell, schema 6 Node.js 24; pinning the number here means baking something
    new into the image without bumping it fails a test rather than shipping
    binaries no existing box can see."""
    import inspect

    from olano.backends.lionclaw_docker import _container_signature

    assert '"schema": 6' in inspect.getsource(_container_signature)


# The everyday shell, package by package. bookworm-slim ships none of these,
# and every apt line in the image is --no-install-recommends, so not even git's
# Recommends (less, patch, openssh-client) arrive on their own.
EVERYDAY_SHELL_PACKAGES = (
    "zip",
    "unzip",
    "bzip2",
    "xz-utils",
    "p7zip-full",
    "file",
    "less",
    "tree",
    "patch",
    "xxd",
    "dos2unix",
    "bc",
    "gettext-base",
    "procps",
    "psmisc",
    "lsof",
    "htop",
    "iproute2",
    "iputils-ping",
    "bind9-dnsutils",
    "netcat-openbsd",
    "openssh-client",
    "rsync",
    "make",
)


def test_sandbox_image_bakes_the_everyday_shell():
    """`unzip skills/export.zip` died with "command not found" in the dashboard
    Terminal of a managed box, and so did `ps`, `free`, `file` and `less`. The
    agent and the Terminal both land in this image as a non-root user that
    cannot apt-get, so what is not baked in does not exist."""
    code = _code_lines(SEED_SCRIPT.parent / "Dockerfile")
    installed = set(re.findall(r"^\s+([a-z0-9][a-z0-9.+-]+) \\$", code, re.MULTILINE))
    missing = [pkg for pkg in EVERYDAY_SHELL_PACKAGES if pkg not in installed]
    assert not missing, f"sandbox image no longer bakes {missing}"
    # iputils-ping's postinst gives the binary cap_net_raw as a file
    # capability, and under --cap-drop=ALL the kernel refuses to exec a file
    # whose capabilities it cannot grant: ping installed and unrunnable.
    assert "setcap -r /usr/bin/ping" in code


def test_sandbox_image_bakes_the_document_toolchain_by_default():
    """The docx/pptx/xlsx/pdf skills are dead weight without these, and an
    agent (non-root, all caps dropped) cannot install them at run time. Default
    ON: every box PULLS this image rather than building it, so anything behind
    a non-default build arg reaches nobody."""
    code = _code_lines(SEED_SCRIPT.parent / "Dockerfile")
    assert "ARG SANDBOX_EXTRAS=1" in code, "the toolchain is opt-out, not opt-in"
    for pkg in (
        "libreoffice-writer",
        # The engine AND its data. `-eng`/`-osd` are apt *Recommends* and this
        # layer runs --no-install-recommends, so a `tesseract-ocr` on its own
        # installs an OCR engine with no traineddata: it builds clean and dies
        # on the first real page. Asserted by their full names because
        # "tesseract-ocr" is a prefix of both and would pass either way.
        "tesseract-ocr-eng",
        "tesseract-ocr-osd",
        "qpdf",
        "imagemagick",
        # LibreOffice substitutes by name: without metric-compatible faces a
        # .docx set in Arial reflows and the exported PDF is wrong. It pulls in
        # no fonts of its own under --no-install-recommends.
        "fonts-liberation2",
        "fonts-noto-core",
    ):
        assert pkg in code, f"sandbox image no longer bakes {pkg}"
    # The first soffice call builds a user profile (15-40s on a 0.5-CPU sandbox)
    # against a 60s exec timeout, so the image warms it as the user that will
    # run it — without this the first conversion times out, which reads to a
    # user exactly like "not installed".
    assert "--terminate_after_init" in code and "su sandbox" in code
    # pdftk would drag a JRE in for a subset of what qpdf already does.
    assert "pdftk" not in code


def test_skill_fonts_reach_fontconfig_through_the_tool_layer():
    """A skill root is bind-mounted at its HOST path, unknowable when the image
    is built — so the image scans one fixed tool-layer path and the runtime
    writes the faces there from the host side, exactly like the shims."""
    from olano.backends.interpreter_shims import CONTAINER_FONTS_DIR

    conf = (SEED_SCRIPT.parent / "skill-fonts.conf").read_text()
    assert f"<dir>{CONTAINER_FONTS_DIR}</dir>" in conf
    assert CONTAINER_FONTS_DIR.startswith("/opt/agent-tools/")
    code = _code_lines(SEED_SCRIPT.parent / "Dockerfile")
    assert "skill-fonts.conf" in code
