"""The OTLP exporter every Langfuse client of this process exports through.

Langfuse 5's own ``mask_otel_spans`` hook can rewrite attributes but not a
span's status description, and a failing tool's error text rides there too.
So instead of letting the SDK build its exporter, Olano passes this one as
``span_exporter=``: every span is cloned through :mod:`.masking` (attributes,
status description, event attributes) and only then handed to a plain
``OTLPSpanExporter`` aimed at the Langfuse ingestion endpoint.

The endpoint and headers mirror what ``langfuse._client.span_processor``
builds when it is not given an exporter; ``tests/test_observability_exporter.py``
pins them to the SDK's, so an upstream change fails a test instead of
silently breaking ingestion.

Fail closed: if masking raises, the batch is dropped (and counted), never
exported unmasked.
"""

from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.util import BoundedList
from opentelemetry.trace import Status
from opentelemetry.util.types import Attributes

from olano.observability.masking import (
    MaskContext,
    mask_attributes,
    mask_event_attributes,
    mask_status_description,
)

logger = logging.getLogger(__name__)

INGESTION_PATH = "/api/public/otel/v1/traces"


def ingestion_headers(public_key: str, secret_key: str) -> dict[str, str]:
    """The headers Langfuse's own exporter sends (see the module docstring)."""
    from langfuse._version import __version__ as langfuse_version

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
    return {
        "Authorization": f"Basic {auth}",
        "x-langfuse-sdk-name": "python",
        "x-langfuse-sdk-version": langfuse_version,
        "x-langfuse-public-key": public_key,
        "x-langfuse-ingestion-version": "4",
    }


def build_otlp_exporter(host: str, public_key: str, secret_key: str, *, timeout: int = 10) -> Any:
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=f"{host.rstrip('/')}{INGESTION_PATH}",
        headers=ingestion_headers(public_key, secret_key),
        timeout=timeout,
        compression=Compression.Gzip,
    )


class ExportStats:
    """Counters the status endpoint reads; thread-safe, process-wide per client."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.exported_spans = 0
        self.failed_batches = 0
        self.masking_failures = 0
        self.last_error = ""
        self.last_error_at = 0.0
        self.last_success_at = 0.0

    def ok(self, spans: int) -> None:
        import time

        with self._lock:
            self.exported_spans += spans
            self.last_success_at = time.time()

    def fail(self, error: str, *, masking: bool = False) -> None:
        import time

        with self._lock:
            if masking:
                self.masking_failures += 1
            else:
                self.failed_batches += 1
            self.last_error = error[:300]
            self.last_error_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "exported_spans": self.exported_spans,
                "failed_batches": self.failed_batches,
                "masking_failures": self.masking_failures,
                "last_error": self.last_error,
                "last_error_at": self.last_error_at or None,
                "last_success_at": self.last_success_at or None,
            }


def _trace_hex(span: ReadableSpan) -> str:
    ctx = span.context
    return format(ctx.trace_id, "032x") if ctx is not None else ""


def mask_span(span: ReadableSpan, ctx: MaskContext) -> ReadableSpan:
    """A copy of ``span`` carrying only what ``ctx`` allows."""
    from opentelemetry.attributes import BoundedAttributes

    attributes = BoundedAttributes(
        maxlen=None,
        attributes=mask_attributes(dict(span.attributes or {}), ctx),
        immutable=True,
    )
    events: BoundedList = BoundedList(None)
    for ev in span.events:
        masked: Attributes = mask_event_attributes(dict(ev.attributes or {}), ctx)
        events.append(Event(ev.name, masked, ev.timestamp))
    status = span.status
    if status is not None and status.description:
        status = Status(status.status_code, mask_status_description(status.description, ctx))
    return ReadableSpan(
        name=span.name,
        context=span.context,
        parent=span.parent,
        resource=span.resource,
        attributes=attributes,
        events=events,
        links=span.links,
        kind=span.kind,
        status=status,
        start_time=span.start_time,
        end_time=span.end_time,
        instrumentation_scope=span.instrumentation_scope,
    )


class MaskingSpanExporter(SpanExporter):
    """Mask every span by its trace's :class:`MaskContext`, then export."""

    def __init__(
        self,
        inner: SpanExporter,
        lookup: Callable[[str], MaskContext],
        stats: ExportStats | None = None,
    ) -> None:
        self._inner = inner
        self._lookup = lookup
        self.stats = stats or ExportStats()

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            masked = [mask_span(span, self._lookup(_trace_hex(span))) for span in spans]
        except Exception as exc:
            # Never export what could not be masked. SUCCESS, so the batch
            # processor does not retry the same spans into the same failure.
            logger.warning("observability: masking failed, batch of %d dropped", len(spans))
            self.stats.fail(f"masking: {type(exc).__name__}", masking=True)
            return SpanExportResult.SUCCESS
        if not masked:
            return SpanExportResult.SUCCESS
        try:
            result = self._inner.export(masked)
        except Exception as exc:
            self.stats.fail(f"{type(exc).__name__}: {exc}")
            return SpanExportResult.FAILURE
        if result == SpanExportResult.SUCCESS:
            self.stats.ok(len(masked))
        else:
            self.stats.fail("the Langfuse host refused or did not answer the export")
        return result

    def shutdown(self) -> None:
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        flush = getattr(self._inner, "force_flush", None)
        return bool(flush(timeout_millis)) if callable(flush) else True
