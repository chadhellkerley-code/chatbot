from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core import responder as responder_module
from src.inbox.message_sender import build_conversation_text

from .browser_pool import BrowserPool
from .conversation_store import ConversationStore


logger = logging.getLogger(__name__)


@dataclass(order=True)
class _SenderTask:
    priority: int
    sequence: int
    task_type: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)


class ConversationSender:
    def __init__(
        self,
        store: ConversationStore,
        browser_pool: BrowserPool,
        *,
        notifier,
    ) -> None:
        self._store = store
        self._browser_pool = browser_pool
        self._notifier = notifier
        self._stop_event = threading.Event()
        self._queue: queue.PriorityQueue[_SenderTask] = queue.PriorityQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._sequence = 0
        self._prepare_generation = 0
        self._active_task = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="inbox-conversation-sender",
            daemon=True,
        )
        self._thread.start()
        self._recover_jobs()

    def stop(self) -> None:
        self._stop_event.set()
        self._enqueue("stop", {}, priority=99)
        worker = self._thread
        self._thread = None
        if worker is not None:
            worker.join(timeout=3.0)
        self._browser_pool.shutdown()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "sender_queue_size": int(self._queue.qsize()),
            "sender_active_task": self._active_task,
        }

    def prepare_thread(self, thread_key: str) -> bool:
        clean_key = str(thread_key or "").strip()
        if not clean_key:
            return False
        with self._lock:
            self._prepare_generation += 1
            generation = self._prepare_generation
        self._enqueue(
            "prepare",
            {"thread_key": clean_key, "generation": generation},
            priority=5,
        )
        return True

    def queue_message(self, thread_key: str, text: str) -> str:
        clean_key = str(thread_key or "").strip()
        content = str(text or "").strip()
        if not clean_key or not content:
            return ""
        thread = self._store.get_thread(clean_key)
        if not isinstance(thread, dict):
            return ""
        local_message = self._store.append_local_outbound_message(clean_key, content)
        local_id = str((local_message or {}).get("message_id") or "").strip()
        if not local_id:
            return ""
        queued_at = local_message.get("timestamp") if isinstance(local_message, dict) else None
        self._store.update_thread_state(
            clean_key,
            {
                "sender_status": "queued",
                "sender_error": "",
                "thread_error": "",
                "last_message": content,
                "last_activity_timestamp": queued_at or time.time(),
                "status": "active",
            },
        )
        job_id = self._store.create_send_queue_job(
            "send_message",
            thread_key=clean_key,
            account_id=str(thread.get("account_id") or "").strip(),
            payload={
                "thread_key": clean_key,
                "text": content,
                "local_message_id": local_id,
            },
        )
        self._enqueue(
            "send_message",
            {
                "job_id": job_id,
                "thread_key": clean_key,
                "text": content,
                "local_message_id": local_id,
            },
            priority=0,
        )
        self._notifier(
            reason="send_message_queued",
            thread_keys=[clean_key],
            account_ids=[str(thread.get("account_id") or "").strip()],
        )
        return local_id

    def queue_pack(self, thread_key: str, pack_id: str) -> bool:
        clean_key = str(thread_key or "").strip()
        clean_pack = str(pack_id or "").strip()
        if not clean_key or not clean_pack:
            return False
        thread = self._store.get_thread(clean_key)
        if not isinstance(thread, dict):
            return False
        pack = self._pack_by_id(clean_pack)
        if not isinstance(pack, dict):
            return False
        self._store.ensure_conversation_from_pack(
            account={"username": str(thread.get("account_id") or "").strip()},
            thread_row=thread,
            pack_name=str(pack.get("name") or clean_pack).strip(),
        )
        self._store.update_thread_state(
            clean_key,
            {
                "pack_status": "queued",
                "pack_error": "",
                "pack_id": clean_pack,
                "pack_name": str(pack.get("name") or clean_pack).strip(),
                "status": "pack_sent",
                "last_activity_timestamp": time.time(),
            },
        )
        job_id = self._store.create_send_queue_job(
            "send_pack",
            thread_key=clean_key,
            account_id=str(thread.get("account_id") or "").strip(),
            payload={"thread_key": clean_key, "pack_id": clean_pack},
            dedupe_key=f"pack:{clean_key}",
        )
        self._enqueue(
            "send_pack",
            {"job_id": job_id, "thread_key": clean_key, "pack_id": clean_pack},
            priority=10,
        )
        self._notifier(
            reason="send_pack_queued",
            thread_keys=[clean_key],
            account_ids=[str(thread.get("account_id") or "").strip()],
        )
        return True

    def _recover_jobs(self) -> None:
        jobs = self._store.list_send_queue_jobs(states=["pending", "sending"], limit=200)
        for job in jobs:
            task_type = str(job.get("task_type") or "").strip().lower()
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            job_id = int(job.get("id") or 0)
            if task_type == "send_message":
                thread_key = str(job.get("thread_key") or payload.get("thread_key") or "").strip()
                local_message_id = str(payload.get("local_message_id") or "").strip()
                text = str(payload.get("text") or "").strip()
                if not thread_key or not text or not local_message_id:
                    continue
                self._store.update_send_queue_job(job_id, state="pending")
                self._store.set_local_outbound_status(thread_key, local_message_id, status="pending")
                self._enqueue(
                    "send_message",
                    {
                        "job_id": job_id,
                        "thread_key": thread_key,
                        "text": text,
                        "local_message_id": local_message_id,
                    },
                    priority=0,
                )
                thread = self._store.get_thread(thread_key) or {}
                self._notifier(
                    reason="send_message_requeued",
                    thread_keys=[thread_key],
                    account_ids=[str(thread.get("account_id") or "").strip()],
                )
            elif task_type == "send_pack":
                thread_key = str(job.get("thread_key") or payload.get("thread_key") or "").strip()
                pack_id = str(payload.get("pack_id") or "").strip()
                if not thread_key or not pack_id:
                    continue
                self._store.update_send_queue_job(job_id, state="pending")
                self._enqueue(
                    "send_pack",
                    {"job_id": job_id, "thread_key": thread_key, "pack_id": pack_id},
                    priority=10,
                )

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task.task_type == "stop":
                self._queue.task_done()
                return
            self._active_task = str(task.task_type or "").strip()
            try:
                if task.task_type == "prepare":
                    self._handle_prepare(task.payload)
                elif task.task_type == "send_message":
                    self._handle_send_message(task.payload)
                elif task.task_type == "send_pack":
                    self._handle_send_pack(task.payload)
            except Exception as exc:
                try:
                    self._handle_task_exception(task, exc)
                except Exception:
                    logger.exception(
                        "Conversation sender failed while handling an unexpected task error"
                    )
            finally:
                self._active_task = ""
                self._queue.task_done()

    def _handle_task_exception(self, task: _SenderTask, error: Exception) -> None:
        task_type = str(task.task_type or "").strip().lower()
        payload = dict(task.payload or {})
        thread_key = str(payload.get("thread_key") or "").strip()
        local_message_id = str(payload.get("local_message_id") or "").strip()
        job_id = int(payload.get("job_id") or 0)
        error_text = str(error or "").strip() or error.__class__.__name__
        logger.exception(
            "Conversation sender task crashed | task_type=%s thread_key=%s job_id=%s",
            task_type,
            thread_key,
            job_id,
        )

        account_id = ""
        if thread_key:
            try:
                thread = self._store.get_thread(thread_key)
            except Exception:
                thread = None
            if isinstance(thread, dict):
                account_id = str(thread.get("account_id") or "").strip()

        if task_type == "send_message":
            if thread_key and local_message_id:
                try:
                    self._store.resolve_local_outbound(
                        thread_key,
                        local_message_id,
                        error_message=error_text,
                    )
                except Exception:
                    logger.debug("Could not mark local outbound message as failed", exc_info=True)
            if job_id > 0:
                try:
                    self._store.update_send_queue_job(job_id, state="error", error_message=error_text)
                except Exception:
                    logger.debug("Could not mark send queue job as failed", exc_info=True)
            if thread_key:
                try:
                    self._store.update_thread_state(
                        thread_key,
                        {
                            "sender_status": "failed",
                            "sender_error": error_text,
                            "thread_error": error_text,
                            "status": "error",
                        },
                    )
                except Exception:
                    logger.debug("Could not persist sender failure state for thread", exc_info=True)
        elif task_type == "send_pack":
            if job_id > 0:
                try:
                    self._store.update_send_queue_job(job_id, state="error", error_message=error_text)
                except Exception:
                    logger.debug("Could not mark pack job as failed", exc_info=True)
            if thread_key:
                try:
                    self._store.update_thread_state(
                        thread_key,
                        {
                            "pack_status": "failed",
                            "pack_error": error_text,
                            "sender_status": "failed",
                            "sender_error": error_text,
                            "status": "error",
                        },
                    )
                except Exception:
                    logger.debug("Could not persist pack failure state for thread", exc_info=True)
        elif task_type == "prepare" and thread_key:
            try:
                self._store.update_thread_state(
                    thread_key,
                    {
                        "thread_status": "failed",
                        "thread_error": error_text,
                        "sender_status": "failed",
                        "sender_error": error_text,
                    },
                )
            except Exception:
                logger.debug("Could not persist prepare failure state for thread", exc_info=True)

        try:
            self._notifier(
                reason="sender_worker_error",
                thread_keys=[thread_key] if thread_key else [],
                account_ids=[account_id] if account_id else [],
            )
        except Exception:
            logger.debug("Could not emit sender worker error notification", exc_info=True)

    def _handle_prepare(self, payload: dict[str, Any]) -> None:
        thread_key = str(payload.get("thread_key") or "").strip()
        generation = int(payload.get("generation") or 0)
        if not thread_key or self._is_stale_prepare(generation):
            return
        thread = self._store.get_thread(thread_key)
        if not isinstance(thread, dict):
            return
        account_id = str(thread.get("account_id") or "").strip()
        self._store.update_thread_state(
            thread_key,
            {
                "thread_status": "opening",
                "thread_error": "",
                "sender_status": "preparing",
                "sender_error": "",
            },
        )
        self._notifier(reason="prepare_thread_started", thread_keys=[thread_key], account_ids=[account_id])
        result = self._browser_pool.prepare(thread)
        if self._is_stale_prepare(generation):
            self._browser_pool.cancel()
            return
        if bool(result.get("ok", False)):
            self._store.update_thread_state(
                thread_key,
                {
                    "thread_status": "ready",
                    "thread_error": "",
                    "sender_status": "ready",
                    "sender_error": "",
                    "sender_prepared_at": time.time(),
                },
            )
            self._notifier(reason="prepare_thread_ready", thread_keys=[thread_key], account_ids=[account_id])
            return
        reason = str(result.get("reason") or "prepare_failed").strip()
        self._store.update_thread_state(
            thread_key,
            {
                "thread_status": "failed",
                "thread_error": reason,
                "sender_status": "failed",
                "sender_error": reason,
            },
        )
        self._notifier(reason="prepare_thread_failed", thread_keys=[thread_key], account_ids=[account_id])

    def _handle_send_message(self, payload: dict[str, Any]) -> None:
        thread_key = str(payload.get("thread_key") or "").strip()
        local_message_id = str(payload.get("local_message_id") or "").strip()
        job_id = int(payload.get("job_id") or 0)
        text = str(payload.get("text") or "").strip()
        thread = self._store.get_thread(thread_key)
        if not isinstance(thread, dict) or not text or not local_message_id:
            return
        account_id = str(thread.get("account_id") or "").strip()
        self._store.set_local_outbound_status(thread_key, local_message_id, status="sending")
        self._store.update_send_queue_job(job_id, state="sending")
        self._store.update_thread_state(
            thread_key,
            {
                "sender_status": "sending",
                "sender_error": "",
            },
        )
        self._notifier(reason="send_message_sending", thread_keys=[thread_key], account_ids=[account_id])
        result = self._browser_pool.send_text(thread, text)
        if bool(result.get("ok", False)):
            message_id = str(result.get("item_id") or "").strip()
            sent_timestamp = time.time()
            self._store.resolve_local_outbound(
                thread_key,
                local_message_id,
                final_message_id=message_id,
                sent_timestamp=sent_timestamp,
            )
            self._store.update_send_queue_job(job_id, state="sent")
            self._store.update_thread_state(
                thread_key,
                {
                    "sender_status": "ready",
                    "sender_error": "",
                    "thread_error": "",
                    "status": "active",
                    "last_message": text,
                    "last_activity_timestamp": sent_timestamp,
                },
            )
            self._store.record_action_memory(
                str(thread.get("thread_id") or "").strip(),
                account_id,
                "manual_reply_sent",
                source="inbox_crm",
            )
            responder_module._record_message_sent(
                account_id,
                str(thread.get("thread_id") or "").strip(),
                text,
                message_id=message_id,
                recipient_username=str(thread.get("recipient_username") or "").strip(),
            )
            self._store.set_account_health(account_id, "healthy", reason="")
            self._notifier(reason="send_message_success", thread_keys=[thread_key], account_ids=[account_id])
            return
        reason = str(result.get("reason") or "send_failed").strip()
        health_state, health_reason = self._classify_health_from_error(reason)
        if health_state != "unknown":
            self._store.set_account_health(account_id, health_state, reason=health_reason)
        self._store.resolve_local_outbound(thread_key, local_message_id, error_message=reason)
        self._store.update_send_queue_job(job_id, state="error", error_message=reason)
        self._store.update_thread_state(
            thread_key,
            {
                "sender_status": "failed",
                "sender_error": reason,
                "thread_error": reason,
                "status": "error",
            },
        )
        self._notifier(reason="send_message_failed", thread_keys=[thread_key], account_ids=[account_id])

    def _handle_send_pack(self, payload: dict[str, Any]) -> None:
        thread_key = str(payload.get("thread_key") or "").strip()
        pack_id = str(payload.get("pack_id") or "").strip()
        job_id = int(payload.get("job_id") or 0)
        thread = self._store.get_thread(thread_key)
        pack = self._pack_by_id(pack_id)
        if not isinstance(thread, dict) or not isinstance(pack, dict):
            return
        account_id = str(thread.get("account_id") or "").strip()
        self._store.update_send_queue_job(job_id, state="sending")
        self._store.update_thread_state(
            thread_key,
            {
                "pack_status": "running",
                "pack_error": "",
                "sender_status": "sending",
                "sender_error": "",
            },
        )
        self._notifier(reason="send_pack_running", thread_keys=[thread_key], account_ids=[account_id])
        result = self._browser_pool.send_pack(
            thread,
            pack,
            conversation_text=build_conversation_text(list(thread.get("messages") or []), limit=12),
            flow_config=responder_module._flow_config_for_account(account_id),
        )
        if bool(result.get("ok", False)):
            sent_at = time.time()
            self._store.update_send_queue_job(job_id, state="sent")
            self._store.update_thread_state(
                thread_key,
                {
                    "pack_status": "done",
                    "pack_error": "",
                    "sender_status": "ready",
                    "sender_error": "",
                    "status": "pack_sent",
                    "last_activity_timestamp": sent_at,
                    "pack_sent_at": sent_at,
                    "crm_relevant": True,
                },
            )
            self._store.record_action_memory(
                str(thread.get("thread_id") or "").strip(),
                account_id,
                "manual_pack_sent",
                pack_id=pack_id,
                source="inbox_crm",
            )
            self._notifier(reason="send_pack_success", thread_keys=[thread_key], account_ids=[account_id])
            return
        reason = str(result.get("reason") or result.get("error") or "pack_failed").strip()
        health_state, health_reason = self._classify_health_from_error(reason)
        if health_state != "unknown":
            self._store.set_account_health(account_id, health_state, reason=health_reason)
        self._store.update_send_queue_job(job_id, state="error", error_message=reason)
        self._store.update_thread_state(
            thread_key,
            {
                "pack_status": "failed",
                "pack_error": reason,
                "sender_status": "failed",
                "sender_error": reason,
                "status": "error",
            },
        )
        self._notifier(reason="send_pack_failed", thread_keys=[thread_key], account_ids=[account_id])

    def _enqueue(self, task_type: str, payload: dict[str, Any], *, priority: int) -> None:
        with self._lock:
            self._sequence += 1
            task = _SenderTask(
                priority=max(0, int(priority or 0)),
                sequence=self._sequence,
                task_type=str(task_type or "").strip(),
                payload=dict(payload or {}),
            )
            self._queue.put(task)

    def _is_stale_prepare(self, generation: int) -> bool:
        with self._lock:
            return generation != self._prepare_generation

    @staticmethod
    def _classify_health_from_error(error: Any) -> tuple[str, str]:
        text = str(error or "").strip()
        lowered = text.lower()
        if "proxy" in lowered:
            return "proxy_error", text or "proxy_error"
        if "checkpoint" in lowered or "challenge" in lowered:
            return "checkpoint", text or "checkpoint"
        if "suspend" in lowered:
            return "suspended", text or "suspended"
        if "banned" in lowered or "disabled" in lowered or "blocked" in lowered:
            return "banned", text or "banned"
        if "login" in lowered or "session" in lowered or "storage_state" in lowered:
            return "login_required", text or "login_required"
        return "unknown", text or "unknown"

    @staticmethod
    def _pack_by_id(pack_id: str) -> dict[str, Any] | None:
        clean_pack = str(pack_id or "").strip()
        if not clean_pack:
            return None
        for row in responder_module._list_packs():
            if isinstance(row, dict) and str(row.get("id") or "").strip() == clean_pack:
                return dict(row)
        return None
