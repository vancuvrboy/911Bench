"""Tests for SQLite runtime state persistence."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gov_server.state_store import StateStore


class StateStoreTest(unittest.TestCase):
    def test_roundtrip_idempotency_and_audit_and_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = StateStore(db_path)
            store.put_idempotency("idem-1", "fp-1", {"decision": "executed"})
            store.put_action_audit("act-1", "audit-1", {"decision": "executed"})
            store.put_southbound_cursor("inc-1", 42)

            idem = store.get_idempotency("idem-1")
            self.assertIsNotNone(idem)
            self.assertEqual(idem["fingerprint"], "fp-1")
            self.assertEqual(idem["outcome"]["decision"], "executed")

            audit = store.get_action_audit("act-1")
            self.assertIsNotNone(audit)
            self.assertEqual(audit["audit_ref"], "audit-1")
            self.assertEqual(audit["audit_entry"]["decision"], "executed")

            cursor = store.get_southbound_cursor("inc-1")
            self.assertEqual(cursor, 42)

    def test_audit_chain_append_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "state.db"
            store = StateStore(db_path)
            first = store.append_audit_chain_event({"action_id": "a1", "decision": "executed"})
            second = store.append_audit_chain_event({"action_id": "a2", "decision": "denied"})
            self.assertEqual(first["seq"], 1)
            self.assertEqual(second["seq"], 2)
            verify = store.verify_audit_chain()
            self.assertTrue(verify["ok"])
            self.assertEqual(verify["length"], 2)


if __name__ == "__main__":
    unittest.main()
