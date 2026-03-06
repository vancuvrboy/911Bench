"""Tests for southbound retry/backoff and circuit-breaker behavior."""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from gov_server.southbound import (
    SimulationSouthboundClient,
    SouthboundCircuitOpenError,
    SouthboundPermanentError,
    SouthboundTransientError,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class SouthboundResilienceTest(unittest.TestCase):
    def test_retry_recovers_from_transient_error(self) -> None:
        client = SimulationSouthboundClient(
            base_url="http://127.0.0.1:8300",
            retry_attempts=2,
            retry_backoff_sec=0.0,
        )
        with patch("gov_server.southbound.urllib.request.urlopen") as mocked_open:
            mocked_open.side_effect = [urllib.error.URLError("temporary"), _FakeResponse({"status": "ok"})]
            out = client.get_state_snapshot("inc-1")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(mocked_open.call_count, 2)

    def test_non_retryable_http_400_is_permanent(self) -> None:
        client = SimulationSouthboundClient(
            base_url="http://127.0.0.1:8300",
            retry_attempts=3,
            retry_backoff_sec=0.0,
        )
        http_400 = urllib.error.HTTPError(
            url="http://127.0.0.1:8300/plant/get_state_snapshot",
            code=400,
            msg="bad request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad_request"}'),
        )
        with patch("gov_server.southbound.urllib.request.urlopen", side_effect=http_400) as mocked_open:
            with self.assertRaises(SouthboundPermanentError):
                client.get_state_snapshot("inc-2")
        self.assertEqual(mocked_open.call_count, 1)

    def test_circuit_opens_after_threshold(self) -> None:
        client = SimulationSouthboundClient(
            base_url="http://127.0.0.1:8300",
            retry_attempts=0,
            retry_backoff_sec=0.0,
            circuit_fail_threshold=2,
            circuit_open_sec=60.0,
        )
        with patch(
            "gov_server.southbound.urllib.request.urlopen",
            side_effect=urllib.error.URLError("down"),
        ) as mocked_open:
            with self.assertRaises(SouthboundTransientError):
                client.get_state_snapshot("inc-3")
            with self.assertRaises(SouthboundTransientError):
                client.get_state_snapshot("inc-3")
            with self.assertRaises(SouthboundCircuitOpenError):
                client.get_state_snapshot("inc-3")
        self.assertEqual(mocked_open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
