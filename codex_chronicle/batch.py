"""Process existing Codex sessions into chronicle records.

Scans ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl for unprocessed sessions.

Usage:
    codex-chronicle process                                  # process pending
    codex-chronicle process --dry-run                        # preview
    codex-chronicle process --project bada --workers 5       # filter project
    codex-chronicle process --force --workers 5              # reprocess successes
    codex-chronicle process --retry-failed --workers 5       # retry terminal failures
"""

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

from .config import codex_sessions_dir, load_config, save_default_config
from .extractor import extract_session
from .filtering import should_skip
from .locks import processing_lock
from .mode import is_background_mode
from .service import pause_service, resume_service
from .storage import (
    write_chronicle, session_filename,
    rebuild_prompts_section,
)
from .summarizer import async_summarize_session

PROGRESS_INTERVAL_SECONDS = 15


def find_all_sessions(project_filter: str | None = None) -> list[tuple[str, Path]]:
    """Find all Codex session JSONL files.

    Codex stores sessions at: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    We scan recursively and build (project_slug, path) pairs.
    The project slug is derived from the cwd stored inside each JSONL's first
    metadata line. As a fast path, we use the hook events.jsonl to build a
    session_id → cwd mapping first.
    """
    sessions_root = codex_sessions_dir()
    if not sessions_root.exists():
        return []

    # Build session_id → cwd map from our own events.jsonl (fast)
    sid_to_cwd = _build_sid_cwd_map()

    sessions = []
    for jsonl_file in sorted(sessions_root.rglob("rollout-*.jsonl")):
        stem = jsonl_file.stem
        session_id = stem.removeprefix("rollout-") if stem.startswith("rollout-") else stem

        # Determine project slug
        cwd = sid_to_cwd.get(session_id, "")
        if cwd:
            project_slug = cwd.rstrip("/").replace("/", "-")
        else:
            # Fall back to reading first line of the JSONL
            project_slug = _slug_from_jsonl(jsonl_file) or "unknown-project"

        if project_filter and project_filter not in project_slug:
            continue

        sessions.append((project_slug, jsonl_file))

    return sessions


def _build_sid_cwd_map() -> dict[str, str]:
    """Read our own events.jsonl to build a session_id → cwd mapping."""
    from .config import events_file
    import json
    result: dict[str, str] = {}
    ef = events_file()
    if not ef.exists():
        return result
    try:
        with open(ef) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                sid = ev.get("session_id", "")
                cwd = ev.get("cwd", ev.get("workdir", ""))
                if sid and cwd:
                    result[sid] = cwd
    except OSError:
        pass
    return result


def _slug_from_jsonl(path: Path) -> str | None:
    """Read the first few lines of a Codex JSONL to extract cwd → slug."""
    import json
    try:
        with open(path) as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    cwd = (entry.get("cwd") or entry.get("workdir") or
                           entry.get("working_directory") or "")
                    if cwd:
                        return cwd.rstrip("/").replace("/", "-")
                except Exception:
                    continue
    except OSError:
        pass
    return None


async def _process_one(digest, semaphore):
    async with semaphore:
        sid = digest.session_id[:8]
        turns = digest.total_turns
        parts = digest.project_slug.lstrip("-").split("-")
        project_name = parts[-1] if parts else digest.project_slug
        print(f"  [{project_name}/{sid}] starting ({turns} turns, {len(digest.user_prompts)} prompts)...")

        start = time.time()
        task = asyncio.create_task(async_summarize_session(digest))
        while not task.done():
            done, _pending = await asyncio.wait({task}, timeout=PROGRESS_INTERVAL_SECONDS)
            if not done:
                elapsed = int(time.time() - start)
                print(f"  [{project_name}/{sid}] still processing... ({elapsed}s)")

        elapsed = int(time.time() - start)
        entry = task.result()
        if entry.is_error:
            print(f"  [{project_name}/{sid}] error after {elapsed}s")
        elif entry.is_empty:
            print(f"  [{project_name}/{sid}] no decisions ({elapsed}s)")
        else:
            print(f"  [{project_name}/{sid}] done ({elapsed}s) — {len(entry.decisions)} decisions")
        return entry


async def async_batch_process(
    project_filter: str | None = None,
    dry_run: bool = False,
    workers: int = 5,
    force: bool = False,
    retry_failed: bool = False,
):
    save_default_config()
    config = load_config()
    max_retries = int(config.get("max_retries", 3))
    sessions = find_all_sessions(project_filter)

    print(f"Found {len(sessions)} session files across "
          f"{len(set(s[0] for s in sessions))} projects\n")

    eligible = []
    skip_count = 0
    already_done = 0
    failed_skipped = 0

    for project_slug, jsonl_path in sessions:
        try:
            digest = extract_session(str(jsonl_path))
        except Exception as e:
            print(f"  SKIP {jsonl_path.stem[:8]}: extraction error: {e}")
            skip_count += 1
            continue

        # Override project slug if extractor found a better one from cwd
        if digest.project_slug and digest.project_slug != "unknown-project":
            project_slug = digest.project_slug
        else:
            digest.project_slug = project_slug

        reason = should_skip(digest, config, force=force, retry_failed=retry_failed)
        if reason:
            if reason == "already chronicled":
                already_done += 1
            elif reason == "terminal failure":
                failed_skipped += 1
            else:
                skip_count += 1
            continue

        eligible.append(digest)

    if dry_run:
        for digest in eligible:
            print(f"  WOULD PROCESS: {digest.project_slug}")
            print(f"    Session: {digest.session_id[:8]}")
            print(f"    Turns: {digest.total_turns}, Prompts: {len(digest.user_prompts)}")
            print(f"    Time: {digest.start_time[:19]} -> {digest.end_time[:19]}")
            if digest.user_prompts:
                print(f"    First prompt: {digest.user_prompts[0].text[:80]}...")
            print()
        print(f"\nDRY RUN Summary:")
        print(f"  Would process: {len(eligible)}")
        print(f"  Skipped (filtered): {skip_count}")
        print(f"  Already chronicled: {already_done}")
        if already_done:
            print(f"\n  View all sessions: codex-chronicle rewind")
        return

    if not eligible:
        print("Nothing to process.")
        print(f"  Skipped: {skip_count}, Already done: {already_done}")
        if failed_skipped:
            print(f"  Terminal failures (use --retry-failed to retry): {failed_skipped}")
        return

    eligible.sort(key=lambda d: d.start_time)

    print(f"Processing {len(eligible)} sessions with {workers} workers...\n")
    semaphore = asyncio.Semaphore(workers)
    tasks = [_process_one(digest, semaphore) for digest in eligible]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    process_count = 0
    error_count = 0

    for digest, result in zip(eligible, results):
        if isinstance(result, BaseException) and not isinstance(result, Exception):
            raise result
        if isinstance(result, Exception):
            print(f"  ERROR {digest.session_id[:8]}: {result}")
            error_count += 1
            continue

        entry = result
        write_chronicle(entry, digest, max_retries=max_retries)
        if entry.is_error:
            print(f"  RETRY-LATER {digest.session_id[:8]}: transient failure")
            error_count += 1
            continue

        process_count += 1

        from .config import project_chronicle_dir
        full_path = project_chronicle_dir(digest.project_slug) / "sessions" / session_filename(entry)
        print(f"  -> vim {full_path}")

    if process_count:
        from .config import project_chronicle_dir
        projects_done = sorted(set(d.project_slug for d in eligible))
        print()
        for slug in projects_done:
            rebuild_prompts_section(slug)
            chronicle_path = project_chronicle_dir(slug) / "chronicle.md"
            if chronicle_path.exists():
                with open(chronicle_path) as f:
                    lines = sum(1 for _ in f)
                print(f"  Chronicle: vim {chronicle_path} ({lines} lines)")

    print(f"\nSummary:")
    print(f"  Processed: {process_count}")
    print(f"  Skipped: {skip_count}")
    print(f"  Already chronicled: {already_done}")
    if failed_skipped:
        print(f"  Terminal failures (use --retry-failed to retry): {failed_skipped}")
    if error_count:
        print(f"  Errors: {error_count}")
    if already_done:
        print(f"\n  View all sessions: codex-chronicle rewind")


def main():
    parser = argparse.ArgumentParser(description="Process existing Codex sessions")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--project", type=str)
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    paused = False
    if is_background_mode() and not args.dry_run:
        paused = pause_service()
        if paused:
            print("Paused background daemon service (will resume after processing).")

    try:
        with processing_lock(blocking=True):
            asyncio.run(async_batch_process(
                project_filter=args.project,
                dry_run=args.dry_run,
                workers=args.workers,
                force=args.force,
                retry_failed=args.retry_failed,
            ))
    except KeyboardInterrupt:
        print("\n\nInterrupted. Already-processed sessions will be skipped on retry.")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if paused:
            resume_service()
            print("Resumed background daemon service.")


if __name__ == "__main__":
    main()
