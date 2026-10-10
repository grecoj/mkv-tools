r"""
mkv_subtitle_tool.py
=====================

Bulk-fix subtitles (and fonts) across a folder of .mkv files using MKVToolNix
(mkvmerge / mkvextract / mkvpropedit), which must already be installed and on your PATH
(https://mkvtoolnix.download/).

Typical order:
    extract -> plan-replace -> merge-replace     text replacement, per show
    extract -> trim | reposition | style-sync    alternatives to the replace pair; each rebuilds the mkvs
    check-fonts, dump-fonts, add-fonts           font tools; work straight from the mkvs, no 'extract' needed

Each stage defines its own options and documentation in its own module (see __init__.py for the
module index). `python mkv_subtitle_tool.py --help` lists the stages, and
`python mkv_subtitle_tool.py <stage> --help` shows that stage's full description and options.

Shared defaults: -i/--input-dir is the current directory, -w/--work-dir is ./work, and -o/--output-dir
(for the stages that rebuild mkvs) is ./out. All can be overridden explicitly.
"""

from __future__ import annotations
import argparse
from . import extract, fonts, replace, reposition, style_sync, trim
from .common import set_color_enabled

# Every stage module defines register(sub), which adds its subparser(s) and flags.
# Adding a stage means creating its module and listing it here; order is the order shown in --help.
STAGES = (extract, replace, trim, reposition, style_sync, fonts)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bulk-edit subtitle tracks and fonts across a folder of MKV files "
                    "(text replacement, trim, reposition, style sync, font checks).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-color", action="store_true",
                        help="Disable colored console output (auto-disabled for non-terminal output too).")
    sub = parser.add_subparsers(dest="stage", required=True)
    for stage in STAGES:
        stage.register(sub)
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if getattr(args, "no_color", False):
        set_color_enabled(False)
    args.func(args)


if __name__ == "__main__":
    main()
