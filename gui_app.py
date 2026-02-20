from __future__ import annotations

import os
import json
import sys
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from io_adapter import IOAdapter
from main_window import MainWindow
from runtime_parity import bootstrap_runtime_env

ExecutionMode = Literal["owner", "client"]


def _read_stylesheet() -> str:
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "styles.qss")

    exe_path = getattr(sys, "executable", "") or ""
    if exe_path:
        try:
            candidates.append(Path(exe_path).resolve().parent / "styles.qss")
        except Exception:
            pass

    candidates.append(Path(__file__).resolve().with_name("styles.qss"))

    for path in candidates:
        if not path.exists():
            continue
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            continue
    return ""


def _resolve_mode(mode: Optional[str]) -> ExecutionMode:
    if mode in ("owner", "client"):
        return mode
    argv0 = Path(sys.argv[0]).stem.lower()
    if argv0 == "client_launcher":
        return "client"
    return "owner"


def _resolve_backend_entrypoint(
    mode: ExecutionMode,
    backend_entrypoint: Optional[Callable[[], None]],
) -> Callable[[], None]:
    if backend_entrypoint is not None:
        return backend_entrypoint
    if mode == "client":
        from license_client import launch_with_license

        return launch_with_license
    from app import menu

    return menu


def _should_show_exe_license_gate(mode: ExecutionMode) -> bool:
    return (
        mode == "owner"
        and bool(getattr(sys, "frozen", False))
        and os.environ.get("LICENSE_ALREADY_VALIDATED") != "1"
    )


def _validate_startup_license(license_key: str) -> tuple[bool, str]:
    provided = (license_key or "").strip()
    if not provided:
        return False, "Debes ingresar una licencia."

    try:
        from licensekit import validate_license_payload
    except Exception as exc:
        return False, f"No se pudo cargar la validacion de licencia: {exc}"

    payload = _load_startup_payload()
    if not payload:
        return False, "No se encontro el archivo de licencia."

    ok, message, _ = validate_license_payload(provided, payload)
    if not ok:
        return False, message or "Licencia invalida."

    os.environ["LICENSE_ALREADY_VALIDATED"] = "1"
    return True, ""


def _payload_candidates() -> list[Path]:
    candidates: list[Path] = []

    env_path = (os.environ.get("LICENSE_FILE") or "").strip()
    if env_path:
        candidates.append(Path(env_path).expanduser())

    exe_path = getattr(sys, "executable", "") or ""
    if exe_path:
        try:
            exe_root = Path(exe_path).resolve().parent
            candidates.extend(
                [
                    exe_root / "license.json",
                    exe_root / "license_payload.json",
                    exe_root / "storage" / "license_payload.json",
                    exe_root / "storage" / "license.json",
                ]
            )
        except Exception:
            pass

    app_data_root = (os.environ.get("APP_DATA_ROOT") or "").strip()
    if app_data_root:
        data_root = Path(app_data_root).expanduser()
        candidates.extend(
            [
                data_root / "license.json",
                data_root / "license_payload.json",
                data_root / "storage" / "license_payload.json",
                data_root / "storage" / "license.json",
            ]
        )

    local_root = Path(__file__).resolve().parent
    candidates.extend(
        [
            local_root / "license.json",
            local_root / "license_payload.json",
            local_root / "storage" / "license_payload.json",
            local_root / "storage" / "license.json",
        ]
    )
    return candidates


def _load_startup_payload() -> dict[str, Any]:
    for path in _payload_candidates():
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return {}


class _ExeLicenseGateDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Validacion de licencia")
        self.setObjectName("LicenseGateDialog")
        self.setModal(True)
        self.setMinimumWidth(720)
        self.setStyleSheet(
            "QDialog#LicenseGateDialog { background-color: #111827; font-family: 'Segoe UI'; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 28, 28, 28)

        card = QFrame()
        card.setObjectName("MetricCard")
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("HERRAMIENTA DE MENSAJERIA - PROPIEDAD DE MATIDIAZLIFE")
        title.setObjectName("PageTitle")
        title.setWordWrap(True)
        layout.addWidget(title)

        prompt = QLabel("Ingresa la licencia y presiona Enter para continuar:")
        prompt.setObjectName("MutedText")
        prompt.setWordWrap(True)
        layout.addWidget(prompt)

        self._license_input = QLineEdit()
        self._license_input.setObjectName("InputField")
        self._license_input.setClearButtonEnabled(True)
        self._license_input.returnPressed.connect(self._submit)
        layout.addWidget(self._license_input)

        self._error_label = QLabel("")
        self._error_label.setObjectName("MutedText")
        self._error_label.setStyleSheet("color: #f87171;")
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

        submit = QPushButton("Continuar")
        submit.setObjectName("PrimaryButton")
        submit.clicked.connect(self._submit)
        layout.addWidget(submit)

        QTimer.singleShot(0, self._license_input.setFocus)

    def _submit(self) -> None:
        ok, message = _validate_startup_license(self._license_input.text())
        if ok:
            self.accept()
            return
        self._error_label.setText(message)
        self._license_input.setFocus()
        self._license_input.selectAll()


def launch_gui_app(
    backend_entrypoint: Optional[Callable[[], None]] = None,
    *,
    mode: Optional[str] = None,
) -> int:
    resolved_mode = _resolve_mode(mode)
    bootstrap_runtime_env(resolved_mode)
    resolved_entrypoint = _resolve_backend_entrypoint(resolved_mode, backend_entrypoint)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Insta CRM")

    stylesheet = _read_stylesheet()
    if stylesheet:
        app.setStyleSheet(stylesheet)

    if _should_show_exe_license_gate(resolved_mode):
        gate = _ExeLicenseGateDialog()
        if gate.exec() != QDialog.Accepted:
            return 2

    window = MainWindow(mode=resolved_mode)
    adapter = IOAdapter()
    window.bind_io_adapter(adapter)

    adapter.install()
    window.show()

    QTimer.singleShot(0, lambda: window.start_backend(resolved_entrypoint))

    qt_exit = app.exec()
    adapter.shutdown()
    adapter.uninstall()

    if window.backend_exit_code is not None:
        return window.backend_exit_code
    return qt_exit


if __name__ == "__main__":
    raise SystemExit(launch_gui_app(mode="owner"))
