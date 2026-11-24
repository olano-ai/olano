"""Rebuild a reply's ordered spans from the frames a group stream forwards.

A 1:1 turn gets its ``segments`` from inside the runner (``_take_text``), which
sees the raw event stream. A group turn cannot: ``groups_routes`` consumes the
runner's *output* — the same ``("text", …)`` / ``("tool_call", …)`` tuples the
dashboard sees — and forwards a filtered subset of them to the client. So the
route rebuilds the spans the way the CLIENT does (``sessionStore.appendText``),
not the way the runner does, and this module is that algorithm on its own so it
can be tested without a stream.

The runner has *already* inserted the ``"\\n\\n"`` separator into the text it
yields, so nothing here needs a paragraph rule — only the boundaries: a span
opens when text resumes after a tool call, tagged with that call's id.

The invariant every consumer depends on: joining the spans reproduces the body.
``turnBlocks.buildTurnBlocks`` silently falls back to the old stacked layout when
they disagree (``segmentsCover``), with no error anywhere, so a normalization
applied to one and not the other disables interleaving product-wide rather than
failing. ``tests/test_groups_streaming.py`` pins it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SegmentAccumulator:
    """Folds a turn's ``text`` / ``tool_call`` frames into ordered spans."""

    pieces: list[str] = field(default_factory=list)
    spans: list[dict[str, Any]] = field(default_factory=list)
    _pending_after: str | None = None
    _opened: bool = False

    def text(self, chunk: str) -> None:
        """Record a chunk of the reply."""
        if not chunk:
            return
        self.pieces.append(chunk)
        if self._pending_after is not None or not self._opened:
            self.spans.append({"text": chunk, "after": self._pending_after})
            self._opened = True
            self._pending_after = None
        else:
            self.spans[-1]["text"] += chunk

    def tool_call(self, call_id: Any) -> None:
        """Note that a tool ran: the next text opens a span behind this call."""
        self._pending_after = str(call_id or "")

    @property
    def body(self) -> str:
        """The reply as it is persisted and rendered."""
        return "".join(self.pieces)

    def segments(self) -> list[dict[str, Any]]:
        """The spans worth persisting, or ``[]``.

        One span is the pre-segment layout — the renderer handles its absence —
        so a turn that never interleaved writes nothing and its row stays
        byte-identical to what earlier releases produced.
        """
        return self.spans if len(self.spans) > 1 else []
