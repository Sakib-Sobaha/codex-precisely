"""Parse Codex CLI session JSONL files and extract meaningful content.

Codex sessions are stored at:
  ~/.codex/sessions/YYYY/MM/DD/rollout-<session-id>.jsonl

The JSONL format follows OpenAI API message conventions with additional
Codex-specific event types. This extractor handles:
  - Standard message events (role: user/assistant)
  - Tool call / function call events (shell, patch, browser tools)
  - Tool result / function output events
  - Session metadata (session_id, cwd, timestamps)

Two output formats:
  digest_to_text()  — filtered for LLM context window (80K chars)
  timeline_to_log() — full chronological archival log
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class UserPrompt:
    text: str
    timestamp: str
    uuid: str


@dataclass
class ToolDetail:
    tool: str
    summary: str
    path: str = ""
    command: str = ""
    content: str = ""
    old_content: str = ""
    query: str = ""
    description: str = ""


@dataclass
class TimelineEntry:
    role: str  # "user", "assistant", "tool_result"
    timestamp: str
    text: str
    tool_actions: list[str] = field(default_factory=list)
    tool_details: list[ToolDetail] = field(default_factory=list)
    tool_results: list[str] = field(default_factory=list)


@dataclass
class SessionDigest:
    session_id: str
    project_path: str
    project_slug: str
    start_time: str
    end_time: str
    git_branch: str
    user_prompts: list[UserPrompt] = field(default_factory=list)
    assistant_responses: list[str] = field(default_factory=list)
    tool_actions: list[str] = field(default_factory=list)
    timeline: list[TimelineEntry] = field(default_factory=list)
    total_turns: int = 0


_SECRET_PATTERNS = re.compile(
    r"(?:"
    r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----|"
    r"Authorization:\s*[^\r\n]+|"
    r"Proxy-Authorization:\s*[^\r\n]+|"
    r"Cookie:\s*[^\r\n]+|"
    r"Set-Cookie:\s*[^\r\n]+|"
    r"X-[A-Za-z-]+-(?:Key|Token|Auth|Secret):\s*[^\r\n]+|"
    r"(?:export\s+)?(?:API_KEY|SECRET|TOKEN|PASSWORD|CREDENTIALS|AUTH|PRIVATE_KEY|ACCESS_KEY)"
    r"[_A-Z]*[\s]*[=:]\s*\S+|"
    r"Bearer\s+\S+|"
    r"(?:sk-|pk-|ghp_|gho_|github_pat_|xoxb-|xoxp-|sk_live_|sk_test_|rk_live_|rk_test_|AKIA)\S+|"
    r"(?:mongodb\+srv|postgres(?:ql)?|mysql|redis|amqp)://\S+|"
    r"(?:eyJ[A-Za-z0-9_-]{20,}\.){1,2}[A-Za-z0-9_-]+|"
    r"[?&](?:token|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|"
    r"secret[_-]?key|client[_-]?secret|sig|signature)=[^&\s#]+"
    r")",
    re.IGNORECASE,
)

_SENSITIVE_PATHS = re.compile(
    r"\.env|credentials|secret|\.pem|\.key|id_rsa|id_ed25519|\.aws/|\.docker/config",
    re.IGNORECASE,
)

_MAX_TOOL_RESULT_CHARS = 10000


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    return _SECRET_PATTERNS.sub("[REDACTED]", text)


def _extract_text_content(content) -> str:
    """Extract text from content (string, list of blocks, or other)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type", "")
                if btype == "text":
                    texts.append(block.get("text", ""))
                elif btype == "output_text":
                    texts.append(block.get("text", ""))
        return "\n".join(texts)
    return ""


def _extract_tool_call(entry: dict) -> tuple[str | None, ToolDetail | None]:
    """Extract a one-liner and ToolDetail from a tool call / function call entry."""
    # Codex uses several formats for tool calls:
    # - {"type": "function_call", "name": "...", "arguments": {...}}
    # - {"type": "tool_use", "name": "...", "input": {...}}
    # - {"type": "local_shell_call", "action": {"type": "run", "command": "..."}}

    etype = entry.get("type", "")
    name = entry.get("name", "")
    inp = entry.get("input", entry.get("arguments", {}))
    if isinstance(inp, str):
        try:
            inp = json.loads(inp)
        except (json.JSONDecodeError, ValueError):
            inp = {"raw": inp}

    if etype == "local_shell_call":
        action = entry.get("action", {})
        if isinstance(action, dict):
            cmd = _redact_secrets(action.get("command", action.get("cmd", "")))
            return f"Shell: {cmd[:120]}", ToolDetail(tool="Shell", summary=f"Shell: {cmd[:120]}", command=cmd)

    if name in ("shell", "bash", "run_command", "execute", "computer") or etype in ("function_call",) and not name:
        name = name or "shell"

    if name in ("shell", "bash", "run_command", "execute"):
        cmd = _redact_secrets(str(inp.get("command", inp.get("cmd", inp.get("input", "")))))
        return f"Shell: {cmd[:120]}", ToolDetail(tool="Shell", summary=f"Shell: {cmd[:120]}", command=cmd)

    elif name in ("str_replace_editor", "str_replace_based_edit_tool", "edit_file", "patch"):
        path = inp.get("path", inp.get("file_path", ""))
        old = _redact_secrets(inp.get("old_str", inp.get("old_string", "")))
        new = _redact_secrets(inp.get("new_str", inp.get("new_string", "")))
        return f"Edit: {path}", ToolDetail(tool="Edit", summary=f"Edit: {path}", path=path,
                                           old_content=old, content=new)

    elif name in ("write_file", "create_file"):
        path = inp.get("path", inp.get("file_path", ""))
        content = inp.get("content", "")
        if _SENSITIVE_PATHS.search(path):
            content = f"[REDACTED — sensitive file: {Path(path).name}]"
        else:
            content = _redact_secrets(content)
        return f"Write: {path}", ToolDetail(tool="Write", summary=f"Write: {path}", path=path, content=content)

    elif name in ("read_file", "view_file", "cat"):
        path = inp.get("path", inp.get("file_path", ""))
        return f"Read: {path}", ToolDetail(tool="Read", summary=f"Read: {path}", path=path)

    elif name in ("glob", "find_files"):
        pattern = _redact_secrets(inp.get("pattern", inp.get("glob", "")))
        return f"Glob: {pattern}", ToolDetail(tool="Glob", summary=f"Glob: {pattern}", query=pattern)

    elif name in ("grep", "search_files", "ripgrep"):
        pattern = _redact_secrets(inp.get("pattern", inp.get("query", "")))
        return f"Grep: {pattern}", ToolDetail(tool="Grep", summary=f"Grep: {pattern}", query=pattern)

    elif name in ("web_search", "search"):
        query = _redact_secrets(inp.get("query", ""))
        return f"WebSearch: {query}", ToolDetail(tool="WebSearch", summary=f"WebSearch: {query}", query=query)

    elif name in ("web_fetch", "browse", "computer"):
        url = _redact_secrets(inp.get("url", inp.get("input", "")))
        return f"WebFetch: {url}", ToolDetail(tool="WebFetch", summary=f"WebFetch: {url}", query=url)

    else:
        detail_text = ""
        for key in ("query", "command", "path", "url", "input"):
            if inp.get(key):
                detail_text = f": {_redact_secrets(str(inp[key]))[:80]}"
                break
        tool_name = name or etype or "unknown"
        summary = f"{tool_name}{detail_text}"
        return summary, ToolDetail(tool=tool_name, summary=summary,
                                   content=_redact_secrets(str(inp))[:500])


def _extract_tool_result_text(content) -> str | None:
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if t == "text":
                    parts.append(block.get("text", ""))
                elif t == "output":
                    parts.append(block.get("output", ""))
            elif isinstance(block, str):
                parts.append(block)
        raw = "\n".join(parts)
    else:
        return None

    if not raw or not raw.strip():
        return None

    raw = _redact_secrets(raw)
    if len(raw) > _MAX_TOOL_RESULT_CHARS:
        half = _MAX_TOOL_RESULT_CHARS // 2
        raw = raw[:half] + "\n[... truncated ...]\n" + raw[-half:]
    return raw.strip()


def _is_real_user_prompt(text: str) -> bool:
    """Filter out system-injected content."""
    stripped = text.strip()
    if not stripped:
        return False
    skip_prefixes = (
        "<system>", "<context>", "[System:", "[Context:",
        "<!-- ", "<codex-context>",
    )
    for prefix in skip_prefixes:
        if stripped.startswith(prefix):
            return False
    return True


def _derive_project_info(entry: dict, path: Path) -> tuple[str, str]:
    """Return (cwd, slug) from a JSONL entry or the file path itself."""
    cwd = (entry.get("cwd") or entry.get("workdir") or entry.get("working_directory") or "")
    if cwd:
        slug = cwd.rstrip("/").replace("/", "-")
        return cwd, slug
    # Fallback: use parent dir of session file as a proxy
    return str(path.parent), path.parent.name


def extract_session(jsonl_path: str) -> SessionDigest:
    """Parse a Codex session JSONL and return structured content."""
    path = Path(jsonl_path)
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {jsonl_path}")

    # Session ID from filename: rollout-<id>.jsonl → strip prefix
    stem = path.stem
    session_id = stem.removeprefix("rollout-") if stem.startswith("rollout-") else stem

    digest = SessionDigest(
        session_id=session_id,
        project_path="",
        project_slug="",
        start_time="",
        end_time="",
        git_branch="",
    )

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if not isinstance(entry, dict):
                continue

            etype = entry.get("type", "")
            timestamp = (entry.get("timestamp") or entry.get("created_at") or "")
            if timestamp and not digest.start_time:
                digest.start_time = timestamp
            if timestamp:
                digest.end_time = timestamp

            # Extract project context from metadata entries
            if not digest.project_path:
                cwd, slug = _derive_project_info(entry, path)
                if cwd:
                    digest.project_path = cwd
                    digest.project_slug = slug

            branch = entry.get("gitBranch", entry.get("git_branch", ""))
            if branch and not digest.git_branch:
                digest.git_branch = branch

            sid = entry.get("session_id", entry.get("sessionId", ""))
            if sid:
                digest.session_id = sid

            # Handle different event types
            role = entry.get("role", "")

            if etype in ("message", "chat.completion") or role in ("user", "assistant"):
                content = entry.get("content", "")
                text = _extract_text_content(content)

                if role == "user" or etype == "user":
                    if _is_real_user_prompt(text):
                        clean_text = text.strip()
                        if clean_text:
                            digest.user_prompts.append(UserPrompt(
                                text=clean_text,
                                timestamp=timestamp,
                                uuid=entry.get("id", entry.get("uuid", "")),
                            ))
                            digest.timeline.append(TimelineEntry(
                                role="user",
                                timestamp=timestamp,
                                text=clean_text,
                            ))

                elif role == "assistant" or etype == "assistant":
                    turn_actions = []
                    turn_details = []

                    # Content blocks may include text and tool_use
                    if isinstance(content, list):
                        text_parts = []
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            btype = block.get("type", "")
                            if btype == "text":
                                text_parts.append(block.get("text", ""))
                            elif btype in ("tool_use", "function_call"):
                                summary, detail = _extract_tool_call(block)
                                if summary:
                                    digest.tool_actions.append(summary)
                                    turn_actions.append(summary)
                                if detail:
                                    turn_details.append(detail)
                        full_text = "\n".join(text_parts).strip()
                    else:
                        full_text = text.strip() if text else ""

                    if full_text:
                        digest.assistant_responses.append(full_text)

                    digest.timeline.append(TimelineEntry(
                        role="assistant",
                        timestamp=timestamp,
                        text=full_text,
                        tool_actions=turn_actions,
                        tool_details=turn_details,
                    ))

            elif etype in ("function_call", "tool_use", "local_shell_call"):
                summary, detail = _extract_tool_call(entry)
                if summary:
                    digest.tool_actions.append(summary)
                    digest.timeline.append(TimelineEntry(
                        role="assistant",
                        timestamp=timestamp,
                        text="",
                        tool_actions=[summary],
                        tool_details=[detail] if detail else [],
                    ))

            elif etype in ("function_call_output", "tool_result", "local_shell_call_output"):
                output = (entry.get("output") or entry.get("content") or
                          entry.get("result") or "")
                text = _extract_tool_result_text(output)
                if text:
                    call_id = entry.get("call_id", entry.get("tool_use_id", ""))[:8]
                    result_text = f"[result {call_id}]: {text}" if call_id else text
                    digest.timeline.append(TimelineEntry(
                        role="tool_result",
                        timestamp=timestamp,
                        text="",
                        tool_results=[result_text],
                    ))

    digest.total_turns = len(digest.timeline)

    # Fallback timestamps from file mtime
    if not digest.start_time:
        from datetime import datetime, timezone
        mtime = path.stat().st_mtime
        iso = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest.start_time = iso
        if not digest.end_time:
            digest.end_time = iso

    # Fallback project slug from path date structure: sessions/YYYY/MM/DD/rollout-*.jsonl
    if not digest.project_slug:
        # Use the grandparent path structure as a proxy slug
        digest.project_path = str(path.parent.parent.parent)
        digest.project_slug = "unknown-project"

    return digest


def digest_to_text(digest: SessionDigest, max_chars: int = 80000) -> str:
    """Format a digest as an interleaved timeline for the LLM prompt."""
    parts = []
    parts.append("=== SESSION METADATA ===")
    parts.append(f"session_id: {digest.session_id}")
    parts.append(f"project: {digest.project_path}")
    parts.append(f"branch: {digest.git_branch}")
    parts.append(f"time: {digest.start_time} -> {digest.end_time}")
    parts.append(f"turns: {digest.total_turns}")
    parts.append("")
    parts.append("=== TIMELINE ===")

    timeline_parts = []
    for turn in digest.timeline:
        ts = turn.timestamp[:19] if turn.timestamp else ""

        if turn.role == "user":
            timeline_parts.append(f"\n[{ts}] USER")
            timeline_parts.append(turn.text)

        elif turn.role == "assistant":
            timeline_parts.append(f"\n[{ts}] ASSISTANT")
            if turn.text:
                timeline_parts.append(turn.text)
            if turn.tool_actions:
                timeline_parts.append("TOOLS:")
                prev = None
                count = 0
                for action in turn.tool_actions:
                    if action == prev:
                        count += 1
                    else:
                        if prev is not None:
                            suffix = f" (x{count})" if count > 1 else ""
                            timeline_parts.append(f"  - {prev}{suffix}")
                        prev = action
                        count = 1
                if prev is not None:
                    suffix = f" (x{count})" if count > 1 else ""
                    timeline_parts.append(f"  - {prev}{suffix}")

        elif turn.role == "tool_result":
            if turn.tool_results:
                timeline_parts.append(f"\n[{ts}] TOOL OUTPUT")
                for result in turn.tool_results:
                    timeline_parts.append(result)

    timeline_text = "\n".join(timeline_parts)

    if len(timeline_text) > max_chars:
        front_budget = int(max_chars * 0.75)
        tail_budget = max_chars - front_budget
        front = timeline_text[:front_budget]
        tail = timeline_text[-tail_budget:]
        omitted = len(timeline_text) - max_chars
        timeline_text = (
            front
            + f"\n\n[... {omitted:,} chars from middle of session omitted ...]\n\n"
            + tail
        )

    parts.append(timeline_text)
    return "\n".join(parts)


def timeline_to_log(digest: SessionDigest) -> str:
    """Generate a full chronological log from the timeline."""
    lines = []
    turn_num = 0

    for turn in digest.timeline:
        ts = turn.timestamp[11:19] if turn.timestamp and len(turn.timestamp) > 19 else ""

        if turn.role == "user":
            turn_num += 1
            lines.append(f"\n[{ts}] USER #{turn_num}:")
            for line in turn.text.split("\n"):
                lines.append(f"  {line}")

        elif turn.role == "assistant":
            lines.append(f"\n[{ts}] ASSISTANT:")
            if turn.text:
                for line in turn.text.split("\n"):
                    lines.append(f"  {line}")

            for td in turn.tool_details:
                lines.append("")
                if td.tool == "Edit" and td.path:
                    lines.append(f"  EDIT: {td.path}")
                    if td.old_content:
                        lines.append("    - old:")
                        for dl in td.old_content.split("\n"):
                            lines.append(f"      {dl}")
                    if td.content:
                        lines.append("    + new:")
                        for dl in td.content.split("\n"):
                            lines.append(f"      {dl}")
                elif td.tool == "Write" and td.path:
                    lines.append(f"  WRITE: {td.path}")
                    if td.content:
                        lines.append("    CONTENT:")
                        for dl in td.content.split("\n"):
                            lines.append(f"      {dl}")
                elif td.tool in ("Shell", "Bash"):
                    lines.append(f"  SHELL: {td.command}")
                elif td.tool == "Read":
                    lines.append(f"  READ: {td.path}")
                elif td.tool in ("Glob", "Grep"):
                    lines.append(f"  {td.tool.upper()}: {td.query}")
                elif td.tool in ("WebSearch", "WebFetch"):
                    lines.append(f"  {td.tool.upper()}: {td.query}")
                else:
                    lines.append(f"  {td.summary}")

        elif turn.role == "tool_result":
            for result in turn.tool_results:
                lines.append(f"\n[{ts}] TOOL OUTPUT:")
                for line in result.split("\n"):
                    lines.append(f"  {line}")

    return "\n".join(lines)
