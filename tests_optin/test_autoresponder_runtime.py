from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace

import responder


@dataclass
class _FakeRow:
    thread_id: str
    title: str
    snippet: str = "hola"


class _FakeLocator:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows

    def count(self) -> int:
        return len(self._rows)

    def nth(self, idx: int) -> _FakeRow:
        return self._rows[idx]


class _FakePage:
    def __init__(self, rows: list[_FakeRow]) -> None:
        self._rows = rows
        self.url = "https://www.instagram.com/direct/inbox/"

    def locator(self, _selector: str):
        return _FakeLocator(self._rows)

    def wait_for_timeout(self, _ms: int) -> None:
        return None


def _msg(message_id: str, user_id: str, text: str, ts: float):
    direction = "outbound" if user_id == "me" else "inbound"
    return SimpleNamespace(
        id=message_id,
        user_id=user_id,
        text=text,
        timestamp=ts,
        direction=direction,
    )


def _make_thread(thread_id: str, lead_username: str):
    lead = responder.UserLike(pk=lead_username, id=lead_username, username=lead_username)
    return responder.ThreadLike(
        id=thread_id,
        pk=thread_id,
        users=[lead],
        unread_count=0,
        title=lead_username,
        source_index=-1,
    )


class _FakeInboxClient:
    def __init__(self, rows: list[_FakeRow], *, list_threads_payload=None) -> None:
        self.username = "tester"
        self.user_id = "me"
        self.headless = True
        self._thread_cache: dict[str, object] = {}
        self._thread_cache_meta: dict[str, dict] = {}
        self._rows = rows
        self._page = _FakePage(rows)
        self.list_threads_payload = list_threads_payload or []
        self.list_threads_calls = 0
        self.get_messages_calls = 0
        self.sent_messages: list[tuple[str, str]] = []

    def _open_inbox(self) -> None:
        self._page.url = "https://www.instagram.com/direct/inbox/"

    def _ensure_page(self):
        return self._page

    def _row_selector_candidates(self):
        return ["fake_rows"]

    def _get_inbox_panel(self, page, rows=None):
        return page, "selector", "fake_rows", {}

    def _scroll_panel_to_top(self, _panel) -> None:
        return None

    def return_to_inbox(self) -> None:
        self._page.url = "https://www.instagram.com/direct/inbox/"

    def _stagnation_limit(self, _target: int) -> int:
        return 10

    def _row_is_valid(self, _row, *, selector=None) -> bool:
        return True

    def _row_lines(self, row: _FakeRow):
        return [row.title, row.snippet]

    def _resolve_thread_key(self, _page, row: _FakeRow, *, title: str, peer_username: str, snippet: str):
        return row.thread_id, "fake", f"/direct/t/{row.thread_id}/"

    def _scroll_panel_down(self, _panel, *, max_attempts=None) -> bool:
        return False

    def _wait_for_scroll_settle(self, _page, *, extra_ms: int = 0) -> None:
        return None

    def debug_dump_inbox(self, _reason: str) -> str:
        return "debug"

    def list_threads(self, amount: int = 20, filter_unread: bool = False):
        self.list_threads_calls += 1
        if self.list_threads_payload:
            return list(self.list_threads_payload)[:amount]
        derived = [_make_thread(row.thread_id, row.title) for row in self._rows]
        return derived[:amount]

    def get_messages(self, thread, amount: int = 10):
        self.get_messages_calls += 1
        now = time.time()
        return [_msg(f"in-{thread.id}", f"lead-{thread.id}", "hola", now)]

    def send_message(self, thread, text: str):
        self.sent_messages.append((str(thread.id), text))
        return f"sent-{len(self.sent_messages)}"


class _FakeFollowupClient:
    def __init__(self, threads):
        self.user_id = "me"
        self._threads = threads
        self.sent_messages: list[tuple[str, str]] = []

    def list_threads(self, amount: int = 20, filter_unread: bool = False):
        return list(self._threads)[:amount]

    def get_messages(self, thread, amount: int = 20):
        old_ts = time.time() - (responder._MIN_TIME_FOR_FOLLOWUP + 120)
        # Mensaje mas reciente saliente para habilitar followup.
        return [_msg(f"out-{thread.id}", "me", "te escribo", old_ts)]

    def send_message(self, thread, text: str):
        self.sent_messages.append((str(thread.id), text))
        return f"fu-{len(self.sent_messages)}"


class _ScenarioFollowupClient(_FakeFollowupClient):
    def __init__(self, threads, messages_by_thread: dict[str, list[object]]):
        super().__init__(threads)
        self._messages_by_thread = messages_by_thread

    def get_messages(self, thread, amount: int = 20):
        return list(self._messages_by_thread.get(str(thread.id), []))[:amount]


class _FakeIterInboxClient(_FakeInboxClient):
    def __init__(self, rows: list[_FakeRow], *, list_threads_payload=None) -> None:
        super().__init__(rows, list_threads_payload=list_threads_payload)
        self.iter_threads_calls = 0

    def iter_threads(self, amount: int = 20, filter_unread: bool = False):
        self.iter_threads_calls += 1
        if self.list_threads_payload:
            for thread in list(self.list_threads_payload)[:amount]:
                yield thread
            return
        for row in self._rows[:amount]:
            yield _make_thread(row.thread_id, row.title)

    def list_threads(self, amount: int = 20, filter_unread: bool = False):
        raise AssertionError("list_threads no debe llamarse cuando existe iter_threads")


class _FakeIterInboxClientWithOpenFailures(_FakeIterInboxClient):
    def __init__(self, rows: list[_FakeRow], *, fail_thread_ids: set[str]) -> None:
        super().__init__(rows)
        self.fail_thread_ids = {str(v) for v in fail_thread_ids}

    def get_messages(self, thread, amount: int = 10):
        self.get_messages_calls += 1
        if str(getattr(thread, "id", "")) in self.fail_thread_ids:
            return []
        now = time.time()
        return [_msg(f"in-{thread.id}", f"lead-{thread.id}", "hola", now)]


def _patch_inbox_dependencies(monkeypatch, *, can_send: bool, delay_calls: list[str]) -> None:
    monkeypatch.setattr(responder, "_gen_response", lambda *_args, **_kwargs: "respuesta")
    monkeypatch.setattr(
        responder,
        "_can_send_message",
        lambda *_args, **_kwargs: (can_send, "ok" if can_send else "skip"),
    )
    monkeypatch.setattr(
        responder,
        "_sleep_between_replies_sync",
        lambda *_args, **kwargs: delay_calls.append(str(kwargs.get("label", "reply_delay"))),
    )
    monkeypatch.setattr(responder, "_determine_conversation_stage", lambda *_a, **_k: responder._STAGE_ACTIVE)
    monkeypatch.setattr(responder, "_get_conversation_state", lambda *_a, **_k: {})
    monkeypatch.setattr(responder, "_update_conversation_state", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_record_message_received", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_record_message_sent", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_classify_response", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_send_lead_to_gohighlevel", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_maybe_schedule_google_calendar_event", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "save_auto_state", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_print_response_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "log_conversation_status", lambda *_a, **_k: None)


def _patch_followup_dependencies(monkeypatch, *, logs: list[dict] | None = None) -> None:
    monkeypatch.setattr(responder, "_load_all_conversations_to_memory", lambda *_a, **_k: None)
    monkeypatch.setattr(
        responder,
        "_followup_enabled_entry_for",
        lambda _user: ("alias", {"enabled": True, "prompt": "seguir"}),
    )
    monkeypatch.setattr(responder, "_followup_decision", lambda *_a, **_k: ("seguimiento", 1))
    monkeypatch.setattr(
        responder,
        "_determine_followup_stage_from_initial_message",
        lambda *_a, **_k: (1, responder._MIN_TIME_FOR_FOLLOWUP + 120),
    )
    monkeypatch.setattr(
        responder,
        "_get_conversation_state",
        lambda *_a, **_k: {
            "last_message_sent_at": time.time() - (responder._MIN_TIME_FOR_FOLLOWUP + 180),
            "last_message_received_at": None,
            "last_message_sender": "me",
            "messages_sent": [],
        },
    )
    monkeypatch.setattr(responder, "_can_send_message", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(responder, "_record_message_sent", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_record_message_received", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_update_conversation_state", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_classify_response", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_send_lead_to_gohighlevel", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_maybe_schedule_google_calendar_event", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_sleep_between_replies_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "save_auto_state", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_print_response_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "log_conversation_status", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_save_conversation_state", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_set_followup_entry", lambda *_a, **_k: None)
    if logs is not None:
        monkeypatch.setattr(responder, "_append_message_log", lambda payload: logs.append(dict(payload)))


def test_inbox_small_scan_is_fast_and_delay_only_between_messages(monkeypatch):
    responder.reset_stop_event()
    rows = [_FakeRow(thread_id=f"t{i}", title=f"lead{i}") for i in range(5)]
    client = _FakeInboxClient(rows)
    delay_calls: list[str] = []
    _patch_inbox_dependencies(monkeypatch, can_send=True, delay_calls=delay_calls)
    stats = responder.BotStats(alias="alias")

    responder._process_inbox(
        client=client,
        user="tester",
        state={},
        api_key="x",
        system_prompt="p",
        stats=stats,
        delay_min=3,
        delay_max=45,
        max_age_days=7,
        allowed_thread_ids=None,
        threads_limit=5,
    )

    assert client.list_threads_calls >= 1
    assert len(client.sent_messages) == 5
    assert len(delay_calls) == 4
    assert stats.responded == 5


def test_inbox_500_threads_reaches_target_with_bulk_reseed(monkeypatch):
    responder.reset_stop_event()
    rows = [_FakeRow(thread_id="seed-1", title="seed1"), _FakeRow(thread_id="seed-2", title="seed2")]
    reseed_threads = [_make_thread(f"bulk-{i}", f"lead{i}") for i in range(650)]
    client = _FakeInboxClient(rows, list_threads_payload=reseed_threads)
    delay_calls: list[str] = []
    _patch_inbox_dependencies(monkeypatch, can_send=False, delay_calls=delay_calls)
    stats = responder.BotStats(alias="alias")

    responder._process_inbox(
        client=client,
        user="tester",
        state={},
        api_key="x",
        system_prompt="p",
        stats=stats,
        delay_min=3,
        delay_max=45,
        max_age_days=7,
        allowed_thread_ids=None,
        threads_limit=500,
    )

    assert client.list_threads_calls >= 1
    assert client.get_messages_calls >= 500
    assert len(client.sent_messages) == 0
    assert stats.responded == 0


def test_followup_sends_and_applies_delay_only_between_messages(monkeypatch):
    responder.reset_stop_event()
    threads = [_make_thread("fu-1", "lead1"), _make_thread("fu-2", "lead2")]
    client = _FakeFollowupClient(threads)
    delay_calls: list[str] = []

    monkeypatch.setattr(responder, "_load_all_conversations_to_memory", lambda *_a, **_k: None)
    monkeypatch.setattr(
        responder,
        "_followup_enabled_entry_for",
        lambda _user: ("alias", {"enabled": True, "prompt": "seguir"}),
    )
    monkeypatch.setattr(responder, "_followup_decision", lambda *_a, **_k: ("seguimiento", 1))
    monkeypatch.setattr(
        responder,
        "_determine_followup_stage_from_initial_message",
        lambda *_a, **_k: (1, responder._MIN_TIME_FOR_FOLLOWUP + 120),
    )
    monkeypatch.setattr(
        responder,
        "_get_conversation_state",
        lambda *_a, **_k: {
            "last_message_sent_at": time.time() - (responder._MIN_TIME_FOR_FOLLOWUP + 180),
            "last_message_received_at": None,
            "last_message_sender": "me",
            "messages_sent": [],
        },
    )
    monkeypatch.setattr(responder, "_can_send_message", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(responder, "_record_message_sent", lambda *_a, **_k: None)
    monkeypatch.setattr(responder, "_update_conversation_state", lambda *_a, **_k: None)
    monkeypatch.setattr(
        responder,
        "_sleep_between_replies_sync",
        lambda *_a, **kwargs: delay_calls.append(str(kwargs.get("label", "reply_delay"))),
    )

    responder._process_followups(
        client=client,
        user="tester",
        api_key="x",
        delay_min=3,
        delay_max=45,
        max_age_days=7,
        threads_limit=5,
        followup_schedule_hours=[4, 8, 12, 24],
    )

    assert len(client.sent_messages) == 2
    assert len(delay_calls) == 1


def test_inbox_prefers_iter_threads_over_list_threads(monkeypatch):
    responder.reset_stop_event()
    rows = [_FakeRow(thread_id=f"t{i}", title=f"lead{i}") for i in range(3)]
    client = _FakeIterInboxClient(rows)
    delay_calls: list[str] = []
    _patch_inbox_dependencies(monkeypatch, can_send=False, delay_calls=delay_calls)
    stats = responder.BotStats(alias="alias")

    responder._process_inbox(
        client=client,
        user="tester",
        state={},
        api_key="x",
        system_prompt="p",
        stats=stats,
        delay_min=3,
        delay_max=45,
        max_age_days=7,
        allowed_thread_ids=None,
        threads_limit=3,
    )

    assert client.iter_threads_calls == 1
    assert client.get_messages_calls == 3


def test_inbox_reaches_target_even_if_some_threads_fail_to_open(monkeypatch):
    responder.reset_stop_event()
    rows = [_FakeRow(thread_id=f"t{i}", title=f"lead{i}") for i in range(6)]
    client = _FakeIterInboxClientWithOpenFailures(rows, fail_thread_ids={"t0", "t1"})
    delay_calls: list[str] = []
    captured_threads: list[str] = []
    _patch_inbox_dependencies(monkeypatch, can_send=False, delay_calls=delay_calls)
    monkeypatch.setattr(
        responder,
        "_update_conversation_state",
        lambda _account, thread_id, _updates, _recipient_username=None: captured_threads.append(str(thread_id)),
    )
    stats = responder.BotStats(alias="alias")

    responder._process_inbox(
        client=client,
        user="tester",
        state={},
        api_key="x",
        system_prompt="p",
        stats=stats,
        delay_min=3,
        delay_max=45,
        max_age_days=7,
        allowed_thread_ids=None,
        threads_limit=3,
    )

    # Debe completar 3 hilos trabajados aunque los primeros 2 fallen al abrir.
    # Cada hilo procesado actualiza estado al menos para captura de contexto.
    unique_captured = {tid for tid in captured_threads}
    assert len(unique_captured) >= 3


def test_followup_allows_thread_when_last_outbound_is_older_than_60s(monkeypatch):
    responder.reset_stop_event()
    now = time.time()
    thread = _make_thread("fu-old", "lead_old")
    client = _ScenarioFollowupClient(
        [thread],
        messages_by_thread={
            "fu-old": [_msg("out-old", "me", "te escribo", now - 180)],
        },
    )
    logs: list[dict] = []
    _patch_followup_dependencies(monkeypatch, logs=logs)

    responder._process_followups(
        client=client,
        user="tester",
        api_key="x",
        delay_min=0,
        delay_max=0,
        max_age_days=7,
        threads_limit=5,
        followup_schedule_hours=[0],
    )

    assert len(client.sent_messages) == 1
    skip_logs = [entry for entry in logs if entry.get("action") == "followup_skip"]
    assert not skip_logs


def test_followup_blocks_thread_when_last_outbound_is_newer_than_60s(monkeypatch):
    responder.reset_stop_event()
    now = time.time()
    thread = _make_thread("fu-recent", "lead_recent")
    client = _ScenarioFollowupClient(
        [thread],
        messages_by_thread={
            "fu-recent": [_msg("out-recent", "me", "te escribo", now - 30)],
        },
    )
    logs: list[dict] = []
    _patch_followup_dependencies(monkeypatch, logs=logs)
    monkeypatch.setattr(
        responder,
        "_get_conversation_state",
        lambda *_a, **_k: {
            "last_message_sent_at": None,
            "last_message_received_at": None,
            "last_message_sender": None,
            "messages_sent": [],
        },
    )

    responder._process_followups(
        client=client,
        user="tester",
        api_key="x",
        delay_min=0,
        delay_max=0,
        max_age_days=7,
        threads_limit=5,
        followup_schedule_hours=[0],
    )

    assert len(client.sent_messages) == 0
    skip_reasons = [entry.get("reason") for entry in logs if entry.get("action") == "followup_skip"]
    assert "skip_last_outbound_lt_60s_or_fallback_suspected" in skip_reasons


def test_followup_does_not_invent_timestamp_when_message_timestamp_is_missing(monkeypatch):
    responder.reset_stop_event()
    thread = _make_thread("fu-missing-ts", "lead_missing_ts")
    message_without_timestamp = SimpleNamespace(
        id="out-missing",
        user_id="me",
        text="te escribo",
        timestamp=None,
        direction="outbound",
    )
    client = _ScenarioFollowupClient(
        [thread],
        messages_by_thread={"fu-missing-ts": [message_without_timestamp]},
    )
    logs: list[dict] = []
    _patch_followup_dependencies(monkeypatch, logs=logs)

    responder._process_followups(
        client=client,
        user="tester",
        api_key="x",
        delay_min=0,
        delay_max=0,
        max_age_days=0,
        threads_limit=5,
        followup_schedule_hours=[0],
    )

    assert len(client.sent_messages) == 0
    skip_reasons = [entry.get("reason") for entry in logs if entry.get("action") == "followup_skip"]
    assert "skip_no_outbound_messages" in skip_reasons
