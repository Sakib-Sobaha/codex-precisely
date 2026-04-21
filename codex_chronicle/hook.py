"""Hook dispatcher for Codex CLI events.

Registered in ~/.codex/hooks.json. Fires on every Codex lifecycle event.

Key differences from Claude Code hooks:
- Codex hooks are SYNCHRONOUS/BLOCKING — must return quickly.
- SessionStart and UserPromptSubmit fire simultaneously on the FIRST prompt
  of a session (known Codex bug #15266). We debounce via a state file.
- No SessionEnd event; Stop is the closest equivalent.
- additionalContext injection uses the same JSON response format.

Events:
- SessionStart: inject past session titles, optionally respawn daemon
- UserPromptSubmit: append event, skip if it's the simultaneous first fire
- PreToolUse / PostToolUse: append event (no blocking needed)
- Stop: append event (session finished, daemon picks it up)
"""

import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    chronicle_dir, pid_file, events_file, load_recent_titles,
)

_MAX_ERROR_LOG_BYTES = 1_000_000

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Debounce window in seconds for the simultaneous SessionStart+UserPromptSubmit
# first-fire bug. If a UserPromptSubmit arrives within this window of a
# SessionStart for the same session, it's the simultaneous fire — skip it.
_DEDUP_WINDOW_SECONDS = 2.0
_session_start_times: dict[str, float] = {}


def _daemon_running() -> bool:
    try:
        pid = int(pid_file().read_text().strip())
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
        return False


def _spawn_daemon_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "daemon"]
    return [sys.executable, "-m", "codex_chronicle.daemon"]


def _spawn_daemon():
    log_file = chronicle_dir() / "daemon.log"
    with open(log_file, "a") as log_fd:
        subprocess.Popen(
            _spawn_daemon_cmd(),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(PROJECT_ROOT),
        )


def main():
    try:
        chronicle_dir().mkdir(parents=True, exist_ok=True)
        os.chmod(str(chronicle_dir()), 0o700)
        data = json.loads(sys.stdin.read())
        event_name = data.get("hook_event_name", data.get("type", ""))
        data["hook_event_name"] = event_name  # normalise key
        data["chronicle_timestamp"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        session_id = data.get("session_id", "")

        # Deduplicate simultaneous SessionStart + UserPromptSubmit.
        # Codex bug: on the very first prompt both events fire at the same
        # instant. We record the SessionStart time and skip the paired
        # UserPromptSubmit if it arrives within the debounce window.
        if event_name == "SessionStart":
            _session_start_times[session_id] = time.monotonic()
        elif event_name == "UserPromptSubmit" and session_id:
            start_at = _session_start_times.get(session_id, 0.0)
            if time.monotonic() - start_at < _DEDUP_WINDOW_SECONDS:
                # Simultaneous fire — skip logging, still inject context below
                pass
            else:
                with open(events_file(), "a") as f:
                    f.write(json.dumps(data, separators=(",", ":")) + "\n")
                return  # UserPromptSubmit: nothing more to do

        # Always log non-UserPromptSubmit events
        if event_name != "UserPromptSubmit":
            with open(events_file(), "a") as f:
                f.write(json.dumps(data, separators=(",", ":")) + "\n")

        if event_name == "SessionStart":
            try:
                from .mode import is_background_mode
                bg = is_background_mode()
            except Exception:
                bg = False
            if bg and not _daemon_running():
                _spawn_daemon()

            cwd = data.get("cwd", data.get("workdir", ""))
            if cwd:
                from .config import cwd_to_slug
                slug = cwd_to_slug(cwd)
                titles = load_recent_titles(slug)
                if titles:
                    context = (
                        "Previous sessions in this project (from Codex Chronicle):\n"
                        + "\n".join(f"- {t}" for t in titles)
                        + "\n\nThese are chronicled decisions from past sessions. "
                        "You can reference them if relevant to the current work."
                    )
                    print(json.dumps({
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": context,
                        }
                    }))

    except Exception:
        try:
            error_log = chronicle_dir() / "hook-errors.log"
            error_log.parent.mkdir(parents=True, exist_ok=True)
            if error_log.exists() and error_log.stat().st_size > _MAX_ERROR_LOG_BYTES:
                content = error_log.read_bytes()
                error_log.write_bytes(content[-(_MAX_ERROR_LOG_BYTES // 2):])
            with open(error_log, "a") as f:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                f.write(f"\n--- {ts} ---\n{traceback.format_exc()}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
