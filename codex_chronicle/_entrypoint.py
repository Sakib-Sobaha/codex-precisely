"""Single-binary dispatch entry point for PyInstaller builds.

The frozen binary is shipped as `codex-chronicle` and symlinked to
`codex-chronicle-hook` at install time (busybox pattern). sys.argv[0]
basename picks which command to run.
"""

import os
import sys


def main():
    prog = os.path.basename(sys.argv[0]).lower()
    for suffix in (".exe",):
        if prog.endswith(suffix):
            prog = prog[: -len(suffix)]

    if prog == "codex-chronicle-hook":
        from codex_chronicle.hook import main as hook_main
        raise SystemExit(hook_main())
    from codex_chronicle.__main__ import main as cli_main
    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
