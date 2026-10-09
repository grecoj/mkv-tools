#!/usr/bin/env python3
"""
Launcher for the mkv subtitle tool. The implementation lives in the
mkv_subtitle_lib/ folder next to this file (see its __init__.py for a module
index); usage docs are in mkv_subtitle_lib/cli.py. Keep this file, the
mkv_subtitle_lib/ folder, and (optionally) mkv_subtitle_fonts/ together.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mkv_subtitle_lib.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
