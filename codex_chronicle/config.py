"""Paths, constants, and configuration for Codex Chronicle.

Path helpers are LAZY functions — each call re-resolves Path.home() so
tests that monkeypatch HOME see fresh paths without importlib.reload.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


# ---------- Lazy path helpers ----------

def chronicle_dir() -> Path:
    """Codex Chronicle's own state dir. Defaults to ~/.codex-chronicle/.

    Honors $CODEX_CHRONICLE_HOME so lifecycle commands resolve to the
    same location install.sh wrote to when the user overrode the default.
    """
    env = os.environ.get("CODEX_CHRONICLE_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".codex-chronicle"


def events_file() -> Path:
    return chronicle_dir() / "events.jsonl"


def offset_file() -> Path:
    return chronicle_dir() / "events.offset"


def pid_file() -> Path:
    return chronicle_dir() / "daemon.pid"


def processing_lock_path() -> Path:
    return chronicle_dir() / "processing.lock"


def config_file() -> Path:
    return chronicle_dir() / "config.json"


def projects_dir() -> Path:
    return chronicle_dir() / "projects"


def processed_dir() -> Path:
    return chronicle_dir() / ".processed"


def failed_dir() -> Path:
    return chronicle_dir() / ".failed"


def codex_sessions_dir() -> Path:
    """~/.codex/sessions/ — Codex CLI's session-transcript storage.
    Codex Chronicle reads but never writes this."""
    codex_home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    return Path(codex_home) / "sessions"


def codex_hooks_file() -> Path:
    """~/.codex/hooks.json — Codex CLI hook configuration."""
    codex_home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    return Path(codex_home) / "hooks.json"


def codex_config_file() -> Path:
    """~/.codex/config.toml — Codex CLI main configuration."""
    codex_home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    return Path(codex_home) / "config.toml"


# ---------- Processing modes ----------

PROCESSING_MODES = ("foreground", "background")

DEFAULT_CONFIG = {
    "processing_mode": "foreground",
    "concurrency": 5,
    "model": "o3",
    "poll_interval_seconds": 5,
    "quiet_minutes": 5,
    "scan_interval_minutes": 30,
    "max_retries": 3,
    "skip_projects": [],
    "fallback_model": "o4-mini",
}


# ---------- Config read/write ----------

def load_config() -> dict:
    """Return the merged config dict. Never raises."""
    cf = config_file()
    if not cf.exists():
        return dict(DEFAULT_CONFIG)
    try:
        with open(cf) as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            return {**DEFAULT_CONFIG, "_load_error":
                    f"{cf}: top-level JSON is not an object"}
        return {**DEFAULT_CONFIG, **user_config}
    except (OSError, json.JSONDecodeError) as e:
        return {**DEFAULT_CONFIG, "_load_error": f"{cf}: {e}"}


def save_default_config():
    d = chronicle_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    cf = config_file()
    if not cf.exists():
        with open(cf, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        os.chmod(cf, 0o600)


# ---------- Per-project helpers ----------

def project_chronicle_dir(slug: str) -> Path:
    return projects_dir() / slug


def ensure_dirs(slug: str):
    d = project_chronicle_dir(slug)
    created = not d.exists()
    d.mkdir(parents=True, exist_ok=True)
    if created:
        os.chmod(d, 0o700)
    sessions = d / "sessions"
    sessions_created = not sessions.exists()
    sessions.mkdir(exist_ok=True)
    if sessions_created:
        os.chmod(sessions, 0o700)


def load_recent_titles(project_slug: str, max_entries: int = 10) -> list[str]:
    """Read recent session titles from a project's chronicle sessions dir."""
    sdir = projects_dir() / project_slug / "sessions"
    if not sdir.exists():
        return []
    titles = []
    for md_file in sorted(sdir.glob("*.md"), reverse=True)[:max_entries]:
        try:
            with open(md_file, errors="ignore") as f:
                first_line = f.readline().rstrip("\n")
            if first_line.startswith("# "):
                titles.append(first_line[2:])
        except Exception:
            continue
    return titles


def cwd_to_slug(cwd: str) -> str:
    """Convert an absolute path to a project slug (same convention as Claude Code)."""
    return cwd.rstrip("/").replace("/", "-")


# ---------- PEP 562 lazy-constant compat shim ----------

_LAZY_CONSTANTS = {
    "CHRONICLE_DIR": chronicle_dir,
    "EVENTS_FILE": events_file,
    "OFFSET_FILE": offset_file,
    "PID_FILE": pid_file,
    "PROCESSING_LOCK": processing_lock_path,
    "CONFIG_FILE": config_file,
    "PROJECTS_DIR": projects_dir,
    "PROCESSED_DIR": processed_dir,
    "FAILED_DIR": failed_dir,
    "CODEX_SESSIONS_DIR": codex_sessions_dir,
}


def __getattr__(name: str):
    factory = _LAZY_CONSTANTS.get(name)
    if factory is not None:
        return factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
