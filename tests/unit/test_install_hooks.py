from __future__ import annotations

import json

import pytest


def test_install_hooks_uses_codex_home_by_default(tmp_path, monkeypatch):
    from codex_chronicle.install_hooks import install_hooks

    codex_home = tmp_path / "custom-codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    install_hooks()

    hooks_file = codex_home / "hooks.json"
    config_file = codex_home / "config.toml"
    assert hooks_file.exists()
    assert config_file.exists()

    hooks = json.loads(hooks_file.read_text())
    assert "SessionStart" in hooks
    assert "Stop" in hooks
    assert "UserPromptSubmit" in hooks
    assert "codex_hooks = true" in config_file.read_text()


def test_install_hooks_preserves_existing_entries_and_is_idempotent(tmp_path):
    from codex_chronicle.install_hooks import install_hooks

    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text(json.dumps({
        "CustomEvent": [{"matcher": "x", "hooks": [{"command": "echo ok"}]}],
        "SessionStart": [{"matcher": "y", "hooks": [{"command": "echo keep"}]}],
    }))

    install_hooks(str(hooks_file))
    install_hooks(str(hooks_file))

    hooks = json.loads(hooks_file.read_text())
    assert "CustomEvent" in hooks

    chronicle_count = sum(
        1
        for group in hooks["SessionStart"]
        for hook in group.get("hooks", [])
        if hook.get("command") == "codex-chronicle-hook"
    )
    assert chronicle_count == 1


def test_install_hooks_refuses_invalid_json(tmp_path, capsys):
    from codex_chronicle.install_hooks import install_hooks

    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text("{ not json }")

    with pytest.raises(SystemExit) as excinfo:
        install_hooks(str(hooks_file))

    assert excinfo.value.code == 2
    assert "not valid JSON" in capsys.readouterr().err
    assert hooks_file.read_text() == "{ not json }"


def test_enable_hooks_feature_updates_existing_false_setting(tmp_path, monkeypatch):
    from codex_chronicle.install_hooks import _enable_hooks_feature

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_file = codex_home / "config.toml"
    config_file.write_text(
        '[profiles.default]\nmodel = "o3"\n\n[features]\ncodex_hooks = false\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    _enable_hooks_feature()
    _enable_hooks_feature()

    content = config_file.read_text()
    assert "codex_hooks = true" in content
    assert "codex_hooks = false" not in content
    assert content.count("codex_hooks = true") == 1


def test_enable_hooks_feature_inserts_into_existing_features_section(tmp_path, monkeypatch):
    from codex_chronicle.install_hooks import _enable_hooks_feature

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config_file = codex_home / "config.toml"
    config_file.write_text('[features]\nexperimental = true\n\n[profiles.default]\nmodel = "o3"\n')
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    _enable_hooks_feature()

    lines = config_file.read_text().splitlines()
    assert lines[:3] == ["[features]", "experimental = true", "codex_hooks = true"]
