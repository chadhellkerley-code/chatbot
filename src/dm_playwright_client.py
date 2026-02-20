from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, List, Optional

from ui import Fore, style_text

try:  # pragma: no cover - optional dependency guard
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except Exception:  # pragma: no cover
    Page = object  # type: ignore
    PlaywrightTimeoutError = Exception  # type: ignore
    sync_playwright = None  # type: ignore

from src.playwright_service import (
    BASE_PROFILES,
    DEFAULT_ARGS,
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
    resolve_playwright_executable,
)

logger = logging.getLogger(__name__)

INBOX_URL = "https://www.instagram.com/direct/inbox/"
THREAD_URL_TEMPLATE = "https://www.instagram.com/direct/t/{thread_id}/"
DM_DEBUG_DIRNAME = "dm_debug"

VERIFY_TIMEOUT_S = float(os.getenv("HUMAN_DM_VERIFY_TIMEOUT", "10.0"))


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "si", "on"}


_DM_VERBOSE_PROBES = _env_enabled("AUTORESPONDER_DM_VERBOSE_PROBES", False)

# Selectores mÃƒÂ­nimos necesarios (eliminadas constantes legacy de anchors)
ROW_SELECTOR = "div[role='button'][tabindex='0']"
_MESSAGE_CONTAINER_SELECTORS = (
    "main",
    "div[role='main']",
)
_MESSAGE_NODE_SELECTORS = (
    "[data-testid='message-bubble']",
    "div[role='row']",
    "div[role='none']",
    "div[dir='auto']",
)
THREAD_ROW_SELECTOR = "div[role='main'] a[href*='/direct/t/']"
_COMPOSER_SELECTORS = (
    "div[role='main'] div[role='textbox'][contenteditable='true']",
    "div[role='main'] div[contenteditable='true'][role='textbox']",
    "div[role='main'] textarea",
    "div[role='textbox'][contenteditable='true']",
    "div[contenteditable='true']",
)

_UNREAD_HINTS = ("unread", "sin leer", "no leido", "no leido")
_NOTE_PHRASES = (
    "tu nota",
    "your note",
    "share a note",
    "compartir una nota",
    "agrega una nota",
    "agregar una nota",
    "create note",
    "create a note",
    "primera nota",
)


def _env_int(name: str, default: int, *, min_value: int = 1, max_value: int = 10_000) -> int:
    raw = os.getenv(name)
    if raw is None:
        return max(min_value, min(max_value, int(default)))
    try:
        value = int(float(str(raw).strip()))
    except Exception:
        value = int(default)
    return max(min_value, min(max_value, value))


_DM_SCROLL_WAIT_MS = _env_int("AUTORESPONDER_DM_SCROLL_WAIT_MS", 180, min_value=50, max_value=2_500)
_DM_SCROLL_ATTEMPTS = _env_int("AUTORESPONDER_DM_SCROLL_ATTEMPTS", 4, min_value=1, max_value=12)
_DM_MESSAGE_HYDRATION_TIMEOUT_MS = _env_int(
    "AUTORESPONDER_DM_MESSAGE_HYDRATION_TIMEOUT_MS",
    900,
    min_value=200,
    max_value=5_000,
)
_DM_RETURN_INBOX_TIMEOUT_MS = _env_int(
    "AUTORESPONDER_DM_RETURN_INBOX_TIMEOUT_MS",
    1200,
    min_value=300,
    max_value=8_000,
)
_DM_STAGNANT_BASE_LIMIT = _env_int(
    "AUTORESPONDER_DM_STAGNANT_BASE_LIMIT",
    12,
    min_value=8,
    max_value=200,
)
_DM_STAGNANT_MAX_LIMIT = _env_int(
    "AUTORESPONDER_DM_STAGNANT_MAX_LIMIT",
    40,
    min_value=_DM_STAGNANT_BASE_LIMIT,
    max_value=500,
)
_DM_API_WAIT_TIMEOUT_MS = _env_int(
    "AUTORESPONDER_DM_API_WAIT_TIMEOUT_MS",
    1_500,
    min_value=250,
    max_value=8_000,
)
_DM_THREAD_VISUAL_SYNC_TIMEOUT_MS = _env_int(
    "AUTORESPONDER_DM_THREAD_VISUAL_SYNC_TIMEOUT_MS",
    6_000,
    min_value=1_000,
    max_value=20_000,
)
_DM_THREAD_NETWORK_SYNC_TIMEOUT_MS = _env_int(
    "AUTORESPONDER_DM_THREAD_NETWORK_SYNC_TIMEOUT_MS",
    5_000,
    min_value=1_000,
    max_value=20_000,
)
_DM_API_CACHE_MAX_PER_THREAD = _env_int(
    "AUTORESPONDER_DM_API_CACHE_MAX_PER_THREAD",
    120,
    min_value=20,
    max_value=2_000,
)
_DM_API_CACHE_MAX_THREADS = _env_int(
    "AUTORESPONDER_DM_API_CACHE_MAX_THREADS",
    1_500,
    min_value=100,
    max_value=20_000,
)


def _status_check_timeout_ms() -> int:
    raw = os.getenv("ACCOUNT_STATUS_CHECK_TIMEOUT_MS", "1500")
    try:
        return max(250, int(float(raw)))
    except Exception:
        return 1500


def _status_log_path() -> Path:
    explicit = (os.getenv("ACCOUNT_STATUS_FILE") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    app_root = (os.getenv("APP_DATA_ROOT") or "").strip()
    if app_root:
        return Path(app_root).expanduser() / "storage" / "accounts_status.json"
    return Path(__file__).resolve().parents[1] / "storage" / "accounts_status.json"


def log_account_status(username: str, status: str) -> None:
    user = str(username or "").strip().lstrip("@")
    current = str(status or "unknown").strip().lower() or "unknown"
    now_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    logger.info("PlaywrightDM account_status account=@%s status=%s", user, current)
    if not user:
        return
    try:
        path = _status_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, dict[str, str]] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                payload = {}
        payload[user] = {
            "username": user,
            "status": current,
            "last_checked": now_utc,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        return


def _safe_page_text(page: Page) -> str:
    try:
        body = page.locator("body")
        if body.count() > 0:
            return (body.first.inner_text() or "").strip().lower()
    except Exception:
        pass
    try:
        html = page.content() or ""
    except Exception:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", html)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip().lower()


def _detect_account_status_impl(page: Page) -> str:
    try:
        url = (getattr(page, "url", "") or "").lower()
    except Exception:
        url = ""

    try:
        if "/accounts/login/" in url or "/accounts/login" in url:
            return "session_expired"
        if "/challenge/" in url or "/checkpoint/" in url or "two_factor" in url:
            return "checkpoint"
        if "/accounts/suspended/" in url:
            return "suspended"
        if "/accounts/disabled/" in url:
            return "blocked"

        try:
            if page.locator("input[name='username'], input[name='password']").count() > 0:
                return "session_expired"
        except Exception:
            pass

        text = _safe_page_text(page)
        if "temporarily blocked" in text or "bloqueada temporalmente" in text:
            return "blocked"
        if "disabled your account" in text or "your account has been disabled" in text:
            return "blocked"
        if "suspended" in text or "cuenta suspendida" in text:
            return "suspended"
        if "checkpoint" in text or "challenge required" in text:
            return "checkpoint"

        alive_selector = ",".join(
            (
                "a[href='/direct/inbox/']",
                "a[href*='/direct/inbox/']",
                "a[href*='/direct/t/']",
                "a[aria-label='Direct']",
                "a[aria-label='Mensajes']",
                "input[placeholder='Buscar']",
                "input[placeholder='Search']",
                "input[name='queryBox']",
                "div[role='textbox'][contenteditable='true']",
                "textarea[placeholder*='Message']",
                "textarea[placeholder*='Mensaje']",
                "svg[aria-label='Home']",
                "svg[aria-label='Inicio']",
            )
        )
        try:
            if page.locator(alive_selector).count() > 0:
                return "alive"
        except Exception:
            pass

        try:
            page.wait_for_selector(alive_selector, timeout=_status_check_timeout_ms())
            return "alive"
        except Exception:
            pass

        if "/direct/inbox/" in url or "/direct/t/" in url:
            return "alive"

        return "unknown"
    except Exception:
        return "unknown"


async def detect_account_status(page: Page) -> str:
    return _detect_account_status_impl(page)


def detect_account_status_sync(page: Page) -> str:
    return _detect_account_status_impl(page)


@dataclass
class UserLike:
    pk: str
    id: str
    username: str


@dataclass
class ThreadLike:
    id: str
    pk: str
    users: List[UserLike]
    unread_count: int = 0
    link: str = ""
    title: str = ""
    snippet: str = ""
    source_index: int = -1


@dataclass
class MessageLike:
    id: str
    user_id: str
    text: str
    timestamp: Optional[float]
    direction: str = "inbound"  # "inbound" or "outbound"


@dataclass(frozen=True)
class _APIMessageRecord:
    thread_id: str
    sender_id: str
    timestamp: float
    item_id: str
    direction: str
    text: str = ""


class PlaywrightDMClient:
    def __init__(
        self,
        *,
        account: Optional[dict] = None,
        headless: bool = False,
        slow_mo_ms: int = 1000,
        dump_on_error: bool = True,
    ) -> None:
        if sync_playwright is None:
            raise RuntimeError(
                "Playwright no esta instalado. Ejecuta 'pip install playwright' y luego 'playwright install'."
            )
        self.account = dict(account or {})
        self.username = (self.account.get("username") or "").strip().lstrip("@")
        if not self.username:
            raise RuntimeError("Cuenta invalida: falta username para PlaywrightDMClient.")

        self.user_id = self.username
        self.headless = bool(headless)
        self.slow_mo_ms = max(0, int(slow_mo_ms or 0))
        self.dump_on_error = bool(dump_on_error)

        self._playwright = None
        self._browser = None
        self._context = None
        self._page: Optional[Page] = None
        self._current_thread_id: Optional[str] = None
        self._thread_cache: dict[str, ThreadLike] = {}
        self._thread_cache_meta: dict[str, dict] = {}
        self._api_messages_by_thread: dict[str, dict[str, _APIMessageRecord]] = {}
        self._api_thread_last_seen: dict[str, float] = {}
        self._response_listener_registered = False
        self._account_status_checked = False

    @staticmethod
    def storage_state_path(username: str) -> Path:
        safe = (username or "").strip().lstrip("@")
        return Path(BASE_PROFILES) / safe / "storage_state.json"

    def close(self) -> None:
        try:
            if self._page is not None:
                try:
                    if not self._page.is_closed():
                        self._page.close()
                except Exception:
                    pass
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
            if self._browser is not None:
                try:
                    self._browser.close()
                except Exception:
                    pass
        finally:
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self._playwright = None
            self._browser = None
            self._context = None
            self._page = None
            self._current_thread_id = None
            self._api_messages_by_thread = {}
            self._api_thread_last_seen = {}
            self._response_listener_registered = False
            self._account_status_checked = False

    def get_my_username(self) -> str:
        return self.username

    def user_info(self, user_id: object) -> object:
        class _Info:
            def __init__(self, username: str) -> None:
                self.username = username

        if isinstance(user_id, str):
            return _Info(user_id)
        return _Info(str(user_id))

    def ensure_ready(self) -> None:
        self._ensure_page()
        self._open_inbox()

    def list_threads(self, amount: int = 20, filter_unread: bool = False) -> List[ThreadLike]:
        return list(self.iter_threads(amount=amount, filter_unread=filter_unread))

    def iter_threads(self, amount: int = 20, filter_unread: bool = False) -> Iterator[ThreadLike]:
        """
        Discovery incremental de inbox.
        Produce threads a medida que se descubren para evitar bloqueos largos
        antes de arrancar el procesamiento del primer hilo.
        """
        page = self._ensure_page()
        self._open_inbox()

        target = max(1, int(amount or 1))
        selector_candidates = self._row_selector_candidates()
        seen_thread_keys: set[str] = set()
        yielded = 0

        rows = None
        selected_selector = ""
        inbox_panel = page

        def _resolve_rows() -> tuple[object | None, str]:
            for selector in selector_candidates:
                try:
                    candidate = page.locator(selector)
                    total = candidate.count()
                    if _DM_VERBOSE_PROBES:
                        print(style_text(f"[Probe] Selector '{selector}' -> count={total}", color=Fore.WHITE))
                    if total > 0:
                        return candidate, selector
                except Exception:
                    continue
            return None, ""

        rows, selected_selector = _resolve_rows()
        if rows is None:
            return

        inbox_panel, _method, _selector, _panel_meta = self._get_inbox_panel(page, rows=rows)
        self._scroll_panel_to_top(inbox_panel)

        if target >= 300:
            max_scroll_passes = max(40, min(5000, target * 8))
        else:
            max_scroll_passes = max(25, min(2000, target * 6))
        stagnant_limit = self._stagnation_limit(target)
        if target >= 250:
            stagnant_limit = max(stagnant_limit, min(90, 24 + (target // 12)))
        stagnant_passes = 0

        for _pass in range(max_scroll_passes):
            if yielded >= target:
                break

            current_url = page.url or ""
            if "/direct/inbox/" not in current_url:
                if "/direct/t/" in current_url and self._has_thread_rows_visible(page):
                    pass
                else:
                    self.return_to_inbox()
                    current_url = page.url or ""
                    if "/direct/inbox/" not in current_url:
                        self._open_inbox()
            if not self._has_thread_rows_visible(page):
                self._open_inbox()
                rows, selected_selector = _resolve_rows()
                if rows is None:
                    break
                inbox_panel, _method, _selector, _panel_meta = self._get_inbox_panel(page, rows=rows)

            if rows is None:
                rows, selected_selector = _resolve_rows()
                if rows is None:
                    break
                inbox_panel, _method, _selector, _panel_meta = self._get_inbox_panel(page, rows=rows)

            before_count = yielded
            try:
                total = rows.count()
            except Exception:
                rows, selected_selector = _resolve_rows()
                if rows is None:
                    break
                try:
                    total = rows.count()
                except Exception:
                    total = 0

            for idx in range(total):
                if yielded >= target:
                    break

                row = rows.nth(idx)
                try:
                    row_valid = self._row_is_valid(row, selector=selected_selector, fast=True)
                except TypeError:
                    row_valid = self._row_is_valid(row, selector=selected_selector)
                if not row_valid:
                    continue

                if filter_unread and _thread_unread_count(row) <= 0:
                    continue

                lines = self._row_lines(row)
                title = (lines[0] if lines else "unknown").strip()
                if not title:
                    continue
                snippet_parts = [(line or "").strip() for line in lines[1:4] if (line or "").strip()]
                snippet = " | ".join(snippet_parts)
                thread_key, key_source, _direct_link = self._resolve_thread_key(
                    page,
                    row,
                    title=title,
                    peer_username=title,
                    snippet=snippet,
                )
                if not thread_key:
                    fallback_seed = f"{self.username}|{title}|{snippet}|{idx}"
                    fallback_hash = hashlib.sha1(
                        fallback_seed.encode("utf-8", errors="ignore")
                    ).hexdigest()[:16]
                    thread_key = f"stable_{fallback_hash}"
                    key_source = "scan_fallback"
                if thread_key in seen_thread_keys:
                    continue
                seen_thread_keys.add(thread_key)

                thread = ThreadLike(
                    id=thread_key,
                    pk=thread_key,
                    users=[UserLike(pk=title, id=title, username=title)],
                    title=title,
                    snippet=snippet,
                    # El indice deja de ser confiable cuando hay scroll/virtualizacion;
                    # forzamos apertura por cache/titulo para evitar clicks en fila incorrecta.
                    source_index=idx,
                )
                self._thread_cache[thread_key] = thread
                self._thread_cache_meta[thread_key] = {
                    "title": title,
                    "snippet": snippet,
                    "idx": idx,
                    "selector": selected_selector,
                    "key_source": key_source,
                }
                yielded += 1
                yield thread

            if yielded >= target:
                break

            before_scroll = self._panel_scroll_metrics(inbox_panel)
            moved = self._scroll_panel_down(inbox_panel)
            if not moved:
                break

            added = yielded - before_count
            try:
                total_after_scroll = rows.count()
            except Exception:
                total_after_scroll = total
            after_scroll = self._panel_scroll_metrics(inbox_panel)
            scroll_top_unchanged = float((after_scroll or {}).get("top", 0)) <= float((before_scroll or {}).get("top", 0)) + 1
            scroll_height_not_increased = float((after_scroll or {}).get("height", 0)) <= float((before_scroll or {}).get("height", 0))
            no_new_rows_detected = int(total_after_scroll) <= int(total)
            if added <= 0 and scroll_top_unchanged and scroll_height_not_increased and no_new_rows_detected:
                stagnant_passes += 1
            else:
                stagnant_passes = 0
            if stagnant_passes >= stagnant_limit:
                break

            self._wait_for_scroll_settle(page)

        if _DM_VERBOSE_PROBES:
            print(
                style_text(
                    f"[Probe] iter_threads target={target} discovered={yielded}",
                    color=Fore.WHITE,
                )
            )

    def open_thread(self, thread_id: str) -> bool:
        key = str(thread_id or "").strip()
        if not key:
            logger.error("PlaywrightDM open_thread called with empty thread_id")
            return False
        thread = self._thread_cache.get(key)
        if thread is None:
            logger.error("PlaywrightDM open_thread cache_miss thread_id=%s", key)
            return False
        return self._open_thread(thread)

    # LEGACY: FunciÃƒÂ³n deshabilitada - ya no se usa (reemplazada por click-first scan)
    # def _list_threads_from_anchors(...)

    def _row_lines(self, row) -> List[str]:
        try:
            raw_text = row.inner_text() or ""
        except Exception:
            raw_text = ""
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        if lines:
            return lines
        # Fallback: aria-label del avatar (suele contener username o "Tu nota")
        try:
            aria = (row.get_attribute("aria-label") or "").strip()
        except Exception:
            aria = ""
        if aria:
            return [aria]
        # Fallback: intentar tomar texto del contenedor superior clickeable
        try:
            parent = row.locator("xpath=ancestor::div[@role='button'][1]").first
            parent_text = parent.inner_text() or ""
        except Exception:
            parent_text = ""
        return [line.strip() for line in parent_text.splitlines() if line.strip()]

    def _row_selector_candidates(self) -> List[str]:
        # Prioritize lightweight selectors first; heavy :has(...) selectors go last.
        return [
            "div[role='navigation'][aria-label='Lista de conversaciones'] div[role='button'][tabindex='0']",
            "div[role='navigation'][aria-label='Conversation list'] div[role='button'][tabindex='0']",
            "div[role='navigation'][aria-label='List of conversations'] div[role='button'][tabindex='0']",
            "div[role='navigation'] div[role='button'][tabindex='0']",
            THREAD_ROW_SELECTOR,
            "a[href*='/direct/t/']",
            "div[role='main'] div[role='list'] a[href*='/direct/t/']",
            "div[aria-label='Chats'] a[href*='/direct/t/']",
            "div[aria-label='Mensajes'] a[href*='/direct/t/']",
            "div[aria-label='Messages'] a[href*='/direct/t/']",
            "div[role='main'] div[role='list'] div[role='listitem']",
            "div[role='main'] div[role='list'] div[role='row']",
            "div[role='main'] div[role='list'] div[role='button'][tabindex='0']",
            "div[role='main'] div[role='listitem']",
            "div[role='main'] div[role='row']",
            "div[role='main'] div[role='button'][tabindex='0']",
            "div[role='navigation'][aria-label='Lista de conversaciones'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'][aria-label='Conversation list'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'][aria-label='List of conversations'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'][aria-label='Lista de conversaciones'] div[role='button'][tabindex='0']:has(abbr)",
            "div[role='navigation'][aria-label='Conversation list'] div[role='button'][tabindex='0']:has(abbr)",
            "div[role='navigation'][aria-label='List of conversations'] div[role='button'][tabindex='0']:has(abbr)",
            "div[role='navigation'] div[role='button'][tabindex='0']:has(abbr)",
        ]

    def _get_inbox_panel(self, page: Page, rows=None):
        """
        [Probe/Fix] Intenta encontrar el panel lateral de mensajes.
        Retorna (locator, metodo, selector, meta).
        """
        if rows is not None:
            try:
                if rows.count() > 0:
                    first_row = rows.nth(0)
                    # Prefer locator-based panels anchored by the first row to avoid stale ElementHandle scroll targets.
                    for selector in (
                        "div[role='navigation'][aria-label='Lista de conversaciones']",
                        "div[role='navigation'][aria-label='Conversation list']",
                        "div[role='navigation'][aria-label='List of conversations']",
                        "div[role='navigation']",
                        "main",
                        "div[role='main']",
                    ):
                        try:
                            scoped = page.locator(selector, has=first_row)
                            if scoped.count() <= 0:
                                continue
                            panel_loc = scoped.first
                            try:
                                meta = panel_loc.evaluate(
                                    """(el) => ({
                                        tag: String((el && el.tagName) || ""),
                                        role: String((el && el.getAttribute && el.getAttribute("role")) || ""),
                                        ariaLabel: String((el && el.getAttribute && el.getAttribute("aria-label")) || ""),
                                        clientHeight: Number((el && el.clientHeight) || 0),
                                        scrollHeight: Number((el && el.scrollHeight) || 0)
                                    })"""
                                )
                            except Exception:
                                meta = {}
                            if _DM_VERBOSE_PROBES:
                                print(
                                    style_text(
                                        f"[Probe] _get_inbox_panel resolved by has-row selector '{selector}': {meta}",
                                        color=Fore.WHITE,
                                    )
                                )
                            return panel_loc, "selector_has_row", selector, meta or {}
                        except Exception:
                            continue
                    handle = first_row.evaluate_handle(
                        """(el) => {
                            if (!el) return null;
                            const isScrollable = (node) => {
                                if (!node) return false;
                                const style = window.getComputedStyle(node);
                                const overflowY = String((style && style.overflowY) || "").toLowerCase();
                                const allowsScroll = overflowY.includes("auto") || overflowY.includes("scroll") || overflowY.includes("overlay");
                                const hasScrollableContent = Number(node.scrollHeight || 0) - Number(node.clientHeight || 0) > 4;
                                return allowsScroll && hasScrollableContent;
                            };

                            let node = el;
                            for (let i = 0; node && i < 14; i += 1) {
                                if (isScrollable(node)) return node;
                                node = node.parentElement;
                            }

                            node = el;
                            for (let i = 0; node && i < 14; i += 1) {
                                const hasScrollableContent = Number(node.scrollHeight || 0) - Number(node.clientHeight || 0) > 4;
                                if (hasScrollableContent) return node;
                                node = node.parentElement;
                            }

                            return document.scrollingElement || document.documentElement || document.body || null;
                        }"""
                    )
                    resolved = handle.as_element() if handle is not None else None
                    if resolved is not None:
                        try:
                            meta = resolved.evaluate(
                                """(el) => ({
                                    tag: String((el && el.tagName) || ""),
                                    role: String((el && el.getAttribute && el.getAttribute("role")) || ""),
                                    ariaLabel: String((el && el.getAttribute && el.getAttribute("aria-label")) || ""),
                                    clientHeight: Number((el && el.clientHeight) || 0),
                                    scrollHeight: Number((el && el.scrollHeight) || 0)
                                })"""
                            )
                        except Exception:
                            meta = {}
                        if _DM_VERBOSE_PROBES:
                            print(
                                style_text(
                                    f"[Probe] _get_inbox_panel resolved by row: {meta}",
                                    color=Fore.WHITE,
                                )
                            )
                        return resolved, "row_ancestor", "row_scrollable_ancestor", meta or {}
            except Exception:
                pass

        for selector in (
            "div[role='navigation'][aria-label='Lista de conversaciones']",
            "div[role='navigation'][aria-label='Conversation list']",
            "div[role='navigation']",
            "div[role='main']",
            "main",
        ):
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    if _DM_VERBOSE_PROBES:
                        print(style_text(f"[Probe] _get_inbox_panel encontrÃƒÂ³ '{selector}'", color=Fore.WHITE))
                    return loc.first, "selector", selector, {"count": loc.count()}
            except Exception:
                continue
        if _DM_VERBOSE_PROBES:
            print(style_text("[Probe] _get_inbox_panel no encontrÃƒÂ³ nada, usando page", color=Fore.YELLOW))
        return page, "page", "", {"count": 1}

    def _stagnation_limit(self, target: int) -> int:
        target_value = max(1, int(target or 1))
        scaled = _DM_STAGNANT_BASE_LIMIT + int(max(0, target_value - 25) / 60)
        return max(_DM_STAGNANT_BASE_LIMIT, min(_DM_STAGNANT_MAX_LIMIT, scaled))

    def _wait_for_scroll_settle(self, page: Page, *, extra_ms: int = 0) -> None:
        wait_ms = max(50, int(_DM_SCROLL_WAIT_MS + max(0, int(extra_ms))))
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass

    def _panel_scroll_metrics(self, panel) -> dict[str, float]:
        try:
            result = panel.evaluate(
                """(el) => {
                    const node = el || document.scrollingElement || document.documentElement || document.body || null;
                    if (!node) return { top: 0, height: 0, client: 0, max: 0 };
                    const top = Number(node.scrollTop || 0);
                    const height = Number(node.scrollHeight || 0);
                    const client = Number(node.clientHeight || 0);
                    const max = Math.max(0, height - client);
                    return { top, height, client, max };
                }"""
            )
        except Exception:
            try:
                page = self._ensure_page()
                result = page.evaluate(
                    """() => {
                        const node = document.scrollingElement || document.documentElement || document.body || null;
                        if (!node) return { top: 0, height: 0, client: 0, max: 0 };
                        const top = Number(node.scrollTop || 0);
                        const height = Number(node.scrollHeight || 0);
                        const client = Number(node.clientHeight || 0);
                        const max = Math.max(0, height - client);
                        return { top, height, client, max };
                    }"""
                )
            except Exception:
                result = {}
        top = float((result or {}).get("top", 0))
        height = float((result or {}).get("height", 0))
        client = float((result or {}).get("client", 0))
        max_scroll = float((result or {}).get("max", max(0.0, height - client)))
        return {"top": top, "height": height, "client": client, "max": max_scroll}

    def _scroll_panel_to_top(self, panel) -> None:
        try:
            panel.evaluate(
                """(el) => {
                    const node = el || document.scrollingElement || document.documentElement || document.body || null;
                    if (!node) return;
                    try { node.scrollTop = 0; } catch (_) {}
                }"""
            )
        except Exception:
            try:
                page = self._ensure_page()
                fallback_panel, _method, _selector, _meta = self._get_inbox_panel(page)
                fallback_panel.evaluate(
                    """(el) => {
                        const node = el || document.scrollingElement || document.documentElement || document.body || null;
                        if (!node) return;
                        try { node.scrollTop = 0; } catch (_) {}
                    }"""
                )
            except Exception:
                return
        self._wait_for_scroll_settle(self._ensure_page(), extra_ms=20)

    def _scroll_panel_down(self, panel, *, max_attempts: Optional[int] = None) -> bool:
        page = self._ensure_page()
        attempts_value = _DM_SCROLL_ATTEMPTS if max_attempts is None else max_attempts
        attempts = max(1, int(attempts_value))

        for attempt in range(attempts):
            before = self._panel_scroll_metrics(panel)
            before_top = float(before.get("top", 0))
            before_height = float(before.get("height", 0))
            before_max = float(before.get("max", 0))

            try:
                factor = 0.90 + min(0.28, attempt * 0.07)
                scroll_script = """(el) => {
                    const factor = __FACTOR__;
                    const node = el || document.scrollingElement || document.documentElement || document.body || null;
                    if (!node) return { before: 0, after: 0, max: 0 };
                    const before = Number(node.scrollTop || 0);
                    const max = Math.max(0, Number((node.scrollHeight || 0) - (node.clientHeight || 0)));
                    const step = Math.max(350, Math.floor(Number(node.clientHeight || 600) * Number(factor || 1)));
                    const next = Math.min(max, before + step);
                    try { node.scrollTop = next; } catch (_) {}
                    const after = Number(node.scrollTop || 0);
                    return { before, after, max };
                }"""
                panel.evaluate(
                    scroll_script.replace("__FACTOR__", f"{factor:.6f}"),
                )
            except Exception:
                try:
                    fallback_panel, _method, _selector, _meta = self._get_inbox_panel(page)
                    panel = fallback_panel
                    panel.evaluate(
                        scroll_script.replace("__FACTOR__", f"{factor:.6f}"),
                    )
                except Exception:
                    pass

            if attempt > 0:
                try:
                    box = panel.bounding_box()
                except Exception:
                    box = None
                if box:
                    x = float(box.get("x") or 0.0) + max(12.0, min(float(box.get("width") or 0.0) - 12.0, 44.0))
                    y = float(box.get("y") or 0.0) + max(12.0, min(float(box.get("height") or 0.0) - 12.0, 44.0))
                    try:
                        page.mouse.move(x, y)
                    except Exception:
                        pass
                try:
                    page.mouse.wheel(0, 1200 + (attempt * 350))
                except Exception:
                    pass
                if attempt >= 2:
                    try:
                        panel.focus()
                    except Exception:
                        pass
                    try:
                        page.keyboard.press("PageDown")
                    except Exception:
                        pass
                    if attempt == attempts - 1:
                        try:
                            page.keyboard.press("End")
                        except Exception:
                            pass

            self._wait_for_scroll_settle(page, extra_ms=30 if attempt > 0 else 0)

            after = self._panel_scroll_metrics(panel)
            after_top = float(after.get("top", 0))
            after_height = float(after.get("height", 0))
            after_max = float(after.get("max", 0))

            top_moved = after_top > before_top + 1
            height_grew = after_height > before_height + 2
            if top_moved or height_grew:
                return True

            max_ref = max(after_max, before_max)
            if after_top + 1 < max_ref:
                continue

        final = self._panel_scroll_metrics(panel)
        return float(final.get("top", 0)) + 1 < float(final.get("max", 0))

    def _row_is_valid(self, row, *, selector: str | None = None, fast: bool = False) -> bool:
        """
        ValidaciÃƒÂ³n mÃƒÂ­nima pre-click.
        El filtrado real ocurre POST-CLICK en _open_thread.
        """
        try:
            lines = self._row_lines(row)
            if not lines:
                return False

            # Filtros de exclusiÃƒÂ³n de UI bÃƒÂ¡sica (incluye Notas)
            first_line = lines[0].lower()
            if first_line in {
                "primary",
                "general",
                "request",
                "buscar",
                "search",
                "enviar mensaje",
                "nuevo mensaje",
                "new message",
                "solicitudes",
                "principal",
                "mensajes",
                "chats",
                "direct",
            }:
                return False
            if self._is_non_thread_control_row(lines):
                return False

            if fast:
                # Fast-path para escaneo: evita consultas DOM pesadas por fila.
                compact = " ".join((line or "").strip().lower() for line in lines[:6] if line)
                if compact:
                    if compact in {"notes", "notas", "nota"}:
                        return False
                    for token in _NOTE_PHRASES:
                        if token in compact:
                            return False
                return True

            note_reason = self._note_reason(row)
            if note_reason:
                logger.info(
                    "PlaywrightDM row_discard reason=note selector=%s token=%s first_line=%s",
                    selector or "-",
                    note_reason,
                    self._row_preview(row),
                )
                return False

            # Descartar botones internos (avatar/nota) por tamaÃƒÂ±o.
            if not self._row_has_thread_dimensions(row):
                logger.info(
                    "PlaywrightDM row_discard reason=small_button selector=%s first_line=%s",
                    selector or "-",
                    self._row_preview(row),
                )
                return False

            # Exigir marcador de thread real para evitar abrir burbujas de nota.
            if not self._row_has_thread_marker(row):
                logger.info(
                    "PlaywrightDM row_discard reason=no_thread_marker selector=%s first_line=%s",
                    selector or "-",
                    self._row_preview(row),
                )
                return False

            return True
        except Exception:
            return False

    def _is_non_thread_control_row(self, lines: List[str]) -> bool:
        if not lines:
            return True
        text = " ".join((line or "").strip().lower() for line in lines if line)
        if not text:
            return True
        control_tokens = (
            "nuevo mensaje",
            "new message",
            "icono de comilla angular",
            "desde el corazÃƒÂ³n",
            "solicitudes",
            "requests",
        )
        return any(token in text for token in control_tokens)

    def _note_reason(self, row) -> str:
        candidates: List[str] = []
        try:
            lines = self._row_lines(row)
        except Exception:
            lines = []
        if lines:
            for line in lines[:6]:
                candidates.append((line or "").lower())
        for attr_name in ("aria-label", "data-testid"):
            try:
                value = (row.get_attribute(attr_name) or "").strip().lower()
            except Exception:
                value = ""
            if value:
                candidates.append(value)
        try:
            parent = row.locator("xpath=ancestor-or-self::*[@aria-label][1]").first
            parent_aria = (parent.get_attribute("aria-label") or "").strip().lower()
        except Exception:
            parent_aria = ""
        if parent_aria:
            candidates.append(parent_aria)

        for text in candidates:
            clean = re.sub(r"\s+", " ", text)
            if clean in {"notes", "notas", "nota"}:
                return clean
            for token in _NOTE_PHRASES:
                if token in clean:
                    return token

        try:
            if row.locator(
                "xpath=ancestor-or-self::*[contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'note') or contains(translate(@aria-label,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'nota')]"
            ).count() > 0:
                return "aria_ancestor_note"
        except Exception:
            pass
        return ""

    def _row_preview(self, row) -> str:
        try:
            lines = self._row_lines(row)
        except Exception:
            lines = []
        if lines:
            return lines[0][:120]
        return ""

    def _row_has_thread_dimensions(self, row) -> bool:
        try:
            box = row.bounding_box()
        except Exception:
            box = None
        if not box:
            return False
        width = float(box.get("width") or 0.0)
        height = float(box.get("height") or 0.0)
        return width >= 220.0 and height >= 44.0

    def _row_has_thread_marker(self, row) -> bool:
        """
        Heur?stica para distinguir threads de notas.
        """
        try:
            href = row.get_attribute("href") or ""
        except Exception:
            href = ""
        if "/direct/t/" in href:
            return True

        try:
            if row.locator("a[href*='/direct/t/']").count() > 0:
                return True
        except Exception:
            pass

        # Los threads suelen mostrar tiempo (p.ej. <time>)
        try:
            if row.locator("time").count() > 0:
                return True
        except Exception:
            pass
        try:
            if row.locator("abbr[aria-label]").count() > 0:
                return True
        except Exception:
            pass
        try:
            if row.locator("abbr").count() > 0:
                return True
        except Exception:
            pass

        # Unread badge tambi?n indica thread real
        try:
            if _thread_unread_count(row) > 0:
                return True
        except Exception:
            pass

        # Modern inbox rows may not expose anchors/time tags; detect timestamp-like text.
        try:
            full = " ".join(self._row_lines(row)).lower()
        except Exception:
            full = ""
        if full:
            if re.search(r"\b\d+\s*(s|min|h|d|sem|sec|hr|hrs)\b", full):
                return True
            if any(token in full for token in (" ahora", " now", " ayer", " yesterday")):
                return True
            if " Ã‚Â· " in full and ("tÃƒÂº:" in full or "tu:" in full or "you:" in full):
                return True

        return False


    def _count_rows_valid(self, rows) -> int:
        try:
            total = rows.count()
        except Exception:
            total = 0
        valid = 0
        for idx in range(total):
            try:
                if self._row_is_valid(rows.nth(idx)):
                    valid += 1
            except Exception:
                continue
        return valid

    # LEGACY: FunciÃƒÂ³n deshabilitada - ya no se usa (reemplazada por click-first scan)
    # def _list_threads_from_rows(...) -> List[ThreadLike]


    def get_messages(self, thread: ThreadLike, amount: int = 20, *, log: bool = True) -> List[MessageLike]:
        page = self._ensure_page()

        # [CLICK-FIRST] Abrir y verificar thread post-click
        try:
            is_valid = self._open_thread(thread)
            if not is_valid:
                if log:
                    logger.info(
                        "[TRACE_MSG_DIAG] thread=%s open_thread_validated=%s final_collected_len=%d return_pairs_ordered=%s",
                        thread.id,
                        is_valid,
                        0,
                        [],
                    )
                return []
        except Exception as e:
            if log:
                logger.warning("PlaywrightDM error al abrir thread %s: %s", thread.id, e)
                logger.info(
                    "[TRACE_MSG_DIAG] thread=%s open_thread_exception=%r final_collected_len=%d return_pairs_ordered=%s",
                    thread.id,
                    e,
                    0,
                    [],
                )
            return []

        # Esperar a que los mensajes se hidraten
        try:
            msg_selector = _MESSAGE_NODE_SELECTORS[0]
            page.wait_for_selector(
                f"main {msg_selector}, div[role='main'] {msg_selector}",
                timeout=_DM_MESSAGE_HYDRATION_TIMEOUT_MS,
            )
        except Exception:
            pass

        collected: List[MessageLike] = []
        api_messages = self._get_api_messages_for_thread(
            thread,
            page,
            timeout_ms=_DM_API_WAIT_TIMEOUT_MS,
        )
        if not api_messages:
            if log:
                logger.info(
                    "PlaywrightDM mensajes vacios thread=%s peer=%s account=@%s",
                    thread.id,
                    _thread_peer_id(thread, self.user_id),
                    self.username,
                )
                logger.info(
                    "[TRACE_MSG_DIAG] thread=%s final_collected_len=%d return_pairs_ordered=%s",
                    thread.id,
                    0,
                    [],
                )
            return []

        for idx, api_msg in enumerate(api_messages[: max(1, amount)]):
            timestamp = api_msg.timestamp
            if timestamp is None:
                logger.warning(
                    "timestamp_missing_from_api thread_id=%s item_id=%s sender_id=%s account=@%s source=get_messages",
                    api_msg.thread_id,
                    api_msg.item_id,
                    api_msg.sender_id,
                    self.username,
                )
                continue
            direction = "outbound" if api_msg.direction == "outbound" else "inbound"
            if direction == "outbound":
                user_id = self.user_id
            else:
                user_id = str(api_msg.sender_id or "").strip() or _thread_peer_id(thread, self.user_id)
            msg_id = str(api_msg.item_id or "").strip()
            if not msg_id:
                continue
            text = str(api_msg.text or "")
            if log:
                preview = text.replace("\n", " ").replace("\r", " ")[:50]
                logger.info(
                    "[TRACE_MSG_DIAG] thread=%s parsed_idx=%d text50=%r timestamp=%s user_id=%s direction=%s",
                    thread.id,
                    idx,
                    preview,
                    timestamp,
                    user_id,
                    direction,
                )
            collected.append(
                MessageLike(
                    id=msg_id,
                    user_id=user_id,
                    text=text,
                    timestamp=timestamp,
                    direction=direction,
                )
            )

        if log:
            pre_sort_pairs = [(m.user_id, m.timestamp) for m in collected]
            logger.info(
                "[TRACE_MSG_DIAG] thread=%s before_sort_count=%d pairs=%s",
                thread.id,
                len(collected),
                pre_sort_pairs,
            )
        collected.sort(key=lambda m: (m.timestamp is not None, m.timestamp or 0), reverse=True)
        if log:
            post_sort_pairs = [(m.user_id, m.timestamp) for m in collected]
            logger.info(
                "[TRACE_MSG_DIAG] thread=%s after_sort_count=%d pairs=%s",
                thread.id,
                len(collected),
                post_sort_pairs,
            )

        last_outbound = next((m for m in collected if m.user_id == self.user_id), None)
        last_inbound = next((m for m in collected if m.user_id != self.user_id), None)
        if log:
            logger.info(
                "[TRACE_MSG_DIAG] thread=%s final_collected_len=%d",
                thread.id,
                len(collected),
            )
            logger.info(
                "PlaywrightDM mensajes_leidos thread=%s peer=%s count=%d last_in_ts=%s last_out_ts=%s",
                thread.id,
                _thread_peer_id(thread, self.user_id),
                len(collected),
                _fmt_ts(last_inbound.timestamp if last_inbound else None),
                _fmt_ts(last_outbound.timestamp if last_outbound else None),
            )
            logger.info(
                "[TRACE_MSG_DIAG] thread=%s return_pairs_ordered=%s",
                thread.id,
                [(m.user_id, m.timestamp) for m in collected],
            )
        return collected

    def send_message(self, thread: ThreadLike, text: str) -> Optional[str]:
        page = self._ensure_page()
        self._open_thread(thread)

        composer = self._find_composer(page)
        if composer is None:
            logger.warning("PlaywrightDM sin composer thread=%s @%s", thread.id, self.username)
            return None

        try:
            composer.click()
            composer.fill(text)
            composer.press("Enter")
        except Exception as e:
            logger.warning("PlaywrightDM no pudo completar acciones de envÃƒÂ­o thread=%s @%s", thread.id, self.username)
            return None

        message_id = self._verify_sent(thread, text)
        if message_id:
            logger.info("PlaywrightDM envio_ok thread=%s msg_id=%s", thread.id, message_id)
        else:
            logger.warning("PlaywrightDM envio_no_verificado thread=%s @%s", thread.id, self.username)
        return message_id

    def _ensure_page(self) -> Page:
        if self._page is not None:
            self._register_response_listener()
            return self._page

        if _DM_VERBOSE_PROBES:
            print(style_text(f"[PlaywrightDM] Iniciando navegador para @{self.username}...", color=Fore.WHITE))

        storage_state = self.storage_state_path(self.username)
        if not storage_state.exists():
            raise RuntimeError(f"No hay sesion Playwright guardada para @{self.username}.")

        self._playwright = sync_playwright().start()
        launch_kwargs = {
            "headless": self.headless,
            "args": list(DEFAULT_ARGS),
        }
        if self.slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = self.slow_mo_ms
        executable = resolve_playwright_executable(headless=self.headless)
        if executable:
            launch_kwargs["executable_path"] = str(executable)
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        proxy_payload = _proxy_from_account(self.account)
        self._context = self._browser.new_context(
            storage_state=str(storage_state),
            proxy=proxy_payload or None,
            viewport=DEFAULT_VIEWPORT,
            user_agent=DEFAULT_USER_AGENT,
            locale=DEFAULT_LOCALE,
            timezone_id=DEFAULT_TIMEZONE,
            permissions=[],
            accept_downloads=False,
        )
        self._context.set_default_timeout(30_000)
        self._page = self._context.new_page()
        try:
            self._page.set_default_navigation_timeout(45_000)
        except Exception:
            pass
        self._register_response_listener()
        return self._page

    def _register_response_listener(self) -> None:
        page = self._page
        if page is None or self._response_listener_registered:
            return
        try:
            page.on("response", self._handle_response)
            self._response_listener_registered = True
        except Exception as exc:
            logger.warning(
                "PlaywrightDM response_listener_error account=@%s error=%s",
                self.username,
                exc,
            )

    def _handle_response(self, response) -> None:
        try:
            url = str(getattr(response, "url", "") or "")
        except Exception:
            return
        if not _is_message_api_url(url):
            return
        try:
            request = getattr(response, "request", None)
            resource_type = str(getattr(request, "resource_type", "") or "").lower()
        except Exception:
            resource_type = ""
        if resource_type and resource_type not in {"xhr", "fetch"}:
            return
        try:
            payload = response.json()
        except Exception:
            return
        self._ingest_api_payload(payload, source_url=url)

    def _ingest_api_payload(self, payload: Any, *, source_url: str = "") -> tuple[int, int]:
        parsed, missing_timestamp = _extract_api_messages_from_payload(
            payload,
            self_user_id=self.user_id,
        )
        added = 0
        for item in parsed:
            if self._store_api_message(item):
                added += 1
        for missing in missing_timestamp:
            logger.warning(
                "timestamp_missing_from_api thread_id=%s item_id=%s sender_id=%s account=@%s source=%s",
                missing.get("thread_id") or "",
                missing.get("item_id") or "",
                missing.get("sender_id") or "",
                self.username,
                source_url,
            )
        return added, len(missing_timestamp)

    def _store_api_message(self, message: _APIMessageRecord) -> bool:
        thread_id = str(message.thread_id or "").strip()
        item_id = str(message.item_id or "").strip()
        if not thread_id or not item_id:
            return False

        bucket = self._api_messages_by_thread.setdefault(thread_id, {})
        existing = bucket.get(item_id)
        if existing is not None:
            # Conservar el mejor contenido si la misma key llega mÃƒÂºltiples veces.
            merged = _APIMessageRecord(
                thread_id=thread_id,
                sender_id=message.sender_id or existing.sender_id,
                timestamp=max(existing.timestamp, message.timestamp),
                item_id=item_id,
                direction=message.direction or existing.direction,
                text=message.text or existing.text,
            )
            if merged == existing:
                self._api_thread_last_seen[thread_id] = time.time()
                return False
            bucket[item_id] = merged
            self._api_thread_last_seen[thread_id] = time.time()
            self._trim_api_cache()
            return True

        bucket[item_id] = message
        self._api_thread_last_seen[thread_id] = time.time()
        self._trim_api_cache()
        return True

    def _trim_api_cache(self) -> None:
        for thread_id, bucket in list(self._api_messages_by_thread.items()):
            overflow = len(bucket) - _DM_API_CACHE_MAX_PER_THREAD
            if overflow <= 0:
                continue
            ordered = sorted(
                bucket.values(),
                key=lambda item: (item.timestamp, item.item_id),
            )
            for old in ordered[:overflow]:
                bucket.pop(old.item_id, None)
        overflow_threads = len(self._api_messages_by_thread) - _DM_API_CACHE_MAX_THREADS
        if overflow_threads <= 0:
            return
        stale_threads = sorted(
            self._api_thread_last_seen.items(),
            key=lambda kv: kv[1],
        )
        for thread_id, _ in stale_threads[:overflow_threads]:
            self._api_messages_by_thread.pop(thread_id, None)
            self._api_thread_last_seen.pop(thread_id, None)

    def _thread_cache_keys(self, thread: ThreadLike, page: Page) -> list[str]:
        keys: list[str] = []
        for candidate in (
            getattr(thread, "id", None),
            getattr(thread, "pk", None),
            self._current_thread_id,
            _extract_thread_id(getattr(thread, "link", "") or ""),
            _extract_thread_id(getattr(page, "url", "") or ""),
        ):
            value = str(candidate or "").strip()
            if value and value not in keys:
                keys.append(value)
        return keys

    def _expected_thread_ids(
        self,
        thread: ThreadLike,
        *,
        post_thread_id: str = "",
        click_href: str = "",
    ) -> list[str]:
        ids: list[str] = []
        for candidate in (
            getattr(thread, "id", None),
            getattr(thread, "pk", None),
            self._current_thread_id,
            post_thread_id,
            _extract_thread_id(click_href),
        ):
            value = str(candidate or "").strip()
            if value and value not in ids:
                ids.append(value)
        return ids

    def _snapshot_api_counts(self, thread_ids: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for thread_id in thread_ids:
            key = str(thread_id or "").strip()
            if not key:
                continue
            counts[key] = len(self._api_messages_by_thread.get(key, {}))
        return counts

    def _thread_has_new_api_messages(
        self,
        thread_ids: list[str],
        *,
        baseline_counts: dict[str, int],
    ) -> tuple[bool, str]:
        for thread_id in thread_ids:
            key = str(thread_id or "").strip()
            if not key:
                continue
            current = len(self._api_messages_by_thread.get(key, {}))
            baseline = int(baseline_counts.get(key, 0))
            if current > baseline and current > 0:
                return True, key
        return False, ""

    def _message_panel_visible(self, page: Page) -> bool:
        try:
            nodes = self._collect_message_nodes(page)
            total = nodes.count()
        except Exception:
            return False
        if total <= 0:
            return False
        scan_limit = min(total, 20)
        for idx in range(scan_limit):
            try:
                node = nodes.nth(idx)
                if node.locator("xpath=ancestor::*[@role='navigation']").count() > 0:
                    continue
                return True
            except Exception:
                continue
        return False

    def _wait_for_visual_thread_sync(self, page: Page, *, timeout_ms: int) -> tuple[bool, dict[str, object]]:
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        stable_hits = 0
        last_state: dict[str, object] = {
            "post_url": page.url or "",
            "post_thread_id": _extract_thread_id(page.url or ""),
            "composer_visible": False,
            "message_panel_visible": False,
        }
        while time.time() < deadline:
            post_url = page.url or ""
            post_thread_id = _extract_thread_id(post_url)
            composer = self._find_composer(page)
            composer_visible = False
            if composer is not None:
                try:
                    composer_visible = bool(composer.is_visible())
                except Exception:
                    composer_visible = False

            message_panel_visible = self._message_panel_visible(page)
            last_state = {
                "post_url": post_url,
                "post_thread_id": post_thread_id,
                "composer_visible": composer_visible,
                "message_panel_visible": message_panel_visible,
            }
            if composer_visible and message_panel_visible:
                stable_hits += 1
                if stable_hits >= 2:
                    return True, last_state
            else:
                stable_hits = 0
            try:
                page.wait_for_timeout(120)
            except Exception:
                break
        return False, last_state

    def _wait_for_thread_network_sync(
        self,
        page: Page,
        *,
        expected_thread_ids: list[str],
        baseline_counts: dict[str, int],
        timeout_ms: int,
    ) -> tuple[bool, str]:
        expected = [tid for tid in expected_thread_ids if str(tid or "").strip()]
        if not expected:
            return False, ""

        has_cached, cached_thread_id = self._thread_has_new_api_messages(
            expected,
            baseline_counts=baseline_counts,
        )
        if has_cached:
            return True, cached_thread_id

        matched_payload: Any = None
        matched_url = ""
        matched_thread_id = ""
        timeout_value = max(200, int(timeout_ms))

        def _response_predicate(response) -> bool:
            nonlocal matched_payload, matched_url, matched_thread_id
            try:
                url = str(getattr(response, "url", "") or "")
            except Exception:
                return False
            if not _is_message_api_url(url):
                return False
            try:
                request = getattr(response, "request", None)
                resource_type = str(getattr(request, "resource_type", "") or "").lower()
            except Exception:
                resource_type = ""
            if resource_type and resource_type not in {"xhr", "fetch"}:
                return False
            try:
                payload = response.json()
            except Exception:
                return False
            payload_thread_ids = _payload_thread_ids(payload, self_user_id=self.user_id)
            for thread_id in expected:
                if thread_id in payload_thread_ids:
                    matched_payload = payload
                    matched_url = url
                    matched_thread_id = thread_id
                    return True
            return False

        try:
            with page.expect_response(_response_predicate, timeout=timeout_value):
                pass
        except Exception:
            pass

        if matched_payload is not None:
            self._ingest_api_payload(matched_payload, source_url=matched_url)
            has_cached, cached_thread_id = self._thread_has_new_api_messages(
                expected,
                baseline_counts=baseline_counts,
            )
            if has_cached:
                return True, cached_thread_id or matched_thread_id

        deadline = time.time() + max(0.0, float(timeout_value) / 1000.0)
        while time.time() < deadline:
            has_cached, cached_thread_id = self._thread_has_new_api_messages(
                expected,
                baseline_counts=baseline_counts,
            )
            if has_cached:
                return True, cached_thread_id
            try:
                page.wait_for_timeout(80)
            except Exception:
                break
        return False, matched_thread_id

    def _get_api_messages_for_thread(
        self,
        thread: ThreadLike,
        page: Page,
        *,
        timeout_ms: int,
    ) -> list[_APIMessageRecord]:
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        merged: dict[str, _APIMessageRecord] = {}

        while time.time() < deadline:
            keys = self._thread_cache_keys(thread, page)
            merged = {}
            for key in keys:
                for item_id, msg in self._api_messages_by_thread.get(key, {}).items():
                    prev = merged.get(item_id)
                    if prev is None or msg.timestamp >= prev.timestamp:
                        merged[item_id] = msg
            if merged:
                break
            try:
                page.wait_for_timeout(80)
            except Exception:
                break

        if not merged:
            keys = self._thread_cache_keys(thread, page)
            for key in keys:
                for item_id, msg in self._api_messages_by_thread.get(key, {}).items():
                    prev = merged.get(item_id)
                    if prev is None or msg.timestamp >= prev.timestamp:
                        merged[item_id] = msg

        ordered = sorted(
            merged.values(),
            key=lambda item: (item.timestamp, item.item_id),
            reverse=True,
        )
        return ordered

    def _open_inbox(self, force_reload: bool = False) -> None:
        page = self._ensure_page()
        if not force_reload and INBOX_URL in (page.url or ""):
            # Ya estamos en el inbox, no recargar
            return

        try:
            page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError:
            pass
        self._dismiss_overlays(page)
        if not self._account_status_checked:
            status = detect_account_status_sync(page)
            self._account_status_checked = True
            log_account_status(self.username, status)
            try:
                import health_store

                health_store.update_from_playwright_status(self.username, status, reason=status)
            except Exception:
                pass
            if status != "alive":
                logger.warning(
                    "PlaywrightDM account_status_stop account=@%s status=%s url=%s",
                    self.username,
                    status,
                    page.url,
                )
                self.close()
                raise RuntimeError(
                    f"Cuenta @{self.username} no disponible para operar. Estado detectado: {status}."
                )
        self._assert_logged_in(page)

        row_selectors = tuple(self._row_selector_candidates())
        chosen = ""
        for selector in row_selectors:
            try:
                if page.locator(selector).count():
                    chosen = selector
                    break
            except Exception:
                continue
        rows_ready = bool(chosen)

        found_container = None
        # Lista ampliada de selectores de contenedor para mayor robustez
        container_candidates = (
            "div[role='main']",
            "main",
            "section",
            "div[aria-label='Direct']",
            "div[aria-label='Mensajes']",
            "div[aria-label='Chats']",
            "div[role='navigation']",
            "a[href^='/direct/t/']"
        )

        for selector in container_candidates:
            try:
                if page.locator(selector).count() > 0:
                    found_container = selector
                    break
            except Exception:
                continue

        if not found_container and not rows_ready:
            # Si ninguno es visible de inmediato, esperar brevemente al mÃƒÂ¡s probable
            try:
                page.wait_for_selector("div[role='main'], main, div[role='navigation']", timeout=10_000)
                # Re-chequear
                for selector in container_candidates:
                    if page.locator(selector).count() > 0:
                        found_container = selector
                        break
            except Exception:
                pass

        if _DM_VERBOSE_PROBES:
            print(style_text(f"[Probe] Inbox container: {found_container}", color=Fore.WHITE))
        if not rows_ready:
            for search_selector in ("input[placeholder='Buscar']", "input[placeholder='Search']", "input[name='queryBox']"):
                try:
                    page.wait_for_selector(search_selector, timeout=15_000)
                    break
                except Exception:
                    continue
        if not rows_ready:
            deadline = time.time() + 12.0
            while time.time() < deadline and not rows_ready:
                for selector in row_selectors:
                    try:
                        if page.locator(selector).count():
                            chosen = selector
                            rows_ready = True
                            break
                    except Exception:
                        continue
                if not rows_ready:
                    try:
                        page.wait_for_timeout(500)
                    except Exception:
                        break
        if not rows_ready:
            logger.warning("PlaywrightDM inbox rows not ready for @%s", self.username)
            for selector in row_selectors:
                try:
                    count = page.locator(selector).count()
                except Exception:
                    count = 0
                logger.info("PlaywrightDM row_selector_count selector=%s count=%s", selector, count)
        logger.info("PlaywrightDM inbox_abierto account=@%s", self.username)
        time.sleep(1)

    def _dismiss_overlays(self, page: Page) -> None:
        """
        Cierra overlays de forma robusta:
        1. Detecta div[role='dialog'] y busca botones de cierre dentro del dialog
        2. Si hay inputs de login y no se puede cerrar ? RuntimeError
        3. Intenta Escape + botones de cierre
        4. Re-chequea login inputs al final (no depende del chequeo inicial)
        """
        # Intentar detectar y cerrar dialogs primero
        try:
            dialogs = page.locator("div[role='dialog']")
            dialog_count = dialogs.count()
            if dialog_count > 0:
                logger.info("PlaywrightDM overlay_detected count=%d", dialog_count)
                for idx in range(dialog_count):
                    try:
                        dialog = dialogs.nth(idx)
                        # Buscar botÃƒÂ³n de cierre dentro del dialog
                        close_selectors = [
                            "button[aria-label='Cerrar']",
                            "button[aria-label='Close']",
                            "svg[aria-label='Close']",
                        ]
                        closed = False
                        for sel in close_selectors:
                            try:
                                close_btn = dialog.locator(sel)
                                if close_btn.count() > 0:
                                    if sel.startswith("svg"):
                                        parent = close_btn.first.locator("xpath=ancestor::button[1]")
                                        if parent.count():
                                            parent.first.click()
                                            closed = True
                                            break
                                    else:
                                        close_btn.first.click()
                                        closed = True
                                        break
                            except Exception:
                                continue
                        if closed:
                            logger.info("PlaywrightDM overlay_closed idx=%d", idx)
                    except Exception:
                        continue
        except Exception:
            pass
        
        # Escape general
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        
        # Buscar botones de cierre globales
        for selector in (
            "button[aria-label='Cerrar']",
            "button[aria-label='Close']",
            "svg[aria-label='Close']",
        ):
            try:
                loc = page.locator(selector)
                if loc.count():
                    if selector.startswith("svg"):
                        try:
                            parent = loc.first.locator("xpath=ancestor::button[1]")
                            if parent.count():
                                parent.first.click()
                                logger.info("PlaywrightDM overlay_closed method=global_svg")
                                break
                        except Exception:
                            pass
                    else:
                        loc.first.click()
                        logger.info("PlaywrightDM overlay_closed method=global_button")
                        break
            except Exception:
                continue
        
        # "Ahora no" / "Not now"
        for label in ("Ahora no", "Not now"):
            try:
                btn = page.locator(f"button:has-text('{label}')")
                if btn.count():
                    btn.first.click()
                    logger.info("PlaywrightDM overlay_closed method=not_now label=%s", label)
                    break
            except Exception:
                continue
        
        # CRÃƒÂTICO: Siempre re-chequear login inputs al final (no depender del chequeo inicial)
        # porque pueden aparecer despuÃƒÂ©s de cerrar otros overlays
        try:
            has_login_after_close = page.locator("input[name='username'], input[name='password']").count() > 0
            if has_login_after_close:
                raise RuntimeError(
                    f"Overlay de login detectado y no se pudo cerrar para @{self.username}. "
                    "SesiÃƒÂ³n posiblemente invÃƒÂ¡lida."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    def return_to_inbox(self) -> None:
        """
        Vuelve a la vista del inbox sin recargar la pÃƒÂ¡gina si es posible.
        """
        page = self._ensure_page()
        current_url = page.url or ""
        if INBOX_URL in current_url and not re.search(r"/direct/t/", current_url):
            return

        # Camino rapido: si estamos en /direct/t/, intentar link al inbox.
        if "/direct/t/" in current_url:
            try:
                inbox_link = page.locator("a[href='/direct/inbox/'], a[href*='/direct/inbox/']").first
                if inbox_link.count() > 0:
                    inbox_link.click(timeout=max(400, _DM_RETURN_INBOX_TIMEOUT_MS // 2))
                    try:
                        page.wait_for_url(re.compile(r".*/direct/inbox/.*"), timeout=_DM_RETURN_INBOX_TIMEOUT_MS)
                        return
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            # Fallback rapido sin bloquear demasiado.
            page.go_back(wait_until="commit", timeout=_DM_RETURN_INBOX_TIMEOUT_MS)
        except Exception:
            self._open_inbox()

    def _open_thread(self, thread: ThreadLike) -> bool:
        """
        [CLICK-FIRST] Intenta abrir un thread y valida post-click.
        Retorna True si el thread es un DM real (/direct/t/ + composer visible).
        """
        page = self._ensure_page()

        # 1. Asegurar workspace Direct sin forzar "back" en cada thread.
        current_url = page.url or ""
        if not self._is_in_direct_workspace(page):
            self._open_inbox()
        elif "/direct/t/" in current_url and not self._has_thread_rows_visible(page):
            self.return_to_inbox()

        # 2. Intentar clickear por indice usando el selector cacheado del scan
        # source_index path
        if thread.source_index != -1:
            meta = self._thread_cache_meta.get(thread.id, {})
            row_selector = meta.get("selector") or THREAD_ROW_SELECTOR
            try:
                rows = page.locator(row_selector)
                row_count = rows.count()
                if row_count > thread.source_index:
                    row = rows.nth(thread.source_index)
                    row_preview = self._row_preview(row)
                    try:
                        row_valid = self._row_is_valid(row, selector=row_selector, fast=True)
                    except TypeError:
                        row_valid = self._row_is_valid(row, selector=row_selector)
                    if not row_valid:
                        logger.error(
                            "PlaywrightDM open_thread_invalid_row account=@%s thread_id=%s selector=%s idx=%s row=%s",
                            self.username,
                            thread.id,
                            row_selector,
                            thread.source_index,
                            row_preview,
                        )
                    else:
                        pre_url = page.url or ""
                        baseline_counts = self._snapshot_api_counts(
                            self._expected_thread_ids(thread, click_href="")
                        )
                        clicked, click_href = self._click_row_target(
                            row,
                            selector=row_selector,
                            idx=thread.source_index,
                        )
                        if clicked and self._validate_open_state(
                            thread,
                            pre_url=pre_url,
                            selector=row_selector,
                            idx=thread.source_index,
                            row_preview=row_preview,
                            click_href=click_href,
                            baseline_counts=baseline_counts,
                        ):
                            return True
                        if not self._is_in_direct_workspace(page):
                            self.return_to_inbox()
                else:
                    logger.error(
                        "PlaywrightDM open_thread_missing_row account=@%s thread_id=%s selector=%s idx=%s row_count=%s",
                        self.username,
                        thread.id,
                        row_selector,
                        thread.source_index,
                        row_count,
                    )
            except Exception as exc:
                logger.error(
                    "PlaywrightDM open_thread_click_error account=@%s thread_id=%s selector=%s idx=%s error=%s",
                    self.username,
                    thread.id,
                    row_selector,
                    thread.source_index,
                    exc,
                )
        if self._open_thread_by_cache(thread):
            return True

        logger.error(
            "PlaywrightDM open_thread_failed account=@%s thread_id=%s title=%s",
            self.username,
            thread.id,
            thread.title,
        )
        return False

    def _click_row_target(self, row, *, selector: str, idx: int) -> tuple[bool, str]:
        click_target = row
        click_href = ""
        page = self._ensure_page()
        self._dismiss_transient_overlay(page)
        try:
            href = (row.get_attribute("href") or "").strip()
        except Exception:
            href = ""
        if "/direct/t/" in href:
            click_href = _normalize_direct_link(href)
        else:
            try:
                anchor = row.locator("a[href*='/direct/t/']").first
                if anchor.count() > 0:
                    click_target = anchor
                    href = (anchor.get_attribute("href") or "").strip()
                    if "/direct/t/" in href:
                        click_href = _normalize_direct_link(href)
            except Exception:
                pass

        try:
            click_target.scroll_into_view_if_needed()
        except Exception:
            pass

        try:
            box = None
            try:
                box = click_target.bounding_box()
            except Exception:
                box = None
            if box:
                width = float(box.get("width") or 0.0)
                height = float(box.get("height") or 0.0)
                if width > 0 and height > 0:
                    safe_x = max(8.0, min(width - 8.0, width * 0.82))
                    safe_y = max(8.0, min(height - 8.0, height * 0.5))
                    click_target.click(timeout=3000, position={"x": safe_x, "y": safe_y})
                    return True, click_href
            click_target.click(timeout=3000)
            return True, click_href
        except Exception:
            try:
                click_target.click(timeout=3000, force=True)
                return True, click_href
            except Exception as exc:
                logger.error(
                    "PlaywrightDM click_failed selector=%s idx=%s error=%s",
                    selector,
                    idx,
                    exc,
                )
                return False, click_href

    def _dismiss_transient_overlay(self, page: Page) -> None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        close_selectors = (
            "button[aria-label='Cerrar']",
            "button[aria-label='Close']",
            "svg[aria-label='Close']",
        )
        for selector in close_selectors:
            try:
                loc = page.locator(selector)
                if loc.count() <= 0:
                    continue
                if selector.startswith("svg"):
                    btn = loc.first.locator("xpath=ancestor::button[1]")
                    if btn.count() > 0:
                        btn.first.click(timeout=500)
                        return
                else:
                    loc.first.click(timeout=500)
                    return
            except Exception:
                continue

    def _is_in_direct_workspace(self, page: Page) -> bool:
        url = page.url or ""
        return ("/direct/inbox/" in url) or ("/direct/t/" in url)

    def _has_thread_rows_visible(self, page: Page) -> bool:
        for selector in self._row_selector_candidates():
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _validate_open_state(
        self,
        thread: ThreadLike,
        *,
        pre_url: str,
        selector: str,
        idx: int,
        row_preview: str,
        click_href: str,
        baseline_counts: Optional[dict[str, int]] = None,
    ) -> bool:
        page = self._ensure_page()
        pre_thread_id = _extract_thread_id(pre_url)

        visual_ok, visual_state = self._wait_for_visual_thread_sync(
            page,
            timeout_ms=_DM_THREAD_VISUAL_SYNC_TIMEOUT_MS,
        )
        post_url = str(visual_state.get("post_url") or (page.url or ""))
        post_thread_id = str(visual_state.get("post_thread_id") or _extract_thread_id(post_url))
        composer_visible = bool(visual_state.get("composer_visible"))
        message_panel_visible = bool(visual_state.get("message_panel_visible"))
        url_is_thread = bool(post_thread_id)
        thread_id_changed = bool(post_thread_id) and post_thread_id != pre_thread_id

        if not visual_ok:
            logger.error(
                "PlaywrightDM open_thread_visual_sync_error account=@%s target_thread=%s selector=%s idx=%s row=%s pre_url=%s post_url=%s click_href=%s url_is_thread=%s composer_visible=%s thread_id_changed=%s message_panel_visible=%s",
                self.username,
                thread.id,
                selector,
                idx,
                row_preview,
                pre_url,
                post_url,
                click_href,
                url_is_thread,
                composer_visible,
                thread_id_changed,
                message_panel_visible,
            )
            self._dismiss_transient_overlay(page)
            return False

        expected_thread_ids = self._expected_thread_ids(
            thread,
            post_thread_id=post_thread_id,
            click_href=click_href,
        )
        baseline = dict(baseline_counts or {})
        for thread_id in expected_thread_ids:
            baseline.setdefault(str(thread_id), 0)
        network_ok, network_thread_id = self._wait_for_thread_network_sync(
            page,
            expected_thread_ids=expected_thread_ids,
            baseline_counts=baseline,
            timeout_ms=_DM_THREAD_NETWORK_SYNC_TIMEOUT_MS,
        )
        if not network_ok:
            logger.error(
                "PlaywrightDM open_thread_network_sync_error account=@%s target_thread=%s selector=%s idx=%s row=%s expected_thread_ids=%s pre_url=%s post_url=%s click_href=%s",
                self.username,
                thread.id,
                selector,
                idx,
                row_preview,
                expected_thread_ids,
                pre_url,
                post_url,
                click_href,
            )
            return False

        resolved_thread_id = post_thread_id or str(network_thread_id or "").strip()
        print(
            style_text(
                f"[TRACE_ID SYNC BEFORE] pre_url={pre_url} post_url={post_url} post_thread_id={post_thread_id} id={thread.id} pk={thread.pk} flags=url_is_thread:{url_is_thread},composer_visible:{composer_visible},message_panel_visible:{message_panel_visible},thread_id_changed:{thread_id_changed}",
                color=Fore.WHITE,
            )
        )
        self._sync_thread_id(thread, resolved_thread_id)
        print(style_text(f"[TRACE_ID SYNC AFTER] id={thread.id} pk={thread.pk}", color=Fore.WHITE))
        self._current_thread_id = thread.id
        self._assert_logged_in(page)
        self._refresh_thread_participants(page, thread)
        logger.info(
            "PlaywrightDM open_thread_ok account=@%s thread_id=%s selector=%s idx=%s row=%s url=%s",
            self.username,
            thread.id,
            selector,
            idx,
            row_preview,
            post_url,
        )
        return True

    def _sync_thread_id(self, thread: ThreadLike, real_id: str) -> None:
        if not real_id:
            return
        if real_id == thread.id:
            return
        old_id = thread.id
        thread.id = real_id
        thread.pk = real_id
        if old_id in self._thread_cache:
            self._thread_cache[real_id] = self._thread_cache.pop(old_id)
        if old_id in self._thread_cache_meta:
            self._thread_cache_meta[real_id] = self._thread_cache_meta.pop(old_id)

    def _collect_message_nodes(self, page: Page):
        container = None
        for selector in _MESSAGE_CONTAINER_SELECTORS:
            try:
                if page.locator(selector).count():
                    container = page.locator(selector).first
                    break
            except Exception:
                continue
        if container is None:
            container = page

        for selector in _MESSAGE_NODE_SELECTORS:
            try:
                nodes = container.locator(selector)
                if nodes.count():
                    return nodes
            except Exception:
                continue
        return container.locator(_MESSAGE_NODE_SELECTORS[0])

    # LEGACY: FunciÃƒÂ³n deshabilitada - ya no se usa (usaba _THREAD_ANCHOR_SELECTORS eliminadas)
    # def _select_thread_anchor(...)

    def _locator_first_attribute(self, locator, attr_name: str, *, timeout_ms: int = 300) -> str:
        try:
            if locator.count() <= 0:
                return ""
        except Exception:
            return ""
        try:
            return (locator.first.get_attribute(attr_name, timeout=timeout_ms) or "").strip()
        except TypeError:
            try:
                return (locator.first.get_attribute(attr_name) or "").strip()
            except Exception:
                return ""
        except Exception:
            return ""

    def _resolve_thread_key(
        self,
        page: Page,
        row,
        *,
        title: str,
        peer_username: str,
        snippet: str,
    ) -> tuple[str, str, str]:
        # IMPORTANT:
        # Resolver la identidad del thread desde la fila (row), no desde la URL actual.
        # Si se usa page.url mientras estamos en /direct/t/, todas las filas pueden quedar
        # con el mismo id y el barrido se corta antes de tiempo.
        href = self._locator_first_attribute(row, "href")
        if "/direct/t/" not in href:
            href = self._locator_first_attribute(row.locator("a[href*='/direct/t/']"), "href")
        thread_id = _extract_thread_id(href)
        if thread_id:
            return thread_id, "row_href", _normalize_direct_link(href)

        base = _normalize_key_source(title)
        peer_norm = _normalize_key_source(peer_username)
        snippet_norm = _normalize_key_source(snippet)
        if base and peer_norm:
            base = f"{base}|{peer_norm}"
        elif not base:
            base = peer_norm or snippet_norm
        if not base:
            return "", "none", ""
        stable_id = hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:16]
        return f"stable_{stable_id}", "stable_id", ""

    def _log_navigation_state(self, label: str) -> None:
        """
        Probe de diagnÃƒÂ³stico para saber exactamente donde estamos y quÃƒÂ© vemos.
        """
        try:
            if self._page is None:
                return
            page = self._page
            url = page.url or ""
            in_inbox = "/direct/inbox/" in url
            in_thread = "/direct/t/" in url

            composer_results = {}
            for selector in _COMPOSER_SELECTORS:
                try:
                    # check if it exists in DOM at all
                    count = page.locator(selector).count()
                    visible = page.locator(selector).first.is_visible() if count > 0 else False
                    composer_results[selector] = {"count": count, "visible": visible}
                except Exception as e:
                    composer_results[selector] = {"error": str(e)}

            message_results = {}
            for selector in _MESSAGE_NODE_SELECTORS:
                try:
                    count = page.locator(selector).count()
                    visible = page.locator(selector).first.is_visible() if count > 0 else False
                    message_results[selector] = {"count": count, "visible": visible}
                except Exception as e:
                    message_results[selector] = {"error": str(e)}

            logger.info(
                "PlaywrightDM diagnostic_probe label=%s url=%s in_inbox=%s in_thread=%s has_composer=%s composer_details=%s message_details=%s",
                label, url, in_inbox, in_thread,
                any(d.get("visible") for d in composer_results.values() if isinstance(d, dict)),
                composer_results, message_results
            )
        except Exception as e:
            logger.error("Error in PlaywrightDM diagnostic_probe: %s", e)

    def _wait_thread_open(self, page: Page, timeout_ms: int = 6000) -> bool:
        """
        Espera sincronizacion visual completa del thread:
        composer visible + panel de mensajes visible.
        """
        visual_ok, state = self._wait_for_visual_thread_sync(page, timeout_ms=timeout_ms)
        found_composer = bool(state.get("composer_visible"))
        message_panel_visible = bool(state.get("message_panel_visible"))
        current_url = page.url or ""
        is_in_thread = bool(re.search(r"/direct/t/([^/]+)", current_url))

        if _DM_VERBOSE_PROBES:
            print(style_text(f"[Probe] URL = {current_url}", color=Fore.WHITE))
            print(style_text(f"[Probe] thread_abierto = {is_in_thread and found_composer and message_panel_visible}", color=Fore.WHITE))
            print(style_text(f"[Probe] existe_composer = {found_composer}", color=Fore.WHITE))
            print(style_text(f"[Probe] message_panel_visible = {message_panel_visible}", color=Fore.WHITE))

        return bool(visual_ok and is_in_thread and found_composer and message_panel_visible)

    def _open_thread_by_cache(self, thread: ThreadLike) -> bool:
        page = self._ensure_page()
        meta = self._thread_cache_meta.get(thread.id)
        if not meta:
            logger.error(
                "PlaywrightDM open_thread_cache_missing account=@%s thread_id=%s",
                self.username,
                thread.id,
            )
            return False

        title = (meta.get("title") or "").strip()
        peer = (meta.get("peer_username") or "").strip()
        candidates = [title, peer]
        candidates = [c for c in candidates if c]
        if not candidates:
            logger.error(
                "PlaywrightDM open_thread_cache_candidates_empty account=@%s thread_id=%s",
                self.username,
                thread.id,
            )
            return False

        current_url = page.url or ""
        if not self._is_in_direct_workspace(page):
            self._open_inbox()
        elif "/direct/t/" in current_url and not self._has_thread_rows_visible(page):
            self.return_to_inbox()

        selector_candidates = [
            "div[role='navigation'][aria-label='Lista de conversaciones'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'][aria-label='Conversation list'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'] div[role='button'][tabindex='0']:has(abbr[aria-label])",
            "div[role='navigation'][aria-label='Lista de conversaciones'] div[role='button'][tabindex='0']",
            "div[role='navigation'][aria-label='Conversation list'] div[role='button'][tabindex='0']",
            "div[role='navigation'] div[role='button'][tabindex='0']",
            THREAD_ROW_SELECTOR,
            "div[role='main'] div[role='list'] a[href*='/direct/t/']",
            "div[role='main'] div[role='listitem']",
            "div[role='main'] div[role='row']",
            "div[role='main'] div[role='button'][tabindex='0']",
        ]

        rows = None
        selected_selector = ""
        for selector in selector_candidates:
            try:
                candidate = page.locator(selector)
                if candidate.count() > 0:
                    rows = candidate
                    selected_selector = selector
                    break
            except Exception:
                continue
        if rows is None:
            logger.error(
                "PlaywrightDM open_thread_cache_no_rows account=@%s thread_id=%s",
                self.username,
                thread.id,
            )
            return False

        inbox_panel, _method, _selector, _panel_counts = self._get_inbox_panel(page, rows=rows)
        self._scroll_panel_to_top(inbox_panel)
        target_scan = max(25, len(self._thread_cache_meta) + 10)
        max_scroll_passes = max(25, min(2000, target_scan * 4))
        stagnant_limit = self._stagnation_limit(target_scan)

        scanned = 0
        stagnant_passes = 0
        for _pass in range(max_scroll_passes):
            try:
                total = rows.count()
            except Exception:
                total = 0
            matched_in_pass = False
            for idx in range(total):
                row = rows.nth(idx)
                try:
                    row_valid = self._row_is_valid(row, selector=selected_selector, fast=True)
                except TypeError:
                    row_valid = self._row_is_valid(row, selector=selected_selector)
                if not row_valid:
                    continue
                try:
                    text_value = (row.inner_text() or "").lower()
                except Exception:
                    continue
                scanned += 1
                if not any(c.lower() in text_value for c in candidates):
                    continue

                matched_in_pass = True
                row_preview = self._row_preview(row)
                pre_url = page.url or ""
                baseline_counts = self._snapshot_api_counts(
                    self._expected_thread_ids(thread, click_href="")
                )
                clicked, click_href = self._click_row_target(
                    row,
                    selector=selected_selector,
                    idx=idx,
                )
                if not clicked:
                    continue
                if self._validate_open_state(
                    thread,
                    pre_url=pre_url,
                    selector=selected_selector,
                    idx=idx,
                    row_preview=row_preview,
                    click_href=click_href,
                    baseline_counts=baseline_counts,
                ):
                    return True
                if not self._is_in_direct_workspace(page):
                    self.return_to_inbox()

            moved = self._scroll_panel_down(inbox_panel)
            if not moved:
                break
            if matched_in_pass:
                stagnant_passes = 0
            else:
                stagnant_passes += 1
            if stagnant_passes >= stagnant_limit:
                break
            self._wait_for_scroll_settle(page, extra_ms=20)

        logger.error(
            "PlaywrightDM open_thread_cache_failed account=@%s thread_id=%s scanned=%s",
            self.username,
            thread.id,
            scanned,
        )
        return False


    def _find_composer(self, page: Page):
        for selector in _COMPOSER_SELECTORS:
            try:
                loc = page.locator(selector)
                if loc.count():
                    return loc.first
            except Exception:
                continue
        return None

    def _refresh_thread_participants(self, page: Page, thread: ThreadLike) -> None:
        username = _extract_header_username(page, self.username)
        if not username:
            return
        if any(u.username == username for u in thread.users):
            return
        thread.users = [UserLike(pk=username, id=username, username=username)]
        self._thread_cache[thread.id] = thread

    def _assert_logged_in(self, page: Page) -> None:
        url = (page.url or "").lower()
        if any(token in url for token in ("/accounts/login", "/challenge/", "/checkpoint/", "two_factor")):
            raise RuntimeError(f"Login requerido o checkpoint detectado para @{self.username}. URL={page.url}")
        try:
            if page.locator("input[name='username'], input[name='password']").count():
                raise RuntimeError(f"Login requerido para @{self.username}.")
        except RuntimeError:
            raise
        except Exception:
            return

    def _is_outbound(self, node) -> bool:
        try:
            if node.locator("[data-testid='own']").count():
                return True
        except Exception:
            pass

        try:
            box = node.bounding_box()
            if not box:
                return False
            viewport = self._page.viewport_size if self._page is not None else None
            if viewport and viewport.get("width"):
                return (box["x"] + box["width"]) > (viewport["width"] * 0.6)
        except Exception:
            pass
        return False

    def _verify_sent(self, thread: ThreadLike, text: str) -> Optional[str]:
        deadline = time.time() + VERIFY_TIMEOUT_S
        target_text = _normalize_message_text(text)
        page = self._ensure_page()
        poll_count = 0
        while time.time() < deadline:
            poll_count += 1
            # Camino rapido: revisar solo los ultimos nodos del DOM sin reabrir el thread.
            try:
                nodes = self._collect_message_nodes(page)
                total_nodes = nodes.count()
                for idx in range(max(0, total_nodes - 12), total_nodes):
                    node = nodes.nth(idx)
                    node_text = _extract_message_text(node)
                    if not _message_text_matches(target_text, node_text):
                        continue
                    try:
                        if self._is_outbound(node):
                            node_id = _extract_message_id(node)
                            if node_id:
                                return node_id
                            return _hash_message_id(thread.id, self.user_id, node_text, time.time())
                    except Exception:
                        # Si no se puede determinar direccion, aceptar match exacto como exito.
                        if _normalize_message_text(node_text) == target_text:
                            node_id = _extract_message_id(node)
                            if node_id:
                                return node_id
                            return _hash_message_id(thread.id, self.user_id, node_text, time.time())
            except Exception:
                pass
            # Camino robusto cada cierto numero de polls: parseo normal de mensajes.
            if poll_count % 3 == 0:
                try:
                    messages = self.get_messages(thread, amount=20, log=False)
                    for msg in messages:
                        if _message_text_matches(target_text, msg.text):
                            return msg.id
                except Exception:
                    pass
            time.sleep(0.25)
        try:
            messages = self.get_messages(thread, amount=20, log=False)
            last_out = next((m for m in messages if m.user_id == self.user_id), None)
            last_in = next((m for m in messages if m.user_id != self.user_id), None)
            logger.warning(
                "PlaywrightDM verify_fail thread=%s peer=%s last_in_ts=%s last_out_ts=%s",
                thread.id,
                _thread_peer_id(thread, self.user_id),
                _fmt_ts(last_in.timestamp if last_in else None),
                _fmt_ts(last_out.timestamp if last_out else None),
            )
        except Exception:
            pass
        return None

    def debug_dump_inbox(self, reason: str) -> str:
        page = self._ensure_page()
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        log_dir = Path("storage") / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        base = log_dir / f"{DM_DEBUG_DIRNAME}_{self.username}_{timestamp}"
        try:
            logger.info("PlaywrightDM debug_dump reason=%s url=%s", reason, page.url)
        except Exception:
            pass
        # Selectores mÃƒÂ­nimos para debugging
        debug_selectors = (
            "div[role='main'] div[role='listitem']",
            "div[role='main'] div[role='row']",
            "div[role='button'][tabindex='0']",
        )
        for selector in debug_selectors:
            try:
                count = page.locator(selector).count()
            except Exception:
                count = 0
            logger.info("PlaywrightDM selector_count selector=%s count=%s", selector, count)
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
        except Exception:
            pass
        try:
            html = page.content()
            Path(str(base) + ".html").write_text(html, encoding="utf-8", errors="ignore")
        except Exception:
            pass
        try:
            main_text = page.locator("div[role='main']").inner_text()
            Path(str(base) + ".txt").write_text(main_text, encoding="utf-8", errors="ignore")
        except Exception:
            pass
        return str(base)


_DM_RESPONSE_URL_HINTS = (
    "/api/graphql/",
    "/api/v1/direct_v2/",
    "/api/v1/direct/",
    "/direct_v2/",
)
_DM_THREAD_ID_KEYS = (
    "thread_id",
    "thread_v2_id",
    "thread_pk",
    "threadid",
    "conversation_id",
    "conversationid",
    "thread_key",
)
_DM_SENDER_ID_KEYS = (
    "sender_id",
    "senderid",
    "user_id",
    "userid",
    "actor_id",
    "profile_id",
)
_DM_ITEM_ID_KEYS = (
    "item_id",
    "itemid",
    "message_id",
    "messageid",
    "pk",
    "id",
)
_DM_TIMESTAMP_KEYS = (
    "timestamp_ms",
    "timestamp",
    "created_at_ms",
    "created_at",
    "client_timestamp",
    "server_timestamp_ms",
    "server_timestamp",
)


def _is_message_api_url(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    if not lowered:
        return False
    if "/api/graphql/" in lowered:
        return True
    return any(hint in lowered for hint in _DM_RESPONSE_URL_HINTS if hint != "/api/graphql/")


def _extract_api_messages_from_payload(
    payload: Any,
    *,
    self_user_id: str,
) -> tuple[list[_APIMessageRecord], list[dict[str, str]]]:
    messages: list[_APIMessageRecord] = []
    missing_timestamp: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for node, context_thread_id in _iter_payload_nodes(payload):
        parsed, missing = _extract_api_message_from_node(
            node,
            context_thread_id=context_thread_id,
            self_user_id=self_user_id,
        )
        if parsed is not None:
            dedup_key = (parsed.thread_id, parsed.item_id)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            messages.append(parsed)
        elif missing is not None:
            missing_timestamp.append(missing)
    return messages, missing_timestamp


def _payload_thread_ids(payload: Any, *, self_user_id: str) -> set[str]:
    parsed, missing = _extract_api_messages_from_payload(payload, self_user_id=self_user_id)
    ids: set[str] = set()
    for item in parsed:
        thread_id = str(item.thread_id or "").strip()
        if thread_id:
            ids.add(thread_id)
    for item in missing:
        thread_id = str(item.get("thread_id") or "").strip()
        if thread_id:
            ids.add(thread_id)
    return ids


def _iter_payload_nodes(payload: Any) -> Iterator[tuple[dict[str, Any], str]]:
    stack: list[tuple[Any, str]] = [(payload, "")]
    while stack:
        current, thread_context = stack.pop()
        if isinstance(current, list):
            for item in reversed(current):
                if isinstance(item, (dict, list)):
                    stack.append((item, thread_context))
            continue
        if not isinstance(current, dict):
            continue
        mapped = {str(key): value for key, value in current.items()}
        next_thread = _extract_thread_id_from_node(mapped) or thread_context
        yield mapped, next_thread
        for value in mapped.values():
            if isinstance(value, (dict, list)):
                stack.append((value, next_thread))


def _extract_api_message_from_node(
    node: dict[str, Any],
    *,
    context_thread_id: str,
    self_user_id: str,
) -> tuple[Optional[_APIMessageRecord], Optional[dict[str, str]]]:
    thread_id = _extract_thread_id_from_node(node) or str(context_thread_id or "").strip()
    sender_id = _extract_sender_id_from_node(node)
    item_id = _extract_item_id_from_node(node)
    timestamp = _extract_timestamp_from_node(node)
    text = _extract_message_text_from_api_node(node)
    has_sender_hint = bool(sender_id or any(key in node for key in _DM_SENDER_ID_KEYS + ("sender", "user", "actor", "from")))

    message_identity = bool(
        item_id
        or _coerce_str(node.get("item_type"))
        or _coerce_str(node.get("message_type"))
        or _coerce_str(node.get("client_context"))
        or (text and has_sender_hint)
    )
    if not message_identity:
        return None, None
    if not thread_id:
        return None, None
    if timestamp is None:
        has_timestamp_key = any(key in node for key in _DM_TIMESTAMP_KEYS)
        if has_timestamp_key or has_sender_hint:
            return None, {
                "thread_id": thread_id,
                "item_id": item_id,
                "sender_id": sender_id,
            }
        return None, None

    direction = _resolve_direction_from_node(node, sender_id=sender_id, self_user_id=self_user_id)
    normalized_sender_id = sender_id
    if not normalized_sender_id:
        normalized_sender_id = self_user_id if direction == "outbound" else "peer"
    normalized_item_id = item_id
    if not normalized_item_id:
        seed = f"{thread_id}|{normalized_sender_id}|{timestamp}|{text}".encode(
            "utf-8",
            errors="ignore",
        )
        normalized_item_id = hashlib.sha1(seed).hexdigest()[:20]
    return (
        _APIMessageRecord(
            thread_id=thread_id,
            sender_id=normalized_sender_id,
            timestamp=timestamp,
            item_id=normalized_item_id,
            direction=direction,
            text=text,
        ),
        None,
    )


def _extract_thread_id_from_node(node: dict[str, Any]) -> str:
    for key in _DM_THREAD_ID_KEYS:
        value = _coerce_str(node.get(key))
        if value:
            return value
    for nested_key in ("thread", "conversation"):
        nested = node.get(nested_key)
        if isinstance(nested, dict):
            for key in _DM_THREAD_ID_KEYS + ("id",):
                value = _coerce_str(nested.get(key))
                if value:
                    return value
    return ""


def _extract_sender_id_from_node(node: dict[str, Any]) -> str:
    for key in _DM_SENDER_ID_KEYS:
        value = _coerce_str(node.get(key))
        if value:
            return value
    for nested_key in ("sender", "user", "actor", "from"):
        nested = node.get(nested_key)
        if not isinstance(nested, dict):
            continue
        value = _coerce_str(nested.get("id") or nested.get("pk") or nested.get("user_id"))
        if value:
            return value
    return ""


def _extract_item_id_from_node(node: dict[str, Any]) -> str:
    for key in _DM_ITEM_ID_KEYS:
        value = _coerce_str(node.get(key))
        if value:
            return value
    return ""


def _extract_timestamp_from_node(node: dict[str, Any]) -> Optional[float]:
    for key in _DM_TIMESTAMP_KEYS:
        if key not in node:
            continue
        value = node.get(key)
        timestamp = _coerce_timestamp_seconds(value)
        if timestamp is not None:
            return timestamp
    return None


def _coerce_timestamp_seconds(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    numeric: Optional[float] = None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            numeric = float(raw)
        except Exception:
            iso = raw
            if raw.endswith("Z"):
                iso = raw[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(iso)
            except Exception:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            numeric = float(dt.timestamp())
    if numeric is None or numeric <= 0:
        return None
    if numeric >= 1e14:
        numeric /= 1_000_000.0
    elif numeric >= 1e11:
        numeric /= 1_000.0
    return numeric if numeric > 0 else None


def _extract_message_text_from_api_node(node: dict[str, Any]) -> str:
    for key in ("text", "message", "content", "caption", "body"):
        value = node.get(key)
        extracted = _extract_text_value(value)
        if extracted:
            return extracted
    return ""


def _extract_text_value(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "body", "content", "message"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _resolve_direction_from_node(
    node: dict[str, Any],
    *,
    sender_id: str,
    self_user_id: str,
) -> str:
    raw_direction = _coerce_str(
        node.get("direction")
        or node.get("message_direction")
        or node.get("folder")
        or node.get("type")
    ).lower()
    if raw_direction in {"outbound", "outgoing", "sent", "viewer"}:
        return "outbound"
    if raw_direction in {"inbound", "incoming", "received"}:
        return "inbound"

    for key in ("is_sent_by_viewer", "sent_by_viewer", "is_outgoing", "outgoing", "from_viewer"):
        value = node.get(key)
        if isinstance(value, bool):
            return "outbound" if value else "inbound"

    if sender_id and self_user_id and str(sender_id) == str(self_user_id):
        return "outbound"
    return "inbound"


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    text = str(value).strip()
    return text


def _proxy_from_account(account: dict) -> Optional[dict]:
    try:
        from src.proxy_payload import proxy_from_account
    except Exception:
        return None
    return proxy_from_account(account)


def _extract_thread_id(href: str) -> str:
    if not href:
        return ""
    match = re.search(r"/direct/t/([^/]+)", href)
    if match:
        return match.group(1)
    return ""


def _normalize_key_source(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"\b(hace\\s+\\d+\\s*[hmd]|ayer|hoy)\\b", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _extract_row_snippet(row) -> str:
    try:
        texts = row.locator("div[dir='auto'], span[dir='auto']").all_inner_texts()
    except Exception:
        texts = []
    cleaned = [text.strip() for text in texts if isinstance(text, str) and text.strip()]
    if not cleaned:
        return ""
    return " ".join(cleaned[:2]).strip()


def _normalize_direct_link(href: str) -> str:
    value = (href or "").strip()
    if not value:
        return ""
    if value.startswith("http"):
        return value
    if value.startswith("/"):
        return f"https://www.instagram.com{value}"
    return f"https://www.instagram.com/{value.lstrip('/')}"


def _fallback_thread_id(account: str, seed: str) -> str:
    raw = f"{account}:{seed}".encode("utf-8", errors="ignore")
    return hashlib.sha1(raw).hexdigest()


def _extract_thread_title(node) -> str:
    try:
        text = node.inner_text() or ""
    except Exception:
        text = ""
    text = text.strip()
    if text:
        parts = [p.strip() for p in text.splitlines() if p.strip()]
        if parts:
            return parts[0]
    try:
        aria = node.get_attribute("aria-label") or ""
    except Exception:
        aria = ""
    lowered = aria.lower()
    for token in ("from ", "de "):
        if token in lowered:
            idx = lowered.find(token)
            if idx >= 0:
                return aria[idx + len(token) :].strip()
    return ""


def _thread_unread_count(node) -> int:
    try:
        aria = (node.get_attribute("aria-label") or "").lower()
    except Exception:
        aria = ""

    if any(token in aria for token in _UNREAD_HINTS):
        return 1

    # BÃƒÂºsqueda de badge por aria-label
    try:
        badge = node.locator("span[aria-label*='unread'], span[aria-label*='sin leer'], span[aria-label*='no leido']")
        if badge.count():
            return 1
    except Exception:
        pass

    # BÃƒÂºsqueda por "punto azul" (visual) - Instagram suele usar un div/span con fondo azul
    # El color rgb(0, 149, 246) es el azul caracterÃƒÂ­stico de Instagram
    try:
        blue_dot = node.locator("div[style*='background-color: rgb(0, 149, 246)'], span[style*='background-color: rgb(0, 149, 246)']")
        if blue_dot.count() > 0:
            return 1
    except Exception:
        pass

    return 0


def _extract_header_username(page: Page, self_username: str) -> str:
    try:
        links = page.locator("header a[href^='/']")
        total = links.count()
    except Exception:
        total = 0
        links = None
    for idx in range(total):
        try:
            href = links.nth(idx).get_attribute("href") or ""
        except Exception:
            continue
        if not href.endswith("/"):
            continue
        candidate = href.strip("/")
        if "/" in candidate:
            continue
        lowered = candidate.lower()
        if lowered in {"direct", "accounts"}:
            continue
        if lowered == self_username.lower():
            continue
        if re.fullmatch(r"[a-z0-9._]{1,30}", lowered) is None:
            continue
        return candidate
    return ""


def _extract_header_title(page: Page) -> str:
    try:
        header = page.locator("header").first
    except Exception:
        return ""
    for selector in ("span[title]", "h1", "h2", "div[dir='auto']", "span[dir='auto']"):
        try:
            loc = header.locator(selector)
            if loc.count():
                text = (loc.first.inner_text() or "").strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


def _extract_message_text(node) -> str:
    texts: List[str] = []
    for selector in ("div[dir='auto']", "span[dir='auto']"):
        try:
            parts = node.locator(selector).all_inner_texts()
        except Exception:
            parts = []
        for part in parts:
            cleaned = (part or "").strip()
            if cleaned:
                texts.append(cleaned)
    if texts:
        return "\n".join(texts).strip()
    try:
        return (node.inner_text() or "").strip()
    except Exception:
        return ""


def _extract_message_id(node) -> str:
    for attr in ("data-message-id", "data-id", "data-item-id"):
        try:
            value = (node.get_attribute(attr) or "").strip()
        except Exception:
            value = ""
        if value:
            return value
    return ""


def _normalize_message_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _message_text_matches(expected: str, candidate: str) -> bool:
    expected_norm = _normalize_message_text(expected)
    candidate_norm = _normalize_message_text(candidate)
    if not expected_norm or not candidate_norm:
        return False
    if expected_norm == candidate_norm:
        return True
    if len(expected_norm) >= 20 and (
        expected_norm in candidate_norm or candidate_norm in expected_norm
    ):
        return True
    expected_prefix = expected_norm[:48]
    candidate_prefix = candidate_norm[:48]
    if len(expected_prefix) >= 16 and expected_prefix == candidate_prefix:
        return True
    return False


def _hash_message_id(thread_id: str, user_id: str, text: str, timestamp: Optional[float]) -> str:
    seed = f"{thread_id}|{user_id}|{timestamp or ''}|{text}".encode("utf-8", errors="ignore")
    return hashlib.sha1(seed).hexdigest()


def _thread_peer_id(thread: ThreadLike, self_id: str) -> str:
    """
    Retorna el ID del peer (otro usuario) en un thread.
    Usa user.id (no pk) para consistencia.
    """
    for user in thread.users:
        if user.id != self_id:
            return user.id
    if thread.users:
        return thread.users[0].id
    return "peer"


def _fmt_ts(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    try:
        return datetime.fromtimestamp(value).isoformat()
    except Exception:
        return str(value)
