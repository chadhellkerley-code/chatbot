# leads.py
# -*- coding: utf-8 -*-
import csv
import json
import logging
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from accounts import (
    auto_login_with_saved_password,
    get_account,
    has_valid_session_settings,
    list_all,
    mark_connected,
    prompt_login,
)
from paths import runtime_base
from proxy_manager import apply_proxy_to_client, record_proxy_failure, should_retry_proxy
from session_store import has_session, load_into
from templates_store import load_templates, save_templates
from client_factory import get_instagram_client
from utils import (
    ask,
    ask_int,
    ask_multiline,
    banner,
    ok,
    press_enter,
    title,
    warn,
)

BASE = runtime_base(Path(__file__).resolve().parent)
BASE.mkdir(parents=True, exist_ok=True)
TEXT = BASE / "text" / "leads"
TEXT.mkdir(parents=True, exist_ok=True)


def _looks_like_login_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(keyword in msg for keyword in ("login", "session", "credential"))

def list_files()->List[str]:
    return sorted([p.stem for p in TEXT.glob("*.txt")])

def load_list(name:str)->List[str]:
    p=TEXT/f"{name}.txt"
    if not p.exists(): return []
    return [line.strip().lstrip("@") for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]

def append_list(name:str, usernames:List[str]):
    p=TEXT/f"{name}.txt"
    with p.open("a", encoding="utf-8") as f:
        for u in usernames:
            f.write(u.strip().lstrip("@")+"\n")


def save_list(name: str, usernames: List[str]) -> None:
    p = TEXT / f"{name}.txt"
    with p.open("w", encoding="utf-8") as f:
        for u in usernames:
            f.write(u.strip().lstrip("@") + "\n")

def import_csv(path:str, name:str):
    path=Path(path)
    if not path.exists():
        warn("CSV no encontrado."); return
    users=[]
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if not row: continue
            users.append(row[0].strip().lstrip("@"))
    append_list(name, users)
    ok(f"Importados {len(users)} a {name}.")

def show_list(name:str):
    users=load_list(name)
    print(f"{name}: {len(users)} usuarios")
    for i,u in enumerate(users[:50],1):
        print(f"{i:02d}. @{u}")
    if len(users)>50: print(f"... (+{len(users)-50})")

def delete_list(name:str):
    p=TEXT/f"{name}.txt"
    if p.exists(): p.unlink(); ok("Eliminada.")
    else: warn("No existe.")


def _template_preview(text: str, limit: int = 60) -> str:
    cleaned = " ".join((text or "").splitlines()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 3)] + "..."


def _list_templates() -> List[Dict[str, str]]:
    templates = load_templates()
    if not templates:
        warn("No hay plantillas guardadas.")
        return []
    for idx, item in enumerate(templates, start=1):
        preview = _template_preview(item.get("text", ""))
        print(f" {idx}) {item.get('name', '')} - {preview}")
    return templates


def _select_template_index(templates: List[Dict[str, str]]) -> Optional[int]:
    if not templates:
        return None
    choice = ask("Selecciona numero de plantilla (Enter para cancelar): ").strip()
    if not choice:
        return None
    if not choice.isdigit():
        warn("Seleccion invalida.")
        return None
    idx = int(choice)
    if 1 <= idx <= len(templates):
        return idx - 1
    warn("Seleccion fuera de rango.")
    return None


def menu_templates() -> None:
    while True:
        banner()
        title("Plantillas")
        templates = load_templates()
        print(f"Plantillas guardadas: {len(templates)}")
        print("\n1) Crear plantilla")
        print("2) Listar plantillas")
        print("3) Editar plantilla")
        print("4) Eliminar plantilla")
        print("5) Volver\n")
        op = ask("Opcion: ").strip()
        if op == "1":
            name = ask("Nombre de la plantilla: ").strip()
            if not name:
                warn("Nombre requerido.")
                press_enter()
                continue
            text = ask_multiline("Texto de la plantilla:")
            if not text:
                warn("Texto requerido.")
                press_enter()
                continue
            templates = load_templates()
            templates.append({"name": name, "text": text})
            save_templates(templates)
            ok("Plantilla guardada.")
            press_enter()
        elif op == "2":
            banner()
            title("Listado de plantillas")
            _list_templates()
            press_enter()
        elif op == "3":
            banner()
            title("Editar plantilla")
            templates = _list_templates()
            idx = _select_template_index(templates)
            if idx is None:
                press_enter()
                continue
            current = templates[idx]
            print("\nTexto actual:\n")
            print(current.get("text", ""))
            new_name = ask(f"Nombre ({current.get('name', '')}): ").strip()
            new_text = ask_multiline("Nuevo texto (vacio para mantener):")
            if new_name:
                current["name"] = new_name
            if new_text:
                current["text"] = new_text
            templates[idx] = current
            save_templates(templates)
            ok("Plantilla actualizada.")
            press_enter()
        elif op == "4":
            banner()
            title("Eliminar plantilla")
            templates = _list_templates()
            idx = _select_template_index(templates)
            if idx is None:
                press_enter()
                continue
            target = templates[idx]
            confirm = ask(f"Eliminar '{target.get('name', '')}'? (s/N): ").strip().lower()
            if confirm == "s":
                templates.pop(idx)
                save_templates(templates)
                ok("Plantilla eliminada.")
            else:
                warn("Sin cambios.")
            press_enter()
        elif op == "5":
            break
        else:
            warn("Opcion invalida.")
            press_enter()

def menu_leads():
    while True:
        banner()
        title("Listas de leads")
        files=list_files()
        if files: print("Disponibles:", ", ".join(files))
        else: print("(aún no hay listas)")
        print("\n1) Crear lista y agregar manual")
        print("2) Importar CSV a una lista")
        print("3) Ver lista")
        print("4) Eliminar lista")
        print("5) Gestionar plantillas")
        print("6) Filtrado profesional de leads (IA)")
        print("7) Volver\n")
        op=ask("Opcion: ").strip()
        if op=="1":
            name=ask("Nombre de la lista: ").strip() or "default"
            print("Pegá usernames (uno por línea). Línea vacía para terminar:")
            lines=[]
            while True:
                s=ask("")
                if not s: break
                lines.append(s)
            append_list(name, lines); ok("Guardado."); press_enter()
        elif op=="2":
            path=ask("Ruta del CSV: ")
            name=ask("Importar a la lista (nombre): ").strip() or "default"
            import_csv(path, name); press_enter()
        elif op=="3":
            name=ask("Nombre de la lista: ").strip()
            show_list(name); press_enter()
        elif op=="4":
            name=ask("Nombre de la lista: ").strip()
            delete_list(name); press_enter()
        elif op=="5":
            menu_templates()
        elif op=="6":
            _lead_filtering_main_menu()
        elif op=="7":
            break
        else:
            warn("Opción inválida."); press_enter()


@dataclass
class LeadFilterConfig:
    name: str
    min_followers: int = 0
    max_followers: int = 0
    min_posts: int = 0
    max_posts: int = 0
    privacy: str = "any"  # any, public, private
    has_link_in_bio: bool = False
    keywords: List[str] = field(default_factory=list)
    llm_criterion: str = ""
    profile_pic_prompt: str = ""
    target_sex: str = "indifferent"  # male, female, indifferent
    min_age: int = 0
    max_age: int = 0


@dataclass
class LeadFilteringSession:
    id: str
    filter_name: str
    leads_total: int
    leads_processed: int
    leads_qualified: List[str]
    leads_disqualified: List[str]
    leads_pending: List[str] = field(default_factory=list)
    status: str = "running"  # running, paused, completed
    last_updated: float = field(default_factory=time.time)


def load_lead_filters() -> List[LeadFilterConfig]:
    p = Path("storage/lead_filters.json")
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [LeadFilterConfig(**item) for item in data]
    except Exception:
        return []


def save_lead_filters(filters: List[LeadFilterConfig]):
    p = Path("storage/lead_filters.json")
    data = [asdict(f) for f in filters]
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_filtering_sessions() -> Dict[str, dict]:
    p = Path("storage/filtering_sessions.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_filtering_session(session: LeadFilteringSession):
    p = Path("storage/filtering_sessions.json")
    sessions = load_filtering_sessions()
    sessions[session.id] = asdict(session)
    p.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass
class PromptCriteria:
    include_groups: List[Set[str]]
    optional_terms: Set[str]
    exclude_terms: Set[str]
    min_followers: Optional[int] = None
    max_followers: Optional[int] = None
    min_posts: Optional[int] = None
    max_posts: Optional[int] = None

    def has_conditions(self) -> bool:
        return bool(
            self.include_groups
            or self.optional_terms
            or self.exclude_terms
            or self.min_followers
            or self.max_followers
            or self.min_posts
            or self.max_posts
        )


class DelayController:
    def __init__(self, delay: float) -> None:
        self._delay = max(0.0, float(delay))
        self._last_recorded: Optional[float] = None

    def pause(self) -> None:
        if self._delay <= 0:
            self._last_recorded = time.monotonic()
            return
        now = time.monotonic()
        if self._last_recorded is None:
            self._last_recorded = now
            return
        jitter = min(2.0, self._delay * 0.3 + 0.5)
        lower = max(0.5, self._delay - jitter)
        upper = self._delay + jitter
        target = random.uniform(lower, upper)
        elapsed = now - self._last_recorded
        remaining = max(0.0, target - elapsed)
        if remaining > 0:
            time.sleep(remaining)
            now = time.monotonic()
        self._last_recorded = now


def _resolve_media_user(media) -> Tuple[Optional[int], Optional[object]]:
    if media is None:
        return None, None
    user = getattr(media, "user", None)
    user_id = _extract_user_id(user) if user is not None else None
    if user_id:
        return user_id, user
    owner = getattr(media, "owner", None)
    owner_id = _extract_user_id(owner) if owner is not None else None
    if owner_id:
        return owner_id, owner
    user_id_attr = getattr(media, "user_id", None)
    if user_id_attr is not None:
        try:
            return int(user_id_attr), None
        except Exception:
            pass
    if isinstance(media, dict):
        for key in ("user", "owner"):
            candidate = media.get(key)
            if candidate:
                candidate_id = _extract_user_id(candidate)
                if candidate_id:
                    return candidate_id, candidate
        user_id_attr = media.get("user_id")
        if user_id_attr is not None:
            try:
                return int(user_id_attr), None
            except Exception:
                pass
    return None, None


def _apply_prompt_filter(users: List[ScrapedUser]) -> List[ScrapedUser]:
    if not users:
        warn("No hay usuarios para filtrar.")
        return users
    print(
        "\nEscribí un prompt describiendo los perfiles que buscás. "
        "El sistema analizará bios, nombres y usuarios para encontrar coincidencias."
    )
    prompt_text = ask_multiline("Prompt: ").strip()
    if not prompt_text:
        warn("No se ingresó un prompt. Se mantiene la lista actual.")
        return users
    criteria = _interpret_prompt(prompt_text)
    if not criteria.has_conditions():
        warn(
            "No se identificaron condiciones claras en el prompt. "
            "Probá con una descripción más específica."
        )
        return users
    matched: List[ScrapedUser] = []
    for user in users:
        if _matches_prompt(user, criteria):
            matched.append(user)
    if not matched:
        warn("Ningún perfil coincide con el prompt. Se mantiene la lista actual.")
        return users
    print("\nCriterios interpretados:")
    if criteria.min_followers:
        print(f" - Seguidores mínimos: {criteria.min_followers}")
    if criteria.max_followers:
        print(f" - Seguidores máximos: {criteria.max_followers}")
    if criteria.min_posts:
        print(f" - Posteos mínimos: {criteria.min_posts}")
    if criteria.max_posts:
        print(f" - Posteos máximos: {criteria.max_posts}")
    if criteria.include_groups:
        for idx, group in enumerate(criteria.include_groups, start=1):
            readable = ", ".join(sorted(group))
            print(f" - Condición {idx}: {readable}")
    if criteria.exclude_terms:
        print(f" - Excluir si contiene: {', '.join(sorted(criteria.exclude_terms))}")
    print(
        f"\nPerfiles coincidentes con el prompt: {len(matched)} "
        f"(de {len(users)})."
    )
    preview = matched[:10]
    if preview:
        print("Ejemplos:")
        for idx, user in enumerate(preview, start=1):
            snippet = (user.biography or user.full_name or "").strip()
            extra = f" — {snippet[:60]}" if snippet else ""
            print(f" {idx:02d}. @{user.username}{extra}")
    confirm = ask(
        "¿Reemplazar la lista actual con los perfiles encontrados por el prompt? (s/N): "
    ).strip().lower()
    if confirm != "s":
        warn("Se mantiene la lista sin cambios.")
        return users
    return matched


def _apply_advanced_filter(users: List[ScrapedUser]) -> List[ScrapedUser]:
    if not users:
        warn("No hay usuarios para filtrar.")
        return users
    print(
        "\nIngresá palabras o frases clave a buscar en la bio, nombre o usuario. "
        "Separalas con comas o saltos de línea."
    )
    print("Podés anteponer '-' para excluir términos específicos.")
    raw = ask_multiline("Condiciones: ").strip()
    if not raw:
        warn("No se ingresaron filtros. Se mantiene la lista actual.")
        return users
    tokens = [chunk.strip() for chunk in raw.replace("\n", ",").split(",")]
    includes = [t.lstrip("+").lower() for t in tokens if t and not t.startswith("-")]
    excludes = [t[1:].lower() for t in tokens if t.startswith("-") and len(t) > 1]
    includes = [t for t in includes if t]
    excludes = [t for t in excludes if t]
    if not includes and not excludes:
        warn("No se ingresaron filtros válidos. Se mantiene la lista actual.")
        return users
    mode = (
        ask(
            "¿Las palabras obligatorias deben aparecer todas (T) o al menos una (A)? (A/T): "
        )
        .strip()
        .lower()
    )
    require_all = mode == "t"
    filtered: List[ScrapedUser] = []
    for user in users:
        haystack = " ".join(
            filter(
                None,
                [
                    getattr(user, "username", "") or "",
                    getattr(user, "full_name", "") or "",
                    getattr(user, "biography", "") or "",
                ],
            )
        ).lower()
        if includes:
            if require_all:
                if not all(term in haystack for term in includes):
                    continue
            else:
                if not any(term in haystack for term in includes):
                    continue
        if excludes and any(term in haystack for term in excludes):
            continue
        filtered.append(user)
    if not filtered:
        warn(
            "Ningún perfil coincidió con los filtros avanzados. Se mantiene la lista actual."
        )
        return users
    print(f"\nPerfiles tras el filtrado avanzado: {len(filtered)} (de {len(users)}).")
    preview = filtered[:10]
    if preview:
        print("Ejemplos filtrados:")
        for idx, user in enumerate(preview, start=1):
            snippet = (user.biography or user.full_name or "").strip()
            extra = f" — {snippet[:60]}" if snippet else ""
            print(f" {idx:02d}. @{user.username}{extra}")
    confirm = ask("¿Aplicar este filtrado a la lista actual? (s/N): ").strip().lower()
    if confirm != "s":
        warn("Se mantiene la lista sin cambios.")
        return users
    return filtered


_PROMPT_STOPWORDS = {
    "a",
    "acerca",
    "ademas",
    "al",
    "algo",
    "algun",
    "alguna",
    "algunas",
    "algunos",
    "ante",
    "antes",
    "aqui",
    "asi",
    "aunque",
    "busco",
    "buscar",
    "cada",
    "casi",
    "como",
    "con",
    "contra",
    "cual",
    "cuales",
    "cualquier",
    "cuenta",
    "cuyo",
    "cuya",
    "cuyos",
    "cuyas",
    "de",
    "del",
    "desde",
    "donde",
    "durante",
    "el",
    "ella",
    "ellas",
    "ellos",
    "en",
    "entre",
    "es",
    "esa",
    "esas",
    "ese",
    "eso",
    "esta",
    "estan",
    "estas",
    "este",
    "esto",
    "estos",
    "etc",
    "gente",
    "habla",
    "hablan",
    "hablar",
    "hablen",
    "hacia",
    "hacen",
    "hacer",
    "hasta",
    "incluye",
    "incluyen",
    "incluir",
    "la",
    "las",
    "le",
    "les",
    "lo",
    "los",
    "mas",
    "menos",
    "mientras",
    "misma",
    "mismas",
    "mismo",
    "mismos",
    "necesito",
    "necesitamos",
    "ningun",
    "ninguna",
    "no",
    "nos",
    "nuestro",
    "nuestra",
    "nuestras",
    "nuestros",
    "o",
    "otra",
    "otras",
    "otro",
    "otros",
    "para",
    "perfiles",
    "perfil",
    "personas",
    "pero",
    "por",
    "porque",
    "preferible",
    "preferiblemente",
    "preferentemente",
    "prefiero",
    "que",
    "quien",
    "quienes",
    "quiero",
    "quiere",
    "queremos",
    "relacion",
    "relaciona",
    "relacionado",
    "relacionada",
    "relacionados",
    "relacionadas",
    "requiere",
    "requieren",
    "requiro",
    "residan",
    "residen",
    "sea",
    "sean",
    "segun",
    "si",
    "sin",
    "sobre",
    "solamente",
    "solo",
    "somos",
    "son",
    "seguidor",
    "seguidores",
    "followers",
    "fans",
    "su",
    "sus",
    "tal",
    "tambien",
    "tan",
    "tanto",
    "tengan",
    "tener",
    "tengo",
    "tema",
    "temas",
    "tipo",
    "tipos",
    "toda",
    "todas",
    "todo",
    "todos",
    "post",
    "posts",
    "posteos",
    "publicaciones",
    "publicacion",
    "contenido",
    "contenidos",
    "trabajan",
    "trabajen",
    "ubicada",
    "ubicadas",
    "ubicado",
    "ubicados",
    "ubicacion",
    "un",
    "una",
    "unas",
    "uno",
    "unos",
    "usuarios",
    "usuario",
    "usar",
    "varias",
    "varios",
    "vive",
    "viven",
    "vivir",
    "vivan",
    "y",
    "ya",
}

_PROMPT_NEGATIONS = {
    "sin",
    "no",
    "excepto",
    "excepta",
    "exceptos",
    "exceptas",
    "excluir",
    "excluye",
    "excluyen",
    "evitar",
    "evita",
    "eviten",
    "salvo",
    "salvos",
    "salvas",
    "menos",
}

_PROMPT_FOLLOWER_KEYWORDS = (
    "seguidor",
    "seguidores",
    "followers",
    "fans",
)

_PROMPT_POST_KEYWORDS = (
    "post",
    "posts",
    "posteos",
    "publicaciones",
    "publicacion",
    "contenido",
    "contenidos",
)


def _normalize_text(value: str) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", str(value))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    normalized = normalized.lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


_RAW_PROMPT_SYNONYMS = {
    "argentina": {"argentina", "argentino", "argentina", "buenos aires", "cordoba"},
    "bolivia": {"bolivia", "boliviano", "boliviana", "la paz", "santa cruz"},
    "chile": {"chile", "chileno", "chilena", "santiago", "valparaiso"},
    "colombia": {"colombia", "colombiano", "colombiana", "bogota", "medellin"},
    "costarica": {"costa rica", "costarricense", "tico", "tica"},
    "ecuador": {"ecuador", "ecuatoriano", "ecuatoriana", "quito", "guayaquil"},
    "espana": {"espana", "spain", "madrid", "barcelona", "sevilla", "valencia", "espanol", "espanola"},
    "europa": {"europa", "europe", "europeo", "europea", "union europea"},
    "latinoamerica": {"latinoamerica", "latam", "latino", "latina"},
    "mexico": {"mexico", "mx", "cdmx", "ciudad de mexico", "mexicana", "mexicano", "monterrey", "guadalajara"},
    "peru": {"peru", "peruano", "peruana", "lima"},
    "uruguay": {"uruguay", "uruguayo", "uruguaya", "montevideo"},
    "venezuela": {"venezuela", "venezolano", "venezolana", "caracas"},
    "espanol": {"espanol", "castellano", "spanish", "hablo espanol", "idioma espanol"},
    "ingles": {"ingles", "english", "bilingue", "bilingual"},
    "portugues": {"portugues", "portuguese", "brasil", "brasileno", "brasilena", "brasilero", "brasilera"},
    "mujer": {"mujer", "mujeres", "female", "femenino", "femenina", "women", "woman", "chica", "damas", "girls"},
    "hombre": {"hombre", "hombres", "male", "masculino", "masculina", "men", "man"},
    "coaching": {"coaching", "coach", "coaches", "mentora", "mentor", "mentoring", "mentoria", "mentorias"},
    "negocios": {"negocio", "negocios", "business", "empresa", "empresas", "empresaria", "empresario", "emprendimiento", "emprendedor", "emprendedora", "startup", "startups"},
    "liderazgo": {"liderazgo", "lider", "lideres", "leader", "leadership", "liderar"},
    "marketing": {"marketing", "marketer", "mercadotecnia", "growth", "digital marketing", "publicidad", "ads"},
    "ventas": {"ventas", "sales", "vendedor", "vendedora", "seller", "comercial", "comerciales"},
    "finanzas": {"finanzas", "finance", "financiero", "financiera", "financial"},
    "tecnologia": {"tecnologia", "technology", "tech", "tecnologico", "tecnologica", "software", "it"},
    "emprendedor": {"emprendedor", "emprendedora", "emprendedores", "emprendedoras", "founder", "founders", "cofounder", "cofounders", "cofundador", "cofundadora"},
    "wellness": {"wellness", "bienestar", "health", "healthy"},
    "inversion": {"inversion", "inversiones", "investor", "investors", "angel", "venture", "capital"},
    "freelance": {"freelance", "freelancer", "independiente", "autonomo", "autonoma"},
}


def _build_prompt_synonyms() -> Dict[str, Set[str]]:
    mapping: Dict[str, Set[str]] = {}
    for key, raw_terms in _RAW_PROMPT_SYNONYMS.items():
        normalized_key = _normalize_text(key)
        bucket: Set[str] = set()
        for term in raw_terms:
            normalized_term = _normalize_text(term)
            if normalized_term and normalized_term not in _PROMPT_STOPWORDS:
                bucket.add(normalized_term)
        if normalized_key and normalized_key not in _PROMPT_STOPWORDS:
            bucket.add(normalized_key)
        if bucket:
            mapping[normalized_key] = bucket
    return mapping


_PROMPT_SYNONYMS = _build_prompt_synonyms()


def _clean_int(raw: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", raw or "")
    if not digits:
        return None
    try:
        return int(digits)
    except Exception:
        return None


def _parse_numeric_bounds(text: str, keywords: Tuple[str, ...]) -> Tuple[Optional[int], Optional[int]]:
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    for keyword in keywords:
        if not keyword:
            continue
        pattern_min = rf"(?:mas de|al menos|minimo(?: de)?|mayor a|superior a|mas que|>=|>\s*)(\d[\d\s\.,]*)\s*{keyword}"
        for match in re.finditer(pattern_min, text):
            value = _clean_int(match.group(1))
            if value is not None:
                min_value = value if min_value is None else max(min_value, value)
        pattern_plus = rf"(\d[\d\s\.,]*)\s*(?:\+|o mas)\s*{keyword}"
        for match in re.finditer(pattern_plus, text):
            value = _clean_int(match.group(1))
            if value is not None:
                min_value = value if min_value is None else max(min_value, value)
        pattern_max = rf"(?:menos de|no mas de|maximo(?: de)?|hasta|menor a|inferior a|<=|<\s*)(\d[\d\s\.,]*)\s*{keyword}"
        for match in re.finditer(pattern_max, text):
            value = _clean_int(match.group(1))
            if value is not None:
                max_value = value if max_value is None else min(max_value, value)
    return min_value, max_value


def _tokenize_prompt_segment(segment: str) -> List[str]:
    normalized = _normalize_text(segment)
    if not normalized:
        return []
    tokens = normalized.split(" ")
    cleaned: List[str] = []
    for token in tokens:
        if not token or token in _PROMPT_STOPWORDS or token.isdigit():
            continue
        cleaned.append(token)
    return cleaned


def _expand_prompt_token(token: str) -> Set[str]:
    normalized = _normalize_text(token)
    if not normalized or normalized in _PROMPT_STOPWORDS:
        return set()
    expanded: Set[str] = {normalized}
    if normalized.endswith("es") and len(normalized) > 4:
        expanded.add(normalized[:-2])
    if normalized.endswith("s") and len(normalized) > 3:
        expanded.add(normalized[:-1])
    synonyms = _PROMPT_SYNONYMS.get(normalized)
    if synonyms:
        expanded.update(synonyms)
    for key, group in _PROMPT_SYNONYMS.items():
        if normalized in group:
            expanded.update(group)
            expanded.add(key)
    return {term for term in expanded if term and term not in _PROMPT_STOPWORDS}


def _interpret_prompt(prompt: str) -> PromptCriteria:
    normalized_prompt = _normalize_text(prompt)
    criteria = PromptCriteria(include_groups=[], optional_terms=set(), exclude_terms=set())
    if not normalized_prompt:
        return criteria
    criteria.min_followers, criteria.max_followers = _parse_numeric_bounds(
        normalized_prompt, _PROMPT_FOLLOWER_KEYWORDS
    )
    criteria.min_posts, criteria.max_posts = _parse_numeric_bounds(
        normalized_prompt, _PROMPT_POST_KEYWORDS
    )
    working = re.sub(r"[,]+", ".", normalized_prompt)
    clauses = [clause.strip() for clause in re.split(r"[\.;\n]+", working) if clause.strip()]
    if not clauses:
        clauses = [working]
    for clause in clauses:
        negated = any(re.search(rf"\b{word}\b", clause) for word in _PROMPT_NEGATIONS)
        cleaned_clause = clause
        if negated:
            for word in _PROMPT_NEGATIONS:
                cleaned_clause = re.sub(rf"\b{word}\b", " ", cleaned_clause)
        and_parts = re.split(r"\b(?:y|e|ademas|tambien|asi como|mas)\b", cleaned_clause)
        for part in and_parts:
            or_terms: Set[str] = set()
            for segment in re.split(r"\b(?:o|u)\b", part):
                tokens = _tokenize_prompt_segment(segment)
                expanded: Set[str] = set()
                for token in tokens:
                    expanded.update(_expand_prompt_token(token))
                if expanded:
                    or_terms.update(expanded)
            if not or_terms:
                continue
            if negated:
                criteria.exclude_terms.update(or_terms)
            else:
                criteria.include_groups.append(or_terms)
                criteria.optional_terms.update(or_terms)
    quoted = re.findall(r'["“”\']([^"“”\']+)["“”\']', prompt)
    for phrase in quoted:
        normalized_phrase = _normalize_text(phrase)
        if not normalized_phrase:
            continue
        expanded = _expand_prompt_token(normalized_phrase) or {normalized_phrase}
        criteria.include_groups.append(expanded)
        criteria.optional_terms.update(expanded)
    criteria.exclude_terms = {
        term for term in criteria.exclude_terms if term not in criteria.optional_terms
    }
    return criteria


def _term_in_haystack(term: str, haystack: str) -> bool:
    normalized_term = _normalize_text(term)
    if not normalized_term:
        return False
    if " " in normalized_term:
        return normalized_term in haystack
    pattern = rf"\b{re.escape(normalized_term)}\b"
    return bool(re.search(pattern, haystack))


def _matches_prompt(user: ScrapedUser, criteria: PromptCriteria) -> bool:
    haystack_parts = [
        getattr(user, "username", "") or "",
        getattr(user, "full_name", "") or "",
        getattr(user, "biography", "") or "",
    ]
    combined = " ".join(part for part in haystack_parts if part).strip()
    normalized_haystack = _normalize_text(combined)
    padded = f" {normalized_haystack} " if normalized_haystack else ""
    if criteria.exclude_terms and padded:
        for term in criteria.exclude_terms:
            if _term_in_haystack(term, padded):
                return False
    follower_count = int(getattr(user, "follower_count", 0) or 0)
    if criteria.min_followers and follower_count < criteria.min_followers:
        return False
    if criteria.max_followers and follower_count > criteria.max_followers:
        return False
    media_count = int(getattr(user, "media_count", 0) or 0)
    if criteria.min_posts and media_count < criteria.min_posts:
        return False
    if criteria.max_posts and media_count > criteria.max_posts:
        return False
    if criteria.include_groups:
        for group in criteria.include_groups:
            if not any(_term_in_haystack(term, padded) for term in group):
                return False
    elif criteria.optional_terms:
        if not any(_term_in_haystack(term, padded) for term in criteria.optional_terms):
            return False
    return True


def _extract_user_id(user) -> Optional[int]:
    for attr in ("pk", "id"):
        value = getattr(user, attr, None)
        if value is None:
            continue
        try:
            return int(value)
        except Exception:
            continue
    return None


def _format_user(user_info, position: int, limit: int) -> str:
    username = getattr(user_info, "username", "?")
    follower_count = int(getattr(user_info, "follower_count", 0) or 0)
    media_count = int(getattr(user_info, "media_count", 0) or 0)
    privacy = "privada" if getattr(user_info, "is_private", False) else "pública"
    return (
        f" {position:02d}/{limit:02d} → @{username} | "
        f"seguidores: {follower_count:,} | posteos: {media_count} | {privacy}"
    )




class PlaywrightProfileScraper:
    def __init__(self, page: Any):
        self.page = page

    def scrape_profile(self, username: str) -> Optional[Dict[str, Any]]:
        if not self.page:
            return None

        url = f"https://www.instagram.com/{username}/"
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                self.page.wait_for_selector("header", timeout=10000)
            except:
                # If header doesn't appear, maybe account not found or challenge
                if "login" in self.page.url:
                    raise RuntimeError("Sesión perdida (login detectado)")
                return None

            time.sleep(1.5)

            full_name = ""
            bio = ""
            followers = 0
            posts = 0
            is_private = False
            profile_pic_url = ""
            external_url = ""

            # Privacy
            is_private = self.page.locator("svg[aria-label='Esta cuenta es privada'], svg[aria-label='Private']").count() > 0

            header = self.page.locator("header")

            # Stats
            stats = header.locator("ul li")
            for i in range(stats.count()):
                try:
                    text = stats.nth(i).inner_text().lower()
                    if "seguidor" in text or "follower" in text:
                        followers = self._parse_count(text)
                    elif "publicaci" in text or "post" in text:
                        posts = self._parse_count(text)
                except:
                    continue

            # Bio area
            try:
                bio_elements = header.locator("div[dir='auto']")
                if bio_elements.count() > 0:
                    bio = "\n".join(bio_elements.all_inner_texts())

                name_el = header.locator("h1, h2").first
                if name_el.count() > 0:
                    full_name = name_el.inner_text()
            except:
                pass

            # Link
            try:
                link_el = header.locator("a[target='_blank']").first
                if link_el.count() > 0:
                    external_url = link_el.get_attribute("href") or ""
            except:
                pass

            # Profile Pic
            try:
                img_el = header.locator("img").first
                if img_el.count() > 0:
                    profile_pic_url = img_el.get_attribute("src") or ""
            except:
                pass

            return {
                "username": username,
                "full_name": full_name,
                "biography": bio,
                "follower_count": followers,
                "media_count": posts,
                "is_private": is_private,
                "external_url": external_url,
                "profile_pic_url": profile_pic_url
            }
        except Exception as e:
            logging.error(f"Error scraping profile {username}: {e}")
            if "Sesión perdida" in str(e):
                raise
            return None

    def _parse_count(self, text: str) -> int:
        try:
            raw = text.split()[0].lower().replace(".", "").replace(",", "")
            if "k" in raw:
                return int(float(raw.replace("k", "")) * 1000)
            if "m" in raw:
                return int(float(raw.replace("m", "")) * 1000000)
            return int(re.sub(r"[^0-9]", "", raw))
        except:
            return 0


def _passes_classic_filters(data: Dict[str, Any], config: LeadFilterConfig) -> bool:
    if config.min_followers and data["follower_count"] < config.min_followers:
        return False
    if config.max_followers and data["follower_count"] > 0 and data["follower_count"] > config.max_followers:
        return False
    if config.min_posts and data["media_count"] < config.min_posts:
        return False
    if config.max_posts and data["media_count"] > 0 and data["media_count"] > config.max_posts:
        return False
    if config.privacy == "public" and data["is_private"]:
        return False
    if config.privacy == "private" and not data["is_private"]:
        return False
    if config.has_link_in_bio and not data["external_url"]:
        return False
    if config.keywords:
        haystack = f"{data['username']} {data['full_name']} {data['biography']}".lower()
        if not any(k.lower() in haystack for k in config.keywords):
            return False
    return True


def _download_profile_pic(url: str, username: str) -> Optional[str]:
    if not url:
        return None
    try:
        import requests
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            temp_dir = Path("storage/temp_pics")
            temp_dir.mkdir(parents=True, exist_ok=True)
            file_path = temp_dir / f"{username}.jpg"
            file_path.write_bytes(resp.content)
            return str(file_path)
    except Exception as e:
        logging.error(f"Error downloading profile pic for {username}: {e}")
    return None


_CLIP_MODEL = None
_CLIP_PREPROCESS = None

def _clip_filter(image_path: str, prompt: str) -> bool:
    global _CLIP_MODEL, _CLIP_PREPROCESS
    if not prompt:
        return True
    try:
        import torch
        import clip
        from PIL import Image

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if _CLIP_MODEL is None:
            _CLIP_MODEL, _CLIP_PREPROCESS = clip.load("ViT-B/32", device=device)

        image = _CLIP_PREPROCESS(Image.open(image_path)).unsqueeze(0).to(device)
        text = clip.tokenize([prompt, "un perfil de instagram irrelevante"]).to(device)

        with torch.no_grad():
            logits_per_image, _ = _CLIP_MODEL(image, text)
            probs = logits_per_image.softmax(dim=-1).cpu().numpy()

        # Si la probabilidad de que coincida con el prompt es mayor a la del fallback
        return probs[0][0] > 0.6 # Un poco más estricto
    except ImportError:
        logging.warning("Bibliotecas CLIP/torch no encontradas. Saltando filtrado de imagen.")
        return True
    except Exception as e:
        logging.error(f"Error en el filtro CLIP: {e}")
        return False


def _deepface_filter(image_path: str, target_sex: str, min_age: int = 0, max_age: int = 0) -> bool:
    if target_sex == "indifferent" and not min_age and not max_age:
        return True
    try:
        from deepface import DeepFace

        results = DeepFace.analyze(img_path=image_path, actions=['gender', 'age'], enforce_detection=False)
        if not results:
            return False

        res = results[0]

        # Sex filter
        if target_sex != "indifferent":
            dominant_gender = res.get("dominant_gender", "").lower()
            # DeepFace usa 'Man' y 'Woman'
            if target_sex == "male" and dominant_gender != "man":
                return False
            if target_sex == "female" and dominant_gender != "woman":
                return False

        # Age filter
        if min_age or max_age:
            age = res.get("age", 0)
            if min_age and age < min_age:
                return False
            if max_age and age > max_age:
                return False

        return True
    except ImportError:
        logging.warning("Biblioteca DeepFace no encontrada. Saltando filtrado por sexo/edad.")
        return True
    except Exception as e:
        logging.error(f"Error en el filtro DeepFace: {e}")
        return False


def _passes_sex_filter(image_path: Optional[str], target_sex: str) -> bool:
    if target_sex == "indifferent":
        return True
    if not image_path:
        # Si no hay foto y se pidió sexo específico, se descarta (Etapa 3 requiere foto)
        return False
    return _deepface_filter(image_path, target_sex)


def _llm_classify(data: Dict[str, Any], criterion: str) -> bool:
    if not criterion:
        return True

    prompt = f"""Analiza el siguiente perfil de Instagram y determina si califica según el criterio dado.

Perfil:
Username: {data['username']}
Nombre: {data['full_name']}
Bio: {data['biography']}

Criterio del usuario: {criterion}

Responde ÚNICAMENTE con una de estas dos palabras:
CALIFICA
o
NO CALIFICA

Si hay duda o ambigüedad, responde NO CALIFICA. No expliques nada."""

    try:
        import requests
        # Intentar conectar con Ollama local (puerto por defecto 11434)
        url = "http://localhost:11434/api/generate"
        # Usamos qwen como modelo por defecto solicitado
        payload = {
            "model": "qwen",
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.0 # Queremos determinismo máximo
            }
        }
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code == 200:
            response_text = resp.json().get("response", "").strip().upper()
            if "CALIFICA" in response_text and "NO CALIFICA" not in response_text:
                return True
            return False
        else:
            logging.error(f"Error de conexión con Ollama (Status {resp.status_code}).")
            return False
    except Exception as e:
        logging.error(f"Error en Stage 2 (LLM): {e}")
        return False


def _lead_filters_menu():
    while True:
        banner()
        title("Configuración de Filtros de Leads")
        filters = load_lead_filters()
        print(f"Filtros guardados: {len(filters)}")
        for i, f in enumerate(filters, 1):
            print(f" {i}) {f.name}")

        print("\n1) Crear nuevo filtro (reemplaza anteriores)")
        print("2) Modificar filtro existente")
        print("3) Eliminar todos los filtros")
        print("4) Volver")

        op = ask("Opción: ").strip()
        if op == "1":
            name = ask("Nombre del filtro: ").strip() or "default"
            new_f = _prompt_lead_filter_config(name)
            save_lead_filters([new_f])
            ok("Filtro creado.")
            press_enter()
        elif op == "2":
            if not filters:
                warn("No hay filtros para modificar.")
                press_enter()
                continue
            idx = ask_int("Selecciona número de filtro: ", min_value=1, max_value=len(filters))
            if idx:
                current = filters[idx - 1]
                updated = _prompt_lead_filter_config(current.name, current)
                filters[idx - 1] = updated
                save_lead_filters(filters)
                ok("Filtro actualizado.")
            press_enter()
        elif op == "3":
            if ask("¿Eliminar todos los filtros? (s/N): ").lower() == "s":
                save_lead_filters([])
                ok("Filtros eliminados.")
            press_enter()
        elif op == "4":
            break


def _prompt_lead_filter_config(name: str, existing: Optional[LeadFilterConfig] = None) -> LeadFilterConfig:
    print(f"\nConfigurando filtro: {name}")

    def _val(attr, default):
        return getattr(existing, attr) if existing else default

    # Stage 1
    min_f = ask_int(f"Mínimo seguidores [{_val('min_followers', 0)}]: ", min_value=0, default=_val("min_followers", 0))
    max_f = ask_int(f"Máximo seguidores [{_val('max_followers', 0)}]: ", min_value=0, default=_val("max_followers", 0))
    min_p = ask_int(f"Mínimo posts [{_val('min_posts', 0)}]: ", min_value=0, default=_val("min_posts", 0))
    max_p = ask_int(f"Máximo posts [{_val('max_posts', 0)}]: ", min_value=0, default=_val("max_posts", 0))

    print("Privacidad: 1) Públicas, 2) Privadas, 3) Ambas")
    p_choice = ask(f"Opción [{_val('privacy', 'any')}]: ").strip()
    privacy = "any"
    if p_choice == "1":
        privacy = "public"
    elif p_choice == "2":
        privacy = "private"
    elif not p_choice and existing:
        privacy = existing.privacy

    has_link = ask(f"¿Requiere link en bio? (s/N) [{_val('has_link_in_bio', False)}]: ").lower() == "s"

    kw_raw = ask(f"Palabras clave (separadas por coma) [{', '.join(_val('keywords', []))}]: ").strip()
    keywords = [k.strip() for k in kw_raw.split(",")] if kw_raw else _val("keywords", [])

    # Stage 2
    llm_criterion = ask(f"Criterio para IA (ej: 'fitness coaches') [{_val('llm_criterion', '')}]: ").strip() or _val("llm_criterion", "")

    # Stage 3
    pic_prompt = ask(f"Prompt para foto (CLIP) [{_val('profile_pic_prompt', '')}]: ").strip() or _val("profile_pic_prompt", "")

    print("Sexo objetivo: 1) Hombre, 2) Mujer, 3) Indiferente")
    s_choice = ask(f"Opción [{_val('target_sex', 'indifferent')}]: ").strip()
    target_sex = "indifferent"
    if s_choice == "1":
        target_sex = "male"
    elif s_choice == "2":
        target_sex = "female"
    elif not s_choice and existing:
        target_sex = existing.target_sex

    min_age = ask_int(f"Edad mínima [{_val('min_age', 0)}]: ", min_value=0, default=_val("min_age", 0))
    max_age = ask_int(f"Edad máxima [{_val('max_age', 0)}]: ", min_value=0, default=_val("max_age", 0))

    return LeadFilterConfig(
        name=name,
        min_followers=min_f,
        max_followers=max_f,
        min_posts=min_p,
        max_posts=max_p,
        privacy=privacy,
        has_link_in_bio=has_link,
        keywords=keywords,
        llm_criterion=llm_criterion,
        profile_pic_prompt=pic_prompt,
        target_sex=target_sex,
        min_age=min_age,
        max_age=max_age
    )


def _lead_filtering_main_menu():
    while True:
        banner()
        title("Motor de Filtrado de Leads")
        print("1) Iniciar nuevo filtrado")
        print("2) Configurar filtros")
        print("3) Reanudar sesión pendiente")
        print("4) Ver historial de sesiones")
        print("5) Volver")

        op = ask("Opción: ").strip()
        if op == "1":
            _lead_filtering_wizard()
        elif op == "2":
            _lead_filters_menu()
        elif op == "3":
            _resume_filtering_session()
        elif op == "4":
            sessions = load_filtering_sessions()
            if not sessions:
                warn("No hay sesiones guardadas.")
            else:
                print(f"{'ID':<20} | {'Filtro':<15} | {'Progreso':<10} | {'Status'}")
                print("-" * 60)
                for sid, s in list(sessions.items())[-15:]:
                    print(f"{sid:<20} | {s.get('filter_name', 'N/A'):<15} | {s.get('leads_processed')}/{s.get('leads_total'):<10} | {s.get('status')}")
            press_enter()
        elif op == "5":
            break


def _resume_filtering_session():
    sessions = load_filtering_sessions()
    pending = {sid: s for sid, s in sessions.items() if s.get("status") == "paused" or (s.get("status") == "running" and s.get("leads_pending"))}

    if not pending:
        warn("No hay sesiones pendientes para reanudar.")
        press_enter()
        return

    print("\nSeleccioná sesión para reanudar:")
    sids = list(pending.keys())
    for i, sid in enumerate(sids, 1):
        s = pending[sid]
        print(f" {i}) {sid} ({s.get('filter_name')}) - Pendientes: {len(s.get('leads_pending', []))}")

    idx = ask_int("Sesión: ", min_value=1, max_value=len(sids))
    if idx is None:
        return

    session_id = sids[idx - 1]
    session_data = pending[session_id]
    session = LeadFilteringSession(**session_data)

    filters = load_lead_filters()
    config = next((f for f in filters if f.name == session.filter_name), None)
    if not config:
        warn(f"El filtro '{session.filter_name}' ya no existe. No se puede reanudar.")
        press_enter()
        return

    try:
        all_accts = list_all()
    except:
        all_accts = []

    if not all_accts:
        warn("No hay cuentas configuradas.")
        press_enter()
        return

    print(f"\nReanudando con {len(session.leads_pending)} leads pendientes.")
    print("Seleccioná cuentas para continuar:")
    print("1) Usar todas las cuentas")
    print("2) Seleccionar manualmente")
    c = ask("Opción [1]: ").strip() or "1"
    selected = all_accts if c == "1" else []
    if c == "2":
        for i, a in enumerate(all_accts, 1): print(f" {i}) @{a.get('username')}")
        idxs_raw = ask("Cuentas (separadas por coma): ").strip()
        if idxs_raw:
            for i in idxs_raw.split(","):
                try: selected.append(all_accts[int(i.strip()) - 1])
                except: pass

    if not selected:
        warn("No se seleccionaron cuentas.")
        return

    concurrency = ask_int("Concurrencia: ", min_value=1, default=1)
    session.status = "running"
    save_filtering_session(session)

    _run_filtering_engine(selected, session.leads_pending, config, session, concurrency, 10, 30)


def _lead_filtering_wizard():
    # 1. Seleccionar Filtro
    filters = load_lead_filters()
    if not filters:
        warn("No hay filtros configurados. Creá uno primero en el menú de configuración.")
        press_enter()
        return

    print("\nSeleccioná un filtro:")
    for i, f in enumerate(filters, 1):
        print(f" {i}) {f.name}")
    f_idx = ask_int("Filtro: ", min_value=1, max_value=len(filters))
    if f_idx is None:
        return
    config = filters[f_idx - 1]

    # 2. Cargar Leads
    usernames = _load_usernames_from_source()
    if not usernames:
        warn("No se cargaron usernames.")
        press_enter()
        return

    # 3. Seleccionar Cuentas
    try:
        all_accts = list_all()
    except:
        all_accts = []

    if not all_accts:
        warn("No hay cuentas configuradas.")
        press_enter()
        return

    print(f"\nCuentas disponibles: {len(all_accts)}")
    print("1) Seleccionar por alias")
    print("2) Usar todas las cuentas")
    print("3) Seleccionar manualmente")
    acct_choice = ask("Opción: ").strip()

    selected_accts = []
    if acct_choice == "1":
        alias = ask("Alias: ").strip()
        selected_accts = [a for a in all_accts if a.get("alias") == alias]
    elif acct_choice == "2":
        selected_accts = all_accts
    elif acct_choice == "3":
        for i, a in enumerate(all_accts, 1):
            print(f" {i}) @{a.get('username')}")
        idxs_raw = ask("Cuentas (ej: 1,3,5): ").strip()
        if idxs_raw:
            for idx_s in idxs_raw.split(","):
                try:
                    selected_accts.append(all_accts[int(idx_s.strip()) - 1])
                except:
                    pass

    if not selected_accts:
        warn("No se seleccionaron cuentas válidas.")
        press_enter()
        return

    # 4. Configuración de ejecución
    concurrency = ask_int("Concurrencia (hilos/cuentas en paralelo): ", min_value=1, default=1)
    min_delay = ask_int("Delay mínimo entre perfiles (segundos): ", min_value=0, default=10)
    max_delay = ask_int("Delay máximo entre perfiles (segundos): ", min_value=0, default=30)

    # 5. Sesión
    session_id = f"filt_{int(time.time())}"
    session = LeadFilteringSession(
        id=session_id,
        filter_name=config.name,
        leads_total=len(usernames),
        leads_processed=0,
        leads_qualified=[],
        leads_disqualified=[],
        leads_pending=usernames.copy()
    )
    save_filtering_session(session)

    ok(f"Iniciando filtrado de {len(usernames)} leads...")
    _run_filtering_engine(selected_accts, usernames, config, session, concurrency, min_delay, max_delay)


def _run_filtering_engine(accounts: List[Dict], usernames: List[str], config: LeadFilterConfig, session: LeadFilteringSession, concurrency: int, min_delay: int, max_delay: int):
    lead_queue = queue.Queue()
    for u in usernames:
        lead_queue.put(u)

    results_lock = threading.Lock()

    def worker(account):
        from src.dm_playwright_client import PlaywrightDMClient
        client = None
        try:
            client = PlaywrightDMClient(account=account, headless=True)
            client.ensure_ready()
            scraper = PlaywrightProfileScraper(client._page)

            while not lead_queue.empty():
                try:
                    username = lead_queue.get_nowait()
                except queue.Empty:
                    break

                with results_lock:
                    prog = f"[{session.leads_processed + 1}/{session.leads_total}]"
                print(f"{prog} [Worker @{account['username']}] Analizando @{username}...")

                try:
                    # Stage 1: Classic
                    profile_data = scraper.scrape_profile(username)
                    if not profile_data:
                        print(f"      @{username} -> ERROR (Scraping)")
                        with results_lock:
                            session.leads_disqualified.append(f"{username} (error scraping)")
                            if username in session.leads_pending: session.leads_pending.remove(username)
                            session.leads_processed += 1
                            save_filtering_session(session)
                        continue

                    if not _passes_classic_filters(profile_data, config):
                        print(f"      @{username} -> DESCARTADO (Stage 1 - Filtros Duros)")
                        with results_lock:
                            session.leads_disqualified.append(f"{username} (stage 1)")
                            if username in session.leads_pending: session.leads_pending.remove(username)
                            session.leads_processed += 1
                            save_filtering_session(session)
                        continue

                    # Stage 2: LLM
                    if config.llm_criterion and not _llm_classify(profile_data, config.llm_criterion):
                        print(f"      @{username} -> DESCARTADO (Stage 2 - IA Texto)")
                        with results_lock:
                            session.leads_disqualified.append(f"{username} (stage 2)")
                            if username in session.leads_pending: session.leads_pending.remove(username)
                            session.leads_processed += 1
                            save_filtering_session(session)
                        continue

                    # Stage 3: Image
                    pic_path = None
                    if config.profile_pic_prompt or config.target_sex != "indifferent":
                        pic_path = _download_profile_pic(profile_data.get("profile_pic_url", ""), username)
                        if not pic_path:
                            with results_lock:
                                session.leads_disqualified.append(f"{username} (no photo)")
                                session.leads_processed += 1
                                save_filtering_session(session)
                            continue

                        # CLIP
                        if config.profile_pic_prompt and not _clip_filter(pic_path, config.profile_pic_prompt):
                            print(f"      @{username} -> DESCARTADO (Stage 3 - CLIP Imagen)")
                            with results_lock:
                                session.leads_disqualified.append(f"{username} (CLIP)")
                                if username in session.leads_pending: session.leads_pending.remove(username)
                                session.leads_processed += 1
                                save_filtering_session(session)
                            continue

                        # DeepFace / Sex
                        if not _passes_sex_filter(pic_path, config.target_sex):
                            print(f"      @{username} -> DESCARTADO (Stage 3 - Sexo/Edad)")
                            with results_lock:
                                session.leads_disqualified.append(f"{username} (sex)")
                                if username in session.leads_pending: session.leads_pending.remove(username)
                                session.leads_processed += 1
                                save_filtering_session(session)
                            continue

                    # EXIT OK
                    print(f"      @{username} -> ✅ CALIFICA!")
                    with results_lock:
                        session.leads_qualified.append(username)
                        if username in session.leads_pending: session.leads_pending.remove(username)
                        session.leads_processed += 1
                        save_filtering_session(session)

                except Exception as e:
                    logging.error(f"Error procesando @{username}: {e}")
                finally:
                    lead_queue.task_done()
                    time.sleep(random.randint(min_delay, max_delay))

        except Exception as e:
            logging.error(f"Error crítico en worker @{account['username']}: {e}")
        finally:
            if client:
                client.close()

    num_workers = min(concurrency, len(accounts))
    try:
        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            executor.map(worker, accounts[:num_workers])
    except KeyboardInterrupt:
        ok("Pausando ejecución...")
        session.status = "paused"
        save_filtering_session(session)
        return

    session.status = "completed"
    save_filtering_session(session)
    ok(f"Filtrado completado. Calificaron {len(session.leads_qualified)} leads.")
    if session.leads_qualified:
        if ask("¿Guardar calificados en una lista nueva? (s/N): ").lower() == "s":
            name = ask("Nombre de la lista: ").strip() or f"qualified_{session.id}"
            save_list(name, session.leads_qualified)
            ok("Guardado.")
    press_enter()


def _load_usernames_from_source() -> List[str]:
    print("\nCargar usernames desde:")
    print("1) Archivo CSV")
    print("2) Archivo TXT (una lista de leads existente)")
    print("3) Pegar manualmente")
    choice = ask("Opción: ").strip()

    if choice == "1":
        path = ask("Ruta del CSV: ").strip()
        p = Path(path)
        if not p.exists():
            warn("Archivo no encontrado.")
            return []
        users = []
        try:
            with p.open(newline="", encoding="utf-8") as f:
                for row in csv.reader(f):
                    if row:
                        users.append(row[0].strip().lstrip("@"))
            return users
        except Exception as e:
            warn(f"Error leyendo CSV: {e}")
            return []
    elif choice == "2":
        files = list_files()
        if not files:
            warn("No hay listas TXT disponibles.")
            return []
        print("Listas: " + ", ".join(files))
        name = ask("Nombre de la lista: ").strip()
        return load_list(name)
    elif choice == "3":
        print("Pegá usernames (uno por línea). Línea vacía para terminar:")
        lines = []
        while True:
            s = ask("").strip()
            if not s:
                break
            lines.append(s.lstrip("@"))
        return lines
    else:
        warn("Opción inválida.")
        return []
