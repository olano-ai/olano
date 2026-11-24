"""Agent avatars, sprite sheets and character generation.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import os
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request

from olano.runtime import api as _api
from olano.runtime import avatar_media


def register_avatar_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the avatar routes to ``app``."""
    # ── Avatar (uploaded or generated) ───────────────────────────────────────

    def _agent_avatar_path(name: str) -> Path | None:
        """Return the on-disk path of this agent's avatar image, if any.
        Looks for `avatar.png|jpg|jpeg|webp` next to the agent.json5.

        This is always the *source* image. The smaller rendition the dashboard
        actually renders is derived from it on demand — see `avatar_media`."""
        try:
            cfg_path = _api._agent_config_path(name)
        except FileNotFoundError:
            return None
        agent_dir = cfg_path.parent
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = agent_dir / f"avatar.{ext}"
            if p.exists():
                return p
        return None

    _MEDIA_MIME = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
    }

    def _agent_avatar_url(name: str) -> str | None:
        """The dashboard-visible URL for an agent's avatar, when one exists.

        Carries a `?v=` stamp of the source file's identity. That is what lets
        the response be cached for a year: regenerating the avatar changes the
        URL, so the existing `agent_avatar_updated` SSE still refreshes every
        rendered head instantly without anyone busting a cache entry."""
        path = _agent_avatar_path(name)
        if path is None:
            return None
        stamp = avatar_media.content_stamp(path)
        return f"/api/agents/{name}/avatar" + (f"?v={stamp}" if stamp else "")

    def _media_response(path: Path, request: Request) -> Any:
        """Serve a media file with real validators and honest freshness.

        Starlette's `FileResponse` emits an `ETag` and then ignores the client's
        `If-None-Match`, so every revalidation came back as a full body. This
        answers the revalidation properly and, for a URL that carries a `?v=`
        stamp, lets the browser skip it altogether."""
        from starlette.responses import FileResponse, Response

        versioned = bool(request.query_params.get("v"))
        headers = avatar_media.cache_headers(path, versioned)
        if avatar_media.is_not_modified(request.headers, headers.get("ETag")):
            return Response(status_code=304, headers=headers)
        mime = _MEDIA_MIME.get(path.suffix.lstrip(".").lower(), "application/octet-stream")
        return FileResponse(str(path), media_type=mime, headers=headers)

    @app.get("/api/agents/{name}/avatar")
    async def get_agent_avatar(name: str, request: Request) -> Any:
        """Stream the agent's avatar.

        By default this is the derived display rendition — one 256px WebP, a
        median 7 KB against the ~888 KB 1024px source the image model produces,
        and still twice the pixels of the largest place the dashboard draws it.
        `?full=1` serves the original, which the avatar editor's preview wants
        and which stays the source of truth on disk.

        `full` is read off the query string rather than declared as a typed
        parameter on purpose: FastAPI would 422 a malformed one (`?full=`
        included), and failing a picture over an unparseable flag is worse than
        ignoring the flag."""
        source = _agent_avatar_path(name)
        if source is None:
            raise HTTPException(status_code=404, detail="No avatar set")
        if (request.query_params.get("full") or "").lower() in ("1", "true", "yes", "on"):
            return _media_response(source, request)
        # Derivation decodes and re-encodes an image; keep it off the loop. Every
        # avatar that predates this endpoint lands here once, then is reused.
        path = await asyncio.to_thread(avatar_media.resolve_display_path, source)
        return _media_response(path, request)

    @app.post("/api/agents/{name}/avatar")
    async def upload_agent_avatar(name: str, request: Request) -> dict:
        """Accept a multipart-form upload OR raw bytes and persist it as the
        agent's avatar. Updates the agent.json5 with the avatar_url so the
        dashboard / runtime see it on next read."""
        try:
            cfg_path = _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        # Multipart upload preferred (the dashboard sends FormData), but raw
        # POST body is supported too for programmatic use.
        ct = (request.headers.get("content-type") or "").lower()
        ext = "png"
        if ct.startswith("multipart/"):
            try:
                form = await request.form()
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"Bad upload: {exc}") from exc
            file = form.get("file")
            if file is None or not hasattr(file, "read"):
                raise HTTPException(status_code=400, detail="Form must contain a 'file' field")
            data = await file.read()
            fname = (getattr(file, "filename", "") or "").lower()
            if fname.endswith(".jpg") or fname.endswith(".jpeg"):
                ext = "jpg"
            elif fname.endswith(".webp"):
                ext = "webp"
            else:
                ext = "png"
        else:
            data = await request.body()
            if "image/jpeg" in ct:
                ext = "jpg"
            elif "image/webp" in ct:
                ext = "webp"
            else:
                ext = "png"

        if not data or len(data) < 64:
            raise HTTPException(status_code=400, detail="Empty or implausibly small image")
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Avatar exceeds 10 MB")

        # Remove any previous avatar so we don't end up with both .png and
        # .jpg sitting next to each other.
        for old_ext in ("png", "jpg", "jpeg", "webp"):
            old = cfg_path.parent / f"avatar.{old_ext}"
            if old.exists() and old_ext != ext:
                try:
                    old.unlink()
                except Exception:
                    pass
        # The previous portrait's rendition describes an image that no longer
        # exists. Drop it here rather than relying on the mtime check, so a
        # replacement that happens to be older than the rendition can't serve
        # the old face.
        avatar_media.clear_variants(cfg_path.parent)

        target = cfg_path.parent / f"avatar.{ext}"
        target.write_bytes(data)
        await asyncio.to_thread(avatar_media.derive_display_variant, target)

        # Persist the URL into the agent config so other readers see it.
        try:
            cfg_data = await _api._read_agent_config_for_update(name)
            cfg_data["avatar_url"] = f"/api/agents/{name}/avatar"
            await _api._write_agent_config_async(name, cfg_data)
        except Exception as exc:
            _api.logger.warning("Could not persist avatar_url for '%s': %s", name, exc)

        return {"ok": True, "name": name, "url": _agent_avatar_url(name)}

    @app.delete("/api/agents/{name}/avatar")
    async def delete_agent_avatar(name: str) -> dict:
        try:
            cfg_path = _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        removed = False
        for ext in ("png", "jpg", "jpeg", "webp"):
            p = cfg_path.parent / f"avatar.{ext}"
            if p.exists():
                p.unlink()
                removed = True
        avatar_media.clear_variants(cfg_path.parent)
        try:
            cfg_data = await _api._read_agent_config_for_update(name)
            cfg_data.pop("avatar_url", None)
            await _api._write_agent_config_async(name, cfg_data)
        except Exception:
            pass
        return {"ok": True, "removed": removed}

    # ── Character sprites ────────────────────────────────────────────────────
    #
    # A "character" is a portrait (avatar.png) PLUS a TWO-SHEET sprite set
    # used to animate the agent in the playground.
    #
    # Sheet 1 (motion.png) holds the four-direction walk cycles (left, right,
    # up, down × 6 frames each = 24 cells). Sheet 2 (stance.png) holds the
    # stationary poses: 4 directional idles + 2 emotional idles, 4 directional
    # talks + laugh + shrug, 6 utility poses (work / sleep / wave / cheer /
    # read / coffee), and 6 SPARE cells reserved for future poses.
    #
    # Each sheet is 1536 × 1024 with 6 cols × 4 rows of 256 × 256 cells. The
    # foot baseline lives 40 px above the bottom of every cell (y=216), which
    # the cropper uses to bottom-anchor the character.
    #
    # We generate each sheet in a single image-model call. The stance call
    # additionally receives the freshly-generated motion sheet as a reference
    # so the character stays visually identical across the two sheets.

    # 8 animation rows × 4 frames each = 32 sprite frames total.
    # Plus per-direction idle aliases derived from each walk row's first frame.
    MOTION_POSES = (
        # Row 1 — WALK LEFT (4 frames)
        "walk_left_a",
        "walk_left_b",
        "walk_left_c",
        "walk_left_d",
        # Row 2 — WALK RIGHT
        "walk_right_a",
        "walk_right_b",
        "walk_right_c",
        "walk_right_d",
        # Row 3 — WALK UP (back view)
        "walk_up_a",
        "walk_up_b",
        "walk_up_c",
        "walk_up_d",
        # Row 4 — WALK DOWN (front view)
        "walk_down_a",
        "walk_down_b",
        "walk_down_c",
        "walk_down_d",
    )
    STANCE_POSES = (
        # Row 1 — SLEEPING (4 frames)
        "sleep_a",
        "sleep_b",
        "sleep_c",
        "sleep_d",
        # Row 2 — DRINKING COFFEE
        "coffee_a",
        "coffee_b",
        "coffee_c",
        "coffee_d",
        # Row 3 — THINKING
        "thinking_a",
        "thinking_b",
        "thinking_c",
        "thinking_d",
        # Row 4 — LAPTOP WORK
        "work_a",
        "work_b",
        "work_c",
        "work_d",
        # Idle direction aliases (point to the first frame of each walk row).
        "idle_left",
        "idle_right",
        "idle_up",
        "idle_down",
        # Legacy single-frame stance names (point to mid-animation frame).
        # Kept so existing renderer setSleep / setWork / etc. don't break.
        "sleep",
        "coffee",
        "idle_thinking",
        "work",
    )
    SPRITE_POSES = MOTION_POSES + STANCE_POSES

    # 4×4 motion layout — walk_xxx_a..d at cols 0-3, directions at rows 0-3.
    # idle_xxx derives from the first frame (col 0) of each walk row.
    _MOTION_LAYOUT: dict[str, tuple[int, int]] = {}
    for _row, _dir in enumerate(("left", "right", "up", "down")):
        for _col, _fr in enumerate(("a", "b", "c", "d")):
            _MOTION_LAYOUT[f"walk_{_dir}_{_fr}"] = (_col, _row)
        _MOTION_LAYOUT[f"idle_{_dir}"] = (0, _row)

    # 4×4 stance layout — every cell is captured: 4 stances × 4 frames.
    # Legacy single-frame names alias the canonical "best" frame of each row.
    _STANCE_LAYOUT: dict[str, tuple[int, int]] = {}
    for _row, _stance in enumerate(("sleep", "coffee", "thinking", "work")):
        for _col, _fr in enumerate(("a", "b", "c", "d")):
            _STANCE_LAYOUT[f"{_stance}_{_fr}"] = (_col, _row)
    _STANCE_LAYOUT["sleep"] = (1, 0)  # legacy alias → sleep_b
    _STANCE_LAYOUT["coffee"] = (2, 1)  # legacy alias → coffee_c (mid-sip)
    _STANCE_LAYOUT["idle_thinking"] = (1, 2)  # legacy alias → thinking_b
    _STANCE_LAYOUT["work"] = (1, 3)  # legacy alias → work_b

    _MOTION_GROUPS = (
        ("walk_left_a", "walk_left_b", "walk_left_c", "walk_left_d"),
        ("walk_right_a", "walk_right_b", "walk_right_c", "walk_right_d"),
        ("walk_up_a", "walk_up_b", "walk_up_c", "walk_up_d"),
        ("walk_down_a", "walk_down_b", "walk_down_c", "walk_down_d"),
    )

    # Per-stance frame groups — useful for the renderer if it wants to play
    # 4-frame stance loops (sleeping breath, coffee sip, etc.).
    _STANCE_GROUPS = (
        ("sleep_a", "sleep_b", "sleep_c", "sleep_d"),
        ("coffee_a", "coffee_b", "coffee_c", "coffee_d"),
        ("thinking_a", "thinking_b", "thinking_c", "thinking_d"),
        ("work_a", "work_b", "work_c", "work_d"),
    )

    # Sprite-gen prompts live in olano/sprites/prompts.py — single source of
    # truth shared with scripts/debug_sprite_gen.py. The dashboard JS mirrors
    # them as CHARGEN_PAGE1_PROMPT / CHARGEN_PAGE2_PROMPT for visibility.
    from olano.sprites.prompts import (
        PAGE1_PROMPT as _PAGE1_PROMPT,
    )
    from olano.sprites.prompts import (
        PAGE2_PROMPT as _PAGE2_PROMPT,
    )
    from olano.sprites.prompts import (
        PORTRAIT_PROMPT_BASE as _PORTRAIT_PROMPT_BASE,
    )

    def _agent_sprites_dir(name: str) -> Path | None:
        try:
            cfg_path = _api._agent_config_path(name)
        except FileNotFoundError:
            return None
        return cfg_path.parent / "sprites"

    def _agent_sprites_urls(name: str, *, stamped: bool = True) -> dict[str, str] | None:
        """Sprite URLs for one agent.

        `stamped` adds the `?v=` content marker, on the same contract as the
        avatar: a stamped URL is content-addressed and so cacheable outright.
        It is what makes the two-pass sprite generation work — the second,
        tightly-cropped pass writes over the same filenames, and the stamp is
        what tells the browser those bytes changed.

        Pass `stamped=False` for anything written to `agent.json5`: a stamp
        describes one moment of one file, and every reader re-derives these
        from disk anyway, so persisting one would only bake in something that
        goes stale."""
        d = _agent_sprites_dir(name)
        if d is None or not d.is_dir():
            return None
        out: dict[str, str] = {}
        for pose in SPRITE_POSES:
            frame = d / f"{pose}.png"
            if not frame.is_file():
                continue
            url = f"/api/agents/{name}/sprites/{pose}"
            stamp = avatar_media.content_stamp(frame) if stamped else None
            out[pose] = f"{url}?v={stamp}" if stamp else url
        return out or None

    # Raw sheet aliases that can be downloaded alongside per-pose pngs. The
    # legacy single-sheet world used the name "sheet"; the two-sheet world
    # uses "motion" and "stance". Old "sheet" is left as an alias for any
    # legacy agent that still has one on disk.
    _SHEET_ALIASES = ("motion", "stance", "sheet")

    @app.get("/api/agents/{name}/sprites/{pose}")
    async def get_agent_sprite(name: str, pose: str, request: Request) -> Any:
        # Frames are served at their authored size — a sprite's foot baseline is
        # pinned to an exact pixel row the renderer relies on, so resizing here
        # would break the animation. Only the caching half applies, and it is
        # the half that matters: the raw sheets are ~1 MB each and were being
        # re-downloaded on every playground mount.
        if pose not in SPRITE_POSES and pose not in _SHEET_ALIASES:
            raise HTTPException(status_code=404, detail="Unknown pose")
        d = _agent_sprites_dir(name)
        if d is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        # "sheet" is a legacy alias — serve motion.png (walk sheet) when present,
        # fall back to stance.png, so old bookmarks / links don't 404.
        candidates = [pose]
        if pose == "sheet":
            candidates = ["motion", "stance", "sheet"]
        for candidate in candidates:
            path = d / f"{candidate}.png"
            if path.is_file():
                return _media_response(path, request)
        raise HTTPException(status_code=404, detail=f"Sprite '{pose}' not generated")

    @app.delete("/api/agents/{name}/sprites")
    async def delete_agent_sprites(name: str) -> dict:
        d = _agent_sprites_dir(name)
        if d is None:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")
        removed = 0
        if d.is_dir():
            for p in d.glob("*.png"):
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    pass
            try:
                d.rmdir()
            except Exception:
                pass
        try:
            cfg_data = await _api._read_agent_config_for_update(name)
            cfg_data.pop("sprites", None)
            await _api._write_agent_config_async(name, cfg_data)
        except Exception:
            pass
        return {"ok": True, "removed": removed}

    @app.post("/api/agents/{name}/character/generate")
    async def generate_agent_character(name: str, request: Request) -> dict:
        """Generate a portrait + 6-frame sprite sheet for an agent.

        Runs on the **Olano image service by default** — billed in credits, no
        provider key on the box — falling back to the customer's own OpenAI
        account when there is no gateway (unmanaged install) or when the
        operator pinned one (``LIONCLAW_IMAGE_MODEL``, or the ``model`` field
        below). Resolution goes through ``image_tools.openai_images_backend``
        so this UI and the agents' own image tools can never disagree about
        which backend a box uses.

        Multipart form fields (all optional):
          • description  — free-form character description (defaults to agent.description)
          • style        — extra style guidance ("watercolor", "cyberpunk", ...)
          • emoji        — single emoji that inspires the look
          • file         — reference image (selfie / pet / character) used as the edit input
          • model        — ``olano:olano-image`` (default) or ``openai:gpt-image-2.5-flare``.
                           The gateway routes ``olano-image`` edit requests to an
                           edit-capable OpenAI model, so the reference-image path
                           works on credits like the rest.

        The locked technical prompt (sheet layout, 6 poses, transparent bg) is server-
        side and not user-controllable. Returns ``{portrait, sprites: {pose: url}}``.
        """
        try:
            cfg_path = _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        ct = (request.headers.get("content-type") or "").lower()
        description = ""
        style = ""
        emoji = ""
        picked_model = ""
        ref_bytes: bytes | None = None
        ref_filename = ""
        if ct.startswith("multipart/"):
            form = await request.form()
            _desc = form.get("description")
            description = _desc.strip() if isinstance(_desc, str) else ""
            _style = form.get("style")
            style = _style.strip() if isinstance(_style, str) else ""
            _emoji = form.get("emoji")
            emoji = _emoji.strip() if isinstance(_emoji, str) else ""
            _model = form.get("model")
            picked_model = _model.strip() if isinstance(_model, str) else ""
            file = form.get("file")
            if file is not None and hasattr(file, "read"):
                ref_bytes = await file.read()
                ref_filename = (getattr(file, "filename", "") or "reference.png").lower()
                if not ref_bytes or len(ref_bytes) < 64:
                    ref_bytes = None
                elif len(ref_bytes) > 10 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="Reference image exceeds 10 MB")
        else:
            try:
                body = await request.json()
            except Exception:
                body = {}
            description = (body or {}).get("description", "") or ""
            style = (body or {}).get("style", "") or ""
            emoji = (body or {}).get("emoji", "") or ""
            picked_model = ((body or {}).get("model") or "").strip()

        # Which backend runs this — Olano credits unless told otherwise. Done
        # after the form parse so an explicit `model` is honored, and before any
        # prompt work so an unusable box fails fast with an actionable message.
        from olano.tools.image_tools import openai_images_backend

        backend = await asyncio.to_thread(openai_images_backend, picked_model)
        if isinstance(backend, str):
            raise HTTPException(status_code=400, detail=backend)
        image_provider, image_model, api_key, image_base_url = backend

        # Every image this endpoint produces is billed — on olano credits via
        # the gateway, or against the operator's own OpenAI key. It bypasses
        # image_tools._format_result (the tools' recording chokepoint), so
        # mirror it into the analytics service ledger here or the Insights
        # "Service calls" card under-reports a credits account's real burn.
        def _record_generated_image(count: int = 1, credits: float | None = None) -> None:
            try:
                from olano.analytics.service_ledger import record_service_event

                record_service_event(
                    "image",
                    provider=image_provider,
                    managed=image_provider == "olano",
                    agent=name,
                    units=count,
                    credits=credits,
                )
            except Exception:  # pragma: no cover - accounting never blocks a render
                _api.logger.debug("service ledger: generated image not recorded", exc_info=True)

        def _image_call_credits(raw_response: Any) -> float | None:
            """Gateway per-call charge from a raw-response wrapper's headers."""
            try:
                from olano.analytics.usage_recorder import credits_from_headers

                return credits_from_headers(raw_response.headers, managed=image_provider == "olano")
            except Exception:  # pragma: no cover - accounting never blocks a render
                return None

        # Fall back to agent description so an empty form still produces something
        # interesting — but never let it be totally blank.
        if not description:
            try:
                cfg_data = await asyncio.to_thread(_api._read_agent_config_cached, name)
                description = cfg_data.get("description") or cfg_data.get("name") or name
            except Exception:
                description = name

        # ── Build prompts — user inputs appended after the locked base text ──
        # The base prompts (page1/page2 markdown files) are used verbatim.
        # Any user input is appended as supplemental context; the model gives
        # later instructions higher weight so description/emoji/style will
        # override the default clothing lock for different characters.
        user_input_parts: list[str] = []
        if description:
            user_input_parts.append(f"CHARACTER: {description.strip()}")
        if emoji:
            user_input_parts.append(f"Character inspired by the emoji: {emoji}")
        if style:
            user_input_parts.append(f"Additional style guidance: {style.strip()}")
        user_input = "\n".join(user_input_parts)

        portrait_prompt = _PORTRAIT_PROMPT_BASE + (f"\n\n{user_input}" if user_input else "")
        page1_prompt = _PAGE1_PROMPT + (f"\n\n{user_input}" if user_input else "")
        page2_prompt = _PAGE2_PROMPT + (f"\n\n{user_input}" if user_input else "")

        # ── 1. Generate the portrait ─────────────────────────────────────────
        # If a reference image was supplied, use it as the edit input so the
        # portrait inherits the source character's vibe. Otherwise, plain
        # text-to-image.
        from openai import APIStatusError as _OAStatusError
        from openai import AsyncOpenAI

        # base_url is what routes this to the Olano gateway: it serves the same
        # OpenAI images shape, so the SDK calls below are provider-agnostic.
        client = AsyncOpenAI(api_key=api_key, base_url=image_base_url)

        # `quality="low"` on both branches is deliberate and stays that way
        # after the move to gpt-image-2.5-flare. A quality tier costs latency as
        # well as money, and this call blocks a human watching a spinner in the
        # avatar editor; the output is then derived down to a 256px webp
        # (`runtime/avatar_media.py`) and drawn at 24-88px, so nothing
        # downstream can show the extra detail a higher tier would buy. Flare's
        # own speed gain is taken as speed here, not spent back on quality.
        async def _portrait_bytes() -> tuple[bytes, float | None]:
            try:
                # with_raw_response keeps the HTTP headers (the gateway's
                # per-call cost rides them) while .parse() yields the same
                # typed object as the plain call; APIStatusError is unchanged.
                if ref_bytes is not None:
                    raw = await client.images.with_raw_response.edit(
                        model=image_model,
                        image=(ref_filename or "reference.png", ref_bytes, "image/png"),
                        prompt=portrait_prompt,
                        quality="low",
                        size="1024x1024",
                    )
                else:
                    raw = await client.images.with_raw_response.generate(
                        model=image_model,
                        quality="low",
                        prompt=portrait_prompt,
                        size="1024x1024",
                    )
                resp = raw.parse()
                credits = _image_call_credits(raw)
            except _OAStatusError as exc:
                raise HTTPException(
                    status_code=exc.status_code or 502,
                    detail=f"{image_provider} portrait failed: {exc.message}",
                ) from exc
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"{image_provider} portrait failed: {exc}",
                ) from exc
            item = resp.data[0] if resp.data else None
            if item is None:
                raise HTTPException(
                    status_code=502, detail=f"{image_provider} returned no portrait data"
                )
            import base64 as _b64

            b64_json = getattr(item, "b64_json", None)
            if b64_json:
                return _b64.b64decode(b64_json), credits
            item_url = getattr(item, "url", None)
            if item_url:
                async with __import__("httpx").AsyncClient(timeout=30.0) as h:
                    resp_bytes: bytes = (await h.get(item_url)).content
                    return resp_bytes, credits
            raise HTTPException(
                status_code=502, detail=f"{image_provider} returned no portrait bytes"
            )

        portrait, portrait_credits = await _portrait_bytes()
        _record_generated_image(credits=portrait_credits)

        # ── 2. Persist portrait now and reset any stale sprites ─────────────
        # The portrait is the slow-but-cheap visible artefact, so we save it
        # before kicking off the sprite-sheet generation. The dashboard can
        # close its dialog at this point and let the user wander off; the
        # sprite generation continues in the background and pushes an event
        # when it's done.
        agent_dir = cfg_path.parent
        for old_ext in ("png", "jpg", "jpeg", "webp"):
            old = agent_dir / f"avatar.{old_ext}"
            if old.exists() and old_ext != "png":
                try:
                    old.unlink()
                except Exception:
                    pass
        avatar_media.clear_variants(agent_dir)
        (agent_dir / "avatar.png").write_bytes(portrait)
        await asyncio.to_thread(avatar_media.derive_display_variant, agent_dir / "avatar.png")
        # Wipe any prior sprite set so the dashboard doesn't render a frame
        # from the previous character while the new sheet renders.
        sprites_dir = agent_dir / "sprites"
        if sprites_dir.is_dir():
            for p in sprites_dir.glob("*.png"):
                try:
                    p.unlink()
                except Exception:
                    pass
        try:
            cfg_data = await _api._read_agent_config_for_update(name)
            cfg_data["avatar_url"] = f"/api/agents/{name}/avatar"
            cfg_data.pop("sprites", None)
            await _api._write_agent_config_async(name, cfg_data)
        except Exception as exc:
            _api.logger.warning("Could not persist portrait URL for '%s': %s", name, exc)

        # Tell every connected dashboard the new portrait is live so any
        # rendered avatar (cards, chat heads, playground head) refreshes
        # immediately — no need to wait for the sprite sheet. The URL carries
        # the new source's `?v=` stamp, which is what actually retires the
        # year-long cache entry the previous portrait was served under.
        _api._push_event("agent_avatar_updated", {"name": name, "url": _agent_avatar_url(name)})

        # ── Playground gate ────────────────────────────────────────────────
        # The animation sprite sheets only exist to walk the agent around the
        # Playground. When the Playground is disabled home-wide (config.yaml
        # playground.enabled: false — the default), skip the slow, multi-call
        # sprite-sheet generation entirely and return with just the
        # portrait. The cheap avatar.png profile cartoon is already saved above.
        from olano.config.system_settings import playground_enabled

        if not playground_enabled():
            return {
                "ok": True,
                "name": name,
                "portrait": f"/api/agents/{name}/avatar",
                "sprites": None,
                "sprites_pending": False,
                "playground_disabled": True,
            }

        # ── 3. Background the two-sheet generation + cropping ──────────────
        # Sheet 1 (motion) is generated first, with grid + style + portrait
        # references. Sheet 2 (stance) uses the same plus the just-generated
        # motion sheet itself, so identity is locked across both.
        async def _generate_sprites_bg() -> None:
            from io import BytesIO

            from PIL import Image

            from olano.sprites.processing import (
                crop_sheet as _crop_sheet_shared,
            )
            from olano.sprites.processing import (
                crop_to_common_bbox as _crop_to_common_bbox,
            )
            from olano.sprites.processing import (
                normalize_heights as _normalize_heights,
            )

            sprites_dir.mkdir(parents=True, exist_ok=True)

            # Wipe any stale .png from a previous generation BEFORE we save
            # new ones. Keeps the renderer from rendering a leftover pose
            # (e.g. legacy walk_a from an older agent style) on top of the
            # fresh set, and ensures a tight-bbox crop here doesn't leave
            # the previous oversized canvas around for the same pose name.
            stale_count = 0
            for stale in sprites_dir.glob("*.png"):
                try:
                    stale.unlink()
                    stale_count += 1
                except Exception:
                    pass
            if stale_count:
                _api.logger.info("Wiped %d stale sprite(s) from %s", stale_count, sprites_dir)

            async def _gen_sheet(
                prompt: str, refs: list[tuple[str, bytes, str]], *, label: str
            ) -> tuple[bytes, float | None]:
                # Log what we're sending so the next failure is easier to
                # debug than a bare "Error code: 404". Names + byte sizes
                # tell us whether one ref is unexpectedly huge or absent.
                _api.logger.info(
                    "%s %s: %d refs (%s)",
                    image_model,
                    label,
                    len(refs),
                    ", ".join(f"{n}={len(b) // 1024}KB" for (n, b, _) in refs),
                )
                try:
                    raw = await client.images.with_raw_response.edit(
                        model=image_model,
                        image=refs if len(refs) > 1 else refs[0],
                        prompt=prompt,
                        size="1024x1024",
                    )
                    resp = raw.parse()
                    sheet_credits = _image_call_credits(raw)
                except _OAStatusError as exc:
                    # exc.message is often empty on 4xx — drain everything
                    # the SDK exposes (status, body dict, raw response text,
                    # request id) so we can actually diagnose the failure
                    # next time instead of staring at a bare "404".
                    detail_bits: list[str] = []
                    sc = getattr(exc, "status_code", None)
                    if sc is not None:
                        detail_bits.append(f"status={sc}")
                    if getattr(exc, "message", ""):
                        detail_bits.append(f"msg={exc.message}")
                    body = getattr(exc, "body", None)
                    if body:
                        detail_bits.append(f"body={str(body)[:500]}")
                    response = getattr(exc, "response", None)
                    if response is not None:
                        try:
                            rtext = response.text if hasattr(response, "text") else None
                            if rtext and (not body or str(body) not in rtext):
                                detail_bits.append(f"resp={rtext[:500]}")
                        except Exception:
                            pass
                    rid = (
                        (getattr(response, "headers", {}) or {}).get("x-request-id")
                        if response is not None
                        else None
                    )
                    rid = rid or getattr(exc, "request_id", None)
                    if rid:
                        detail_bits.append(f"req_id={rid}")
                    raise RuntimeError(
                        f"{image_provider} {label} sheet failed: " + " | ".join(detail_bits)
                    ) from exc
                item = resp.data[0] if resp.data else None
                if item is None:
                    raise RuntimeError(f"OpenAI returned no {label} sheet data")
                import base64 as _b64

                b64_json = getattr(item, "b64_json", None)
                if b64_json:
                    return _b64.b64decode(b64_json), sheet_credits
                item_url = getattr(item, "url", None)
                if item_url:
                    import httpx as _httpx

                    async with _httpx.AsyncClient(timeout=60.0) as h:
                        sheet_bytes: bytes = (await h.get(item_url)).content
                        return sheet_bytes, sheet_credits
                raise RuntimeError(f"OpenAI returned no {label} sheet bytes")

            def _crop_sheet(
                sheet_bytes: bytes,
                layout: dict[str, tuple[int, int]],
                *,
                expected_cols: int = 4,
                expected_rows: int = 4,
            ) -> dict[str, "Image.Image"]:
                """Return per-pose aligned sprites in memory (does NOT save).

                Identical pipeline to scripts/debug_sprite_gen.py — both call
                olano.sprites.processing.crop_sheet so behaviour stays in
                lock-step. Caller is responsible for the final tight-bbox crop
                and saving to disk.
                """
                return _crop_sheet_shared(
                    sheet_bytes,
                    layout,
                    expected_cols=expected_cols,
                    expected_rows=expected_rows,
                    on_log=lambda msg: _api.logger.info("crop_sheet: %s", msg),
                )

            def _save_sprites(sprites: dict[str, "Image.Image"]) -> None:
                for pose, img in sprites.items():
                    img.save(sprites_dir / f"{pose}.png", format="PNG", optimize=True)

            try:
                # ── 3a. Page 1 — motion / walk cycles ─────────────────
                # Refs: portrait (always) + optional user reference photo.
                # The layout and style are fully described in page1_prompt.
                page1_refs: list[tuple[str, bytes, str]] = [
                    ("portrait.png", portrait, "image/png"),
                ]
                if ref_bytes is not None:
                    page1_refs.append((ref_filename or "reference.png", ref_bytes, "image/png"))

                motion_sheet, motion_credits = await _gen_sheet(
                    page1_prompt, page1_refs, label="page1-motion"
                )
                _record_generated_image(credits=motion_credits)
                (sprites_dir / "motion.png").write_bytes(motion_sheet)
                walk_sprites = _crop_sheet(motion_sheet, _MOTION_LAYOUT)
                # Save walks immediately at full canvas — gives the dashboard
                # something to show while the stance sheet renders. We'll
                # tighten the bbox + re-save once page 2 is done.
                _save_sprites(walk_sprites)

                _api._push_event(
                    "agent_sprites_ready",
                    {
                        "name": name,
                        "sprites": _agent_sprites_urls(name) or {},
                    },
                )

                # ── 3b. Page 2 — stances ────────────────────────────────
                # Refs: portrait + the just-generated motion sheet (identity
                # anchor). Re-encode to plain RGB PNG before sending — the
                # model's own output sometimes has alpha/profiles that the
                # edit endpoint rejects with a bare 404.
                try:
                    with Image.open(BytesIO(motion_sheet)) as _msrc:
                        _norm = BytesIO()
                        _msrc.convert("RGB").save(_norm, format="PNG", optimize=True)
                        motion_ref_bytes = _norm.getvalue()
                except Exception as exc:
                    _api.logger.warning("Could not re-encode motion sheet for stance ref: %s", exc)
                    motion_ref_bytes = motion_sheet

                page2_refs: list[tuple[str, bytes, str]] = [
                    ("portrait.png", portrait, "image/png"),
                    ("motion.png", motion_ref_bytes, "image/png"),
                ]

                stance_sheet, stance_credits = await _gen_sheet(
                    page2_prompt, page2_refs, label="page2-stances"
                )
                _record_generated_image(credits=stance_credits)
                (sprites_dir / "stance.png").write_bytes(stance_sheet)
                stance_sprites = _crop_sheet(stance_sheet, _STANCE_LAYOUT)

                # Final two-pass cleanup over the whole 32-frame set:
                #   1. normalize_heights — scale each sprite so its character
                #      bbox has the median height across the set. Without this,
                #      mid-stride walk frames render visibly smaller than the
                #      standing/coffee/sleep poses because the renderer sizes
                #      by texture height.
                #   2. crop_to_common_bbox — tight crop to the union bbox so
                #      every saved PNG is small but still shares one coordinate
                #      system (animations stay aligned).
                all_sprites = {**walk_sprites, **stance_sprites}
                normalized = _normalize_heights(all_sprites)
                tight = _crop_to_common_bbox(normalized, padding=4)
                if tight:
                    sample = next(iter(tight.values()))
                    raw = next(iter(all_sprites.values()))
                    _api.logger.info(
                        "crop_sheet: normalized + tight = %dx%d (was %dx%d)",
                        sample.width,
                        sample.height,
                        raw.width,
                        raw.height,
                    )
                _save_sprites(tight)

                try:
                    cfg_data = await _api._read_agent_config_for_update(name)
                    cfg_data["sprites"] = _agent_sprites_urls(name, stamped=False) or {}
                    await _api._write_agent_config_async(name, cfg_data)
                except Exception as exc:
                    _api.logger.warning("Could not persist sprite URLs for '%s': %s", name, exc)

                # The event carries the *stamped* URLs: this is the second,
                # tightly-cropped pass landing on the same filenames as the
                # first, and the stamp is what tells the canvas to refetch.
                _api._push_event(
                    "agent_sprites_ready",
                    {"name": name, "sprites": _agent_sprites_urls(name) or {}},
                )
            except Exception as exc:  # propagate via SSE to the UI
                _api.logger.warning("Sprite-sheet background task for '%s' failed: %s", name, exc)
                _api._push_event("agent_sprites_failed", {"name": name, "error": str(exc)})

        asyncio.create_task(_generate_sprites_bg(), name=f"sprites:{name}")

        # ── 4. Return immediately — sprites land later via SSE ─────────────
        return {
            "ok": True,
            "name": name,
            "portrait": f"/api/agents/{name}/avatar",
            "sprites": None,
            "sprites_pending": True,
        }

    def _vault_lookup(name: str) -> str:
        try:
            from olano.vault.resolver import get_vault

            v = get_vault()
            if v is None:
                return ""
            try:
                return cast(str, v.get(name))
            except Exception:
                return ""
        except Exception:
            return ""

    def _resolve_api_key(env_var: str) -> str:
        return os.environ.get(env_var, "") or _vault_lookup(env_var)

    @app.get("/api/avatar/models")
    async def list_avatar_models() -> dict:
        """Enumerate image-generation backends the user can pick from. Each
        entry is annotated with whether it's currently available (API key
        present, or model installed for Ollama)."""
        import httpx as _httpx

        models: list[dict] = []

        # Olano — the default. Billed in credits, no provider key on the box,
        # so it leads the list on every managed deployment.
        from olano.tools.image_tools import (
            DEFAULT_OPENAI_IMAGE_MODEL,
            OLANO_IMAGE_MODEL,
            olano_managed_media_available,
        )

        olano_ok = await asyncio.to_thread(olano_managed_media_available)
        models.append(
            {
                "id": f"olano:{OLANO_IMAGE_MODEL}",
                "provider": "olano",
                "model": OLANO_IMAGE_MODEL,
                "label": "Olano · Image (credits)",
                "available": bool(olano_ok),
                "default": bool(olano_ok),
                "reason": "" if olano_ok else "deployment not connected to cloud.olano.ai",
            }
        )

        # OpenAI — gpt-image-2.5-flare, and dall-e-3 (older, cheaper). Runs on
        # the customer's own account. The default here is read from
        # image_tools, never retyped: the dashboard picker and an unpinned
        # agent call must agree about what "OpenAI" means on this box.
        oa_key = _resolve_api_key("OPENAI_API_KEY")
        for mid, label in (
            (DEFAULT_OPENAI_IMAGE_MODEL, "GPT Image 2.5 Flare"),
            ("dall-e-3", "DALL·E 3"),
        ):
            models.append(
                {
                    "id": f"openai:{mid}",
                    "provider": "openai",
                    "model": mid,
                    "label": f"OpenAI · {label}",
                    "available": bool(oa_key),
                    "default": bool(oa_key and not olano_ok and mid == DEFAULT_OPENAI_IMAGE_MODEL),
                    "reason": "" if oa_key else "OPENAI_API_KEY not set",
                }
            )

        # Google Gemini / Imagen.
        g_key = _resolve_api_key("GOOGLE_API_KEY") or _resolve_api_key("GEMINI_API_KEY")
        models.append(
            {
                "id": "gemini:imagen-3.0-generate-002",
                "provider": "gemini",
                "model": "imagen-3.0-generate-002",
                "label": "Google · Imagen 3",
                "available": bool(g_key),
                "default": False,
                "reason": "" if g_key else "GOOGLE_API_KEY / GEMINI_API_KEY not set",
            }
        )

        # Ollama — query http://localhost:11434/api/tags to discover what's
        # installed locally; surface every model that looks image-capable.
        ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        try:
            async with _httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{ollama_url}/api/tags")
                if resp.status_code == 200:
                    tags = (resp.json() or {}).get("models", []) or []
                    for tag in tags:
                        mid = tag.get("name") or tag.get("model") or ""
                        low = mid.lower()
                        # Heuristic: surface any tag that looks like a known
                        # diffusion / image model. Users who run something
                        # exotic can still pick it via the prompt below.
                        looks_image = any(
                            k in low
                            for k in (
                                "flux",
                                "image",
                                "sdxl",
                                "diffusion",
                                "stable",
                                "dalle",
                                "playground",
                                "schnell",
                                "pixart",
                            )
                        )
                        if not mid or not looks_image:
                            continue
                        models.append(
                            {
                                "id": f"ollama:{mid}",
                                "provider": "ollama",
                                "model": mid,
                                "label": f"Ollama · {mid}",
                                "available": True,
                                "default": False,
                                "reason": "",
                            }
                        )
        except Exception:
            # Ollama unreachable — silently omit. The cloud entries above
            # are still listed.
            pass

        return {"models": models}

    @app.post("/api/agents/{name}/avatar/generate")
    async def generate_agent_avatar(name: str, body: dict) -> dict:
        """Generate a square avatar from a prompt using the chosen backend.

        Body: ``{"prompt": str, "size"?: "1024x1024" | "512x512",
                 "model"?: "olano:olano-image" | "openai:gpt-image-2.5-flare" |
                           "gemini:..." | "ollama:..."}``.

        With no model, it runs where the agents' own image tools run: the Olano
        image service (credits, no key) on a managed deployment, else the
        customer's OpenAI account.
        """
        try:
            cfg_path = _api._agent_config_path(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")

        prompt = (body or {}).get("prompt", "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt is required")
        size = (body or {}).get("size") or "1024x1024"
        model_id = ((body or {}).get("model") or "").strip()
        if not model_id:
            from olano.tools.image_tools import openai_images_backend

            resolved_default = await asyncio.to_thread(openai_images_backend, "")
            if isinstance(resolved_default, str):
                raise HTTPException(status_code=400, detail=resolved_default)
            model_id = f"{resolved_default[0]}:{resolved_default[1]}"
        provider, _, model_name = model_id.partition(":")

        # Augment the prompt so the result fits inside a circular avatar.
        full_prompt = (
            f"Cute friendly cartoon character avatar of: {prompt}. "
            f"Square portrait composition, large head and face, simple flat colour "
            f"background that matches a clean office UI, no text, no watermarks, "
            f"warm lighting, soft pastel palette, clean vector style."
        )

        import base64 as _b64

        import httpx as _httpx

        img_bytes: bytes
        if provider in ("openai", "olano"):
            # One branch: the Olano gateway serves the same /images/generations
            # shape as OpenAI, so only the endpoint and the credential differ.
            if provider == "olano":
                from olano.tools.image_tools import OLANO_IMAGE_MODEL, openai_images_backend

                resolved = await asyncio.to_thread(openai_images_backend, model_id)
                if isinstance(resolved, str):
                    raise HTTPException(status_code=400, detail=resolved)
                _, model_name, api_key, base_url = resolved
                model_name = model_name or OLANO_IMAGE_MODEL
            else:
                from olano.tools.image_tools import DEFAULT_OPENAI_IMAGE_MODEL

                api_key = _resolve_api_key("OPENAI_API_KEY")
                base_url = "https://api.openai.com/v1"
                model_name = model_name or DEFAULT_OPENAI_IMAGE_MODEL
                if not api_key:
                    raise HTTPException(status_code=400, detail="OPENAI_API_KEY not set")
            try:
                async with _httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        f"{base_url}/images/generations",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": model_name,
                            "prompt": full_prompt,
                            "size": size,
                            "n": 1,
                        },
                    )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"{provider} image call failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"{provider} returned {resp.status_code}: {resp.text[:300]}",
                )
            entry = (resp.json().get("data") or [{}])[0]
            if "b64_json" in entry:
                img_bytes = _b64.b64decode(entry["b64_json"])
            elif "url" in entry:
                async with _httpx.AsyncClient(timeout=30.0) as client:
                    img_bytes = (await client.get(entry["url"])).content
            else:
                raise HTTPException(
                    status_code=502, detail=f"{provider}: no image data in response"
                )
        elif provider == "gemini":
            api_key = _resolve_api_key("GOOGLE_API_KEY") or _resolve_api_key("GEMINI_API_KEY")
            if not api_key:
                raise HTTPException(
                    status_code=400, detail="GOOGLE_API_KEY / GEMINI_API_KEY not set"
                )
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name or 'imagen-3.0-generate-002'}:predict"
            try:
                async with _httpx.AsyncClient(timeout=60.0) as client:
                    resp = await client.post(
                        url,
                        params={"key": api_key},
                        json={
                            "instances": [{"prompt": full_prompt}],
                            "parameters": {"sampleCount": 1, "aspectRatio": "1:1"},
                        },
                    )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"Gemini image call failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Gemini returned {resp.status_code}: {resp.text[:300]}",
                )
            preds = (resp.json().get("predictions") or [{}])[0]
            b64 = preds.get("bytesBase64Encoded") or preds.get("imageBytes")
            if not b64:
                raise HTTPException(status_code=502, detail="Gemini: no image data in response")
            img_bytes = _b64.b64decode(b64)
        elif provider == "ollama":
            ollama_url = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
            try:
                async with _httpx.AsyncClient(timeout=120.0) as client:
                    # Ollama's image-gen endpoint is /api/generate with
                    # `images` returned in the response for diffusion tags.
                    resp = await client.post(
                        f"{ollama_url}/api/generate",
                        json={"model": model_name, "prompt": full_prompt, "stream": False},
                    )
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail=f"Ollama image call failed: {exc}"
                ) from exc
            if resp.status_code >= 400:
                raise HTTPException(
                    status_code=resp.status_code,
                    detail=f"Ollama returned {resp.status_code}: {resp.text[:300]}",
                )
            payload = resp.json() or {}
            imgs = payload.get("images") or []
            if not imgs:
                raise HTTPException(
                    status_code=502,
                    detail=f"Ollama '{model_name}' did not return any images. The selected model may not be diffusion-capable.",
                )
            img_bytes = _b64.b64decode(imgs[0])
        else:
            raise HTTPException(status_code=400, detail=f"Unknown image provider: {provider!r}")

        # Wipe any prior avatar (different ext), then write the new one.
        for old_ext in ("png", "jpg", "jpeg", "webp"):
            old = cfg_path.parent / f"avatar.{old_ext}"
            if old.exists() and old_ext != "png":
                try:
                    old.unlink()
                except Exception:
                    pass
        avatar_media.clear_variants(cfg_path.parent)
        target = cfg_path.parent / "avatar.png"
        target.write_bytes(img_bytes)
        await asyncio.to_thread(avatar_media.derive_display_variant, target)

        try:
            cfg_data = await _api._read_agent_config_for_update(name)
            cfg_data["avatar_url"] = f"/api/agents/{name}/avatar"
            await _api._write_agent_config_async(name, cfg_data)
        except Exception:
            pass
        return {
            "ok": True,
            "name": name,
            "url": _agent_avatar_url(name),
            "prompt": prompt,
            "model": model_id,
        }

    # Closures that later route modules bind (see routes/__init__.py).
    closures["_agent_avatar_url"] = _agent_avatar_url
    closures["_agent_sprites_urls"] = _agent_sprites_urls
    closures["_resolve_api_key"] = _resolve_api_key
