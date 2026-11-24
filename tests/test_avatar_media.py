"""Avatar renditions + media caching.

Two defects are pinned here, and they are independent — the suite would still
be worth having if either were fixed alone.

1. The dashboard was served the 1024×1024 image model output (a measured median
   of 888 KB) to draw a 40 px circle.
2. Every render re-downloaded it in full: the endpoint sent `Cache-Control:
   no-cache`, and Starlette's `FileResponse` emits an `ETag` and then ignores
   the client's `If-None-Match`, so a revalidation came back 200 with the whole
   body.

The regression that would hurt most is silent: a rendition that stops being
derived, or a `?v=` stamp that stops moving when the file changes, both leave a
working dashboard that is merely slow again — so the size assertions here are
deliberately in *bytes*, not "is smaller".
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from olano.runtime import avatar_media


def _portrait(size: int = 1024, *, transparent: bool = False, seed: int = 0) -> bytes:
    """A PNG shaped like what the avatar generator produces.

    Two things this must not be. Not a flat fill — that compresses to a few
    hundred bytes in any format, and every size assertion below would pass for
    the wrong reason. And not per-pixel noise either, which is the opposite
    mistake: it is incompressible, costs ~36 KB as WebP, and would set the byte
    bounds five times higher than a real avatar ever reaches.

    So: smooth gradients plus a few large flat shapes, which is what a
    flat-shaded cartoon portrait actually looks like to an encoder. `seed`
    shifts the palette so two calls produce genuinely different bytes.

    `transparent` gives it a *varying* alpha channel. A fully-opaque one would
    not survive the round trip — Pillow's WebP encoder drops an all-255 alpha
    because it carries no information, which is correct and would make an
    alpha-preservation test fail for a reason that has nothing to do with us.
    """
    mode = "RGBA" if transparent else "RGB"
    im = Image.new(mode, (size, size))
    draw = ImageDraw.Draw(im)
    for y in range(size):
        shade = int(255 * y / size)
        draw.line(
            [(0, y), (size, y)],
            fill=((shade + seed) % 256, (200 - shade // 2) % 256, (120 + seed) % 256)
            + ((255,) if transparent else ()),
        )
    draw.ellipse(
        [size * 0.2, size * 0.15, size * 0.8, size * 0.75],
        fill=((240 + seed) % 256, 190, 150) + ((255,) if transparent else ()),
    )
    draw.ellipse(
        [size * 0.35, size * 0.35, size * 0.44, size * 0.44],
        fill=(30, 30, 40, 255)[: 4 if transparent else 3],
    )
    draw.ellipse(
        [size * 0.56, size * 0.35, size * 0.65, size * 0.44],
        fill=(30, 30, 40, 255)[: 4 if transparent else 3],
    )
    if transparent:
        # A real cut-out avatar: circular subject, transparent corners.
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size, size], fill=255)
        im.putalpha(mask)
    buf = BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def agent_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents" / "sunny"
    d.mkdir(parents=True)
    return d


# ── derivation ───────────────────────────────────────────────────────────────


def test_rendition_is_a_fraction_of_the_source(agent_dir: Path):
    """The whole point: what the dashboard downloads must be tiny.

    The bound is absolute rather than relative because "smaller than the 888 KB
    source" is a bar a 300 KB regression clears comfortably. 16 KB is set from
    the real thing: run through this module, fifteen captured generations from a
    debug run come out at a median of 7.0 KB and a maximum of 8.8, so this
    leaves headroom for a heavier portrait without leaving room for a format or
    resize regression.
    """
    source = agent_dir / "avatar.png"
    source.write_bytes(_portrait())

    variant = avatar_media.derive_display_variant(source)

    assert variant is not None
    assert variant.name == "avatar-256.webp"
    assert variant.stat().st_size < 16 * 1024
    assert variant.stat().st_size < source.stat().st_size
    # And it really is the smaller image, not a copy under a new name.
    with Image.open(variant) as im:
        assert max(im.size) == avatar_media.DISPLAY_PX


def test_rendition_still_out_resolves_the_largest_place_it_is_drawn(agent_dir: Path):
    """256 px is not arbitrary — it must clear the 88 px profile header at 2×.

    If someone shrinks `DISPLAY_PX` to save bytes, this is the constraint they
    are trading against, stated in the units that matter.
    """
    largest_css_px_in_dashboard = 88
    assert avatar_media.DISPLAY_PX >= largest_css_px_in_dashboard * 2


def test_derivation_is_reused_not_repeated(agent_dir: Path):
    source = agent_dir / "avatar.png"
    source.write_bytes(_portrait())

    first = avatar_media.derive_display_variant(source)
    assert first is not None
    stamp = first.stat().st_mtime_ns

    second = avatar_media.derive_display_variant(source)

    assert second == first
    assert second.stat().st_mtime_ns == stamp, "re-derived an up-to-date rendition"


def test_a_replaced_source_is_re_derived(agent_dir: Path):
    """A stale rendition would show the previous agent's face indefinitely."""
    source = agent_dir / "avatar.png"
    source.write_bytes(_portrait())
    avatar_media.derive_display_variant(source)

    stale = avatar_media.variant_path(source).read_bytes()

    source.write_bytes(_portrait(seed=90))
    variant = avatar_media.derive_display_variant(source)

    assert variant is not None
    assert variant.read_bytes() != stale, "rendition still describes the previous source"


def test_transparency_survives(agent_dir: Path):
    """A cut-out avatar sits in a circular frame; losing alpha fills it black."""
    source = agent_dir / "avatar.png"
    source.write_bytes(_portrait(transparent=True))

    variant = avatar_media.derive_display_variant(source)

    assert variant is not None
    with Image.open(variant) as im:
        assert im.mode == "RGBA"
        assert im.getchannel("A").getextrema()[0] == 0, "corners are no longer transparent"


def test_an_already_small_source_is_left_alone(agent_dir: Path):
    """Deriving from a 64 px icon would spend CPU to produce a bigger file."""
    source = agent_dir / "avatar.png"
    im = Image.new("RGB", (64, 64), (20, 120, 200))
    buf = BytesIO()
    im.save(buf, "PNG")
    source.write_bytes(buf.getvalue())

    assert avatar_media.derive_display_variant(source) is None
    assert not avatar_media.variant_path(source).exists()


def test_an_undecodable_source_degrades_to_itself(agent_dir: Path):
    """A corrupt avatar must cost a missed optimisation, never a failed request."""
    source = agent_dir / "avatar.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not actually a png" * 8)

    assert avatar_media.derive_display_variant(source) is None
    assert avatar_media.resolve_display_path(source) == source


def test_a_broken_source_is_not_re_decoded_every_request(agent_dir: Path, monkeypatch):
    """Without the memo, one bad file costs a Pillow decode per page render."""
    source = agent_dir / "avatar.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage" * 16)
    avatar_media.derive_display_variant(source)

    calls: list[object] = []
    real_open = Image.open

    def counting_open(*a, **kw):
        calls.append(a)
        return real_open(*a, **kw)

    monkeypatch.setattr(Image, "open", counting_open)
    avatar_media.derive_display_variant(source)

    assert calls == [], "re-attempted a source already known to be undecodable"


def test_clear_variants_spares_an_uploaded_webp_source(agent_dir: Path):
    """`avatar.webp` is a legal *source*; only `avatar-<N>.webp` is derived.

    The glob is what separates them, so a hyphen slipping out of it would have
    `clear_variants` delete the user's uploaded avatar.
    """
    source = agent_dir / "avatar.webp"
    source.write_bytes(_portrait()[:0] or b"")
    Image.new("RGB", (512, 512), (9, 9, 9)).save(source, "WEBP")
    (agent_dir / "avatar-256.webp").write_bytes(b"derived")
    (agent_dir / "avatar-128.webp").write_bytes(b"derived, older size")

    avatar_media.clear_variants(agent_dir)

    assert source.exists(), "clear_variants ate the uploaded source"
    assert not (agent_dir / "avatar-256.webp").exists()
    assert not (agent_dir / "avatar-128.webp").exists()


# ── cache stamps and validators ──────────────────────────────────────────────


def test_stamp_moves_when_the_file_changes(agent_dir: Path):
    """The stamp *is* the cache invalidation. If it stops moving, a regenerated
    avatar stays invisible behind a year-long cache entry."""
    source = agent_dir / "avatar.png"
    source.write_bytes(b"a" * 5000)
    before = avatar_media.content_stamp(source)

    source.write_bytes(b"b" * 6000)
    after = avatar_media.content_stamp(source)

    assert before and after and before != after


def test_stamp_is_stable_for_an_unchanged_file(agent_dir: Path):
    source = agent_dir / "avatar.png"
    source.write_bytes(b"a" * 5000)

    assert avatar_media.content_stamp(source) == avatar_media.content_stamp(source)


def test_a_missing_file_has_no_stamp(agent_dir: Path):
    assert avatar_media.content_stamp(agent_dir / "nope.png") is None


def test_versioned_urls_are_cacheable_and_bare_ones_are_not(agent_dir: Path):
    """The asymmetry is the safety property, not an inconsistency.

    A stamped URL is content-addressed and safe to pin for a year. A bare one is
    a stable URL: pinning *that* would strand an old dashboard bundle on a stale
    avatar with nothing able to bust it.
    """
    source = agent_dir / "avatar.png"
    source.write_bytes(b"x" * 4096)

    versioned = avatar_media.cache_headers(source, versioned=True)
    bare = avatar_media.cache_headers(source, versioned=False)

    assert "immutable" in versioned["Cache-Control"]
    assert "max-age=31536000" in versioned["Cache-Control"]
    assert "immutable" not in bare["Cache-Control"]
    assert "must-revalidate" in bare["Cache-Control"]
    # Both carry a validator — that is what lets the bare URL answer with a 304.
    assert versioned["ETag"] == bare["ETag"]


@pytest.mark.parametrize(
    "header,expected",
    [
        ('"abc"', True),
        ('W/"abc"', True),  # weak comparison is the one a 304 uses
        ("*", True),
        ('"other", "abc"', True),
        ('"other"', False),
        ("", False),
    ],
)
def test_if_none_match_matching(header: str, expected: bool):
    assert avatar_media.is_not_modified({"if-none-match": header}, '"abc"') is expected


def test_no_etag_never_claims_not_modified():
    assert avatar_media.is_not_modified({"if-none-match": "*"}, None) is False


def test_concurrent_derivations_never_publish_a_partial_image(agent_dir: Path):
    """Renditions are built on `asyncio.to_thread`, so two requests for the same
    not-yet-derived avatar genuinely run at once — which is the state every box
    is in on the first page load after this ships.

    The rename is only atomic if the thing being renamed is private to the
    caller. With a temp name shared across threads (a bare pid, say), one
    thread's cleanup deletes the file another is about to rename into place, its
    `replace` raises, and the handler for that adds the source to `_underivable`
    — so the rendition is not merely missed once, it is switched **off for the
    life of the process** and every later request serves the full ~888 KB PNG.
    The whole optimisation disables itself, silently, under precisely the
    concurrency of a first load.
    """
    from concurrent.futures import ThreadPoolExecutor

    source = agent_dir / "avatar.png"
    source.write_bytes(_portrait())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: avatar_media.derive_display_variant(source), range(8)))

    assert all(r is not None for r in results)
    # The published file must be a complete, decodable image every time.
    with Image.open(avatar_media.variant_path(source)) as im:
        im.load()
        assert max(im.size) == avatar_media.DISPLAY_PX
    # And nothing may be left behind: a stranded temp file is disk nobody sweeps.
    assert list(agent_dir.glob("*.tmp")) == []
