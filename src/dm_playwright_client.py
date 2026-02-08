from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ui import Fore, style_text

try:  # pragma: no cover - optional dependency guard
    from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
except Exception:  # pragma: no cover
    Page = object  # type: ignore
    PlaywrightTimeoutError = Exception  # type: ignore
    sync_playwright = None  # type: ignore

from src.auth.onboarding import build_proxy
from src.playwright_service import (
    BASE_PROFILES,
    DEFAULT_ARGS,
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    DEFAULT_USER_AGENT,
    DEFAULT_VIEWPORT,
)

logger = logging.getLogger(__name__)

INBOX_URL = "https://www.instagram.com/direct/inbox/"
THREAD_URL_TEMPLATE = "https://www.instagram.com/direct/t/{thread_id}/"
DM_DEBUG_DIRNAME = "dm_debug"

VERIFY_TIMEOUT_S = float(os.getenv("HUMAN_DM_VERIFY_TIMEOUT", "10.0"))

# Selectores mínimos necesarios (eliminadas constantes legacy de anchors)
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
_COMPOSER_SELECTORS = (
    "div[role='main'] div[role='textbox'][contenteditable='true']",
    "div[role='main'] div[contenteditable='true'][role='textbox']",
    "div[role='main'] textarea",
    "div[role='textbox'][contenteditable='true']",
    "div[contenteditable='true']",
)

_UNREAD_HINTS = ("unread", "sin leer", "no leido", "no leido")


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


@dataclass
class MessageLike:
    id: str
    user_id: str
    text: str
    timestamp: Optional[float]
    direction: str = "inbound"  # "inbound" or "outbound"


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
        """
        Discovery de inbox: Retorna lista de threads visibles sin abrirlos aún.
        """
        page = self._ensure_page()
        self._open_inbox()

        selector_candidates = self._row_selector_candidates()

        print(style_text(f"[Probe] Iniciando list_threads en {page.url}", color=Fore.WHITE))

        threads: List[ThreadLike] = []
        seen_titles = set()

        for selector in selector_candidates:
            try:
                rows = page.locator(selector)
                total = rows.count()
                print(style_text(f"[Probe] Selector '{selector}' -> count={total}", color=Fore.WHITE))
                if total == 0:
                    continue

                for idx in range(total):
                    if len(threads) >= amount:
                        break

                    row = rows.nth(idx)
                    if not self._row_is_valid(row):
                        continue

                    if filter_unread and _thread_unread_count(row) <= 0:
                        continue

                    lines = self._row_lines(row)
                    title = lines[0] if lines else "unknown"
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)

                    # ID Estable recomendado: account:recipient
                    stable_id = f"{self.username}:{title}"

                    thread = ThreadLike(
                        id=stable_id,
                        pk=stable_id,
                        users=[UserLike(pk=title, id=title, username=title)],
                        title=title,
                    )
                    # Guardar meta para poder encontrarlo luego por título
                    self._thread_cache[stable_id] = thread
                    self._thread_cache_meta[stable_id] = {"title": title, "idx": idx, "selector": selector}
                    threads.append(thread)

                if threads:
                    break
            except Exception:
                continue

        return threads

    # LEGACY: Función deshabilitada - ya no se usa (reemplazada por click-first scan)
    # def _list_threads_from_anchors(...)

    def _row_lines(self, row) -> List[str]:
        try:
            raw_text = row.inner_text() or ""
        except Exception:
            raw_text = ""
        return [line.strip() for line in raw_text.splitlines() if line.strip()]

    def _row_selector_candidates(self) -> List[str]:
        return [
            "a[href*='/direct/t/']",
            "div[aria-label='Chats'] div[role='button']",
            "div[aria-label='Mensajes'] div[role='button']",
            "div[role='main'] div[role='listitem']",
            "div[role='main'] div[role='row']",
            "div[role='listitem']",
            "div[role='row']",
            "div[role='main'] div[role='button'][tabindex='0']",
            "div[role='button'][tabindex='0']",
        ]

    def _get_inbox_panel(self, page: Page):
        """
        [Probe/Fix] Intenta encontrar el panel lateral de mensajes.
        Retorna (locator, metodo, selector, meta).
        """
        for selector in ("div[role='main']", "main", "div[role='navigation']"):
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    print(style_text(f"[Probe] _get_inbox_panel encontró '{selector}'", color=Fore.WHITE))
                    return loc.first, "selector", selector, {"count": loc.count()}
            except Exception:
                continue
        print(style_text("[Probe] _get_inbox_panel no encontró nada, usando page", color=Fore.YELLOW))
        return page, "page", "", {"count": 1}

    def _row_is_valid(self, row) -> bool:
        lines = self._row_lines(row)
        if not lines:
            return False
        first_line = lines[0].strip()
        lowered = first_line.lower()

        # [CRÍTICO] Un thread DM real DEBE tener un enlace a /direct/t/
        # Las burbujas de Notas no suelen tener este enlace directo.
        valid_href = False
        try:
            # 1. ¿El elemento mismo es el link?
            href = row.get_attribute("href") or ""
            if "/direct/t/" in href:
                valid_href = True
            # 2. ¿Contiene un link?
            elif row.locator("a[href*='/direct/t/']").count() > 0:
                valid_href = True
        except Exception:
            pass

        if not valid_href:
            print(style_text(f"[Probe] Fila rechazada (sin link DM): '{first_line[:30]}...'", color=Fore.YELLOW))
            return False

        # Filtros de exclusión conocidos (headers, tabs, botones de búsqueda)
        if lowered in {"primary", "general", "request", "buscar", "search", "enviar mensaje", "solicitudes", "principal"}:
            print(style_text(f"[Probe] Fila rechazada (filtro texto): '{first_line}'", color=Fore.YELLOW))
            return False

        # Tokens específicos de UI de Notas
        notes_tokens = ("tu nota", "primera nota", "compartir una nota", "notas", "notes", "share a note", "nota de")
        if any(token in lowered for token in notes_tokens):
            print(style_text(f"[Probe] Fila rechazada (filtro Notas): '{first_line}'", color=Fore.YELLOW))
            return False

        try:
            aria = (row.get_attribute("aria-label") or "").lower()
            if any(token in aria for token in notes_tokens):
                print(style_text(f"[Probe] Fila rechazada (filtro Notas aria): '{first_line}'", color=Fore.YELLOW))
                return False
        except Exception:
            pass

        # RELAXED VALIDATION: Priorizamos la existencia de señales de interacción real
        signals = 0
        try:
            # Señal 1: Presencia de timestamp real (etiqueta <time>)
            if row.locator("time").count() > 0:
                signals += 1
            else:
                # Buscar patrones de tiempo más específicos (ej: "2 h", "5 min", "1 d")
                # Evitamos matches genéricos de una sola letra
                full_text = " ".join(lines)
                if re.search(r"\b\d+\s*[hdmws]\b", full_text) or re.search(r"\b\d+\s*(min|seg|hor|dia|sem)\b", full_text):
                    signals += 1
        except Exception:
            pass

        try:
            # Señal 2: Indicadores de mensajes no leídos
            aria = (row.get_attribute("aria-label") or "").lower()
            if any(token in aria for token in _UNREAD_HINTS) or row.locator("span[aria-label*='unread']").count() > 0:
                signals += 1
        except Exception:
            pass

        # PROBE LOG: Para diagnosticar por qué se aceptan o rechazan filas
        if len(lines) >= 1:
            # Si tiene al menos una línea y pasó los filtros críticos, es válido.
            logger.debug("PlaywrightDM checking row: first_line=%s signals=%d", first_line[:30], signals)
            print(style_text(f"[Probe] Fila aceptada: '{first_line}' (signals={signals})", color=Fore.GREEN))
            return True

        logger.info("PlaywrightDM row_is_valid=False first_line=%s reason=no_content", first_line[:50])
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

    # LEGACY: Función deshabilitada - ya no se usa (reemplazada por click-first scan)
    # def _list_threads_from_rows(...) -> List[ThreadLike]


    def get_messages(self, thread: ThreadLike, amount: int = 20, *, log: bool = True) -> List[MessageLike]:
        page = self._ensure_page()
        self._open_thread(thread)

        # Esperar a que los mensajes se hidraten
        try:
            msg_selector = _MESSAGE_NODE_SELECTORS[0]
            page.wait_for_selector(f"main {msg_selector}, div[role='main'] {msg_selector}", timeout=3000)
        except Exception:
            pass

        nodes = self._collect_message_nodes(page)
        total = nodes.count()
        if total <= 0:
            if log:
                logger.info(
                    "PlaywrightDM mensajes vacios thread=%s peer=%s account=@%s",
                    thread.id,
                    _thread_peer_id(thread, self.user_id),
                    self.username,
                )
            return []

        start_idx = max(0, total - max(1, amount))
        collected: List[MessageLike] = []
        used_fallback_ts = False
        for idx in range(start_idx, total):
            try:
                node = nodes.nth(idx)
                text = _extract_message_text(node)
                timestamp = _extract_message_timestamp(node)
                if timestamp is None:
                    timestamp = time.time()
                    used_fallback_ts = True
                outbound = self._is_outbound(node)
                direction = "outbound" if outbound else "inbound"
                user_id = self.user_id if outbound else _thread_peer_id(thread, self.user_id)
                msg_id = _extract_message_id(node)
                if not msg_id:
                    # Message ID estable recomendado
                    msg_id = hashlib.sha1(f"{text}|{timestamp}|{direction}".encode()).hexdigest()[:12]
                collected.append(
                    MessageLike(
                        id=msg_id,
                        user_id=user_id,
                        text=text,
                        timestamp=timestamp,
                        direction=direction,
                    )
                )
            except Exception:
                continue

        collected.sort(key=lambda m: (m.timestamp is not None, m.timestamp or 0), reverse=True)

        last_outbound = next((m for m in collected if m.user_id == self.user_id), None)
        last_inbound = next((m for m in collected if m.user_id != self.user_id), None)
        if log:
            logger.info(
                "PlaywrightDM mensajes_leidos thread=%s peer=%s count=%d last_in_ts=%s last_out_ts=%s",
                thread.id,
                _thread_peer_id(thread, self.user_id),
                len(collected),
                _fmt_ts(last_inbound.timestamp if last_inbound else None),
                _fmt_ts(last_outbound.timestamp if last_outbound else None),
            )
            if used_fallback_ts:
                logger.warning(
                    "PlaywrightDM timestamps ausentes en thread=%s @%s; usando now() como fallback",
                    thread.id,
                    self.username,
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
            logger.warning("PlaywrightDM no pudo completar acciones de envío thread=%s @%s", thread.id, self.username)
            return None

        message_id = self._verify_sent(thread, text)
        if message_id:
            logger.info("PlaywrightDM envio_ok thread=%s msg_id=%s", thread.id, message_id)
        else:
            logger.warning("PlaywrightDM envio_no_verificado thread=%s @%s", thread.id, self.username)
        return message_id

    def _ensure_page(self) -> Page:
        if self._page is not None:
            return self._page

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
        return self._page

    def _open_inbox(self) -> None:
        page = self._ensure_page()
        try:
            page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError:
            pass
        self._dismiss_overlays(page)
        self._assert_logged_in(page)

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

        if not found_container:
            # Si ninguno es visible de inmediato, esperar brevemente al más probable
            try:
                page.wait_for_selector("div[role='main'], main, div[role='navigation']", timeout=10_000)
                # Re-chequear
                for selector in container_candidates:
                    if page.locator(selector).count() > 0:
                        found_container = selector
                        break
            except Exception:
                pass

        print(style_text(f"[Probe] Inbox container: {found_container}", color=Fore.WHITE))
        for search_selector in ("input[placeholder='Buscar']", "input[placeholder='Search']", "input[name='queryBox']"):
            try:
                page.wait_for_selector(search_selector, timeout=15_000)
                break
            except Exception:
                continue
        rows_ready = False
        row_selectors = tuple(self._row_selector_candidates())
        chosen = ""
        deadline = time.time() + 12.0
        while time.time() < deadline and not chosen:
            for selector in row_selectors:
                try:
                    if page.locator(selector).count():
                        chosen = selector
                        break
                except Exception:
                    continue
            if not chosen:
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    break
        if chosen:
            try:
                page.wait_for_selector(chosen, timeout=8_000)
                rows_ready = True
            except Exception:
                rows_ready = False
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
                        # Buscar botón de cierre dentro del dialog
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
        
        # CRÍTICO: Siempre re-chequear login inputs al final (no depender del chequeo inicial)
        # porque pueden aparecer después de cerrar otros overlays
        try:
            has_login_after_close = page.locator("input[name='username'], input[name='password']").count() > 0
            if has_login_after_close:
                raise RuntimeError(
                    f"Overlay de login detectado y no se pudo cerrar para @{self.username}. "
                    "Sesión posiblemente inválida."
                )
        except RuntimeError:
            raise
        except Exception:
            pass

    def _open_thread(self, thread: ThreadLike) -> None:
        page = self._ensure_page()

        # PROBE: ¿Ya estamos en el thread correcto?
        if "/direct/t/" in (page.url or "") and thread.id in page.url:
            self._current_thread_id = thread.id
            return

        opened = False

        # 1. Intentar navegación directa si tenemos ID real
        if thread.id and not thread.id.startswith("stable_"):
            url = THREAD_URL_TEMPLATE.format(thread_id=thread.id)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                opened = self._wait_thread_open(page, timeout_ms=5000)
            except Exception:
                opened = False

        # 2. Si es ID sintético o la navegación falló, usar el buscador de filas (sidebar)
        if not opened:
            opened = self._open_thread_by_cache(thread)

        # 3. Fallback final: ir a inbox y reintentar sidebar
        if not opened:
            try:
                page.goto(INBOX_URL, wait_until="domcontentloaded", timeout=30_000)
                opened = self._open_thread_by_cache(thread)
            except Exception:
                opened = False

        if not opened:
            self.debug_dump_inbox("failed_to_open_thread")
            raise RuntimeError(f"No se pudo abrir el thread '{thread.title}'")

        # Capturar y actualizar ID real en el objeto thread
        new_url = page.url or ""
        real_id = _extract_thread_id(new_url)
        if real_id and real_id != thread.id:
            old_id = thread.id
            logger.info("PlaywrightDM id_updated %s -> %s", old_id, real_id)
            thread.id = real_id
            thread.pk = real_id
            # Actualizar cache interno
            if old_id in self._thread_cache:
                self._thread_cache[real_id] = self._thread_cache.pop(old_id)
                self._thread_cache_meta[real_id] = self._thread_cache_meta.pop(old_id)

        self._current_thread_id = thread.id
        self._assert_logged_in(page)
        try:
            page.wait_for_selector("textarea, div[role='textbox']", timeout=12_000)
        except Exception:
            pass

        self._current_thread_id = thread.id
        self._refresh_thread_participants(page, thread)
        logger.info("PlaywrightDM thread_abierto id=%s user=%s", thread.id, _thread_peer_id(thread, self.user_id))

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

    # LEGACY: Función deshabilitada - ya no se usa (usaba _THREAD_ANCHOR_SELECTORS eliminadas)
    # def _select_thread_anchor(...)

    def _resolve_thread_key(
        self,
        page: Page,
        row,
        *,
        title: str,
        peer_username: str,
        snippet: str,
    ) -> tuple[str, str, str]:
        url = page.url or ""
        if "/direct/t/" in url:
            thread_id = _extract_thread_id(url)
            if thread_id:
                return thread_id, "real_url", _normalize_direct_link(url)
        href = ""
        try:
            href = row.locator("a[href*='/direct/t/']").first.get_attribute("href") or ""
        except Exception:
            href = ""
        thread_id = _extract_thread_id(href)
        if thread_id:
            return thread_id, "row_href", _normalize_direct_link(href)
        try:
            header_href = page.locator("header a[href*='/direct/t/']").first.get_attribute("href") or ""
        except Exception:
            header_href = ""
        thread_id = _extract_thread_id(header_href)
        if thread_id:
            return thread_id, "header_href", _normalize_direct_link(header_href)
        try:
            any_href = page.locator("a[href*='/direct/t/']").first.get_attribute("href") or ""
        except Exception:
            any_href = ""
        thread_id = _extract_thread_id(any_href)
        if thread_id:
            return thread_id, "dom_fallback", _normalize_direct_link(any_href)

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
        Probe de diagnóstico para saber exactamente donde estamos y qué vemos.
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
        Espera a que el composer esté visible y confirma que estamos en un thread.
        """
        # 1. Esperar a que la URL cambie al patrón de thread
        try:
            page.wait_for_url(re.compile(r".*/direct/t/.*"), timeout=timeout_ms // 2)
        except Exception:
            pass

        found_composer = False
        for selector in _COMPOSER_SELECTORS:
            try:
                # Intentar esperar al selector
                page.wait_for_selector(selector, timeout=timeout_ms // len(_COMPOSER_SELECTORS))
                found_composer = True
                break
            except Exception:
                continue

        # Re-obtener URL después de la espera
        current_url = page.url or ""
        is_in_thread = bool(re.search(r"/direct/t/([^/]+)", current_url))

        # Probes de estado EXACTOS requeridos por el usuario
        print(style_text(f"[Probe] URL = {current_url}", color=Fore.WHITE))
        print(style_text(f"[Probe] thread_abierto = {is_in_thread and found_composer}", color=Fore.WHITE))
        print(style_text(f"[Probe] existe_composer = {found_composer}", color=Fore.WHITE))

        return is_in_thread and found_composer

    def _open_thread_by_cache(self, thread: ThreadLike) -> bool:
        """
        Intenta reabrir un thread usando metadata del cache.
        CRÍTICO: NO usa selectores en div[role='navigation'] para evitar clicks en Notas.
        Solo retorna True si el composer está visible después del click.
        """
        page = self._ensure_page()
        meta = self._thread_cache_meta.get(thread.id)
        if not meta:
            logger.info("PlaywrightDM cache_reopen_skip thread=%s reason=no_meta", thread.id)
            return False
        
        title = (meta.get("title") or "").strip()
        peer = (meta.get("peer_username") or "").strip()
        candidates = [title, peer]
        candidates = [c for c in candidates if c]
        if not candidates:
            logger.info("PlaywrightDM cache_reopen_skip thread=%s reason=no_candidates", thread.id)
            return False
        
        # Obtener panel del inbox - NUNCA usar div[role='navigation']
        inbox_panel, _method, _selector, _panel_counts = self._get_inbox_panel(page)
        
        # Estrategia de selectores: SOLO dentro de div[role='main'] o panel principal
        # Prohibido: div[role='navigation']
        selector_candidates = [
            "div[role='main'] div[role='listitem']",
            "div[role='main'] div[role='row']",
            "div[role='main'] div[role='button'][tabindex='0']",
        ]
        
        rows = None
        row_selector_used = ""
        for selector in selector_candidates:
            try:
                candidate = inbox_panel.locator(selector)
                count = candidate.count()
                if count > 0:
                    rows = candidate
                    row_selector_used = selector
                    logger.info(
                        "PlaywrightDM cache_reopen_rows thread=%s selector=%s count=%d",
                        thread.id,
                        selector,
                        count
                    )
                    break
            except Exception:
                continue
        
        if rows is None:
            logger.info("PlaywrightDM cache_reopen_fail thread=%s reason=no_rows", thread.id)
            return False
        
        total = rows.count()
        logger.info(
            "PlaywrightDM cache_reopen_scan thread=%s selector=%s total_rows=%d candidates=%s",
            thread.id,
            row_selector_used,
            total,
            candidates
        )
        
        for idx in range(total):
            row = rows.nth(idx)
            
            # Validar que la fila sea válida (no Notas, etc)
            if not self._row_is_valid(row):
                continue
            
            try:
                text_value = (row.inner_text() or "").lower()
            except Exception:
                text_value = ""
            
            if not text_value:
                continue
            
            # Verificar si esta fila contiene alguno de los candidatos
            if not any(c.lower() in text_value for c in candidates):
                continue
            
            # Extraer primera línea para logs
            lines = self._row_lines(row)
            first_line = lines[0] if lines else ""
            
            logger.info(
                "PlaywrightDM cache_reopen_attempt thread=%s idx=%d selector=%s first_line=%s",
                thread.id,
                idx,
                row_selector_used,
                first_line[:120]
            )
            
            try:
                row.click()
            except Exception:
                logger.info(
                    "PlaywrightDM cache_row_discard thread=%s idx=%d reason=click_failed first_line=%s",
                    thread.id,
                    idx,
                    first_line[:120]
                )
                continue
            
            # CRÍTICO: Solo considerar éxito si el composer aparece
            opened = self._wait_thread_open(page)
            if not opened:
                logger.info(
                    "PlaywrightDM cache_row_discard thread=%s idx=%d reason=no_composer first_line=%s",
                    thread.id,
                    idx,
                    first_line[:120]
                )
                continue
            
            # Éxito: composer visible
            logger.info(
                "PlaywrightDM cache_thread_open_ok thread=%s idx=%d key=%s first_line=%s",
                thread.id,
                idx,
                thread.id,
                first_line[:120]
            )
            return True
        
        logger.info(
            "PlaywrightDM cache_reopen_fail thread=%s reason=no_match_with_composer total_scanned=%d",
            thread.id,
            total
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
        while time.time() < deadline:
            try:
                messages = self.get_messages(thread, amount=20, log=False)
                for msg in messages:
                    if msg.user_id == self.user_id and _normalize_message_text(msg.text) == target_text:
                        return msg.id
            except Exception:
                pass
            time.sleep(0.5)
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
        # Selectores mínimos para debugging
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


def _proxy_from_account(account: dict) -> Optional[dict]:
    if not account:
        return None
    proxy = account.get("proxy")
    if proxy:
        return proxy
    payload = {
        "url": account.get("proxy_url"),
        "username": account.get("proxy_user"),
        "password": account.get("proxy_pass"),
    }
    try:
        return build_proxy(payload)
    except Exception:
        return None


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

    # Búsqueda de badge por aria-label
    try:
        badge = node.locator("span[aria-label*='unread'], span[aria-label*='sin leer'], span[aria-label*='no leido']")
        if badge.count():
            return 1
    except Exception:
        pass

    # Búsqueda por "punto azul" (visual) - Instagram suele usar un div/span con fondo azul
    # El color rgb(0, 149, 246) es el azul característico de Instagram
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


def _extract_message_timestamp(node) -> Optional[float]:
    time_value = ""
    try:
        time_node = node.locator("time").first
        if time_node.count():
            time_value = (time_node.get_attribute("datetime") or time_node.get_attribute("title") or "").strip()
    except Exception:
        time_value = ""
    if not time_value:
        return None
    return _parse_iso_ts(time_value)


def _parse_iso_ts(value: str) -> Optional[float]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


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
    return re.sub(r"\\s+", " ", (text or "").strip())


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