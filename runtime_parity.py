from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_RUNTIME_CONFIG_REL = Path("storage") / "runtime_config.json"
_PREFLIGHT_REPORT_REL = Path("storage") / "runtime_preflight_report.json"
_MIN_EXECUTABLE_BYTES = 1 * 1024 * 1024
_PLAYWRIGHT_CHROMIUM_PREFIX = "chromium-"
_PLAYWRIGHT_HEADLESS_PREFIX = "chromium_headless_shell-"
_BOOTSTRAP_CACHE: dict[str, dict[str, Any]] = {}

_MANAGED_ENV_KEYS = (
    "PROFILES_DIR",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PLAYWRIGHT_CHROME_EXECUTABLE",
    "AUTORESPONDER_DM_HEADLESS",
    "AUTORESPONDER_DM_SLOW_MO_MS",
    "AUTORESPONDER_DM_VERBOSE_PROBES",
    "AUTORESPONDER_DM_SCROLL_WAIT_MS",
    "AUTORESPONDER_DM_SCROLL_ATTEMPTS",
    "AUTORESPONDER_DM_STAGNANT_BASE_LIMIT",
    "AUTORESPONDER_DM_STAGNANT_MAX_LIMIT",
    "HUMAN_DM_VERIFY_TIMEOUT",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_BASE_URL",
)
_SENSITIVE_ENV_KEYS = {
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
}
_DEFAULT_MANAGED_ENV: dict[str, str] = {
    "AUTORESPONDER_DM_SCROLL_WAIT_MS": "320",
    "AUTORESPONDER_DM_SCROLL_ATTEMPTS": "8",
    "AUTORESPONDER_DM_STAGNANT_BASE_LIMIT": "18",
    "AUTORESPONDER_DM_STAGNANT_MAX_LIMIT": "80",
}


def _is_file_writable(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        marker = path.parent / f".writable_{int(time.time() * 1000)}.tmp"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _is_valid_executable(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= _MIN_EXECUTABLE_BYTES
    except Exception:
        return False


def _normalize_mode(mode: str | None) -> str:
    raw = str(mode or "").strip().lower()
    return "client" if raw == "client" else "owner"


def _project_root_from_here() -> Path:
    return Path(__file__).resolve().parent


def _resolve_data_root(mode: str, app_root_hint: Optional[Path] = None) -> Path:
    override = (os.environ.get("APP_DATA_ROOT") or "").strip()
    if override:
        return Path(override).expanduser()

    if app_root_hint is not None:
        return Path(app_root_hint).expanduser()

    if getattr(sys, "frozen", False):
        exe_path = getattr(sys, "executable", "") or ""
        if exe_path:
            try:
                return Path(exe_path).resolve().parent
            except Exception:
                pass

    if mode == "client":
        argv0 = (sys.argv[0] or "").strip()
        if argv0:
            try:
                return Path(argv0).resolve().parent
            except Exception:
                pass

    return _project_root_from_here()


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {}

    parsed: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        parsed[key] = value.strip().strip('"').strip("'")
    return parsed


def _load_runtime_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_runtime_config(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        return


def _ensure_runtime_config(path: Path) -> dict[str, Any]:
    payload = _load_runtime_config(path)
    if payload:
        return payload

    snapshot_env: dict[str, str] = {}
    for key in _MANAGED_ENV_KEYS:
        if key in _SENSITIVE_ENV_KEYS:
            continue
        value = (os.environ.get(key) or "").strip()
        if value:
            snapshot_env[key] = value

    created = {
        "version": 1,
        "env": snapshot_env,
    }
    _write_runtime_config(path, created)
    return created


def _apply_env_values(values: dict[str, str], *, override: bool) -> None:
    for key, value in values.items():
        if not key:
            continue
        if override or key not in os.environ or not str(os.environ.get(key) or "").strip():
            os.environ[key] = str(value)


def _parse_revision(name: str, prefix: str) -> int:
    if not name.startswith(prefix):
        return -1
    suffix = name[len(prefix) :]
    digits = "".join(ch for ch in suffix if ch.isdigit())
    return int(digits) if digits else -1


def _pick_latest_dir(root: Path, prefix: str) -> Optional[Path]:
    try:
        pairs: list[tuple[int, Path]] = []
        for item in root.iterdir():
            if item.is_dir() and item.name.startswith(prefix):
                pairs.append((_parse_revision(item.name, prefix), item))
    except Exception:
        return None
    if not pairs:
        return None
    pairs.sort(key=lambda pair: pair[0], reverse=True)
    return pairs[0][1]


def _windows_standalone_candidates(root: Path) -> list[Path]:
    return [
        root / "chrome-win64" / "chrome.exe",
        root / "chrome-win" / "chrome.exe",
        root / "browsers" / "chrome-win64" / "chrome.exe",
        root / "browsers" / "chrome-win" / "chrome.exe",
    ]


def _windows_browser_dir_candidates(root: Path, *, headless: bool) -> list[Path]:
    prefix = _PLAYWRIGHT_HEADLESS_PREFIX if headless else _PLAYWRIGHT_CHROMIUM_PREFIX
    browser_dir = _pick_latest_dir(root, prefix)
    if not browser_dir:
        return []
    if headless:
        return [
            browser_dir / "chrome-headless-shell-win64" / "chrome-headless-shell.exe",
            browser_dir / "chrome-headless-shell-win32" / "chrome-headless-shell.exe",
            browser_dir / "chrome-headless-shell" / "chrome-headless-shell.exe",
            browser_dir / "headless_shell" / "headless_shell.exe",
        ]
    return [
        browser_dir / "chrome-win64" / "chrome.exe",
        browser_dir / "chrome-win" / "chrome.exe",
    ]


def _unique_paths(values: Iterable[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for value in values:
        try:
            key = str(value.resolve())
        except Exception:
            key = str(value)
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _browser_root_candidates(app_root: Path) -> list[Path]:
    env_root = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser())

    candidates.extend(
        [
            app_root / "playwright_browsers",
            app_root / "ms-playwright",
            app_root / "playwright",
            app_root / "browsers",
            app_root,
        ]
    )

    exe_path = getattr(sys, "executable", "") or ""
    if exe_path:
        try:
            exe_dir = Path(exe_path).resolve().parent
            candidates.extend(
                [
                    exe_dir / "playwright_browsers",
                    exe_dir / "ms-playwright",
                    exe_dir / "playwright",
                    exe_dir / "browsers",
                    exe_dir,
                ]
            )
        except Exception:
            pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates.extend(
            [
                base / "playwright_browsers",
                base / "ms-playwright",
                base / "playwright",
                base / "browsers",
                base,
            ]
        )
    return _unique_paths(candidates)


def _resolve_browser_from_roots(app_root: Path) -> tuple[Optional[Path], Optional[Path]]:
    explicit = (os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if _is_valid_executable(candidate):
            return candidate, candidate.parent

    for root in _browser_root_candidates(app_root):
        if not root.exists():
            continue

        for exe_path in _windows_standalone_candidates(root):
            if _is_valid_executable(exe_path):
                return exe_path, root

        for exe_path in _windows_browser_dir_candidates(root, headless=False):
            if _is_valid_executable(exe_path):
                return exe_path, root

        for exe_path in _windows_browser_dir_candidates(root, headless=True):
            if _is_valid_executable(exe_path):
                return exe_path, root

        nested = root / "ms-playwright"
        if nested.exists():
            for exe_path in _windows_browser_dir_candidates(nested, headless=False):
                if _is_valid_executable(exe_path):
                    return exe_path, nested
            for exe_path in _windows_browser_dir_candidates(nested, headless=True):
                if _is_valid_executable(exe_path):
                    return exe_path, nested

    return None, None


def _configure_browser_env(app_root: Path) -> tuple[Optional[str], Optional[str]]:
    executable, browsers_root = _resolve_browser_from_roots(app_root)
    if executable is not None:
        os.environ["PLAYWRIGHT_CHROME_EXECUTABLE"] = str(executable)
    if browsers_root is not None:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_root)
    return (
        str(executable) if executable is not None else None,
        str(browsers_root) if browsers_root is not None else None,
    )


def _profiles_dir(app_root: Path) -> Path:
    raw = (os.environ.get("PROFILES_DIR") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = (app_root / candidate).resolve()
    else:
        candidate = app_root / "profiles"
    candidate.mkdir(parents=True, exist_ok=True)
    os.environ["PROFILES_DIR"] = str(candidate)
    return candidate


def bootstrap_runtime_env(
    mode: str | None,
    *,
    app_root_hint: Optional[Path] = None,
    force: bool = False,
) -> dict[str, Any]:
    normalized_mode = _normalize_mode(mode)
    cache_key = f"{normalized_mode}:{str(app_root_hint or '')}"
    if not force and cache_key in _BOOTSTRAP_CACHE:
        return dict(_BOOTSTRAP_CACHE[cache_key])

    app_root = _resolve_data_root(normalized_mode, app_root_hint=app_root_hint)
    os.environ["APP_DATA_ROOT"] = str(app_root)

    for rel in ("storage", "profiles", "data"):
        try:
            (app_root / rel).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    env_sources: list[str] = []
    for env_path in (app_root / ".env", app_root / ".env.local"):
        values = _parse_env_file(env_path)
        if not values:
            continue
        _apply_env_values(values, override=False)
        env_sources.append(str(env_path))

    runtime_cfg_path = app_root / _RUNTIME_CONFIG_REL
    runtime_cfg = _ensure_runtime_config(runtime_cfg_path)
    runtime_env_raw = runtime_cfg.get("env") if isinstance(runtime_cfg, dict) else {}
    runtime_env = runtime_env_raw if isinstance(runtime_env_raw, dict) else {}
    managed_values: dict[str, str] = {}
    for key, value in runtime_env.items():
        key_text = str(key or "").strip()
        if key_text in _MANAGED_ENV_KEYS and str(value or "").strip():
            managed_values[key_text] = str(value).strip()
    _apply_env_values(managed_values, override=True)
    _apply_env_values(_DEFAULT_MANAGED_ENV, override=False)

    profiles_dir = _profiles_dir(app_root)
    browser_executable, browser_root = _configure_browser_env(app_root)

    result = {
        "mode": normalized_mode,
        "app_data_root": str(app_root),
        "profiles_dir": str(profiles_dir),
        "browser_executable": browser_executable,
        "browser_root": browser_root,
        "runtime_config_path": str(runtime_cfg_path),
        "env_sources": env_sources,
    }
    _BOOTSTRAP_CACHE[cache_key] = dict(result)
    return result


def _normalize_username(value: Any) -> str:
    return str(value or "").strip().lstrip("@")


def _storage_state_path_for_user(profiles_dir: Path, username: str) -> Path:
    safe = _normalize_username(username)
    return profiles_dir / safe / "storage_state.json"


def run_runtime_preflight(
    mode: str | None,
    *,
    strict: bool = False,
    sync_connected: bool = True,
) -> dict[str, Any]:
    bootstrap = bootstrap_runtime_env(mode)
    app_root = Path(str(bootstrap.get("app_data_root") or "."))
    profiles_dir = Path(str(bootstrap.get("profiles_dir") or (app_root / "profiles")))
    storage_dir = app_root / "storage"
    issues: list[dict[str, str]] = []
    account_total = 0
    connected_total = 0
    disconnected_by_preflight: list[str] = []
    connected_without_storage_state: list[str] = []

    if not _is_file_writable(storage_dir / "runtime_preflight_write_test.tmp"):
        issues.append(
            {
                "level": "critical",
                "code": "data_root_not_writable",
                "message": f"No write permissions in storage path: {storage_dir}",
            }
        )

    browser_executable = str(os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE") or "").strip()
    browser_path = Path(browser_executable).expanduser() if browser_executable else None
    if not browser_path or not _is_valid_executable(browser_path):
        candidate_exe, _candidate_root = _resolve_browser_from_roots(app_root)
        if candidate_exe is not None:
            browser_path = candidate_exe
            os.environ["PLAYWRIGHT_CHROME_EXECUTABLE"] = str(candidate_exe)
        else:
            browser_path = None

    try:
        from src.playwright_service import resolve_playwright_executable

        resolved = resolve_playwright_executable(headless=False) or resolve_playwright_executable(
            headless=True
        )
        if resolved:
            browser_path = resolved
            os.environ["PLAYWRIGHT_CHROME_EXECUTABLE"] = str(resolved)
    except Exception:
        pass

    if browser_path is None or not _is_valid_executable(browser_path):
        issues.append(
            {
                "level": "critical",
                "code": "browser_not_found",
                "message": "No valid Playwright browser executable found.",
            }
        )

    missing_keys = [
        key
        for key in (
            "AUTORESPONDER_DM_SCROLL_WAIT_MS",
            "AUTORESPONDER_DM_SCROLL_ATTEMPTS",
            "AUTORESPONDER_DM_STAGNANT_BASE_LIMIT",
            "AUTORESPONDER_DM_STAGNANT_MAX_LIMIT",
        )
        if not str(os.environ.get(key) or "").strip()
    ]
    if missing_keys:
        issues.append(
            {
                "level": "warning",
                "code": "dm_tuning_missing",
                "message": "Missing DM tuning env keys: " + ", ".join(missing_keys),
            }
        )

    try:
        from accounts import list_all, mark_connected

        accounts = list_all()
        for account in accounts:
            username = _normalize_username(account.get("username"))
            if not username:
                continue
            account_total += 1
            connected = bool(account.get("connected"))
            if connected:
                connected_total += 1
            storage_state = _storage_state_path_for_user(profiles_dir, username)
            if connected and not storage_state.exists():
                connected_without_storage_state.append(username)
                if sync_connected:
                    try:
                        mark_connected(username, False)
                        disconnected_by_preflight.append(username)
                    except Exception:
                        pass
    except Exception as exc:
        issues.append(
            {
                "level": "warning",
                "code": "accounts_preflight_unavailable",
                "message": f"Could not validate account/profile integrity: {exc}",
            }
        )

    if connected_without_storage_state:
        issues.append(
            {
                "level": "warning",
                "code": "connected_without_storage_state",
                "message": (
                    "Connected accounts without storage_state.json: "
                    + ", ".join(connected_without_storage_state[:20])
                    + (" ..." if len(connected_without_storage_state) > 20 else "")
                ),
            }
        )

    critical_count = sum(1 for issue in issues if issue.get("level") == "critical")
    warning_count = sum(1 for issue in issues if issue.get("level") == "warning")

    report = {
        "mode": _normalize_mode(mode),
        "app_data_root": str(app_root),
        "profiles_dir": str(profiles_dir),
        "storage_dir": str(storage_dir),
        "browser_executable": str(browser_path) if browser_path else "",
        "account_total": account_total,
        "connected_total": connected_total,
        "connected_without_storage_state": connected_without_storage_state,
        "disconnected_by_preflight": disconnected_by_preflight,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "issues": issues,
        "timestamp": int(time.time()),
    }

    report_path = app_root / _PREFLIGHT_REPORT_REL
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass
    report["report_path"] = str(report_path)

    if strict and critical_count > 0:
        raise RuntimeError(format_runtime_preflight(report))

    return report


def format_runtime_preflight(report: dict[str, Any]) -> str:
    issues = report.get("issues") or []
    lines = [
        "[runtime] preflight summary",
        f"mode: {report.get('mode')}",
        f"app_data_root: {report.get('app_data_root')}",
        f"profiles_dir: {report.get('profiles_dir')}",
        f"browser_executable: {report.get('browser_executable') or '-'}",
        f"accounts: total={report.get('account_total', 0)} connected={report.get('connected_total', 0)}",
        (
            "connected_without_storage_state: "
            + str(len(report.get("connected_without_storage_state") or []))
        ),
        f"issues: critical={report.get('critical_count', 0)} warning={report.get('warning_count', 0)}",
        f"report_path: {report.get('report_path') or '-'}",
    ]
    for issue in issues[:15]:
        level = str(issue.get("level") or "info").upper()
        code = str(issue.get("code") or "-")
        message = str(issue.get("message") or "")
        lines.append(f"- {level} [{code}] {message}")
    if len(issues) > 15:
        lines.append(f"- ... ({len(issues) - 15} more issues)")
    return "\n".join(lines)
