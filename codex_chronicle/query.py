"""Search and browse the Codex Chronicle.

Usage:
    codex-chronicle query projects
    codex-chronicle query sessions
    codex-chronicle query timeline [--limit N]
    codex-chronicle query search "term"
"""

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

from .config import projects_dir


def search(query: str, project: str | None = None):
    if not projects_dir().exists():
        print("No chronicles found. Run `codex-chronicle process` "
              "or enable background mode with `codex-chronicle install-daemon`.")
        return

    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []

    for md_file in sorted(projects_dir().rglob("*.md")):
        if project and project not in str(md_file):
            continue
        content = md_file.read_text()
        matches = list(pattern.finditer(content))
        if not matches:
            continue
        for match in matches:
            start = max(0, match.start() - 100)
            end = min(len(content), match.end() + 100)
            context = content[start:end].replace("\n", " ").strip()
            context = context.replace(match.group(), f"**{match.group()}**")
            results.append((md_file, context))

    if not results:
        print(f"No results for '{query}'")
        return

    print(f"Found {len(results)} match(es) for '{query}':\n")
    current_file = None
    for filepath, context in results:
        if filepath != current_file:
            rel = filepath.relative_to(projects_dir())
            print(f"--- {rel} ---")
            current_file = filepath
        print(f"  ...{context}...")
        print()


def timeline(limit: int = 20, project: str | None = None):
    if not projects_dir().exists():
        print("No chronicles found.")
        return

    sessions = []
    for session_file in projects_dir().rglob("sessions/*.md"):
        if project and project not in str(session_file):
            continue
        content = session_file.read_text()
        date_match = re.search(r"\*\*Date\*\*:\s*([^|\n]+)", content)
        date_str = date_match.group(1).strip() if date_match else "0000"
        title_match = re.search(r"^# (.+)", content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else session_file.stem[:8]
        project_slug = session_file.parent.parent.name
        sessions.append((date_str, project_slug, title, session_file, content))

    sessions.sort(key=lambda x: x[0], reverse=True)

    if not sessions:
        print("No session records found.")
        return

    print(f"Recent sessions (showing {min(limit, len(sessions))} of {len(sessions)}):\n")
    for date_str, proj, title, filepath, content in sessions[:limit]:
        decision_count = len(re.findall(r"^### ", content, re.MULTILINE))
        summary_match = re.search(r"## Summary\n\n(.+?)(?:\n\n|\Z)", content, re.DOTALL)
        if not summary_match:
            summary_match = re.search(r"## What happened\n\n(.+?)(?:\n\n|\Z)", content, re.DOTALL)
        summary = summary_match.group(1).strip()[:150] if summary_match else ""

        print(f"  [{date_str}] {proj}")
        print(f"    Session {title}")
        if summary:
            print(f"    {summary}")
        if decision_count:
            print(f"    ({decision_count} decisions)")
        print()


def sessions(project_path: str | None = None):
    cwd = project_path or os.environ.get("CODEX_CHRONICLE_ORIGINAL_CWD", os.getcwd())
    cwd = cwd.rstrip("/")
    slug = cwd.replace("/", "-")
    project_dir = projects_dir() / slug

    if not project_dir.exists() and projects_dir().exists() and project_path:
        matches = [d for d in sorted(projects_dir().iterdir())
                    if d.is_dir() and project_path in d.name]
        if matches:
            project_dir = matches[0]
            slug = project_dir.name
            cwd = project_path

    chronicle_file = project_dir / "chronicle.md"
    sessions_dir = project_dir / "sessions"

    if not project_dir.exists():
        from .config import codex_sessions_dir
        codex_root = codex_sessions_dir()
        if codex_root.exists():
            # Try to find sessions matching this project
            from .batch import _build_sid_cwd_map
            sid_cwd = _build_sid_cwd_map()
            count = sum(1 for c in sid_cwd.values() if c.rstrip("/") == cwd)
            if count:
                from .mode import is_background_mode
                from .daemon import _is_running
                running, pid = _is_running()
                bg = is_background_mode()
                print(f"Not yet processed. {count} session(s) found for '{cwd}'")
                if bg and running:
                    print(f"Daemon is running (pid {pid}) — will process after "
                          f"5 minutes of inactivity.")
                else:
                    print("Mode=foreground — run: codex-chronicle process --workers 5")
                return
        print(f"No sessions found for '{cwd}'")
        return

    session_count = len(list(sessions_dir.glob("*.md"))) if sessions_dir.exists() else 0

    if chronicle_file.exists():
        print(f"Chronicle for {cwd} ({session_count} sessions):\n")
        print(f"  vim {chronicle_file}")
        print()
        if sessions_dir.exists():
            print(f"Detailed per-session files:")
            for md_file in sorted(sessions_dir.glob("*.md"), reverse=True):
                with open(md_file, errors="ignore") as f:
                    first_line = f.readline().rstrip("\n")
                title = first_line[2:] if first_line.startswith("# ") else md_file.stem
                print(f"  {title}")
                print(f"    vim {md_file}")
            print()
    elif session_count > 0:
        print(f"Chronicles for {cwd} ({session_count} sessions):\n")
        for md_file in sorted(sessions_dir.glob("*.md"), reverse=True):
            with open(md_file, errors="ignore") as f:
                first_line = f.readline().rstrip("\n")
            title = first_line[2:] if first_line.startswith("# ") else md_file.stem
            print(f"  {title}")
            print(f"    vim {md_file}")
        print()
    else:
        print(f"No chronicles for {cwd}")


def list_projects():
    from .storage import is_succeeded, is_terminal_failure
    from .config import codex_sessions_dir
    from .batch import _build_sid_cwd_map, find_all_sessions

    slugs: set[str] = set()
    if projects_dir().exists():
        for d in projects_dir().iterdir():
            if d.is_dir():
                slugs.add(d.name)

    # Add slugs from codex session files (via events.jsonl cwd mapping)
    sid_cwd = _build_sid_cwd_map()
    for cwd in sid_cwd.values():
        slug = cwd.rstrip("/").replace("/", "-")
        if slug:
            slugs.add(slug)

    if not slugs:
        print("No chronicles and no sessions found.")
        print("Start a Codex session first, then run: codex-chronicle process --workers 5")
        return

    # Build per-slug session counts
    all_sessions = find_all_sessions()
    slug_sessions: dict[str, list] = {}
    for proj_slug, jsonl_path in all_sessions:
        stem = jsonl_path.stem
        session_id = stem.removeprefix("rollout-") if stem.startswith("rollout-") else stem
        slug_sessions.setdefault(proj_slug, []).append(session_id)

    totals = {"processed": 0, "pending": 0, "failed": 0}
    rows: list[tuple[str, int, int, int]] = []

    for slug in sorted(slugs):
        processed = 0
        pending = 0
        failed = 0
        for sid in slug_sessions.get(slug, []):
            if is_succeeded(sid):
                processed += 1
            elif is_terminal_failure(sid):
                failed += 1
            else:
                pending += 1

        totals["processed"] += processed
        totals["pending"] += pending
        totals["failed"] += failed
        rows.append((slug, processed, pending, failed))

    print(f"  {'Project':50}  {'OK':>4}  {'Pend':>4}  {'Fail':>4}")
    print(f"  {'─' * 50}  {'─' * 4}  {'─' * 4}  {'─' * 4}")
    for slug, p, pe, f in rows:
        if p + pe + f == 0:
            continue
        short = slug if len(slug) <= 50 else slug[:47] + "..."
        print(f"  {short:50}  {p:>4}  {pe:>4}  {f:>4}")
    print(f"  {'─' * 50}  {'─' * 4}  {'─' * 4}  {'─' * 4}")
    print(f"  {'Total':50}  {totals['processed']:>4}  "
          f"{totals['pending']:>4}  {totals['failed']:>4}")

    if totals["pending"]:
        print(f"\n  Process pending:    codex-chronicle process --workers 5")
    if totals["failed"]:
        print(f"  Retry failed:       codex-chronicle process --retry-failed --workers 5")


def show_project(name: str):
    if not projects_dir().exists():
        print(f"No chronicles found for '{name}'.")
        return

    matches = []
    for project_dir in sorted(projects_dir().iterdir()):
        if not project_dir.is_dir():
            continue
        if name in project_dir.name:
            matches.append(project_dir)

    if not matches:
        print(f"No chronicles found matching '{name}'.")
        return

    for project_dir in matches:
        sessions_dir = project_dir / "sessions"
        chronicle_file = project_dir / "chronicle.md"
        session_count = len(list(sessions_dir.glob("*.md"))) if sessions_dir.exists() else 0

        print(f"Project: {project_dir.name} ({session_count} sessions)")
        if chronicle_file.exists():
            print(f"  Chronicle: vim {chronicle_file}\n")

        if sessions_dir.exists():
            for md_file in sorted(sessions_dir.glob("*.md"), reverse=True):
                with open(md_file, errors="ignore") as f:
                    first_line = f.readline().rstrip("\n")
                title = first_line[2:] if first_line.startswith("# ") else md_file.stem
                print(f"  {title}")
                print(f"    vim {md_file}")
            print()


def main():
    parser = argparse.ArgumentParser(description="Query the Codex Chronicle")
    subparsers = parser.add_subparsers(dest="command")

    search_p = subparsers.add_parser("search", help="Full-text search")
    search_p.add_argument("query")
    search_p.add_argument("--project")

    timeline_p = subparsers.add_parser("timeline", help="Recent decisions")
    timeline_p.add_argument("--limit", type=int, default=20)
    timeline_p.add_argument("--project")

    sessions_p = subparsers.add_parser("sessions", help="Sessions for current project")
    sessions_p.add_argument("path", nargs="?")

    subparsers.add_parser("projects", help="List chronicled projects")

    known = {"search", "timeline", "sessions", "projects", "-h", "--help"}
    if len(sys.argv) > 1 and sys.argv[1] not in known:
        show_project(sys.argv[1])
        return

    args = parser.parse_args()

    if args.command == "search":
        search(args.query, args.project)
    elif args.command == "timeline":
        timeline(args.limit, getattr(args, "project", None))
    elif args.command == "sessions":
        sessions(getattr(args, "path", None))
    elif args.command == "projects":
        list_projects()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
