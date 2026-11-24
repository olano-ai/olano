"""Opt-in turn tracing to an operator's Langfuse project.

Off by default. When a deployment (``config.yaml → observability.langfuse``) or
an agent (``agent.json5 → observability.langfuse``) turns it on and Langfuse
keys resolve, every agent turn becomes one Langfuse trace: a root span named
for the turn's origin, with the LangChain model and tool spans nested under
it, grouped by thread as a Langfuse session.

Layout, cheapest first, because most of this package must stay importable on
every turn without loading the SDK:

- :mod:`.policy`: who is traced and how (``effective_trace_policy``). No SDK.
- :mod:`.credentials`: reserved vault rows, then the ``LANGFUSE_*`` env. No SDK.
- :mod:`.ids`: deterministic trace ids, sampling, links. No SDK.
- :mod:`.masking`: what an exported span may carry. No SDK.
- :mod:`.exporter`: the OTLP exporter wrapper that applies the mask. Imports
  OpenTelemetry, only from :mod:`.tracing`.
- :mod:`.tracing`: client cache and the per-turn API the runner calls. The
  only module that imports ``langfuse``, and only inside functions.

``tests/test_observability_import_deferral.py`` pins that a turn with tracing
off never imports ``langfuse``.
"""
