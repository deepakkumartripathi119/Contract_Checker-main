"""Run the first-start setup, then the real server command."""

from __future__ import annotations

import os
import sys

from scripts.prepare_runtime import prepare


def main() -> None:
    prepare()
    if len(sys.argv) < 2:
        raise SystemExit("No server command was given.")
    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
