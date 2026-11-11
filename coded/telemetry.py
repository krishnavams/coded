"""Tracing: JSONL span log and optional OpenTelemetry export.

Config (``"telemetry"`` in config.json), or ``--trace FILE`` / ``$CODED_TRACE_FILE``::

    {"jsonl": "~/.cache/coded/trace.jsonl", "otel": true}

Each span is one JSON line: ``trace_id``, ``span_id``, ``parent_id``, ``name``,
``start``, ``duration_ms``, ``status`` and ``attrs`` (model, token counts, tool
name, …). With ``otel`` and the ``opentelemetry-sdk`` +
``opentelemetry-exporter-otlp`` packages installed, spans are also exported via
OTLP, configured by the standard ``OTEL_*`` environment variables.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

_current: contextvars.ContextVar = contextvars.ContextVar("coded_span", default=None)


class Tracer:
    def __init__(self, jsonl: Optional[str] = None, otel: bool = False):
        self.path = os.path.expanduser(jsonl) if jsonl else None
        self._lock = threading.Lock()
        self._otel = _otel_tracer() if otel else None
        if self.path:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(self.path or self._otel)

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[Dict[str, Any]]:
        """Time a block. Mutate the yielded dict to add attributes."""
        if not self.enabled:
            yield attrs
            return
        parent = _current.get()
        span = {
            "trace_id": parent["trace_id"] if parent else uuid.uuid4().hex,
            "span_id": uuid.uuid4().hex[:16],
            "parent_id": parent["span_id"] if parent else None,
            "name": name,
        }
        token = _current.set(span)
        start = time.time()
        status = "ok"
        otel_cm = self._otel.start_as_current_span(f"coded.{name}") if self._otel else None
        otel_span = otel_cm.__enter__() if otel_cm else None
        try:
            yield attrs
        except BaseException as exc:
            status = f"error: {type(exc).__name__}"
            raise
        finally:
            _current.reset(token)
            if otel_span is not None:
                for k, v in attrs.items():
                    if isinstance(v, (str, bool, int, float)):
                        otel_span.set_attribute(f"coded.{k}", v)
                otel_cm.__exit__(None, None, None)
            if self.path:
                record = dict(span, start=start, duration_ms=round((time.time() - start) * 1000, 2),
                              status=status, attrs=attrs)
                line = json.dumps(record, default=str)
                with self._lock, open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")


def _otel_tracer():
    try:
        from opentelemetry import trace
    except ImportError:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        if not isinstance(trace.get_tracer_provider(), TracerProvider):
            provider = TracerProvider(resource=Resource.create({"service.name": "coded"}))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            trace.set_tracer_provider(provider)
    except ImportError:
        pass  # API only: spans go to whatever provider the host configured
    return trace.get_tracer("coded")


def tracer_from_config(config: Any, override_path: Optional[str] = None) -> Tracer:
    tel = getattr(config, "telemetry", None) or {}
    path = override_path or tel.get("jsonl") or os.environ.get("CODED_TRACE_FILE")
    return Tracer(jsonl=path, otel=bool(tel.get("otel")))


def run_in_context(fn, *args, **kwargs):
    """Wrap `fn` so it runs in a copy of the caller's context (for thread pools)."""
    ctx = contextvars.copy_context()
    return lambda: ctx.run(fn, *args, **kwargs)
