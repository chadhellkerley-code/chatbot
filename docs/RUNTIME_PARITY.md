# Runtime Parity (Owner vs Client EXE)

This project now uses a unified runtime bootstrap/preflight layer:

- `runtime_parity.py`

It is executed from:

- `app.py` (owner mode)
- `gui_app.py` (owner/client GUI boot)
- `client_launcher.py` (client EXE boot)
- `license_client.py` (client license flow + preflight gate)

## What It Enforces

1. Same `APP_DATA_ROOT` contract across modes.
2. Deterministic `PROFILES_DIR` and runtime folders.
3. Unified Playwright browser resolution and environment wiring.
4. Runtime preflight report at:
   - `storage/runtime_preflight_report.json`
5. Session integrity:
   - Connected accounts without `profiles/<user>/storage_state.json` are flagged and disconnected.

## Runtime Config

Bootstrap reads and creates:

- `storage/runtime_config.json`

Use this file to pin operational env values across machines (for example DM scroll tuning).

## Deployment Checklist (Client EXE)

1. Ship the EXE with embedded browser folders (`playwright_browsers` or equivalent).
2. Keep the same `storage/runtime_config.json` policy used by owner.
3. Ensure writable permission in `APP_DATA_ROOT/storage` and `APP_DATA_ROOT/profiles`.
4. Validate first run by checking:
   - `storage/runtime_preflight_report.json`
   - No critical issues.

