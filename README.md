# Codex Chronicle

Codex Chronicle records past Codex work and makes it queryable across sessions. It installs a small hook-based companion around the Codex CLI and stores searchable history under `~/.codex-chronicle/`.

## Install

Install from GitHub with:

```bash
curl -fsSL https://raw.githubusercontent.com/Sakib-Sobaha/codex-precisely/main/install.sh | bash
```

This installer:

- clones the repository into `~/.codex-chronicle/src`
- installs `codex-chronicle` and `codex-chronicle-hook` into `~/.local/bin`
- configures Codex hooks in `~/.codex/hooks.json`
- enables `features.codex_hooks = true` in `~/.codex/config.toml`

## Local Development

```bash
cd codex
pip install -e ".[dev]"
codex-chronicle install-hooks
codex-chronicle doctor
```

## Verify

```bash
codex-chronicle doctor
codex-chronicle query timeline
```
