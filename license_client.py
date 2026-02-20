# license_client.py
# -*- coding: utf-8 -*-
"""Lanzador para builds de cliente con validación de licencia."""

from __future__ import annotations

import getpass
import glob
import hashlib
import json
import os
import platform
import socket
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from runtime_parity import (
    bootstrap_runtime_env,
    format_runtime_preflight,
    run_runtime_preflight,
)


def _initial_app_root() -> Path:
    """Devuelve una única raíz de datos para CLI y EXE."""

    if sys.argv and sys.argv[0]:
        try:
            raw_entry = Path(os.path.abspath(sys.argv[0]))
            if not raw_entry.exists():
                raise FileNotFoundError(raw_entry)
            entry = raw_entry.resolve()
            base = entry.parent if entry.is_file() else entry
            # En desarrollo, el EXE suele vivir en ./dist; usamos la raíz del proyecto.
            if base.name.lower() == "dist":
                parent = base.parent
                if (parent / "app.py").exists():
                    return parent
            return base
        except Exception:
            pass
    return Path(__file__).resolve().parent


os.environ.setdefault("APP_DATA_ROOT", str(_initial_app_root()))
bootstrap_runtime_env("client", app_root_hint=_initial_app_root(), force=True)

def _get_app_root() -> Path:
    """Determina el directorio raiz del bundle/ejecutable."""
    current = (os.environ.get("APP_DATA_ROOT") or "").strip()
    if current:
        return Path(current).expanduser()
    return _initial_app_root()


_PLAYWRIGHT_BROWSER_PREFIXES = (
    "chromium-",
    "chromium_headless_shell-",
    "firefox-",
    "webkit-",
    "ffmpeg-",
)
_PLAYWRIGHT_CHROMIUM_PREFIX = "chromium-"
_PLAYWRIGHT_HEADLESS_PREFIX = "chromium_headless_shell-"
_MIN_EXECUTABLE_BYTES = 1 * 1024 * 1024
_PAK_FILES = (
    "resources.pak",
    "chrome_100_percent.pak",
    "chrome_200_percent.pak",
    "headless_lib_data.pak",
    "headless_lib_strings.pak",
    "headless_command_resources.pak",
)
_PLAYWRIGHT_SELECTION_LOGGED = False


def _safe_stat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except Exception:
        return 0


def _parse_revision(name: str, prefix: str) -> int:
    if not name.startswith(prefix):
        return -1
    suffix = name[len(prefix) :]
    digits = "".join(ch for ch in suffix if ch.isdigit())
    return int(digits) if digits else -1


def _pick_latest_dir(root: Path, prefix: str) -> Optional[Path]:
    try:
        candidates: List[Tuple[int, Path]] = []
        for item in root.iterdir():
            if item.is_dir() and item.name.startswith(prefix):
                candidates.append((_parse_revision(item.name, prefix), item))
    except Exception:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _key_files_ok(folder: Path) -> bool:
    if _safe_stat_size(folder / "icudtl.dat") <= 0:
        return False
    for name in _PAK_FILES:
        if _safe_stat_size(folder / name) > 0:
            return True
    return False


def _chromium_exe_candidates(browser_dir: Path) -> List[Path]:
    if sys.platform.startswith("win"):
        return [
            browser_dir / "chrome-win64" / "chrome.exe",
            browser_dir / "chrome-win" / "chrome.exe",
        ]
    if sys.platform == "darwin":
        return [
            browser_dir / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        ]
    return [browser_dir / "chrome-linux" / "chrome"]


def _headless_exe_candidates(browser_dir: Path) -> List[Path]:
    if sys.platform.startswith("win"):
        return [
            browser_dir / "chrome-headless-shell-win64" / "chrome-headless-shell.exe",
            browser_dir / "chrome-headless-shell-win32" / "chrome-headless-shell.exe",
            browser_dir / "chrome-headless-shell" / "chrome-headless-shell.exe",
            browser_dir / "headless_shell" / "headless_shell.exe",
        ]
    if sys.platform == "darwin":
        return [
            browser_dir
            / "chrome-headless-shell"
            / "Chromium.app"
            / "Contents"
            / "MacOS"
            / "Chromium"
        ]
    return [browser_dir / "chrome-headless-shell" / "chrome-headless-shell"]


def _standalone_chrome_candidates(root: Path) -> List[Path]:
    if sys.platform.startswith("win"):
        return [
            root / "chrome-win64" / "chrome.exe",
            root / "chrome-win" / "chrome.exe",
            root / "browsers" / "chrome-win64" / "chrome.exe",
            root / "browsers" / "chrome-win" / "chrome.exe",
        ]
    if sys.platform == "darwin":
        return [
            root / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
            root / "browsers" / "chrome-mac" / "Chromium.app" / "Contents" / "MacOS" / "Chromium",
        ]
    return [
        root / "chrome-linux" / "chrome",
        root / "browsers" / "chrome-linux" / "chrome",
    ]


def _validate_executable(exe_path: Path) -> Tuple[bool, int]:
    size = _safe_stat_size(exe_path)
    if size <= _MIN_EXECUTABLE_BYTES:
        return False, size
    if not _key_files_ok(exe_path.parent):
        return False, size
    return True, size


def _select_executable(
    browser_dir: Path, candidates: List[Path], reason: str
) -> Optional[Tuple[Path, int, str, Path]]:
    for exe_path in candidates:
        ok, size = _validate_executable(exe_path)
        if ok:
            return exe_path, size, reason, browser_dir
    return None


def _select_standalone_chrome(candidate: Path) -> Optional[Tuple[Path, int, str, Path]]:
    for exe_path in _standalone_chrome_candidates(candidate):
        ok, size = _validate_executable(exe_path)
        if ok:
            return exe_path, size, "standalone_chrome", exe_path.parent
    return None


def _select_playwright_root(
    candidate: Path,
) -> Optional[Tuple[Path, Path, int, str, Path]]:
    if not candidate.exists():
        return None

    standalone = _select_standalone_chrome(candidate)
    if standalone:
        exe_path, size, reason, browser_dir = standalone
        return candidate, exe_path, size, reason, browser_dir

    chromium_dir = _pick_latest_dir(candidate, _PLAYWRIGHT_CHROMIUM_PREFIX)
    if chromium_dir:
        selection = _select_executable(
            chromium_dir, _chromium_exe_candidates(chromium_dir), "chromium_ok"
        )
        if selection:
            exe_path, size, reason, browser_dir = selection
            return candidate, exe_path, size, reason, browser_dir

    headless_dir = _pick_latest_dir(candidate, _PLAYWRIGHT_HEADLESS_PREFIX)
    if headless_dir:
        reason = "headless_ok" if not chromium_dir else "headless_fallback"
        selection = _select_executable(
            headless_dir, _headless_exe_candidates(headless_dir), reason
        )
        if selection:
            exe_path, size, reason, browser_dir = selection
            return candidate, exe_path, size, reason, browser_dir

    nested = candidate / "ms-playwright"
    if nested.exists():
        return _select_playwright_root(nested)
    return None


def _log_playwright_selection(
    root: Path, browser_dir: Path, exe_path: Path, size: int, reason: str
) -> None:
    global _PLAYWRIGHT_SELECTION_LOGGED
    if _PLAYWRIGHT_SELECTION_LOGGED:
        return
    _PLAYWRIGHT_SELECTION_LOGGED = True
    print(
        "Playwright browsers selected: "
        f"root={root} browser_dir={browser_dir.name} "
        f"exe={exe_path.name} size={size} reason={reason}"
    )


def _detect_playwright_browsers_path() -> Optional[Tuple[Path, Path, int, str, Path]]:
    env_path = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        selection = _select_playwright_root(candidate)
        if selection:
            return selection

    candidates: List[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.extend(
            [
                Path(base) / "playwright_browsers",
                Path(base) / "playwright",
                Path(base) / "browsers",
                Path(base),
            ]
        )

    app_root = _get_app_root()
    candidates.extend(
        [
            app_root / "playwright_browsers",
            app_root / "playwright",
            app_root / "browsers",
            app_root,
        ]
    )

    exe_parent = None
    try:
        executable = getattr(sys, "executable", "") or ""
        if executable:
            exe_parent = Path(executable).resolve().parent
    except Exception:
        exe_parent = None
    if exe_parent:
        candidates.extend(
            [
                exe_parent / "playwright_browsers",
                exe_parent / "playwright",
                exe_parent / "browsers",
                exe_parent,
            ]
        )

    for candidate in candidates:
        selection = _select_playwright_root(candidate)
        if selection:
            return selection
    return None


def _ensure_playwright_browsers_env() -> Optional[Path]:
    current = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if current:
        selection = _select_playwright_root(Path(current).expanduser())
        if selection:
            root, exe_path, size, reason, browser_dir = selection
            os.environ["PLAYWRIGHT_CHROME_EXECUTABLE"] = str(exe_path)
            if reason == "standalone_chrome":
                os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            elif str(root) != current:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
            _log_playwright_selection(root, browser_dir, exe_path, size, reason)
            return root
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    selection = _detect_playwright_browsers_path()
    if selection:
        root, exe_path, size, reason, browser_dir = selection
        os.environ["PLAYWRIGHT_CHROME_EXECUTABLE"] = str(exe_path)
        if reason == "standalone_chrome":
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
        _log_playwright_selection(root, browser_dir, exe_path, size, reason)
        return root
    return None


_ensure_playwright_browsers_env()

import config
from licensekit import validate_license_payload
from ui import Fore, banner, full_line, style_text

PAYLOAD_NAME = "storage/license_payload.json"
_ALT_PAYLOAD_NAME = "license.json"

SESSION_PATTERNS = [
    "session_*.json",
    "v1_settings_*.json",
    "settings_*.json",
    "*.session.json",
    "*.json",
]

_DEBUG_ROOT_PRINTED = False
_FINGERPRINT_FILENAME = "client_fingerprint.json"


def _resource_path(relative: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent / relative


def _is_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _remote_only_enabled() -> bool:
    return _is_truthy(os.environ.get("LICENSE_REMOTE_ONLY"))


def _payload_candidates() -> List[Path]:
    candidates: List[Path] = []
    env_path = (os.environ.get("LICENSE_FILE") or "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    app_root = _get_app_root()
    candidates.extend(
        [
            app_root / _ALT_PAYLOAD_NAME,
            app_root / "license_payload.json",
            app_root / PAYLOAD_NAME,
            app_root / "storage" / _ALT_PAYLOAD_NAME,
        ]
    )

    data_root = Path(os.environ.get("APP_DATA_ROOT", str(app_root)))
    candidates.extend(
        [
            data_root / _ALT_PAYLOAD_NAME,
            data_root / PAYLOAD_NAME,
            data_root / "storage" / _ALT_PAYLOAD_NAME,
        ]
    )
    return candidates


def _load_payload() -> Dict[str, str]:
    for path in _payload_candidates():
        if not path.is_file():
            continue
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    path = _resource_path(PAYLOAD_NAME)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _resolve_backend_url(payload: Dict[str, str]) -> str:
    payload_url = str(payload.get("backend_url") or "").strip()
    if payload_url:
        return payload_url
    return str(os.environ.get("BACKEND_URL") or "").strip()


def _prompt_license_key(default_key: str) -> str:
    if default_key:
        provided = input(
            "Ingrese su codigo de licencia (Enter para usar el guardado): "
        ).strip()
        return provided or default_key
    return input("Ingrese su codigo de licencia: ").strip()


def _activate_remote_license(
    license_key: str,
    backend_url: str,
    client_fingerprint: Optional[str],
    machine_id: Optional[str],
) -> Tuple[bool, Dict, str]:
    try:
        from backend_license_client import LicenseBackendClient
    except Exception as exc:
        return False, {}, f"Backend client no disponible: {exc}"

    client = LicenseBackendClient(backend_url)
    success, data, error = client.activate_license(
        license_key, client_fingerprint=client_fingerprint, machine_id=machine_id
    )
    return success, data or {}, error or ""


def _print_section(title: str, *, color: str = Fore.CYAN) -> None:
    banner()
    print(style_text(title, color=color, bold=True))
    print(full_line(color=color))
    print()


def _print_error(msg: str) -> None:
    print(full_line(color=Fore.RED))
    print(style_text("Licencia inválida", color=Fore.RED, bold=True))
    print(msg)
    print(full_line(color=Fore.RED))
    print()


def _storage_root() -> Path:
    root = Path(os.environ.get("APP_DATA_ROOT") or _get_app_root())
    storage = root / "storage"
    try:
        storage.mkdir(parents=True, exist_ok=True)
    except Exception:
        return root
    return storage


def _fingerprint_storage_path() -> Path:
    return _storage_root() / _FINGERPRINT_FILENAME


def _load_persisted_fingerprint(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return raw if raw.strip() else None
    if isinstance(data, str):
        return data.strip() or None
    if isinstance(data, dict):
        for key in ("client_fingerprint", "machine_id", "machine", "fingerprint"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
    return None


def _build_fingerprint_from_components(components: Iterable[str]) -> Optional[str]:
    clean = [value.strip() for value in components if value and value.strip()]
    if not clean:
        return None
    blob = "|".join(clean).encode("utf-8", errors="ignore")
    digest = hashlib.sha256(blob).hexdigest()
    return f"fp-{digest[:16]}"


def _windows_volume_serial(path: Path) -> str:
    if not sys.platform.startswith("win"):
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        drive = path.drive or os.environ.get("SystemDrive", "C:")
        root = drive.rstrip("\\") + "\\"
        serial = wintypes.DWORD()
        result = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, ctypes.byref(serial), None, None, None, 0
        )
        if result:
            return f"{serial.value:08X}"
    except Exception:
        return ""
    return ""


def _generate_fingerprint() -> Tuple[str, str, Dict[str, str]]:
    details: Dict[str, str] = {}
    components = [
        platform.node(),
        platform.system(),
        platform.release(),
        platform.machine(),
        str(uuid.getnode()),
    ]
    details["hostname"] = platform.node() or socket.gethostname()
    details["username"] = getpass.getuser()
    details["volume_serial"] = _windows_volume_serial(_get_app_root())
    fingerprint = _build_fingerprint_from_components(components)
    if fingerprint:
        return fingerprint, "primary", details
    fallback_components = [
        details.get("hostname", ""),
        details.get("username", ""),
        details.get("volume_serial", ""),
    ]
    fingerprint = _build_fingerprint_from_components(fallback_components)
    if fingerprint:
        return fingerprint, "fallback", details
    return f"fp-{uuid.uuid4().hex[:16]}", "random", details


def _persist_fingerprint(
    path: Path, fingerprint: str, source: str, details: Dict[str, str]
) -> None:
    payload = {
        "client_fingerprint": fingerprint,
        "machine_id": fingerprint,
        "source": source,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "details": details,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _get_or_create_fingerprint() -> Tuple[str, Path, str]:
    path = _fingerprint_storage_path()
    existing = _load_persisted_fingerprint(path)
    if existing:
        return existing, path, "loaded"
    fingerprint, source, details = _generate_fingerprint()
    if not fingerprint:
        fingerprint = f"fp-{uuid.uuid4().hex[:16]}"
        source = "random"
    _persist_fingerprint(path, fingerprint, source, details)
    return fingerprint, path, source


def _resolve_sessions_dir() -> Path:
    root = Path(os.environ.get("APP_DATA_ROOT") or _get_app_root())
    target = root / "storage" / "sessions"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _iter_session_files(sess_dir: Path) -> Iterable[Path]:
    seen: set[str] = set()
    for pattern in SESSION_PATTERNS:
        for path in glob.glob(str(sess_dir / pattern)):
            base = os.path.basename(path)
            if base in seen:
                continue
            seen.add(base)
            candidate = Path(path)
            if candidate.is_file():
                yield candidate
        for path in glob.glob(str(sess_dir / "*" / pattern)):
            base = os.path.basename(path)
            if base in seen:
                continue
            seen.add(base)
            candidate = Path(path)
            if candidate.is_file():
                yield candidate


def _prepare_client_environment(record: Dict[str, str]) -> None:
    _ = record
    app_root = _get_app_root()
    os.environ.setdefault("CLIENT_DISTRIBUTION", "1")
    os.environ["LICENSE_ALREADY_VALIDATED"] = "1"
    os.environ["APP_DATA_ROOT"] = str(app_root)
    os.environ.pop("CLIENT_SESSIONS_ROOT", None)
    os.environ.pop("CLIENT_ALIAS", None)
    bootstrap_runtime_env("client", app_root_hint=app_root, force=True)


def _client_integrity_marker_path() -> Path:
    base = Path(os.environ.get("APP_DATA_ROOT", str(_get_app_root())))
    return base / "storage" / ".client_integrity_check"


def _run_client_integrity_check() -> None:
    if not config.SETTINGS.client_distribution:
        return

    marker = _client_integrity_marker_path()
    if marker.exists():
        return

    try:
        from app import current_menu_option_labels
        from responder import autoresponder_menu_options, autoresponder_prompt_length
    except Exception:
        return

    print(full_line(color=Fore.CYAN))
    print(style_text("Verificación de integridad del cliente", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.CYAN))
    print(style_text("Versión detectada: cliente", color=Fore.GREEN, bold=True))

    options = current_menu_option_labels()
    sin_siete = all(not opt.strip().startswith("7)") for opt in options)
    estado_menu = "sí" if sin_siete else "no"
    print(f"Menú sin opción 7: {estado_menu}")
    if options:
        print("Opciones visibles:")
        for opt in options:
            print(f" • {opt.strip()}")

    prompt_length = autoresponder_prompt_length()
    print(
        f"Autoresponder 5.2 listo. System Prompt actual: {prompt_length} caracteres."
    )

    calendar_disponible = any(
        "Conectar con Google Calendar" in opt for opt in autoresponder_menu_options()
    )
    estado_calendar = "sí" if calendar_disponible else "no"
    print(f"Submenú 'Conectar con Google Calendar' disponible: {estado_calendar}")

    gohighlevel_disponible = any(
        "Conectar con GoHighLevel" in opt for opt in autoresponder_menu_options()
    )
    estado_gohighlevel = "sí" if gohighlevel_disponible else "no"
    print(f"Submenú 'Conectar con GoHighLevel' disponible: {estado_gohighlevel}")

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass

    print(full_line(color=Fore.CYAN))
    print()


def _verify_playwright_bundle() -> None:
    print(full_line(color=Fore.CYAN))
    print(style_text("Verificacion Playwright", color=Fore.CYAN, bold=True))
    print(full_line(color=Fore.CYAN))
    try:
        import playwright  # noqa: F401
    except Exception as exc:
        print(style_text("Playwright: NO DISPONIBLE", color=Fore.RED, bold=True))
        print(style_text(f"Detalle: {exc}", color=Fore.RED))
        print(full_line(color=Fore.CYAN))
        print()
        return

    resolved = _ensure_playwright_browsers_env()
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    chrome_executable = os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE", "").strip()
    print(style_text(f"PLAYWRIGHT_BROWSERS_PATH: {browsers_path or '-'}", color=Fore.WHITE))
    print(style_text(f"PLAYWRIGHT_CHROME_EXECUTABLE: {chrome_executable or '-'}", color=Fore.WHITE))
    try:
        from playwright._impl import _driver

        node_path, cli_path = _driver.compute_driver_executable()
        print(style_text(f"Playwright driver: {node_path}", color=Fore.WHITE))
        print(style_text(f"Playwright CLI: {cli_path}", color=Fore.WHITE))
    except Exception:
        pass
    if not browsers_path:
        print(style_text("Playwright: OK", color=Fore.GREEN, bold=True))
        print(style_text("Browsers: NO CONFIGURADOS", color=Fore.YELLOW, bold=True))
        print(full_line(color=Fore.CYAN))
        print()
        return

    candidate = Path(browsers_path)
    if not resolved:
        print(style_text("Playwright: OK", color=Fore.GREEN, bold=True))
        print(style_text("Browsers: NO ENCONTRADOS", color=Fore.RED, bold=True))
        print(style_text(f"Ruta: {candidate}", color=Fore.YELLOW))
        print(full_line(color=Fore.CYAN))
        print()
        return

    try:
        folders = [p.name for p in resolved.iterdir() if p.is_dir()]
    except Exception:
        folders = []

    print(style_text("Playwright: OK", color=Fore.GREEN, bold=True))
    print(style_text("Browsers: OK", color=Fore.GREEN, bold=True))
    if folders:
        preview = ", ".join(folders[:4])
        if len(folders) > 4:
            preview += ", ..."
        print(style_text(f"Detectados: {preview}", color=Fore.WHITE))
    print(style_text(f"Ruta: {resolved}", color=Fore.WHITE))
    print(full_line(color=Fore.CYAN))
    print()


def _ensure_account_record(username: str, accounts: List[Dict]) -> Dict | None:
    """Garantiza que exista un registro básico de cuenta para el usuario."""

    normalized_username = username.lower()
    for record in accounts:
        existing = (record.get("username") or "").strip().lower()
        if existing == normalized_username:
            return record

    try:
        from accounts import _normalize_account as normalize_account  # type: ignore[attr-defined]
        from accounts import _save as save_accounts  # type: ignore[attr-defined]
    except Exception:
        return None

    alias = "default"
    base_record = {
        "username": username,
        "alias": alias,
        "active": True,
        "connected": False,
    }
    try:
        normalized = normalize_account(base_record)
    except Exception:
        normalized = base_record

    accounts.append(normalized)
    try:
        save_accounts(accounts)
    except Exception:
        pass
    return normalized


def _load_sessions_on_boot() -> Tuple[int, int, List[str]]:
    global _DEBUG_ROOT_PRINTED

    sessions_dir = _resolve_sessions_dir()
    profiles_root = Path(os.environ.get("PROFILES_DIR") or (_get_app_root() / "profiles"))
    found_files = list(_iter_session_files(sessions_dir))
    print(f"📦 Sesiones detectadas en '{sessions_dir.name}': {len(found_files)}")
    try:
        names_preview = ", ".join(path.name for path in found_files[:5])
        if len(found_files) > 5:
            names_preview += ", ..."
        if names_preview:
            print(f"🗂️ Archivos: {names_preview}")
    except Exception:
        pass

    try:
        from accounts import list_all, mark_connected
    except Exception:
        print("🔄 Sesiones restauradas: 0")
        return 0, len(found_files), []

    accounts = list_all()
    account_map = {
        (acct.get("username") or "").strip().lstrip("@").lower(): acct
        for acct in accounts
        if acct.get("username")
    }

    for acct in accounts:
        username = acct.get("username")
        if username:
            mark_connected(username, False)

    loaded = 0
    errors = 0
    loaded_users: List[str] = []

    for path in found_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            errors += 1
            print(f"⚠️ No se pudo cargar la sesión desde: {path.name}")
            continue

        username = (
            (data.get("username") or data.get("user") or data.get("account") or "")
            .strip()
            .lstrip("@")
        )
        if not username:
            stem = path.stem
            for prefix in ("session_", "v1_settings_", "settings_"):
                if stem.startswith(prefix):
                    username = stem[len(prefix) :]
                    break
            if not username:
                username = stem
        username = username.strip().lstrip("@")
        if not username:
            errors += 1
            print(f"⚠️ No se pudo cargar la sesión desde: {path.name}")
            continue

        lower_username = username.lower()
        account = account_map.get(lower_username)
        if not account:
            account = _ensure_account_record(username, accounts)
            if account:
                account_map[lower_username] = account
                try:
                    mark_connected(username, False)
                except Exception:
                    pass
            else:
                errors += 1
                print(f"⚠️ Sesión de @{username} no vinculada a una cuenta guardada.")
                continue

        raw_cookies = data.get("cookies") or {}
        cookies: Dict[str, str] = {}
        if isinstance(raw_cookies, dict):
            cookies = {str(k): raw_cookies[k] for k in raw_cookies if raw_cookies[k]}
        elif isinstance(raw_cookies, list):
            for item in raw_cookies:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if name and value:
                    cookies[str(name)] = value

        session_id = (
            data.get("sessionid")
            or cookies.get("sessionid")
            or cookies.get("session_id")
            or (data.get("authorization_data") or {}).get("sessionid")
        )
        if not session_id:
            errors += 1
            mark_connected(username, False)
            print(f"⚠️ Sesión de @{username} inválida, por favor volvé a iniciar sesión.")
            continue

        storage_state = profiles_root / username / "storage_state.json"
        if not storage_state.exists():
            errors += 1
            mark_connected(username, False)
            print(
                f"⚠️ Sesión de @{username} sin storage_state Playwright "
                f"({storage_state}). Requiere relogin."
            )
            continue

        mark_connected(username, True)
        try:
            account["connected"] = True
        except Exception:
            pass
        loaded += 1
        loaded_users.append(username)

    if found_files and loaded == 0 and not _DEBUG_ROOT_PRINTED:
        _DEBUG_ROOT_PRINTED = True
        print(f"ROOT={_get_app_root()}")
        print(f"SESS_DIR={sessions_dir}")
        print(f"EXISTS={sessions_dir.exists()}")

    print(f"🔄 Sesiones restauradas: {loaded}")
    return loaded, errors, loaded_users


def launch_with_license() -> None:
    payload = _load_payload()
    backend_url = _resolve_backend_url(payload)
    remote_only = _remote_only_enabled()

    if remote_only or backend_url:
        _print_section("Validacion de licencia")
        if not backend_url:
            _print_error(
                "BACKEND_URL no configurado. Configure BACKEND_URL o use un"
                " archivo de licencia con backend_url."
            )
            sys.exit(2)
        license_key = _prompt_license_key(str(payload.get("license_key") or "").strip())
        if not license_key:
            _print_error("Falta la licencia.")
            sys.exit(2)
        fingerprint, fingerprint_path, fingerprint_source = _get_or_create_fingerprint()
        if fingerprint:
            os.environ["CLIENT_FINGERPRINT"] = fingerprint
        payload_keys = ["license_key", "client_fingerprint", "machine"]
        print(f"fingerprint={fingerprint}")
        print(f"fingerprint_path={fingerprint_path}")
        print(f"fingerprint_source={fingerprint_source}")
        print(f"payload keys={', '.join(payload_keys)}")
        ok_remote, activation, error = _activate_remote_license(
            license_key, backend_url, fingerprint, fingerprint
        )
        if not ok_remote:
            _print_error(error or "No se pudo validar la licencia.")
            sys.exit(2)
        record = dict(payload or {})
        record["license_key"] = license_key
        if not record.get("client_name"):
            record["client_name"] = "Cliente"
        if "days_left" in activation:
            record["days_left"] = activation.get("days_left")
        if activation.get("expires_at") and not record.get("expires_at"):
            record["expires_at"] = activation.get("expires_at")

        _prepare_client_environment(record)
        config.refresh_settings()
        _load_sessions_on_boot()
        preflight = run_runtime_preflight(
            "client",
            strict=False,
            sync_connected=True,
        )
        print(format_runtime_preflight(preflight))
        if int(preflight.get("critical_count", 0)) > 0:
            _print_error(
                "Preflight runtime falló con errores críticos. "
                f"Revisa: {preflight.get('report_path')}"
            )
            sys.exit(2)

        _print_section("Licencia validada", color=Fore.GREEN)
        client = record.get("client_name", "Cliente")
        print(style_text(f"Licencia valida para {client}", color=Fore.GREEN, bold=True))
        expires = record.get("expires_at")
        if expires:
            print(style_text(f"Vence: {expires}", color=Fore.GREEN))
        elif "days_left" in record:
            print(
                style_text(
                    f"Dias restantes: {record['days_left']}", color=Fore.GREEN
                )
            )
        print(full_line(color=Fore.GREEN))
        print()

        _run_client_integrity_check()
        _verify_playwright_bundle()

        from app import menu  # import tardio para evitar ciclos

        menu()
        return

    if not payload:
        _print_section("Validacion de licencia", color=Fore.RED)
        _print_error("No se encontro la licencia incluida en el paquete.")
        sys.exit(2)

    attempts = 3
    record: Dict[str, str] = {}
    _print_section("Validacion de licencia")
    print(style_text("Ingrese su codigo de licencia para continuar.", color=Fore.WHITE))
    print()
    for remaining in range(attempts, 0, -1):
        provided = input("Ingrese su codigo de licencia: ").strip()
        ok, message, record = validate_license_payload(provided, payload)
        if ok:
            break
        _print_error(message or "Licencia invalida.")
        if remaining - 1:
            print(style_text(f"Intentos restantes: {remaining - 1}", color=Fore.YELLOW))
            print()
    else:
        sys.exit(2)

    _prepare_client_environment(record)
    config.refresh_settings()
    _load_sessions_on_boot()
    preflight = run_runtime_preflight(
        "client",
        strict=False,
        sync_connected=True,
    )
    print(format_runtime_preflight(preflight))
    if int(preflight.get("critical_count", 0)) > 0:
        _print_error(
            "Preflight runtime falló con errores críticos. "
            f"Revisa: {preflight.get('report_path')}"
        )
        sys.exit(2)

    _print_section("Licencia validada", color=Fore.GREEN)
    client = record.get("client_name", "Cliente")
    print(style_text(f"Licencia valida para {client}", color=Fore.GREEN, bold=True))
    expires = record.get("expires_at")
    if expires:
        print(style_text(f"Vence: {expires}", color=Fore.GREEN))
    print(full_line(color=Fore.GREEN))
    print()

    _run_client_integrity_check()
    _verify_playwright_bundle()

    from app import menu  # import tardio para evitar ciclos

    menu()

if __name__ == "__main__":
    launch_with_license()
