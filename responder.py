# -*- coding: utf-8 -*-  NUEVA VERSION MATI, SI FUNCIONA ESTO!
import base64
import hashlib
import importlib
import getpass
import json
import logging
import re
import random
import subprocess
import sys
import time
import threading
import unicodedata
import uuid
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from accounts import (
    _account_password,
    _store_account_password,
    get_account,
    list_all,
    mark_connected,
)
from config import (
    SETTINGS,
    read_app_config,
    read_env_local,
    refresh_settings,
    update_app_config,
    update_env_local,
)
from proxy_manager import record_proxy_failure, should_retry_proxy
from paths import runtime_base
from runtime import (
    STOP_EVENT,
    ensure_logging,
    request_stop,
    reset_stop_event,
    sleep_with_stop,
    start_q_listener,
)
from storage import get_auto_state, log_conversation_status, save_auto_state
from ui import Fore, full_line, style_text
from src.auth.onboarding import build_proxy, login_account_playwright
from src.auth.persistent_login import check_session
from src.dm_playwright_client import PlaywrightDMClient
from utils import ask, ask_int, banner, ok, press_enter, warn

_ZONEINFO_CLASS_SENTINEL = object()
_ZONEINFO_CLASS: object | None = _ZONEINFO_CLASS_SENTINEL


def _load_zoneinfo_class():
    """Obtiene la clase ZoneInfo desde la stdlib o backports."""

    global _ZONEINFO_CLASS
    if _ZONEINFO_CLASS is _ZONEINFO_CLASS_SENTINEL:
        zoneinfo_class = None
        try:  # pragma: no cover - depende de la versia�n de Python
            from zoneinfo import ZoneInfo as builtin_zoneinfo  # type: ignore[attr-defined]
            zoneinfo_class = builtin_zoneinfo
        except Exception:
            try:  # pragma: no cover - requiere dependencia opcional
                module = importlib.import_module("backports.zoneinfo")
                zoneinfo_class = getattr(module, "ZoneInfo", None)
            except Exception:
                zoneinfo_class = None
        _ZONEINFO_CLASS = zoneinfo_class
    return None if _ZONEINFO_CLASS is None else _ZONEINFO_CLASS

try:  # pragma: no cover - depende de dependencia opcional
    from dateutil import parser as date_parser
    from dateutil import tz as dateutil_tz
except Exception:  # pragma: no cover - fallback si falta dependencia
    date_parser = None  # type: ignore[assignment]
    dateutil_tz = None  # type: ignore[assignment]

try:  # pragma: no cover - depende de dependencia opcional
    import requests
    from requests import RequestException
except Exception:  # pragma: no cover - fallback si requests no esta�
    requests = None  # type: ignore
    RequestException = Exception  # type: ignore

try:  # pragma: no cover - depende de dependencias opcionales
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore
    from google.auth.transport.requests import Request as GoogleAuthRequest  # type: ignore
except Exception:  # pragma: no cover - si faltan dependencias opcionales
    Credentials = None  # type: ignore
    build = None  # type: ignore
    GoogleAuthRequest = None  # type: ignore

try:  # pragma: no cover - depende de dependencias opcionales
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
except Exception:  # pragma: no cover - si falta dependencia opcional
    InstalledAppFlow = None  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = "Responde� cordial, breve y como humano."
PROMPT_KEY = "autoresponder_system_prompt"
ACTIVE_ALIAS: str | None = None
MAX_SYSTEM_PROMPT_CHARS = 50000
_AUTORESPONDER_STUB_WARNED = False


def _safe_parse_datetime(*args, **kwargs) -> Optional[datetime]:
    """Parsea una fecha utilizando dateutil si esta� disponible."""
    if date_parser is None:
        return None
    try:
        return date_parser.parse(*args, **kwargs)
    except Exception:
        return None

_GOHIGHLEVEL_FILE = runtime_base(Path(__file__).resolve().parent) / "storage" / "gohighlevel.json"
_GOHIGHLEVEL_BASE = "https://rest.gohighlevel.com/v1"
_PHONE_PATTERN = re.compile(r"(?<!\d)(?:\a?\d[\d\s().-]{7,}\d)")
_EMAIL_PATTERN = re.compile(r"[A-Z0-9._%a-]a@[A-Z0-9.-]a\.[A-Z]{2,}", re.IGNORECASE)
_GOHIGHLEVEL_STATE: Dict[str, dict] | None = None
_DEFAULT_GOHIGHLEVEL_PROMPT = (
    "Sos un asistente que evala�a conversaciones de Instagram y determina si un lead esta� "
    "calificado para enviarse automa�ticamente al CRM GoHighLevel. Responde� a�nicamente "
    "con 'SI' cuando corresponda enviarlo y 'NO' cuando no cumpla con los criterios. "
    "Considera� el contexto, el intera�s real del lead y si el equipo comercial debera�a "
    "contactarlo."
)

_PROMPT_STORAGE_DIR = runtime_base(Path(__file__).resolve().parent) / "data" / "autoresponder"
_PROMPT_DEFAULT_ALIAS = "default"












_GOOGLE_CALENDAR_FILE = (
    runtime_base(Path(__file__).resolve().parent) / "storage" / "google_calendar.json"
)
_GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"
_DEFAULT_GOOGLE_CALENDAR_PROMPT = ""
_GOOGLE_REDIRECT_URI = "http://localhost"
_GOOGLE_STATE: Dict[str, dict] | None = None
_MEETING_TIME_PATTERN = re.compile(
    r"(?P<hour>\b[01]?\d|2[0-3])(?:(?:[:h\.])(?P<minute>[0-5]\d))?\s*(?P<ampm>am|pm)?\s*(?P<label>hs|hrs|horas)?",
    re.IGNORECASE,
)
_MEETING_DATE_PATTERN = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
    re.IGNORECASE,
)
_RELATIVE_DATE_KEYWORDS = (
    ("hoy", 0),
    ("manana", 1),
    ("pasado manana", 2),
)
_WEEKDAY_KEYWORDS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "mia�rcoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "si�bado": 5,
    "domingo": 6,
}



_GOOGLE_CALENDAR_FILE = (
    runtime_base(Path(__file__).resolve().parent) / "storage" / "google_calendar.json"
)
_GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_GOOGLE_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"
_GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar"
_GOOGLE_STATE: Dict[str, dict] | None = None
_MEETING_TIME_PATTERN = re.compile(
    r"(?P<hour>\b[01]?\d|2[0-3])(?:(?:[:h\.])(?P<minute>[0-5]\d))?\s*(?P<ampm>am|pm)?\s*(?P<label>hs|hrs|horas)?",
    re.IGNORECASE,
)
_MEETING_DATE_PATTERN = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
    re.IGNORECASE,
)
_RELATIVE_DATE_KEYWORDS = (
    ("hoy", 0),
    ("manana", 1),
    ("pasado manana", 2),
)
_WEEKDAY_KEYWORDS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "mia�rcoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "si�bado": 5,
    "domingo": 6,
}

_FOLLOWUP_FILE = (
    runtime_base(Path(__file__).resolve().parent) / "storage" / "followups.json"
)
_FOLLOWUP_STATE: Dict[str, dict] | None = None
_DEFAULT_FOLLOWUP_PROMPT = (
    "Disea�a� un plan de seguimiento amable para leads de Instagram. "
    "Envia� el primer recordatorio cuando hayan pasado al menos 6 horas sin respuesta. "
    "Si despua�s de 12 horas ma�s no responden, envia� un segundo y a�ltimo mensaje. "
    "No enva�es ma�s de dos seguimientos y evita� sonar insistente."
)
_FOLLOWUP_MIN_INTERVAL = 300
_FOLLOWUP_HISTORY_MAX_AGE = 14 * 24 * 3600

_CONVERSATION_ENGINE_FILE = (
    runtime_base(Path(__file__).resolve().parent) / "storage" / "conversation_engine.json"
)
_CONVERSATION_ENGINE_CACHE: Dict[str, dict] | None = None

_MESSAGE_LOG_FILE = (
    runtime_base(Path(__file__).resolve().parent) / "storage" / "message_log.jsonl"
)
_MESSAGE_LOG_LOCK = threading.Lock()

_STAGE_INITIAL = "initial"
_STAGE_FOLLOWUP = "followup"
_STAGE_WAITING = "waiting"
_STAGE_CLOSED = "closed"
_STAGE_ACTIVE = "active"

_MIN_TIME_BETWEEN_MESSAGES = 60
_MIN_TIME_FOR_FOLLOWUP = 4 * 3600
_MIN_TIME_FOR_REACTIVATION = 24 * 3600

# Forzar comportamiento solicitado por el usuario: responder siempre y evaluar seguimientos.
_FORCE_ALWAYS_RESPOND = True
_FORCE_ALWAYS_FOLLOWUP = True


def _normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def _get_conversation_key(account: str, thread_id: str) -> str:
    account_norm = _normalize_username(account)
    thread_str = str(thread_id).strip()
    return f"{account_norm}|{thread_str}"


def _load_conversation_engine(refresh: bool = False) -> Dict[str, dict]:
    global _CONVERSATION_ENGINE_CACHE
    if refresh or _CONVERSATION_ENGINE_CACHE is None:
        data: Dict[str, dict] = {"conversations": {}}
        if _CONVERSATION_ENGINE_FILE.exists():
            try:
                loaded = json.loads(_CONVERSATION_ENGINE_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
                    if "conversations" not in data:
                        data["conversations"] = {}
            except Exception as exc:
                logger.warning("Error cargando conversation_engine.json: %s", exc, exc_info=False)
                data = {"conversations": {}}
        _CONVERSATION_ENGINE_CACHE = data
    return _CONVERSATION_ENGINE_CACHE


def _save_conversation_engine() -> None:
    if _CONVERSATION_ENGINE_CACHE is None:
        return
    try:
        count = len(_CONVERSATION_ENGINE_CACHE.get("conversations", {}))
        print(style_text(f"[Persistencia] Guardando {count} conversaciones en conversation_engine.json...", color=Fore.WHITE))

        # PROBE: Capturar tiempo antes de escribir
        before_ts = time.time()

        _CONVERSATION_ENGINE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CONVERSATION_ENGINE_FILE.write_text(
            json.dumps(_CONVERSATION_ENGINE_CACHE, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # PROBE: Verificar escritura física
        if _CONVERSATION_ENGINE_FILE.exists():
            mtime = _CONVERSATION_ENGINE_FILE.stat().st_mtime
            if mtime >= before_ts - 1: # Un pequeño margen por precisión del FS
                print(style_text(f"[Persistencia] CONFIRMACIÓN: Archivo escrito correctamente (mtime: {mtime})", color=Fore.GREEN))
            else:
                print(style_text(f"[Persistencia] ADVERTENCIA: El archivo existe pero el mtime no se actualizó.", color=Fore.YELLOW))

        print(style_text(f"[Persistencia] Guardado OK. Ruta: {_CONVERSATION_ENGINE_FILE}", color=Fore.GREEN))
    except Exception as exc:
        print(style_text(f"[Persistencia] ERROR CRÍTICO al guardar: {exc}", color=Fore.RED, bold=True))
        logger.warning("Error guardando conversation_engine.json: %s", exc, exc_info=False)


def _append_message_log(event: Dict[str, Any]) -> None:
    record = dict(event or {})
    record.setdefault("ts", int(time.time()))
    record.setdefault("iso", datetime.utcnow().isoformat())
    try:
        payload = json.dumps(record, ensure_ascii=False)
    except Exception:
        return
    try:
        _MESSAGE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _MESSAGE_LOG_LOCK:
            with open(_MESSAGE_LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(f"{payload}\n")
    except Exception:
        logger.debug("No se pudo registrar message_log.jsonl", exc_info=False)

def _get_conversation_state(account: str, thread_id: str) -> Dict[str, Any]:
    engine = _load_conversation_engine()
    conversations = engine.get("conversations", {})
    key = _get_conversation_key(account, thread_id)
    return conversations.get(key, {})


def _update_conversation_state(
    account: str,
    thread_id: str,
    updates: Dict[str, Any],
    recipient_username: Optional[str] = None,
) -> Dict[str, Any]:
    print(style_text(f"[Persistencia] Actualizando estado de thread {thread_id} (recipient: {recipient_username})", color=Fore.WHITE))
    engine = _load_conversation_engine()
    conversations = engine.setdefault("conversations", {})
    key = _get_conversation_key(account, thread_id)
    current = conversations.get(key, {})
    if not current:
        current = {
            "account": account,
            "thread_id": str(thread_id),
            "recipient_username": recipient_username or current.get("recipient_username", ""),
            "stage": _STAGE_INITIAL,
            "messages_sent": [],
            "last_message_sent_at": None,
            "last_message_received_at": None,
            "last_message_id_seen": None,
            "last_message_sender": None,
            "followup_stage": 0,
            "last_followup_sent_at": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
    current.update(updates)
    current["updated_at"] = time.time()
    
    if recipient_username:
        current["recipient_username"] = recipient_username
    
    conversations[key] = current
    _CONVERSATION_ENGINE_CACHE = engine
    _save_conversation_engine()
    return current


def _load_all_conversations_to_memory(
    client,
    account: str,
    max_age_days: int = 7,
    threads_limit: int = 20,
) -> None:
    now = time.time()
    max_age_seconds = max(0, int(max_age_days)) * 24 * 3600 if max_age_days is not None else 0
    print(style_text(f"[Memoria] Cargando conversaciones para @{account}...", color=Fore.CYAN))
    start_ts = time.time()
    # Para Playwright el escaneo es secuencial y por click, aumentamos el tiempo maximo.
    max_seconds = 180
    
    try:
        print(style_text(f"[Memoria] Solicitando threads para @{account}...", color=Fore.CYAN))
        threads = client.list_threads(amount=threads_limit, filter_unread=False)
    except Exception as exc:
        logger.warning("No se pudieron obtener threads para cargar memoria de @%s: %s", account, exc, exc_info=False)
        print(style_text(f"[Memoria] Error obteniendo threads para @{account}", color=Fore.YELLOW))
        return
    
    if not threads:
        print(style_text(f"[Memoria] No hay threads para @{account}", color=Fore.YELLOW))
        return
    
    logger.info("Cargando memoria: analizando %d threads para @%s", len(threads), account)
    loaded_count = 0

    for idx, thread in enumerate(threads):
        if STOP_EVENT.is_set():
            break
        if time.time() - start_ts > max_seconds:
            print(style_text(f"[Memoria] Tiempo maximo alcanzado para @{account}", color=Fore.YELLOW))
            break
        
        thread_id_val = getattr(thread, "id", None) or getattr(thread, "pk", None)
        if thread_id_val is None:
            continue
        thread_id = str(thread_id_val)
        
        try:
            messages = client.get_messages(thread, amount=20)
        except Exception as exc:
            print(style_text(f"[Memoria] ERROR leyendo mensajes de thread {thread_id}: {exc}", color=Fore.RED))
            logger.debug("No se pudieron obtener mensajes del thread %s: %s", thread_id, exc, exc_info=False)
            continue
        
        if not messages:
            print(style_text(f"[Memoria] Thread {thread_id} sin mensajes, saltando...", color=Fore.YELLOW))
            continue
        
        # Obtener información del participante
        participants = getattr(thread, "users", None) or []
        recipient_id: Optional[str] = None
        recipient_username = ""
        for participant in participants:
            pk_val = getattr(participant, "pk", None) or getattr(participant, "id", None)
            pk = str(pk_val) if pk_val is not None else ""
            if pk and pk != str(client.user_id):
                recipient_id = pk
                recipient_username = getattr(participant, "username", None) or pk
                break
        
        if not recipient_id:
            continue
        
        outbound_messages = []
        inbound_messages = []
        
        for msg in messages:
            msg_ts = _message_timestamp(msg)
            if msg_ts is None:
                continue
            
            if max_age_seconds and (now - msg_ts) > max_age_seconds:
                continue
            
            msg_text = getattr(msg, "text", "") or ""
            msg_id = getattr(msg, "id", None)
            
            if _same_user_id(getattr(msg, 'user_id', ''), client.user_id):
                outbound_messages.append({
                    "text": msg_text,
                    "timestamp": msg_ts,
                    "message_id": str(msg_id) if msg_id else "",
                })
            else:
                inbound_messages.append({
                    "text": msg_text,
                    "timestamp": msg_ts,
                    "message_id": str(msg_id) if msg_id else "",
                })
        
        outbound_messages.sort(key=lambda x: x["timestamp"])
        inbound_messages.sort(key=lambda x: x["timestamp"])
        
        conv_state = _get_conversation_state(account, thread_id)
        
        first_sent_message = outbound_messages[0] if outbound_messages else None
        first_sent_at = first_sent_message["timestamp"] if first_sent_message else None
        
        last_received_message = inbound_messages[-1] if inbound_messages else None
        last_received_at = last_received_message["timestamp"] if last_received_message else None
        
        last_sent_message = outbound_messages[-1] if outbound_messages else None
        last_sent_at = last_sent_message["timestamp"] if last_sent_message else None
        
        updates = {
            "recipient_username": recipient_username,
            "first_message_sent_at": first_sent_at,
            "last_message_sent_at": last_sent_at,
            "last_message_received_at": last_received_at,
        }
        
        # No marcar como "visto" durante el cargado inicial de memoria.
        # Esto evita ignorar mensajes reales que aún no fueron respondidos.
        
        if outbound_messages and inbound_messages:
            if last_sent_at and last_received_at:
                updates["last_message_sender"] = "bot" if last_sent_at > last_received_at else "lead"
            elif last_sent_at:
                updates["last_message_sender"] = "bot"
            elif last_received_at:
                updates["last_message_sender"] = "lead"
        elif outbound_messages:
            updates["last_message_sender"] = "bot"
        elif inbound_messages:
            updates["last_message_sender"] = "lead"
        
        messages_sent_in_json = conv_state.get("messages_sent", [])
        for outbound_msg in outbound_messages:
            msg_text = outbound_msg["text"].strip()
            if not msg_text:
                continue
            
            found = False
            for sent_msg in messages_sent_in_json:
                if sent_msg.get("text", "").strip().lower() == msg_text.lower():
                    if first_sent_at and sent_msg.get("first_sent_at") is None:
                        sent_msg["first_sent_at"] = first_sent_at
                    found = True
                    break
            
            if not found and first_sent_at:
                is_first = outbound_msg["timestamp"] == first_sent_at
                if is_first:
                    messages_sent_in_json.append({
                        "text": msg_text,
                        "first_sent_at": first_sent_at,
                        "last_sent_at": outbound_msg["timestamp"],
                        "message_id": outbound_msg["message_id"],
                        "times_sent": 1,
                        "is_followup": False,
                    })
        
        updates["messages_sent"] = messages_sent_in_json
        
        if not outbound_messages:
            updates["stage"] = _STAGE_INITIAL
        elif not inbound_messages:
            updates["stage"] = _STAGE_FOLLOWUP
        elif first_sent_at and last_received_at:
            if last_received_at > first_sent_at:
                if last_received_at > last_sent_at:
                    updates["stage"] = _STAGE_ACTIVE
                else:
                    updates["stage"] = _STAGE_WAITING
            else:
                updates["stage"] = _STAGE_FOLLOWUP
        elif last_received_at and last_sent_at and last_received_at > last_sent_at:
            updates["stage"] = _STAGE_ACTIVE
        elif last_sent_at and last_received_at and last_sent_at > last_received_at:
            updates["stage"] = _STAGE_FOLLOWUP
        else:
            pass
        
        _update_conversation_state(account, thread_id, updates, recipient_username)
        loaded_count += 1
        
        if (idx + 1) % 10 == 0:
            _save_conversation_engine()
            print(style_text(f"[Memoria] Progreso @{account}: {idx + 1}/{len(threads)}", color=Fore.CYAN))
    
    logger.info("Memoria cargada: %d conversaciones sincronizadas para @%s", loaded_count, account)
    _save_conversation_engine()
    print(style_text(f"[Memoria] Listo @{account}: {loaded_count} conversaciones", color=Fore.GREEN))


def _determine_followup_stage_from_initial_message(
    account: str,
    thread_id: str,
    now: float,
    schedule_hours: Optional[List[int]] = None,
) -> tuple[Optional[int], Optional[float]]:
    state = _get_conversation_state(account, thread_id)
    
    first_sent_at = state.get("first_message_sent_at")
    if not first_sent_at:
        messages_sent = state.get("messages_sent", [])
        if messages_sent:
            non_followup_messages = [m for m in messages_sent if not m.get("is_followup", False)]
            if non_followup_messages:
                first_msg = min(non_followup_messages, key=lambda m: m.get("first_sent_at", m.get("last_sent_at", float("inf"))))
                first_sent_at = first_msg.get("first_sent_at") or first_msg.get("last_sent_at")
            else:
                first_msg = min(messages_sent, key=lambda m: m.get("first_sent_at", m.get("last_sent_at", float("inf"))))
                first_sent_at = first_msg.get("first_sent_at") or first_msg.get("last_sent_at")
    
    if not first_sent_at:
        return None, None
    
    last_received_at = state.get("last_message_received_at")
    if last_received_at and last_received_at > first_sent_at:
        return None, None
    
    time_since_initial = now - first_sent_at
    hours_since_initial = time_since_initial / 3600.0
    
    messages_sent = state.get("messages_sent", [])
    followups_sent = [m for m in messages_sent if m.get("is_followup", False)]
    
    followups_sent_sorted = sorted(followups_sent, key=lambda m: m.get("last_sent_at", m.get("first_sent_at", 0)))
    
    num_followups_sent = len(followups_sent_sorted)
    schedule = [h for h in (schedule_hours or []) if isinstance(h, int) and h > 0]
    if schedule:
        schedule = sorted(set(schedule))
        if num_followups_sent >= len(schedule):
            return None, None
        required_hours = schedule[num_followups_sent]
        if hours_since_initial >= required_hours:
            return num_followups_sent + 1, time_since_initial
        return None, None

    if hours_since_initial >= 5:
        if num_followups_sent == 0:
            return 1, time_since_initial
        elif hours_since_initial >= 24 and num_followups_sent == 1:
            return 2, time_since_initial
        elif hours_since_initial >= 48 and num_followups_sent == 2:
            return 3, time_since_initial
        elif hours_since_initial >= 72 and num_followups_sent == 3:
            return 4, time_since_initial
        elif num_followups_sent >= 4:
            return None, None
        else:
            return None, None

    return None, None


def _record_message_sent(
    account: str,
    thread_id: str,
    message_text: str,
    message_id: Optional[str] = None,
    recipient_username: Optional[str] = None,
    is_followup: bool = False,
    followup_stage: Optional[int] = None,
) -> None:
    now = time.time()
    state = _get_conversation_state(account, thread_id)
    
    messages_sent = state.get("messages_sent", [])
    message_normalized = message_text.strip().lower()
    message_found = False
    for sent_msg in messages_sent:
        if sent_msg.get("text", "").strip().lower() == message_normalized:
            sent_msg["last_sent_at"] = now
            sent_msg["times_sent"] = sent_msg.get("times_sent", 0) + 1
            if message_id:
                sent_msg["last_message_id"] = message_id
            if is_followup:
                sent_msg["is_followup"] = True
            message_found = True
            break
    
    if not message_found:
        new_message = {
            "text": message_text,
            "first_sent_at": now,
            "last_sent_at": now,
            "message_id": message_id or "",
            "times_sent": 1,
            "is_followup": is_followup,
        }
        messages_sent.append(new_message)
    
    updates = {
        "messages_sent": messages_sent,
        "last_message_sent_at": now,
        "last_message_sender": "bot",
    }
    
    if is_followup and followup_stage is not None:
        updates["followup_stage"] = followup_stage
        updates["last_followup_sent_at"] = now
    
    _update_conversation_state(
        account,
        thread_id,
        updates,
        recipient_username=recipient_username,
    )
    _append_message_log(
        {
            "action": "message_sent",
            "account": account,
            "thread_id": str(thread_id),
            "lead": recipient_username or "",
            "is_followup": bool(is_followup),
            "followup_stage": followup_stage if is_followup else None,
            "message_id": message_id or "",
            "message_text": message_text,
        }
    )


def _record_message_received(
    account: str,
    thread_id: str,
    message_id: Optional[str] = None,
    recipient_username: Optional[str] = None,
) -> None:
    now = time.time()
    state = _get_conversation_state(account, thread_id)
    
    updates = {
        "last_message_received_at": now,
        "last_message_sender": "lead",
    }
    
    if message_id:
        updates["last_message_id_seen"] = message_id
    
    if state.get("last_message_sent_at") and state.get("stage") in (_STAGE_INITIAL, _STAGE_FOLLOWUP, _STAGE_WAITING):
        updates["stage"] = _STAGE_ACTIVE
        updates["followup_stage"] = 0
    
    _update_conversation_state(account, thread_id, updates, recipient_username=recipient_username)
    _append_message_log(
        {
            "action": "message_received",
            "account": account,
            "thread_id": str(thread_id),
            "lead": recipient_username or "",
            "message_id": message_id or "",
        }
    )


def _determine_conversation_stage(
    account: str,
    thread_id: str,
    has_new_inbound: bool,
    time_since_last_sent: Optional[float],
    time_since_last_received: Optional[float],
) -> str:
    state = _get_conversation_state(account, thread_id)
    current_stage = state.get("stage", _STAGE_INITIAL)
    messages_sent = state.get("messages_sent", [])
    
    if not messages_sent:
        return _STAGE_INITIAL
    
    if has_new_inbound:
        return _STAGE_ACTIVE
    
    if current_stage == _STAGE_CLOSED:
        return _STAGE_CLOSED
    
    if time_since_last_received is None or time_since_last_received > _MIN_TIME_FOR_FOLLOWUP:
        if time_since_last_sent is None or time_since_last_sent > _MIN_TIME_FOR_FOLLOWUP:
            return _STAGE_FOLLOWUP
        return _STAGE_WAITING
    
    if time_since_last_received and time_since_last_received < 3600:
        return _STAGE_ACTIVE
    
    return current_stage


def _can_send_message(
    account: str,
    thread_id: str,
    message_text: str,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        return True, "forced"
    state = _get_conversation_state(account, thread_id)
    now = time.time()
    
    messages_sent = state.get("messages_sent", [])
    message_normalized = message_text.strip().lower()
    
    for sent_msg in messages_sent:
        if sent_msg.get("text", "").strip().lower() == message_normalized:
            last_sent = sent_msg.get("last_sent_at", 0)
            times_sent = sent_msg.get("times_sent", 0)
            
            if now - last_sent < 3600:
                return False, f"Mensaje ya enviado hace {int((now - last_sent) / 60)} minutos"
            
            if times_sent >= 3:
                return False, "Mensaje ya enviado 3 veces, evitar repetición"
    
    last_sent_at = state.get("last_message_sent_at")
    if last_sent_at and not force:
        time_since_last = now - last_sent_at
        if time_since_last < _MIN_TIME_BETWEEN_MESSAGES:
            remaining = int(_MIN_TIME_BETWEEN_MESSAGES - time_since_last)
            return False, f"Esperar {remaining} segundos antes de enviar otro mensaje"
    
    if state.get("stage") == _STAGE_CLOSED:
        return False, "Conversación cerrada, no enviar más mensajes"
    
    return True, "ok"


def _should_process_old_lead(
    account: str,
    thread_id: str,
    time_since_last_activity: float,
) -> bool:
    state = _get_conversation_state(account, thread_id)
    stage = state.get("stage", _STAGE_INITIAL)
    
    if not state.get("messages_sent"):
        return True
    
    if stage == _STAGE_CLOSED:
        return False
    
    if time_since_last_activity > _MIN_TIME_FOR_REACTIVATION:
        return True
    
    if stage == _STAGE_FOLLOWUP and time_since_last_activity > _MIN_TIME_FOR_FOLLOWUP:
        return True
    
    if stage == _STAGE_ACTIVE:
        return True
    
    return False


def _read_followup_state(refresh: bool = False) -> Dict[str, dict]:
    global _FOLLOWUP_STATE
    if refresh or _FOLLOWUP_STATE is None:
        data: Dict[str, dict] = {"aliases": {}}
        if _FOLLOWUP_FILE.exists():
            try:
                loaded = json.loads(_FOLLOWUP_FILE.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data.update(loaded)
            except Exception:
                data = {"aliases": {}}
        aliases = data.get("aliases")
        if not isinstance(aliases, dict):
            data["aliases"] = {}
        _FOLLOWUP_STATE = data
    return _FOLLOWUP_STATE


def _write_followup_state(state: Dict[str, dict]) -> None:
    state.setdefault("aliases", {})
    _FOLLOWUP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FOLLOWUP_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _read_followup_state(refresh=True)


def _followup_prune_history(history: Dict[str, dict]) -> Dict[str, dict]:
    now = time.time()
    cleaned: Dict[str, dict] = {}
    for key, record in history.items():
        if not isinstance(record, dict):
            continue
        last_ts = record.get("last_sent_ts") or record.get("last_eval_ts")
        try:
            ts_value = float(last_ts)
        except Exception:
            ts_value = 0.0
        if ts_value and now - ts_value > _FOLLOWUP_HISTORY_MAX_AGE:
            continue
        cleaned[key] = record
    return cleaned


def _get_followup_entry(alias: str) -> Dict[str, object]:
    state = _read_followup_state()
    key = _normalize_alias_key(alias)
    aliases: Dict[str, dict] = state.get("aliases", {})
    entry = aliases.get(key)
    if not isinstance(entry, dict):
        return {}
    entry.setdefault("alias", alias.strip() or alias)
    entry.setdefault("enabled", False)
    entry.setdefault("accounts", [])
    entry.setdefault("prompt", _DEFAULT_FOLLOWUP_PROMPT)
    entry.setdefault("history", {})
    history = entry.get("history")
    if isinstance(history, dict):
        entry["history"] = _followup_prune_history(history)
    return entry


def _set_followup_entry(alias: str, updates: Dict[str, object]) -> None:
    alias = alias.strip()
    if not alias:
        warn("Alias inva�lido.")
        return
    state = _read_followup_state()
    aliases: Dict[str, dict] = state.setdefault("aliases", {})
    key = _normalize_alias_key(alias)
    entry = aliases.get(key, {})
    entry.setdefault("alias", alias)
    entry.setdefault("prompt", _DEFAULT_FOLLOWUP_PROMPT)
    normalized_updates: Dict[str, object] = {}
    for key_name, value in updates.items():
        if key_name == "accounts":
            normalized: List[str] = []
            if isinstance(value, (list, tuple, set)):
                seen: set[str] = set()
                for raw in value:
                    if not isinstance(raw, str):
                        continue
                    norm = _normalize_username(raw)
                    if norm and norm not in seen:
                        seen.add(norm)
                        normalized.append(norm)
            elif isinstance(value, str):
                norm = _normalize_username(value)
                if norm:
                    normalized = [norm]
            normalized_updates[key_name] = normalized
        elif key_name == "enabled":
            normalized_updates[key_name] = bool(value)
        elif key_name == "prompt":
            normalized_updates[key_name] = str(value or "")
        elif key_name == "history":
            if isinstance(value, dict):
                normalized_updates[key_name] = _followup_prune_history(value)
        else:
            normalized_updates[key_name] = value
    entry.update(normalized_updates)
    aliases[key] = entry
    _write_followup_state(state)


def _followup_status_lines() -> List[str]:
    state = _read_followup_state()
    aliases: Dict[str, dict] = state.get("aliases", {})
    if not aliases:
        return ["(sin configuraciones)"]
    rows: List[str] = []
    for key in sorted(aliases.keys()):
        entry = aliases[key]
        if not isinstance(entry, dict):
            continue
        alias_label = str(entry.get("alias") or key)
        enabled = bool(entry.get("enabled"))
        accounts = entry.get("accounts") or []
        if accounts:
            preview_accounts = [f"@{acc}" for acc in accounts[:3]]
            if len(accounts) > 3:
                preview_accounts.append(f"a{len(accounts) - 3}")
            accounts_label = ", ".join(preview_accounts)
        else:
            accounts_label = "todas las cuentas del alias"
        prompt_preview = _preview_prompt(str(entry.get("prompt") or ""))
        status_label = "Activo" if enabled else "Inactivo"
        rows.append(
            f" - {alias_label}: {status_label} ��� Cuentas: {accounts_label} ��� Prompt: {prompt_preview}"
        )
    return rows


def _followup_summary_line() -> str:
    state = _read_followup_state()
    aliases: Dict[str, dict] = state.get("aliases", {})
    active = [
        str(entry.get("alias") or key)
        for key, entry in aliases.items()
        if isinstance(entry, dict) and entry.get("enabled")
    ]
    if not active:
        return "Seguimiento: (sin configurar)"
    return f"Seguimiento: activo para {', '.join(sorted(active))}"


def _followup_accounts_for_alias(alias: str) -> List[str]:
    targets = _choose_targets(alias)
    normalized = {_normalize_username(user) for user in targets}
    return sorted(account for account in normalized if account)


def _followup_enabled_entry_for(username: str) -> tuple[Optional[str], Dict[str, object]]:
    alias_candidates: List[str] = []
    if ACTIVE_ALIAS:
        alias_candidates.append(ACTIVE_ALIAS)
    account_data = get_account(username) or {}
    account_alias = str(account_data.get("alias") or "").strip()
    if account_alias:
        alias_candidates.append(account_alias)
    alias_candidates.append(username)
    alias_candidates.append("ALL")

    seen: set[str] = set()
    for alias in alias_candidates:
        norm = _normalize_alias_key(alias)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        entry = _get_followup_entry(alias)
        if not entry or not entry.get("enabled"):
            continue
        accounts = entry.get("accounts") or []
        if accounts:
            norm_user = _normalize_username(username)
            if norm_user not in accounts:
                continue
        return alias, entry
    return None, {}


def _followup_allowed_thread_ids(user: str) -> set[str]:
    alias, entry = _followup_enabled_entry_for(user)
    if not alias or not entry or not entry.get('enabled'):
        return set()
    history_source = entry.get('history')
    history = history_source if isinstance(history_source, dict) else {}
    account_norm = _normalize_username(user)
    allowed: set[str] = set()
    prefix = f'{account_norm}|'
    for key in history.keys():
        if not isinstance(key, str):
            continue
        if key.startswith(prefix):
            allowed.add(key.split('|', 1)[1])
    return allowed

def _followup_configure_accounts() -> None:
    banner()
    print(style_text("Seguimiento automa�tico ��� Cuentas", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _followup_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _prompt_alias_selection()
    if not alias:
        return
    available = _followup_accounts_for_alias(alias)
    if not available:
        warn("No se encontraron cuentas activas para ese alias.")
        press_enter()
        return
    entry = _get_followup_entry(alias)
    stored_accounts = set(entry.get("accounts") or [])
    use_all = entry.get("enabled") and not stored_accounts
    print("Selecciona� las cuentas que usara�n seguimiento automa�tico:")
    for idx, account in enumerate(available, start=1):
        selected = use_all or account in stored_accounts
        marker = "[x]" if selected else "[ ]"
        print(f" {idx:>2}) {marker} @{account}")
    print("  0) Todas las cuentas del alias")
    choice = ask(
        "Na�meros separados por coma (vaca�o cancela, 0 = todas): "
    ).strip()
    if not choice:
        warn("No se realizaron cambios.")
        press_enter()
        return
    if choice.lower() in {"0", "todas", "all"}:
        _set_followup_entry(alias, {"accounts": [], "enabled": True})
        ok("Seguimiento habilitado para todas las cuentas del alias.")
        press_enter()
        return
    tokens = re.split(r"[\s,;]a", choice)
    indices: List[int] = []
    for token in tokens:
        if not token:
            continue
        if not token.isdigit():
            continue
        idx = int(token)
        if 1 <= idx <= len(available):
            if idx not in indices:
                indices.append(idx)
    if not indices:
        warn("No se seleccionaron cuentas validas.")
        press_enter()
        return
    selected_accounts = [available[i - 1] for i in indices]
    _set_followup_entry(alias, {"accounts": selected_accounts, "enabled": True})
    ok(f"Seguimiento habilitado para {len(selected_accounts)} cuentas.")
    press_enter()


def _followup_configure_prompt() -> None:
    banner()
    print(style_text("Seguimiento automatico Prompt", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _followup_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _prompt_alias_selection()
    if not alias:
        return
    entry = _get_followup_entry(alias)
    current_prompt = str(entry.get("prompt") or _DEFAULT_FOLLOWUP_PROMPT)
    print(style_text("Prompt actual:", color=Fore.BLUE))
    print(current_prompt.strip() or "(sin definir)")
    print(full_line(color=Fore.BLUE))
    print("Elige una opcian:")
    print("  E) Editar prompt")
    print("  D) Restaurar valor predeterminado")
    print("  Enter) Cancelar")
    action = ask("Accian: ").strip().lower()
    if not action:
        warn("No se realizaron cambios.")
        press_enter()
        return
    if action in {"d", "default", "predeterminado"}:
        _set_followup_entry(alias, {"prompt": _DEFAULT_FOLLOWUP_PROMPT})
        ok("Se restauro el prompt predeterminado de seguimiento.")
        press_enter()
        return
    if action not in {"e", "editar"}:
        warn("Opcian invalida.")
        press_enter()
        return
    print(
        style_text(
            "Pega el nuevo prompt y finaliza con una lanea que diga <<<END>>>.",
            color=Fore.CYAN,
        )
    )
    lines: List[str] = []
    while True:
        line = ask("Ǧ ")
        if line.strip() == "<<<END>>>":
            break
        lines.append(line.replace("\r", ""))
    new_prompt = "\n".join(lines).strip()
    _set_followup_entry(alias, {"prompt": new_prompt})
    if new_prompt:
        ok(f"Prompt actualizado. Longitud: {len(new_prompt)} caracteres.")
    else:
        ok("Se elimino� el prompt personalizado. Se usara� el valor predeterminado.")
    press_enter()


def _followup_disable() -> None:
    banner()
    print(style_text("Seguimiento automa�tico ��� Desactivar", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _followup_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _prompt_alias_selection()
    if not alias:
        return
    entry = _get_followup_entry(alias)
    if not entry or not entry.get("enabled"):
        warn("El seguimiento ya esta� inactivo para ese alias.")
        press_enter()
        return
    _set_followup_entry(alias, {"enabled": False, "history": {}})
    ok("Seguimiento desactivado para ese alias.")
    press_enter()


def _followup_menu() -> None:
    while True:
        banner()
        print(style_text("Seguimiento automa�tico", color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))
        for line in _followup_status_lines():
            print(line)
        print(full_line(color=Fore.BLUE))
        print("1) Configurar cuentas con seguimiento")
        print("2) Configurar prompt de seguimiento")
        print("3) Desactivar seguimiento para un alias")
        print("4) Volver")
        choice = ask("Opcia�n: ").strip()
        if choice == "1":
            _followup_configure_accounts()
        elif choice == "2":
            _followup_configure_prompt()
        elif choice == "3":
            _followup_disable()
        elif choice == "4":
            break
        else:
            warn("Opcia�n inva�lida.")
            press_enter()


def _followup_decision(
    api_key: str,
    prompt_text: str,
    conversation: str,
    metadata: Dict[str, object],
) -> Optional[tuple[str, int]]:
    prompt_text = prompt_text.strip()
    if not prompt_text or not api_key:
        return None
    try:  # pragma: no cover - depende de dependencia externa
        from openai import OpenAI
    except Exception as exc:
        logger.warning(
            "No se pudo importar OpenAI para seguimiento: %s", exc, exc_info=False
        )
        return None
    try:  # pragma: no cover - depende de credenciales externas
        client = OpenAI(api_key=api_key)
    except Exception as exc:
        logger.warning(
            "No se pudo inicializar OpenAI para seguimiento: %s",
            exc,
            exc_info=False,
        )
        return None

    system_prompt = (
        "Sos un asistente que decide si enviar un mensaje de seguimiento en Instagram. "
        "Deba�s seguir estrictamente las reglas provistas y responder SOLO con un objeto "
        "JSON con las claves 'enviar' (booleano), 'mensaje' (texto) y 'etapa' (na�mero entero). "
        "Si no corresponde enviar, devuelve� enviar=false, mensaje=\"\" y usa� la etapa actual."
    )
    context_lines = ["Prompt de seguimiento personalizado:", prompt_text, "", "Contexto:"]
    for key, value in metadata.items():
        context_lines.append(f"- {key}: {value}")
    context_lines.append("")
    context_lines.append("Conversacia�n completa (orden cronola�gico):")
    context_lines.append(conversation)
    user_content = "\n".join(context_lines)

    try:  # pragma: no cover - depende de red externa
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.2,
            max_output_tokens=240,
        )
        raw_text = (response.output_text or "").strip()
    except Exception as exc:
        logger.warning(
            "No se pudo evaluar el seguimiento con OpenAI: %s", exc, exc_info=False
        )
        return None
    if not raw_text:
        return None
    data: Optional[dict] = None
    try:
        data = json.loads(raw_text)
    except Exception:
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return None
    enviar = data.get("enviar")
    if isinstance(enviar, str):
        enviar = enviar.strip().lower() in {"true", "1", "si", "si�", "yes"}
    if not enviar:
        return None
    message = str(data.get("mensaje") or "").strip()
    if not message:
        return None
    etapa_value = data.get("etapa")
    try:
        etapa_int = int(etapa_value)
    except Exception:
        etapa_int = int(metadata.get("seguimientos_previos", 0)) + 1
    etapa_int = max(1, etapa_int)
    return message, etapa_int


def _process_followups(
    client,
    user: str,
    api_key: str,
    delay_min: float = 0.0,
    delay_max: float = 0.0,
    max_age_days: int = 7,
    threads_limit: int = 15,
    followup_schedule_hours: Optional[List[int]] = None,
) -> None:
    _load_all_conversations_to_memory(client, user, max_age_days, threads_limit=threads_limit)
    
    alias, entry = _followup_enabled_entry_for(user)
    if not alias or not entry or not entry.get("enabled"):
        if not _FORCE_ALWAYS_FOLLOWUP:
            return
        alias = alias or ACTIVE_ALIAS or user
        entry = _get_followup_entry(alias) if alias else {}
    prompt_text = str(entry.get("prompt") or _DEFAULT_FOLLOWUP_PROMPT)
    if not prompt_text.strip():
        return
    try:
        threads = client.list_threads(amount=threads_limit, filter_unread=False)
    except Exception as exc:  # pragma: no cover - depende de SDK externo
        logger.debug(
            "No se pudieron obtener hilos para seguimiento de @%s: %s",
            user,
            exc,
            exc_info=False,
        )
        return
    now_ts = time.time()
    max_age_seconds = max(0, int(max_age_days)) * 24 * 3600 if max_age_days is not None else 0
    for thread in threads:
        if STOP_EVENT.is_set():
            break
        thread_id = getattr(thread, "id", None)
        if not thread_id:
            continue
        unread_count = getattr(thread, "unread_count", None)
        try:
            unread_int = int(unread_count)
        except Exception:
            unread_int = 0
        if unread_int > 0:
            continue
        participants = getattr(thread, "users", None)
        recipient_id: Optional[str] = None
        recipient_username = ""
        if isinstance(participants, list):
            for participant in participants:
                pk_val = getattr(participant, "pk", None) or getattr(participant, "id", None)
                pk = str(pk_val) if pk_val is not None else ""
                if pk and pk != str(client.user_id):
                    recipient_id = pk
                    recipient_username = getattr(participant, "username", pk)
                    break
        if not recipient_id:
            continue
        try:
            messages = client.get_messages(thread, amount=20)
        except Exception as exc:  # pragma: no cover - depende de SDK externo
            logger.debug(
                "No se pudieron obtener mensajes del hilo %s para seguimiento: %s",
                thread_id,
                exc,
                exc_info=False,
            )
            continue
        if not messages:
            continue
        latest_ts = None
        for msg in messages:
            msg_ts = _message_timestamp(msg)
            if msg_ts is None:
                continue
            latest_ts = msg_ts if latest_ts is None else max(latest_ts, msg_ts)
        if max_age_seconds and (latest_ts is None or now_ts - latest_ts > max_age_seconds):
            continue
        last_message = messages[0]
        if not _same_user_id(getattr(last_message, 'user_id', ''), client.user_id):
            continue

        def _msg_ts(msg: object) -> Optional[float]:
            ts_obj = getattr(msg, "timestamp", None)
            if isinstance(ts_obj, datetime):
                return ts_obj.timestamp()
            try:
                return float(ts_obj)
            except Exception:
                return None

        last_outbound_ts = _msg_ts(last_message)
        if last_outbound_ts and now_ts - last_outbound_ts < 60:
            continue
        if last_outbound_ts and now_ts - last_outbound_ts < _MIN_TIME_FOR_FOLLOWUP:
            _append_message_log(
                {
                    "action": "followup_skip",
                    "reason": "last_bot_message_too_recent",
                    "account": user,
                    "thread_id": str(thread_id),
                    "lead": recipient_username or str(recipient_id),
                    "seconds_since_last_sent": int(now_ts - last_outbound_ts),
                }
            )
            continue

        outbound_ts_values = [
            _msg_ts(msg)
            for msg in messages
            if _same_user_id(getattr(msg, 'user_id', ''), client.user_id) and _msg_ts(msg) is not None
        ]
        first_outbound_ts = min(outbound_ts_values) if outbound_ts_values else None
        engine_state = _get_conversation_state(user, thread_id)
        if first_outbound_ts is None:
            first_outbound_ts = engine_state.get("first_message_sent_at")

        inbound_messages = [
            msg
            for msg in messages
            if not _same_user_id(getattr(msg, 'user_id', ''), client.user_id) and isinstance(getattr(msg, "text", None), str)
        ]
        has_inbound = bool(inbound_messages)
        last_inbound = inbound_messages[0] if has_inbound else None
        last_inbound_ts = _msg_ts(last_inbound) if last_inbound else None
        if has_inbound:
            if last_inbound_ts and now_ts - last_inbound_ts < 60:
                continue

        try:
            thread_messages = client.get_messages(thread, amount=50)
            if thread_messages:
                outbound = [
                    m for m in thread_messages
                    if _same_user_id(getattr(m, "user_id", ""), client.user_id)
                ]
                inbound = [
                    m for m in thread_messages
                    if not _same_user_id(getattr(m, "user_id", ""), client.user_id)
                ]
                
                if outbound:
                    first_outbound_ts = min(_message_timestamp(m) or now_ts for m in outbound)
                    last_outbound_ts = max(_message_timestamp(m) or now_ts for m in outbound)
                    conv_state = _get_conversation_state(user, thread_id)
                    if not conv_state.get("first_message_sent_at"):
                        _update_conversation_state(user, thread_id, {"first_message_sent_at": first_outbound_ts}, recipient_username)
        except Exception:
            pass
        
        conv_state = _get_conversation_state(user, thread_id)
        last_sent_at = conv_state.get("last_message_sent_at")
        last_received_at = conv_state.get("last_message_received_at")
        last_sender = conv_state.get("last_message_sender")
        current_followup_stage = conv_state.get("followup_stage", 0)
        
        if last_sender == "lead" and last_received_at:
            time_since_last_received = now_ts - last_received_at
            if time_since_last_received < 60:
                continue
        
        followup_stage, time_since_initial = _determine_followup_stage_from_initial_message(
            user, thread_id, now_ts, followup_schedule_hours
        )
        
        if followup_stage is None:
            continue
        
        last_followup_sent_at = conv_state.get("last_followup_sent_at")
        if last_followup_sent_at:
            time_since_last_followup = now_ts - last_followup_sent_at
            if time_since_last_followup < _FOLLOWUP_MIN_INTERVAL:
                continue
            schedule = [h for h in (followup_schedule_hours or []) if isinstance(h, int) and h > 0]
            schedule = sorted(set(schedule))
            if schedule and followup_stage:
                if followup_stage > 1 and len(schedule) >= followup_stage:
                    min_gap_hours = schedule[followup_stage - 1] - schedule[followup_stage - 2]
                    if min_gap_hours > 0 and time_since_last_followup < min_gap_hours * 3600:
                        continue
        
        last_eval_ts = conv_state.get("last_eval_ts")
        if last_eval_ts and now_ts - last_eval_ts < _FOLLOWUP_MIN_INTERVAL:
            continue

        messages_sent = conv_state.get("messages_sent", [])
        followups_sent = len([m for m in messages_sent if m.get("is_followup", False)])
        
        conversation_lines: List[str] = []
        for msg in reversed(messages[:40]):
            text_value = getattr(msg, "text", "") or ""
            prefix = "YO" if _same_user_id(getattr(msg, 'user_id', ''), client.user_id) else "ELLOS"
            conversation_lines.append(f"{prefix}: {text_value}")
        conversation_text = "\n".join(conversation_lines[-40:])

        time_since_last_followup = (now_ts - conv_state.get("last_followup_sent_at")) if conv_state.get("last_followup_sent_at") else None
        time_since_last_sent = (now_ts - last_sent_at) if last_sent_at else None
        time_since_last_received = (now_ts - last_received_at) if last_received_at else None
        first_sent_at = conv_state.get("first_message_sent_at")
        hours_since_initial = (time_since_initial / 3600.0) if time_since_initial else None
        
        metadata = {
            "alias": alias,
            "cuenta_origen": f"@{user}",
            "lead": recipient_username or str(recipient_id),
            "etapa_actual": followup_stage,
            "seguimientos_previos": followups_sent,
            "ultimo_mensaje_de": last_sender or "desconocido",
            "horas_desde_mensaje_inicial": round(hours_since_initial, 1) if hours_since_initial else "desconocido",
            "horas_desde_ultimo_seguimiento": round(time_since_last_followup / 3600.0, 1) if time_since_last_followup else "nunca",
            "horas_desde_ultimo_mensaje_enviado": round(time_since_last_sent / 3600.0, 1) if time_since_last_sent else "nunca",
            "horas_desde_ultima_respuesta": round(time_since_last_received / 3600.0, 1) if time_since_last_received else "desconocido",
            "segundos_desde_mensaje_inicial": int(time_since_initial) if time_since_initial else "desconocido",
            "segundos_desde_ultimo_seguimiento": int(time_since_last_followup) if time_since_last_followup else "nunca",
            "segundos_desde_ultima_respuesta": int(time_since_last_received) if time_since_last_received else "desconocido",
            "segundos_desde_ultimo_mensaje_enviado": int(time_since_last_sent) if time_since_last_sent else "desconocido",
        }
        
        _update_conversation_state(user, thread_id, {"last_eval_ts": now_ts}, recipient_username)
        
        decision = _followup_decision(api_key, prompt_text, conversation_text, metadata)
        if not decision:
            continue
            
        message_text, stage = decision
        
        stage_int = followup_stage
        
        can_send, reason = _can_send_message(
            user,
            thread_id,
            message_text,
            force=_FORCE_ALWAYS_FOLLOWUP,
        )
        if not can_send:
            logger.info(
                "Omitiendo seguimiento para @%s → @%s: %s",
                user,
                recipient_username,
                reason,
            )
            continue

        logger.info(
            "Decision seguimiento @%s thread=%s stage=%s reason=%s",
            user,
            thread_id,
            stage_int,
            reason,
        )
        
        _sleep_between_replies_sync(delay_min, delay_max, label="reply_delay")
        try:
            message_id = client.send_message(thread, message_text)
        except Exception as exc:  # pragma: no cover - depende de SDK externo
            logger.warning(
                "No se pudo enviar seguimiento automatico a %s desde @%s: %s",
                recipient_username or recipient_id,
                user,
                exc,
                exc_info=False,
            )
            continue
        if not message_id:
            logger.warning(
                "Seguimiento no verificado para @%s -> @%s (thread %s)",
                user,
                recipient_username or recipient_id,
                thread_id,
            )
            continue
        
        _record_message_sent(
            user,
            thread_id,
            message_text,
            str(message_id),
            recipient_username,
            is_followup=True,
            followup_stage=stage_int,
        )
        _update_conversation_state(
            user,
            thread_id,
            {
                "stage": _STAGE_FOLLOWUP,
            },
            recipient_username,
        )
        
        logger.info(
            "Seguimiento enviado por @%s → @%s: etapa %d (último mensaje del bot hace %.1f horas)",
            user,
            recipient_username,
            stage_int,
            (now_ts - last_sent_at) / 3600.0 if last_sent_at else 0,
        )
        print(
            style_text(
                f"[Seguimiento] @{user} -> @{recipient_username}: mensaje etapa {stage_int}",
                color=Fore.MAGENTA,
            )
        )

_POSITIVE_KEYWORDS = (
    "si",
    "quiero saber mas",
    "me interesa",
    "interesado",
)
_NEGATIVE_KEYWORDS = (
    "no",
    "ya tengo",
    "no me interesa",
    "no gracias",
)
_INFO_KEYWORDS = (
    "info",
    "informacion",
    "informacia�n",
    "detalle",
    "detalles",
    "precio",
    "costo",
    "mas info",
    "ma�s info",
)
_CALL_KEYWORDS = (
    "agenda",
    "agendar",
    "llamar",
    "llamada",
    "cita",
    "call",
    "reunion",
    "reunia�n",
)
_DEFAULT_LEAD_TAG = "Lead sin clasificar"


def _format_handle(value: str | None) -> str:
    if not value:
        return "@-"
    value = value.strip()
    if value.startswith("@"):
        return value
    return f"@{value}"


def _default_timezone_label() -> str:
    try:
        tz = datetime.now().astimezone().tzinfo
        if tz is None:
            return "UTC"
        key = getattr(tz, "key", None)
        if key:
            return str(key)
        zone = getattr(tz, "zone", None)
        if zone:
            return str(zone)
    except Exception:
        pass
    return "UTC"


def _safe_timezone(label: str):
    zoneinfo_class = _load_zoneinfo_class()
    if zoneinfo_class is not None:
        for candidate in (label, _default_timezone_label(), "UTC"):
            try:
                return zoneinfo_class(candidate)
            except Exception:
                continue
    if dateutil_tz is not None:  # pragma: no cover - depende de dateutil
        for candidate in (label, _default_timezone_label(), "UTC"):
            tzinfo = dateutil_tz.gettz(candidate)
            if tzinfo is not None:
                return tzinfo
    return timezone.utc


def _print_response_summary(
    index: int, sender: str, recipient: str, success: bool, extra: Optional[str] = None
) -> None:
    icon = "ԣ����" if success else "���"
    status = "OK" if success else "ERROR"
    print(
        f"[{icon}] Respuesta {index} | Emisor: {_format_handle(sender)} | "
        f"Receptor: {_format_handle(recipient)} | Estado: {status}"
    )
    if extra:
        print(style_text(extra, color=Fore.GREEN, bold=True))


@contextmanager
def _suppress_console_noise() -> None:
    root = logging.getLogger()
    stream_handlers: list[logging.Handler] = [
        handler
        for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
    ]
    original_levels = [handler.level for handler in stream_handlers]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            for handler in stream_handlers:
                handler.setLevel(logging.CRITICAL + 1)
            yield
        finally:
            for handler, level in zip(stream_handlers, original_levels):
                handler.setLevel(level)


def _normalize_text_for_match(value: str) -> str:
    normalized = unicodedata.normalize("NFD", value.lower())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _safe_unread_count(thread: object) -> Optional[int]:
    unread = getattr(thread, "unread_count", None)
    if unread is None:
        return None
    try:
        return int(unread)
    except Exception:
        return None




def _same_user_id(a: object, b: object) -> bool:
    return str(a) == str(b)


def _message_timestamp(msg: object) -> Optional[float]:
    ts_obj = getattr(msg, "timestamp", None)
    if isinstance(ts_obj, datetime):
        return ts_obj.timestamp()
    try:
        return float(ts_obj)
    except Exception:
        return None


def _random_delay_seconds(delay_min: float, delay_max: float) -> float:
    try:
        min_value = float(delay_min)
    except Exception:
        min_value = 0.0
    try:
        max_value = float(delay_max)
    except Exception:
        max_value = min_value
    if max_value < min_value:
        max_value = min_value
    if max_value <= 0:
        return 0.0
    if min_value <= 0:
        min_value = 0.0
    return random.uniform(min_value, max_value)

def _sleep_between_replies_sync(delay_min: float, delay_max: float, label: str = 'reply_delay') -> None:
    delay = _random_delay_seconds(delay_min, delay_max)
    if delay <= 0:
        return
    logger.info('%s sleep=%.1fs', label, delay)
    sleep_with_stop(delay)

def _parse_followup_schedule_hours(value: str, default: Optional[List[int]] = None) -> List[int]:
    raw = (value or "").strip()
    if not raw:
        return list(default or [])
    parts = [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]
    hours: List[int] = []
    for item in parts:
        try:
            num = int(float(item))
        except Exception:
            continue
        if num > 0:
            hours.append(num)
    if not hours:
        return list(default or [])
    return sorted(set(hours))

def _latest_inbound_message(messages: List[object], client_user_id: object) -> Optional[object]:
    candidates = [msg for msg in messages if getattr(msg, "user_id", None) != client_user_id]
    if not candidates:
        return None
    scored = []
    for idx, msg in enumerate(candidates):
        scored.append((_message_timestamp(msg), idx, msg))
    if any(score[0] is not None for score in scored):
        scored.sort(key=lambda item: ((item[0] is not None), item[0] or 0, item[1]))
        return scored[-1][2]
    return candidates[-1]

def _latest_message(messages: List[object]) -> Optional[object]:
    if not messages:
        return None
    scored = []
    for idx, msg in enumerate(messages):
        scored.append((_message_timestamp(msg), idx, msg))
    if any(score[0] is not None for score in scored):
        scored.sort(key=lambda item: ((item[0] is not None), item[0] or 0, item[1]))
        return scored[-1][2]
    return messages[-1]


def _fetch_inbox_threads(client, amount: int = 10) -> List[object]:
    collected: List[object] = []
    try:
        print(style_text(f"[Barrido] Buscando chats no leídos (limite: {amount})...", color=Fore.WHITE))
        threads = client.list_threads(amount=amount, filter_unread=True)
        if threads:
            collected.extend(threads)
    except TypeError:
        pass
    except Exception:
        pass

    if len(collected) < amount:
        try:
            print(style_text(f"[Barrido] Buscando todos los chats (faltan {amount - len(collected)})...", color=Fore.WHITE))
            threads = client.list_threads(amount=amount, filter_unread=False)
            if threads:
                for t in threads:
                    if not any(getattr(t, 'id', '1') == getattr(c, 'id', '2') for c in collected):
                        collected.append(t)
                        if len(collected) >= amount:
                            break
        except Exception:
            pass
    if not collected:
        return []
    seen_ids: set[str] = set()
    deduped: List[object] = []
    for thread in collected:
        thread_id_val = getattr(thread, "id", None) or getattr(thread, "pk", None)
        if thread_id_val is None:
            # Fallback a ID sintético para threads sin ID (ej: no cargados completamente)
            title = getattr(thread, "title", "unknown")
            synth_id = hashlib.sha1(f"synth|{title}".encode()).hexdigest()[:16]
            thread_id_val = f"stable_{synth_id}"
            setattr(thread, "id", thread_id_val)

        thread_id = str(thread_id_val)
        if thread_id in seen_ids:
            continue
        seen_ids.add(thread_id)
        deduped.append(thread)
        if len(deduped) >= amount:
            break
    return deduped


def _contains_token(text: str, token: str) -> bool:
    token = token.strip()
    if not token:
        return False
    if " " in token:
        return token in text
    return (
        text == token
        or text.startswith(token + " ")
        or text.endswith(" " + token)
        or f" {token} " in text
    )


def _classify_response(message: str) -> str | None:
    norm = _normalize_text_for_match(message)
    if not norm:
        return None
    for keyword in _POSITIVE_KEYWORDS:
        if _contains_token(norm, keyword):
            return "Interesado"
    for keyword in _NEGATIVE_KEYWORDS:
        if _contains_token(norm, keyword):
            return "No interesado"
    return None


def _resolve_username(client, thread, target_user_id: str) -> str:
    target = str(target_user_id) if target_user_id is not None else ""
    client_id = str(getattr(client, "user_id", "") or "")
    try:
        participants = getattr(thread, "users", []) or []
        if participants:
            for participant in participants:
                pk = getattr(participant, "pk", None) or getattr(participant, "id", None)
                pk_str = str(pk) if pk is not None else ""
                if pk_str and pk_str == target:
                    username = getattr(participant, "username", None)
                    if username:
                        return username
            for participant in participants:
                pk = getattr(participant, "pk", None) or getattr(participant, "id", None)
                pk_str = str(pk) if pk is not None else ""
                username = getattr(participant, "username", None)
                if username and pk_str and pk_str != client_id:
                    return username
            for participant in participants:
                username = getattr(participant, "username", None)
                if username:
                    return username
    except Exception:
        pass
    title = getattr(thread, "title", None)
    if title:
        return str(title)
    return "unknown"


@dataclass
class BotStats:
    alias: str
    responded: int = 0
    errors: int = 0
    responses: int = 0
    accounts: set[str] = field(default_factory=set)

    def _bump_responses(self, account: str) -> int:
        self.responses += 1
        self.accounts.add(account)
        return self.responses

    def record_success(self, account: str) -> int:
        index = self._bump_responses(account)
        self.responded += 1
        return index

    def record_response_error(self, account: str) -> int:
        index = self._bump_responses(account)
        self.errors += 1
        return index

    def record_error(self, account: str) -> None:
        self.errors += 1
        self.accounts.add(account)


def _playwright_storage_state_path(username: str) -> Path:
    return PlaywrightDMClient.storage_state_path(username)


def _proxy_payload_for_playwright(account: Optional[Dict]) -> Optional[Dict[str, str]]:
    if not account:
        return None
    if account.get("proxy"):
        return account.get("proxy")
    payload = {
        "url": account.get("proxy_url"),
        "username": account.get("proxy_user"),
        "password": account.get("proxy_pass"),
    }
    try:
        return build_proxy(payload)
    except Exception:
        return None


def _has_playwright_session(username: str, *, account: Optional[Dict] = None) -> bool:
    if not username:
        return False
    proxy = _proxy_payload_for_playwright(account)
    try:
        ok, reason = check_session(username, proxy=proxy, headless=True)
    except Exception as exc:
        logger.warning("Playwright session check failed for @%s: %s", username, exc)
        return False
    logger.info("Playwright session check for @%s: %s (%s)", username, ok, reason)
    return bool(ok)


def _prompt_playwright_login(username: str, *, alias: Optional[str] = None) -> bool:
    account = get_account(username)
    if not account:
        warn("No existe la cuenta indicada.")
        return False

    stored_password = _account_password(account).strip()
    if not stored_password:
        stored_password = getpass.getpass(f"Password @{username}: ")
    if not stored_password:
        warn("Se cancelo el inicio de sesion.")
        return False

    payload = dict(account)
    payload["username"] = username
    payload["password"] = stored_password
    try:
        result = login_account_playwright(payload, alias or username, headful=True)
    except Exception as exc:
        warn(f"No se pudo iniciar login con Playwright: {exc}")
        return False

    ok = (result.get("status") == "ok")
    if ok:
        try:
            _store_account_password(username, stored_password)
        except Exception:
            pass
    return ok


def _client_for(username: str):
    account = get_account(username)
    if not account:
        raise RuntimeError(f"No se encontro la cuenta {username}.")
    logger.info("autoresponder_dm_engine=playwright account=@%s", username)
    client = PlaywrightDMClient(account=account, headless=False)
    try:
        client.ensure_ready()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        mark_connected(username, False)
        raise
    mark_connected(username, True)
    return client


def _ensure_session(username: str) -> bool:
    try:
        account = get_account(username)
        return _has_playwright_session(username, account=account)
    except Exception:
        return False


def _gen_response(api_key: str, system_prompt: str, convo_text: str) -> str:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        msg = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": convo_text},
            ],
            temperature=0.6,
            max_output_tokens=180,
        )
        return (msg.output_text or "").strip() or "Gracias por tu mensaje ���� -aCa�mo te puedo ayudar?"
    except Exception as e:  # pragma: no cover - depende de red externa
        logger.warning("Fallo al generar respuesta con OpenAI: %s", e, exc_info=False)
        return "Gracias por tu mensaje ���� -aCa�mo te puedo ayudar?"


def _choose_targets(alias: str) -> list[str]:
    accounts_data = list_all()
    alias_key = alias.lstrip("@")
    alias_lower = alias_key.lower()

    if alias.upper() == "ALL":
        candidates = [a["username"] for a in accounts_data if a.get("active")]
    else:
        alias_matches = [
            a for a in accounts_data if a.get("alias", "").lower() == alias_lower and a.get("active")
        ]
        if alias_matches:
            candidates = [a["username"] for a in alias_matches]
        else:
            username_matches = [
                a for a in accounts_data if a.get("username", "").lower() == alias_lower and a.get("active")
            ]
            if username_matches:
                candidates = [username_matches[0]["username"]]
            else:
                candidates = [alias_key]

    seen = set()
    deduped: list[str] = []
    for user in candidates:
        norm = user.lstrip("@")
        if norm not in seen:
            seen.add(norm)
            deduped.append(norm)
    return deduped


def _filter_valid_sessions(targets: list[str], *, alias: Optional[str] = None) -> list[str]:
    verified: list[str] = []
    needing_login: list[tuple[str, str]] = []
    for user in targets:
        account = get_account(user)
        if not account:
            needing_login.append((user, "cuenta no encontrada"))
            continue
        if not _playwright_storage_state_path(user).exists():
            needing_login.append((user, "sin sesion guardada"))
            continue
        if not _ensure_session(user):
            needing_login.append((user, "sesion expirada"))
            continue
        verified.append(user)

    if needing_login:
        remaining: list[tuple[str, str]] = []
        for user, reason in needing_login:
            remaining.append((user, reason))

        if remaining:
            print("\nLas siguientes cuentas necesitan volver a iniciar sesion:")
            for user, reason in remaining:
                print(f" - @{user}: {reason}")
            if ask("-aIniciar sesion ahora? (s/N): ").strip().lower() == "s":
                for user, _ in remaining:
                    if _prompt_playwright_login(user, alias=alias) and _ensure_session(user):
                        if user not in verified:
                            verified.append(user)
            else:
                warn("Se omitieron las cuentas sin sesion valida.")
    return verified



def _mask_key(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 6:
        return value[:2] + "�Ǫ"
    return f"{value[:4]}�Ǫ{value[-2:]}"


def _system_prompt_file(alias: str | None = None) -> Path:
    alias_key = (alias or _PROMPT_DEFAULT_ALIAS).strip() or _PROMPT_DEFAULT_ALIAS
    safe_alias = re.sub(r"[^a-z0-9_.-]", "_", alias_key.lower())
    return _PROMPT_STORAGE_DIR / safe_alias / "system_prompt.txt"


def _normalize_system_prompt_text(value: str) -> str:
    if not value:
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _read_system_prompt_from_file(alias: str | None = None) -> str | None:
    path = _system_prompt_file(alias)
    if not path.exists():
        return None
    try:
        return _normalize_system_prompt_text(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("No se pudo leer %s: %s", path, exc, exc_info=False)
        return None


def _persist_system_prompt(prompt: str, alias: str | None = None) -> str:
    normalized = _normalize_system_prompt_text(prompt)
    path = _system_prompt_file(alias)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized, encoding="utf-8")
    except Exception as exc:
        logger.warning("No se pudo escribir %s: %s", path, exc, exc_info=False)
    try:
        update_app_config({PROMPT_KEY: normalized})
    except Exception as exc:
        logger.warning("No se pudo actualizar el system prompt en config: %s", exc, exc_info=False)
    return normalized


def _load_preferences() -> tuple[str, str]:
    env_values = read_env_local()
    api_key = env_values.get("OPENAI_API_KEY") or SETTINGS.openai_api_key or ""
    config_values = read_app_config()
    prompt = _read_system_prompt_from_file() or config_values.get(PROMPT_KEY, "") or ""
    prompt = _normalize_system_prompt_text(prompt) or DEFAULT_PROMPT
    return api_key, prompt


def _read_state_json(path: Path, default: Dict[str, dict]) -> Dict[str, dict]:
    data: Dict[str, dict] = dict(default)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except Exception:
            data = dict(default)
    return data


def _write_state_json(path: Path, data: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ensure_alias_container(data: Dict[str, dict]) -> Dict[str, dict]:
    if "aliases" not in data or not isinstance(data["aliases"], dict):
        data["aliases"] = {}
    return data


def _read_gohighlevel_state(refresh: bool = False) -> Dict[str, dict]:
    global _GOHIGHLEVEL_STATE
    if refresh or _GOHIGHLEVEL_STATE is None:
        data = _read_state_json(_GOHIGHLEVEL_FILE, {"aliases": {}})
        _GOHIGHLEVEL_STATE = _ensure_alias_container(data)
    return _GOHIGHLEVEL_STATE


def _write_gohighlevel_state(state: Dict[str, dict]) -> None:
    state.setdefault("aliases", {})
    _write_state_json(_GOHIGHLEVEL_FILE, state)
    _read_gohighlevel_state(refresh=True)


def _read_google_calendar_state(refresh: bool = False) -> Dict[str, dict]:
    global _GOOGLE_STATE
    if refresh or _GOOGLE_STATE is None:
        data = _read_state_json(_GOOGLE_CALENDAR_FILE, {"aliases": {}})
        _GOOGLE_STATE = _ensure_alias_container(data)
    return _GOOGLE_STATE


def _write_google_calendar_state(state: Dict[str, dict]) -> None:
    state.setdefault("aliases", {})
    _write_state_json(_GOOGLE_CALENDAR_FILE, state)
    _read_google_calendar_state(refresh=True)


def _normalize_key(value: str) -> str:
    return value.strip().lower()


def _normalize_alias_key(alias: str) -> str:
    return _normalize_key(alias)


def _normalize_lead_id(lead: str) -> str:
    return _normalize_key(lead)


def _sanitize_location_ids(raw: object) -> List[str]:
    if raw is None:
        return []
    tokens: List[str] = []
    if isinstance(raw, str):
        parts = re.split(r"[\s,;]a", raw)
        tokens = [part.strip() for part in parts if part.strip()]
    elif isinstance(raw, (list, tuple, set)):
        for item in raw:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    tokens.append(value)
    else:
        try:
            iterable = list(raw)  # type: ignore[arg-type]
        except Exception:
            iterable = []
        for item in iterable:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    tokens.append(value)
    seen: set[str] = set()
    cleaned: List[str] = []
    for token in tokens:
        norm = token.strip()
        if not norm:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        cleaned.append(norm)
    return cleaned


def _get_gohighlevel_entry(alias: str) -> Dict[str, dict]:
    state = _read_gohighlevel_state()
    key = _normalize_alias_key(alias)
    aliases: Dict[str, dict] = state.get("aliases", {})
    entry = aliases.get(key)
    if isinstance(entry, dict):
        entry.setdefault("alias", alias.strip())
        entry.setdefault("sent", {})
        entry.setdefault("qualify_prompt", _DEFAULT_GOHIGHLEVEL_PROMPT)
        if "location_ids" in entry:
            entry["location_ids"] = _sanitize_location_ids(entry.get("location_ids"))
        return entry
    return {}


def _set_gohighlevel_entry(alias: str, updates: Dict[str, object]) -> None:
    alias = alias.strip()
    if not alias:
        warn("Alias inva�lido.")
        return
    state = _read_gohighlevel_state()
    aliases: Dict[str, dict] = state.setdefault("aliases", {})
    key = _normalize_alias_key(alias)
    entry = aliases.get(key, {})
    entry.setdefault("alias", alias)
    entry.setdefault("sent", {})
    normalized_updates: Dict[str, object] = {}
    for key, value in updates.items():
        if value is None:
            continue
        if key == "location_ids":
            normalized_updates[key] = _sanitize_location_ids(value)
        else:
            normalized_updates[key] = value
    entry.update(normalized_updates)
    aliases[key] = entry
    _write_gohighlevel_state(state)


def _get_google_calendar_entry(alias: str) -> Dict[str, object]:
    state = _read_google_calendar_state()
    key = _normalize_alias_key(alias)
    aliases: Dict[str, dict] = state.get("aliases", {})
    entry = aliases.get(key)
    if isinstance(entry, dict):
        entry.setdefault("alias", alias.strip())
        entry.setdefault("scheduled", {})
        entry.setdefault("event_name", "{{username}} - Sistema de adquisicia�n con IA")
        entry.setdefault("duration_minutes", 30)
        entry.setdefault("timezone", _default_timezone_label())
        entry.setdefault("auto_meet", True)
        entry.setdefault("schedule_prompt", _DEFAULT_GOOGLE_CALENDAR_PROMPT)
        return entry
    return {}


def _set_google_calendar_entry(alias: str, updates: Dict[str, object]) -> None:
    alias = alias.strip()
    if not alias:
        warn("Alias inva�lido.")
        return
    state = _read_google_calendar_state()
    aliases: Dict[str, dict] = state.setdefault("aliases", {})
    key = _normalize_alias_key(alias)
    entry = aliases.get(key, {})
    entry.setdefault("alias", alias)
    entry.setdefault("scheduled", {})
    entry.setdefault("event_name", "{{username}} - Sistema de adquisicia�n con IA")
    entry.setdefault("duration_minutes", 30)
    entry.setdefault("timezone", _default_timezone_label())
    entry.setdefault("auto_meet", True)
    entry.setdefault("schedule_prompt", _DEFAULT_GOOGLE_CALENDAR_PROMPT)
    normalized_updates: Dict[str, object] = {}
    for key_name, value in updates.items():
        if value is None:
            continue
        if key_name == "duration_minutes":
            try:
                normalized_updates[key_name] = max(5, int(value))
            except Exception:
                continue
        elif key_name == "timezone":
            try:
                tz_value = str(value).strip() or _default_timezone_label()
                _ = _safe_timezone(tz_value)
                normalized_updates[key_name] = tz_value
            except Exception:
                warn("Zona horaria inva�lida; se mantiene el valor previo.")
                continue
        elif key_name == "schedule_prompt":
            normalized_updates[key_name] = str(value)
        else:
            normalized_updates[key_name] = value
    entry.update(normalized_updates)
    aliases[key] = entry
    _write_google_calendar_state(state)


def _mask_google_calendar_status(entry: Dict[str, object]) -> str:
    connected = bool(entry.get("connected"))
    enabled = bool(entry.get("enabled"))
    status = "���� Activo" if connected and enabled else "���� Conectado" if connected else "�ܬ Inactivo"
    summary = entry.get("event_name") or "(sin nombre)"
    tz_label = entry.get("timezone") or "UTC"
    return f"{status} ��� Evento: {summary} ��� TZ: {tz_label}"


def _google_calendar_status_lines() -> List[str]:
    state = _read_google_calendar_state()
    aliases: Dict[str, dict] = state.get("aliases", {})
    if not aliases:
        return ["(sin configuraciones)"]
    rows: List[str] = []
    for key in sorted(aliases.keys()):
        entry = aliases[key]
        label = str(entry.get("alias") or key)
        rows.append(f" - {label}: {_mask_google_calendar_status(entry)}")
    return rows


def _google_calendar_summary_line() -> str:
    state = _read_google_calendar_state()
    aliases: Dict[str, dict] = state.get("aliases", {})
    enabled_aliases = [
        entry
        for entry in aliases.values()
        if isinstance(entry, dict) and entry.get("connected") and entry.get("enabled")
    ]
    if not enabled_aliases:
        configured_aliases = [
            entry
            for entry in aliases.values()
            if isinstance(entry, dict) and entry.get("connected")
        ]
        if configured_aliases:
            labels = sorted(str(entry.get("alias") or "?") for entry in configured_aliases)
            return f"Google Calendar: conectado para {', '.join(labels)} (inactivo)"
        return "Google Calendar: (sin configurar)"
    labels = sorted(str(entry.get("alias") or "?") for entry in enabled_aliases)
    return f"Google Calendar: activo para {', '.join(labels)}"


def _google_calendar_candidate_keys(lead: str, phone: str) -> list[str]:
    normalized_lead = _normalize_lead_id(lead)
    normalized_phone = _normalize_phone(phone)
    keys = [f"{normalized_lead}|{normalized_phone}"]
    if normalized_lead:
        keys.append(f"{normalized_lead}|")
    if normalized_phone:
        keys.append(f"|{normalized_phone}")
    keys.append("||")
    seen: set[str] = set()
    ordered: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def _google_calendar_share_link_from_event_id(event_id: str) -> str:
    event_id = str(event_id or "").strip()
    if not event_id:
        return ""
    if "@" in event_id:
        event_id = event_id.split("@", 1)[0]
    sanitized = re.sub(r"[^A-Za-z0-9_\-]", "", event_id)
    if not sanitized:
        return ""
    return f"https://calendar.app.google/{sanitized}"


def _google_calendar_preferred_link(link: Optional[str]) -> str:
    if not link:
        return ""
    try:
        parsed = urlparse(str(link))
    except Exception:
        return str(link)
    host = parsed.netloc.lower()
    if host.startswith("calendar.app.google"):
        return str(link)
    if "google.com" not in host:
        return str(link)
    query = parse_qs(parsed.query)
    eid_values = query.get("eid") or []
    eid = next((value for value in eid_values if value), "")
    if not eid:
        return str(link)
    eid = eid.strip()
    if not eid:
        return str(link)
    padding = "=" * (-len(eid) % 4)
    try:
        decoded = base64.urlsafe_b64decode((eid + padding).encode("ascii", "ignore"))
    except Exception:
        return str(link)
    try:
        decoded_text = decoded.decode("utf-8", "ignore").strip()
    except Exception:
        decoded_text = ""
    if not decoded_text:
        return str(link)
    event_id = decoded_text.split()[0].strip()
    if not event_id:
        return str(link)
    if any(ch for ch in event_id if ord(ch) < 33):
        return str(link)
    share_link = _google_calendar_share_link_from_event_id(event_id)
    if share_link:
        return share_link
    return str(link)


def _google_calendar_mark_scheduled(
    alias: str,
    lead: str,
    phone: str,
    event_id: str,
    link: str | None,
    start_iso: str,
) -> None:
    entry = _get_google_calendar_entry(alias)
    scheduled = entry.setdefault("scheduled", {})
    candidate_keys = _google_calendar_candidate_keys(lead, phone)
    canonical_key = candidate_keys[0]
    existing_key = None
    for key in candidate_keys:
        if key in scheduled:
            existing_key = key
            break
    if existing_key and existing_key != canonical_key:
        scheduled.pop(existing_key, None)
    scheduled[canonical_key] = {
        "event_id": event_id,
        "link": _google_calendar_preferred_link(link) if link else "",
        "start": start_iso,
        "ts": int(time.time()),
    }
    _set_google_calendar_entry(alias, {"scheduled": scheduled})


def _google_calendar_already_scheduled(alias: str, lead: str, phone: str) -> bool:
    entry = _get_google_calendar_entry(alias)
    scheduled = entry.get("scheduled") or {}
    for key in _google_calendar_candidate_keys(lead, phone):
        if key in scheduled:
            return True
    return False


def _google_calendar_get_scheduled(
    alias: str, lead: str, phone: str
) -> tuple[Optional[Dict[str, object]], Optional[str]]:
    entry = _get_google_calendar_entry(alias)
    scheduled = entry.get("scheduled") or {}
    for key in _google_calendar_candidate_keys(lead, phone):
        data = scheduled.get(key)
        if isinstance(data, dict):
            return data, key
    return None, None


def _google_calendar_token_is_valid(entry: Dict[str, object]) -> bool:
    expires_at = entry.get("token_expires_at")
    try:
        expires_float = float(expires_at)
    except Exception:
        return False
    return expires_float - time.time() > 60


def _google_calendar_store_tokens(
    alias: str, entry: Dict[str, object], token_data: Dict[str, object]
) -> Dict[str, object]:
    access_token = token_data.get("access_token") or entry.get("access_token")
    refresh_token = token_data.get("refresh_token") or entry.get("refresh_token")
    token_type = token_data.get("token_type") or entry.get("token_type")
    expires_in = token_data.get("expires_in")
    if isinstance(expires_in, str) and expires_in.isdigit():
        expires_in = int(expires_in)
    if not isinstance(expires_in, (int, float)):
        expires_in = 3600
    expires_at = time.time() + float(expires_in)
    updated = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_type,
        "token_expires_at": expires_at,
        "connected": bool(access_token and refresh_token),
    }
    _set_google_calendar_entry(alias, updated)
    entry.update(updated)
    return entry


def _google_calendar_refresh_access_token(
    alias: str, entry: Dict[str, object]
) -> Optional[str]:
    if requests is None and (Credentials is None or build is None):
        return None
    refresh_token = entry.get("refresh_token")
    client_id = entry.get("client_id")
    client_secret = entry.get("client_secret")
    if not refresh_token or not client_id:
        return None
    data = {
        "client_id": client_id,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        response = requests.post(_GOOGLE_TOKEN_URL, data=data, timeout=15)
    except RequestException as exc:  # pragma: no cover - depende de red externa
        logger.warning("No se pudo refrescar el token de Google Calendar: %s", exc, exc_info=False)
        return None
    if response.status_code != 200:
        logger.warning(
            "Respuesta inesperada al refrescar token de Google Calendar: %s", response.text
        )
        return None
    token_data = response.json()
    entry = _google_calendar_store_tokens(alias, entry, token_data)
    return entry.get("access_token")


def _google_calendar_update_tokens_from_credentials(
    alias: str, entry: Dict[str, object], creds: object
) -> None:
    token = getattr(creds, "token", None)
    if not token:
        return
    refresh_token = getattr(creds, "refresh_token", None) or entry.get("refresh_token")
    expiry = getattr(creds, "expiry", None)
    expires_in = 3600
    if expiry is not None:
        try:
            expiry_dt = expiry
            if isinstance(expiry_dt, str):
                parsed = _safe_parse_datetime(expiry_dt)
                if parsed is not None:
                    expiry_dt = parsed
            if isinstance(expiry_dt, datetime):
                if expiry_dt.tzinfo is None:
                    expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
                delta = expiry_dt - datetime.now(timezone.utc)
                expires_in = max(60, int(delta.total_seconds()))
        except Exception:
            expires_in = 3600
    token_payload = {
        "access_token": token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
    }
    _google_calendar_store_tokens(alias, entry, token_payload)


def _google_calendar_credentials_from_entry(
    alias: str, entry: Dict[str, object]
) -> Optional[object]:
    if Credentials is None:
        return None
    access_token = entry.get("access_token")
    refresh_token = entry.get("refresh_token")
    client_id = entry.get("client_id")
    if not access_token or not refresh_token or not client_id:
        return None
    try:
        creds = Credentials(
            token=str(access_token),
            refresh_token=str(refresh_token),
            token_uri=_GOOGLE_TOKEN_URL,
            client_id=str(client_id),
            client_secret=str(entry.get("client_secret") or "") or None,
            scopes=[_GOOGLE_SCOPE],
        )
    except Exception as exc:  # pragma: no cover - depende de librera�as externas
        logger.warning(
            "No se pudieron preparar credenciales de Google Calendar: %s",
            exc,
            exc_info=False,
        )
        return None
    if not getattr(creds, "valid", False) and getattr(creds, "refresh_token", None):
        if GoogleAuthRequest is None:
            return creds
        try:
            creds.refresh(GoogleAuthRequest())  # type: ignore[misc]
            _google_calendar_update_tokens_from_credentials(alias, entry, creds)
        except Exception as exc:  # pragma: no cover - depende de red/creds
            logger.warning(
                "No se pudo refrescar credenciales de Google Calendar via google-auth: %s",
                exc,
                exc_info=False,
            )
            return None
    return creds


def _google_calendar_create_event_via_service(
    alias: str,
    entry: Dict[str, object],
    payload: Dict[str, object],
) -> Optional[Dict[str, object]]:
    if build is None or Credentials is None:
        return None
    creds = _google_calendar_credentials_from_entry(alias, entry)
    if not creds:
        return None
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        logger.warning(
            "No se pudo inicializar el cliente de Google Calendar: %s",
            exc,
            exc_info=False,
        )
        return None
    kwargs: Dict[str, object] = {}
    if "conferenceData" in payload:
        kwargs["conferenceDataVersion"] = 1
    try:
        event = (
            service.events()  # type: ignore[call-arg]
            .insert(calendarId="primary", body=payload, **kwargs)
            .execute()
        )
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        logger.warning(
            "Error al crear evento de Google Calendar mediante googleapiclient: %s",
            exc,
            exc_info=False,
        )
        return None
    _google_calendar_update_tokens_from_credentials(alias, entry, creds)
    if isinstance(event, dict):
        return event
    return None


def _google_calendar_create_event_via_requests(
    alias: str,
    entry: Dict[str, object],
    payload: Dict[str, object],
    params: Dict[str, object],
    access_token: Optional[str],
) -> Optional[Dict[str, object]]:
    if requests is None and (Credentials is None or build is None):
        return None
    token_value = access_token or entry.get("access_token")
    if not token_value:
        return None
    headers = {
        "Authorization": f"Bearer {token_value}",
        "Content-Type": "application/json",
    }
    url = f"{_GOOGLE_CALENDAR_BASE}/calendars/primary/events"
    try:
        response = requests.post(  # type: ignore[call-arg]
            url,
            headers=headers,
            json=payload,
            params=params or None,
            timeout=20,
        )
    except RequestException as exc:  # pragma: no cover - depende de red externa
        logger.warning("No se pudo crear el evento en Google Calendar: %s", exc, exc_info=False)
        return None
    if response.status_code == 401:
        new_token = _google_calendar_refresh_access_token(alias, entry)
        if not new_token:
            return None
        headers["Authorization"] = f"Bearer {new_token}"
        try:
            response = requests.post(  # type: ignore[call-arg]
                url,
                headers=headers,
                json=payload,
                params=params or None,
                timeout=20,
            )
        except RequestException as exc:  # pragma: no cover - depende de red externa
            logger.warning(
                "No se pudo crear el evento en Google Calendar tras refrescar token: %s",
                exc,
                exc_info=False,
            )
            return None
    if response.status_code not in {200, 201}:
        logger.warning(
            "Respuesta inesperada al crear evento de Google Calendar (%s): %s",
            response.status_code,
            response.text,
        )
        return None
    try:
        data = response.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        return data
    return None


def _google_calendar_fetch_event_via_service(
    alias: str,
    entry: Dict[str, object],
    event_id: str,
) -> Optional[Dict[str, object]]:
    if build is None or Credentials is None:
        return None
    creds = _google_calendar_credentials_from_entry(alias, entry)
    if not creds:
        return None
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        logger.warning(
            "No se pudo inicializar el cliente de Google Calendar para leer evento: %s",
            exc,
            exc_info=False,
        )
        return None
    try:
        event = (
            service.events()  # type: ignore[call-arg]
            .get(calendarId="primary", eventId=event_id)
            .execute()
        )
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        logger.warning(
            "Error al obtener evento de Google Calendar mediante googleapiclient: %s",
            exc,
            exc_info=False,
        )
        return None
    _google_calendar_update_tokens_from_credentials(alias, entry, creds)
    if isinstance(event, dict):
        return event
    return None


def _google_calendar_fetch_event_via_requests(
    alias: str,
    entry: Dict[str, object],
    event_id: str,
    access_token: Optional[str],
) -> Optional[Dict[str, object]]:
    if requests is None and (Credentials is None or build is None):
        return None
    token_value = access_token or entry.get("access_token")
    if not token_value:
        return None
    headers = {
        "Authorization": f"Bearer {token_value}",
        "Content-Type": "application/json",
    }
    params = {"fields": "id,htmlLink,hangoutLink,conferenceData,start"}
    url = f"{_GOOGLE_CALENDAR_BASE}/calendars/primary/events/{event_id}"
    try:
        response = requests.get(  # type: ignore[call-arg]
            url,
            headers=headers,
            params=params,
            timeout=20,
        )
    except RequestException as exc:  # pragma: no cover - depende de red externa
        logger.warning(
            "No se pudo leer el evento en Google Calendar: %s",
            exc,
            exc_info=False,
        )
        return None
    if response.status_code == 401:
        new_token = _google_calendar_refresh_access_token(alias, entry)
        if not new_token:
            return None
        headers["Authorization"] = f"Bearer {new_token}"
        try:
            response = requests.get(  # type: ignore[call-arg]
                url,
                headers=headers,
                params=params,
                timeout=20,
            )
        except RequestException as exc:  # pragma: no cover - depende de red externa
            logger.warning(
                "No se pudo leer el evento en Google Calendar tras refrescar token: %s",
                exc,
                exc_info=False,
            )
            return None
    if response.status_code != 200:
        logger.warning(
            "Respuesta inesperada al obtener evento de Google Calendar (%s): %s",
            response.status_code,
            response.text,
        )
        return None
    try:
        data = response.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        return data
    return None


def _google_calendar_fetch_event(
    alias: str,
    entry: Dict[str, object],
    event_id: str,
    access_token: Optional[str],
) -> Optional[Dict[str, object]]:
    event = _google_calendar_fetch_event_via_service(alias, entry, event_id)
    if event:
        return event
    return _google_calendar_fetch_event_via_requests(alias, entry, event_id, access_token)


def _google_calendar_create_event(
    alias: str,
    entry: Dict[str, object],
    payload: Dict[str, object],
    params: Dict[str, object],
    access_token: Optional[str],
) -> Optional[Dict[str, object]]:
    event = _google_calendar_create_event_via_service(alias, entry, payload)
    if event:
        return event
    return _google_calendar_create_event_via_requests(alias, entry, payload, params, access_token)


def _google_calendar_update_event_via_service(
    alias: str,
    entry: Dict[str, object],
    event_id: str,
    payload: Dict[str, object],
    params: Dict[str, object],
) -> Optional[Dict[str, object]]:
    if build is None or Credentials is None:
        return None
    creds = _google_calendar_credentials_from_entry(alias, entry)
    if not creds:
        return None
    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        logger.warning(
            "No se pudo inicializar el cliente de Google Calendar para actualizar evento: %s",
            exc,
            exc_info=False,
        )
        return None
    kwargs: Dict[str, object] = {}
    if params.get("conferenceDataVersion"):
        kwargs["conferenceDataVersion"] = params["conferenceDataVersion"]
    try:
        event = (
            service.events()  # type: ignore[call-arg]
            .patch(calendarId="primary", eventId=event_id, body=payload, **kwargs)
            .execute()
        )
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        logger.warning(
            "Error al actualizar evento de Google Calendar mediante googleapiclient: %s",
            exc,
            exc_info=False,
        )
        return None
    _google_calendar_update_tokens_from_credentials(alias, entry, creds)
    if isinstance(event, dict):
        return event
    return None


def _google_calendar_update_event_via_requests(
    alias: str,
    entry: Dict[str, object],
    event_id: str,
    payload: Dict[str, object],
    params: Dict[str, object],
    access_token: Optional[str],
) -> Optional[Dict[str, object]]:
    if requests is None and (Credentials is None or build is None):
        return None
    token_value = access_token or entry.get("access_token")
    if not token_value:
        return None
    headers = {
        "Authorization": f"Bearer {token_value}",
        "Content-Type": "application/json",
    }
    url = f"{_GOOGLE_CALENDAR_BASE}/calendars/primary/events/{event_id}"
    try:
        response = requests.patch(  # type: ignore[call-arg]
            url,
            headers=headers,
            json=payload,
            params=params or None,
            timeout=20,
        )
    except RequestException as exc:  # pragma: no cover - depende de red externa
        logger.warning(
            "No se pudo actualizar el evento en Google Calendar: %s",
            exc,
            exc_info=False,
        )
        return None
    if response.status_code == 401:
        new_token = _google_calendar_refresh_access_token(alias, entry)
        if not new_token:
            return None
        headers["Authorization"] = f"Bearer {new_token}"
        try:
            response = requests.patch(  # type: ignore[call-arg]
                url,
                headers=headers,
                json=payload,
                params=params or None,
                timeout=20,
            )
        except RequestException as exc:  # pragma: no cover - depende de red externa
            logger.warning(
                "No se pudo actualizar el evento en Google Calendar tras refrescar token: %s",
                exc,
                exc_info=False,
            )
            return None
    if response.status_code not in {200}:  # 200 OK al actualizar
        if response.status_code == 404:
            logger.info(
                "El evento de Google Calendar no existe; se creara� uno nuevo. (%s)",
                event_id,
            )
            return None
        logger.warning(
            "Respuesta inesperada al actualizar evento de Google Calendar (%s): %s",
            response.status_code,
            response.text,
        )
        return None
    try:
        data = response.json()
    except Exception:
        data = {}
    if isinstance(data, dict):
        return data
    return None


def _google_calendar_update_event(
    alias: str,
    entry: Dict[str, object],
    event_id: str,
    payload: Dict[str, object],
    params: Dict[str, object],
    access_token: Optional[str],
) -> Optional[Dict[str, object]]:
    event = _google_calendar_update_event_via_service(alias, entry, event_id, payload, params)
    if event:
        return event
    return _google_calendar_update_event_via_requests(alias, entry, event_id, payload, params, access_token)


def _google_calendar_ensure_token(alias: str, entry: Dict[str, object]) -> Optional[str]:
    access_token = entry.get("access_token")
    if access_token and _google_calendar_token_is_valid(entry):
        return str(access_token)
    return _google_calendar_refresh_access_token(alias, entry)


def _google_calendar_enabled_entry_for(username: str) -> tuple[Optional[str], Dict[str, object]]:
    alias_candidates: List[str] = []
    if ACTIVE_ALIAS:
        alias_candidates.append(ACTIVE_ALIAS)
    account = get_account(username) or {}
    account_alias = str(account.get("alias") or "").strip()
    if account_alias:
        alias_candidates.append(account_alias)
    alias_candidates.append(username)
    alias_candidates.append("ALL")

    seen: set[str] = set()
    for alias in alias_candidates:
        norm = _normalize_alias_key(alias)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        entry = _get_google_calendar_entry(alias)
        if entry.get("connected") and entry.get("enabled"):
            access_token = entry.get("access_token")
            refresh_token = entry.get("refresh_token")
            if access_token and refresh_token:
                return alias, entry
    return None, {}


def _google_calendar_lead_qualifies(
    entry: Dict[str, object],
    conversation: str,
    status: Optional[str],
    phone_numbers: List[str],
    meeting_dt: datetime,
    api_key: Optional[str],
) -> bool:
    prompt_text = str(entry.get("schedule_prompt") or "").strip()
    if not prompt_text:
        return True
    if not api_key:
        return True
    try:  # pragma: no cover - depende de dependencia externa
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - entorno sin openai
        logger.warning(
            "No se pudo importar OpenAI para evaluar Google Calendar: %s",
            exc,
            exc_info=False,
        )
        return True
    try:  # pragma: no cover - depende de credenciales externas
        client = OpenAI(api_key=api_key)
    except Exception as exc:
        logger.warning(
            "No se pudo inicializar OpenAI para evaluar Google Calendar: %s",
            exc,
            exc_info=False,
        )
        return True

    system_prompt = (
        prompt_text
        + "\n\nResponde a�nicamente con 'SI' o 'NO' indicando si se debe crear un evento en Google Calendar."
    )
    context_lines = [
        f"Estado detectado: {status or 'desconocido'}",
        "Tela�fonos detectados: "
        + (", ".join(phone_numbers) if phone_numbers else "(sin tela�fono)"),
        f"Fecha/hora detectada: {meeting_dt.isoformat()}",
        "Conversacia�n completa:",
        conversation,
    ]
    user_content = "\n".join(context_lines)
    try:  # pragma: no cover - depende de red externa
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_output_tokens=20,
        )
        decision = (response.output_text or "").strip().lower()
    except Exception as exc:  # pragma: no cover - depende de red externa
        logger.warning(
            "No se pudo evaluar el criterio de Google Calendar con OpenAI: %s",
            exc,
            exc_info=False,
        )
        return True

    normalized = _normalize_text_for_match(decision)
    return normalized.startswith("s")


def _mask_gohighlevel_status(entry: Dict[str, object]) -> str:
    api_key = str(entry.get("api_key") or "")
    enabled = bool(entry.get("enabled"))
    status = "���� Activo" if enabled else "�ܬ Inactivo"
    location_ids = _sanitize_location_ids(entry.get("location_ids"))
    locations_text = (
        f"{len(location_ids)} Location ID(s)"
        if location_ids
        else "Location IDs: (sin definir)"
    )
    return (
        f"{status} ��� API Key: {_mask_key(api_key) or '(sin definir)'}"
        f" ��� {locations_text}"
    )


def _gohighlevel_status_lines() -> List[str]:
    state = _read_gohighlevel_state()
    aliases: Dict[str, dict] = state.get("aliases", {})
    if not aliases:
        return ["(sin configuraciones)"]
    rows: List[str] = []
    for key in sorted(aliases.keys()):
        entry = aliases[key]
        label = str(entry.get("alias") or key)
        rows.append(f" - {label}: {_mask_gohighlevel_status(entry)}")
    return rows


def _gohighlevel_summary_line() -> str:
    state = _read_gohighlevel_state()
    aliases: Dict[str, dict] = state.get("aliases", {})
    if not aliases:
        return "GoHighLevel: (sin configurar)"
    active = sum(1 for entry in aliases.values() if entry.get("enabled"))
    configured = sum(1 for entry in aliases.values() if entry.get("api_key"))
    return f"GoHighLevel: {active} activos / {configured} configurados"


def _gohighlevel_mark_sent(alias: str, lead: str, phone: str) -> None:
    state = _read_gohighlevel_state()
    aliases: Dict[str, dict] = state.setdefault("aliases", {})
    key = _normalize_alias_key(alias)
    entry = aliases.setdefault(key, {"alias": alias.strip(), "sent": {}})
    entry.setdefault("sent", {})
    entry["sent"][_normalize_lead_id(lead)] = {"phone": phone, "ts": int(time.time())}
    aliases[key] = entry
    _write_gohighlevel_state(state)


def _gohighlevel_already_sent(alias: str, lead: str, phone: str) -> bool:
    entry = _get_gohighlevel_entry(alias)
    sent: Dict[str, dict] = entry.get("sent", {})  # type: ignore[assignment]
    record = sent.get(_normalize_lead_id(lead))
    if not isinstance(record, dict):
        return False
    stored_phone = str(record.get("phone") or "")
    return bool(stored_phone) and stored_phone == phone


def _gohighlevel_enabled_entry_for(username: str) -> tuple[Optional[str], Dict[str, object]]:
    alias_candidates: List[str] = []
    if ACTIVE_ALIAS:
        alias_candidates.append(ACTIVE_ALIAS)
    account = get_account(username) or {}
    account_alias = str(account.get("alias") or "").strip()
    if account_alias:
        alias_candidates.append(account_alias)
    alias_candidates.append(username)
    alias_candidates.append("ALL")

    seen: set[str] = set()
    for alias in alias_candidates:
        norm = _normalize_alias_key(alias)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        entry = _get_gohighlevel_entry(alias)
        api_key = str(entry.get("api_key") or "")
        if api_key and entry.get("enabled"):
            return alias, entry
    return None, {}


def _gohighlevel_lead_qualifies(
    entry: Dict[str, object],
    conversation: str,
    status: Optional[str],
    phone_numbers: List[str],
    api_key: Optional[str],
) -> bool:
    prompt_text = str(entry.get("qualify_prompt") or "").strip()
    if not prompt_text:
        return True
    if not api_key:
        return True
    try:  # pragma: no cover - depende de dependencia externa
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - entorno sin openai
        logger.warning(
            "No se pudo importar OpenAI para evaluar GoHighLevel: %s", exc, exc_info=False
        )
        return True
    try:  # pragma: no cover - depende de credenciales externas
        client = OpenAI(api_key=api_key)
    except Exception as exc:
        logger.warning(
            "No se pudo inicializar OpenAI para evaluar GoHighLevel: %s",
            exc,
            exc_info=False,
        )
        return True

    system_prompt = (
        prompt_text
        + "\n\nResponde a�nicamente con 'SI' o 'NO' indicando si se debe enviar el lead a GoHighLevel."
    )
    context_lines = [
        f"Estado detectado: {status or 'desconocido'}",
        "Tela�fonos detectados: "
        + (", ".join(phone_numbers) if phone_numbers else "(sin tela�fono)"),
        "Conversacia�n completa:",
        conversation,
    ]
    user_content = "\n".join(context_lines)
    try:  # pragma: no cover - depende de red externa
        response = client.responses.create(
            model="gpt-4o-mini",
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_output_tokens=20,
        )
        decision = (response.output_text or "").strip().lower()
    except Exception as exc:  # pragma: no cover - depende de red externa
        logger.warning(
            "No se pudo evaluar el criterio de GoHighLevel con OpenAI: %s",
            exc,
            exc_info=False,
        )
        return True

    normalized = _normalize_text_for_match(decision)
    return normalized.startswith("s")


def _require_requests() -> bool:
    if requests is None:  # pragma: no cover - entorno sin dependencia
        warn("La librera�a 'requests' no esta� disponible. Instala�la para usar GoHighLevel.")
        press_enter()
        return False
    return True


def _gohighlevel_select_alias() -> Optional[str]:
    alias = _prompt_alias_selection()
    if not alias:
        warn("Alias inva�lido.")
        press_enter()
        return None
    return alias


def _gohighlevel_configure_key() -> None:
    banner()
    print(style_text("GoHighLevel ��� Configurar API Key", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _gohighlevel_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _gohighlevel_select_alias()
    if not alias:
        return
    current = _get_gohighlevel_entry(alias)
    print(f"Actual: {_mask_key(str(current.get('api_key') or '')) or '(sin definir)'}")
    new_key = ask("Ingresi� la API Key de GoHighLevel (vaca�o para cancelar): ").strip()
    if not new_key:
        warn("No se modifica� la API Key de GoHighLevel.")
        press_enter()
        return
    _set_gohighlevel_entry(alias, {"api_key": new_key})
    ok(f"API Key guardada para {alias}.")
    press_enter()


def _gohighlevel_configure_locations() -> None:
    banner()
    print(style_text("GoHighLevel ��� Configurar Location IDs", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _gohighlevel_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _gohighlevel_select_alias()
    if not alias:
        return
    entry = _get_gohighlevel_entry(alias)
    current_ids = _sanitize_location_ids(entry.get("location_ids"))
    if current_ids:
        print("Actual:")
        for idx, value in enumerate(current_ids, start=1):
            print(f" {idx}) {value}")
    else:
        print("Actual: (sin definir)")
    print()
    prompt = (
        "Ingresi� uno o ma�s Location IDs (separados por coma o espacio).\n"
        "Escriba� 'eliminar N' para borrar uno especa�fico (usa el na�mero de la lista),\n"
        "'limpiar' para eliminar todos o deja� vaca�o para cancelar: "
    )
    raw = ask(prompt).strip()
    if not raw:
        warn("No se modificaron los Location IDs.")
        press_enter()
        return
    if raw.lower().startswith("eliminar"):
        if not current_ids:
            warn("No hay Location IDs para eliminar.")
            press_enter()
            return
        indexes = [token for token in re.split(r"[^0-9]a", raw) if token.isdigit()]
        if not indexes:
            warn("Indica� el na�mero del Location ID a eliminar.")
            press_enter()
            return
        to_remove: set[int] = set()
        for token in indexes:
            try:
                idx = int(token)
            except ValueError:
                continue
            if 1 <= idx <= len(current_ids):
                to_remove.add(idx - 1)
        if not to_remove:
            warn("Los na�meros indicados no coinciden con Location IDs existentes.")
            press_enter()
            return
        remaining = [value for idx, value in enumerate(current_ids) if idx not in to_remove]
        _set_gohighlevel_entry(alias, {"location_ids": remaining})
        ok(
            "Se eliminaron los Location IDs seleccionados. Total restante: "
            f"{len(remaining)}"
        )
        press_enter()
        return
    if raw.lower() in {"limpiar", "clear", "ninguno", "eliminar", "borrar"}:
        _set_gohighlevel_entry(alias, {"location_ids": []})
        ok(f"Se eliminaron los Location IDs para {alias}.")
        press_enter()
        return
    location_ids = _sanitize_location_ids(raw)
    if not location_ids:
        warn("No se detectaron Location IDs va�lidos.")
        press_enter()
        return
    _set_gohighlevel_entry(alias, {"location_ids": location_ids})
    ok(f"Location IDs guardados para {alias}. Total: {len(location_ids)}")
    press_enter()


def _gohighlevel_configure_prompt() -> None:
    banner()
    print(style_text("GoHighLevel ��� Criterios de enva�o", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _gohighlevel_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _gohighlevel_select_alias()
    if not alias:
        return
    entry = _get_gohighlevel_entry(alias)
    current_prompt = str(entry.get("qualify_prompt") or _DEFAULT_GOHIGHLEVEL_PROMPT)
    print(style_text("Prompt actual:", color=Fore.BLUE))
    print(current_prompt or "(sin definir)")
    print(full_line(color=Fore.BLUE))
    print("Elige� una opcia�n:")
    print("  E) Editar prompt")
    print("  D) Restaurar prompt predeterminado")
    print("  Enter) Cancelar")
    action = ask("Accia�n: ").strip().lower()
    if not action:
        warn("No se modifica� el prompt de calificacia�n.")
        press_enter()
        return
    if action in {"d", "default", "predeterminado"}:
        _set_gohighlevel_entry(alias, {"qualify_prompt": _DEFAULT_GOHIGHLEVEL_PROMPT})
        ok("Se restauro� el prompt predeterminado para GoHighLevel.")
        press_enter()
        return
    if action not in {"e", "editar"}:
        warn("Opcia�n inva�lida. No se modifica� el prompt de calificacia�n.")
        press_enter()
        return
    print(
        style_text(
            "Pega� el nuevo prompt y finaliza� con una la�nea que diga <<<END>>>."
            " Deja� vaca�o para cancelar.",
            color=Fore.CYAN,
        )
    )
    lines: List[str] = []
    while True:
        line = ask("�Ǧ ")
        if line.strip() == "<<<END>>>":
            break
        lines.append(line.replace("\r", ""))
    new_prompt = "\n".join(lines).strip()
    if not new_prompt:
        warn("No se modifica� el prompt de calificacia�n.")
        press_enter()
        return
    _set_gohighlevel_entry(alias, {"qualify_prompt": new_prompt})
    ok(f"Prompt actualizado. Longitud: {len(new_prompt)} caracteres.")
    press_enter()


def _gohighlevel_activate() -> None:
    if not _require_requests():
        return
    banner()
    print(style_text("GoHighLevel ��� Activar enva�o automa�tico", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _gohighlevel_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _gohighlevel_select_alias()
    if not alias:
        return
    entry = _get_gohighlevel_entry(alias)
    api_key = str(entry.get("api_key") or "")
    if not api_key:
        warn("Configura� la API Key antes de activar la conexia�n.")
        press_enter()
        return
    _set_gohighlevel_entry(alias, {"enabled": True})
    ok(f"Conexia�n GoHighLevel activada para {alias}.")
    press_enter()


def _gohighlevel_deactivate() -> None:
    banner()
    print(style_text("GoHighLevel ��� Desactivar conexia�n", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _gohighlevel_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _gohighlevel_select_alias()
    if not alias:
        return
    _set_gohighlevel_entry(alias, {"enabled": False})
    ok(f"Conexia�n GoHighLevel desactivada para {alias}.")
    press_enter()


def _gohighlevel_menu() -> None:
    while True:
        banner()
        print(style_text("Conectar con GoHighLevel", color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))
        for line in _gohighlevel_status_lines():
            print(line)
        print(full_line(color=Fore.BLUE))
        print("1) Ingresar API Key de GoHighLevel")
        print("2) Configurar Location IDs de GoHighLevel")
        print("3) Activar el enva�o automa�tico de leads calificados al CRM de GoHighLevel")
        print("4) Desactivar conexia�n")
        print("5) Configurar criterios de calificacia�n")
        print("6) Volver al submena� anterior")
        print(full_line(color=Fore.BLUE))
        choice = ask("Opcia�n: ").strip()
        if choice == "1":
            _gohighlevel_configure_key()
        elif choice == "2":
            _gohighlevel_configure_locations()
        elif choice == "3":
            _gohighlevel_activate()
        elif choice == "4":
            _gohighlevel_deactivate()
        elif choice == "5":
            _gohighlevel_configure_prompt()
        elif choice == "6":
            break
        else:
            warn("Opcia�n inva�lida.")
            press_enter()


def _google_calendar_select_alias() -> Optional[str]:
    alias = _prompt_alias_selection()
    if not alias:
        warn("Alias inva�lido.")
        press_enter()
        return None
    return alias


def _google_calendar_perform_device_flow(
    client_id: str, client_secret: str | None
) -> Optional[Dict[str, object]]:
    if requests is None:
        return None
    try:
        response = requests.post(
            _GOOGLE_DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": _GOOGLE_SCOPE},
            timeout=15,
        )
    except RequestException as exc:  # pragma: no cover - depende de red externa
        warn(f"No se pudo iniciar la autorizacia�n de Google: {exc}")
        press_enter()
        return None
    if response.status_code != 200:
        warn(f"Respuesta inesperada de Google: {response.text}")
        press_enter()
        return None
    payload = response.json()
    device_code = payload.get("device_code")
    if not device_code:
        warn("Google no devolvia� device_code va�lido.")
        press_enter()
        return None
    verification_url = payload.get("verification_url") or payload.get("verification_uri")
    user_code = payload.get("user_code")
    print(style_text("Para continuar:", color=Fore.CYAN, bold=True))
    if verification_url and user_code:
        print(f"1. Visita� {verification_url}")
        print(f"2. Ingresi� el ca�digo: {user_code}")
    elif user_code:
        print(f"Ingresi� el ca�digo: {user_code}")
    else:
        print("Abra� la URL indicada por Google y autoriza� el acceso.")
    print("Esperando confirmacia�n...")
    interval = int(payload.get("interval", 5))
    expires_at = time.time() + int(payload.get("expires_in", 1800))
    data = {
        "client_id": client_id,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    }
    if client_secret:
        data["client_secret"] = client_secret
    while time.time() < expires_at:
        time.sleep(interval)
        try:
            token_response = requests.post(_GOOGLE_TOKEN_URL, data=data, timeout=15)
        except RequestException as exc:  # pragma: no cover - depende de red externa
            warn(f"Error al consultar token de Google: {exc}")
            press_enter()
            return None
        if token_response.status_code == 200:
            return token_response.json()
        try:
            error_payload = token_response.json()
        except Exception:
            error_payload = {}
        error_code = (error_payload or {}).get("error")
        if error_code in {"authorization_pending"}:
            continue
        if error_code == "slow_down":
            interval = min(interval + 2, 15)
            continue
        if error_code in {"expired_token", "access_denied"}:
            warn("La autorizacia�n no fue completada.")
            press_enter()
            return None
        warn(f"Error al obtener token de Google: {token_response.text}")
        press_enter()
        return None
    warn("El ca�digo de autorizacia�n expira�. Intenta� nuevamente.")
    press_enter()
    return None


def _google_calendar_validate_client_payload(
    payload: Dict[str, object]
) -> tuple[Optional[Dict[str, object]], Optional[str]]:
    if not isinstance(payload, dict):
        return None, "El archivo JSON no contiene una estructura va�lida."
    installed = payload.get("installed")
    if not isinstance(installed, dict):
        return (
            None,
            "El archivo JSON debe corresponder a una 'Aplicacia�n de escritorio' generada en Google Cloud Console.",
        )
    redirect_uris = installed.get("redirect_uris")
    normalized_uris: set[str] = set()
    if isinstance(redirect_uris, (list, tuple)):
        normalized_uris = {
            str(uri).strip().rstrip("/")
            for uri in redirect_uris
            if isinstance(uri, str) and uri.strip()
        }
    if _GOOGLE_REDIRECT_URI.rstrip("/") not in normalized_uris:
        return (
            None,
            "El JSON debe incluir http://localhost como redirect URI autorizado en la consola de Google.",
        )
    client_id = installed.get("client_id")
    if not client_id:
        return None, "El archivo JSON no contiene un Client ID va�lido."
    return installed, None


def _google_calendar_extract_client_credentials(
    payload: Dict[str, object]
) -> tuple[Optional[str], Optional[str]]:
    config, _ = _google_calendar_validate_client_payload(payload)
    if not config:
        return None, None
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    return (
        (str(client_id) if client_id else None),
        (str(client_secret) if client_secret else None),
    )


def _google_calendar_report_oauth_error(exc: Exception) -> None:
    base_message = (
        "Error de autenticacia�n. Verifica� que el JSON cargado sea va�lido, "
        "que esta�s autorizado como tester y que el proyecto esta� correctamente configurado."
    )
    details = str(exc).strip()
    if details:
        warn(f"{base_message} Detalle: {details}")
    else:
        warn(base_message)


def _ensure_google_auth_oauthlib() -> bool:
    global InstalledAppFlow
    if InstalledAppFlow is not None:
        return True
    flow_cls = None
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow as flow_cls  # type: ignore
    except Exception:
        warn("Esta opcia�n requiere la librera�a google-auth-oauthlib.")
        confirm = (
            ask("-aDesea�s que la instalemos automa�ticamente ahora? (s/n): ")
            .strip()
            .lower()
        )
        if confirm not in {"s", "si", "si�", "y", "yes"}:
            warn("Instalacia�n cancelada. Instala� google-auth-oauthlib para continuar.")
            press_enter()
            return False
        python_bin = sys.executable or "python3"
        print(
            style_text(
                "Instalando google-auth-oauthlib, por favor espera�...",
                color=Fore.YELLOW,
            )
        )
        try:
            subprocess.check_call(
                [python_bin, "-m", "pip", "install", "google-auth-oauthlib"]
            )
        except Exception as exc:
            warn(f"No se pudo instalar google-auth-oauthlib automa�ticamente: {exc}")
            press_enter()
            return False
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow as flow_cls  # type: ignore
        except Exception as exc:
            warn(f"La librera�a google-auth-oauthlib no pudo cargarse: {exc}")
            press_enter()
            return False
        ok("La librera�a google-auth-oauthlib se instala� correctamente.")
    if flow_cls is None:
        warn("No se pudo cargar la librera�a google-auth-oauthlib.")
        press_enter()
        return False
    InstalledAppFlow = flow_cls
    return True


def _google_calendar_load_credentials_json() -> None:
    if not _ensure_google_auth_oauthlib():
        return
    banner()
    print(
        style_text(
            "Google Calendar ��� Cargar credenciales JSON",
            color=Fore.CYAN,
            bold=True,
        )
    )
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    path_input = ask(
        "Ruta del archivo JSON de Google (vaca�o para cancelar): "
    ).strip()
    if not path_input:
        warn("No se carga� ninga�n archivo de credenciales.")
        press_enter()
        return
    file_path = Path(path_input).expanduser()
    if not file_path.exists():
        warn("El archivo especificado no existe.")
        press_enter()
        return
    try:
        json_payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        warn(f"No se pudo leer el archivo JSON: {exc}")
        press_enter()
        return
    config, error = _google_calendar_validate_client_payload(json_payload)
    if error:
        warn(error)
        press_enter()
        return
    client_id = str(config.get("client_id") or "")
    client_secret_value = config.get("client_secret")
    client_secret = str(client_secret_value) if client_secret_value else None
    if not client_id:
        warn("El archivo JSON no contiene un Client ID va�lido.")
        press_enter()
        return
    _set_google_calendar_entry(
        alias,
        {"client_id": client_id, "client_secret": client_secret},
    )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(file_path), scopes=[_GOOGLE_SCOPE]
        )
        try:
            flow.redirect_uri = _GOOGLE_REDIRECT_URI
        except Exception:
            # Algunos objetos Flow no exponen redirect_uri hasta ejecutar run_*.
            pass
    except Exception as exc:  # pragma: no cover - depende de librera�a externa
        warn(f"No se pudo inicializar el flujo OAuth: {exc}")
        press_enter()
        return
    try:
        credentials = flow.run_local_server(port=0)
    except Exception as exc_local:  # pragma: no cover - depende de librera�a externa
        logger.debug(
            "Fallo run_local_server para Google OAuth, se intenta modo consola",
            exc_info=exc_local,
        )
        try:
            credentials = flow.run_console()
        except Exception as exc_console:  # pragma: no cover - depende de librera�a externa
            _google_calendar_report_oauth_error(exc_console)
            press_enter()
            return
    entry = _get_google_calendar_entry(alias)
    _google_calendar_update_tokens_from_credentials(alias, entry, credentials)
    entry = _get_google_calendar_entry(alias)
    if entry.get("connected"):
        ok(f"Google Calendar conectado para {alias}.")
    else:
        warn("No se pudo completar la conexia�n con Google Calendar.")
    press_enter()


def _google_calendar_connect() -> None:
    if not _require_requests():
        return
    banner()
    print(style_text("Google Calendar ��� Conectar cuenta", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    entry = _get_google_calendar_entry(alias)
    current_client_id = str(entry.get("client_id") or "")
    current_client_secret = str(entry.get("client_secret") or "")
    print(f"Client ID actual: {current_client_id or '(sin definir)'}")
    client_id = ask("Ingresi� el Client ID de OAuth (vaca�o mantiene actual): ").strip()
    if not client_id:
        client_id = current_client_id
    if not client_id:
        warn("Se requiere un Client ID va�lido para continuar.")
        press_enter()
        return
    client_secret = ask(
        "Ingresi� el Client Secret (vaca�o mantiene actual o se omite si no aplica): "
    ).strip()
    if not client_secret:
        client_secret = current_client_secret
    _set_google_calendar_entry(alias, {"client_id": client_id, "client_secret": client_secret})
    token_data = _google_calendar_perform_device_flow(client_id, client_secret or None)
    if not token_data:
        return
    entry = _get_google_calendar_entry(alias)
    entry = _google_calendar_store_tokens(alias, entry, token_data)
    if entry.get("connected"):
        ok(f"Google Calendar conectado para {alias}.")
    else:
        warn("No se pudo completar la conexia�n con Google Calendar.")
    press_enter()


def _google_calendar_configure_event() -> None:
    banner()
    print(style_text("Google Calendar ��� Configuracia�n de eventos", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    entry = _get_google_calendar_entry(alias)
    current_name = str(entry.get("event_name") or "{{username}} - Sistema de adquisicia�n con IA")
    current_duration = int(entry.get("duration_minutes") or 30)
    current_timezone = str(entry.get("timezone") or _default_timezone_label())
    current_auto_meet = bool(entry.get("auto_meet", True))
    print(f"Nombre actual del evento: {current_name}")
    new_name = ask("Nuevo nombre (usa {{username}} para el lead, Enter mantiene): ").strip()
    updates: Dict[str, object] = {}
    if new_name:
        updates["event_name"] = new_name
    duration_input = ask(
        f"Duracia�n en minutos (actual {current_duration}, Enter mantiene): "
    ).strip()
    if duration_input:
        try:
            updates["duration_minutes"] = max(5, int(duration_input))
        except Exception:
            warn("Duracia�n inva�lida; se mantiene el valor actual.")
    tz_input = ask(
        f"Zona horaria (actual {current_timezone}, Enter mantiene): "
    ).strip()
    if tz_input:
        updates["timezone"] = tz_input
    auto_meet_input = ask(
        f"Generar enlace de Google Meet automa�ticamente? (S/N, actual {'S' if current_auto_meet else 'N'}): "
    ).strip().lower()
    if auto_meet_input in {"s", "si", "si�"}:
        updates["auto_meet"] = True
    elif auto_meet_input in {"n", "no"}:
        updates["auto_meet"] = False
    if updates:
        _set_google_calendar_entry(alias, updates)
        ok("Configuracia�n de eventos actualizada.")
    else:
        warn("No se realizaron cambios.")
    press_enter()


def _google_calendar_configure_prompt() -> None:
    banner()
    print(
        style_text(
            "Google Calendar ��� Criterio para creacia�n de eventos",
            color=Fore.CYAN,
            bold=True,
        )
    )
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    entry = _get_google_calendar_entry(alias)
    current_prompt = str(entry.get("schedule_prompt") or _DEFAULT_GOOGLE_CALENDAR_PROMPT)
    print(style_text("Prompt actual:", color=Fore.BLUE))
    print(current_prompt.strip() or "(sin definir)")
    print(full_line(color=Fore.BLUE))
    print("Elige� una opcia�n:")
    print("  E) Editar prompt")
    print("  D) Restaurar valor predeterminado")
    print("  Enter) Cancelar")
    action = ask("Accia�n: ").strip().lower()
    if not action:
        warn("No se modifica� el criterio de calendario.")
        press_enter()
        return
    if action in {"d", "default", "predeterminado"}:
        _set_google_calendar_entry(
            alias,
            {"schedule_prompt": _DEFAULT_GOOGLE_CALENDAR_PROMPT},
        )
        ok("Se restauro� el criterio predeterminado de Google Calendar.")
        press_enter()
        return
    if action not in {"e", "editar"}:
        warn("Opcia�n inva�lida. No se modifica� el criterio de calendario.")
        press_enter()
        return
    print(
        style_text(
            (
                "Pega� el nuevo criterio y finaliza� con una la�nea que diga <<<END>>>."
                " Deja� vaca�o para cancelar."
            ),
            color=Fore.CYAN,
        )
    )
    lines: List[str] = []
    while True:
        line = ask("�Ǧ ")
        if line.strip() == "<<<END>>>":
            break
        lines.append(line.replace("\r", ""))
    new_prompt = "\n".join(lines).strip()
    _set_google_calendar_entry(alias, {"schedule_prompt": new_prompt})
    if new_prompt:
        ok(f"Criterio actualizado. Longitud: {len(new_prompt)} caracteres.")
    else:
        ok("Se elimino� el criterio personalizado. Se usara� la la�gica automa�tica predeterminada.")
    press_enter()


def _google_calendar_activate() -> None:
    banner()
    print(style_text("Google Calendar ��� Activar creacia�n automa�tica", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    entry = _get_google_calendar_entry(alias)
    if not entry.get("connected"):
        warn("Conecta� Google Calendar antes de activar la la�gica automa�tica.")
        press_enter()
        return
    _set_google_calendar_entry(alias, {"enabled": True})
    ok(f"La�gica automa�tica activada para {alias}.")
    press_enter()


def _google_calendar_deactivate() -> None:
    banner()
    print(style_text("Google Calendar ��� Desactivar creacia�n automa�tica", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    _set_google_calendar_entry(alias, {"enabled": False})
    ok(f"La�gica automa�tica desactivada para {alias}.")
    press_enter()


def _google_calendar_revoke() -> None:
    banner()
    print(style_text("Google Calendar ��� Revocar conexia�n", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    for line in _google_calendar_status_lines():
        print(line)
    print(full_line(color=Fore.BLUE))
    alias = _google_calendar_select_alias()
    if not alias:
        return
    entry = _get_google_calendar_entry(alias)
    token = entry.get("access_token") or entry.get("refresh_token")
    if token and _require_requests():
        try:
            requests.post(_GOOGLE_REVOKE_URL, params={"token": token}, timeout=15)
        except RequestException:
            logger.warning("No se pudo notificar la revocacia�n a Google.", exc_info=False)
    _set_google_calendar_entry(
        alias,
        {
            "access_token": "",
            "refresh_token": "",
            "token_type": "",
            "token_expires_at": 0,
            "connected": False,
            "enabled": False,
        },
    )
    ok(f"Conexia�n revocada para {alias}.")
    press_enter()


def _google_calendar_menu() -> None:
    while True:
        banner()
        print(style_text("Conectar con Google Calendar", color=Fore.CYAN, bold=True))
        print(full_line(color=Fore.BLUE))
        for line in _google_calendar_status_lines():
            print(line)
        print(full_line(color=Fore.BLUE))
        print("1) Conectar cuenta mediante OAuth")
        print("2) Configurar para�metros del evento")
        print("3) Configurar criterio para creacia�n de evento")
        print("4) Activar creacia�n automa�tica de eventos")
        print("5) Desactivar creacia�n automa�tica de eventos")
        print("6) Revocar conexia�n")
        print("7) Cargar credenciales JSON (Google OAuth 2.0)")
        print("8) Volver al submena� anterior")
        print(full_line(color=Fore.BLUE))
        choice = ask("Opcia�n: ").strip()
        if choice == "1":
            _google_calendar_connect()
        elif choice == "2":
            _google_calendar_configure_event()
        elif choice == "3":
            _google_calendar_configure_prompt()
        elif choice == "4":
            _google_calendar_activate()
        elif choice == "5":
            _google_calendar_deactivate()
        elif choice == "6":
            _google_calendar_revoke()
        elif choice == "7":
            _google_calendar_load_credentials_json()
        elif choice == "8":
            break
        else:
            warn("Opcia�n inva�lida.")
            press_enter()


def _configure_api_key() -> None:
    banner()
    current_key, _ = _load_preferences()
    print(style_text("Configurar OPENAI_API_KEY", color=Fore.CYAN, bold=True))
    print(f"Actual: {(_mask_key(current_key) or '(sin definir)')}")
    print()
    new_key = ask("Nueva API Key (vaca�o para cancelar): ").strip()
    if not new_key:
        warn("Se mantuvo la API Key actual.")
        press_enter()
        return
    update_env_local({"OPENAI_API_KEY": new_key})
    refresh_settings()
    ok("OPENAI_API_KEY guardada en .env.local")
    press_enter()


def _configure_prompt() -> None:
    while True:
        banner()
        _, current_prompt = _load_preferences()
        print(style_text("Configurar System Prompt", color=Fore.CYAN, bold=True))
        print(style_text("Actual:", color=Fore.BLUE))
        print(current_prompt or "(sin definir)")
        print()
        print(f"Longitud actual: {len(current_prompt or '')} caracteres.")
        print(full_line(color=Fore.BLUE))
        print("1) Editar/pegar en consola (delimitador <<<END>>>)")
        print("2) Cargar desde archivo .txt")
        print("3) Ver primeros 400 caracteres")
        print("4) Volver")
        print(full_line(color=Fore.BLUE))
        choice = ask("Opcia�n: ").strip()

        if choice == "1":
            print(style_text(
                "Pega� tu System Prompt y cerra� con una la�nea que diga <<<END>>>.",
                color=Fore.CYAN,
            ))
            lines: list[str] = []
            while True:
                line = ask("�Ǧ ")
                if line.strip() == "<<<END>>>":
                    break
                lines.append(line.replace("\r", ""))
            new_prompt = "\n".join(lines)
            if not _normalize_system_prompt_text(new_prompt):
                warn("No se modifica� el prompt.")
                press_enter()
                continue
            saved_prompt = _persist_system_prompt(new_prompt)
            ok(f"System Prompt guardado. Longitud: {len(saved_prompt)} caracteres.")
            press_enter()
        elif choice == "2":
            path_input = ask("Ruta del archivo .txt (vaca�o para cancelar): ").strip()
            if not path_input:
                warn("No se modifica� el prompt.")
                press_enter()
                continue
            file_path = Path(path_input).expanduser()
            if not file_path.exists():
                warn("El archivo especificado no existe.")
                press_enter()
                continue
            try:
                file_contents = file_path.read_text(encoding="utf-8")
            except Exception as exc:
                warn(f"No se pudo leer el archivo: {exc}")
                press_enter()
                continue
            if not _normalize_system_prompt_text(file_contents):
                warn("No se modifica� el prompt.")
                press_enter()
                continue
            saved_prompt = _persist_system_prompt(file_contents)
            ok(f"System Prompt guardado. Longitud: {len(saved_prompt)} caracteres.")
            press_enter()
        elif choice == "3":
            preview = (current_prompt or "")[:400]
            print(style_text("Primeros 400 caracteres:", color=Fore.BLUE))
            if not preview:
                print("(sin definir)")
            else:
                print(preview)
                if len(current_prompt or "") > 400:
                    print(style_text("�Ǫ (truncado)", color=Fore.YELLOW))
            press_enter()
        elif choice == "4":
            break
        else:
            warn("Opcia�n inva�lida.")
            press_enter()


def _available_aliases() -> List[str]:
    aliases: set[str] = {"ALL"}
    for account in list_all():
        if account.get("alias"):
            aliases.add(account["alias"].strip())
        if account.get("username"):
            aliases.add(account["username"].strip())
    return sorted(a for a in aliases if a)


def _preview_prompt(prompt: str) -> str:
    if not prompt:
        return "(sin definir)"
    first_line = prompt.splitlines()[0]
    if len(first_line) > 60:
        return first_line[:57] + "�Ǫ"
    if len(prompt.splitlines()) > 1:
        return first_line + " �Ǫ"
    return first_line


def autoresponder_menu_options() -> List[str]:
    return [
        "1) Configurar API Key",
        "2) Configurar System Prompt",
        "3) Activar bot (alias/grupo)",
        "4) Seguimiento",
        "5) Conectar con GoHighLevel",
        "6) Conectar con Google Calendar",
        "7) Desactivar bot",
        "8) Volver",
    ]


def autoresponder_prompt_length() -> int:
    _, prompt = _load_preferences()
    return len(prompt or "")


def _print_menu_header() -> None:
    banner()
    api_key, prompt = _load_preferences()
    status = (
        style_text(f"Estado: activo para {ACTIVE_ALIAS}", color=Fore.GREEN, bold=True)
        if ACTIVE_ALIAS
        else style_text("Estado: inactivo", color=Fore.YELLOW, bold=True)
    )
    print(style_text("Auto-responder con OpenAI", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.BLUE))
    print(f"API Key: {_mask_key(api_key) or '(sin definir)'}")
    print(f"System prompt: {_preview_prompt(prompt)}")
    print(status)
    print(_followup_summary_line())
    print(_gohighlevel_summary_line())
    print(_google_calendar_summary_line())
    print(full_line(color=Fore.BLUE))
    for option in autoresponder_menu_options():
        print(option)
    print(full_line(color=Fore.BLUE))


def _prompt_alias_selection() -> str | None:
    options = _available_aliases()
    print("Alias/grupos disponibles:")
    for idx, alias in enumerate(options, start=1):
        print(f" {idx}) {alias}")
    raw = ask("Selecciona� alias (na�mero o texto, Enter=ALL): ").strip()
    if not raw:
        return "ALL"
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(options):
            return options[idx - 1]
        warn("Na�mero fuera de rango.")
        return None
    return raw


def _handle_account_issue(user: str, exc: Exception, active: List[str]) -> None:
    message = str(exc).lower()
    detail = f"{exc.__class__.__name__}: {exc}"
    if should_retry_proxy(exc):
        label = style_text(f"[WARN][@{user}] proxy falla�", color=Fore.YELLOW, bold=True)
        record_proxy_failure(user, exc)
        print(label)
        warn("Revisi� la opcia�n 1 para actualizar o quitar el proxy de esta cuenta.")
    elif "login_required" in message or "login requerido" in message:
        label = style_text(f"[ERROR][@{user}] sesia�n inva�lida", color=Fore.RED, bold=True)
        print(label)
    elif any(key in message for key in ("challenge", "checkpoint")):
        label = style_text(f"[WARN][@{user}] checkpoint requerido", color=Fore.YELLOW, bold=True)
        print(label)
    elif "feedback_required" in message or "rate" in message:
        label = style_text(f"[WARN][@{user}] rate limit detectado", color=Fore.YELLOW, bold=True)
        print(label)
    else:
        label = style_text(f"[WARN][@{user}] error inesperado", color=Fore.YELLOW, bold=True)
        print(label)
    try:
        warn(detail)
    except Exception:
        print(detail)
    try:
        _append_message_log(
            {
                "action": "account_error",
                "account": user,
                "error": detail,
            }
        )
    except Exception:
        pass
    logger.warning("Incidente con @%s en auto-responder: %s", user, exc, exc_info=False)

    while True:
        choice = ask("- Continuar sin esta cuenta (C) / Reintentar (R) / Pausar (P)? ").strip().lower()
        if choice in {"c", "r", "p"}:
            break
        warn("Elige� C, R o P.")

    if choice == "c":
        if user in active:
            active.remove(user)
        mark_connected(user, False)
        warn(f"Se excluye @{user} del ciclo actual.")
        return

    if choice == "p":
        request_stop("pausa solicitada desde mena� del bot")
        return

    while choice == "r":
        if _prompt_playwright_login(user, alias=ACTIVE_ALIAS or user) and _ensure_session(user):
            mark_connected(user, True)
            ok(f"Sesia�n renovada para @{user}")
            return
        warn("La sesia�n sigue fallando. Intenta� nuevamente o elega� otra opcia�n.")
        choice = ask("- Reintentar (R) / Continuar sin la cuenta (C) / Pausar (P)? ").strip().lower()
        if choice == "c":
            if user in active:
                active.remove(user)
            mark_connected(user, False)
            warn(f"Se excluye @{user} del ciclo actual.")
            return
        if choice == "p":
            request_stop("pausa solicitada desde mena� del bot")
            return


def _normalize_phone(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    has_plus = raw.startswith("a")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return ""
    return f"a{digits}" if has_plus else digits


def _extract_phone_numbers(text: str) -> List[str]:
    if not text:
        return []
    matches = _PHONE_PATTERN.findall(text)
    numbers: List[str] = []
    for match in matches:
        normalized = _normalize_phone(match)
        if normalized and len(normalized.replace("a", "")) >= 8:
            if normalized not in numbers:
                numbers.append(normalized)
    return numbers


def _extract_email_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    matches = list(_EMAIL_PATTERN.findall(text))
    if not matches:
        return None
    return matches[-1]


def _infer_lead_tag(
    conversation: str,
    phone_numbers: List[str],
    status: Optional[str] = None,
) -> str:
    if status and status.strip().lower() == "no interesado":
        return "No calificado"
    normalized = _normalize_text_for_match(conversation)
    if any(_contains_token(normalized, keyword) for keyword in _NEGATIVE_KEYWORDS):
        return "No calificado"
    if phone_numbers:
        if any(word in normalized for word in _CALL_KEYWORDS):
            return "Listo para agendar llamada"
        if status and status.strip().lower() == "interesado":
            return "Listo para agendar llamada"
        return "Listo para agendar llamada"
    if any(_contains_token(normalized, keyword) for keyword in _POSITIVE_KEYWORDS):
        return "Interesado sin na�mero"
    if any(keyword in normalized for keyword in _INFO_KEYWORDS) or "?" in conversation:
        return "Solicita ma�s info"
    if normalized.strip():
        return _DEFAULT_LEAD_TAG
    return _DEFAULT_LEAD_TAG


def _build_conversation_note(
    account: str, recipient: str, conversation: str, status: Optional[str] = None
) -> str:
    header = [f"Cuenta IG: @{account}"]
    if recipient:
        header.append(f"Usuario: @{recipient}")
    if status:
        header.append(f"Estado detectado: {status}")
    header.append("Historial completo:")
    return "\n".join(header + [conversation])


def _next_weekday_date(base: datetime, target_weekday: int) -> datetime.date:
    days_ahead = (target_weekday - base.weekday() + 7) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (base + timedelta(days=days_ahead)).date()


def _parse_meeting_datetime_from_text(text: str, tz_label: str) -> Optional[datetime]:
    if not text:
        return None
    if not _MEETING_TIME_PATTERN.search(text):
        return None
    match = _MEETING_TIME_PATTERN.search(text)
    if not match:
        return None
    try:
        hour = int(match.group("hour"))
    except Exception:
        return None
    minute_str = match.group("minute")
    minute = int(minute_str) if minute_str and minute_str.isdigit() else 0
    ampm = match.group("ampm")
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    hour %= 24
    tz = _safe_timezone(tz_label)
    base = datetime.now(tz)
    normalized = _normalize_text_for_match(text)
    date_value: Optional[datetime.date] = None
    for keyword, offset in _RELATIVE_DATE_KEYWORDS:
        if keyword in normalized:
            date_value = (base + timedelta(days=offset)).date()
            break
    if date_value is None:
        for keyword, weekday in _WEEKDAY_KEYWORDS.items():
            if _contains_token(normalized, keyword):
                date_value = _next_weekday_date(base, weekday)
                break
    if date_value is None and _MEETING_DATE_PATTERN.search(text):
        parsed = _safe_parse_datetime(text, fuzzy=True, dayfirst=True, default=base)
        if isinstance(parsed, datetime):
            date_value = parsed.date()
    if date_value is None:
        return None
    meeting_dt = datetime.combine(date_value, dt_time(hour=hour, minute=minute), tz)
    if meeting_dt < base:
        if _MEETING_DATE_PATTERN.search(text):
            return None
        meeting_dt += timedelta(days=7)
    return meeting_dt


def _detect_meeting_datetime(conversation: str, tz_label: str) -> Optional[datetime]:
    lines = [line for line in conversation.splitlines() if line.startswith("ELLOS:")]
    for line in reversed(lines):
        _, _, content = line.partition(":")
        meeting_dt = _parse_meeting_datetime_from_text(content.strip(), tz_label)
        if meeting_dt:
            return meeting_dt
    return None


def _render_calendar_summary(template: str, username: str) -> str:
    template = template or "{{username}} - Sistema de adquisicia�n con IA"
    return template.replace("{{username}}", username or "Lead")


def _parse_gohighlevel_contact_id(data: Dict[str, object]) -> Optional[str]:
    if not isinstance(data, dict):
        return None
    contact = data.get("contact") if isinstance(data.get("contact"), dict) else None
    if isinstance(contact, dict):
        for key in ("id", "Id", "contactId"):
            if contact.get(key):
                return str(contact[key])
    for key in ("contactId", "id", "Id"):
        value = data.get(key)
        if value:
            return str(value)
    return None


def _update_gohighlevel_contact(
    api_key: str, contact_id: str, payload: Dict[str, object]
) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{_GOHIGHLEVEL_BASE}/contacts/{contact_id}"
    response = requests.put(url, json=payload, headers=headers, timeout=15)  # type: ignore[call-arg]
    response.raise_for_status()
    data: Dict[str, object] = {}
    if response.content:
        try:
            data = response.json()
        except ValueError:
            data = {}
    return _parse_gohighlevel_contact_id(data) or contact_id


def _create_gohighlevel_contact(
    api_key: str, payload: Dict[str, object]
) -> tuple[Optional[str], bool]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{_GOHIGHLEVEL_BASE}/contacts/"
    response = requests.post(url, json=payload, headers=headers, timeout=15)  # type: ignore[call-arg]
    data: Dict[str, object] = {}
    if response.status_code == 409:
        try:
            data = response.json()
        except ValueError:
            data = {}
        contact_id = _parse_gohighlevel_contact_id(data)
        if contact_id:
            updated_id = _update_gohighlevel_contact(api_key, str(contact_id), payload)
            return updated_id, False
    response.raise_for_status()
    if response.content:
        try:
            data = response.json()
        except ValueError:
            data = {}
    contact_id = _parse_gohighlevel_contact_id(data)
    if contact_id:
        return contact_id, True
    return None, True


def _attach_gohighlevel_note(api_key: str, contact_id: str, note: str) -> None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    url = f"{_GOHIGHLEVEL_BASE}/contacts/{contact_id}/notes/"
    payload = {"body": note}
    response = requests.post(url, json=payload, headers=headers, timeout=15)  # type: ignore[call-arg]
    response.raise_for_status()


def _send_lead_to_gohighlevel(
    account: str,
    recipient: str,
    conversation: str,
    phone_numbers: List[str],
    status: Optional[str],
    openai_api_key: Optional[str] = None,
) -> None:
    if requests is None:
        logger.warning("GoHighLevel no disponible: falta la librera�a requests.")
        return
    if not phone_numbers:
        return
    alias, entry = _gohighlevel_enabled_entry_for(account)
    if not alias or not entry:
        return
    api_key = str(entry.get("api_key") or "")
    if not api_key:
        return
    location_ids = _sanitize_location_ids(entry.get("location_ids"))
    if not location_ids:
        logger.info(
            "GoHighLevel sin Location IDs configurados | alias=%s | cuenta=%s",
            alias,
            account,
        )
        return
    lead_identifier = recipient or phone_numbers[0]
    normalized_lead = _normalize_lead_id(lead_identifier)
    main_phone = phone_numbers[0]
    if _gohighlevel_already_sent(alias, normalized_lead, main_phone):
        return

    if not _gohighlevel_lead_qualifies(
        entry,
        conversation,
        status,
        phone_numbers,
        openai_api_key,
    ):
        return

    contact_payload: Dict[str, object] = {
        "name": recipient or "Lead Instagram",
        "phone": main_phone,
    }
    email = _extract_email_from_text(conversation)
    if email:
        contact_payload["email"] = email
    note_text = _build_conversation_note(account, recipient, conversation, status)
    lead_tag = _infer_lead_tag(conversation, phone_numbers, status)
    successes: List[str] = []
    for location_id in location_ids:
        payload = dict(contact_payload)
        payload["locationId"] = location_id
        if lead_tag:
            payload["tags"] = [lead_tag]
        try:
            contact_id, created = _create_gohighlevel_contact(api_key, payload)
            if not contact_id:
                message = (
                    "No se obtuvo contactId al crear contacto en GoHighLevel para %s (location %s)."
                )
                logger.warning(
                    message,
                    recipient or "(sin usuario)",
                    location_id,
                )
                print(
                    f"��� Falla� el enva�o a GHL (Location {location_id}): no se recibia� identificador del contacto"
                )
                continue
            _attach_gohighlevel_note(api_key, contact_id, note_text)
            successes.append(location_id)
            action = "creado" if created else "actualizado"
            print(
                f"ԣ� Lead enviado a GHL (Location {location_id}) ��� contacto {action} (ID {contact_id})"
            )
        except RequestException as exc:  # pragma: no cover - depende de red externa
            logger.warning(
                "Error enviando lead a GoHighLevel (location %s): %s",
                location_id,
                exc,
                exc_info=False,
            )
            print(f"��� Falla� el enva�o a GHL (Location {location_id}): {exc}")
        except Exception as exc:  # pragma: no cover - manejo defensivo
            logger.warning(
                "Fallo inesperado con GoHighLevel (location %s): %s",
                location_id,
                exc,
                exc_info=False,
            )
            print(f"��� Falla� el enva�o a GHL (Location {location_id}): {exc}")
    if not successes:
        return

    _gohighlevel_mark_sent(alias, normalized_lead, main_phone)
    logger.info(
        "Lead enviado a GoHighLevel | alias=%s | cuenta=%s | contacto=%s | locations=%s | tag=%s",
        alias,
        account,
        recipient or "(sin usuario)",
        ",".join(successes),
        lead_tag,
    )


def _maybe_schedule_google_calendar_event(
    account: str,
    recipient: str,
    conversation: str,
    phone_numbers: List[str],
    status: Optional[str],
    openai_api_key: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    if ACTIVE_ALIAS is None:
        return None
    if status and status.strip().lower() == "no interesado":
        return None
    alias, entry = _google_calendar_enabled_entry_for(account)
    if not alias or not entry:
        return None
    if requests is None and (Credentials is None or build is None):
        return None
    tz_label = str(entry.get("timezone") or _default_timezone_label())
    meeting_dt = _detect_meeting_datetime(conversation, tz_label)
    if not meeting_dt:
        return None
    normalized_convo = _normalize_text_for_match(conversation)
    prompt_text = str(entry.get("schedule_prompt") or "").strip()
    if not prompt_text and not any(
        keyword in normalized_convo for keyword in _CALL_KEYWORDS
    ):
        return None
    if not _google_calendar_lead_qualifies(
        entry,
        conversation,
        status,
        phone_numbers,
        meeting_dt,
        openai_api_key,
    ):
        return None
    main_phone = _normalize_phone(phone_numbers[0]) if phone_numbers else ""
    normalized_lead = recipient or main_phone or f"{account}-lead"
    scheduled_entry, _ = _google_calendar_get_scheduled(
        alias, normalized_lead, main_phone
    )
    previous_start: Optional[datetime] = None
    previous_link = ""
    event_id_to_update: Optional[str] = None
    if scheduled_entry:
        event_id_value = scheduled_entry.get("event_id")
        if isinstance(event_id_value, str) and event_id_value.strip():
            event_id_to_update = event_id_value
        start_value = scheduled_entry.get("start")
        if isinstance(start_value, str) and start_value:
            try:
                previous_start = datetime.fromisoformat(start_value)
            except Exception:
                previous_start = None
        link_value = scheduled_entry.get("link")
        if isinstance(link_value, str):
            previous_link = _google_calendar_preferred_link(link_value)
    access_token = _google_calendar_ensure_token(alias, entry)
    if not access_token and requests is None and (Credentials is None or build is None):
        return None
    summary_template = str(entry.get("event_name") or "{{username}} - Sistema de adquisicia�n con IA")
    summary = _render_calendar_summary(summary_template, recipient or "Lead")
    try:
        duration = int(entry.get("duration_minutes") or 30)
    except Exception:
        duration = 30
    duration = max(5, duration)
    tz = _safe_timezone(tz_label)
    start_dt = meeting_dt.astimezone(tz)
    end_dt = start_dt + timedelta(minutes=duration)
    email = _extract_email_from_text(conversation)
    description_lines = [
        "Evento generado automa�ticamente desde el bot de Instagram.",
        f"Cuenta IG: @{account}",
    ]
    if recipient:
        description_lines.append(f"Usuario IG: @{recipient}")
    if main_phone:
        description_lines.append(f"Tela�fono: {main_phone}")
    else:
        description_lines.append("Tela�fono: (sin proporcionar)")
    if email:
        description_lines.append(f"Email: {email}")
    if status:
        description_lines.append(f"Estado detectado: {status}")
    description_lines.append("")
    description_lines.append("Historial de la conversacia�n:")
    description_lines.append(conversation)
    description = "\n".join(description_lines)
    payload: Dict[str, object] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_label},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_label},
    }
    attendees: List[Dict[str, str]] = []
    if email:
        attendees.append({"email": email})
    if attendees:
        payload["attendees"] = attendees
    params: Dict[str, object] = {}
    if entry.get("auto_meet", True):
        payload["conferenceData"] = {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }
        params["conferenceDataVersion"] = 1
    event: Optional[Dict[str, object]] = None
    event_action = "created"
    skip_message = False
    if event_id_to_update:
        should_update = True
        if previous_start:
            delta = abs((previous_start - start_dt).total_seconds())
            if delta < 60:
                should_update = False
        if should_update:
            event = _google_calendar_update_event(
                alias, entry, event_id_to_update, payload, params, access_token
            )
            if event:
                event_action = "updated"
        else:
            skip_message = True
        if not event and not skip_message:
            event = _google_calendar_create_event(alias, entry, payload, params, access_token)
            event_action = "created"
    else:
        event = _google_calendar_create_event(alias, entry, payload, params, access_token)
        event_action = "created"
    if skip_message:
        return None
    if not event:
        return None
    event_id = event.get("id") if isinstance(event, dict) else None
    if not event_id:
        return None
    share_link = _google_calendar_share_link_from_event_id(event_id)
    event_link = ""
    backup_link = ""
    if isinstance(event, dict):
        event_link = _google_calendar_preferred_link(str(event.get("htmlLink") or ""))
        backup_link = str(event.get("hangoutLink") or "")
        if not backup_link:
            conference_data = event.get("conferenceData") if isinstance(event.get("conferenceData"), dict) else {}
            if isinstance(conference_data, dict):
                entry_points = conference_data.get("entryPoints")
                if isinstance(entry_points, list):
                    for item in entry_points:
                        if isinstance(item, dict) and item.get("uri"):
                            backup_link = str(item["uri"])
                            break
        if share_link and (
            not event_link or "calendar.app.google" not in event_link
        ):
            event_link = share_link
    if not event_link:
        fetched_event = _google_calendar_fetch_event(
            alias, entry, event_id, access_token
        )
        if isinstance(fetched_event, dict):
            event = fetched_event
            event_link = _google_calendar_preferred_link(
                str(fetched_event.get("htmlLink") or "")
            )
            if share_link and (
                not event_link or "calendar.app.google" not in event_link
            ):
                event_link = share_link
            if not backup_link:
                backup_link = str(fetched_event.get("hangoutLink") or "")
                if not backup_link:
                    conference_data = (
                        fetched_event.get("conferenceData")
                        if isinstance(fetched_event.get("conferenceData"), dict)
                        else {}
                    )
                    if isinstance(conference_data, dict):
                        entry_points = conference_data.get("entryPoints")
                        if isinstance(entry_points, list):
                            for item in entry_points:
                                if isinstance(item, dict) and item.get("uri"):
                                    backup_link = str(item["uri"])
                                    break
    stored_link = event_link or share_link or backup_link or previous_link
    stored_link = _google_calendar_preferred_link(stored_link)
    _google_calendar_mark_scheduled(
        alias,
        normalized_lead,
        main_phone,
        event_id,
        stored_link,
        start_dt.isoformat(),
    )
    logger.info(
        "Evento programado en Google Calendar | alias=%s | cuenta=%s | lead=%s | inicio=%s",
        alias,
        account,
        recipient or "(sin usuario)",
        start_dt.isoformat(),
    )
    formatted_dt = start_dt.strftime("%d/%m/%Y %H:%M")
    recipient_handle = _format_handle(recipient or main_phone or None)
    if event_action == "updated":
        message_lines = [
            f"Perfecto, actualica� nuestra llamada para {formatted_dt} ({tz_label}).",
        ]
    else:
        message_lines = [
            f"Listo, acabo de agendar nuestra llamada para {formatted_dt} ({tz_label}).",
        ]
    if event_link:
        message_lines.append(
            f"Te paso el link del evento para que confirmes la asistencia: {event_link}"
        )
    elif stored_link:
        message_lines.append(
            f"Te comparta� los detalles de la reunia�n en nuestro calendario: {stored_link}"
        )
    else:
        message_lines.append("Te comparta� los detalles de la reunia�n en nuestro calendario.")
    status_line = f"ԣ� Reunia�n agendada en Google Calendar para {recipient_handle}"
    log_conversation_status(
        account,
        recipient or normalized_lead,
        status_line,
        timestamp=int(time.time()),
    )
    return "\n".join(message_lines), status_line


def _process_inbox(
    client,
    user: str,
    state: Dict[str, Dict[str, str]],
    api_key: str,
    system_prompt: str,
    stats: BotStats,
    delay_min: float = 0.0,
    delay_max: float = 0.0,
    max_age_days: int = 7,
    allowed_thread_ids: Optional[set[str]] = None,
    threads_limit: int = 20,
) -> None:
    print(style_text(f"[Barrido] Iniciando scan de @{user}", color=Fore.CYAN))
    # Para Playwright omitimos el pre-cargado redundante de toda la memoria por click.
    # _load_all_conversations_to_memory(client, user, max_age_days, threads_limit=threads_limit)
    
    inbox = _fetch_inbox_threads(client, amount=threads_limit)
    if not inbox:
        print(style_text(f"[Barrido] Sin chats visibles para @{user}", color=Fore.YELLOW))
        logger.warning(
            "No threads visibles para @%s: ver screenshot/html en storage/logs (dm_debug_...)",
            user,
        )
        return

    total_threads = len(inbox)
    logger.info("PlaywrightDM inbox_scan_result account=@%s total_found=%d ids=%s",
                user, total_threads, [getattr(t, "id", "synthetic_" + hashlib.sha1((user + getattr(t, "title", "")).encode()).hexdigest()[:8]) for t in inbox])
    state.setdefault(user, {})
    max_age_seconds = max(0, int(max_age_days)) * 24 * 3600 if max_age_days is not None else 0
    now = time.time()
    
    total_threads = len(inbox)
    print(style_text(f"[Barrido] Threads visibles: {total_threads}", color=Fore.CYAN))
    for idx, thread in enumerate(inbox, start=1):
        if STOP_EVENT.is_set():
            break
        print(style_text(f"[PROBE] Transición a memoria iniciada para thread {idx}/{total_threads}", color=Fore.CYAN, bold=True))
        print(style_text(f"[Barrido] Thread {idx}/{total_threads} en progreso", color=Fore.CYAN))
        thread_id_val = getattr(thread, "id", None) or getattr(thread, "pk", None)
        if thread_id_val is None:
            continue
        thread_id = str(thread_id_val)
        print(style_text(f"[Barrido] Procesando Thread ID: {thread_id}", color=Fore.CYAN))
        logger.debug("PlaywrightDM processing thread %s", thread_id)
        if allowed_thread_ids is not None and thread_id not in allowed_thread_ids:
            continue
        messages = client.get_messages(thread, amount=10)
        if not messages:
            print(style_text(f"[Barrido] Thread {thread_id} sin mensajes (omitido)", color=Fore.YELLOW))
            # PROBE: ¿Estamos dentro del thread aunque no haya mensajes?
            # Intentamos persistir al menos el ID para seguimiento de diagnóstico
            _update_conversation_state(user, thread_id, {"updated_at": time.time()}, recipient_username=_resolve_username(client, thread, None))
            continue
        last = _latest_message(messages)
        if not last:
            continue
        last_seen_id = None
        last_id = getattr(last, "id", None)
        if last_id is None:
            last_id = getattr(last, "message_id", None)
        if last_id is None:
            logger.info(
                "PlaywrightDM hook account=@%s thread_id=%s latest_id=- last_seen=%s decision=skip_no_message_id",
                user,
                thread_id,
                last_seen_id,
            )
            continue
        last_id_str = str(last_id)
        sender_id = getattr(last, "user_id", None)
        inbound: Optional[bool] = None
        if sender_id is not None:
            inbound = not _same_user_id(sender_id, client.user_id)
        else:
            from_me = getattr(last, "from_me", None)
            is_outgoing = getattr(last, "is_outgoing", None)
            direction = getattr(last, "direction", None)
            if isinstance(from_me, bool):
                inbound = not from_me
            elif isinstance(is_outgoing, bool):
                inbound = not is_outgoing
            elif isinstance(direction, str):
                lowered = direction.lower()
                if lowered in {"outgoing", "sent", "outbound", "from_me"}:
                    inbound = False
                elif lowered in {"incoming", "inbound", "received", "from_them", "from_lead"}:
                    inbound = True
        if inbound is None:
            logger.info(
                "PlaywrightDM hook account=@%s thread_id=%s latest_id=%s last_seen=%s decision=skip_unknown_direction",
                user,
                thread_id,
                last_id_str,
                last_seen_id,
            )
            continue
        if not inbound:
            logger.info(
                "PlaywrightDM hook account=@%s thread_id=%s latest_id=%s last_seen=%s decision=skip_outbound",
                user,
                thread_id,
                last_id_str,
                last_seen_id,
            )
            continue
        last_ts = _message_timestamp(last)
        if max_age_seconds:
            if last_ts is None or (now - last_ts) > max_age_seconds:
                continue

        conv_state = _get_conversation_state(user, thread_id)
        last_seen_id = conv_state.get("last_message_id_seen")
        if last_seen_id is not None and str(last_seen_id) == last_id_str:
            logger.info(
                "PlaywrightDM hook account=@%s thread_id=%s latest_id=%s last_seen=%s decision=skip_seen",
                user,
                thread_id,
                last_id_str,
                last_seen_id,
            )
            continue

        recipient_username = None
        if sender_id is not None:
            recipient_username = _resolve_username(client, thread, sender_id) or str(sender_id)
        if recipient_username is None:
            recipient_username = conv_state.get("recipient_username") or "unknown"
        logger.info(
            "PlaywrightDM hook account=@%s thread_id=%s latest_id=%s last_seen=%s decision=persist",
            user,
            thread_id,
            last_id_str,
            last_seen_id,
        )
        # DIAGNOSTICO: Confirmar paso a persistencia
        print(style_text(f"[Persistencia] PERSISTIENDO mensaje recibido para @{recipient_username} (Thread: {thread_id})", color=Fore.GREEN, bold=True))
        logger.info("PlaywrightDM passing to _record_message_received: thread_id=%s recipient=%s", thread_id, recipient_username)
        _record_message_received(user, thread_id, last_id_str, recipient_username)
        
        convo = "\n".join(
            [
                f"{'YO' if _same_user_id(getattr(msg, 'user_id', ''), client.user_id) else 'ELLOS'}: {msg.text or ''}"
                for msg in reversed(messages)
            ]
        )
        
        last_sent_at = conv_state.get("last_message_sent_at")
        last_received_at = conv_state.get("last_message_received_at")
        time_since_last_sent = (now - last_sent_at) if last_sent_at else None
        time_since_last_received = (now - last_received_at) if last_received_at else None
        
        stage = _determine_conversation_stage(
            user,
            thread_id,
            has_new_inbound=True,
            time_since_last_sent=time_since_last_sent,
            time_since_last_received=time_since_last_received,
        )
        
        # DIAGNOSTICO: Confirmar paso a _update_conversation_state
        print(style_text(f"[Persistencia] Actualizando estado a '{stage}' para @{recipient_username}", color=Fore.WHITE))
        logger.info("PlaywrightDM passing to _update_conversation_state: thread_id=%s stage=%s", thread_id, stage)
        _update_conversation_state(user, thread_id, {"stage": stage}, recipient_username)
        
        status = _classify_response(last.text or "")
        if status and recipient_username:
            msg_ts = getattr(last, "timestamp", None)
            ts_value = None
            if isinstance(msg_ts, datetime):
                ts_value = int(msg_ts.timestamp())
            log_conversation_status(user, recipient_username, status, timestamp=ts_value)
            
            if status == "No interesado":
                _update_conversation_state(user, thread_id, {"stage": _STAGE_CLOSED})
                continue
        
        phone_numbers = _extract_phone_numbers(last.text or "")
        if not phone_numbers:
            phone_numbers = _extract_phone_numbers(convo)
        calendar_message: Optional[str] = None
        calendar_status_line: Optional[str] = None
        if status != "No interesado":
            if phone_numbers:
                _send_lead_to_gohighlevel(
                    user,
                    recipient_username,
                    convo,
                    phone_numbers,
                    status,
                    api_key,
                )
            calendar_result = _maybe_schedule_google_calendar_event(
                user,
                recipient_username,
                convo,
                phone_numbers,
                status,
                api_key,
            )
            if calendar_result:
                calendar_message, calendar_status_line = calendar_result
        
        try:
            reply = _gen_response(api_key, system_prompt, convo)
            
            can_send, reason = _can_send_message(
                user,
                thread_id,
                reply,
                force=_FORCE_ALWAYS_RESPOND,
            )
            if not can_send:
                logger.info(
                    "Omitiendo envío para @%s → @%s: %s",
                    user,
                    recipient_username,
                    reason,
                )
                if last_id:
                    state[user][thread_id] = last_id
                save_auto_state(state)
                continue

            logger.info(
                "Decision responder @%s thread=%s stage=%s reason=%s",
                user,
                thread_id,
                stage,
                reason,
            )
            
            _sleep_between_replies_sync(delay_min, delay_max, label="reply_delay")
            
            message_id = client.send_message(thread, reply)
            if not message_id:
                logger.warning(
                    "Envio no verificado para @%s -> @%s (thread %s)",
                    user,
                    recipient_username,
                    thread_id,
                )
                continue

            _record_message_sent(user, thread_id, reply, str(message_id), recipient_username, is_followup=False)
            
            if calendar_message:
                calendar_id = client.send_message(thread, calendar_message)
                if calendar_id:
                    _record_message_sent(user, thread_id, calendar_message, calendar_id, recipient_username, is_followup=False)
                    _sleep_between_replies_sync(delay_min, delay_max, label="reply_delay")
            
            _update_conversation_state(user, thread_id, {"stage": _STAGE_WAITING})
            
            logger.info(
                "Mensaje enviado por @%s → @%s (último mensaje del lead hace %.1f horas)",
                user,
                recipient_username,
                time_since_last_received / 3600.0 if time_since_last_received else 0,
            )
            
        except Exception as exc:
            setattr(exc, "_autoresponder_sender", user)
            setattr(exc, "_autoresponder_recipient", recipient_username)
            setattr(exc, "_autoresponder_message_attempt", True)
            raise
        if last_id:
            state[user][thread_id] = last_id
        save_auto_state(state)
        
        index = stats.record_success(user)
        logger.info("Respuesta enviada por @%s en hilo %s (etapa: %s)", user, thread_id, stage)
        _print_response_summary(index, user, recipient_username, True, calendar_status_line)
    print(style_text(f"[Barrido] Scan completo para @{user}", color=Fore.GREEN))

def _print_bot_summary(stats: BotStats) -> None:
    print(full_line(color=Fore.MAGENTA))
    print(style_text("=== BOT DETENIDO ===", color=Fore.YELLOW, bold=True))
    print(style_text(f"Alias: {stats.alias}", color=Fore.WHITE, bold=True))
    print(style_text(f"Mensajes respondidos: {stats.responded}", color=Fore.GREEN, bold=True))
    print(style_text(f"Cuentas activas: {len(stats.accounts)}", color=Fore.CYAN, bold=True))
    print(style_text(f"Errores: {stats.errors}", color=Fore.RED if stats.errors else Fore.GREEN, bold=True))
    print(full_line(color=Fore.MAGENTA))
    press_enter()


def _activate_bot() -> None:
    global ACTIVE_ALIAS
    api_key, system_prompt = _load_preferences()
    if not api_key:
        warn("Configura OPENAI_API_KEY antes de activar el bot.")
        press_enter()
        return

    alias = _prompt_alias_selection()
    if not alias:
        warn("Alias invalido.")
        press_enter()
        return

    targets = _choose_targets(alias)
    if not targets:
        warn("No se encontraron cuentas activas para ese alias.")
        press_enter()
        return

    active_accounts = _filter_valid_sessions(targets, alias=alias)
    if not active_accounts:
        warn("Ninguna cuenta tiene sesion valida.")
        press_enter()
        return

    settings = refresh_settings()
    delay_min_default = max(1, settings.autoresponder_delay)
    delay_min = ask_int(
        f"Delay minimo entre mensajes (segundos) [{delay_min_default}]: ",
        1,
        default=delay_min_default,
    )
    delay_max = ask_int(
        f"Delay maximo entre mensajes (segundos) [{delay_min}]: ",
        delay_min,
        default=delay_min,
    )
    if delay_max < delay_min:
        delay_max = delay_min
    max_concurrent = ask_int(
        "Cuentas en simultaneo [1]: ",
        1,
        default=1,
    )
    if max_concurrent > len(active_accounts):
        max_concurrent = len(active_accounts)
    print(style_text("[Mientras mas threads, mas lenta sera la busqueda]", color=Fore.YELLOW))
    threads_limit = ask_int(
        "Cuantos threads quieres leer? [20]: ",
        1,
        default=20,
    )
    raw_schedule = ask(
        "Horas de seguimiento (ej: 4,8,12,24) [4,8,12,24]: "
    ).strip()
    followup_schedule_hours = _parse_followup_schedule_hours(
        raw_schedule, default=[4, 8, 12, 24]
    )
    followup_only_raw = ask("Solo seguimiento (S/N) [N]: ").strip().lower()
    followup_only = followup_only_raw in {"s", "si", "y", "yes"}
    max_age_days = 7

    ensure_logging(quiet=settings.quiet, log_dir=settings.log_dir, log_file=settings.log_file)
    reset_stop_event()
    state = get_auto_state()
    stats = BotStats(alias=alias)
    ACTIVE_ALIAS = alias
    listener = start_q_listener("Presiona Q para detener el auto-responder.", logger)
    print(style_text(f"Bot activo para {alias} ({len(active_accounts)} cuentas)", color=Fore.GREEN, bold=True))
    logger.info(
        "Auto-responder activo para %d cuentas (alias %s). Delay: %.1fs-%.1fs concurrent=%d followup_only=%s",
        len(active_accounts),
        alias,
        delay_min,
        delay_max,
        max_concurrent,
        "si" if followup_only else "no",
    )

    account_queue = list(active_accounts)
    try:
        with _suppress_console_noise():
            while not STOP_EVENT.is_set() and account_queue:
                batch = account_queue[:max_concurrent]
                for user in list(batch):
                    if STOP_EVENT.is_set():
                        break
                    if user not in account_queue:
                        continue
                    client = None
                    try:
                        client = _client_for(user)
                    except Exception as exc:
                        stats.record_error(user)
                        _handle_account_issue(user, exc, active_accounts)
                        if user not in active_accounts and user in account_queue:
                            account_queue.remove(user)
                        continue

                    allowed_thread_ids = None
                    if followup_only:
                        allowed_thread_ids = _followup_allowed_thread_ids(user)

                    try:
                        if not followup_only or allowed_thread_ids:
                            _process_inbox(
                                client,
                                user,
                                state,
                                api_key,
                                system_prompt,
                                stats,
                                delay_min,
                                delay_max,
                                max_age_days,
                                allowed_thread_ids=allowed_thread_ids if followup_only else None,
                                threads_limit=threads_limit,
                            )
                        _process_followups(
                            client,
                            user,
                            api_key,
                            delay_min,
                            delay_max,
                            max_age_days,
                            threads_limit=threads_limit,
                            followup_schedule_hours=followup_schedule_hours,
                        )
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:  # pragma: no cover - depende de SDK/insta
                        if getattr(exc, "_autoresponder_message_attempt", False):
                            index = stats.record_response_error(user)
                            sender = getattr(exc, "_autoresponder_sender", user)
                            recipient = getattr(exc, "_autoresponder_recipient", "-")
                            _print_response_summary(index, sender, recipient, False)
                        else:
                            stats.record_error(user)
                        logger.warning(
                            "Error en auto-responder para @%s: %s",
                            user,
                            exc,
                            exc_info=not settings.quiet,
                        )
                        _handle_account_issue(user, exc, active_accounts)
                    finally:
                        if client is not None:
                            try:
                                client.close()
                            except Exception:
                                pass

                    if user not in active_accounts and user in account_queue:
                        account_queue.remove(user)

                if account_queue and not STOP_EVENT.is_set():
                    account_queue = account_queue[max_concurrent:] + account_queue[:max_concurrent]
                    _sleep_between_replies_sync(delay_min, delay_max, label="scan_delay")

        if not account_queue:
            warn("No quedan cuentas activas; el bot se detiene.")
            request_stop("sin cuentas activas para responder")

    except KeyboardInterrupt:
        request_stop("interrupcion con CtrlaC")
    finally:
        request_stop("auto-responder detenido")
        if listener:
            listener.join(timeout=0.1)
        ACTIVE_ALIAS = None
        _print_bot_summary(stats)

def _manual_stop() -> None:
    if STOP_EVENT.is_set():
        warn("El bot ya esta� detenido.")
    else:
        request_stop("detencia�n solicitada desde el mena�")
        warn("Si el bot esta� activo, finalizara� al terminar el ciclo en curso.")
    press_enter()


def menu_autoresponder(app_context=None):
    while True:
        _print_menu_header()
        choice = ask("Opcia�n: ").strip()
        if choice == "1":
            _configure_api_key()
        elif choice == "2":
            _configure_prompt()
        elif choice == "3":
            _activate_bot()
        elif choice == "4":
            _followup_menu()
        elif choice == "5":
            _gohighlevel_menu()
        elif choice == "6":
            _google_calendar_menu()
        elif choice == "7":
            _manual_stop()
        elif choice == "8":
            break
        else:
            warn("Opcia�n inva�lida.")
            press_enter()


# ------------- Extensiones para seguimiento con estado persistente ------------
import json as _json_mod_for_state
import os as _os_mod_for_state
from pathlib import Path as _Path_for_state
from typing import Dict as _Dict_for_state, List as _List_for_state, Optional as _Optional_for_state
import time as _time_for_state
from datetime import datetime as _datetime_for_state

# Determinamos la ruta del archivo de estado.  Si 'runtime_base' est� disponible,
# la utilizamos para resolver un directorio consistente; de lo contrario,
# usamos el directorio actual.
try:
    _CONV_STATE_PATH = runtime_base((_Path_for_state(__file__).resolve().parent)) / "storage" / "conversation_state.json"
except Exception:
    _CONV_STATE_PATH = _Path_for_state(__file__).resolve().parent / "storage" / "conversation_state.json"

# Constantes de limpieza (en d�as y segundos)
_CLEANUP_AFTER_DAYS_CLOSED = 90
_CLEANUP_AFTER_DAYS_FINISHED = 14
_CLEANUP_INTERVAL = 24 * 3600  # 24 horas

def _load_conversation_state() -> _Dict_for_state[str, object]:
    # Carga el estado de conversaciones desde el archivo JSON
    try:
        if _os_mod_for_state.path.exists(_CONV_STATE_PATH):
            with open(_CONV_STATE_PATH, "r", encoding="utf-8") as f:
                data = _json_mod_for_state.load(f)
            if isinstance(data, dict):
                data.setdefault("version", "1.0")
                data.setdefault("last_cleanup_ts", 0)
                if not isinstance(data.get("conversations"), dict):
                    data["conversations"] = {}
                return data
    except Exception as exc:
        try:
            logger.warning("No se pudo cargar el estado de conversaciones: %s", exc)
        except Exception:
            pass
    return {"version": "1.0", "last_cleanup_ts": 0, "conversations": {}}

def _save_conversation_state(state: _Dict_for_state[str, object]) -> None:
    # Guarda el estado de conversaciones en el archivo JSON de manera segura
    try:
        _os_mod_for_state.makedirs(_CONV_STATE_PATH.parent, exist_ok=True)
        tmp_path = _CONV_STATE_PATH.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            _json_mod_for_state.dump(state, f, ensure_ascii=False, indent=2)
        _os_mod_for_state.replace(tmp_path, _CONV_STATE_PATH)
    except Exception as exc:
        try:
            logger.warning("No se pudo guardar el estado de conversaciones: %s", exc)
        except Exception:
            pass

def _clean_conversation_state(state: _Dict_for_state[str, object]) -> _Dict_for_state[str, object]:
    # Elimina conversaciones antiguas del estado seg�n las reglas de limpieza
    now_ts = _time_for_state.time()
    last_cleanup_ts = state.get("last_cleanup_ts", 0) or 0
    try:
        last_cleanup_float = float(last_cleanup_ts)
    except Exception:
        last_cleanup_float = 0.0
    if now_ts - last_cleanup_float < _CLEANUP_INTERVAL:
        return state
    conversations = state.get("conversations", {})
    if not isinstance(conversations, dict):
        conversations = {}
    new_conversations: _Dict_for_state[str, object] = {}
    for key, rec in conversations.items():
        try:
            last_contact = rec.get("ultimo_contacto_ts") or rec.get("last_contact_ts")
            last_contact_float = float(last_contact) if last_contact else 0.0
        except Exception:
            last_contact_float = 0.0
        cerrado = bool(rec.get("cerrado"))
        if last_contact_float <= 0:
            new_conversations[key] = rec
            continue
        days_since = (now_ts - last_contact_float) / 86400.0
        if cerrado:
            if days_since <= _CLEANUP_AFTER_DAYS_CLOSED:
                new_conversations[key] = rec
        else:
            if days_since <= _CLEANUP_AFTER_DAYS_FINISHED:
                new_conversations[key] = rec
    state["conversations"] = new_conversations
    state["last_cleanup_ts"] = now_ts
    return state

def _process_followups_extended(
    client,
    user: str,
    api_key: str,
    delay_min: float = 0.0,
    delay_max: float = 0.0,
    max_age_days: int = 7,
    threads_limit: int = 15,
    followup_schedule_hours: Optional[List[int]] = None,
) -> None:
    # Implementaci�n extendida de seguimientos con memoria persistente
    alias, entry = _followup_enabled_entry_for(user)
    if not alias or not entry or not entry.get("enabled"):
        if not _FORCE_ALWAYS_FOLLOWUP:
            return
        alias = alias or ACTIVE_ALIAS or user
        entry = _get_followup_entry(alias) if alias else {}
    prompt_text = str(entry.get("prompt") or _DEFAULT_FOLLOWUP_PROMPT)
    if not prompt_text.strip():
        return

    conv_state = _load_conversation_state()
    conv_state = _clean_conversation_state(conv_state)

    history_source = entry.get("history")
    history: _Dict_for_state[str, dict] = dict(history_source) if isinstance(history_source, dict) else {}

    now_ts = _time_for_state.time()
    max_age_seconds = max(0, int(max_age_days)) * 24 * 3600 if max_age_days is not None else 0
    account_norm = _normalize_username(user)

    updated_history = False
    updated_state = False

    try:
        threads = client.list_threads(amount=threads_limit, filter_unread=False)
    except Exception as exc:
        logger.debug(
            "No se pudieron obtener hilos para seguimiento de @%s: %s",
            user,
            exc,
            exc_info=False,
        )
        return

    for thread in threads:
        if STOP_EVENT.is_set():
            break
        thread_id = getattr(thread, "id", None)
        if not thread_id:
            continue
        unread_count = getattr(thread, "unread_count", None)
        try:
            unread_int = int(unread_count)
        except Exception:
            unread_int = 0
        if unread_int > 0:
            continue

        participants = getattr(thread, "users", None)
        recipient_id: str | None = None
        recipient_username = ""
        if isinstance(participants, list):
            for participant in participants:
                pk_val = getattr(participant, "pk", None) or getattr(participant, "id", None)
                pk = str(pk_val) if pk_val is not None else ""
                if pk and pk != str(client.user_id):
                    recipient_id = pk
                    recipient_username = getattr(participant, "username", pk)
                    break
        if not recipient_id:
            continue

        try:
            messages = client.get_messages(thread, amount=20)
        except Exception as exc:
            logger.debug(
                "No se pudieron obtener mensajes del hilo %s para seguimiento: %s",
                thread_id,
                exc,
                exc_info=False,
            )
            continue
        if not messages:
            continue

        latest_ts = None
        for msg in messages:
            msg_ts = _message_timestamp(msg)
            if msg_ts is None:
                continue
            latest_ts = msg_ts if latest_ts is None else max(latest_ts, msg_ts)
        if max_age_seconds and (latest_ts is None or now_ts - latest_ts > max_age_seconds):
            continue

        last_message = messages[0]
        if not _same_user_id(getattr(last_message, 'user_id', ''), client.user_id):
            continue

        def _msg_ts(msg: object) -> float | None:
            ts_obj = getattr(msg, "timestamp", None)
            if isinstance(ts_obj, _datetime_for_state):
                return ts_obj.timestamp()
            try:
                return float(ts_obj)
            except Exception:
                return None

        last_outbound_ts = _msg_ts(last_message)
        if last_outbound_ts and now_ts - last_outbound_ts < 60:
            continue
        outbound_ts_values = [
            _msg_ts(msg)
            for msg in messages
            if _same_user_id(getattr(msg, 'user_id', ''), client.user_id) and _msg_ts(msg) is not None
        ]
        first_outbound_ts = min(outbound_ts_values) if outbound_ts_values else None

        inbound_messages = [
            msg
            for msg in messages
            if not _same_user_id(getattr(msg, 'user_id', ''), client.user_id) and isinstance(getattr(msg, "text", None), str)
        ]
        has_inbound = bool(inbound_messages)
        last_inbound = inbound_messages[0] if has_inbound else None
        last_inbound_ts = _msg_ts(last_inbound) if last_inbound else None
        if has_inbound and last_inbound_ts and now_ts - last_inbound_ts < 60:
            continue

        engine_state = _get_conversation_state(user, thread_id)
        last_sent_at = engine_state.get("last_message_sent_at")
        last_received_at = engine_state.get("last_message_received_at")
        if last_received_at and last_sent_at and last_received_at > last_sent_at:
            _append_message_log(
                {
                    "action": "followup_skip",
                    "reason": "lead_replied_after_last_bot_message",
                    "account": user,
                    "thread_id": str(thread_id),
                    "lead": recipient_username or str(recipient_id),
                }
            )
            continue
        if last_sent_at and now_ts - last_sent_at < _MIN_TIME_FOR_FOLLOWUP:
            _append_message_log(
                {
                    "action": "followup_skip",
                    "reason": "last_bot_message_too_recent",
                    "account": user,
                    "thread_id": str(thread_id),
                    "lead": recipient_username or str(recipient_id),
                    "seconds_since_last_sent": int(now_ts - last_sent_at),
                }
            )
            continue

        conv_key = f"{account_norm}|{thread_id}"
        convs = conv_state.setdefault("conversations", {})
        conv_record = convs.get(conv_key)
        if not isinstance(conv_record, dict):
            conv_record = {
                "seguimiento_actual": 0,
                "last_sent_ts": 0.0,
                "last_eval_ts": 0.0,
                "ultimo_contacto_ts": 0.0,
                "cerrado": False,
                "ultima_actualizacion_ts": now_ts,
            }
            convs[conv_key] = conv_record
            updated_state = True

        if conv_record.get("cerrado"):
            continue

        if has_inbound:
            conv_record["seguimiento_actual"] = 0
            conv_record["ultimo_contacto_ts"] = last_inbound_ts or now_ts
            conv_record["ultima_actualizacion_ts"] = now_ts
            updated_state = True

        try:
            last_eval_float = float(conv_record.get("last_eval_ts", 0) or 0)
        except Exception:
            last_eval_float = 0.0
        if now_ts - last_eval_float < _FOLLOWUP_MIN_INTERVAL:
            continue

        followups_sent = int(conv_record.get("seguimiento_actual", 0) or 0)
        last_followup_ts = conv_record.get("last_sent_ts") or 0.0
        try:
            last_followup_float = float(last_followup_ts)
        except Exception:
            last_followup_float = 0.0

        if first_outbound_ts is None:
            first_outbound_ts = conv_record.get("first_sent_ts") or last_outbound_ts
        if first_outbound_ts:
            conv_record.setdefault("first_sent_ts", first_outbound_ts)
        schedule = [h for h in (followup_schedule_hours or []) if isinstance(h, int) and h > 0]
        schedule = sorted(set(schedule))
        if schedule:
            if followups_sent >= len(schedule):
                continue
            if not first_outbound_ts:
                _append_message_log(
                    {
                        "action": "followup_skip",
                        "reason": "missing_first_sent_ts",
                        "account": user,
                        "thread_id": str(thread_id),
                        "lead": recipient_username or str(recipient_id),
                    }
                )
                continue
            hours_since_initial = (now_ts - first_outbound_ts) / 3600.0
            required_hours = schedule[followups_sent]
            if hours_since_initial < required_hours:
                _append_message_log(
                    {
                        "action": "followup_skip",
                        "reason": "schedule_not_reached",
                        "account": user,
                        "thread_id": str(thread_id),
                        "lead": recipient_username or str(recipient_id),
                        "hours_since_initial": round(hours_since_initial, 2),
                        "required_hours": required_hours,
                    }
                )
                continue

        if last_followup_float:
            time_since_last_followup = now_ts - last_followup_float
            min_gap_hours = None
            if schedule:
                next_stage = followups_sent + 1
                if next_stage > 1 and len(schedule) >= next_stage:
                    min_gap_hours = schedule[next_stage - 1] - schedule[next_stage - 2]
            if min_gap_hours is None:
                min_gap_known = _MIN_TIME_FOR_FOLLOWUP
            else:
                min_gap_known = max(1, int(min_gap_hours * 3600))
            if time_since_last_followup < min_gap_known:
                _append_message_log(
                    {
                        "action": "followup_skip",
                        "reason": "min_interval_not_met",
                        "account": user,
                        "thread_id": str(thread_id),
                        "lead": recipient_username or str(recipient_id),
                        "seconds_since_last_followup": int(time_since_last_followup),
                    }
                )
                continue

        conversation_lines: _List_for_state[str] = []
        for msg in reversed(messages[:40]):
            text_value = getattr(msg, "text", "") or ""
            prefix = "YO" if _same_user_id(getattr(msg, 'user_id', ''), client.user_id) else "ELLOS"
            conversation_lines.append(f"{prefix}: {text_value}")
        # Construimos el texto de la conversaci�n uniendo l�neas
        conversation_text = "\n".join(conversation_lines[-40:])

        metadata = {
            "alias": alias,
            "cuenta_origen": f"@{user}",
            "lead": recipient_username or str(recipient_id),
            "seguimientos_previos": followups_sent,
            "segundos_desde_ultimo_seguimiento": int(now_ts - last_followup_float)
            if last_followup_float
            else "nunca",
            "segundos_desde_ultima_respuesta": int(now_ts - last_inbound_ts)
            if last_inbound_ts
            else "desconocido",
            "segundos_desde_ultimo_mensaje_enviado": int(now_ts - last_outbound_ts)
            if last_outbound_ts
            else "desconocido",
        }

        _append_message_log(
            {
                "action": "followup_eval",
                "account": user,
                "thread_id": str(thread_id),
                "lead": recipient_username or str(recipient_id),
                "seguimientos_previos": followups_sent,
            }
        )
        decision = _followup_decision(api_key, prompt_text, conversation_text, metadata)
        conv_record["last_eval_ts"] = now_ts
        updated_state = True

        if not decision:
            _append_message_log(
                {
                    "action": "followup_skip",
                    "reason": "no_decision",
                    "account": user,
                    "thread_id": str(thread_id),
                    "lead": recipient_username or str(recipient_id),
                }
            )
            convs[conv_key] = conv_record
            record = history.get(conv_key, {})
            record["last_eval_ts"] = now_ts
            history[conv_key] = record
            updated_history = True
            continue

        message_text, stage = decision
        try:
            stage_int = int(stage)
        except Exception:
            stage_int = followups_sent + 1
        stage_int = max(1, stage_int)

        expected_stage = followups_sent + 1
        if stage_int != expected_stage:
            _append_message_log(
                {
                    "action": "followup_skip",
                    "reason": "stage_mismatch",
                    "account": user,
                    "thread_id": str(thread_id),
                    "lead": recipient_username or str(recipient_id),
                    "stage_requested": stage_int,
                    "stage_expected": expected_stage,
                }
            )
            continue

        logger.info(
            "Decision seguimiento @%s thread=%s stage=%s reason=%s",
            user,
            thread_id,
            stage_int,
            "ok",
        )

        _sleep_between_replies_sync(delay_min, delay_max, label="reply_delay")

        try:
            message_id = client.send_message(thread, message_text)
        except Exception as exc:
            logger.warning(
                "No se pudo enviar seguimiento automatico a %s desde @%s: %s",
                recipient_username or recipient_id,
                user,
                exc,
                exc_info=False,
            )
            conv_record["last_error"] = str(exc)
            convs[conv_key] = conv_record
            updated_state = True
            record = history.get(conv_key, {})
            record["last_error"] = str(exc)
            history[conv_key] = record
            updated_history = True
            continue
        if not message_id:
            logger.warning(
                "Seguimiento no verificado para @%s -> @%s (thread %s)",
                user,
                recipient_username or recipient_id,
                thread_id,
            )
            continue

        _append_message_log(
            {
                "action": "followup_sent",
                "account": user,
                "thread_id": str(thread_id),
                "lead": recipient_username or str(recipient_id),
                "followup_stage": stage_int,
                "message_id": message_id or "",
                "message_text": message_text,
            }
        )
        conv_record["seguimiento_actual"] = stage_int
        conv_record["last_sent_ts"] = now_ts
        conv_record.pop("last_error", None)
        conv_record["ultima_actualizacion_ts"] = now_ts
        convs[conv_key] = conv_record
        updated_state = True

        record = history.get(conv_key, {})
        record["count"] = stage_int
        record["last_sent_ts"] = now_ts
        record["last_message_id"] = message_id or ""
        record.pop("last_error", None)
        history[conv_key] = record
        updated_history = True

        try:
            print(
                style_text(
                    f"[Seguimiento] @{user} -> @{recipient_username}: mensaje etapa {stage_int}",
                    color=Fore.MAGENTA,
                )
            )
        except Exception:
            print(f"[Seguimiento] @{user} -> @{recipient_username}: mensaje etapa {stage_int}")

    # Guardamos cambios al terminar
    if updated_state:
        _save_conversation_state(conv_state)
    if updated_history:
        _set_followup_entry(alias, {"history": history})

# Sustituimos la implementaci�n original por la extendida
_process_followups = _process_followups_extended
