from __future__ import annotations

import asyncio
import os
import logging
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from playwright.async_api import Locator, Page, TimeoutError as PwTimeoutError

from src.actions.direct_helpers import (
    DmAvailability,
    detect_dm_availability,
    find_profile_dm_button,
)
from src.auth.persistent_login import ensure_logged_in_async
from src.humanizer import random_wait
from src.playwright_service import BASE_PROFILES, PlaywrightService

logger = logging.getLogger(__name__)

INSTAGRAM = "https://www.instagram.com/"
DIRECT_INBOX = f"{INSTAGRAM}direct/inbox/"
DIRECT_NEW = f"{INSTAGRAM}direct/new/"

DIALOG_SELECTOR = "div[role='dialog']"
SEARCH_INPUTS = (
    "div[role='dialog'] input[placeholder*='Search']",
    "div[role='dialog'] input[placeholder*='Buscar']",
    "div[role='dialog'] input[name='queryBox']",
    "input[placeholder='Search...']",
    "input[placeholder='Search']"
)
NEW_MESSAGE_BUTTONS = (
    "div[role='button']:has-text('Send message')",
    "div[role='button']:has-text('Enviar mensaje')",
    "a[href='/direct/new/']",
    "[aria-label='New message']",
    "[aria-label='Nuevo mensaje']",
    "button:has-text('Enviar mensaje')"
)
NEXT_BTNS = (
    "div[role='dialog'] button:has-text('Next')",
    "div[role='dialog'] button:has-text('Siguiente')",
    "button:has-text('Next')",
    "button:has-text('Siguiente')",
    "button:has-text('Chat')",
    "div[role='button']:has-text('Chat')",
    "div[role='button']:has-text('Next')",
    "div[role='button']:has-text('Siguiente')",
)
COMPOSERS = (
    "div[role='textbox'][aria-label='Mensaje']",
    "div[role='textbox'][aria-label='Message']",
    "div[contenteditable='true']",
    "div[role='textbox']",
    "textarea[placeholder*='message']",
    "textarea[placeholder*='Mensaje']",
    "textarea[aria-label*='Message']",
    "textarea[data-testid='message-input']",
    "div[data-testid='message-input']",
    "textarea"
)
SEND_BUTTONS = (
    "div[role='button']:has-text('Send'), "
    "div[role='button']:has-text('Enviar'), "
    "button:has-text('Send'), "
    "button:has-text('Enviar'), "
    "button[aria-label*='Send'], "
    "button[aria-label*='Enviar'], "
    "div[role='button'][aria-label*='Send'], "
    "div[role='button'][aria-label*='Enviar'], "
    "[data-testid='send'], "
    "[data-testid*='send'], "
    "button[type='submit'], "
    "form button[type='submit']"
)
VERIFY_TIMEOUT_S = float(os.getenv("HUMAN_DM_VERIFY_TIMEOUT", "10.0"))
ALLOW_UNVERIFIED = os.getenv("HUMAN_DM_ALLOW_UNVERIFIED", "0").strip().lower() in (
    "1",
    "true",
    "yes",
    "y",
)
NO_DM_SKIP_REASON = "NO_DM_BUTTON"
NO_DM_SKIP_DETAIL = "Perfil sin botón de mensaje / no permite DM"
NO_DM_SKIP_LOG = f"skip | no_dm | {NO_DM_SKIP_DETAIL}"
NO_DM_SEND_METHOD = "skip_no_dm"

EXACT_USER_SKIP_REASON = "EXACT_USER_NOT_FOUND"
EXACT_USER_SKIP_DETAIL = "No se encontró coincidencia exacta para el username"
EXACT_USER_SKIP_LOG = f"skip | exact_match | {EXACT_USER_SKIP_DETAIL}"
EXACT_USER_SEND_METHOD = "skip_exact_match"


class HumanInstagramSender:
    def __init__(self, headless: bool = True) -> None:
        self.headless = headless

    async def _sleep(self, low: float, high: float) -> None:
        await asyncio.sleep(random.uniform(low, high))

    def _normalize_username(self, username: str) -> str:
        return username.strip().lstrip("@").split("?", 1)[0]

    def send_message_like_human_sync(
        self,
        account: Dict,
        target_username: str,
        text: str,
        *,
        base_delay_seconds: float = 0,
        jitter_seconds: float = 0,
        proxy: Optional[Dict] = None,
        return_detail: bool = False,
        return_payload: bool = False,
    ) -> Union[
        bool,
        Tuple[bool, Optional[str]],
        Tuple[bool, Optional[str], Dict[str, Any]],
    ]:
        coro = self.send_message_like_human(
            account,
            target_username,
            text,
            base_delay_seconds=base_delay_seconds,
            jitter_seconds=jitter_seconds,
            proxy=proxy,
            return_detail=return_detail,
            return_payload=return_payload,
        )
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        raise RuntimeError("send_message_like_human_sync requiere contexto sync.")


    async def _goto_inbox(self, page: Page) -> None:
        await page.goto(DIRECT_INBOX, wait_until="domcontentloaded", timeout=45_000)
        try:
            await page.wait_for_selector("nav[role='navigation'], [role='dialog'], a[href='/direct/new/']", timeout=15_000)
        except Exception:
            pass

    async def _dialog_ready(self, page: Page) -> bool:
        return await page.locator(", ".join(SEARCH_INPUTS)).count() > 0

    async def _open_new_message_dialog(self, page: Page) -> bool:
        try:
            await page.goto(DIRECT_NEW, wait_until="domcontentloaded", timeout=45_000)
        except PwTimeoutError:
            pass
        if await self._dialog_ready(page):
            return True

        await self._goto_inbox(page)
        buttons = page.locator(", ".join(NEW_MESSAGE_BUTTONS))
        if await buttons.count() > 0:
            target = buttons.first
            try:
                await target.scroll_into_view_if_needed(timeout=2_000)
            except Exception:
                pass
            await target.click()
            try:
                await page.wait_for_selector(f"{DIALOG_SELECTOR}, {SEARCH_INPUTS}", timeout=15_000)
            except Exception:
                pass
        return await self._dialog_ready(page)

    async def _read_search_value(self, field: Locator) -> str:
        try:
            return await field.input_value() or ""
        except Exception:
            try:
                return await field.evaluate("el => el.value || el.textContent || ''") or ""
            except Exception:
                return ""

    async def _type_search_handle(self, field: Locator, handle: str) -> None:
        try:
            await field.click()
        except Exception:
            pass
        try:
            await field.fill("")
        except Exception:
            try:
                await field.press("Control+A")
                await field.press("Delete")
            except Exception:
                pass
        try:
            await field.type(handle, delay=random.randint(55, 85))
        except Exception:
            try:
                await field.fill(handle)
            except Exception:
                pass
        current = (await self._read_search_value(field)).strip().lower()
        if handle and handle not in current:
            try:
                await field.fill(handle)
            except Exception:
                pass

    async def _search_and_select(self, page: Page, handle: str) -> bool:
        cleaned = handle.strip().lstrip("@")
        if not cleaned:
            return False
        search_locator = page.locator(", ".join(SEARCH_INPUTS))
        if await search_locator.count() == 0:
            return False
        field = search_locator.first
        normalized = cleaned.lower()
        print("debug Escribiendo handle:", normalized)
        await self._type_search_handle(field, normalized)
        typed_value = (await self._read_search_value(field)).strip().lower()
        if normalized not in typed_value:
            logger.info("Busqueda incompleta ('%s' -> '%s'), reintentando.", normalized, typed_value)
            await self._type_search_handle(field, normalized)
        try:
            # Aumentamos espera para que IG cargue resultados 
            await page.wait_for_timeout(3000)
        except Exception:
            pass

        # Estrategia de selección exacta:
        # Buscamos dentro del diálogo de resultados
        results_container = page.locator("div[role='dialog']")
        if await results_container.count() == 0:
            # Fallback a contenedores comunes de búsqueda
            results_container = page.locator("div[aria-label='Nuevo mensaje'], div[aria-label='New message'], main")
            if await results_container.count() == 0:
                results_container = page

        # Buscamos todos los elementos que podrían ser una fila de resultado
        # Generalmente son div[role='button'] o li
        # Limitamos a los primeros 10 resultados para evitar ruido
        rows = results_container.locator("div[role='button'], li")
        try:
            row_count = await rows.count()
        except Exception:
            row_count = 0
        
        selection: Optional[Locator] = None
        logger.debug("Escaneando %d posibles filas de resultados para match exacto con '%s'", row_count, normalized)

        for i in range(min(row_count, 15)):
            row = rows.nth(i)
            try:
                if not await row.is_visible():
                    continue

                # El username suele estar en un span o div
                # Requisito Fase 2: Coincidencia EXACTA (case-insensitive)

                # Obtenemos todos los spans y divs internos que podrían contener el texto
                candidates = row.locator("span, div")
                cand_count = await candidates.count()

                for j in range(cand_count):
                    cand = candidates.nth(j)
                    # Usamos text_content() para evitar ruidos de sub-elementos si los hay,
                    # aunque inner_text() suele ser más "lo que ve el usuario".
                    try:
                        val = (await cand.text_content() or "").strip().lower()
                        if val == normalized:
                            # Encontrado match exacto en un elemento interno
                            selection = row
                            logger.info("Match exacto encontrado en elemento %d de la fila %d: '%s'", j, i, val)
                            break
                    except Exception:
                        continue

                if selection:
                    break

            except Exception as e:
                logger.debug("Error procesando fila %d: %s", i, e)
                continue

        if selection:
            logger.info("Seleccionando usuario con match exacto: %s", normalized)
            try:
                await selection.scroll_into_view_if_needed(timeout=2_000)
                await selection.click()
                return True
            except Exception as e:
                logger.error("Error al clickear el usuario encontrado: %s", e)
                return False

        logger.warning("No se encontró ningún usuario con match exacto para: %s", normalized)
        return False

    async def _confirm_next(self, page: Page) -> bool:
        # 1. Verificar si ya estamos en el chat (si al seleccionar usuario nos llevó directo)
        try:
            if await self._composer(page):
                logger.info("Composer ya visible inmediatamente. Asumiendo éxito.")
                return True
        except: pass

        # 2. Buscar botones de confirmación
        potential_texts = ["Next", "Siguiente", "Chat", "Conversar"]
        
        # Intentar clic en botones estandar
        try:
            nxt = page.locator(", ".join(NEXT_BTNS))
            if await nxt.count() > 0:
                # Filtrar visibilidad
                for i in range(await nxt.count()):
                    if await nxt.nth(i).is_visible():
                        await nxt.nth(i).click()
                        await self._sleep(1, 2)
                        return True
        except Exception as e:
            logger.debug(f"Fallo click selector estandar: {e}")

        # 3. Fuerza bruta: Buscar texto 'Chat'/'Next' arriba a la derecha (común en web)
        logger.info("Buscando botón Next/Chat por texto...")
        for text in potential_texts:
            try:
                # Buscamos botones o divs con ese texto exacto
                el = page.get_by_role("button", name=text).first
                if await el.is_visible():
                    await el.click()
                    await self._sleep(1, 2)
                    return True
                
                # O simplemente texto clickeable
                el_text = page.get_by_text(text, exact=True).first
                if await el_text.is_visible():
                    await el_text.click()
                    await self._sleep(1, 2)
                    return True
            except:
                continue

        # 4. Esperar un poco y verificar composer de nuevo
        try:
            await page.wait_for_selector(", ".join(COMPOSERS), timeout=5000)
            return True
        except:
            pass

        return False

    async def _composer(self, page: Page) -> Optional[Locator]:
        locator = page.locator(", ".join(COMPOSERS))
        try:
            count = await locator.count()
        except Exception:
            count = 0
        if count <= 0:
            return None
        for idx in range(count):
            candidate = locator.nth(idx)
            try:
                if not await candidate.is_visible():
                    continue
            except Exception:
                continue
            return candidate
        return None

    async def _type_text(self, composer: Locator, text: str) -> None:
        await composer.click()
        try:
            await composer.fill("")
        except Exception:
            pass
        payload = text.replace("\r\n", "\n")
        parts = payload.split("\n")
        for idx, part in enumerate(parts):
            if idx > 0:
                await composer.press("Shift+Enter")
                await self._sleep(0.08, 0.25)
            if not part:
                continue
            for ch in part:
                await composer.type(ch, delay=random.randint(30, 120))
            await self._sleep(0.05, 0.2)

    async def _type_and_send(self, page: Page, text: str) -> str:
        try:
            await page.wait_for_selector(", ".join(COMPOSERS), timeout=20_000)
        except Exception:
            pass
        composer = await self._composer(page)
        if composer is None:
            raise RuntimeError("Composer no encontrado.")
        if not text.strip():
            raise ValueError("El mensaje está vacío.")
        await self._type_text(composer, text)
        await self._sleep(0.25, 0.9)
        send_candidates = [
            SEND_BUTTONS,
            "button[aria-label*='Send']",
            "button[aria-label*='Enviar']",
            "div[role='button'][aria-label*='Send']",
            "div[role='button'][aria-label*='Enviar']",
        ]
        clicked = False
        for sel in send_candidates:
            btn = page.locator(sel)
            try:
                count = await btn.count()
            except Exception:
                count = 0
            if count <= 0:
                continue
            for idx in range(min(count, 3)):
                candidate = btn.nth(idx)
                try:
                    if not await candidate.is_visible():
                        continue
                except Exception:
                    continue
                try:
                    await candidate.click()
                    clicked = True
                    break
                except Exception:
                    continue
            if clicked:
                break
        if not clicked:
            await composer.press("Enter")
            await self._sleep(0.25, 0.6)
            try:
                composer_text = await self._composer_text(composer)
            except Exception:
                composer_text = ""
            if composer_text:
                try:
                    await composer.press("Control+Enter")
                except Exception:
                    pass
                await self._sleep(0.25, 0.6)
                for sel in send_candidates:
                    btn = page.locator(sel)
                    try:
                        count = await btn.count()
                    except Exception:
                        count = 0
                    if count <= 0:
                        continue
                    try:
                        await btn.first.click()
                        break
                    except Exception:
                        continue
                return "enter_ctrl_fallback"
            return "enter_fallback"
        await self._sleep(0.3, 1.0)
        return "click"

    async def _composer_text(self, composer: Locator) -> str:
        try:
            value = await composer.input_value()
            if isinstance(value, str):
                return value.strip()
        except Exception:
            pass
        try:
            text = await composer.inner_text()
            if isinstance(text, str):
                return text.strip()
        except Exception:
            pass
        try:
            text = await composer.text_content()
            if isinstance(text, str):
                return text.strip()
        except Exception:
            pass
        return ""

    def _message_snippet(self, text: str, limit: int = 48) -> str:
        for line in (text or "").splitlines():
            cleaned = line.strip()
            if cleaned:
                return cleaned[:limit]
        return (text or "").strip()[:limit]

    async def _confirm_message_sent(
        self,
        page: Page,
        text: str,
        composer: Optional[Locator] = None,
    ) -> tuple[bool, str]:
        snippet = self._message_snippet(text)
        if not snippet:
            return False, "snippet_empty"

        current_url = page.url or ""
        if "accounts/login" in current_url:
            return False, "login_lost"
        if any(token in current_url for token in ("challenge", "checkpoint", "accounts/confirm_email", "two_factor")):
            return False, "challenge_detected"

        toast_selectors = [
            "[role='alert']",
            "[data-testid='toast']",
            "div[role='status']",
        ]
        toast_success = re.compile(r"(sent|enviado|mensaje enviado|message sent)", re.IGNORECASE)
        toast_negative = re.compile(r"(not sent|no se pudo enviar|no enviado)", re.IGNORECASE)

        message_selectors = [
            "[data-testid='own']",
            "[data-testid='message-bubble-outgoing']",
            "div[role='list'] div[role='listitem']",
            "div[role='list'] div[role='row']",
            "div[role='log'] div[role='listitem']",
            "div[role='log'] div[role='row']",
            "div[role='main'] div[role='listitem']",
            "div[role='main'] div[role='row']",
            "div[role='row']",
            "[data-testid='message-bubble']",
        ]

        def _contains_snippet(value: str) -> bool:
            return snippet.lower() in (value or "").lower()

        prefix_len = min(18, len(snippet))
        prefix = snippet[:prefix_len].lower()

        def _contains_prefix(value: str) -> bool:
            if not prefix:
                return False
            return prefix in (value or "").lower()

        deadline = time.time() + VERIFY_TIMEOUT_S
        while time.time() < deadline:
            try:
                await page.wait_for_timeout(500)
            except Exception:
                pass
            for selector in toast_selectors:
                try:
                    toast = page.locator(selector).last
                    if await toast.count() > 0:
                        toast_text = (await toast.inner_text() or "").strip()
                        if toast_text:
                            if toast_negative.search(toast_text):
                                logger.warning("Detalle: Error detectado en toast: %s", toast_text)
                                return False, f"error_toast: {toast_text}"
                            if toast_success.search(toast_text):
                                return True, "toast_sent"
                except Exception:
                    continue
            if composer is not None:
                try:
                    composer_text = await self._composer_text(composer)
                    if composer_text == "":
                        return True, "composer_cleared"
                    if _contains_snippet(composer_text):
                        continue
                except Exception:
                    pass
            for sel in message_selectors:
                try:
                    items = page.locator(sel)
                    count = await items.count()
                    if count > 0:
                        # Revisamos los últimos 5 mensajes para mayor seguridad
                        start = max(0, count - 5)
                        for idx in range(start, count):
                            item = items.nth(idx)
                            try:
                                text_value = await item.inner_text()
                            except Exception:
                                text_value = await item.text_content() or ""

                            if _contains_snippet(text_value) or _contains_prefix(text_value):
                                # Verificación determinística de que es OUTGOING
                                is_own = False
                                try:
                                    if await item.get_attribute("data-testid") == "own":
                                        is_own = True
                                    elif await item.locator("[data-testid='own']").count() > 0:
                                        is_own = True
                                    else:
                                        # Heurística de posición (derecha del thread)
                                        box = await item.bounding_box()
                                        if box:
                                            viewport = page.viewport_size
                                            if viewport and (box['x'] + box['width']/2) > (viewport['width'] / 2):
                                                is_own = True
                                except Exception:
                                    pass

                                if is_own:
                                    return True, "message_present_outgoing"
                                else:
                                    # Si encontramos el texto pero es del otro lado, seguimos buscando
                                    continue

                        matches = items.filter(has_text=snippet)
                        if await matches.count() > 0:
                            # Si el filter encuentra algo, intentamos validar el último match
                            last_match = matches.last
                            box = await last_match.bounding_box()
                            if box:
                                viewport = page.viewport_size
                                if viewport and (box['x'] + box['width']/2) > (viewport['width'] / 2):
                                    return True, "message_present_outgoing_heuristic"
                except Exception:
                    continue

        if composer is not None:
            try:
                if _contains_snippet(await self._composer_text(composer)):
                    return False, "composer_not_cleared"
            except Exception:
                pass
        return False, "message_not_present_after_send"

    async def _capture_success(self, page: Optional[Page], username: str, target: str) -> Optional[str]:
        if page is None:
            return None
        try:
            folder = Path(BASE_PROFILES) / username / "dm_success"
            folder.mkdir(parents=True, exist_ok=True)
            safe_target = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_") or "target"
            ts = int(time.time())
            screenshot_path = folder / f"{safe_target}_{ts}.png"
            await page.screenshot(path=str(screenshot_path))
            return str(screenshot_path)
        except Exception:
            return None

    async def send_message_like_human(
        self,
        account: Dict,
        target_username: str,
        text: str,
        *,
        base_delay_seconds: float = 0,
        jitter_seconds: float = 0,
        proxy: Optional[Dict] = None,
        return_detail: bool = False,
        return_payload: bool = False,
    ) -> Union[
        bool,
        Tuple[bool, Optional[str]],
        Tuple[bool, Optional[str], Dict[str, Any]],
    ]:
        detail: Optional[str] = None
        payload: Dict[str, Any] = {}
        username = account.get("username") or ""
        if not username:
            detail = "Cuenta sin username configurado."
            if return_payload:
                return False, detail, payload
            return (False, detail) if return_detail else False
        logger.info("Engine=playwright_async sender=human account=@%s", username)
        svc: Optional[PlaywrightService] = None
        ctx = None
        page: Optional[Page] = None
        normalized_target = self._normalize_username(target_username)
        if not normalized_target:
            detail = "Lead sin username."
            if return_payload:
                return False, detail, payload
            return (False, detail) if return_detail else False
        delay_total = 0.0
        if base_delay_seconds or jitter_seconds:
            jitter = max(0.0, jitter_seconds)
            delay_total = max(0.0, base_delay_seconds) + random.uniform(0, jitter)
            if delay_total > 0:
                await asyncio.sleep(delay_total)

        strategy = os.getenv("HUMAN_DM_STRATEGY", "auto").strip().lower()
        if strategy not in {"profile", "direct_new", "auto"}:
            strategy = "profile"

        async def _send_via_profile() -> str:
            if page is None:
                raise RuntimeError("Pagina no inicializada.")

            profile_url = f"https://www.instagram.com/{normalized_target}/"
            logger.info("Navegando al perfil directo: %s", profile_url)

            try:
                await page.goto(profile_url, wait_until="domcontentloaded", timeout=45_000)
                await self._sleep(2, 4)
            except Exception as exc:
                logger.warning("Error cargando perfil: %s", exc)

            clicked = False
            click_error: Exception | None = None

            # 1. Buscar botón visible en el perfil
            dm_button = await find_profile_dm_button(page)
            if dm_button is not None:
                try:
                    if await dm_button.is_visible():
                        logger.info("Clickeando botón de mensaje visible en perfil.")
                        await dm_button.click()
                        clicked = True
                except Exception as exc:
                    logger.debug("Fallo click en botón visible: %s", exc)
                    click_error = exc

            # 2. Si no se encontró o no se pudo clickear, buscar en menú de tres puntos (⋯)
            if not clicked:
                logger.info("Botón directo no encontrado o falló. Probando menú de tres puntos (⋯).")
                three_dots_selectors = [
                    "svg[aria-label='More options']",
                    "svg[aria-label='Más opciones']",
                    "svg[aria-label='Options']",
                    "svg[aria-label='Opciones']",
                    "div[role='button']:has(svg[aria-label*='opcion'])",
                    "div[role='button']:has(svg[aria-label*='option'])",
                ]

                three_dots = None
                for sel in three_dots_selectors:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            three_dots = loc
                            break
                    except Exception:
                        continue

                if three_dots:
                    try:
                        await three_dots.click()
                        await self._sleep(1, 2)

                        # Buscar opción de "Enviar mensaje" en el diálogo abierto
                        menu_options = [
                            "span:has-text('Send message')",
                            "span:has-text('Enviar mensaje')",
                            "button:has-text('Send message')",
                            "button:has-text('Enviar mensaje')",
                            "div[role='button']:has-text('Send message')",
                            "div[role='button']:has-text('Enviar mensaje')",
                        ]

                        option_found = False
                        for opt_sel in menu_options:
                            try:
                                opt = page.locator(opt_sel).first
                                if await opt.count() > 0 and await opt.is_visible():
                                    logger.info("Opción 'Enviar mensaje' encontrada en menú de tres puntos.")
                                    await opt.click()
                                    option_found = True
                                    clicked = True
                                    break
                            except Exception:
                                continue

                        if not option_found:
                            # Cerrar el menú si no se encontró la opción (usualmente con Escape)
                            await page.keyboard.press("Escape")
                    except Exception as e:
                        logger.debug("Error interactuando con menú de tres puntos: %s", e)

            # 3. Verificación final de disponibilidad
            if not clicked:
                availability = await detect_dm_availability(page)
                if availability == DmAvailability.NO_DM:
                    logger.info(NO_DM_SKIP_LOG)
                    return NO_DM_SEND_METHOD

                if click_error is not None:
                    logger.debug("Error acumulado clickeando botón de mensaje: %s", click_error)
                raise RuntimeError("No se encontró botón 'Enviar mensaje' ni en el perfil ni en el menú opcional.")

            try:
                await page.wait_for_selector(", ".join(COMPOSERS), timeout=25_000)
            except Exception:
                raise RuntimeError("No aparecio la caja de texto del chat tras clickear 'Enviar mensaje'.")

            return await self._type_and_send(page, text)

        async def _send_via_direct_new() -> str:
            if page is None:
                raise RuntimeError("Pagina no inicializada.")

            if not await self._open_new_message_dialog(page):
                raise RuntimeError("No se pudo abrir el dialogo de nuevo mensaje (/direct/new).")

            if not await self._search_and_select(page, normalized_target):
                logger.info(EXACT_USER_SKIP_LOG)
                return EXACT_USER_SEND_METHOD

            if not await self._confirm_next(page):
                raise RuntimeError("No se pudo abrir el chat (Next/Chat).")

            try:
                await page.wait_for_selector(", ".join(COMPOSERS), timeout=25_000)
            except Exception:
                raise RuntimeError("No aparecio la caja de texto del chat tras abrir el dialogo.")

            return await self._type_and_send(page, text)

        try:
            svc, ctx, page = await ensure_logged_in_async(
                account,
                headless=self.headless,
                proxy=proxy or account.get("proxy"),
            )

            used_strategy = "profile"
            if strategy == "direct_new":
                send_method = await _send_via_direct_new()
                used_strategy = "direct_new"
            elif strategy == "auto":
                try:
                    send_method = await _send_via_direct_new()
                    used_strategy = "direct_new"
                except Exception as exc:
                    logger.warning(
                        "Direct/new fallo para @%s -> @%s: %s. Fallback a perfil.",
                        username,
                        normalized_target,
                        exc,
                    )
                    send_method = await _send_via_profile()
                    used_strategy = "profile"
            else:
                send_method = await _send_via_profile()
                used_strategy = "profile"

            if send_method == NO_DM_SEND_METHOD:
                payload.update(
                    {
                        "engine": "playwright_async",
                        "url": page.url if page else "",
                        "strategy": used_strategy,
                        "send_method": NO_DM_SEND_METHOD,
                        "skip_reason": NO_DM_SKIP_REASON,
                        "skip_detail": NO_DM_SKIP_DETAIL,
                        "skipped": True,
                    }
                )
                if return_payload:
                    return False, NO_DM_SKIP_REASON, payload
                return (False, NO_DM_SKIP_REASON) if return_detail else False

            if send_method == EXACT_USER_SEND_METHOD:
                payload.update(
                    {
                        "engine": "playwright_async",
                        "url": page.url if page else "",
                        "strategy": used_strategy,
                        "send_method": EXACT_USER_SEND_METHOD,
                        "skip_reason": EXACT_USER_SKIP_REASON,
                        "skip_detail": EXACT_USER_SKIP_DETAIL,
                        "skipped": True,
                    }
                )
                if return_payload:
                    return False, EXACT_USER_SKIP_REASON, payload
                return (False, EXACT_USER_SKIP_REASON) if return_detail else False

            composer = await self._composer(page)
            ok, reason = await self._confirm_message_sent(page, text, composer=composer)
            payload["verified"] = bool(ok)
            if not ok and reason in {"message_not_present_after_send", "composer_not_cleared"}:
                try:
                    composer_text = await self._composer_text(composer) if composer else ""
                except Exception:
                    composer_text = ""
                if composer_text:
                    try:
                        await composer.press("Control+Enter")
                        await self._sleep(0.25, 0.6)
                    except Exception:
                        pass
                ok_retry, reason_retry = await self._confirm_message_sent(
                    page,
                    text,
                    composer=composer,
                )
                if ok_retry:
                    ok = True
                    reason = reason_retry
                    payload["verified"] = True
            if not ok:
                # Si llegamos aquí sin 'ok', es que falló la verificación determinística.
                # Para cumplir con el requerimiento de 'no suposiciones', devolvemos False.
                payload["verified"] = False
                payload["verification_reason"] = reason
                detail = reason or "verification_failed"
                payload["detail"] = detail
                logger.warning("Envío no verificado: %s", reason)
                if return_payload:
                    return False, detail, payload
                return (False, detail) if return_detail else False

            storage_path = Path(BASE_PROFILES) / username / "storage_state.json"
            try:
                await svc.save_storage_state(ctx, storage_path)
            except Exception:
                pass
            payload.update(
                {
                    "engine": "playwright_async",
                    "url": page.url if page else "",
                    "strategy": used_strategy,
                    "send_method": send_method,
                    "verified": payload.get("verified", True),
                }
            )
            screenshot = await self._capture_success(page, username, normalized_target)
            if screenshot:
                payload["screenshot"] = screenshot
            logger.info(
                "Mensaje enviado con Playwright (%s): @%s -> @%s",
                used_strategy,
                username,
                normalized_target,
            )
            if return_payload:
                return True, payload.get("detail"), payload
            return (True, payload.get("detail")) if return_detail else True
        except Exception as exc:
            detail = str(exc)
            await self._capture_debug(page, username, normalized_target, detail)
            logger.warning(
                "Fallo envio humano con @%s -> @%s: %s",
                username,
                normalized_target,
                exc,
            )
            if return_payload:
                return False, detail, payload
            return (False, detail) if return_detail else False
        finally:
            # Si quedamos en captcha/suspension o two_factor, dejamos el navegador abierto
            stay_open = False
            try:
                current_url = page.url if page else ""
                if current_url:
                    stay_open = any(
                        token in current_url
                        for token in (
                            "accounts/suspended",
                            "two_factor",
                            "challenge",
                            "checkpoint",
                            "accounts/confirm_email",
                        )
                    )
            except Exception:
                stay_open = False

            if not stay_open:
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass
                if svc is not None:
                    try:
                        await svc.close()
                    except Exception:
                        pass

    async def _capture_debug(self, page: Optional[Page], username: str, target: str, reason: Optional[str]) -> None:
        if page is None:
            return
        try:
            folder = Path(BASE_PROFILES) / username / "dm_errors"
            folder.mkdir(parents=True, exist_ok=True)
            safe_target = re.sub(r"[^a-z0-9]+", "_", target.lower()).strip("_") or "target"
            safe_reason = re.sub(r"[^a-z0-9]+", "_", (reason or "error").lower()).strip("_") or "error"
            ts = int(time.time())
            await page.screenshot(path=str(folder / f"{safe_target}_{safe_reason}_{ts}.png"))
            try:
                html = await page.content()
                html_path = folder / f"{safe_target}_{safe_reason}_{ts}.html"
                html_path.write_text(html, encoding="utf-8")
            except Exception:
                pass
        except Exception:
            pass
