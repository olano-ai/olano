"""Recording an agent's browser to a video, and a picture of it.

A person watching an agent work (or working in its browser themselves) can
press **Record** and get an MP4 of what the screen showed: a demo of the
agent doing something, or the evidence of a take-over. The browser draws
nothing into a file by itself, so the recording is made from the same
screencast the live view streams: a :class:`~olano.browser.live.Screencast`
of its own (one DevTools session more; a recording never depends on a viewer
being connected, so an agent can record itself from a tool) feeds the newest
frame to ``ffmpeg`` ten times a second, and ffmpeg writes H.264 in an MP4
that plays everywhere. Chromium only sends a frame when the page changed, so
the feeder repeats the last one in between: the video runs at a steady rate
and a page that sits still costs almost nothing to encode.

The file lands in the agent's workspace, under ``recordings/``, where the
Files tab shows it, a message can carry it and the agent can read it, and it
is handed to the sandbox like every other file the runtime writes there. A
recording ends when **Stop** is pressed, when the browser session closes,
and on its own after :data:`MAX_RECORDING_S`. One recording per agent at a
time.

A picture (:func:`take_picture`) is the page's screenshot, saved beside the
agent's own under ``screenshots/``.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from olano.browser import sessions
from olano.browser.live import Screencast

logger = logging.getLogger(__name__)

RECORD_FPS = 10
RECORD_WIDTH = 1280
RECORD_HEIGHT = 800
# The longest a recording runs before it is saved on its own: a person who
# walked away must not fill the disk.
MAX_RECORDING_S = 30 * 60
RECORDINGS_DIR = "recordings"
PICTURES_DIR = "screenshots"
# How long stopping waits for ffmpeg to write the file out.
FINISH_TIMEOUT_S = 30.0


class RecordingUnavailable(RuntimeError):
    """Recording cannot start; the message is user-facing."""


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffmpeg_args(out: Path) -> list[str]:
    """The encoder: JPEG frames in on stdin at a steady rate, every frame
    fitted into the recording's size (a frame can change size when the
    viewer's own size changes), H.264 that plays in a browser and on a phone,
    and the index at the front so playback starts before the download ends."""
    fit = (
        f"scale={RECORD_WIDTH}:{RECORD_HEIGHT}:force_original_aspect_ratio=decrease:flags=bicubic,"
        f"pad={RECORD_WIDTH}:{RECORD_HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,format=yuv420p"
    )
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-framerate",
        str(RECORD_FPS),
        "-i",
        "pipe:0",
        "-vf",
        fit,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-r",
        str(RECORD_FPS),
        "-movflags",
        "+faststart",
        str(out),
    ]


def recording_name(now: float | None = None) -> str:
    """``browser-20261018-191502.mp4``: sorts by time, says what it is."""
    return time.strftime("browser-%Y%m%d-%H%M%S", time.localtime(now)) + ".mp4"


def picture_name(now: float | None = None) -> str:
    return time.strftime("live-%Y%m%d-%H%M%S", time.localtime(now)) + ".png"


@dataclass
class SavedFile:
    path: Path
    workspace_root: Path
    bytes: int
    seconds: float = 0.0

    @property
    def relative(self) -> str:
        try:
            return self.path.relative_to(self.workspace_root).as_posix()
        except ValueError:
            return self.path.name

    def public(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "relative": self.relative,
            "name": self.path.name,
            "bytes": self.bytes,
            "seconds": round(self.seconds, 1),
        }


class Recorder:
    """One recording of one agent's browser. Lives on the browser loop."""

    def __init__(self, agent: str, workspace_root: Path) -> None:
        self.agent = agent
        self.workspace_root = workspace_root
        self.path = workspace_root / RECORDINGS_DIR / recording_name()
        self.started_at = time.time()
        self.frames = 0
        self._latest: bytes | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._cast: Screencast | None = None
        self._feeder: asyncio.Task[None] | None = None
        self._stopping = False
        self._done: asyncio.Future[SavedFile] | None = None

    def public(self) -> dict[str, Any]:
        return {
            "since": self.started_at,
            "seconds": round(max(0.0, time.time() - self.started_at), 1),
            "name": self.path.name,
        }

    async def start(self) -> None:
        if ffmpeg_path() is None:
            raise RecordingUnavailable("Recording needs ffmpeg on this machine, which is missing.")
        folder = self.path.parent
        created = not folder.exists()
        folder.mkdir(parents=True, exist_ok=True)
        if created:
            _handoff(folder, self.agent)
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *ffmpeg_args(self.path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RecordingUnavailable(f"The recorder could not start: {exc}") from exc
        cast = Screencast(self.agent, self._on_frame)
        await cast.set_viewer(RECORD_WIDTH, RECORD_HEIGHT)
        try:
            await cast.start()
        except Exception:
            await self._end_process()
            raise
        self._cast = cast
        loop = asyncio.get_running_loop()
        self._done = loop.create_future()
        self._feeder = loop.create_task(self._feed())

    def _on_frame(self, data: bytes, _meta: dict[str, Any]) -> None:
        self._latest = data

    async def _feed(self) -> None:
        """Hand the newest frame to ffmpeg at RECORD_FPS, the same frame again
        while nothing changed, until stopped or the cap is reached."""
        loop = asyncio.get_running_loop()
        interval = 1.0 / RECORD_FPS
        next_at = loop.time()
        timed_out = False
        try:
            while not self._stopping:
                proc = self._proc
                frame = self._latest
                if proc is None or proc.returncode is not None:
                    break
                if frame is not None and proc.stdin is not None:
                    try:
                        proc.stdin.write(frame)
                        await proc.stdin.drain()
                        self.frames += 1
                    except (BrokenPipeError, ConnectionResetError):
                        break
                if time.time() - self.started_at >= MAX_RECORDING_S:
                    timed_out = True
                    break
                next_at += interval
                await asyncio.sleep(max(0.0, next_at - loop.time()))
        except asyncio.CancelledError:
            return
        if timed_out or not self._stopping:
            # Ended on its own (the cap, or ffmpeg went away): save what there is.
            await self._finish(unlisted=True)

    async def stop(self) -> SavedFile:
        """End the recording and wait for the file. Safe to call twice."""
        return await self._finish(unlisted=False)

    async def _finish(self, *, unlisted: bool) -> SavedFile:
        if self._done is not None and self._done.done():
            return self._done.result()
        self._stopping = True
        feeder, self._feeder = self._feeder, None
        if feeder is not None and feeder is not asyncio.current_task():
            feeder.cancel()
            try:
                await feeder
            except (asyncio.CancelledError, Exception):
                pass
        cast, self._cast = self._cast, None
        if cast is not None:
            try:
                await cast.stop()
            except Exception:
                logger.debug("recording: screencast did not stop cleanly", exc_info=True)
        await self._end_process()
        size = 0
        try:
            size = self.path.stat().st_size
        except OSError:
            pass
        if size > 0:
            _handoff(self.path, self.agent)
        saved = SavedFile(
            path=self.path,
            workspace_root=self.workspace_root,
            bytes=size,
            seconds=self.frames / RECORD_FPS,
        )
        if self._done is not None and not self._done.done():
            self._done.set_result(saved)
        sess = sessions.get(self.agent)
        if sess is not None and sess.recorder is self:
            sess.recorder = None
            sessions._emit(self.agent, "recording")
        if unlisted:
            logger.info("recording: %s saved on its own (%s)", self.agent, saved.relative)
        return saved

    async def _end_process(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), FINISH_TIMEOUT_S)
        except TimeoutError:
            logger.warning("recording: ffmpeg did not finish in time; killing it")
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        if proc.returncode not in (0, None):
            err = b""
            try:
                if proc.stderr is not None:
                    err = await asyncio.wait_for(proc.stderr.read(), 2.0)
            except Exception:
                pass
            logger.warning(
                "recording: ffmpeg exited %s: %s",
                proc.returncode,
                err.decode(errors="replace")[-500:],
            )


def _handoff(path: Path, agent: str) -> None:
    """The file belongs to the agent (its sandbox is another uid)."""
    try:
        from olano.tools._sandbox_exec import handoff_to_sandbox

        handoff_to_sandbox(path, agent=agent)
    except Exception:
        logger.debug("recording: hand-off skipped", exc_info=True)


# ── The session's recording ─────────────────────────────────────────────────


def recording_state(sess: Any) -> dict[str, Any] | None:
    """What the live view shows while a recording runs, else ``None``."""
    recorder = getattr(sess, "recorder", None)
    return recorder.public() if recorder is not None else None


async def start_recording(agent: str, workspace_root: Path) -> dict[str, Any]:
    sess = sessions.get(agent)
    if sess is None or sess.page is None or sess.page.is_closed():
        raise RecordingUnavailable("This agent has no open browser.")
    running: Recorder | None = sess.recorder
    if running is not None:
        return running.public()
    recorder = Recorder(agent, workspace_root)
    await recorder.start()
    sess.recorder = recorder
    sess.touch()
    sessions._emit(agent, "recording")
    return recorder.public()


async def stop_recording(agent: str) -> SavedFile | None:
    """End the agent's recording and return the file, or ``None`` when none ran."""
    sess = sessions.get(agent)
    recorder: Recorder | None = sess.recorder if sess is not None else None
    if recorder is None:
        return None
    return await recorder.stop()


async def take_picture(agent: str, workspace_root: Path) -> SavedFile:
    """The page as it is now, as a PNG under ``screenshots/``."""
    sess = sessions.get(agent)
    if sess is None or sess.page is None or sess.page.is_closed():
        raise RecordingUnavailable("This agent has no open browser.")
    folder = workspace_root / PICTURES_DIR
    created = not folder.exists()
    folder.mkdir(parents=True, exist_ok=True)
    if created:
        _handoff(folder, agent)
    path = folder / picture_name()
    await sess.page.screenshot(path=str(path), full_page=False)
    _handoff(path, agent)
    sess.touch()
    return SavedFile(path=path, workspace_root=workspace_root, bytes=path.stat().st_size)
