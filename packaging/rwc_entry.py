"""Entry point of the standalone rwc binary (PyInstaller)."""

import sys

from rwc.cli import main

if __name__ == "__main__":
    sys.exit(main())
