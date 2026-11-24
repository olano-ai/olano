"""Derived avatar renditions, and the cache headers that make them worth having.

An agent's avatar is generated at 1024×1024 by an image model and written to
disk verbatim — measured across fifteen real generations, a median of **888 KB**
of PNG, up to 1.5 MB. The dashboard renders it at 24–44 px on cards, rails and
chat heads, and at 88 px in the bot profile header, its largest use anywhere.
Even at 2× DPR that is 176 px: we were shipping 164× the pixel area, and ~440×
the bytes, of what any surface could draw.

That alone would be a first-load cost. What made it a cost on *every* load was
the second half: the endpoint served `Cache-Control: no-cache`, which obliges
the browser to revalidate before every reuse, and Starlette's ``FileResponse``
sets an ``ETag`` but has **no conditional-request handling** — a revalidation
carrying a matching ``If-None-Match`` came back ``200`` with all 902,251 bytes.
So a roster of eight agents re-downloaded ~7 MB every time a view holding the
roster mounted, which is every navigation back to Overview.

This module fixes both halves, and the two fixes are independent — either alone
leaves most of the cost on the table.

**The rendition.** ``derive_display_variant`` writes one ``avatar-256.webp``
beside the source. One size, not a ``srcset`` ladder: 256 px covers the 88 px
profile header at 2.9× DPR, so nothing on any surface can out-resolve it, and a
ladder would multiply the derivation work and the disk for no visible pixel.
Measured on the same fifteen portraits it costs a **median 7.0 KB** (max 8.8),
against 888 KB for the original — 127× smaller. The 1024 original is kept as the
source of truth so a future larger surface can re-derive from it rather than
from a thumbnail; ``?full=1`` still serves it.

**The caching.** URLs carry ``?v=<stamp>``, a short digest of the source file's
``(mtime_ns, size)`` — one ``stat``, no read, and it moves on every write. A
request that carries a ``v`` is by construction content-addressed, so it earns
``max-age=31536000, immutable``; the existing ``agent_avatar_updated`` SSE still
refreshes instantly because regeneration changes the URL, not the cache entry.

A request *without* ``v`` — an older dashboard bundle, a hand-typed URL, a
platform adapter — must never get that far-future entry, or it pins a stale
avatar for a year with no way to bust it. Those get ``max-age=0,
must-revalidate`` plus a real ``ETag``, and this module answers their
revalidation with a genuine ``304``. That is strictly better than what they had
(a full re-download) and cannot strand anyone.

``private``, never ``public``: on a multi-user install ``/api/agents/*`` sits
behind the session gate (``auth.access_gate``), and a shared proxy cache has no
business holding one tenant's avatar. The browser still caches it either way, so
``private`` costs nothing here.

Derivation is lazy and self-healing: every avatar that already exists on disk
predates this module, so the variant is built on the first request that wants it
and reused thereafter. A source that cannot be decoded is served as-is and
remembered as underivable, so a corrupt file costs one failed decode rather than
one per request.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# The one derived width/height. See the module docstring for why one and why
# this number: the largest avatar the dashboard draws is the 88 px profile
# header (`BotProfilePanel`), and 256 clears it at 2.9× DPR.
DISPLAY_PX = 256

# Encoded into the filename rather than kept only in this constant, so raising
# it yields a *new* file instead of silently reusing a smaller stale one. The
# `-` is what separates a derived rendition from a legitimately uploaded
# `avatar.webp` source, so the glob below can clean up old sizes without ever
# matching the original.
_VARIANT_GLOB = "avatar-*.webp"

# WebP quality. 82 is where the curve flattens for flat-shaded cartoon
# portraits — 90 costs ~60% more bytes for no visible difference at 88 px, and
# 75 starts to band the large areas of soft gradient these avatars are mostly
# made of.
_WEBP_QUALITY = 82

# Sources below this are already smaller than the rendition would be, so
# deriving one would spend CPU and disk to make the download *bigger*. Sized to
# sit just under what a 256 px WebP costs at the top of its measured range.
_ALREADY_SMALL_BYTES = 12 * 1024

# Sources we failed to decode, keyed by (path, mtime_ns, size). Without this a
# corrupt or exotic file re-enters Pillow on every single request. Bounded
# because it is keyed by mtime: a repaired file is a different key and gets a
# fresh attempt, and a pathological home cannot grow it without also growing
# the agent count.
_underivable: set[tuple[str, int, int]] = set()


def variant_path(source: Path) -> Path:
    """Where this source's derived rendition lives."""
    return source.parent / f"avatar-{DISPLAY_PX}.webp"


def content_stamp(path: Path) -> str | None:
    """A short cache-busting digest of the file's identity.

    Deliberately ``(mtime_ns, size)`` and not the file's bytes: this is called
    once per agent inside the roster loop, on every ``GET /api/agents``, and
    hashing ~900 KB per agent per request to build a URL would cost more than
    the download it saves. Any write moves both inputs.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return hashlib.sha1(f"{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()[:12]


def clear_variants(agent_dir: Path) -> None:
    """Drop every derived rendition in an agent's directory.

    Called wherever the source avatar is replaced or deleted. Globs rather than
    naming one file so a `DISPLAY_PX` bump doesn't strand the previous size on
    disk forever.
    """
    for stale in agent_dir.glob(_VARIANT_GLOB):
        try:
            stale.unlink()
        except OSError:
            logger.debug("Could not remove stale avatar rendition %s", stale, exc_info=True)


def derive_display_variant(source: Path) -> Path | None:
    """Build (or refresh) the display rendition for ``source``.

    Returns the rendition's path, or ``None`` when the caller should just serve
    the source — because it is already small enough to be pointless to shrink,
    or because it could not be decoded. Never raises: a thumbnail that cannot be
    made is a missed optimisation, not a failed request.
    """
    try:
        src_stat = source.stat()
    except OSError:
        return None

    key = (str(source), src_stat.st_mtime_ns, src_stat.st_size)
    if key in _underivable:
        return None

    target = variant_path(source)
    # Up to date already? `>=` and not `>`: a derivation that lands in the same
    # nanosecond as the write is still current.
    try:
        if target.stat().st_mtime_ns >= src_stat.st_mtime_ns:
            return target
    except OSError:
        pass

    try:
        from PIL import Image, ImageOps

        with Image.open(source) as opened:
            # A phone upload carries its rotation in EXIF; the browser honours
            # it on the original, so the rendition must too or the two disagree.
            im = ImageOps.exif_transpose(opened) or opened
            if im.width <= DISPLAY_PX and im.height <= DISPLAY_PX:
                if src_stat.st_size <= _ALREADY_SMALL_BYTES:
                    return None
                rendition = im.copy()
            else:
                rendition = ImageOps.contain(im, (DISPLAY_PX, DISPLAY_PX), Image.Resampling.LANCZOS)
            # Animated sources collapse to their first frame. An avatar drawn at
            # 40 px in a rail is not a place anyone watches an animation, and
            # keeping one would preserve exactly the weight this exists to shed.
            rendition = rendition.convert("RGBA" if _has_alpha(rendition) else "RGB")

            # Write-then-rename: two requests can race into deriving the same
            # avatar, and a half-written WebP served to the loser of that race
            # is a broken image in the rail.
            #
            # The suffix must be unique per *call*, not per process. These run
            # on `asyncio.to_thread`, so a shared temp name lets one thread
            # truncate the file another is about to rename into place — which
            # publishes a partial image, the exact failure the rename avoids.
            # Two tabs opening Overview on a box with no renditions yet is all
            # it takes.
            tmp = target.with_suffix(f".webp.{uuid.uuid4().hex}.tmp")
            try:
                rendition.save(tmp, "WEBP", quality=_WEBP_QUALITY, method=6)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
    except Exception:
        logger.warning("Could not derive display avatar from %s", source, exc_info=True)
        _underivable.add(key)
        return None

    # A rendition that came out heavier than its source helps nobody — small
    # already-optimised uploads can land here. Serve the source instead.
    try:
        if target.stat().st_size >= src_stat.st_size:
            target.unlink(missing_ok=True)
            return None
    except OSError:
        pass
    return target


def _has_alpha(im: object) -> bool:
    mode = getattr(im, "mode", "")
    return mode in ("RGBA", "LA", "PA") or "transparency" in getattr(im, "info", {})


def resolve_display_path(source: Path) -> Path:
    """The path to serve for a normal (non-``?full=1``) avatar request."""
    return derive_display_variant(source) or source


def cache_headers(path: Path, versioned: bool) -> dict[str, str]:
    """Validator + freshness headers for a media response.

    ``versioned`` says the request carried a ``?v=`` stamp, which makes the URL
    content-addressed and therefore safe to pin. Without it the response must
    stay revalidating — see the module docstring on why a far-future entry on a
    stable URL is a trap rather than an optimisation.
    """
    headers = {
        "Cache-Control": (
            "private, max-age=31536000, immutable"
            if versioned
            else "private, max-age=0, must-revalidate"
        )
    }
    etag = file_etag(path)
    if etag:
        headers["ETag"] = etag
    return headers


def file_etag(path: Path) -> str | None:
    """A strong-form ETag over the file's identity, matching ``content_stamp``."""
    stamp = content_stamp(path)
    return f'"{stamp}"' if stamp else None


def is_not_modified(request_headers: object, etag: str | None) -> bool:
    """Whether an ``If-None-Match`` on the request already matches ``etag``.

    Starlette's ``FileResponse`` sends an ``ETag`` and then ignores the client's
    ``If-None-Match`` entirely, answering every revalidation with the full body.
    This is the missing half.
    """
    if not etag:
        return False
    header = ""
    getter = getattr(request_headers, "get", None)
    if callable(getter):
        header = getter("if-none-match") or ""
    if not header:
        return False
    # RFC 9110 §13.1.2: a comma-separated list, `*` matches anything, and a weak
    # validator compares equal to its strong twin under the weak comparison a
    # 304 uses.
    candidates = [c.strip() for c in header.split(",")]
    if "*" in candidates:
        return True
    return any(c.removeprefix("W/") == etag.removeprefix("W/") for c in candidates if c)
