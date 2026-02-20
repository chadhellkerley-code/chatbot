# client_launcher.py
# -*- coding: utf-8 -*-
"""Punto de entrada para ejecutables generados."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from runtime_parity import bootstrap_runtime_env


def _bootstrap_playwright_env() -> None:
    frozen = getattr(sys, "frozen", False)
    if frozen:
        exe_dir = Path(sys.executable).resolve().parent
    else:
        exe_dir = Path(__file__).resolve().parent
    pw_root = exe_dir / "playwright_browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_root)
    os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    os.environ.setdefault("PLAYWRIGHT_DOWNLOAD_HOST", "")
    exists = pw_root.exists()
    print(
        "Playwright bootstrap: "
        f"exe_dir={exe_dir} pw_root={pw_root} exists={'true' if exists else 'false'}"
    )
    if not exists:
        print("Error: Falta la carpeta playwright_browsers al lado del exe.")
        sys.exit(2)
    try:
        entries = [item.name for item in pw_root.iterdir() if item.is_dir()]
    except Exception:
        entries = []
    has_chromium = any(name.startswith("chromium-") for name in entries)
    has_headless = any(name.startswith("chromium_headless_shell-") for name in entries)
    if not (has_chromium or has_headless):
        print(
            "Error: playwright_browsers no contiene Chromium válido "
            "(chromium-* o chromium_headless_shell-*)."
        )
        sys.exit(2)


_bootstrap_playwright_env()
bootstrap_runtime_env("client")


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


def _validate_executable(exe_path: Path) -> Tuple[bool, int]:
    size = _safe_stat_size(exe_path)
    if size <= _MIN_EXECUTABLE_BYTES:
        return False, size
    return True, size


def _select_executable(
    browser_dir: Path,
    candidates: List[Path],
    reason: str,
    browser_type: str,
    prefix: str,
) -> Optional[Tuple[Path, int, str, Path, str, str]]:
    for exe_path in candidates:
        ok, size = _validate_executable(exe_path)
        if ok:
            version = browser_dir.name[len(prefix) :] if browser_dir.name.startswith(prefix) else ""
            return exe_path, size, reason, browser_dir, browser_type, version
    return None


def _select_playwright_root(
    candidate: Path,
) -> Optional[Tuple[Path, Path, int, str, Path, str, str]]:
    if not candidate.exists():
        return None

    headless_dir = _pick_latest_dir(candidate, _PLAYWRIGHT_HEADLESS_PREFIX)
    if headless_dir:
        selection = _select_executable(
            headless_dir,
            _headless_exe_candidates(headless_dir),
            "accepted_standalone",
            "headless",
            _PLAYWRIGHT_HEADLESS_PREFIX,
        )
        if selection:
            exe_path, size, reason, browser_dir, browser_type, version = selection
            return candidate, exe_path, size, reason, browser_dir, browser_type, version

    chromium_dir = _pick_latest_dir(candidate, _PLAYWRIGHT_CHROMIUM_PREFIX)
    if chromium_dir:
        selection = _select_executable(
            chromium_dir,
            _chromium_exe_candidates(chromium_dir),
            "accepted_standalone",
            "chromium",
            _PLAYWRIGHT_CHROMIUM_PREFIX,
        )
        if selection:
            exe_path, size, reason, browser_dir, browser_type, version = selection
            return candidate, exe_path, size, reason, browser_dir, browser_type, version

    nested = candidate / "ms-playwright"
    if nested.exists():
        return _select_playwright_root(nested)
    return None


def _log_playwright_selection(
    root: Path,
    browser_dir: Path,
    exe_path: Path,
    size: int,
    reason: str,
    browser_type: str,
    version: str,
) -> None:
    global _PLAYWRIGHT_SELECTION_LOGGED
    if _PLAYWRIGHT_SELECTION_LOGGED:
        return
    _PLAYWRIGHT_SELECTION_LOGGED = True
    print(
        "Selected browser: "
        f"type={browser_type} version={version or '-'} "
        f"exe={exe_path.name} size={size} reason={reason}"
    )


def _resolve_playwright_browsers_path() -> Optional[
    Tuple[Path, Path, int, str, Path, str, str]
]:
    candidates: List[Path] = []
    exe_parent = None
    if getattr(sys, "frozen", False):
        exe = getattr(sys, "executable", "") or ""
        if exe:
            try:
                exe_parent = Path(exe).resolve().parent
            except Exception:
                exe_parent = None
    if exe_parent:
        candidates.extend(
            [
                exe_parent / "playwright_browsers",
                exe_parent / "playwright",
            ]
        )

    app_root = Path(__file__).resolve().parent
    candidates.extend([app_root / "playwright_browsers", app_root / "playwright"])

    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.extend([Path(base) / "playwright_browsers", Path(base) / "playwright"])

    for candidate in candidates:
        selection = _select_playwright_root(candidate)
        if selection:
            return selection
    return None


def _configure_playwright_browsers() -> None:
    current = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if current:
        selection = _select_playwright_root(Path(current).expanduser())
        if selection:
            root, exe_path, size, reason, browser_dir, browser_type, version = selection
            if str(root) != current:
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
            _log_playwright_selection(
                root, browser_dir, exe_path, size, reason, browser_type, version
            )
            return
        print(
            "Error: No se encontró un Chromium/headless sano en PLAYWRIGHT_BROWSERS_PATH. "
            f"Ruta: {current}"
        )
        sys.exit(2)
    selection = _resolve_playwright_browsers_path()
    if selection:
        root, exe_path, size, reason, browser_dir, browser_type, version = selection
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
        _log_playwright_selection(
            root, browser_dir, exe_path, size, reason, browser_type, version
        )


_configure_playwright_browsers()


def _configure_app_version() -> None:
    if os.environ.get("APP_VERSION") or os.environ.get("CLIENT_VERSION"):
        return
    candidates: List[Path] = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(Path(base) / "storage" / "app_version.json")
    exe_path = getattr(sys, "executable", "") or ""
    if exe_path:
        try:
            exe_parent = Path(exe_path).resolve().parent
        except Exception:
            exe_parent = None
        if exe_parent:
            candidates.append(exe_parent / "storage" / "app_version.json")
    candidates.append(Path(__file__).resolve().parent / "storage" / "app_version.json")

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
            version = str(payload.get("version") or payload.get("app_version") or "").strip()
        except Exception:
            version = ""
        if version:
            os.environ["APP_VERSION"] = version
            return


_configure_app_version()

from license_client import launch_with_license


if __name__ == "__main__":
    launch_with_license()
