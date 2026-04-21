"""Configure Codex Chronicle hooks in Codex CLI's hooks.json and config.toml.

Called by install.sh. Writes hook entries to ~/.codex/hooks.json (idempotent)
and enables the features.codex_hooks flag in ~/.codex/config.toml.
Also exposes uninstall_hooks() for `codex-chronicle uninstall`.

Codex hook differences vs Claude Code:
- Config file is ~/.codex/hooks.json (not settings.json)
- No `async: true` field — Codex hooks are synchronous/blocking
- Feature flag features.codex_hooks must be enabled in config.toml
- Matcher field uses regex patterns (empty string = match all)
"""

import json
import os
import re
import sys
from pathlib import Path

from .config import codex_hooks_file, codex_config_file

CHRONICLE_HOOKS = {
    "SessionStart": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "codex-chronicle-hook",
                    "statusMessage": "Loading Codex Chronicle context...",
                    "timeout": 10,
                }
            ],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "codex-chronicle-hook",
                    "timeout": 10,
                }
            ],
        }
    ],
    "UserPromptSubmit": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "codex-chronicle-hook",
                    "timeout": 10,
                }
            ],
        }
    ],
}


def _has_chronicle_hook(matcher_group: dict) -> bool:
    for hook in matcher_group.get("hooks", []):
        if hook.get("command") == "codex-chronicle-hook":
            return True
    return False


def _enable_hooks_feature():
    """Add features.codex_hooks = true to ~/.codex/config.toml if missing."""
    cfg_path = codex_config_file()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    content = cfg_path.read_text() if cfg_path.exists() else ""
    lines = content.splitlines()
    feature_line = "codex_hooks = true"
    in_features = False
    features_header_idx: int | None = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            if in_features:
                features_header_idx = idx
            continue

        if not in_features:
            continue

        if re.match(r"^\s*codex_hooks\s*=", line):
            if stripped == feature_line:
                return
            lines[idx] = feature_line
            cfg_path.write_text("\n".join(lines) + "\n")
            print(f"Enabled features.codex_hooks in {cfg_path}")
            return

    if features_header_idx is not None:
        insert_at = features_header_idx + 1
        while insert_at < len(lines):
            stripped = lines[insert_at].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                break
            insert_at += 1
        lines.insert(insert_at, feature_line)
        new_content = "\n".join(lines) + "\n"
    else:
        suffix = "\n\n" if content.strip() else ""
        new_content = content.rstrip() + suffix + "[features]\n" + feature_line + "\n"

    cfg_path.write_text(new_content)
    print(f"Enabled features.codex_hooks in {cfg_path}")


def install_hooks(hooks_path: str | None = None):
    path = Path(hooks_path) if hooks_path else codex_hooks_file()

    if path.exists():
        try:
            raw = path.read_text()
            hooks_data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as e:
            print(
                f"ERROR: {path} is not valid JSON ({e}).\n"
                f"Codex Chronicle will not overwrite it. Fix the file or back it up:\n"
                f"  cp {path} {path}.bak && echo '{{}}' > {path}",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        hooks_data = {}

    if not isinstance(hooks_data, dict):
        print(
            f"ERROR: {path} top-level JSON is not an object. "
            f"Refusing to overwrite.",
            file=sys.stderr,
        )
        sys.exit(2)

    for event_name, chronicle_matchers in CHRONICLE_HOOKS.items():
        existing = hooks_data.get(event_name, [])
        existing = [mg for mg in existing if not _has_chronicle_hook(mg)]
        hooks_data[event_name] = existing + chronicle_matchers

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(hooks_data, indent=2) + "\n")
    print(f"Configured hooks in {path}")

    # Enable the feature flag so hooks actually fire
    try:
        _enable_hooks_feature()
    except Exception as e:
        print(f"WARN: could not enable features.codex_hooks in config.toml: {e}",
              file=sys.stderr)
        print("You can enable it manually by adding to ~/.codex/config.toml:")
        print("  [features]")
        print("  codex_hooks = true")
        print("Or run: codex -c features.codex_hooks=true")


def _is_chronicle_hook_command(cmd) -> bool:
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    first = cmd.strip().split(None, 1)[0]
    return os.path.basename(first) == "codex-chronicle-hook"


def uninstall_hooks(hooks_path: str | None = None, dry_run: bool = False) -> int:
    """Remove codex-chronicle-hook entries from hooks.json.

    Returns the number of entries removed (or would be removed in dry_run).
    """
    path = Path(hooks_path) if hooks_path else codex_hooks_file()
    if not path.exists():
        return 0

    try:
        raw = path.read_text()
        hooks_data = json.loads(raw) if raw.strip() else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN: {path} could not be read or parsed ({e}); leaving it alone.",
              file=sys.stderr)
        return 0

    if not isinstance(hooks_data, dict):
        return 0

    removed = 0
    for event_name in list(hooks_data.keys()):
        matcher_groups = hooks_data[event_name]
        if not isinstance(matcher_groups, list):
            continue
        kept_groups = []
        for mg in matcher_groups:
            if not isinstance(mg, dict):
                kept_groups.append(mg)
                continue
            entries = mg.get("hooks")
            if not isinstance(entries, list):
                kept_groups.append(mg)
                continue
            kept_entries = []
            for h in entries:
                cmd = (h or {}).get("command") if isinstance(h, dict) else None
                if _is_chronicle_hook_command(cmd):
                    removed += 1
                else:
                    kept_entries.append(h)
            if kept_entries:
                new_mg = dict(mg)
                new_mg["hooks"] = kept_entries
                kept_groups.append(new_mg)
        if kept_groups:
            hooks_data[event_name] = kept_groups
        else:
            del hooks_data[event_name]

    if not hooks_data:
        hooks_data_out = {}
    else:
        hooks_data_out = hooks_data

    if removed and not dry_run:
        path.write_text(json.dumps(hooks_data_out, indent=2) + "\n")

    return removed


if __name__ == "__main__":
    install_hooks()
