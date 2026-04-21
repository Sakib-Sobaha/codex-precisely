"""Shared storage operations for Codex Chronicle data.

Marker state layout:
  ~/.codex-chronicle/.processed/<hash>       — success
  ~/.codex-chronicle/.failed/<hash>.json     — failure state with retry counter

Hash is sha256(session_id)[:16].
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import (
    failed_dir, processed_dir,
    ensure_dirs, project_chronicle_dir,
)
from .summarizer import entry_to_session_markdown


def _atomic_write(path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(str(tmp), str(path))


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def _ensure_dir(d: Path):
    d.mkdir(parents=True, exist_ok=True)


def is_succeeded(session_id: str) -> bool:
    _ensure_dir(processed_dir())
    return (processed_dir() / session_hash(session_id)).exists()


def mark_succeeded(session_id: str, end_time: str, cost_usd: float = 0.0):
    _ensure_dir(processed_dir())
    h = session_hash(session_id)
    (processed_dir() / h).write_text(f"{session_id}\n{end_time}\n{cost_usd:.4f}\n")
    clear_failed(session_id)


def _failed_path(session_id: str) -> Path:
    return failed_dir() / f"{session_hash(session_id)}.json"


def get_failed(session_id: str) -> dict | None:
    _ensure_dir(failed_dir())
    p = _failed_path(session_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def is_terminal_failure(session_id: str) -> bool:
    rec = get_failed(session_id)
    return bool(rec and rec.get("terminal"))


def get_attempt_count(session_id: str) -> int:
    rec = get_failed(session_id)
    return int(rec.get("attempts", 0)) if rec else 0


def record_failed_attempt(session_id: str, *, error_kind: str,
                          error_message: str, terminal: bool) -> int:
    _ensure_dir(failed_dir())
    rec = get_failed(session_id) or {"session_id": session_id, "attempts": 0}
    rec["attempts"] = int(rec.get("attempts", 0)) + 1
    rec["terminal"] = bool(terminal)
    rec["last_error_kind"] = error_kind
    rec["last_error_message"] = (error_message or "")[:500]
    rec["last_attempt_iso"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_write(_failed_path(session_id), json.dumps(rec, indent=2))
    return rec["attempts"]


def clear_failed(session_id: str):
    p = _failed_path(session_id)
    if p.exists():
        p.unlink()


def list_failed(*, terminal_only: bool = False) -> list[dict]:
    _ensure_dir(failed_dir())
    out = []
    for p in sorted(failed_dir().glob("*.json")):
        try:
            rec = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if terminal_only and not rec.get("terminal"):
            continue
        out.append(rec)
    return out


def slugify(text: str, max_len: int = 40) -> str:
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return slug[:max_len].rstrip("-")


def session_filename(entry) -> str:
    ts = entry.start_time[:16] if entry.start_time else "unknown"
    date_part = ts.replace("T", "_").replace(":", "")
    short_id = entry.session_id[:8]
    title_slug = f"_{slugify(entry.title)}" if entry.title else ""
    return f"{date_part}_{short_id}{title_slug}.md"


def clear_session_markers(session_id: str):
    _ensure_dir(processed_dir())
    _ensure_dir(failed_dir())
    h = session_hash(session_id)
    removed_any = False
    for p in (processed_dir() / h, failed_dir() / f"{h}.json"):
        if p.exists():
            p.unlink()
            removed_any = True
    if removed_any:
        return
    for marker in processed_dir().glob("[0-9a-f]*"):
        if not marker.is_file():
            continue
        try:
            content = marker.read_text()
            if content.startswith(session_id):
                full_id = content.split("\n")[0].strip()
                clear_session_markers(full_id)
                return
        except OSError:
            continue


def delete_session(session_path, slug: str):
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"
    content = session_path.read_text()
    sid_match = re.search(r"\*\*Session\*\*:\s*(\w+)", content)
    short_id = sid_match.group(1) if sid_match else session_path.stem[:8]
    full_id = short_id
    if chronicle_file.exists():
        chronicle = chronicle_file.read_text()
        full_match = re.search(rf"<!-- session:({re.escape(short_id)}[a-f0-9-]*)", chronicle)
        if full_match:
            full_id = full_match.group(1)
        session_marker = f"<!-- session:{full_id}"
        for line in chronicle.split("\n"):
            if session_marker in line:
                session_marker = line.strip()
                break
        if session_marker in chronicle:
            chronicle = _remove_session_entry(chronicle, session_marker)
            _atomic_write(chronicle_file, chronicle)
    session_path.unlink()
    clear_session_markers(full_id)
    if full_id != short_id:
        clear_session_markers(short_id)
    rebuild_prompts_section(slug)


def write_session_record(entry, slug: str):
    ensure_dirs(slug)
    session_dir = project_chronicle_dir(slug) / "sessions"
    short_id = entry.session_id[:8]
    for old in session_dir.glob(f"*_{short_id}*.md"):
        old.unlink()
    session_file = session_dir / session_filename(entry)
    _atomic_write(session_file, entry_to_session_markdown(entry))


def _remove_session_entry(content: str, session_marker: str) -> str:
    marker_idx = content.index(session_marker)
    search_region = content[max(0, marker_idx - 300):marker_idx]
    heading_offset = -1
    for prefix in ("\n# ", "\n## "):
        pos = search_region.rfind(prefix)
        if pos >= 0:
            heading_offset = max(heading_offset, pos)
    if heading_offset >= 0:
        heading_start = max(0, marker_idx - 300) + heading_offset + 1
    else:
        heading_start = marker_idx
    after_marker = marker_idx + len(session_marker)
    next_session = content.find("<!-- session:", after_marker)
    search_bound = next_session if next_session >= 0 else len(content)
    separator = "\n---\n"
    sep_idx = content.rfind(separator, marker_idx, search_bound)
    if sep_idx >= 0:
        section_end = sep_idx + len(separator)
    else:
        section_end = search_bound
    content = content[:heading_start] + content[section_end:]
    sid = session_marker.split(":")[1].split(" ")[0]
    short_id = sid[:8]
    lines = content.split("\n")
    cleaned = [l for l in lines if not (l.startswith("|") and short_id in l and "](sessions/" in l)]
    return "\n".join(cleaned)


def _demote_headings(md: str) -> str:
    lines = md.split("\n")
    result = []
    in_code_block = False
    for line in lines:
        if line.startswith("```"):
            in_code_block = not in_code_block
        if not in_code_block and line.startswith("#"):
            line = "#" + line
        result.append(line)
    return "\n".join(result)


_PROMPTS_MARKER = "<!-- prompts -->"


def rebuild_prompts_section(slug: str):
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"
    if not chronicle_file.exists():
        return
    sessions_dir = project_chronicle_dir(slug) / "sessions"
    if not sessions_dir.exists():
        return

    all_prompts = []
    for md_file in sorted(sessions_dir.glob("*.md")):
        content = md_file.read_text()
        title_match = re.match(r"^# (.+)", content)
        session_title = title_match.group(1) if title_match else md_file.stem
        details_match = re.search(
            r"<details><summary>User prompts \(verbatim\)</summary>\s*\n(.*?)</details>",
            content, re.DOTALL
        )
        if not details_match:
            continue
        prompts_text = details_match.group(1)
        for m in re.finditer(
            r"\*\*Prompt (\d+)\*\* \(([^)]*)\):\s*\n((?:> .+\n?)+)",
            prompts_text
        ):
            num, ts, quoted = m.group(1), m.group(2), m.group(3)
            text = "\n".join(line[2:] for line in quoted.strip().split("\n"))
            all_prompts.append((ts, session_title, int(num), text))

    if not all_prompts:
        content = chronicle_file.read_text()
        if _PROMPTS_MARKER in content:
            marker_idx = content.index(_PROMPTS_MARKER)
            section_start = content.rfind("\n\n", 0, marker_idx)
            if section_start == -1:
                section_start = marker_idx
            _atomic_write(chronicle_file, content[:section_start].rstrip() + "\n")
        return

    all_prompts.sort(key=lambda x: x[0])
    lines = ["", _PROMPTS_MARKER, "", "## All User Prompts (Chronological)", ""]
    current_session = None
    for ts, session_title, num, text in all_prompts:
        if session_title != current_session:
            current_session = session_title
            lines.append(f"### {session_title}")
            lines.append("")
        lines.append(f"**Prompt {num}** ({ts}):")
        for pline in text.split("\n"):
            lines.append(f"> {pline}")
        lines.append("")

    prompts_section = "\n".join(lines)
    content = chronicle_file.read_text()
    if _PROMPTS_MARKER in content:
        marker_idx = content.index(_PROMPTS_MARKER)
        section_start = content.rfind("\n\n", 0, marker_idx)
        if section_start == -1:
            section_start = marker_idx
        content = content[:section_start] + prompts_section
    else:
        content = content.rstrip() + "\n" + prompts_section
    _atomic_write(chronicle_file, content + "\n")


_TIMELINE_HEADER = "| Date | Session | Decisions | Summary |"
_TIMELINE_SEP = "|------|---------|-----------|---------|"
_TIMELINE_END = "<!-- /timeline -->"
_DETAIL_START = "<!-- details -->"


def _timeline_row(entry, sf: str) -> str:
    ts = entry.start_time[:16].replace("T", " ") if entry.start_time else "unknown"
    title = entry.title or f"Session {entry.session_id[:8]}"
    if len(title) > 60:
        title = title[:57] + "..."
    n_decisions = len(entry.decisions) if entry.decisions else 0
    summary = (entry.summary or "")[:100].replace("\n", " ").replace("|", "/")
    if entry.summary and len(entry.summary) > 100:
        summary += "..."
    return f"| {ts} | [{title}](sessions/{sf}) | {n_decisions} | {summary} |"


def append_to_chronicle(entry, slug: str):
    ensure_dirs(slug)
    chronicle_file = project_chronicle_dir(slug) / "chronicle.md"
    short_id = entry.session_id[:8]
    sf = session_filename(entry)
    session_marker = f"<!-- session:{entry.session_id} -->"
    full_md = entry_to_session_markdown(entry)
    full_md = _demote_headings(full_md)
    first_newline = full_md.index("\n")
    full_md = full_md[:first_newline + 1] + session_marker + "\n" + full_md[first_newline + 1:]
    detail_section = full_md + "\n---\n\n"
    table_row = _timeline_row(entry, sf)

    if chronicle_file.exists():
        existing = chronicle_file.read_text()
        if session_marker in existing:
            existing = _remove_session_entry(existing, session_marker)
        if _TIMELINE_END in existing:
            sep_idx = existing.index(_TIMELINE_SEP)
            after_sep = existing.index("\n", sep_idx) + 1
            existing = existing[:after_sep] + table_row + "\n" + existing[after_sep:]
            _atomic_write(chronicle_file, existing + detail_section)
        else:
            _retrofit_timeline(chronicle_file, existing)
            existing = chronicle_file.read_text()
            sep_idx = existing.index(_TIMELINE_SEP)
            after_sep = existing.index("\n", sep_idx) + 1
            existing = existing[:after_sep] + table_row + "\n" + existing[after_sep:]
            _atomic_write(chronicle_file, existing + detail_section)
    else:
        project_name = slug.rsplit("-", 1)[-1] if "-" in slug else slug
        header = f"# Chronicle: {project_name}\n\n"
        timeline = f"{_TIMELINE_HEADER}\n{_TIMELINE_SEP}\n{table_row}\n{_TIMELINE_END}\n\n{_DETAIL_START}\n\n"
        _atomic_write(chronicle_file, header + timeline + detail_section)


def _retrofit_timeline(chronicle_file, existing: str):
    rows = []
    for match in re.finditer(
        r"^## (.+?) \| (.+)\n<!-- session:([a-f0-9-]+) -->",
        existing, re.MULTILINE
    ):
        ts, section_title, session_id = match.group(1), match.group(2), match.group(3)
        start = match.end()
        next_section = re.search(r"^## ", existing[start:], re.MULTILINE)
        section_text = existing[start:start + next_section.start()] if next_section else existing[start:]
        n_decisions = len(re.findall(r"^- \*\*", section_text, re.MULTILINE))
        sf_match = re.search(r"\[sessions/(.+?\.md)\]", section_text)
        sf = sf_match.group(1) if sf_match else ""
        summary_match = re.search(r"\n\n(.+?)(?:\n\n|\Z)", section_text, re.DOTALL)
        summary = ""
        if summary_match:
            summary = summary_match.group(1).strip()[:100].replace("\n", " ").replace("|", "/")
            if len(summary_match.group(1).strip()) > 100:
                summary += "..."
        title = section_title.strip()
        if len(title) > 60:
            title = title[:57] + "..."
        if sf:
            row = f"| {ts} | [{title}](sessions/{sf}) | {n_decisions} | {summary} |"
        else:
            row = f"| {ts} | {title} | {n_decisions} | {summary} |"
        rows.append(row)

    header_end = existing.index("\n", existing.index("# ")) + 1
    header = existing[:header_end]
    body = existing[header_end:].lstrip("\n")
    timeline = f"\n{_TIMELINE_HEADER}\n{_TIMELINE_SEP}\n"
    timeline += "\n".join(rows) + "\n"
    timeline += f"{_TIMELINE_END}\n\n{_DETAIL_START}\n\n"
    _atomic_write(chronicle_file, header + timeline + body)


def write_chronicle(entry, digest, max_retries: int = 3):
    """Write per-session detail file and append to cumulative chronicle."""
    if entry.is_error:
        if entry.error_kind == "infra":
            print(f"[codex-chronicle] infra error for {digest.session_id[:8]} "
                  f"(not counted): {entry.error_message[:150]}")
            return

        current = get_attempt_count(digest.session_id)
        will_be = current + 1
        terminal = will_be >= max_retries
        attempts = record_failed_attempt(
            digest.session_id,
            error_kind=entry.error_kind or "transient",
            error_message=entry.error_message or "(no detail)",
            terminal=terminal,
        )
        if terminal:
            print(f"[codex-chronicle] giving up on {digest.session_id[:8]} "
                  f"after {attempts} failed attempts "
                  f"(kind={entry.error_kind or 'unknown'})")
        else:
            print(f"[codex-chronicle] transient error for {digest.session_id[:8]} "
                  f"(attempt {attempts}/{max_retries}): "
                  f"{entry.error_message[:150]}")
        return

    if entry.is_empty:
        entry.title = entry.title or f"Session {digest.session_id[:8]}"
        entry.summary = entry.summary or "(No meaningful decisions recorded)"

    write_session_record(entry, digest.project_slug)
    append_to_chronicle(entry, digest.project_slug)
    rebuild_prompts_section(digest.project_slug)
    mark_succeeded(digest.session_id, digest.end_time,
                   cost_usd=getattr(entry, "total_cost_usd", 0.0))
