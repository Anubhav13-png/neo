"""Tracing: one trace per question, a span per step with timing and attributes, saved with the run.

The span shape (name, start, duration, attributes, parent) maps one-to-one onto OpenTelemetry; export by
reading trace.json or by replacing `Tracer.emit` with an OTLP exporter.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional


class Tracer:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.spans: List[Dict[str, Any]] = []
        self._stack: List[int] = []

    @contextmanager
    def span(self, name: str, **attrs):
        idx = len(self.spans)
        rec = {"name": name, "start": time.time(), "parent": self._stack[-1] if self._stack else None,
               "attributes": dict(attrs), "status": "ok"}
        self.spans.append(rec)
        self._stack.append(idx)
        try:
            yield rec["attributes"]
        except Exception as e:
            rec["status"] = "error"
            rec["attributes"]["error"] = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            rec["duration_ms"] = round((time.time() - rec["start"]) * 1000, 1)
            self._stack.pop()
            self.emit(rec)

    def emit(self, span: Dict[str, Any]):
        """Hook for an exporter; spans are kept in memory and saved with the run."""

    def export(self) -> Dict[str, Any]:
        return {"trace_id": self.trace_id, "spans": self.spans}
