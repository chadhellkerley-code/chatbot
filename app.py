# app.py
# -*- coding: utf-8 -*-

## VERSION NUEVA - TEST UPDATE

import importlib
import os
import time

from runtime_parity import (
    bootstrap_runtime_env,
    format_runtime_preflight,
    run_runtime_preflight,
)

bootstrap_runtime_env("owner")

from config import SETTINGS
from storage import sent_totals_today
from ui import (
    Fore,
    clear_console,
    full_line,
    print_daily_metrics,
    print_header,
    style_text,
)
from utils import ask, em, press_enter, warn

try:
    from src.auth.onboarding import login_and_persist as _onboarding_login  # noqa: F401
    from src.auth.onboarding import onboard_accounts_from_csv as _onboarding_csv  # noqa: F401
except Exception as e:
    print("[ERROR] Backend de onboarding no disponible:", e)
    print("Instala dependencias: pip install playwright pyotp && playwright install")
    print("Verifica que existan los archivos src/__init__.py y src/auth/__init__.py")

# --- BEGIN OPT-IN HOOK ---


def _optin_enabled() -> bool:
    return os.getenv("OPTIN_ENABLE", "0") == "1"


def _optin_try_imports():
    try:
        from optin_browser import login as _opt_login
        from optin_browser import dm as _opt_dm
        from optin_browser import replies as _opt_replies
        from optin_browser import recorder as _opt_recorder
        from optin_browser import playback as _opt_playback
        return {
            "login": _opt_login,
            "dm": _opt_dm,
            "replies": _opt_replies,
            "recorder": _opt_recorder,
            "playback": _opt_playback,
        }
    except Exception:
        print("\n[OPT-IN] Falta el módulo opt-in o deps. Ejecuta:")
        print("  pip install -r requirements_optin.txt")
        print("  python -m playwright install\n")
        return None


def show_optin_menu():
    mods = _optin_try_imports()
    if not mods:
        input("Presiona Enter para volver...")
        return
    while True:
        print("\n=== Modo Automático (Opt-in navegador) ===")
        print("1) Login humanizado (guardar sesión)")
        print("2) Enviar DM (usar sesión guardada)")
        print("3) Responder no leídos")
        print("4) Grabar flujo (una sola vez)")
        print("5) Reproducir flujo grabado")
        print("6) Volver")
        choice = input("Elige una opción: ").strip()
        try:
            if choice == "1":
                acc = input("Alias de cuenta: ").strip()
                usr = input("Usuario IG: ").strip()
                pwd = input("Contraseña IG: ").strip()
                mods["login"].cli_login(acc, usr, pwd)
            elif choice == "2":
                acc = input("Alias de cuenta: ").strip()
                to_user = input("Enviar a (username): ").strip()
                text = input("Texto: ").strip()
                mods["dm"].cli_send_dm(acc, to_user, text)
            elif choice == "3":
                acc = input("Alias de cuenta: ").strip()
                reply = input("Respuesta: ").strip()
                mods["replies"].cli_reply_unread(acc, reply)
            elif choice == "4":
                alias = input("Nombre del flujo: ").strip()
                mods["recorder"].cli_record(alias)
            elif choice == "5":
                alias = input("Flujo: ").strip()
                mods["playback"].cli_play(alias, {}, "")
            elif choice == "6":
                break
            else:
                print("Opción inválida.")
        except Exception as e:
            print(f"[OPT-IN] Error: {e}")


def print_menu_extra_optin():
    if _optin_enabled():
        print("10) Modo Automático (Opt-in navegador)")


def handle_choice_optin(choice: str) -> bool:
    if _optin_enabled() and choice == "10":
        show_optin_menu()
        return True
    return False


# --- END OPT-IN HOOK ---


def _safe_import(name):
    try:
        return importlib.import_module(name)
    except Exception as e:
        warn(f"Módulo no disponible o con error: {name} ({e})")
        return None


accounts = _safe_import("accounts")
leads = _safe_import("leads")
ig = _safe_import("ig")
storage = _safe_import("storage")
responder = _safe_import("responder")
licensekit = _safe_import("licensekit")
state_view = _safe_import("state_view")
try:
    # Static import so frozen builds (PyInstaller) don't miss this module.
    from src.analytics import stats_engine as stats_engine  # type: ignore
except Exception as e:
    stats_engine = None
    warn(f"Módulo no disponible o con error: src.analytics.stats_engine ({e})")
whatsapp = _safe_import("whatsapp")
_RUNTIME_PREFLIGHT_DONE = False


def _counts():
    try:
        items = accounts.list_all()
        total = len(items)
        resolver = getattr(accounts, "connected_status", None)
        if callable(resolver):
            connected = sum(
                1
                for it in items
                if resolver(
                    it,
                    strict=False,
                    reason="dashboard-count",
                    fast=True,
                    persist=False,
                )
            )
        else:
            connected = sum(1 for it in items if it.get("connected"))
        active = sum(1 for it in items if it.get("active"))
        return total, connected, active
    except Exception:
        return 0, 0, 0


def _print_dashboard() -> None:
    print_header()
    total, connected, active = _counts()
    sent_today, err_today, last_reset, tz_label = sent_totals_today()

    line = full_line(color=Fore.BLUE, bold=True)
    section = style_text(em("📊  ESTADO GENERAL"), color=Fore.CYAN, bold=True)
    print(section)
    print(line)
    print(style_text(f"Cuentas totales: {total}", bold=True))
    print(style_text(f"Conectadas: {connected}", color=Fore.GREEN if connected else Fore.WHITE, bold=True))
    print(style_text(f"Activas: {active}", color=Fore.CYAN if active else Fore.WHITE, bold=True))
    print(line)
    print_daily_metrics(
        sent_today,
        err_today,
        tz_label,
        last_reset,
    )
    print()
    for text in current_menu_option_labels():
        print(style_text(text))
    print_menu_extra_optin()
    print()
    print(line)


def current_menu_option_labels() -> list[str]:
    options = [
        f"1) {em('🔐')} Gestionar cuentas de instagram",
        f"2) {em('🗂️')} Gestionar leads",
        f"3) {em('💬')} Enviar mensajes",
        f"4) {em('📜')} Ver registros de envíos",
        f"5) {em('🤖')} Auto-responder con OpenAI",
        f"6) {em('📊')} Estadísticas y métricas",
        f"7) {em('📱')} Automatización por WhatsApp",
    ]
    if not SETTINGS.client_distribution:
        options.append(f"8) {em('📦')} Entregar a cliente")
        options.append(f"9) {em('🔄')} Actualizaciones")
        options.append(f"10) {em('🚪')} Salir")
    else:
        options.append(f"8) {em('🔄')} Actualizaciones")
        options.append(f"9) {em('🚪')} Salir")
    return options


def menu():
    global _RUNTIME_PREFLIGHT_DONE
    if not _RUNTIME_PREFLIGHT_DONE:
        runtime_mode = "client" if SETTINGS.client_distribution else "owner"
        preflight = run_runtime_preflight(
            runtime_mode,
            strict=False,
            sync_connected=True,
        )
        print(format_runtime_preflight(preflight))
        if int(preflight.get("critical_count", 0)) > 0:
            raise RuntimeError(
                "Runtime preflight failed with critical issues. "
                f"Report: {preflight.get('report_path')}"
            )
        _RUNTIME_PREFLIGHT_DONE = True

    if licensekit and hasattr(licensekit, "enforce_startup_validation"):
        licensekit.enforce_startup_validation()
    
    # Verificación automática de actualizaciones al inicio (solo si no es distribución de cliente)
    if not SETTINGS.client_distribution:
        try:
            from update_system import auto_update_check
            auto_update_check()
        except ImportError:
            pass  # Sistema de actualizaciones no disponible
    
    while True:
        clear_console()
        _print_dashboard()
        op = ask("Opción: ").strip()
        if handle_choice_optin(op):
            continue
        if op == "1" and accounts:
            clear_console()
            accounts.menu_accounts()
        elif op == "2" and leads:
            clear_console()
            leads.menu_leads()
        elif op == "3" and ig:
            clear_console()
            ig.menu_send_rotating()
        elif op == "4" and storage:
            clear_console()
            storage.menu_logs()
        elif op == "5" and responder:
            clear_console()
            responder.menu_autoresponder()
        elif op == "6" and stats_engine:
            clear_console()
            stats_engine.menu_stats()
        elif op == "7" and whatsapp:
            clear_console()
            whatsapp.menu_whatsapp()
        elif (
            op == "8"
            and licensekit
            and not SETTINGS.client_distribution
        ):
            licensekit.menu_deliver()
        elif (op == "9" and not SETTINGS.client_distribution) or (
            op == "8" and SETTINGS.client_distribution
        ):
            try:
                from update_system import menu_updates
                clear_console()
                menu_updates()
            except ImportError:
                warn("Sistema de actualizaciones no disponible.")
                press_enter()
        elif (
            (op == "9" and SETTINGS.client_distribution)
            or (op == "10" and not SETTINGS.client_distribution)
        ):
            print("Saliendo...")
            time.sleep(0.3)
            break
        else:
            warn("Opción inválida o módulo faltante.")
            press_enter()


if __name__ == "__main__":
    menu()
