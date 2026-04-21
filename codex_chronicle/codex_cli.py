"""Central Codex CLI subprocess management.

Analogous to chronicle's claude_cli.py, but for `codex exec` instead of
`claude -p`. Handles:

1. PATH resolution — finds the `codex` binary even when the daemon runs
   under launchd's minimal PATH.
2. Subprocess env — strips OPENAI_API_KEY / OPENAI_BASE_URL so subscription
   routing wins over API-key routing.
3. Error classification — INFRA / TRANSIENT / PARSE.
4. Schema output — writes JSON schema to a temp file and passes it via
   `--output-schema`. Cleans up the temp file after the call.
5. Subprocess registry for graceful daemon shutdown.

`codex exec` invocation pattern:
    codex exec --ephemeral --full-auto --skip-git-repo-check \
               [--output-schema <schema_path>]
    (prompt piped on stdin)

Without --json flag: stdout is the final plain-text / JSON response.
With --output-schema: the response is a JSON object matching the schema.
"""

import asyncio
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional


_STRIP_ENV_VARS = frozenset({
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_ORG_ID",
})


def _fallback_bin_dirs() -> list[Path]:
    return [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ]


def _standard_path_dirs() -> list[Path]:
    return [
        Path.home() / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]


class ErrorKind(Enum):
    INFRA = "infra"
    TRANSIENT = "transient"
    PARSE = "parse"


class CodexNotFound(RuntimeError):
    """Raised when the codex binary cannot be resolved."""


@dataclass
class CodexResult:
    stdout_text: str = ""
    stdout_json: Optional[dict] = None
    total_cost_usd: float = 0.0
    error_kind: Optional[ErrorKind] = None
    error_message: str = ""

    @property
    def ok(self) -> bool:
        return self.error_kind is None


_cached_codex_path: Optional[Path] = None


def resolve_codex_binary(force_refresh: bool = False) -> Path:
    """Return the absolute path to `codex`. Raises CodexNotFound if absent."""
    global _cached_codex_path
    if _cached_codex_path is not None and not force_refresh:
        if _cached_codex_path.exists():
            return _cached_codex_path
        _cached_codex_path = None

    hit = shutil.which("codex")
    if hit:
        _cached_codex_path = Path(hit).resolve()
        return _cached_codex_path

    for d in _fallback_bin_dirs():
        candidate = d / "codex"
        if candidate.exists() and os.access(candidate, os.X_OK):
            _cached_codex_path = candidate.resolve()
            return _cached_codex_path

    searched = [os.environ.get("PATH", "")] + [str(d) for d in _fallback_bin_dirs()]
    raise CodexNotFound(
        "Could not find `codex` binary. Searched: "
        + " | ".join(searched)
        + ". Install Codex CLI or ensure it is on the daemon's PATH "
        "(see `codex-chronicle doctor`)."
    )


def try_resolve_codex_binary() -> Optional[Path]:
    try:
        return resolve_codex_binary()
    except CodexNotFound:
        return None


def build_subprocess_env(base: Optional[dict] = None) -> dict:
    src = base if base is not None else os.environ
    env = {k: v for k, v in src.items() if k not in _STRIP_ENV_VARS}
    existing = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    extra = [str(d) for d in _standard_path_dirs() if d.exists()]
    seen: set[str] = set()
    merged: list[str] = []
    for p in extra + existing:
        if p and p not in seen:
            seen.add(p)
            merged.append(p)
    env["PATH"] = os.pathsep.join(merged)
    return env


_active_procs: "set[asyncio.subprocess.Process]" = set()


def _register(proc: "asyncio.subprocess.Process") -> None:
    _active_procs.add(proc)


def _unregister(proc: "asyncio.subprocess.Process") -> None:
    _active_procs.discard(proc)


async def terminate_active_subprocesses(grace_seconds: float = 5.0) -> dict:
    if not _active_procs:
        return {"terminated": 0, "killed": 0}
    victims = list(_active_procs)
    for p in victims:
        try:
            p.terminate()
        except ProcessLookupError:
            pass
    await asyncio.sleep(grace_seconds)
    killed = 0
    for p in victims:
        if p.returncode is None:
            try:
                p.kill()
                killed += 1
            except ProcessLookupError:
                pass
    for p in victims:
        try:
            await asyncio.wait_for(p.wait(), timeout=2.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
    return {"terminated": len(victims), "killed": killed}


def active_subprocess_count() -> int:
    return len(_active_procs)


def _parse_codex_output(stdout: str, used_schema: bool) -> tuple[Optional[dict], float, Optional[str]]:
    """Parse codex exec stdout.

    Returns (parsed_dict_or_None, cost_usd, error_text_or_None).

    Codex exec output formats:
    1. With --output-schema: stdout is a JSON object matching the schema.
    2. Plain text: stdout is free-form text (we try to parse as JSON anyway).
    3. JSONL streaming (--json flag): each line is an event; we find the last
       assistant message.
    """
    stdout = stdout.strip()
    if not stdout:
        return None, 0.0, "empty output"

    # Try direct JSON parse first (--output-schema case)
    try:
        data = json.loads(stdout)
        if isinstance(data, dict):
            cost = data.pop("_cost_usd", 0.0)
            return data, cost, None
    except (json.JSONDecodeError, ValueError):
        pass

    # Try JSONL streaming format: scan events for last assistant message
    lines = stdout.splitlines()
    last_message = None
    total_cost = 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            etype = event.get("type", "")
            # Extract cost from various event types
            if "cost" in event:
                c = event["cost"]
                if isinstance(c, (int, float)):
                    total_cost = float(c)
                elif isinstance(c, dict):
                    total_cost = float(c.get("total_usd", c.get("total", 0)) or 0)
            if etype in ("message", "response") and event.get("role") == "assistant":
                content = event.get("content", "")
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            texts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            texts.append(block)
                    last_message = "\n".join(texts)
                elif isinstance(content, str):
                    last_message = content
            elif etype == "text" or etype == "output_text":
                text = event.get("text", event.get("output", ""))
                if text:
                    last_message = text
        except (json.JSONDecodeError, ValueError):
            continue

    if last_message:
        last_message = last_message.strip()
        try:
            parsed = json.loads(last_message)
            if isinstance(parsed, dict):
                return parsed, total_cost, None
        except (json.JSONDecodeError, ValueError):
            pass
        # Return as raw text in a wrapper dict for non-JSON responses
        return {"result": last_message}, total_cost, None

    # Last resort: treat entire stdout as the result
    return {"result": stdout}, 0.0, None


async def spawn_codex(
    prompt: str,
    *,
    model: str,
    fallback_model: str,
    json_schema: Optional[dict] = None,
    extra_flags: Iterable[str] = (),
    timeout: float = 300.0,
) -> CodexResult:
    """Invoke `codex exec` and return a classified result.

    Never raises for expected failure paths; returns a CodexResult with
    error_kind set.
    """
    try:
        codex_bin = resolve_codex_binary()
    except CodexNotFound as e:
        return CodexResult(error_kind=ErrorKind.INFRA, error_message=str(e))

    schema_path: Optional[str] = None
    schema_tmp: Optional[str] = None

    try:
        args = [
            str(codex_bin), "exec",
            "--model", model,
            "--ephemeral",
            "--full-auto",
            "--skip-git-repo-check",
        ]

        if json_schema is not None:
            # Write schema to temp file — codex exec --output-schema takes a path
            fd, schema_tmp = tempfile.mkstemp(suffix=".json", prefix="codex_chronicle_")
            with os.fdopen(fd, "w") as f:
                json.dump(json_schema, f)
            schema_path = schema_tmp
            args += ["--output-schema", schema_path]

        args += list(extra_flags)

        env = build_subprocess_env()

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as e:
            return CodexResult(
                error_kind=ErrorKind.INFRA,
                error_message=f"codex binary vanished before spawn: {e}",
            )
        except PermissionError as e:
            return CodexResult(
                error_kind=ErrorKind.INFRA,
                error_message=f"permission denied spawning codex: {e}",
            )

        _register(proc)
        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(prompt.encode()), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.communicate()
                except Exception:
                    pass
                return CodexResult(
                    error_kind=ErrorKind.TRANSIENT,
                    error_message=f"codex exec timed out after {timeout}s",
                )
        finally:
            _unregister(proc)

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")

        if proc.returncode != 0:
            msg = stderr[:300] or stdout[:300] or f"exit {proc.returncode}"
            combined = (stderr + " " + stdout).lower()
            infra_hints = (
                "command not found", "no such file",
                "not authenticated", "authentication required",
                "unauthorized", "please run", "please log in",
                "api key", "openai_api_key",
            )
            if any(h in combined for h in infra_hints):
                return CodexResult(error_kind=ErrorKind.INFRA, error_message=msg)
            return CodexResult(error_kind=ErrorKind.TRANSIENT, error_message=msg)

        parsed, cost, err = _parse_codex_output(stdout, used_schema=json_schema is not None)

        if err and parsed is None:
            return CodexResult(
                error_kind=ErrorKind.PARSE,
                error_message=f"output parse failed: {err}: {stdout[:200]}",
            )

        return CodexResult(stdout_text=stdout, stdout_json=parsed, total_cost_usd=cost)

    finally:
        if schema_tmp and os.path.exists(schema_tmp):
            try:
                os.unlink(schema_tmp)
            except OSError:
                pass


def _reset_cache_for_tests() -> None:
    global _cached_codex_path
    _cached_codex_path = None
    _active_procs.clear()
