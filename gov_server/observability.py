"""Structured observability helpers for governance server."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Any

JSONObject = dict[str, Any]


class Observability:
    def __init__(self, component: str, max_latency_samples: int = 5000) -> None:
        self.component = component
        self._lock = threading.RLock()
        self._counts: dict[str, int] = defaultdict(int)
        self._latencies_ms: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=max_latency_samples))
        self._events: deque[JSONObject] = deque(maxlen=5000)
        self._event_seq = 0
        self._logger = logging.getLogger("gov_server")

    def incr(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counts[name] += int(value)

    def observe_latency_ms(self, name: str, value_ms: float) -> None:
        with self._lock:
            self._latencies_ms[name].append(float(value_ms))

    def event(self, event_type: str, **fields: Any) -> None:
        payload: JSONObject = {
            "ts": int(time.time() * 1000),
            "component": self.component,
            "event_type": event_type,
            **fields,
        }
        with self._lock:
            self._event_seq += 1
            payload["seq"] = self._event_seq
            self._events.append(payload)
        self._logger.info(json.dumps(payload, separators=(",", ":"), sort_keys=True))

    def snapshot(self) -> JSONObject:
        with self._lock:
            counts = dict(self._counts)
            lats = {k: list(v) for k, v in self._latencies_ms.items()}
            last_seq = self._event_seq
        return {
            "component": self.component,
            "counts": counts,
            "latency_ms": {name: self._summarize(values) for name, values in lats.items()},
            "event_seq": last_seq,
        }

    def events_since(self, cursor: int, verbosity: str = "normal", limit: int = 200) -> JSONObject:
        with self._lock:
            items = [dict(e) for e in self._events if int(e.get("seq", 0)) > int(cursor)]
            latest = self._event_seq
        if limit > 0:
            items = items[:limit]
        if verbosity != "debug":
            items = [self._to_normal_event(e) for e in items]
        new_cursor = int(items[-1]["seq"]) if items else int(cursor)
        return {
            "cursor": int(cursor),
            "new_cursor": new_cursor,
            "latest_seq": latest,
            "verbosity": verbosity,
            "events": items,
        }

    @staticmethod
    def _to_normal_event(event: JSONObject) -> JSONObject:
        keep = {
            "seq",
            "ts",
            "component",
            "event_type",
            "correlation_id",
            "method",
            "path",
            "status",
            "incident_id",
            "action_id",
            "action_class",
            "decision",
            "denial_reason",
            "error",
        }
        return {k: v for k, v in event.items() if k in keep and v not in (None, "", [])}

    @staticmethod
    def _summarize(values: list[float]) -> JSONObject:
        if not values:
            return {"count": 0}
        ordered = sorted(values)

        def pct(p: float) -> float:
            idx = int((len(ordered) - 1) * p)
            return float(ordered[idx])

        return {
            "count": len(ordered),
            "min": float(ordered[0]),
            "max": float(ordered[-1]),
            "p50": pct(0.5),
            "p95": pct(0.95),
            "p99": pct(0.99),
        }
