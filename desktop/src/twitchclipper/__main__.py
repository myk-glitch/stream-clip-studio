"""`python -m twitchclipper` launches the desktop app; `python -m twitchclipper <command>` runs the debug CLI."""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in {"doctor", "analyze", "export", "hwtest", "import", "--help", "-h"}:
        from .cli import main as cli_main
        return cli_main(sys.argv[1:])
    from .app import run
    return run()


if __name__ == "__main__":
    sys.exit(main())
