# Viddrop — Project Instructions

A Linux AppImage GUI app for downloading and converting internet videos.
Uses yt-dlp as download backend and FFmpeg for media conversion.
Inspired by the Parabolic app. Built with PyQt6.

## Stack
- Python 3.11+
- PyQt6 (GUI framework)
- yt-dlp (download backend, via subprocess and Python API)
- FFmpeg (conversion, bundled inside AppImage via subprocess)
- SQLite via stdlib sqlite3 (queue and history persistence)
- keyring + libsecret (secure credential storage — never plain files)
- pytest + pytest-qt + pytest-asyncio (testing)
- ruff + mypy (linting and type checking)

## Commands
- `python -m pytest` — run all tests
- `python -m pytest tests/unit/` — unit tests only
- `python -m pytest tests/regression/` — regression tests only
- `ruff check src/` — lint
- `mypy src/` — type check
- `python src/viddrop/main.py` — run the app locally
- `./packaging/build_appimage.sh` — build the AppImage

## Architecture
- Business logic lives in `src/viddrop/core/`. UI files never import from each other's business logic directly.
- `queue_manager.py` is the single source of truth for download state.
- UI widgets emit signals; core modules handle the actual work.
- All downloads and conversions run in QThreadPool workers, never on the main thread.
- Credentials are stored ONLY via `credential_store.py` using `keyring`. Never write credentials to disk directly.
- Logging: every significant action is logged via `src/viddrop/utils/logger.py`. Log file: `~/.local/share/viddrop/viddrop.log` (rotating, 5 MB max, 3 backups).

## UI Structure
- Main window: sidebar (left) + content area (right)
- Sidebar has three navigation items: Add Videos, In Progress, Complete
- Land tab is Add Videos
- In Progress: shows active/paused/queued downloads with progress bars
- Complete: shows finished downloads with options to open folder, play, delete from list, delete from storage

## Themes
- Three themes: Dracula, Dark Nord, Breeze Light
- All themes are `.qss` files in `src/viddrop/themes/`
- Theme is applied at `QApplication` level; never hardcode colors in widget code

## Security Rules
- NEVER log credentials, tokens, cookies, or authentication headers
- NEVER pass credentials as CLI arguments visible in `ps aux` (use yt-dlp config files in a temp dir instead)
- NEVER expose raw yt-dlp or FFmpeg error output directly to the user (sanitize it first)
- NEVER store credentials in plain text files or SQLite

## Testing
- Every feature needs: happy path test, failure/error path test, and at least one edge case test
- Use `pytest-qt` for UI widget tests
- Use `pytest-asyncio` for async download worker tests
- Mock yt-dlp and FFmpeg in unit tests — never make real network calls in tests
- Regression tests may use a local test server (see `tests/conftest.py`)

## Don't Do
- Do not run yt-dlp or FFmpeg synchronously on the main thread
- Do not store secrets in SQLite or plain files
- Do not expose raw subprocess stderr to the user UI
- Do not hardcode theme colors — use QSS files only
- Do not add new Python dependencies without updating `pyproject.toml` and noting in the PR
