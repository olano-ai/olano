"""What ``read_file`` may hand the model, decided before the file is read.

deepagents' ``read_file`` returns any file whose extension it maps to a media
type (``backends/utils._EXTENSION_TO_FILE_TYPE``: images, audio, video, PDF,
PowerPoint) as **the whole file, base64-encoded, in one content block**, with
no size limit except for video. That table is the list of formats a native
multimodal model can take as input, and for images it is useful: a vision
model looks at the chart it just rendered. For everything else it is a hazard:

* the context counter charges audio and document blocks by base64 length
  (images get a fixed ~90 tokens), so a 30 second voice note reads as ~320k
  tokens and a 1 MB PDF as ~333k, past every model's window;
* the eviction middleware deliberately keeps media blocks when it offloads a
  large tool result, so compaction can never shrink one, and the block rides
  along on every later model call and in the thread's saved state.

An agent told "the voice note was saved to uploads/voice-1.wav" that reached
for ``read_file`` put the runtime past its memory limit and the container was
OOM-killed. Every one of these formats already has a tool that turns it into
text, so ``read_file`` refuses them and names that tool. Anything else that is
not text (an archive, an executable, a database) is refused by its bytes,
the same sniff ``document_to_text`` uses, rather than failing on a UTF-8
decode error that tells the model nothing.
"""

from __future__ import annotations

from pathlib import Path

from olano.uploads import (
    ARCHIVE_EXTENSIONS,
    DOCUMENT_EXTENSIONS,
    TEXT_SNIFF_BYTES,
    human_size,
    looks_like_text,
)

AUDIO_EXTENSIONS = frozenset(
    {
        ".wav",
        ".mp3",
        ".mpga",
        ".aiff",
        ".aif",
        ".aac",
        ".m4a",
        ".ogg",
        ".oga",
        ".opus",
        ".flac",
        ".wma",
        ".amr",
    }
)
VIDEO_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".m4v",
        ".mpeg",
        ".mpg",
        ".mov",
        ".avi",
        ".flv",
        ".webm",
        ".wmv",
        ".mkv",
        ".ogv",
        ".3gp",
        ".3gpp",
    }
)
#: Formats every vision provider takes as an image block. ``read_file`` keeps
#: returning these: it is how an agent looks at an image it produced.
INLINE_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
#: Images a provider will not take inline (HEIC from an iPhone, BMP, TIFF).
OTHER_IMAGE_EXTENSIONS = frozenset({".heic", ".heif", ".bmp", ".tif", ".tiff", ".ico"})
#: Documents ``document_to_text`` converts. HTML is excluded: it is text, and
#: agents read and edit their pages with ``read_file``.
CONVERTED_DOCUMENT_EXTENSIONS = DOCUMENT_EXTENSIONS - {".html", ".htm"}

#: Largest image ``read_file`` returns inline. Providers reject a single image
#: of a few MB (Anthropic's limit is 5 MB), and base64 adds a third.
MAX_INLINE_IMAGE_BYTES = 3_750_000


def read_file_refusal(path: Path) -> str:
    """Why ``read_file`` must not return ``path``, or ``""`` when it may.

    ``path`` is the host path the tool would read. The extension decides the
    media and document formats (a file that does not exist yet is still refused
    by type, so the model learns the right tool before it looks for the file);
    the size decides an image; the bytes decide whether anything else is text.
    Unreadable files are left for the tool to report.
    """
    suffix = path.suffix.lower()
    name = path.name
    if suffix in AUDIO_EXTENSIONS:
        return (
            f"read_file does not open audio. {name} is a recording: call "
            f'audio_to_text(file_path="{path}") to get its words. If a transcript of it '
            "is already in the conversation (a voice message is transcribed when it "
            "arrives), use that and do not process the file again."
        )
    if suffix in VIDEO_EXTENSIONS:
        return (
            f"read_file does not open video. To get what is said in {name}, call "
            f'video_to_text(file_path="{path}").'
        )
    if suffix in CONVERTED_DOCUMENT_EXTENSIONS:
        return (
            f"read_file does not open {suffix} documents. Call "
            f'document_to_text(file_path="{path}") to read {name} as text.'
        )
    if suffix in OTHER_IMAGE_EXTENSIONS:
        return (
            f"read_file cannot show a {suffix} image. Call "
            f'image_describe(image="{path}") to look at {name}, or convert it to PNG '
            "or JPEG with the shell if you have one."
        )
    if suffix in ARCHIVE_EXTENSIONS:
        return (
            f"{name} is an archive, not a file read_file can show. List or unpack it "
            "with the shell if you have one (unzip -l / tar -tf), then read the files "
            "inside."
        )
    try:
        if not path.is_file():
            return ""
        if suffix in INLINE_IMAGE_EXTENSIONS:
            size = path.stat().st_size
            if size > MAX_INLINE_IMAGE_BYTES:
                return (
                    f"{name} is {human_size(size)}, too large to show inline (the limit is "
                    f"{human_size(MAX_INLINE_IMAGE_BYTES)}). Call "
                    f'image_describe(image="{path}") instead, or scale it down first.'
                )
            return ""
        with path.open("rb") as fh:
            head = fh.read(TEXT_SNIFF_BYTES)
    except OSError:
        return ""
    if looks_like_text(head):
        return ""
    return (
        f"{name} is a binary file, not text, so read_file cannot show it. Inspect it "
        "with the shell if you have one (for example `file` on its path), or use the "
        "tool made for its format."
    )
