r"""
mkv_subtitle_tool.py
=====================

A pipeline for bulk-fixing subtitle text (and, optionally, trimming
subtitle tracks that run longer than the video) across a folder of .mkv
files using MKVToolNix (mkvmerge / mkvextract), which must already be
installed and on your PATH (https://mkvtoolnix.download/).

STAGE 1 - extract
    Pulls every subtitle track out of every .mkv in a folder, preserving
    each track's metadata (language, name, default/forced flags) in a
    sidecar JSON file so it can be restored later.

STAGE 2 - plan-replace
    Scans the extracted subtitle files for lines matching a set of known
    find/replace rules for the show given via --show. Any line that
    contains that show's watch word but doesn't match a known rule
    triggers an interactive prompt asking what it should become.
    Everything is recorded to a plan JSON file (nothing is modified yet).

STAGE 3 - merge-replace
    Applies the plan to the extracted subtitle files, then uses mkvmerge
    to rebuild each .mkv with the edited subtitle tracks swapped in for
    the originals, keeping every other track and all original metadata
    (language, track name, default/forced flags, chapters, attachments,
    etc.) untouched.

OPTIONAL STAGE - trim
    An alternative to plan-replace/merge-replace for a different problem: a subtitle
    track that runs longer than the video itself. Reads each extracted
    subtitle file, drops any entries that start after the video ends and
    truncates any entry that starts before the end but runs past it, then
    rebuilds the mkv the same way 'merge-replace' does. Requires 'extract' to
    have been run first; does not require 'plan-replace'. Needs ffprobe (part of
    FFmpeg, https://ffmpeg.org/) on PATH to determine the video's true
    duration -- mkvmerge's own container-duration figure isn't usable
    here since it reflects the longest track, which is exactly the
    oversized subtitle track this stage is trying to fix.

OPTIONAL STAGE - reposition
    Another alternative to plan-replace/merge-replace: adjusts an ASS/SSA subtitle's
    positioning for a resolution change where content is letterboxed or
    pillarboxed (padded with bars) rather than cropped or stretched.
    Auto-detects each file's original resolution from its own
    PlayResX/PlayResY (or use --old-res to override), computes the
    resulting scale/offset for a centered "contain" fit into --new-res,
    and adjusts PlayResX/PlayResY, style Fontsize/Outline/Shadow/Spacing/
    Margins, and inline \pos/\org/\fs/\bord/\shad/\fsp tags
    accordingly. Any line containing \move(...) or \clip(...)/\iclip(...)
    is left untouched and reported as a warning instead -- those need
    per-case judgment (timed motion paths, vector clip regions with their
    own drawing scale) that isn't safe to transform generically. Requires
    'extract' to have been run first; does not require 'plan-replace'.

OPTIONAL STAGE - style-sync
    Makes a show's named [V4+ Styles] entries (e.g. Main, Italics,
    Flashback) match a reference look (font, size, colors, outline,
    shadow, margins) defined per-show via --show, scaling pixel-sized
    fields by (this file's own PlayResY / the rule's reference PlayResY)
    so releases using different reference resolutions still end up
    visually consistent. Name, Italic, and Alignment are always left
    alone -- they're what make each named style distinct, not part of
    the "look." A style listed in the rule but absent from a given file
    is skipped, not an error. Requires 'extract' to have been run first;
    does not require 'plan-replace'.

OPTIONAL STAGE - check-fonts   (check-fonts and add-fonts require fontTools: pip install fonttools)
    Read-only check for missing fonts. For every ASS/SSA subtitle track in
    each mkv, finds the fonts actually used (styles referenced by dialogue
    lines plus inline \fn overrides) and reports any that are neither
    attached to the mkv, available in the fonts folder (default: the
    'mkv_subtitle_fonts' folder next to this script, or --fonts-dir), nor
    (with --check-system) installed on this machine. Fonts found in the
    fonts folder or installed locally are listed separately from truly
    missing ones.
    Does not require 'extract' and does not modify any files. Uses fontTools
    (pip install fonttools) to read attached font names when available,
    falling back to fc-scan.

OPTIONAL STAGE - dump-fonts
    Extracts every font attached to the mkvs in a folder into a single
    folder, keeping only one copy of each unique file (compared by content,
    so the same font attached to many episodes is written once). Re-runs
    skip fonts already in the output folder. If two different files share a
    filename (e.g. different versions), the later one gets a short hash
    suffix. Read-only on the mkvs; does not require 'extract'.

OPTIONAL STAGE - add-fonts
    Attaches missing fonts to the mkvs. For each mkv, finds fonts used by its
    ASS/SSA tracks that aren't attached, looks them up (by internal font name)
    in the fonts folder (default: 'mkv_subtitle_fonts' next to this script, or
    --fonts-dir), and attaches matching files with mkvpropedit. By default
    this edits the mkv files IN PLACE (fast, no remux); use -o to write edited
    copies to another folder instead, or --dry-run to only show what would be
    attached. For each font it attaches one file per bold/italic variant of
    the family (Regular, Bold, ...). Where the folder holds several candidate
    files for a variant (.ttf vs .otf, filename-case variants, hash-suffixed
    versions), they are checked against the characters the subs actually
    render with that font: candidates missing any of those glyphs are
    excluded, and if the remaining ones differ in character widths the pick
    is flagged for review. Ties go to .ttf, then an unsuffixed name, then the
    largest file. Fonts not found in the folder are listed as still missing. Needs mkvpropedit (part of MKVToolNix).

USAGE
-----
    python mkv_subtitle_tool.py extract        [-i .] [-w work]
    python mkv_subtitle_tool.py plan-replace   --show frieren [-w work]
    python mkv_subtitle_tool.py merge-replace  [-i .] [-w work] [-o out]
    python mkv_subtitle_tool.py trim           [-i .] [-w work] [-o out] [--margin 0.05]
    python mkv_subtitle_tool.py reposition     --new-res 1920x1080 [-i .] [-w work] [-o out]
    python mkv_subtitle_tool.py style-sync     --show dnt [-i .] [-w work] [-o out]
    python mkv_subtitle_tool.py check-fonts    [-i .] [--fonts-dir DIR] [--check-system]
    python mkv_subtitle_tool.py dump-fonts     [-i .] [-o fonts_dir]
    python mkv_subtitle_tool.py add-fonts      [-i .] [--fonts-dir DIR] [-o out] [--dry-run]

`-i/--input-dir` defaults to the current directory, `-w/--work-dir`
defaults to ./work, and `-o/--output-dir` (merge-replace/trim/reposition/style-sync)
defaults to ./out. All three can still be overridden explicitly.

Run `python mkv_subtitle_tool.py <stage> --help` for stage-specific options.
"""

from __future__ import annotations
import argparse
from .common import (
    DEFAULT_FONTS_DIRNAME,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_WORK_DIR,
    parse_int_list,
    parse_resolution,
    parse_str_list,
    set_color_enabled,
)
from .extract import (
    cmd_extract,
)
from .replace import (
    REPLACE_RULES,
    cmd_merge_replace,
    cmd_plan_replace,
)
from .trim import (
    DEFAULT_TRIM_MARGIN_SECONDS,
    cmd_trim,
)
from .reposition import (
    cmd_reposition,
)
from .style_sync import (
    STYLE_SYNC_RULES,
    cmd_style_sync,
)
from .fonts import (
    cmd_check_fonts,
    cmd_dump_fonts,
    cmd_add_fonts,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bulk-edit subtitle tracks across a folder of MKV files (text replacement, trim, reposition, style sync).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-color", action="store_true",
                         help="Disable colored console output (auto-disabled for non-terminal output too).")
    sub = parser.add_subparsers(dest="stage", required=True)

    p_extract = sub.add_parser("extract", help="Extract all subtitle tracks from every mkv in a folder.")
    p_extract.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                            help=f"Folder containing .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_extract.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                            help=f"Scratch/work directory (default: ./{DEFAULT_WORK_DIR}).")
    p_extract.set_defaults(func=cmd_extract)

    p_plan_replace = sub.add_parser("plan-replace", help="Scan extracted subtitles for per-show text replacements and build a plan.")
    p_plan_replace.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                         help=f"Scratch/work directory used by 'extract' (default: ./{DEFAULT_WORK_DIR}).")
    p_plan_replace.add_argument("--show", required=True, choices=sorted(REPLACE_RULES),
                         help="Which show's built-in rule set to use.")
    p_plan_replace.add_argument("--rules-file", default=None,
                         help="Optional JSON file of extra {\"find\": \"replace\"} rules "
                              "to add on top of the selected show's built-in rules.")
    p_plan_replace.set_defaults(func=cmd_plan_replace)

    p_merge_replace = sub.add_parser("merge-replace", help="Apply the replacement plan and rebuild the mkv files.")
    p_merge_replace.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                          help=f"Folder containing the original .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_merge_replace.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                          help=f"Scratch/work directory used by 'extract'/'plan-replace' (default: ./{DEFAULT_WORK_DIR}).")
    p_merge_replace.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR,
                          help=f"Where rebuilt .mkv files are written (default: ./{DEFAULT_OUTPUT_DIR}).")
    p_merge_replace.add_argument("--in-place", action="store_true",
                          help="After building, move the rebuilt files over the originals in "
                               "--input-dir (originals are backed up as *.mkv.bak).")
    p_merge_replace.add_argument("--dry-run", action="store_true",
                          help="Show what would be rebuilt without touching any .mkv files. Still "
                               "writes the edited subtitle files to the work directory for inspection.")
    p_merge_replace.set_defaults(func=cmd_merge_replace)

    p_trim = sub.add_parser(
        "trim",
        help="Cut subtitle entries that run past the video's duration and rebuild the mkv files.",
    )
    p_trim.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                         help=f"Folder containing the original .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_trim.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                         help=f"Scratch/work directory used by 'extract' (default: ./{DEFAULT_WORK_DIR}).")
    p_trim.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR,
                         help=f"Where rebuilt .mkv files are written (default: ./{DEFAULT_OUTPUT_DIR}).")
    p_trim.add_argument("--margin", type=float, default=DEFAULT_TRIM_MARGIN_SECONDS,
                         help="Seconds of buffer added to the video duration before trimming, so a "
                              f"caption ending right at the last frame isn't cut (default: {DEFAULT_TRIM_MARGIN_SECONDS}).")
    p_trim.add_argument("--in-place", action="store_true",
                         help="After building, move the rebuilt files over the originals in "
                              "--input-dir (originals are backed up as *.mkv.bak).")
    p_trim.add_argument("--dry-run", action="store_true",
                         help="Show what would be trimmed/rebuilt without touching any .mkv files. Still "
                              "writes the trimmed subtitle files to the work directory for inspection.")
    p_trim.set_defaults(func=cmd_trim)

    p_reposition = sub.add_parser(
        "reposition",
        help="Adjust ASS/SSA subtitle positioning for a resolution change "
             "(e.g. letterbox/pillarbox bars added instead of cropping).",
    )
    p_reposition.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                               help=f"Folder containing the original .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_reposition.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                               help=f"Scratch/work directory used by 'extract' (default: ./{DEFAULT_WORK_DIR}).")
    p_reposition.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR,
                               help=f"Where rebuilt .mkv files are written (default: ./{DEFAULT_OUTPUT_DIR}).")
    p_reposition.add_argument("--new-res", required=True, type=parse_resolution, metavar="WxH",
                               help="Target video resolution, e.g. 1920x1080.")
    p_reposition.add_argument("--old-res", default=None, type=parse_resolution, metavar="WxH",
                               help="Override the original resolution instead of auto-detecting each "
                                    "file's own PlayResX/PlayResY.")
    p_reposition.add_argument("--old-content-res", default=None, type=parse_resolution, metavar="WxH",
                               help="Use this when --new-res REMOVES letterbox/pillarbox padding "
                                    "rather than adding it (e.g. 1920x1080 padded -> 960x720 "
                                    "content-only). Give the actual content's size within the old "
                                    "(padded) frame, e.g. 1440x1080; the content is cropped out and "
                                    "rescaled to exactly fill --new-res. Without this flag, --new-res "
                                    "is assumed to ADD padding around the existing content instead.")
    p_reposition.add_argument("--track-id", default=None, type=parse_int_list, metavar="ID[,ID...]",
                               help="Only reposition subtitle track(s) with this ID (as extracted, e.g. "
                                    "'2' or '2,3'). Default: all editable ASS/SSA tracks.")
    p_reposition.add_argument("--track-name", default=None, type=parse_str_list, metavar="NAME[,NAME...]",
                               help="Only reposition subtitle track(s) whose track name contains this "
                                    "text (case-insensitive, e.g. 'Signs' or 'Signs,Songs'). Combined "
                                    "with --track-id (if both given) as AND, not OR. Default: all "
                                    "editable ASS/SSA tracks.")
    p_reposition.add_argument("--in-place", action="store_true",
                               help="After building, move the rebuilt files over the originals in "
                                    "--input-dir (originals are backed up as *.mkv.bak).")
    p_reposition.add_argument("--dry-run", action="store_true",
                               help="Show what would be repositioned/rebuilt without touching any .mkv "
                                    "files. Still writes the repositioned subtitle files to the work "
                                    "directory for inspection.")
    p_reposition.add_argument("--condensed", action="store_true",
                               help="Print a one-line-per-file summary of review items (\\move/\\clip "
                                    "adjustments, clamped margins, out-of-bounds points) instead of "
                                    "itemizing every instance. Recommended for batch runs across many files.")
    p_reposition.set_defaults(func=cmd_reposition)

    p_style_sync = sub.add_parser(
        "style-sync",
        help="Make a show's named subtitle styles (font/size/colors/margins) match a reference look, "
             "scaled for each file's own resolution.",
    )
    p_style_sync.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                               help=f"Folder containing the original .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_style_sync.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                               help=f"Scratch/work directory used by 'extract' (default: ./{DEFAULT_WORK_DIR}).")
    p_style_sync.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR,
                               help=f"Where rebuilt .mkv files are written (default: ./{DEFAULT_OUTPUT_DIR}).")
    p_style_sync.add_argument("--show", required=True, choices=sorted(STYLE_SYNC_RULES),
                               help="Which show's style-sync rule set to use.")
    p_style_sync.add_argument("--track-id", default=None, type=parse_int_list, metavar="ID[,ID...]",
                               help="Only sync subtitle track(s) with this ID. Default: all editable "
                                    "ASS/SSA tracks.")
    p_style_sync.add_argument("--track-name", default=None, type=parse_str_list, metavar="NAME[,NAME...]",
                               help="Only sync subtitle track(s) whose track name contains this text "
                                    "(case-insensitive). Default: all editable ASS/SSA tracks.")
    p_style_sync.add_argument("--generate-signs-track", action="store_true",
                               help="Also generate a new subtitle track per processed file, containing "
                                    "only the Dialogue/Comment lines whose style is NOT one of the "
                                    "show's dialogue styles (--show's style_names) -- i.e. signs, "
                                    "titles, and any other non-dialogue typesetting. Added as a new "
                                    "track alongside the original(s), not a replacement.")
    p_style_sync.add_argument("--fonts-dir", default=None, metavar="DIR",
                               help="Folder to look up this show's required font files (rule set's "
                                    "'fonts' list) in, for attaching to the rebuilt mkv. Default: a "
                                    f"'{DEFAULT_FONTS_DIRNAME}' folder next to this script.")
    p_style_sync.add_argument("--in-place", action="store_true",
                               help="After building, move the rebuilt files over the originals in "
                                    "--input-dir (originals are backed up as *.mkv.bak).")
    p_style_sync.add_argument("--dry-run", action="store_true",
                               help="Show what would be synced/rebuilt without touching any .mkv files. "
                                    "Still writes the synced subtitle files to the work directory for "
                                    "inspection.")
    p_style_sync.set_defaults(func=cmd_style_sync)

    p_check_fonts = sub.add_parser(
        "check-fonts",
        help="Check each mkv's ASS/SSA subtitle tracks for fonts that are neither attached nor installed.",
    )
    p_check_fonts.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                          help=f"Folder containing .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_check_fonts.add_argument("--check-system", action="store_true",
                          help="Also treat fonts installed on this machine (per fc-list) as present, "
                               "so only fonts missing from both the mkv and the system are reported.")
    p_check_fonts.add_argument("--fonts-dir", default=None, metavar="DIR",
                                help="Folder of font files to also count as available (reported as "
                                     "'in fonts folder, not attached', not as missing). Default: the "
                                     f"'{DEFAULT_FONTS_DIRNAME}' folder next to this script, if it exists.")
    p_check_fonts.set_defaults(func=cmd_check_fonts)

    p_dump_fonts = sub.add_parser(
        "dump-fonts",
        help="Extract every unique attached font from a folder of mkv files into one folder.",
    )
    p_dump_fonts.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                               help=f"Folder containing .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_dump_fonts.add_argument("-o", "--output-dir", default=None, metavar="DIR",
                               help="Folder to write the unique font files into. Default: a "
                                    f"'{DEFAULT_FONTS_DIRNAME}' folder next to this script.")
    p_dump_fonts.set_defaults(func=cmd_dump_fonts)

    p_add_fonts = sub.add_parser(
        "add-fonts",
        help="Attach fonts from the fonts folder to mkvs whose subs use fonts the mkv doesn't carry.",
    )
    p_add_fonts.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                              help=f"Folder containing .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_add_fonts.add_argument("--fonts-dir", default=None, metavar="DIR",
                              help="Folder of font files to attach from. Default: the "
                                   f"'{DEFAULT_FONTS_DIRNAME}' folder next to this script.")
    p_add_fonts.add_argument("-o", "--output-dir", default=None, metavar="DIR",
                              help="Write edited copies of the mkvs here instead of editing the "
                                   "originals in place.")
    p_add_fonts.add_argument("--dry-run", action="store_true",
                              help="Only show which fonts would be attached; change nothing.")
    p_add_fonts.set_defaults(func=cmd_add_fonts)

    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if getattr(args, "no_color", False):
        set_color_enabled(False)
    args.func(args)


if __name__ == "__main__":
    main()