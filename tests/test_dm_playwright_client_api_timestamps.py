from __future__ import annotations

import re
import time
from pathlib import Path

import src.dm_playwright_client as dm


def _build_client_stub(username: str = "tester"):
    client = dm.PlaywrightDMClient.__new__(dm.PlaywrightDMClient)
    client.username = username
    client.user_id = "me"
    client._api_messages_by_thread = {}
    client._api_thread_last_seen = {}
    client._response_listener_registered = False
    return client


def test_extract_api_messages_uses_real_timestamp_from_payload():
    payload = {
        "thread_id": "thread-1",
        "items": [
            {
                "item_id": "msg-1",
                "user_id": "me",
                "timestamp_ms": "1700000000123",
                "item_type": "text",
                "text": "hola",
            }
        ],
    }

    messages, missing = dm._extract_api_messages_from_payload(payload, self_user_id="me")

    assert not missing
    assert len(messages) == 1
    record = messages[0]
    assert record.thread_id == "thread-1"
    assert record.item_id == "msg-1"
    assert record.sender_id == "me"
    assert record.direction == "outbound"
    assert abs(record.timestamp - 1700000000.123) < 1e-6


def test_ingest_payload_without_timestamp_logs_and_skips_message(caplog):
    client = _build_client_stub()
    payload = {
        "thread_id": "thread-missing-ts",
        "items": [
            {
                "item_id": "msg-missing-ts",
                "user_id": "lead-1",
                "item_type": "text",
                "text": "hola",
            }
        ],
    }

    with caplog.at_level("WARNING"):
        added, missing_count = client._ingest_api_payload(
            payload,
            source_url="https://www.instagram.com/api/graphql/",
        )

    assert added == 0
    assert missing_count == 1
    assert "timestamp_missing_from_api" in caplog.text
    assert client._api_messages_by_thread == {}


def test_ingest_massive_payload_is_stable_and_deduplicates_items():
    clients = [_build_client_stub("acc1"), _build_client_stub("acc2")]
    start = time.perf_counter()

    for client in clients:
        for idx in range(500):
            thread_id = f"thread-{idx}"
            base_ts = 1700000000000 + (idx * 1000)
            payload = {
                "thread_id": thread_id,
                "items": [
                    {
                        "item_id": f"{thread_id}-1",
                        "user_id": "me",
                        "item_type": "text",
                        "text": "hola",
                        "timestamp_ms": base_ts,
                    },
                    {
                        "item_id": f"{thread_id}-1",
                        "user_id": "me",
                        "item_type": "text",
                        "text": "hola",
                        "timestamp_ms": base_ts,
                    },
                    {
                        "item_id": f"{thread_id}-2",
                        "user_id": "lead",
                        "item_type": "text",
                        "text": "ok",
                        "timestamp_ms": base_ts + 250,
                    },
                ],
            }
            added, missing_count = client._ingest_api_payload(
                payload,
                source_url="https://www.instagram.com/api/graphql/",
            )
            assert missing_count == 0
            assert added == 2

        overflow_total = dm._DM_API_CACHE_MAX_PER_THREAD + 90
        for msg_idx in range(overflow_total):
            payload = {
                "thread_id": "thread-overflow",
                "items": [
                    {
                        "item_id": f"overflow-{msg_idx}",
                        "user_id": "me",
                        "item_type": "text",
                        "text": "overflow",
                        "timestamp_ms": 1700001000000 + msg_idx,
                    }
                ],
            }
            client._ingest_api_payload(payload, source_url="https://www.instagram.com/api/graphql/")

    elapsed = time.perf_counter() - start

    for client in clients:
        assert len(client._api_messages_by_thread) <= dm._DM_API_CACHE_MAX_THREADS
        for idx in range(500):
            bucket = client._api_messages_by_thread[f"thread-{idx}"]
            assert len(bucket) == 2
        assert len(client._api_messages_by_thread["thread-overflow"]) == dm._DM_API_CACHE_MAX_PER_THREAD

    assert elapsed < 8.0


def test_dm_client_has_no_dom_timestamp_fallback_paths():
    source_path = Path(__file__).resolve().parents[1] / "src" / "dm_playwright_client.py"
    source = source_path.read_text(encoding="utf-8")

    assert "def _extract_message_timestamp" not in source
    assert "PlaywrightDM timestamps ausentes en thread" not in source
    assert "time_node = node.locator(\"time\")" not in source
    assert re.search(
        r"if\s+timestamp\s+is\s+None\s*:\s*\n\s*timestamp\s*=\s*time\\.time\(",
        source,
    ) is None
    assert "timestamp_missing_from_api" in source
