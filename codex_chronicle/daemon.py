"""Background Codex Chronicle daemon.

Only processes sessions when `processing_mode=background`. In foreground
mode (the default), this daemon idles rather than exits — avoiding a
KeepAlive restart loop under launchd/systemd.

When background mode is active:
- Polls ~/.codex-chronicle/events.jsonl for hook events.
- Global debounce — waits until all sessions have been quiet for
  `quiet_minutes` (default 5) before processing.
- Periodic scanner (default every 30 min) that queues any
  ~/.codex/sessions/.../rollout-*.jsonl without a .processed marker.
- Processes in parallel (default 5 workers via asyncio.Semaphore).
- Holds ~/.codex-chronicle/processing.lock across batches.
- Graceful SIGTERM: terminates in-flight codex subprocesses.

Usage:
    python -m codex_chronicle.daemon          # foreground
    python -m codex_chronicle.daemon --bg     # daemonize
    python -m codex_chronicle.daemon --stop   # SIGTERM running daemon
    python -m codex_chronicle.daemon --status # check status
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from .codex_cli import terminate_active_subprocesses
from .config import (
    chronicle_dir, codex_sessions_dir, events_file, offset_file, pid_file,
    load_config, save_default_config,
)
from .extractor import extract_session
from .filtering import should_skip
from .locks import (
    acquire_daemon_lock, daemon_lock_still_valid, daemon_is_running,
    processing_lock,
)
from .mode import is_background_mode
from .storage import (
    is_succeeded, is_terminal_failure, write_chronicle,
)
from .summarizer import async_summarize_session


def _read_offset() -> int:
    if offset_file().exists():
        try:
            return int(offset_file().read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def _save_offset(offset: int):
    tmp = offset_file().with_suffix(".tmp")
    tmp.write_text(str(offset))
    os.replace(str(tmp), str(offset_file()))


def _extract_and_filter(event: dict, config: dict):
    session_id = event.get("session_id", "")
    transcript_path = event.get("transcript_path", "")

    # If transcript_path not in event, try to locate the file from session_id
    if not transcript_path:
        transcript_path = _find_session_file(session_id)

    if not transcript_path or not Path(transcript_path).exists():
        return None

    try:
        digest = extract_session(transcript_path)
    except Exception as e:
        print(f"[codex-chronicle] extraction failed: {e}", file=sys.stderr)
        return None

    # Override project slug with cwd from event if available
    cwd = event.get("cwd", event.get("workdir", ""))
    if cwd and digest.project_slug == "unknown-project":
        from .config import cwd_to_slug
        digest.project_slug = cwd_to_slug(cwd)
        digest.project_path = cwd

    reason = should_skip(digest, config)
    if reason:
        print(f"[codex-chronicle] skipping {session_id[:8]}: {reason}")
        return None

    return digest


def _find_session_file(session_id: str) -> str | None:
    """Search ~/.codex/sessions/ for a JSONL matching session_id."""
    root = codex_sessions_dir()
    if not root.exists():
        return None
    # Search for rollout-<session_id>.jsonl
    for candidate in root.rglob(f"rollout-{session_id}.jsonl"):
        return str(candidate)
    # Try without rollout- prefix (some versions)
    for candidate in root.rglob(f"{session_id}.jsonl"):
        return str(candidate)
    return None


async def _async_process_one(event: dict, config: dict, semaphore: asyncio.Semaphore):
    digest = _extract_and_filter(event, config)
    if digest is None:
        return None
    async with semaphore:
        print(f"[codex-chronicle] summarizing session {digest.session_id[:8]} "
              f"({digest.total_turns} turns, {len(digest.user_prompts)} prompts)...")
        entry = await async_summarize_session(digest)
        return (digest, entry)


async def _process_batch(events: list[tuple[str, dict]], config: dict) -> list[tuple[str, dict]]:
    concurrency = config.get("concurrency", 5)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_async_process_one(event, config, semaphore) for _, event in events]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    max_retries = config.get("max_retries", 3)
    pending_writes = []
    retry = []
    for (sid, ev), result in zip(events, results):
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        if isinstance(result, Exception):
            print(f"[codex-chronicle] error processing {sid[:8]}: {result}", file=sys.stderr)
            retry.append((sid, ev))
        elif result is not None:
            pending_writes.append(result)

    pending_writes.sort(key=lambda pair: pair[0].start_time)
    for digest, entry in pending_writes:
        write_chronicle(entry, digest, max_retries=max_retries)
        if (entry.is_error
                and not is_succeeded(digest.session_id)
                and not is_terminal_failure(digest.session_id)):
            for sid, ev in events:
                if sid == digest.session_id:
                    retry.append((sid, ev))
                    break

    return retry


def _read_new_events(offset: int) -> tuple[list[dict], int]:
    if not events_file().exists():
        return [], offset

    file_size = events_file().stat().st_size
    if offset > file_size:
        print(f"[codex-chronicle] offset ({offset}) exceeds file size ({file_size}), resetting to 0")
        offset = 0

    with open(events_file(), "rb") as f:
        f.seek(offset)
        buf = f.read()

    events = []
    pos = 0
    last_complete_end = 0
    while pos < len(buf):
        nl = buf.find(b"\n", pos)
        if nl == -1:
            break
        line = buf[pos:nl].strip()
        pos = nl + 1
        last_complete_end = pos
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            print("[codex-chronicle] skipping malformed event line", file=sys.stderr)
            continue

    new_offset = offset + last_complete_end
    return events, new_offset


def _process_events(events: list[dict], pending_sessions: dict) -> bool:
    activity = False
    for event in events:
        event_name = event.get("hook_event_name", event.get("type", ""))
        session_id = event.get("session_id", "")

        if event_name in ("UserPromptSubmit", "Stop"):
            activity = True

        if event_name == "Stop" and session_id:
            existing = pending_sessions.get(session_id)
            if not existing or not existing.get("transcript_path"):
                pending_sessions[session_id] = event
        elif event_name == "UserPromptSubmit" and session_id:
            pending_sessions.pop(session_id, None)

    return activity


def _acquire_lock() -> bool:
    return acquire_daemon_lock()


def _lock_still_valid() -> bool:
    return daemon_lock_still_valid()


def _is_running() -> tuple[bool, int | None]:
    return daemon_is_running()


def _scan_for_unprocessed(pending_sessions: dict, config: dict) -> int:
    """Scan ~/.codex/sessions/ for rollout-*.jsonl files not yet chronicled."""
    sessions_root = codex_sessions_dir()
    if not sessions_root.exists():
        return 0

    queued = 0
    quiet_seconds = config.get("quiet_minutes", 5) * 60
    now = time.time()
    skip_projects = config.get("skip_projects", [])

    # Build session_id → cwd map for project slug resolution
    sid_to_cwd: dict[str, str] = {}
    try:
        from .batch import _build_sid_cwd_map
        sid_to_cwd = _build_sid_cwd_map()
    except Exception:
        pass

    for jsonl_file in sessions_root.rglob("rollout-*.jsonl"):
        stem = jsonl_file.stem
        session_id = stem.removeprefix("rollout-") if stem.startswith("rollout-") else stem

        if session_id in pending_sessions:
            continue

        try:
            age = now - jsonl_file.stat().st_mtime
            if age < quiet_seconds:
                continue
        except OSError:
            continue

        # Determine cwd/slug for skip-projects check
        cwd = sid_to_cwd.get(session_id, "")
        slug = cwd.rstrip("/").replace("/", "-") if cwd else session_id
        if any(sp in slug for sp in skip_projects):
            continue

        if is_succeeded(session_id):
            continue
        if is_terminal_failure(session_id):
            continue

        pending_sessions[session_id] = {
            "session_id": session_id,
            "transcript_path": str(jsonl_file),
            "cwd": cwd,
            "hook_event_name": "Stop",
            "source": "scan",
        }
        queued += 1

    return queued


async def run_daemon_async():
    save_default_config()

    if not _acquire_lock():
        running, pid = _is_running()
        if running:
            print(f"[codex-chronicle] daemon already running (pid {pid})")
            sys.exit(1)
        print("[codex-chronicle] could not acquire singleton lock and no running "
              f"daemon detected — check permissions on {pid_file()}", file=sys.stderr)
        sys.exit(2)

    print(f"[codex-chronicle] daemon started (pid {os.getpid()})")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        loop.add_signal_handler(sig, stop_event.set)

    config = load_config()
    poll_interval = config.get("poll_interval_seconds", 5)
    offset = _read_offset()
    pending_sessions: dict = {}
    last_activity = 0.0
    last_scan = 0.0
    idle_printed_once = False

    try:
        while not stop_event.is_set():
            try:
                if not is_background_mode():
                    if not idle_printed_once:
                        print("[codex-chronicle] processing_mode=foreground — "
                              "daemon idle; run `codex-chronicle uninstall-daemon` to remove this service",
                              file=sys.stderr)
                        idle_printed_once = True
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                idle_printed_once = False

                if not _lock_still_valid():
                    print("[codex-chronicle] PID file replaced — another daemon took over, exiting")
                    break

                config = load_config()
                events, new_offset = _read_new_events(offset)

                if _process_events(events, pending_sessions):
                    last_activity = time.time()

                offset = new_offset

                scan_interval = config.get("scan_interval_minutes", 30) * 60
                now = time.time()
                if now - last_scan >= scan_interval:
                    queued = _scan_for_unprocessed(pending_sessions, config)
                    if queued:
                        print(f"[codex-chronicle] scan found {queued} un-chronicled session(s)")
                        if not last_activity:
                            last_activity = now
                    last_scan = now

                quiet_minutes = config.get("quiet_minutes", 5)
                now = time.time()
                global_quiet = (
                    (now - last_activity) >= (quiet_minutes * 60)
                    if last_activity else False
                )

                if global_quiet and pending_sessions:
                    to_process = list(pending_sessions.items())
                    pending_sessions.clear()
                    try:
                        with processing_lock(blocking=False) as acquired:
                            if not acquired:
                                print("[codex-chronicle] processing lock held — deferring",
                                      file=sys.stderr)
                                for sid, ev in to_process:
                                    pending_sessions[sid] = ev
                                last_activity = time.time()
                            else:
                                retry = await _process_batch(to_process, config)
                                if retry:
                                    for sid, ev in retry:
                                        pending_sessions[sid] = ev
                                    last_activity = time.time()
                    except asyncio.CancelledError:
                        for sid, ev in to_process:
                            pending_sessions[sid] = ev
                        raise
                    except Exception as e:
                        print(f"[codex-chronicle] batch error: {e}", file=sys.stderr)
                        for sid, ev in to_process:
                            pending_sessions[sid] = ev
                        last_activity = time.time()

                if not pending_sessions:
                    _save_offset(offset)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[codex-chronicle] loop error: {e}", file=sys.stderr)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        terminated = await terminate_active_subprocesses(grace_seconds=5.0)
        if terminated.get("terminated"):
            print(f"[codex-chronicle] terminated {terminated['terminated']} in-flight "
                  f"codex subprocess(es), killed {terminated['killed']}")
        print("[codex-chronicle] daemon stopped")


def run_daemon():
    asyncio.run(run_daemon_async())


def main():
    parser = argparse.ArgumentParser(description="Codex Chronicle daemon")
    parser.add_argument("--bg", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        running, pid = _is_running()
        if running:
            print(f"Codex Chronicle daemon is running (pid {pid})")
        else:
            print("Codex Chronicle daemon is not running")
        sys.exit(0)

    if args.stop:
        running, pid = _is_running()
        if running:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (pid {pid})")
        else:
            print("No daemon running")
        sys.exit(0)

    if args.bg:
        pid = os.fork()
        if pid > 0:
            print(f"[codex-chronicle] daemon started in background (pid {pid})")
            sys.exit(0)
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDONLY)
        os.dup2(devnull, sys.stdin.fileno())
        os.close(devnull)
        chronicle_dir().mkdir(parents=True, exist_ok=True)
        log_file = chronicle_dir() / "daemon.log"
        log_fd = open(log_file, "a")
        os.dup2(log_fd.fileno(), sys.stdout.fileno())
        os.dup2(log_fd.fileno(), sys.stderr.fileno())
        log_fd.close()

    run_daemon()


if __name__ == "__main__":
    main()
