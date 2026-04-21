"""Codex Chronicle — persistent session knowledge tracker for Codex CLI.

Two processing modes:
  foreground  (default) — no daemon, summarize on demand via `codex-chronicle process`.
  background            — daemon auto-summarizes. Enable with `codex-chronicle install-daemon`.

Usage:
    codex-chronicle process [--project NAME] [--workers N] [--force] [--retry-failed] [--dry-run]
        Summarize pending sessions. --retry-failed retries terminal failures
        after the underlying issue has been fixed. --force reprocesses
        already-successful sessions.

    codex-chronicle query projects
        Per-project counts: processed / pending / terminal-failed.
    codex-chronicle query sessions [PATH]
        Show chronicle.md and session files for a project.
    codex-chronicle query timeline [--limit N]
        Recent sessions across all projects, newest first.
    codex-chronicle query search "term"
        Full-text search across all chronicle markdown files.

    codex-chronicle rewind [N] [--since N] [--diff N] [--summary N]
        Navigate session history. View, compare, or summarize sessions.
    codex-chronicle rewind --delete N
        Delete a session record. --prune deletes all 0-decision sessions.

    codex-chronicle insight [project-name]
        Generate an LLM-powered HTML dashboard and open in browser.
    codex-chronicle story [project-name]
        Generate a unified project narrative (story.md) for stakeholders.

    codex-chronicle doctor [--json]
        Diagnose: mode, resolved codex binary, daemon/service status,
        drift warnings, counts. --json emits a schema-versioned document
        (top-level `ok: bool`, `schema_version: 1`) for CI health checks.

    codex-chronicle install-daemon
        Switch to background mode: install & start launchd/systemd service.
    codex-chronicle uninstall-daemon
        Switch to foreground mode: stop & remove launchd/systemd service.

    codex-chronicle daemon [--bg|--stop|--status]
        Internal / manual daemon control. Normal mode switching is
        `install-daemon` / `uninstall-daemon` above — which manages the
        service manager for you.

    codex-chronicle update
        Download the latest release binary, verify SHA256, swap it into
        place, and restart the daemon if it's running.
    codex-chronicle uninstall [--purge] [--yes] [--dry-run]
        Remove codex-chronicle from this machine. Stops/removes the daemon,
        strips codex-chronicle-hook entries from ~/.codex/hooks.json, and
        removes ~/.local/bin/codex-chronicle{,-hook} + ~/.codex-chronicle/runtime/.
        Preserves user data at ~/.codex-chronicle/ (events.jsonl, config.json,
        .processed/, .failed/). Pass --purge to delete that too (prompts
        unless --yes). --dry-run shows the plan without executing.
    codex-chronicle install-hooks [hooks-path]
        Install codex-chronicle hooks into Codex's hooks.json and enable
        the codex_hooks feature flag in ~/.codex/config.toml.
        Defaults to ~/.codex/hooks.json. Called by install.sh; safe to re-run.

    codex-chronicle --version
"""

import sys


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]

    if command in ("--version", "-V"):
        from . import __version__
        print(f"codex-chronicle {__version__}")
        sys.exit(0)

    sys.argv = [f"codex-chronicle.{command}"] + sys.argv[2:]

    if command == "daemon":
        from .daemon import main as daemon_main
        daemon_main()
    elif command in ("process", "batch"):
        from .batch import main as batch_main
        batch_main()
    elif command == "query":
        from .query import main as query_main
        query_main()
    elif command == "rewind":
        from .rewind import main as rewind_main
        rewind_main()
    elif command == "insight":
        from .insight import main as insight_main
        insight_main()
    elif command == "story":
        from .story import main as story_main
        story_main()
    elif command == "install-daemon":
        install_daemon()
    elif command == "uninstall-daemon":
        uninstall_daemon()
    elif command == "doctor":
        from .doctor import run as doctor_run
        sys.exit(doctor_run())
    elif command == "update":
        update_install()
    elif command == "uninstall":
        uninstall_install()
    elif command == "install-hooks":
        from .config import codex_hooks_file
        from .install_hooks import install_hooks
        default_path = str(codex_hooks_file())
        install_hooks(sys.argv[1] if len(sys.argv) >= 2 else default_path)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


def install_daemon():
    from . import service
    from .mode import set_processing_mode

    set_processing_mode("background")
    try:
        accepted = service.install_service()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        set_processing_mode("foreground")
        sys.exit(1)

    if accepted:
        print("Installed background daemon and set processing_mode=background.")
    else:
        print("Service file written, but the service manager did NOT start the daemon cleanly.",
              file=sys.stderr)
        print("processing_mode=background is set; run `codex-chronicle doctor` for details.",
              file=sys.stderr)
    print()
    if sys.platform == "darwin":
        print("macOS launchd service:")
        print(f"  {service._MAC_PLIST_PATH}")
        print("Manage:")
        print("  launchctl print gui/$UID/com.codex-chronicle.daemon")
        print("  launchctl bootout gui/$UID/com.codex-chronicle.daemon")
    elif sys.platform.startswith("linux"):
        print("Linux systemd --user service:")
        print(f"  {service._LINUX_UNIT_PATH}")
        print("Manage:")
        print("  systemctl --user status codex-chronicle-daemon.service")
        print("  journalctl --user -u codex-chronicle-daemon.service -f")
        print()
        print("Note: on Ubuntu 24.04, enable user-service persistence with")
        print("  sudo loginctl enable-linger $USER")
    print()
    print("Verify:  codex-chronicle doctor")


def uninstall_daemon():
    from . import service
    from .mode import set_processing_mode

    try:
        service.uninstall_service()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    set_processing_mode("foreground")
    print("Uninstalled background daemon and set processing_mode=foreground.")
    print()
    print("Verify:  codex-chronicle doctor")
    print("Note: hooks still record session events and inject past titles;")
    print("      run `codex-chronicle process` to summarize on demand.")


def update_install():
    import subprocess
    url = "https://raw.githubusercontent.com/ehzawad/CodexPrecisely/main/codex/install.sh"
    rc = subprocess.call(f"curl -fsSL {url} | bash", shell=True)
    sys.exit(rc)


def uninstall_install():
    import argparse
    import os
    import shutil as _shutil
    from pathlib import Path

    from . import service as _service
    from .config import chronicle_dir, codex_hooks_file
    from .install_hooks import uninstall_hooks
    from .mode import set_processing_mode

    parser = argparse.ArgumentParser(
        prog="codex-chronicle uninstall",
        description="Remove codex-chronicle from this machine.",
    )
    parser.add_argument("--purge", action="store_true",
                        help="Also delete ~/.codex-chronicle/ (events.jsonl, config, logs).")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the --purge confirmation prompt.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be removed, don't remove anything.")
    args = parser.parse_args()

    home_dir = chronicle_dir()
    runtime_dir = home_dir / "runtime"
    bin_dir = Path.home() / ".local" / "bin"
    hooks_file = codex_hooks_file()

    def _symlink_is_owned(link: Path) -> bool:
        if not link.is_symlink():
            return False
        try:
            target = os.readlink(str(link))
        except OSError:
            return False
        probe = Path(target) if os.path.isabs(target) else (link.parent / target)
        try:
            probe_resolved = probe.resolve(strict=False)
        except (OSError, RuntimeError):
            return False
        runtime_resolved = runtime_dir.resolve(strict=False) if runtime_dir.exists() \
                           else runtime_dir
        try:
            probe_resolved.relative_to(runtime_resolved)
            return True
        except ValueError:
            return False

    plan_integration: list[str] = []
    plan_data: list[str] = []
    plan_preserved: list[str] = []
    plan_warn: list[str] = []

    if _service.service_installed():
        sfp = _service.service_file_path()
        plan_integration.append(f"daemon service file: {sfp}")

    hook_entries_to_strip = 0
    if hooks_file.exists():
        try:
            hook_entries_to_strip = uninstall_hooks(str(hooks_file), dry_run=True)
        except Exception as e:
            plan_warn.append(f"could not preview {hooks_file}: {e}")
        if hook_entries_to_strip:
            plan_integration.append(
                f"{hook_entries_to_strip} codex-chronicle-hook entries from {hooks_file}"
            )

    symlinks_to_remove: list[Path] = []
    for name in ("codex-chronicle", "codex-chronicle-hook"):
        link = bin_dir / name
        if not (link.exists() or link.is_symlink()):
            continue
        if _symlink_is_owned(link):
            symlinks_to_remove.append(link)
            plan_integration.append(str(link))
        else:
            try:
                tgt = os.readlink(str(link)) if link.is_symlink() else "(regular file)"
            except OSError:
                tgt = "?"
            plan_warn.append(
                f"{link} is not a codex-chronicle-owned symlink (target: {tgt}); leaving it alone"
            )

    if runtime_dir.exists():
        plan_integration.append(f"{runtime_dir}/")

    if args.purge and home_dir.exists():
        plan_data.append(f"{home_dir}/ (events.jsonl, config, logs, markers)")
    elif plan_integration and home_dir.exists():
        for name in ("events.jsonl", "config.json", ".processed", ".failed",
                     "projects", "daemon.log", "hook-errors.log"):
            p = home_dir / name
            if p.exists():
                plan_preserved.append(str(p))

    nothing_to_do = not plan_integration and not plan_data

    if args.dry_run:
        if nothing_to_do:
            print("codex-chronicle is not installed on this machine. Nothing to do.")
            for item in plan_warn:
                print(f"  ! {item}")
            return
        print("DRY RUN — codex-chronicle uninstall would do the following:\n")
        if plan_integration:
            print("Remove integration:")
            for item in plan_integration:
                print(f"  - {item}")
        if plan_data:
            if plan_integration:
                print()
            print("Purge data (--purge):")
            for item in plan_data:
                print(f"  - {item}")
        if plan_preserved:
            print("\nPreserve (use --purge to delete):")
            for item in plan_preserved:
                print(f"  - {item}")
        if plan_warn:
            print("\nWarnings:")
            for item in plan_warn:
                print(f"  ! {item}")
        return

    if nothing_to_do:
        print("codex-chronicle is not installed on this machine. Nothing to do.")
        for item in plan_warn:
            print(f"WARN: {item}", file=sys.stderr)
        return

    if plan_data and not args.yes:
        print(f"WARNING: --purge will delete ALL codex-chronicle data under {home_dir}.")
        print("This includes events.jsonl, config.json, processed/failed markers,")
        print("per-project chronicles, and logs. This cannot be undone.\n")
        try:
            answer = input("Type 'yes' to confirm: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer != "yes":
            print("Aborted.")
            sys.exit(1)

    integration_done: list[str] = []
    data_done: list[str] = []

    if not args.purge and (home_dir / "config.json").exists():
        try:
            set_processing_mode("foreground")
        except Exception as e:
            print(f"WARN: could not reset processing_mode: {e}", file=sys.stderr)

    if _service.service_installed():
        try:
            _service.uninstall_service()
            integration_done.append("daemon service removed")
        except Exception as e:
            print(f"WARN: service uninstall failed: {e}", file=sys.stderr)

    if hook_entries_to_strip and hooks_file.exists():
        try:
            removed = uninstall_hooks(str(hooks_file), dry_run=False)
            integration_done.append(f"{removed} codex-chronicle-hook entries removed from {hooks_file}")
        except Exception as e:
            print(f"WARN: could not edit {hooks_file}: {e}", file=sys.stderr)

    for link in symlinks_to_remove:
        try:
            link.unlink()
            integration_done.append(f"{link} removed")
        except OSError as e:
            print(f"WARN: could not remove {link}: {e}", file=sys.stderr)

    if runtime_dir.exists():
        _shutil.rmtree(runtime_dir, ignore_errors=True)
        integration_done.append(f"{runtime_dir}/ removed")

    if args.purge and home_dir.exists():
        _shutil.rmtree(home_dir, ignore_errors=True)
        data_done.append(f"{home_dir}/ purged")

    if integration_done:
        print("Uninstalled:")
        for item in integration_done:
            print(f"  - {item}")
    if data_done:
        if integration_done:
            print()
        print("Purged data:")
        for item in data_done:
            print(f"  - {item}")

    if integration_done and not args.purge and home_dir.exists():
        print(f"\nPreserved user data at {home_dir}")
        print(f"To delete it later:  rm -rf {home_dir}")

    if integration_done and data_done:
        print("\ncodex-chronicle has been removed and all data purged.")
    elif integration_done:
        print("\ncodex-chronicle has been removed. Restart Codex so the stripped hooks take effect.")
    elif data_done:
        print("\nLeftover codex-chronicle data purged.")
    else:
        print("\nERROR: planned uninstall steps did not complete. See WARN messages above.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
