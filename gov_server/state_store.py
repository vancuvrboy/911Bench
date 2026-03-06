"""SQLite-backed persistence for governance runtime state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import hashlib
from pathlib import Path
from typing import Any

JSONObject = dict[str, Any]


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                  key TEXT PRIMARY KEY,
                  fingerprint TEXT NOT NULL,
                  outcome_json TEXT NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS action_audit (
                  action_id TEXT PRIMARY KEY,
                  audit_ref TEXT NOT NULL,
                  audit_entry_json TEXT,
                  updated_at INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS southbound_cursor (
                  incident_id TEXT PRIMARY KEY,
                  cursor INTEGER NOT NULL,
                  updated_at INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_chain (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  prev_hash TEXT NOT NULL,
                  event_json TEXT NOT NULL,
                  event_hash TEXT NOT NULL,
                  created_at INTEGER NOT NULL
                )
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __del__(self) -> None:  # pragma: no cover
        try:
            self.close()
        except Exception:
            pass

    def get_idempotency(self, key: str) -> JSONObject | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT fingerprint, outcome_json FROM idempotency WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return {"fingerprint": row[0], "outcome": json.loads(row[1])}

    def put_idempotency(self, key: str, fingerprint: str, outcome: JSONObject) -> None:
        payload = json.dumps(outcome, separators=(",", ":"), sort_keys=True)
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO idempotency(key, fingerprint, outcome_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  fingerprint = excluded.fingerprint,
                  outcome_json = excluded.outcome_json,
                  updated_at = excluded.updated_at
                """,
                (key, fingerprint, payload, now),
            )
            self._conn.commit()

    def get_action_audit(self, action_id: str) -> JSONObject | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT audit_ref, audit_entry_json FROM action_audit WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "audit_ref": row[0],
            "audit_entry": json.loads(row[1]) if row[1] else None,
        }

    def put_action_audit(self, action_id: str, audit_ref: str, audit_entry: JSONObject | None = None) -> None:
        now = int(time.time())
        entry = json.dumps(audit_entry, separators=(",", ":"), sort_keys=True) if audit_entry is not None else None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO action_audit(action_id, audit_ref, audit_entry_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                  audit_ref = excluded.audit_ref,
                  audit_entry_json = excluded.audit_entry_json,
                  updated_at = excluded.updated_at
                """,
                (action_id, audit_ref, entry, now),
            )
            self._conn.commit()

    def get_southbound_cursor(self, incident_id: str) -> int | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM southbound_cursor WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
        return int(row[0]) if row is not None else None

    def put_southbound_cursor(self, incident_id: str, cursor: int) -> None:
        now = int(time.time())
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO southbound_cursor(incident_id, cursor, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                  cursor = excluded.cursor,
                  updated_at = excluded.updated_at
                """,
                (incident_id, int(cursor), now),
            )
            self._conn.commit()

    def append_audit_chain_event(self, event: JSONObject) -> JSONObject:
        now = int(time.time())
        event_json = json.dumps(event, separators=(",", ":"), sort_keys=True)
        with self._lock:
            row = self._conn.execute(
                "SELECT event_hash FROM audit_chain ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row[0] if row is not None else "GENESIS"
            event_hash = hashlib.sha256(f"{prev_hash}:{event_json}".encode("utf-8")).hexdigest()
            cur = self._conn.execute(
                """
                INSERT INTO audit_chain(prev_hash, event_json, event_hash, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (prev_hash, event_json, event_hash, now),
            )
            seq = int(cur.lastrowid)
            self._conn.commit()
        return {"seq": seq, "prev_hash": prev_hash, "event_hash": event_hash}

    def verify_audit_chain(self) -> JSONObject:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, prev_hash, event_json, event_hash FROM audit_chain ORDER BY seq ASC"
            ).fetchall()
        prev_hash = "GENESIS"
        for seq, stored_prev, event_json, stored_hash in rows:
            if stored_prev != prev_hash:
                return {"ok": False, "error": f"prev_hash_mismatch_at_seq:{seq}"}
            computed = hashlib.sha256(f"{stored_prev}:{event_json}".encode("utf-8")).hexdigest()
            if computed != stored_hash:
                return {"ok": False, "error": f"event_hash_mismatch_at_seq:{seq}"}
            prev_hash = stored_hash
        return {"ok": True, "length": len(rows), "head_hash": prev_hash}
