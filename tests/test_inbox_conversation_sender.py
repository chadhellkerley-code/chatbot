from __future__ import annotations

from core.inbox.conversation_sender import ConversationSender, _SenderTask


class _StoreStub:
    pass


class _BrowserPoolStub:
    def shutdown(self) -> None:
        return None


class _StoreSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_thread(self, thread_key: str):
        self.calls.append(("get_thread", thread_key))
        return {"account_id": "acc-1"}

    def resolve_local_outbound(
        self,
        thread_key: str,
        local_message_id: str,
        *,
        final_message_id: str = "",
        sent_timestamp: float | None = None,
        error_message: str = "",
    ) -> None:
        self.calls.append(
            (
                "resolve_local_outbound",
                {
                    "thread_key": thread_key,
                    "local_message_id": local_message_id,
                    "error_message": error_message,
                    "final_message_id": final_message_id,
                    "sent_timestamp": sent_timestamp,
                },
            )
        )

    def update_send_queue_job(self, job_id: int, *, state: str, error_message: str = "") -> None:
        self.calls.append(
            (
                "update_send_queue_job",
                {"job_id": job_id, "state": state, "error_message": error_message},
            )
        )

    def update_thread_state(self, thread_key: str, updates: dict[str, object]) -> None:
        self.calls.append(("update_thread_state", {"thread_key": thread_key, "updates": dict(updates)}))


def test_sender_run_loop_continues_after_unexpected_task_exception(monkeypatch) -> None:
    sender = ConversationSender(_StoreStub(), _BrowserPoolStub(), notifier=lambda **_kwargs: None)
    processed: list[int] = []
    failures: list[tuple[str, str]] = []

    def _handle_send_message(payload: dict[str, object]) -> None:
        marker = int(payload.get("marker") or 0)
        if marker == 1:
            raise RuntimeError("boom")
        processed.append(marker)

    monkeypatch.setattr(sender, "_handle_send_message", _handle_send_message)
    monkeypatch.setattr(
        sender,
        "_handle_task_exception",
        lambda task, exc: failures.append((str(task.task_type), str(exc))),
    )

    sender._enqueue("send_message", {"marker": 1}, priority=0)
    sender._enqueue("send_message", {"marker": 2}, priority=0)
    sender._enqueue("stop", {}, priority=99)
    sender._run_loop()

    assert failures == [("send_message", "boom")]
    assert processed == [2]


def test_sender_worker_exception_marks_send_message_as_failed() -> None:
    store = _StoreSpy()
    notifications: list[dict[str, object]] = []
    sender = ConversationSender(
        store,
        _BrowserPoolStub(),
        notifier=lambda **payload: notifications.append(dict(payload)),
    )
    task = _SenderTask(
        priority=0,
        sequence=1,
        task_type="send_message",
        payload={
            "thread_key": "acc-1:thread-1",
            "local_message_id": "local-1",
            "job_id": 77,
        },
    )

    sender._handle_task_exception(task, RuntimeError("sender exploded"))

    assert (
        "resolve_local_outbound",
        {
            "thread_key": "acc-1:thread-1",
            "local_message_id": "local-1",
            "error_message": "sender exploded",
            "final_message_id": "",
            "sent_timestamp": None,
        },
    ) in store.calls
    assert (
        "update_send_queue_job",
        {"job_id": 77, "state": "error", "error_message": "sender exploded"},
    ) in store.calls
    assert (
        "update_thread_state",
        {
            "thread_key": "acc-1:thread-1",
            "updates": {
                "sender_status": "failed",
                "sender_error": "sender exploded",
                "thread_error": "sender exploded",
                "status": "error",
            },
        },
    ) in store.calls
    assert notifications[-1]["reason"] == "sender_worker_error"
    assert notifications[-1]["thread_keys"] == ["acc-1:thread-1"]
    assert notifications[-1]["account_ids"] == ["acc-1"]
