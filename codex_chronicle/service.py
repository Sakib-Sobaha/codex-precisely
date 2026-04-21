"""Platform service-manager integration (launchd + systemd-user) for Codex Chronicle."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .codex_cli import try_resolve_codex_binary

_MAC_LABEL = "com.codex-chronicle.daemon"
_LINUX_UNIT = "codex-chronicle-daemon.service"

_MAC_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_MAC_LABEL}.plist"
_LINUX_UNIT_PATH = Path.home() / ".config" / "systemd" / "user" / _LINUX_UNIT


def _standard_path() -> str:
    parts = [
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p and p not in parts:
            parts.append(p)
    return os.pathsep.join(parts)


def _chronicle_binary() -> str:
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    found = shutil.which("codex-chronicle")
    if found:
        return found
    return str(Path.home() / ".local" / "bin" / "codex-chronicle")


def _mac_plist_contents() -> str:
    chronicle_bin = _chronicle_binary()
    home = str(Path.home())
    path_val = _standard_path()
    codex = try_resolve_codex_binary()
    codex_hint = f"    <!-- resolved codex at install: {codex} -->\n" if codex else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
{codex_hint}    <key>Label</key>
    <string>{_MAC_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{chronicle_bin}</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{home}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{path_val}</string>
        <key>HOME</key>
        <string>{home}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{home}/.codex-chronicle/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{home}/.codex-chronicle/daemon.log</string>
</dict>
</plist>
"""


def _linux_unit_contents() -> str:
    chronicle_bin = _chronicle_binary()
    path_val = _standard_path()
    return f"""[Unit]
Description=Codex Chronicle Daemon
After=default.target

[Service]
Type=simple
WorkingDirectory=%h
Environment="PATH={path_val}"
ExecStart={chronicle_bin} daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""


def _mac_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _mac_bootout() -> None:
    uid = os.getuid()
    _mac_run(["launchctl", "bootout", f"gui/{uid}/{_MAC_LABEL}"])


def _mac_bootstrap() -> bool:
    uid = os.getuid()
    res = _mac_run(["launchctl", "bootstrap", f"gui/{uid}", str(_MAC_PLIST_PATH)])
    return res.returncode == 0


def _mac_is_loaded() -> bool:
    res = _mac_run(["launchctl", "print", f"gui/{os.getuid()}/{_MAC_LABEL}"])
    return res.returncode == 0


def _mac_install() -> bool:
    _MAC_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MAC_PLIST_PATH.write_text(_mac_plist_contents())
    _mac_bootout()
    return _mac_bootstrap()


def _mac_uninstall() -> None:
    _mac_bootout()
    if _MAC_PLIST_PATH.exists():
        _MAC_PLIST_PATH.unlink()


def _linux_run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


def _linux_is_active() -> bool:
    res = _linux_run(["systemctl", "--user", "is-active", _LINUX_UNIT])
    return res.returncode == 0


def _linux_install() -> bool:
    _LINUX_UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LINUX_UNIT_PATH.write_text(_linux_unit_contents())
    _linux_run(["systemctl", "--user", "daemon-reload"])
    res = _linux_run(["systemctl", "--user", "enable", "--now", _LINUX_UNIT])
    return res.returncode == 0


def _linux_uninstall() -> None:
    _linux_run(["systemctl", "--user", "disable", "--now", _LINUX_UNIT])
    if _LINUX_UNIT_PATH.exists():
        _LINUX_UNIT_PATH.unlink()
    _linux_run(["systemctl", "--user", "daemon-reload"])


def platform_key() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def install_service() -> bool:
    p = platform_key()
    if p == "macos":
        return _mac_install()
    if p == "linux":
        return _linux_install()
    raise RuntimeError(
        f"Unsupported platform {sys.platform}; run `codex-chronicle daemon` manually."
    )


def uninstall_service() -> None:
    p = platform_key()
    if p == "macos":
        _mac_uninstall()
    elif p == "linux":
        _linux_uninstall()


def service_installed() -> bool:
    p = platform_key()
    if p == "macos":
        return _MAC_PLIST_PATH.exists()
    if p == "linux":
        return _LINUX_UNIT_PATH.exists()
    return False


def service_running() -> bool:
    p = platform_key()
    if p == "macos":
        if not shutil.which("launchctl"):
            return False
        return _mac_is_loaded()
    if p == "linux":
        if not shutil.which("systemctl"):
            return False
        return _linux_is_active()
    return False


def service_file_path() -> Optional[Path]:
    p = platform_key()
    if p == "macos":
        return _MAC_PLIST_PATH
    if p == "linux":
        return _LINUX_UNIT_PATH
    return None


def pause_service() -> bool:
    p = platform_key()
    if p == "macos":
        if not shutil.which("launchctl"):
            return False
        was_running = _mac_is_loaded()
        _mac_bootout()
        return was_running
    if p == "linux":
        if not shutil.which("systemctl"):
            return False
        was_active = _linux_is_active()
        if was_active:
            _linux_run(["systemctl", "--user", "stop", _LINUX_UNIT])
        return was_active
    return False


def resume_service() -> None:
    p = platform_key()
    if p == "macos":
        if _MAC_PLIST_PATH.exists() and shutil.which("launchctl"):
            _mac_bootstrap()
    elif p == "linux":
        if _LINUX_UNIT_PATH.exists() and shutil.which("systemctl"):
            _linux_run(["systemctl", "--user", "start", _LINUX_UNIT])


def mode_drift_warnings() -> list[str]:
    from .mode import get_processing_mode
    warnings: list[str] = []
    mode = get_processing_mode()
    installed = service_installed()
    running = service_running()

    if mode == "foreground" and (installed or running):
        bits = []
        if installed:
            bits.append("service file present")
        if running:
            bits.append("daemon running")
        warnings.append(
            f"Mode=foreground but {', '.join(bits)} — "
            "run `codex-chronicle uninstall-daemon` to fix."
        )
    elif mode == "background" and not installed:
        warnings.append(
            "Mode=background but service file missing — "
            "run `codex-chronicle install-daemon` to fix."
        )
    elif mode == "background" and installed and not running:
        warnings.append(
            "Mode=background and service file present, but daemon not running. "
            "Check daemon.log; re-run `codex-chronicle install-daemon` to reinstall."
        )
    return warnings
