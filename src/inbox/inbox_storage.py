from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from core.storage_atomic import load_json_file
from paths import storage_root


class InboxStorage:
    THREADS_FILE = "inbox_threads.json"
    MESSAGES_FILE = "inbox_messages.json"
    STATE_FILE = "inbox_state.json"
    DATABASE_FILE = "inbox_rm.sqlite3"
    _MAX_MESSAGES_PER_THREAD = 80
    _MAX_ACTIVE_THREADS = 500
    _MAX_THREADS_PER_ACCOUNT = _MAX_ACTIVE_THREADS
    _LOCAL_TAG_FOLLOW_UP = "seguimiento"
    _RESPONSE_BLOCK_WINDOW_SECONDS = 180.0

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = Path(root_dir)
        self._storage_dir = storage_root(self._root_dir)
        self._storage_dir.mkdir(parents=True, exist_ok=True)
        self._threads_path = self._storage_dir / self.THREADS_FILE
        self._messages_path = self._storage_dir / self.MESSAGES_FILE
        self._state_path = self._storage_dir / self.STATE_FILE
        self._database_path = self._storage_dir / self.DATABASE_FILE
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._create_schema()
        self._migrate_legacy_json_if_needed()

    def _configure_connection(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")

    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox_threads (
                    thread_key TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    thread_href TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL,
                    account_alias TEXT NOT NULL DEFAULT '',
                    recipient_username TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    last_message_text TEXT NOT NULL DEFAULT '',
                    last_message_timestamp REAL,
                    last_message_direction TEXT NOT NULL DEFAULT 'unknown',
                    last_message_id TEXT NOT NULL DEFAULT '',
                    unread_count INTEGER NOT NULL DEFAULT 0,
                    needs_reply INTEGER NOT NULL DEFAULT 0,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    participants_json TEXT NOT NULL DEFAULT '[]',
                    last_synced_at REAL,
                    last_seen_text TEXT NOT NULL DEFAULT '',
                    last_seen_at REAL,
                    latest_customer_message_at REAL
                );
                CREATE INDEX IF NOT EXISTS inbox_threads_account_ts_idx
                    ON inbox_threads(account_id, last_message_timestamp DESC, thread_key);

                CREATE TABLE IF NOT EXISTS inbox_messages (
                    thread_key TEXT NOT NULL,
                    block_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    timestamp REAL,
                    direction TEXT NOT NULL DEFAULT 'unknown',
                    user_id TEXT NOT NULL DEFAULT '',
                    delivery_status TEXT NOT NULL DEFAULT 'sent',
                    local_echo INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(thread_key, block_id, ordinal),
                    FOREIGN KEY(thread_key) REFERENCES inbox_threads(thread_key) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS inbox_messages_thread_ts_idx
                    ON inbox_messages(thread_key, timestamp, ordinal);

                CREATE TABLE IF NOT EXISTS inbox_thread_state (
                    thread_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY(thread_key) REFERENCES inbox_threads(thread_key) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS inbox_account_state (
                    account_id TEXT PRIMARY KEY,
                    session_marker TEXT NOT NULL DEFAULT '',
                    session_started_at REAL,
                    last_sync_at REAL,
                    last_error TEXT NOT NULL DEFAULT '',
                    thread_count INTEGER NOT NULL DEFAULT 0,
                    health_state TEXT NOT NULL DEFAULT 'healthy',
                    health_reason TEXT NOT NULL DEFAULT '',
                    health_updated_at REAL
                );

                CREATE TABLE IF NOT EXISTS thread_action_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    pack_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS thread_action_memory_lookup_idx
                    ON thread_action_memory(account_id, thread_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS inbox_send_queue_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL DEFAULT '',
                    thread_key TEXT NOT NULL DEFAULT '',
                    account_id TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS inbox_send_queue_state_idx
                    ON inbox_send_queue_jobs(state, created_at);

                CREATE TABLE IF NOT EXISTS inbox_deleted_threads (
                    thread_key TEXT PRIMARY KEY,
                    account_id TEXT NOT NULL DEFAULT '',
                    deleted_at REAL NOT NULL,
                    last_activity_timestamp REAL
                );
                CREATE INDEX IF NOT EXISTS inbox_deleted_threads_account_idx
                    ON inbox_deleted_threads(account_id, deleted_at DESC);
                """
            )
            self._conn.commit()

    def _migrate_legacy_json_if_needed(self) -> None:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS total FROM inbox_threads").fetchone()
            if int(self._row_value(row, "total", 0) or 0):
                return
        if not (self._threads_path.exists() or self._messages_path.exists() or self._state_path.exists()):
            return

        threads_payload = load_json_file(self._threads_path, {"threads": {}}, label="inbox.legacy.threads")
        messages_payload = load_json_file(self._messages_path, {"messages": {}}, label="inbox.legacy.messages")
        state_payload = load_json_file(self._state_path, {"accounts": {}, "threads": {}}, label="inbox.legacy.state")

        raw_threads = threads_payload.get("threads") if isinstance(threads_payload, dict) else {}
        raw_messages = messages_payload.get("messages") if isinstance(messages_payload, dict) else {}
        raw_state_accounts = state_payload.get("accounts") if isinstance(state_payload, dict) else {}
        raw_state_threads = state_payload.get("threads") if isinstance(state_payload, dict) else {}

        if isinstance(raw_threads, dict):
            migrated_threads: list[dict[str, Any]] = []
            for fallback_key, item in raw_threads.items():
                if not isinstance(item, dict):
                    continue
                payload = dict(item)
                payload.setdefault("thread_key", str(fallback_key or "").strip())
                migrated_threads.append(payload)
            self.upsert_threads(migrated_threads)
        if isinstance(raw_messages, dict):
            for thread_key, rows in raw_messages.items():
                if isinstance(rows, list):
                    self.replace_messages(str(thread_key or "").strip(), rows)
        if isinstance(raw_state_threads, dict):
            for thread_key, state in raw_state_threads.items():
                if isinstance(state, dict):
                    self.update_thread_state(str(thread_key or "").strip(), dict(state))
        if isinstance(raw_state_accounts, dict):
            for account_id, state in raw_state_accounts.items():
                if not isinstance(state, dict):
                    continue
                clean_account = self._clean_account_id(account_id)
                if not clean_account:
                    continue
                self.register_account_sync(
                    clean_account,
                    last_error=str(state.get("last_error") or "").strip(),
                    thread_count=state.get("thread_count"),
                )
                self.prepare_account_session(
                    clean_account,
                    session_marker=str(state.get("session_marker") or f"legacy:{clean_account}").strip(),
                    started_at=self._coerce_timestamp(state.get("session_started_at")),
                )
                self.set_account_health(
                    clean_account,
                    str(state.get("health_state") or "healthy"),
                    reason=str(state.get("health_reason") or "").strip(),
                    updated_at=self._coerce_timestamp(state.get("health_updated_at")),
                )

    @staticmethod
    def _clean_account_id(value: Any) -> str:
        return str(value or "").strip().lstrip("@").lower()

    @staticmethod
    def _coerce_timestamp(value: Any) -> float | None:
        try:
            stamp = float(value)
        except Exception:
            return None
        return stamp if stamp > 0 else None

    @staticmethod
    def _split_thread_key(thread_key: str) -> tuple[str, str]:
        clean_key = str(thread_key or "").strip()
        if ":" not in clean_key:
            return "", ""
        raw_account, raw_thread = clean_key.split(":", 1)
        return InboxStorage._clean_account_id(raw_account), str(raw_thread or "").strip()

    @staticmethod
    def _normalize_participants(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        rows: list[str] = []
        seen: set[str] = set()
        for raw in value:
            text = str(raw or "").strip().lstrip("@")
            key = text.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(text)
        return rows

    @staticmethod
    def _normalize_tags(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        rows: list[str] = []
        seen: set[str] = set()
        for raw in value:
            text = str(raw or "").strip()
            key = text.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(text)
        return rows

    @staticmethod
    def _encode_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))

    @staticmethod
    def _decode_json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return list(value)
        text = str(value or "").strip()
        if not text:
            return []
        try:
            payload = json.loads(text)
        except Exception:
            return []
        return list(payload) if isinstance(payload, list) else []

    @staticmethod
    def _decode_json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        text = str(value or "").strip()
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _normalize_delivery_status(value: Any) -> str:
        status = str(value or "").strip().lower() or "sent"
        if status == "failed":
            status = "error"
        if status not in {"pending", "sending", "sent", "error"}:
            return "sent"
        return status

    @staticmethod
    def _normalize_health_state(value: Any) -> str:
        state = str(value or "").strip().lower() or "unknown"
        if state not in {
            "healthy",
            "login_required",
            "checkpoint",
            "suspended",
            "banned",
            "proxy_error",
            "unknown",
        }:
            return "unknown"
        return state

    @staticmethod
    def _row_value(row: sqlite3.Row | dict[str, Any] | None, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(key, default)
        try:
            return row[key]
        except Exception:
            return default

    def _normalize_thread_record(self, raw: Any, *, fallback_key: str = "") -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        thread_key = str(raw.get("thread_key") or fallback_key or "").strip()
        thread_id = str(raw.get("thread_id") or "").strip()
        account_id = self._clean_account_id(raw.get("account_id") or raw.get("account"))
        if thread_key and (not thread_id or not account_id):
            fallback_account, fallback_thread = self._split_thread_key(thread_key)
            account_id = account_id or fallback_account
            thread_id = thread_id or fallback_thread
        if not thread_key and account_id and thread_id:
            thread_key = f"{account_id}:{thread_id}"
        if not thread_key or not thread_id or not account_id:
            return None
        participants = self._normalize_participants(raw.get("participants"))
        recipient_username = str(raw.get("recipient_username") or "").strip().lstrip("@")
        if not recipient_username and participants:
            recipient_username = participants[0]
        tags = self._normalize_tags(raw.get("tags"))
        last_direction = str(raw.get("last_message_direction") or "unknown").strip().lower() or "unknown"
        try:
            unread_count = max(0, int(raw.get("unread_count") or 0))
        except Exception:
            unread_count = 0
        needs_reply_raw = raw.get("needs_reply")
        needs_reply = bool(needs_reply_raw) if needs_reply_raw is not None else last_direction == "inbound"
        return {
            "thread_key": thread_key,
            "thread_id": thread_id,
            "thread_href": str(raw.get("thread_href") or "").strip()
            or f"https://www.instagram.com/direct/t/{thread_id}/",
            "account_id": account_id,
            "account_alias": str(raw.get("account_alias") or "").strip(),
            "recipient_username": recipient_username,
            "display_name": str(raw.get("display_name") or raw.get("title") or recipient_username or thread_id).strip(),
            "last_message_text": str(raw.get("last_message_text") or "").strip(),
            "last_message_timestamp": self._coerce_timestamp(raw.get("last_message_timestamp")),
            "last_message_direction": last_direction,
            "last_message_id": str(raw.get("last_message_id") or "").strip(),
            "unread_count": unread_count,
            "needs_reply": 1 if needs_reply else 0,
            "tags": tags,
            "participants": participants,
            "last_synced_at": self._coerce_timestamp(raw.get("last_synced_at")) or time.time(),
            "last_seen_text": str(raw.get("last_seen_text") or "").strip(),
            "last_seen_at": self._coerce_timestamp(raw.get("last_seen_at")),
            "latest_customer_message_at": self._coerce_timestamp(raw.get("latest_customer_message_at")),
        }

    def _normalize_message_record(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        message_id = str(raw.get("message_id") or raw.get("id") or "").strip()
        if not message_id:
            return None
        direction = str(raw.get("direction") or "").strip().lower() or "unknown"
        if direction not in {"inbound", "outbound", "unknown"}:
            direction = "unknown"
        return {
            "message_id": message_id,
            "text": str(raw.get("text") or "").strip(),
            "timestamp": self._coerce_timestamp(raw.get("timestamp")),
            "direction": direction,
            "user_id": str(raw.get("user_id") or "").strip(),
            "delivery_status": self._normalize_delivery_status(raw.get("delivery_status")),
            "local_echo": bool(raw.get("local_echo", False)),
            "error_message": str(raw.get("error_message") or "").strip(),
        }

    def _load_thread_state(self, thread_key: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT state_json FROM inbox_thread_state WHERE thread_key = ?",
            (str(thread_key or "").strip(),),
        ).fetchone()
        if row is None:
            return {}
        return self._decode_json_dict(row["state_json"])

    def _save_thread_state(self, thread_key: str, state: dict[str, Any]) -> None:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return
        self._conn.execute(
            """
            INSERT INTO inbox_thread_state(thread_key, state_json)
            SELECT ?, ?
            WHERE EXISTS(SELECT 1 FROM inbox_threads WHERE thread_key = ?)
            ON CONFLICT(thread_key) DO UPDATE SET state_json = excluded.state_json
            """,
            (
                clean_key,
                self._encode_json(state),
                clean_key,
            ),
        )

    def _account_overlay(self, account_id: str) -> dict[str, Any]:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return {"account_health": "unknown", "account_health_reason": ""}
        row = self._conn.execute(
            """
            SELECT last_sync_at, last_error, thread_count, health_state, health_reason, health_updated_at
            FROM inbox_account_state
            WHERE account_id = ?
            """,
            (clean_account,),
        ).fetchone()
        if row is None:
            return {"account_health": "healthy", "account_health_reason": ""}
        return {
            "account_health": self._normalize_health_state(row["health_state"]),
            "account_health_reason": str(row["health_reason"] or row["last_error"] or "").strip(),
            "account_last_sync_at": self._coerce_timestamp(row["last_sync_at"]),
            "account_last_error": str(row["last_error"] or "").strip(),
            "account_thread_count": max(0, int(row["thread_count"] or 0)),
        }

    @classmethod
    def _thread_activity_timestamp(cls, thread: dict[str, Any] | None) -> float | None:
        if not isinstance(thread, dict):
            return None
        for key in (
            "last_activity_timestamp",
            "last_message_timestamp",
            "latest_customer_message_at",
            "last_seen_at",
        ):
            stamp = cls._coerce_timestamp(thread.get(key))
            if stamp is not None:
                return stamp
        return None

    @classmethod
    def _thread_sort_key(cls, thread: dict[str, Any]) -> tuple[float, float, str, str]:
        activity_stamp = cls._thread_activity_timestamp(thread) or 0.0
        message_stamp = cls._coerce_timestamp(thread.get("last_message_timestamp")) or 0.0
        return (
            -activity_stamp,
            -message_stamp,
            str(thread.get("display_name") or "").strip().lower(),
            str(thread.get("thread_key") or "").strip(),
        )

    def _load_deleted_thread_row(self, thread_key: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT thread_key, account_id, deleted_at, last_activity_timestamp
            FROM inbox_deleted_threads
            WHERE thread_key = ?
            """,
            (str(thread_key or "").strip(),),
        ).fetchone()

    def _remember_deleted_thread_locked(self, thread: dict[str, Any]) -> None:
        clean_key = str(thread.get("thread_key") or "").strip()
        if not clean_key:
            return
        self._conn.execute(
            """
            INSERT INTO inbox_deleted_threads(thread_key, account_id, deleted_at, last_activity_timestamp)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(thread_key) DO UPDATE SET
                account_id = excluded.account_id,
                deleted_at = excluded.deleted_at,
                last_activity_timestamp = excluded.last_activity_timestamp
            """,
            (
                clean_key,
                self._clean_account_id(thread.get("account_id")),
                time.time(),
                self._thread_activity_timestamp(thread),
            ),
        )

    def _clear_deleted_thread_locked(self, thread_key: str) -> None:
        self._conn.execute(
            "DELETE FROM inbox_deleted_threads WHERE thread_key = ?",
            (str(thread_key or "").strip(),),
        )

    def _row_to_thread_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        thread_key = str(row["thread_key"] or "").strip()
        payload = {
            "thread_key": thread_key,
            "thread_id": str(row["thread_id"] or "").strip(),
            "thread_href": str(row["thread_href"] or "").strip(),
            "account_id": self._clean_account_id(row["account_id"]),
            "account_alias": str(row["account_alias"] or "").strip(),
            "recipient_username": str(row["recipient_username"] or "").strip(),
            "display_name": str(row["display_name"] or "").strip(),
            "last_message_text": str(row["last_message_text"] or "").strip(),
            "last_message_timestamp": self._coerce_timestamp(row["last_message_timestamp"]),
            "last_message_direction": str(row["last_message_direction"] or "").strip() or "unknown",
            "last_message_id": str(row["last_message_id"] or "").strip(),
            "unread_count": max(0, int(row["unread_count"] or 0)),
            "needs_reply": bool(row["needs_reply"]),
            "tags": self._normalize_tags(self._decode_json_list(row["tags_json"])),
            "participants": self._normalize_participants(self._decode_json_list(row["participants_json"])),
            "last_synced_at": self._coerce_timestamp(row["last_synced_at"]),
            "last_seen_text": str(row["last_seen_text"] or "").strip(),
            "last_seen_at": self._coerce_timestamp(row["last_seen_at"]),
            "latest_customer_message_at": self._coerce_timestamp(row["latest_customer_message_at"]),
        }
        payload.update(self._load_thread_state(thread_key))
        payload.update(self._account_overlay(payload["account_id"]))
        return payload

    def _load_thread_record(self, thread_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM inbox_threads WHERE thread_key = ?",
            (str(thread_key or "").strip(),),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_thread_payload(row)

    def _thread_shell(
        self,
        thread_key: str,
        *,
        current: dict[str, Any] | None = None,
        participants: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        current_payload = dict(current or {})
        clean_key = str(thread_key or "").strip()
        account_id, thread_id = self._split_thread_key(clean_key)
        current_participants = self._normalize_participants(current_payload.get("participants"))
        merged_participants = list(current_participants)
        for item in self._normalize_participants(participants):
            if item not in merged_participants:
                merged_participants.append(item)
        current_tags = self._normalize_tags(current_payload.get("tags"))
        merged_tags = list(current_tags)
        for item in self._normalize_tags(tags):
            if item not in merged_tags:
                merged_tags.append(item)
        recipient_username = (
            str(current_payload.get("recipient_username") or "").strip().lstrip("@")
            or (merged_participants[0] if merged_participants else "")
        )
        display_name = (
            str(current_payload.get("display_name") or "").strip()
            or recipient_username
            or thread_id
            or "Conversacion"
        )
        try:
            unread_count = max(0, int(current_payload.get("unread_count") or 0))
        except Exception:
            unread_count = 0
        return {
            "thread_key": clean_key,
            "thread_id": str(current_payload.get("thread_id") or thread_id).strip(),
            "thread_href": str(current_payload.get("thread_href") or "").strip()
            or (f"https://www.instagram.com/direct/t/{thread_id}/" if thread_id else ""),
            "account_id": self._clean_account_id(current_payload.get("account_id") or account_id),
            "account_alias": str(current_payload.get("account_alias") or "").strip(),
            "recipient_username": recipient_username,
            "display_name": display_name,
            "last_message_text": str(current_payload.get("last_message_text") or "").strip(),
            "last_message_timestamp": self._coerce_timestamp(current_payload.get("last_message_timestamp")),
            "last_message_direction": str(current_payload.get("last_message_direction") or "unknown").strip() or "unknown",
            "last_message_id": str(current_payload.get("last_message_id") or "").strip(),
            "unread_count": unread_count,
            "needs_reply": 1 if bool(current_payload.get("needs_reply")) else 0,
            "tags": merged_tags,
            "participants": merged_participants,
            "last_synced_at": self._coerce_timestamp(current_payload.get("last_synced_at")) or time.time(),
            "last_seen_text": str(current_payload.get("last_seen_text") or "").strip(),
            "last_seen_at": self._coerce_timestamp(current_payload.get("last_seen_at")),
            "latest_customer_message_at": self._coerce_timestamp(current_payload.get("latest_customer_message_at")),
        }

    def _account_session_started_at_locked(self, account_id: str) -> float | None:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return None
        row = self._conn.execute(
            "SELECT session_started_at FROM inbox_account_state WHERE account_id = ?",
            (clean_account,),
        ).fetchone()
        return self._coerce_timestamp(self._row_value(row, "session_started_at"))

    def _upsert_thread_record(self, thread: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO inbox_threads(
                thread_key, thread_id, thread_href, account_id, account_alias,
                recipient_username, display_name, last_message_text, last_message_timestamp,
                last_message_direction, last_message_id, unread_count, needs_reply,
                tags_json, participants_json, last_synced_at, last_seen_text, last_seen_at,
                latest_customer_message_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_key) DO UPDATE SET
                thread_id = excluded.thread_id,
                thread_href = excluded.thread_href,
                account_id = excluded.account_id,
                account_alias = excluded.account_alias,
                recipient_username = excluded.recipient_username,
                display_name = excluded.display_name,
                last_message_text = excluded.last_message_text,
                last_message_timestamp = excluded.last_message_timestamp,
                last_message_direction = excluded.last_message_direction,
                last_message_id = excluded.last_message_id,
                unread_count = excluded.unread_count,
                needs_reply = excluded.needs_reply,
                tags_json = excluded.tags_json,
                participants_json = excluded.participants_json,
                last_synced_at = excluded.last_synced_at,
                last_seen_text = excluded.last_seen_text,
                last_seen_at = excluded.last_seen_at,
                latest_customer_message_at = excluded.latest_customer_message_at
            """,
            (
                thread["thread_key"],
                thread["thread_id"],
                thread["thread_href"],
                thread["account_id"],
                thread["account_alias"],
                thread["recipient_username"],
                thread["display_name"],
                thread["last_message_text"],
                thread["last_message_timestamp"],
                thread["last_message_direction"],
                thread["last_message_id"],
                max(0, int(thread.get("unread_count") or 0)),
                1 if bool(thread.get("needs_reply")) else 0,
                self._encode_json(self._normalize_tags(thread.get("tags"))),
                self._encode_json(self._normalize_participants(thread.get("participants"))),
                self._coerce_timestamp(thread.get("last_synced_at")),
                str(thread.get("last_seen_text") or "").strip(),
                self._coerce_timestamp(thread.get("last_seen_at")),
                self._coerce_timestamp(thread.get("latest_customer_message_at")),
            ),
        )

    def _load_blocks(self, thread_key: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT block_id, ordinal, message_id, text, timestamp, direction, user_id,
                   delivery_status, local_echo, error_message
            FROM inbox_messages
            WHERE thread_key = ?
            ORDER BY COALESCE(timestamp, 0), ordinal
            """,
            (str(thread_key or "").strip(),),
        ).fetchall()
        return [
            {
                "block_id": str(row["block_id"] or "").strip(),
                "message_id": str(row["message_id"] or "").strip(),
                "text": str(row["text"] or "").strip(),
                "timestamp": self._coerce_timestamp(row["timestamp"]),
                "direction": str(row["direction"] or "").strip() or "unknown",
                "user_id": str(row["user_id"] or "").strip(),
                "delivery_status": self._normalize_delivery_status(row["delivery_status"]),
                "local_echo": bool(row["local_echo"]),
                "error_message": str(row["error_message"] or "").strip(),
            }
            for row in rows
        ]

    def _save_blocks(self, thread_key: str, blocks: list[dict[str, Any]]) -> None:
        self._conn.execute("DELETE FROM inbox_messages WHERE thread_key = ?", (thread_key,))
        for ordinal, block in enumerate(blocks):
            self._conn.execute(
                """
                INSERT INTO inbox_messages(
                    thread_key, block_id, ordinal, message_id, text, timestamp, direction,
                    user_id, delivery_status, local_echo, error_message
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    thread_key,
                    str(block.get("block_id") or block.get("message_id") or f"block:{ordinal}").strip(),
                    ordinal,
                    str(block.get("message_id") or "").strip(),
                    str(block.get("text") or "").strip(),
                    self._coerce_timestamp(block.get("timestamp")),
                    str(block.get("direction") or "unknown").strip(),
                    str(block.get("user_id") or "").strip(),
                    self._normalize_delivery_status(block.get("delivery_status")),
                    1 if bool(block.get("local_echo")) else 0,
                    str(block.get("error_message") or "").strip(),
                ),
            )

    def _compress_blocks(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [self._normalize_message_record(item) for item in messages]
        clean_rows = [item for item in normalized if isinstance(item, dict)]
        clean_rows.sort(
            key=lambda item: (
                self._coerce_timestamp(item.get("timestamp")) or 0.0,
                str(item.get("message_id") or ""),
            )
        )
        if not clean_rows:
            return []

        blocks: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        current_start_ts: float | None = None
        for message in clean_rows:
            stamp = self._coerce_timestamp(message.get("timestamp"))
            direction = str(message.get("direction") or "unknown").strip().lower() or "unknown"
            should_group = False
            if current is not None:
                current_direction = str(current.get("direction") or "").strip().lower()
                current_ts = self._coerce_timestamp(current.get("timestamp"))
                should_group = (
                    current_direction == direction
                    and current_ts is not None
                    and stamp is not None
                    and current_start_ts is not None
                    and (stamp - current_start_ts) <= self._RESPONSE_BLOCK_WINDOW_SECONDS
                    and str(current.get("delivery_status") or "").strip().lower() not in {"pending", "sending", "error"}
                    and str(message.get("delivery_status") or "").strip().lower() not in {"pending", "sending", "error"}
                )
            if not should_group:
                if current is not None:
                    blocks.append(current)
                current_start_ts = stamp
                current = {
                    "block_id": str(message.get("message_id") or f"block:{len(blocks)}").strip(),
                    "message_id": str(message.get("message_id") or "").strip(),
                    "text": str(message.get("text") or "").strip(),
                    "timestamp": stamp,
                    "direction": direction,
                    "user_id": str(message.get("user_id") or "").strip(),
                    "delivery_status": self._normalize_delivery_status(message.get("delivery_status")),
                    "local_echo": bool(message.get("local_echo")),
                    "error_message": str(message.get("error_message") or "").strip(),
                }
                continue
            if current is None:
                continue
            combined = [str(current.get("text") or "").strip(), str(message.get("text") or "").strip()]
            current["text"] = "\n".join([item for item in combined if item])
            current["timestamp"] = stamp if stamp is not None else current.get("timestamp")
            current["message_id"] = str(message.get("message_id") or current.get("message_id") or "").strip()
            current["user_id"] = str(message.get("user_id") or current.get("user_id") or "").strip()
            current["local_echo"] = bool(current.get("local_echo")) or bool(message.get("local_echo"))
            status = self._normalize_delivery_status(message.get("delivery_status"))
            current_status = self._normalize_delivery_status(current.get("delivery_status"))
            if "error" in {status, current_status}:
                current["delivery_status"] = "error"
            elif "sending" in {status, current_status}:
                current["delivery_status"] = "sending"
            elif "pending" in {status, current_status}:
                current["delivery_status"] = "pending"
            else:
                current["delivery_status"] = "sent"
            if str(message.get("error_message") or "").strip():
                current["error_message"] = str(message.get("error_message") or "").strip()
        if current is not None:
            blocks.append(current)
        return blocks[-self._MAX_MESSAGES_PER_THREAD :]

    def _merge_remote_and_local_blocks(self, thread_key: str, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        remote_ids = {str(item.get("message_id") or "").strip() for item in blocks if str(item.get("message_id") or "").strip()}
        pending_blocks = [
            item
            for item in self._load_blocks(thread_key)
            if self._normalize_delivery_status(item.get("delivery_status")) in {"pending", "sending", "error"}
        ]
        merged = list(blocks)
        for block in pending_blocks:
            block_id = str(block.get("message_id") or "").strip()
            if block_id and block_id in remote_ids:
                continue
            merged.append(dict(block))
        merged.sort(
            key=lambda item: (
                self._coerce_timestamp(item.get("timestamp")) or 0.0,
                str(item.get("message_id") or ""),
            )
        )
        deduped: dict[str, dict[str, Any]] = {}
        for block in merged:
            key = str(block.get("message_id") or block.get("block_id") or "").strip()
            if not key:
                key = f"anon:{len(deduped)}"
            deduped[key] = dict(block)
        return list(deduped.values())[-self._MAX_MESSAGES_PER_THREAD :]

    def _merge_cached_blocks(
        self,
        existing_blocks: list[dict[str, Any]],
        incoming_blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for source in (existing_blocks, incoming_blocks):
            for block in source:
                key = self._block_identity(block)
                if not key:
                    continue
                if key not in merged:
                    order.append(key)
                merged[key] = dict(block)
        rows = [merged[key] for key in order]
        rows.sort(
            key=lambda item: (
                self._coerce_timestamp(item.get("timestamp")) or 0.0,
                str(item.get("message_id") or item.get("block_id") or ""),
            )
        )
        return rows[-self._MAX_MESSAGES_PER_THREAD :]

    @staticmethod
    def _block_identity(block: dict[str, Any]) -> str:
        message_id = str(block.get("message_id") or block.get("block_id") or "").strip()
        if message_id:
            return f"id:{message_id}"
        return "|".join(
            (
                str(block.get("direction") or "").strip().lower(),
                str(block.get("timestamp") or ""),
                str(block.get("text") or "").strip(),
            )
        )

    def _derive_thread_metrics(
        self,
        blocks: list[dict[str, Any]],
        *,
        current_unread_count: int,
        mark_read: bool,
    ) -> dict[str, Any]:
        latest_message = blocks[-1] if blocks else None
        latest_inbound_at: float | None = None
        latest_outbound_sent_at: float | None = None
        for block in blocks:
            stamp = self._coerce_timestamp(block.get("timestamp"))
            direction = str(block.get("direction") or "").strip().lower()
            status = self._normalize_delivery_status(block.get("delivery_status"))
            if direction == "inbound" and stamp is not None:
                latest_inbound_at = stamp if latest_inbound_at is None else max(latest_inbound_at, stamp)
            if direction == "outbound" and stamp is not None and status == "sent":
                latest_outbound_sent_at = stamp if latest_outbound_sent_at is None else max(latest_outbound_sent_at, stamp)
        needs_reply = bool(
            latest_inbound_at is not None
            and (latest_outbound_sent_at is None or latest_outbound_sent_at < latest_inbound_at)
        )
        unread_count = 0 if mark_read else current_unread_count
        if not mark_read and latest_message is not None and str(latest_message.get("direction") or "").strip().lower() == "inbound":
            unread_count = max(1, int(current_unread_count or 0))
        return {
            "latest_message": latest_message,
            "latest_customer_message_at": latest_inbound_at,
            "needs_reply": needs_reply,
            "unread_count": unread_count,
        }

    def _drop_thread_locked(self, thread_key: str) -> None:
        self._conn.execute("DELETE FROM inbox_messages WHERE thread_key = ?", (thread_key,))
        self._conn.execute("DELETE FROM inbox_thread_state WHERE thread_key = ?", (thread_key,))
        self._conn.execute("DELETE FROM inbox_threads WHERE thread_key = ?", (thread_key,))

    def _thread_has_pending_work_locked(self, thread_key: str, state: dict[str, Any] | None = None) -> bool:
        payload = dict(state or {})
        sender_status = str(payload.get("sender_status") or "").strip().lower()
        pack_status = str(payload.get("pack_status") or "").strip().lower()
        if sender_status in {"queued", "preparing", "sending"}:
            return True
        if pack_status in {"queued", "running"}:
            return True
        for block in self._load_blocks(thread_key):
            status = self._normalize_delivery_status(block.get("delivery_status"))
            if status in {"pending", "sending"}:
                return True
        return False

    def _trim_global_threads_locked(self) -> None:
        rows = [
            self._row_to_thread_payload(row)
            for row in self._conn.execute("SELECT * FROM inbox_threads").fetchall()
        ]
        rows.sort(key=self._thread_sort_key)
        if len(rows) <= self._MAX_ACTIVE_THREADS:
            return
        for row in rows[self._MAX_ACTIVE_THREADS :]:
            self._drop_thread_locked(str(row.get("thread_key") or "").strip())

    @staticmethod
    def _include_thread(thread: dict[str, Any], filter_mode: str) -> bool:
        mode = str(filter_mode or "all").strip().lower()
        if mode == "unread":
            try:
                return int(thread.get("unread_count") or 0) > 0
            except Exception:
                return False
        if mode == "pending":
            if "needs_reply" in thread:
                return bool(thread.get("needs_reply"))
            return str(thread.get("last_message_direction") or "").strip().lower() == "inbound"
        return True

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            threads = {
                row["thread_key"]: self.get_thread(row["thread_key"])
                for row in self._conn.execute("SELECT thread_key FROM inbox_threads").fetchall()
            }
            state = {
                "accounts": {
                    row["account_id"]: {
                        "session_marker": str(row["session_marker"] or "").strip(),
                        "session_started_at": self._coerce_timestamp(row["session_started_at"]),
                        "last_sync_at": self._coerce_timestamp(row["last_sync_at"]),
                        "last_error": str(row["last_error"] or "").strip(),
                        "thread_count": max(0, int(row["thread_count"] or 0)),
                        "health_state": self._normalize_health_state(row["health_state"]),
                        "health_reason": str(row["health_reason"] or "").strip(),
                        "health_updated_at": self._coerce_timestamp(row["health_updated_at"]),
                    }
                    for row in self._conn.execute("SELECT * FROM inbox_account_state").fetchall()
                },
                "threads": {
                    row["thread_key"]: self._load_thread_state(row["thread_key"])
                    for row in self._conn.execute("SELECT thread_key FROM inbox_threads").fetchall()
                },
            }
        return {
            "threads": copy.deepcopy(threads),
            "messages": {
                thread_key: thread.get("messages", [])
                for thread_key, thread in threads.items()
                if isinstance(thread, dict)
            },
            "state": state,
        }

    def stats(self) -> dict[str, int]:
        with self._lock:
            thread_total = self._conn.execute("SELECT COUNT(*) AS total FROM inbox_threads").fetchone()
            message_total = self._conn.execute("SELECT COUNT(DISTINCT thread_key) AS total FROM inbox_messages").fetchone()
        return {
            "thread_count": int(self._row_value(thread_total, "total", 0) or 0),
            "message_groups": int(self._row_value(message_total, "total", 0) or 0),
        }

    def get_threads(self, filter_mode: str = "all") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM inbox_threads").fetchall()
            payload = [self._row_to_thread_payload(row) for row in rows]
            payload.sort(key=self._thread_sort_key)
        return [dict(row) for row in payload if self._include_thread(row, filter_mode)]

    def get_thread(self, thread_key: str) -> dict[str, Any] | None:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return None
        with self._lock:
            thread = self._load_thread_record(clean_key)
            if not isinstance(thread, dict):
                return None
            thread["messages"] = self._load_blocks(clean_key)
            return copy.deepcopy(thread)

    def get_messages(self, thread_key: str) -> list[dict[str, Any]]:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return []
        with self._lock:
            return copy.deepcopy(self._load_blocks(clean_key))

    def upsert_threads(self, thread_rows: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._lock:
            for raw in thread_rows:
                thread = self._normalize_thread_record(raw)
                if not thread:
                    continue
                current = self._load_thread_record(thread["thread_key"]) or {}
                if not thread.get("last_message_text"):
                    thread["last_message_text"] = str(current.get("last_message_text") or "").strip()
                if not thread.get("last_message_timestamp"):
                    thread["last_message_timestamp"] = self._coerce_timestamp(current.get("last_message_timestamp"))
                if not thread.get("last_message_id"):
                    thread["last_message_id"] = str(current.get("last_message_id") or "").strip()
                if not thread.get("latest_customer_message_at"):
                    thread["latest_customer_message_at"] = self._coerce_timestamp(current.get("latest_customer_message_at"))
                if not thread.get("last_seen_text"):
                    thread["last_seen_text"] = str(current.get("last_seen_text") or "").strip()
                    thread["last_seen_at"] = self._coerce_timestamp(current.get("last_seen_at"))
                thread["participants"] = self._normalize_participants(
                    list(current.get("participants") or []) + list(thread.get("participants") or [])
                )
                thread["tags"] = self._normalize_tags(
                    list(current.get("tags") or []) + list(thread.get("tags") or [])
                )
                thread["last_synced_at"] = now
                self._upsert_thread_record(thread)
            self._trim_global_threads_locked()
            self._conn.commit()

    def replace_messages(
        self,
        thread_key: str,
        messages: list[dict[str, Any]],
        *,
        seen_text: str = "",
        seen_at: float | None = None,
        participants: list[str] | None = None,
        mark_read: bool = False,
    ) -> None:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return
        with self._lock:
            current = self._load_thread_record(clean_key) or self._thread_shell(clean_key, participants=participants)
            compressed = self._compress_blocks(messages)
            blocks = self._merge_remote_and_local_blocks(clean_key, compressed)
            metrics = self._derive_thread_metrics(
                blocks,
                current_unread_count=max(0, int(current.get("unread_count") or 0)),
                mark_read=mark_read,
            )
            latest = metrics["latest_message"]
            thread = self._thread_shell(clean_key, current=current, participants=participants)
            if latest is not None:
                thread["last_message_text"] = str(latest.get("text") or "").strip()
                thread["last_message_timestamp"] = self._coerce_timestamp(latest.get("timestamp"))
                thread["last_message_direction"] = str(latest.get("direction") or "unknown").strip()
                thread["last_message_id"] = str(latest.get("message_id") or "").strip()
            thread["latest_customer_message_at"] = metrics["latest_customer_message_at"]
            thread["needs_reply"] = 1 if metrics["needs_reply"] else 0
            thread["unread_count"] = int(metrics["unread_count"] or 0)
            thread["last_synced_at"] = time.time()
            if seen_text:
                thread["last_seen_text"] = str(seen_text or "").strip()
                thread["last_seen_at"] = self._coerce_timestamp(seen_at) or time.time()
            self._upsert_thread_record(thread)
            if mark_read:
                state = self._load_thread_state(clean_key)
                state["last_opened_at"] = time.time()
                self._save_thread_state(clean_key, state)
            self._save_blocks(clean_key, blocks)
            self._conn.commit()

    def seed_messages(
        self,
        thread_key: str,
        messages: list[dict[str, Any]],
        *,
        participants: list[str] | None = None,
    ) -> None:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return
        with self._lock:
            preview_blocks = self._compress_blocks(messages)
            if not preview_blocks:
                return
            current = self._load_thread_record(clean_key) or self._thread_shell(clean_key, participants=participants)
            existing_blocks = self._load_blocks(clean_key)
            blocks = self._merge_cached_blocks(existing_blocks, preview_blocks)
            metrics = self._derive_thread_metrics(
                blocks,
                current_unread_count=max(0, int(current.get("unread_count") or 0)),
                mark_read=False,
            )
            latest = metrics["latest_message"]
            thread = self._thread_shell(clean_key, current=current, participants=participants)
            if latest is not None:
                thread["last_message_text"] = str(latest.get("text") or "").strip()
                thread["last_message_timestamp"] = self._coerce_timestamp(latest.get("timestamp"))
                thread["last_message_direction"] = str(latest.get("direction") or "unknown").strip()
                thread["last_message_id"] = str(latest.get("message_id") or "").strip()
            thread["latest_customer_message_at"] = metrics["latest_customer_message_at"]
            thread["needs_reply"] = 1 if metrics["needs_reply"] else 0
            thread["unread_count"] = int(metrics["unread_count"] or 0)
            thread["last_synced_at"] = time.time()
            self._upsert_thread_record(thread)
            self._save_blocks(clean_key, blocks)
            self._conn.commit()

    def append_local_outbound_message(self, thread_key: str, text: str) -> dict[str, Any] | None:
        clean_key = str(thread_key or "").strip()
        content = str(text or "").strip()
        if not clean_key or not content:
            return None
        local_id = f"local-{int(time.time() * 1000)}"
        block = {
            "block_id": local_id,
            "message_id": local_id,
            "text": content,
            "timestamp": time.time(),
            "direction": "outbound",
            "user_id": "",
            "delivery_status": "pending",
            "local_echo": True,
            "error_message": "",
        }
        with self._lock:
            current = self._load_thread_record(clean_key) or self._thread_shell(clean_key)
            blocks = self._load_blocks(clean_key)
            blocks.append(block)
            blocks.sort(
                key=lambda item: (
                    self._coerce_timestamp(item.get("timestamp")) or 0.0,
                    str(item.get("message_id") or ""),
                )
            )
            blocks = blocks[-self._MAX_MESSAGES_PER_THREAD :]
            metrics = self._derive_thread_metrics(
                blocks,
                current_unread_count=max(0, int(current.get("unread_count") or 0)),
                mark_read=True,
            )
            thread = self._thread_shell(clean_key, current=current)
            thread["last_message_text"] = content
            thread["last_message_timestamp"] = block["timestamp"]
            thread["last_message_direction"] = "outbound"
            thread["last_message_id"] = local_id
            thread["unread_count"] = 0
            thread["needs_reply"] = 1 if metrics["needs_reply"] else 0
            thread["last_synced_at"] = time.time()
            self._upsert_thread_record(thread)
            self._save_blocks(clean_key, blocks)
            self._conn.commit()
        return dict(block)

    def set_local_outbound_status(
        self,
        thread_key: str,
        local_message_id: str,
        *,
        status: str,
        error_message: str = "",
    ) -> None:
        clean_key = str(thread_key or "").strip()
        local_id = str(local_message_id or "").strip()
        next_status = self._normalize_delivery_status(status)
        if not clean_key or not local_id:
            return
        with self._lock:
            blocks = self._load_blocks(clean_key)
            changed = False
            for block in blocks:
                if str(block.get("message_id") or "").strip() != local_id:
                    continue
                block["delivery_status"] = next_status
                block["error_message"] = str(error_message or "").strip()
                changed = True
                break
            if not changed:
                return
            self._save_blocks(clean_key, blocks)
            self._conn.commit()

    def resolve_local_outbound(
        self,
        thread_key: str,
        local_message_id: str,
        *,
        final_message_id: str = "",
        sent_timestamp: float | None = None,
        error_message: str = "",
    ) -> None:
        clean_key = str(thread_key or "").strip()
        local_id = str(local_message_id or "").strip()
        if not clean_key or not local_id:
            return
        with self._lock:
            current = self._load_thread_record(clean_key) or self._thread_shell(clean_key)
            blocks = self._load_blocks(clean_key)
            changed = False
            for block in blocks:
                if str(block.get("message_id") or "").strip() != local_id:
                    continue
                if error_message:
                    block["delivery_status"] = "error"
                    block["error_message"] = str(error_message).strip()
                else:
                    block["delivery_status"] = "sent"
                    block["error_message"] = ""
                    block["local_echo"] = False
                    if final_message_id:
                        block["message_id"] = str(final_message_id).strip()
                        block["block_id"] = str(final_message_id).strip()
                    if sent_timestamp:
                        block["timestamp"] = self._coerce_timestamp(sent_timestamp)
                changed = True
                break
            if not changed:
                return
            metrics = self._derive_thread_metrics(
                blocks,
                current_unread_count=max(0, int(current.get("unread_count") or 0)),
                mark_read=False,
            )
            latest = metrics["latest_message"]
            thread = self._thread_shell(clean_key, current=current)
            if latest is not None:
                thread["last_message_text"] = str(latest.get("text") or "").strip()
                thread["last_message_timestamp"] = self._coerce_timestamp(latest.get("timestamp"))
                thread["last_message_direction"] = str(latest.get("direction") or "unknown").strip()
                thread["last_message_id"] = str(latest.get("message_id") or "").strip()
            thread["needs_reply"] = 1 if metrics["needs_reply"] else 0
            thread["latest_customer_message_at"] = metrics["latest_customer_message_at"]
            thread["last_synced_at"] = time.time()
            self._upsert_thread_record(thread)
            self._save_blocks(clean_key, blocks[-self._MAX_MESSAGES_PER_THREAD :])
            self._conn.commit()

    def update_thread_state(self, thread_key: str, updates: dict[str, Any]) -> None:
        clean_key = str(thread_key or "").strip()
        if not clean_key or not isinstance(updates, dict):
            return
        with self._lock:
            if self._load_thread_record(clean_key) is None:
                return
            state = self._load_thread_state(clean_key)
            state.update(copy.deepcopy(updates))
            self._save_thread_state(clean_key, state)
            self._conn.commit()

    def mark_thread_opened(self, thread_key: str) -> None:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return
        with self._lock:
            thread = self._load_thread_record(clean_key)
            if not isinstance(thread, dict):
                return
            thread["unread_count"] = 0
            self._upsert_thread_record(thread)
            state = self._load_thread_state(clean_key)
            state["last_opened_at"] = time.time()
            self._save_thread_state(clean_key, state)
            self._conn.commit()

    def register_account_sync(
        self,
        account_id: str,
        *,
        last_error: str = "",
        thread_count: int | None = None,
    ) -> None:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT session_marker, session_started_at, health_state, health_reason, health_updated_at
                FROM inbox_account_state WHERE account_id = ?
                """,
                (clean_account,),
            ).fetchone()
            session_marker = str(self._row_value(existing, "session_marker", "") or "").strip()
            session_started_at = self._coerce_timestamp(self._row_value(existing, "session_started_at"))
            health_state = self._normalize_health_state(self._row_value(existing, "health_state", "healthy"))
            health_reason = str(self._row_value(existing, "health_reason", "") or "").strip()
            health_updated_at = self._coerce_timestamp(self._row_value(existing, "health_updated_at"))
            next_thread_count = max(0, int(thread_count or 0)) if thread_count is not None else 0
            self._conn.execute(
                """
                INSERT INTO inbox_account_state(
                    account_id, session_marker, session_started_at, last_sync_at, last_error, thread_count,
                    health_state, health_reason, health_updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_marker = excluded.session_marker,
                    session_started_at = excluded.session_started_at,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error,
                    thread_count = excluded.thread_count,
                    health_state = excluded.health_state,
                    health_reason = excluded.health_reason,
                    health_updated_at = excluded.health_updated_at
                """,
                (
                    clean_account,
                    session_marker,
                    session_started_at,
                    time.time(),
                    str(last_error or "").strip(),
                    next_thread_count,
                    health_state,
                    health_reason,
                    health_updated_at,
                ),
            )
            self._conn.commit()

    def prepare_account_session(
        self,
        account_id: str,
        *,
        session_marker: str,
        started_at: float | None = None,
    ) -> float | None:
        clean_account = self._clean_account_id(account_id)
        clean_marker = str(session_marker or "").strip()
        if not clean_account or not clean_marker:
            return None
        requested_started_at = self._coerce_timestamp(started_at)
        session_started_at = requested_started_at or time.time()
        with self._lock:
            current = self._conn.execute(
                """
                SELECT session_marker, session_started_at, health_state, health_reason, health_updated_at
                FROM inbox_account_state WHERE account_id = ?
                """,
                (clean_account,),
            ).fetchone()
            current_marker = str(self._row_value(current, "session_marker", "") or "").strip()
            current_started_at = self._coerce_timestamp(self._row_value(current, "session_started_at"))
            if current_marker == clean_marker and current_started_at is not None:
                if requested_started_at is not None and requested_started_at + 0.000001 < current_started_at:
                    self._conn.execute(
                        "UPDATE inbox_account_state SET session_started_at = ? WHERE account_id = ?",
                        (requested_started_at, clean_account),
                    )
                    self._conn.commit()
                    return requested_started_at
                return current_started_at
            thread_rows = self._conn.execute(
                "SELECT thread_key FROM inbox_threads WHERE account_id = ?",
                (clean_account,),
            ).fetchall()
            for row in thread_rows:
                self._drop_thread_locked(str(row["thread_key"] or "").strip())
            self._conn.execute("DELETE FROM inbox_deleted_threads WHERE account_id = ?", (clean_account,))
            self._conn.execute("DELETE FROM inbox_send_queue_jobs WHERE account_id = ?", (clean_account,))
            self._conn.execute("DELETE FROM thread_action_memory WHERE account_id = ?", (clean_account,))
            health_state = self._normalize_health_state(self._row_value(current, "health_state", "healthy"))
            health_reason = str(self._row_value(current, "health_reason", "") or "").strip()
            health_updated_at = self._coerce_timestamp(self._row_value(current, "health_updated_at"))
            self._conn.execute(
                """
                INSERT INTO inbox_account_state(
                    account_id, session_marker, session_started_at, last_sync_at, last_error, thread_count,
                    health_state, health_reason, health_updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_marker = excluded.session_marker,
                    session_started_at = excluded.session_started_at,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error,
                    thread_count = excluded.thread_count,
                    health_state = excluded.health_state,
                    health_reason = excluded.health_reason,
                    health_updated_at = excluded.health_updated_at
                """,
                (
                    clean_account,
                    clean_marker,
                    session_started_at,
                    None,
                    "",
                    0,
                    health_state,
                    health_reason,
                    health_updated_at,
                ),
            )
            self._conn.commit()
        return session_started_at

    def account_session_started_at(self, account_id: str) -> float | None:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return None
        with self._lock:
            return self._account_session_started_at_locked(clean_account)

    def prune_accounts(self, account_ids: set[str]) -> None:
        allowed = {self._clean_account_id(item) for item in account_ids if self._clean_account_id(item)}
        with self._lock:
            account_rows = self._conn.execute("SELECT account_id FROM inbox_account_state").fetchall()
            for row in account_rows:
                account_id = self._clean_account_id(row["account_id"])
                if account_id in allowed:
                    continue
                self._conn.execute("DELETE FROM inbox_account_state WHERE account_id = ?", (account_id,))
                self._conn.execute("DELETE FROM inbox_send_queue_jobs WHERE account_id = ?", (account_id,))
                self._conn.execute("DELETE FROM thread_action_memory WHERE account_id = ?", (account_id,))
            deleted_rows = self._conn.execute("SELECT thread_key, account_id FROM inbox_deleted_threads").fetchall()
            for row in deleted_rows:
                account_id = self._clean_account_id(row["account_id"])
                if account_id in allowed:
                    continue
                self._clear_deleted_thread_locked(str(row["thread_key"] or "").strip())
            thread_rows = self._conn.execute("SELECT thread_key, account_id FROM inbox_threads").fetchall()
            for row in thread_rows:
                account_id = self._clean_account_id(row["account_id"])
                if account_id in allowed:
                    continue
                self._drop_thread_locked(str(row["thread_key"] or "").strip())
            self._conn.commit()

    def prune_account_threads(self, account_id: str, *, keep_thread_keys: set[str]) -> list[str]:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return []
        keep = {str(item or "").strip() for item in keep_thread_keys if str(item or "").strip()}
        removed: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_key FROM inbox_threads WHERE account_id = ?",
                (clean_account,),
            ).fetchall()
            for row in rows:
                thread_key = str(row["thread_key"] or "").strip()
                if not thread_key or thread_key in keep:
                    continue
                state = self._load_thread_state(thread_key)
                if self._thread_has_pending_work_locked(thread_key, state):
                    continue
                self._drop_thread_locked(thread_key)
                removed.append(thread_key)
            if removed:
                self._conn.commit()
        return removed

    def set_account_health(
        self,
        account_id: str,
        state: str,
        *,
        reason: str = "",
        updated_at: float | None = None,
    ) -> None:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return
        normalized_state = self._normalize_health_state(state)
        with self._lock:
            existing = self._conn.execute(
                """
                SELECT session_marker, session_started_at, last_sync_at, last_error, thread_count
                FROM inbox_account_state WHERE account_id = ?
                """,
                (clean_account,),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO inbox_account_state(
                    account_id, session_marker, session_started_at, last_sync_at, last_error, thread_count,
                    health_state, health_reason, health_updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    session_marker = excluded.session_marker,
                    session_started_at = excluded.session_started_at,
                    last_sync_at = excluded.last_sync_at,
                    last_error = excluded.last_error,
                    thread_count = excluded.thread_count,
                    health_state = excluded.health_state,
                    health_reason = excluded.health_reason,
                    health_updated_at = excluded.health_updated_at
                """,
                (
                    clean_account,
                    str(self._row_value(existing, "session_marker", "") or "").strip(),
                    self._coerce_timestamp(self._row_value(existing, "session_started_at")),
                    self._coerce_timestamp(self._row_value(existing, "last_sync_at")),
                    str(self._row_value(existing, "last_error", "") or "").strip(),
                    max(0, int(self._row_value(existing, "thread_count", 0) or 0)),
                    normalized_state,
                    str(reason or "").strip(),
                    self._coerce_timestamp(updated_at) or time.time(),
                ),
            )
            self._conn.commit()

    def get_account_health(self, account_id: str) -> dict[str, Any]:
        clean_account = self._clean_account_id(account_id)
        if not clean_account:
            return {"state": "unknown", "reason": ""}
        with self._lock:
            row = self._conn.execute(
                "SELECT health_state, health_reason, health_updated_at FROM inbox_account_state WHERE account_id = ?",
                (clean_account,),
            ).fetchone()
        if row is None:
            return {"state": "healthy", "reason": ""}
        return {
            "state": self._normalize_health_state(row["health_state"]),
            "reason": str(row["health_reason"] or "").strip(),
            "updated_at": self._coerce_timestamp(row["health_updated_at"]),
        }

    def thread_has_action_since(
        self,
        account_id: str,
        thread_id: str,
        *,
        action_types: list[str] | tuple[str, ...],
        started_at: float | None = None,
    ) -> bool:
        clean_account = self._clean_account_id(account_id)
        clean_thread = str(thread_id or "").strip()
        clean_actions = [str(item or "").strip() for item in action_types if str(item or "").strip()]
        if not clean_account or not clean_thread or not clean_actions:
            return False
        placeholders = ",".join("?" for _ in clean_actions)
        query = (
            "SELECT 1 FROM thread_action_memory "
            f"WHERE account_id = ? AND thread_id = ? AND action_type IN ({placeholders})"
        )
        params: list[Any] = [clean_account, clean_thread, *clean_actions]
        started_value = self._coerce_timestamp(started_at)
        if started_value is not None:
            query += " AND created_at >= ?"
            params.append(started_value)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, tuple(params)).fetchone()
        return row is not None

    def record_action_memory(
        self,
        thread_id: str,
        account_id: str,
        action_type: str,
        *,
        pack_id: str = "",
        source: str = "inbox_rm",
        created_at: float | None = None,
    ) -> None:
        clean_thread = str(thread_id or "").strip()
        clean_account = self._clean_account_id(account_id)
        clean_action = str(action_type or "").strip()
        if not clean_thread or not clean_account or not clean_action:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO thread_action_memory(
                    thread_id, account_id, action_type, pack_id, source, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_thread,
                    clean_account,
                    clean_action,
                    str(pack_id or "").strip(),
                    str(source or "").strip(),
                    self._coerce_timestamp(created_at) or time.time(),
                ),
            )
            self._conn.commit()

    def create_send_queue_job(
        self,
        task_type: str,
        *,
        thread_key: str,
        account_id: str,
        payload: dict[str, Any],
        dedupe_key: str = "",
    ) -> int:
        clean_type = str(task_type or "").strip()
        if not clean_type:
            return 0
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO inbox_send_queue_jobs(
                    task_type, dedupe_key, thread_key, account_id, payload_json, state, error_message, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, ?, 'pending', '', ?, ?)
                """,
                (
                    clean_type,
                    str(dedupe_key or "").strip(),
                    str(thread_key or "").strip(),
                    self._clean_account_id(account_id),
                    self._encode_json(dict(payload or {})),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def update_send_queue_job(self, job_id: int, *, state: str, error_message: str = "") -> None:
        if int(job_id or 0) <= 0:
            return
        with self._lock:
            self._conn.execute(
                """
                UPDATE inbox_send_queue_jobs
                SET state = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    self._normalize_delivery_status(state),
                    str(error_message or "").strip(),
                    time.time(),
                    int(job_id),
                ),
            )
            self._conn.commit()

    def list_send_queue_jobs(
        self,
        *,
        states: list[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, int(limit or 100))
        clean_states = [
            self._normalize_delivery_status(item)
            for item in (states or [])
            if self._normalize_delivery_status(item)
        ]
        query = """
            SELECT id, task_type, dedupe_key, thread_key, account_id, payload_json, state, error_message, created_at, updated_at
            FROM inbox_send_queue_jobs
        """
        params: list[Any] = []
        if clean_states:
            placeholders = ",".join("?" for _ in clean_states)
            query += f" WHERE state IN ({placeholders})"
            params.extend(clean_states)
        query += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(safe_limit)
        with self._lock:
            rows = self._conn.execute(query, tuple(params)).fetchall()
        payload: list[dict[str, Any]] = []
        for row in rows:
            raw_payload = str(row["payload_json"] or "").strip()
            try:
                decoded_payload = json.loads(raw_payload) if raw_payload else {}
            except Exception:
                decoded_payload = {}
            payload.append(
                {
                    "id": int(row["id"] or 0),
                    "task_type": str(row["task_type"] or "").strip(),
                    "dedupe_key": str(row["dedupe_key"] or "").strip(),
                    "thread_key": str(row["thread_key"] or "").strip(),
                    "account_id": self._clean_account_id(row["account_id"]),
                    "payload": decoded_payload if isinstance(decoded_payload, dict) else {},
                    "state": self._normalize_delivery_status(row["state"]),
                    "error_message": str(row["error_message"] or "").strip(),
                    "created_at": self._coerce_timestamp(row["created_at"]),
                    "updated_at": self._coerce_timestamp(row["updated_at"]),
                }
            )
        return payload

    def append_thread_tag(self, thread_key: str, tag: str) -> list[str]:
        clean_key = str(thread_key or "").strip()
        clean_tag = str(tag or "").strip()
        if not clean_key or not clean_tag:
            return []
        with self._lock:
            thread = self._load_thread_record(clean_key) or self._thread_shell(clean_key)
            tags = self._normalize_tags(list(thread.get("tags") or []) + [clean_tag])
            thread["tags"] = tags
            self._upsert_thread_record(thread)
            self._conn.commit()
        return tags

    def mark_follow_up(self, thread_key: str) -> bool:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return False
        with self._lock:
            thread = self._load_thread_record(clean_key)
            if not isinstance(thread, dict):
                return False
            tags = self._normalize_tags(list(thread.get("tags") or []) + [self._LOCAL_TAG_FOLLOW_UP])
            thread["tags"] = tags
            self._upsert_thread_record(thread)
            state = self._load_thread_state(clean_key)
            state["follow_up_marked_at"] = time.time()
            self._save_thread_state(clean_key, state)
            self.record_action_memory(
                str(thread.get("thread_id") or "").strip(),
                str(thread.get("account_id") or "").strip(),
                "follow_up_tag_added",
                source="inbox_rm",
            )
            self._conn.commit()
            return True

    def delete_thread(self, thread_key: str) -> bool:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return False
        with self._lock:
            thread = self._load_thread_record(clean_key)
            if not isinstance(thread, dict):
                return False
            self._remember_deleted_thread_locked(thread)
            self._drop_thread_locked(clean_key)
            self._conn.commit()
            return True

    def clear_deleted_thread(self, thread_key: str) -> None:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return
        with self._lock:
            self._clear_deleted_thread_locked(clean_key)
            self._conn.commit()

    def allow_deleted_thread_recreate(
        self,
        thread_key: str,
        *,
        last_activity_timestamp: float | None = None,
    ) -> bool:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return False
        incoming_activity = self._coerce_timestamp(last_activity_timestamp)
        with self._lock:
            row = self._load_deleted_thread_row(clean_key)
            if row is None:
                return True
            deleted_activity = self._coerce_timestamp(self._row_value(row, "last_activity_timestamp"))
            if incoming_activity is not None and (
                deleted_activity is None or incoming_activity > (deleted_activity + 0.000001)
            ):
                self._clear_deleted_thread_locked(clean_key)
                self._conn.commit()
                return True
        return False

    def flush(self) -> None:
        with self._lock:
            self._conn.commit()

    def shutdown(self) -> None:
        with self._lock:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
