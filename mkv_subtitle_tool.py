#!/usr/bin/env python3
"""
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

STAGE 2 - plan
    Scans the extracted subtitle files for lines matching a set of known
    find/replace rules for the show given via --show. Any line that
    contains that show's watch word but doesn't match a known rule
    triggers an interactive prompt asking what it should become.
    Everything is recorded to a plan JSON file (nothing is modified yet).

STAGE 3 - merge
    Applies the plan to the extracted subtitle files, then uses mkvmerge
    to rebuild each .mkv with the edited subtitle tracks swapped in for
    the originals, keeping every other track and all original metadata
    (language, track name, default/forced flags, chapters, attachments,
    etc.) untouched.

OPTIONAL STAGE - trim
    An alternative to plan/merge for a different problem: a subtitle
    track that runs longer than the video itself. Reads each extracted
    subtitle file, drops any entries that start after the video ends and
    truncates any entry that starts before the end but runs past it, then
    rebuilds the mkv the same way 'merge' does. Requires 'extract' to
    have been run first; does not require 'plan'. Needs ffprobe (part of
    FFmpeg, https://ffmpeg.org/) on PATH to determine the video's true
    duration -- mkvmerge's own container-duration figure isn't usable
    here since it reflects the longest track, which is exactly the
    oversized subtitle track this stage is trying to fix.

OPTIONAL STAGE - reposition
    Another alternative to plan/merge: adjusts an ASS/SSA subtitle's
    positioning for a resolution change where content is letterboxed or
    pillarboxed (padded with bars) rather than cropped or stretched.
    Auto-detects each file's original resolution from its own
    PlayResX/PlayResY (or use --old-res to override), computes the
    resulting scale/offset for a centered "contain" fit into --new-res,
    and adjusts PlayResX/PlayResY, style Fontsize/Outline/Shadow/Spacing/
    Margins, and inline \\pos/\\org/\\fs/\\bord/\\shad/\\fsp tags
    accordingly. Any line containing \\move(...) or \\clip(...)/\\iclip(...)
    is left untouched and reported as a warning instead -- those need
    per-case judgment (timed motion paths, vector clip regions with their
    own drawing scale) that isn't safe to transform generically. Requires
    'extract' to have been run first; does not require 'plan'.

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
    does not require 'plan'.

USAGE
-----
    python mkv_subtitle_tool.py extract      [-i .] [-w work]
    python mkv_subtitle_tool.py plan         --show frieren [-w work]
    python mkv_subtitle_tool.py merge        [-i .] [-w work] [-o out]
    python mkv_subtitle_tool.py trim         [-i .] [-w work] [-o out] [--margin 0.05]
    python mkv_subtitle_tool.py reposition   --new-res 1920x1080 [-i .] [-w work] [-o out]
    python mkv_subtitle_tool.py style-sync   --show dnt [-i .] [-w work] [-o out]

`-i/--input-dir` defaults to the current directory, `-w/--work-dir`
defaults to ./work, and `-o/--output-dir` (merge/trim/style-sync only)
defaults to ./out. All three can still be overridden explicitly.

Run `python mkv_subtitle_tool.py <stage> --help` for stage-specific options.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

# Per-show replacement rule sets, selected via the 'plan' stage's required
# --show flag. Each show has:
#   replacements             - exact substring match, case-insensitive.
#                               Key = text to search for, Value = replacement.
#   width_compensated_rules  - subset of the replacement keys that should
#                               get automatic \fscx width compensation in
#                               ASS/SSA files when the replacement text is
#                               longer (see compute_width_compensation).
#                               Rules not listed here are left at their
#                               natural width even if longer.
#   watch_word                - word that triggers an interactive prompt for
#                               any line containing it that isn't already
#                               covered by a replacement rule above.
SHOW_RULES: Dict[str, Dict[str, Any]] = {
    "frieren": {
        "replacements": {
            "an ageless elegy": "BEYOND JOURNEY'S END",
            "Frieren the Elegy": "Frieren the Slayer",
            "Solltrag": "Zoltraak",
            "end your life": "Kill yourself",
        },
        "width_compensated_rules": {
            "an ageless elegy",
        },
        "watch_word": "elegy",
    },
}

# Per-show style-sync rule sets, selected via the 'style-sync' stage's
# required --show flag. Each show has:
#   reference_res    - the PlayResX/PlayResY the values below were designed
#                       at. Pixel-sized fields are rescaled by
#                       (file's own PlayResY / reference_res[1]) for each
#                       individual file, since different releases of the
#                       same show can use different reference resolutions
#                       (e.g. a 640x360 vs. a 1920x1080 convention) even
#                       though the on-screen result should look the same.
#   style_names       - which named [V4+ Styles] entries get synced. A
#                       style name not present in a given file is skipped
#                       (not an error -- not every episode necessarily
#                       defines every variant).
#   fixed_fields      - field -> value applied as-is, unscaled (colors,
#                       Bold/Underline/StrikeOut, ScaleX/ScaleY, Angle,
#                       BorderStyle, Encoding -- anything that isn't a
#                       pixel size).
#   scaled_fields     - field -> reference value, multiplied by the
#                       computed scale before being applied (Fontsize,
#                       Outline, Shadow, Spacing, MarginL/MarginR/MarginV).
# Name, Italic, and Alignment are deliberately never touched here -- they
# define what makes each named style distinct (regular vs. italic,
# bottom- vs. top-positioned), so the sync preserves whatever each style
# already has for those three fields.
STYLE_SYNC_RULES: Dict[str, Dict[str, Any]] = {
    "dnt": {
        "reference_res": (1920, 1080),
        "style_names": [
            "Main", "Italics", "Flashback", "Flashback_Italics",
            "Main - Top", "Italics - Top", "Narration",
        ],
        "fixed_fields": {
            "Fontname": "Gandhi Sans",
            "PrimaryColour": "&H00FFFFFF",
            "SecondaryColour": "&H000000FF",
            "OutlineColour": "&H00000000",
            "BackColour": "&H96000000",
            "Bold": "-1",
            "Underline": "0",
            "StrikeOut": "0",
            "ScaleX": "100",
            "ScaleY": "100",
            "Angle": "0",
            "BorderStyle": "1",
            "Encoding": "1",
        },
        "scaled_fields": {
            "Fontsize": 75,
            "Outline": 3,
            "Shadow": 1,
            "Spacing": 0,
            "MarginL": 150,
            "MarginR": 150,
            "MarginV": 50,
        },
        # Font files this show's "look" needs embedded as mkv attachments --
        # DNT's original releases use Roboto Medium (which IS embedded), but
        # after fixed_fields above switches Fontname to Gandhi Sans, that
        # font also needs to be attached or it won't render correctly for
        # viewers who don't have it installed system-wide. Filenames are
        # looked up in --fonts-dir (default: a "mkv_subtitle_fonts" folder
        # next to this script).
        "fonts": [
            "GandhiSans-Bold_A.otf",
            "GandhiSans-BoldItalic_A.otf",
        ],
    },
}

# codec_id (from `mkvmerge -J`) -> file extension for extraction.
CODEC_EXTENSIONS: Dict[str, str] = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASCII": ".srt",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/WEBVTT": ".vtt",
    "S_TEXT/USF": ".usf",
    "S_VOBSUB": ".sub",       # image-based, paired with .idx
    "S_HDMV/PGS": ".sup",     # image-based
    "S_HDMV/TEXTST": ".sup",
    "S_KATE": ".ogg",
}

# Formats we can actually search/edit as text.
TEXT_EDITABLE_EXTS = {".srt", ".ass", ".ssa", ".vtt"}

TRACKS_META_FILENAME = "_tracks.json"
DEFAULT_WORK_DIR = "work"
DEFAULT_INPUT_DIR = "."
DEFAULT_OUTPUT_DIR = "out"
EXTRACTED_DIRNAME = "extracted"
EDITED_DIRNAME = "edited"
TRIMMED_DIRNAME = "trimmed"
REPOSITIONED_DIRNAME = "repositioned"
STYLE_SYNCED_DIRNAME = "style_synced"
DEFAULT_FONTS_DIRNAME = "mkv_subtitle_fonts"
# Explicit MIME types for font attachments rather than relying on mkvmerge's
# own content-sniffing, which can misfire on malformed/truncated font files.
FONT_MIME_TYPES = {
    ".ttf": "application/x-truetype-font",
    ".otf": "application/vnd.ms-opentype",
    ".ttc": "application/x-truetype-font",
}


def default_fonts_dir() -> Path:
    """
    A `mkv_subtitle_fonts/` folder next to this script itself (not the
    current working directory, and not --input-dir) -- the natural place
    to keep font files shared across every show/episode you process,
    overridable per-run with --fonts-dir.
    """
    return Path(__file__).resolve().parent / DEFAULT_FONTS_DIRNAME
PLAN_FILENAME = "plan.json"
PLAN_LOG_FILENAME = "plan.log"

# Default buffer (seconds) added to the video's duration before trimming,
# so a caption that legitimately ends right at (or a hair past, due to
# rounding) the last video frame isn't needlessly cut.
DEFAULT_TRIM_MARGIN_SECONDS = 0.05

# Cap how much a replacement's on-screen text is allowed to shrink to
# compensate for extra width (percent of original horizontal scale).
MIN_FSCX_SCALE = 80
MAX_FSCX_SCALE = 100
# How much of the "full" correction to actually apply (0 = never scale,
# 1 = fully match the old text's width). 0.5 splits the difference so
# long replacements shrink noticeably less than a full 1:1 correction.
WIDTH_COMPENSATION_STRENGTH = 1.0


# --------------------------------------------------------------------------
# Console color helpers (auto-disabled for non-tty output / NO_COLOR / --no-color)
# --------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


COLOR_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def set_color_enabled(enabled: bool) -> None:
    global COLOR_ENABLED
    COLOR_ENABLED = enabled


def c(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes for terminal display only (never used in the log file)."""
    if not COLOR_ENABLED or not codes:
        return text
    return f"{''.join(codes)}{text}{C.RESET}"


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def check_tools() -> None:
    missing = [t for t in ("mkvmerge", "mkvextract") if shutil.which(t) is None]
    if missing:
        sys.exit(
            f"Error: required tool(s) not found on PATH: {', '.join(missing)}.\n"
            "Install MKVToolNix (https://mkvtoolnix.download/) and try again."
        )


def check_ffprobe() -> None:
    if shutil.which("ffprobe") is None:
        sys.exit(
            "Error: 'trim' needs ffprobe (part of FFmpeg) to determine the video's true "
            "duration on PATH.\nInstall FFmpeg (https://ffmpeg.org/) and try again."
        )


def run(cmd: List[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def probe(mkv_path: Path) -> Dict[str, Any]:
    proc = run(["mkvmerge", "-J", str(mkv_path)])
    return json.loads(proc.stdout)


def find_mkvs(input_dir: Path) -> List[Path]:
    files = sorted(p for p in input_dir.glob("*.mkv") if p.is_file())
    if not files:
        sys.exit(f"No .mkv files found in {input_dir}")
    return files


def read_text_smart(path: Path) -> Tuple[str, str]:
    """Read a subtitle file, returning (text, encoding_used)."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc), enc
        except UnicodeDecodeError:
            continue
    # last resort
    return path.read_text(encoding="utf-8", errors="replace"), "utf-8"


# --------------------------------------------------------------------------
# STAGE 1: extract
# --------------------------------------------------------------------------

def cmd_extract(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    out_root = work_dir / EXTRACTED_DIRNAME
    out_root.mkdir(parents=True, exist_ok=True)

    mkvs = find_mkvs(input_dir)
    print(f"Found {len(mkvs)} mkv file(s) in {input_dir}\n")

    for mkv in mkvs:
        print(f"Probing {mkv.name} ...")
        info = probe(mkv)
        sub_tracks = [t for t in info.get("tracks", []) if t.get("type") == "subtitles"]

        if not sub_tracks:
            print("  no subtitle tracks, skipping.\n")
            continue

        dest_dir = out_root / mkv.stem
        dest_dir.mkdir(parents=True, exist_ok=True)

        extract_args: List[str] = []
        tracks_meta: List[Dict[str, Any]] = []

        for t in sub_tracks:
            tid = t["id"]
            props = t.get("properties", {})
            codec_id = props.get("codec_id", "")
            ext = CODEC_EXTENSIONS.get(codec_id, f".track{tid}.bin")
            lang = props.get("language_ietf") or props.get("language") or "und"
            name = props.get("track_name", "")
            safe_name = re.sub(r"[^\w.-]+", "_", name)[:40] if name else ""
            fname = f"track{tid}_{lang}" + (f"_{safe_name}" if safe_name else "") + ext
            out_path = dest_dir / fname

            extract_args.append(f"{tid}:{out_path}")
            tracks_meta.append(
                {
                    "id": tid,
                    "codec_id": codec_id,
                    "codec": t.get("codec"),
                    "language": props.get("language", "und"),
                    "language_ietf": props.get("language_ietf"),
                    "track_name": props.get("track_name", ""),
                    "default_track": bool(props.get("default_track", False)),
                    "forced_track": bool(props.get("forced_track", False)),
                    "enabled_track": bool(props.get("enabled_track", True)),
                    "file": str(out_path.relative_to(work_dir)),
                    "editable": ext in TEXT_EDITABLE_EXTS,
                }
            )

            flags = []
            if props.get("default_track"):
                flags.append("default")
            if props.get("forced_track"):
                flags.append("forced")
            flags_note = f"  [{', '.join(flags)}]" if flags else ""
            editable_note = "" if ext in TEXT_EDITABLE_EXTS else c("  (not text-editable)", C.YELLOW)
            print(
                f"  {c(f'id {tid}', C.BOLD, C.CYAN)}  "
                f"{name or c('(unnamed)', C.DIM)}  "
                f"{c(f'[{lang}]', C.DIM)}  "
                f"{t.get('codec', codec_id)}{flags_note}{editable_note}"
            )

        run(["mkvextract", "tracks", str(mkv), *extract_args])

        meta_payload = {
            "source_mkv": str(mkv.resolve()),
            "source_name": mkv.name,
            "title": info.get("container", {}).get("properties", {}).get("title", ""),
            "tracks": tracks_meta,
        }
        (dest_dir / TRACKS_META_FILENAME).write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")

        editable = sum(1 for m in tracks_meta if m["editable"])
        skipped = len(tracks_meta) - editable
        print(f"  extracted {len(tracks_meta)} subtitle track(s) "
              f"({editable} text-editable, {skipped} image/binary - not editable).\n")

    print(f"Done. Extracted subtitles are under: {out_root}")


# --------------------------------------------------------------------------
# STAGE 2: plan
# --------------------------------------------------------------------------

SRT_INDEX_RE = re.compile(r"^\d+$")
TIMESTAMP_RE = re.compile(r"-->")
ASS_DIALOGUE_RE = re.compile(r"^(Dialogue:\s*(?:[^,]*,){9})(.*)$")  # 9 fields before Text
ASS_COMMENT_RE = re.compile(r"^(Comment:\s*(?:[^,]*,){9})(.*)$")
ASS_TIME_FIELDS_RE = re.compile(r"^(?:Dialogue|Comment):\s*([^,]*),([^,]*),([^,]*),")


def get_timestamp(lines: List[str], line_no: int, ext: str) -> str:
    """
    Best-effort timestamp lookup for a given line, for display in logs.
    - .ass/.ssa: read the Start/End fields directly off the Dialogue/Comment line.
    - .srt/.vtt: the timestamp is on its own line above the text; search
      upward for the nearest "-->" line.
    """
    if ext in (".ass", ".ssa"):
        m = ASS_TIME_FIELDS_RE.match(lines[line_no])
        if m:
            start, end = m.group(2).strip(), m.group(3).strip()
            return f"{start} --> {end}"
        return ""

    for i in range(line_no, -1, -1):
        if TIMESTAMP_RE.search(lines[i]):
            return lines[i].strip()
    return ""


ASS_TIME_VALUE_RE = re.compile(r"^(\d+):(\d{2}):(\d{2})\.(\d{2})$")
SRT_TIME_VALUE_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})[.,](\d{3})$")

# When grouping consecutive matches for the log, two records are treated as
# part of the same animation/effect sequence if the next one starts within
# this many seconds of the previous one ending (handles frame-stepped
# reveal/wipe animations with lots of tiny timing increments). Set higher
# to merge more generously, lower to split on any real timing gap.
TIME_GROUPING_TOLERANCE_SECONDS = 0.5
# Sanity cap on how many raw file lines a group is allowed to span, so a
# rule matching the same text again far later in the file (a genuinely
# separate occurrence) doesn't get merged just because timing happens to
# line up.
MAX_LINE_GAP_FOR_GROUPING = 50


def parse_timestamp_value(value: str) -> Optional[float]:
    """Parse a single ASS (h:mm:ss.cc) or SRT (hh:mm:ss,mmm) time value to seconds."""
    value = value.strip()
    m = SRT_TIME_VALUE_RE.match(value)
    if m:
        h, mm, s, ms = (int(g) for g in m.groups())
        return h * 3600 + mm * 60 + s + ms / 1000
    m = ASS_TIME_VALUE_RE.match(value)
    if m:
        h, mm, s, cs = (int(g) for g in m.groups())
        return h * 3600 + mm * 60 + s + cs / 100
    return None


def parse_timestamp_bounds(timestamp: str) -> Optional[Tuple[float, float]]:
    """Parse a "start --> end" display timestamp into (start_seconds, end_seconds)."""
    if not timestamp:
        return None
    parts = timestamp.split("-->")
    if len(parts) != 2:
        return None
    start = parse_timestamp_value(parts[0])
    end = parse_timestamp_value(parts[1])
    if start is None or end is None:
        return None
    return start, end


ASS_INLINE_TAG_RE = re.compile(r"\{[^}]*\}")
ASS_LEADING_TAGS_RE = re.compile(r"^(?:\{[^}]*\})+")


def split_leading_ass_tags(text: str) -> Tuple[str, str]:
    """
    Split off any {...} override-tag block(s) sitting at the very start of
    an ASS/SSA text field (e.g. positioning/color/font setup) from the
    rest of the visible text.
    """
    m = ASS_LEADING_TAGS_RE.match(text)
    leading = m.group(0) if m else ""
    return leading, text[len(leading):]


def strip_inline_ass_tags(text: str) -> str:
    """
    Remove any remaining {...} override-tag blocks embedded *within* the
    text (e.g. per-letter effects like {\\fsp} used for stylized
    typesetting/karaoke). These often split words across tag boundaries
    (e.g. "AN AGELESS ELEG{\\fsp}Y"), which would otherwise prevent plain
    substring matching from ever succeeding.
    """
    return ASS_INLINE_TAG_RE.sub("", text)


def compute_width_compensation(old_visible: str, new_visible: str) -> Optional[int]:
    """
    If new_visible is longer than old_visible, return a horizontal-scale
    percentage (for an ASS \\fscx tag) that shrinks the new text to
    partially compensate for the extra width. Only WIDTH_COMPENSATION_STRENGTH
    of the full 1:1 correction is applied, so the result sits between "no
    scaling" and "exactly matching the old width" rather than fully
    matching it. Returns None if no compensation is needed (new text
    isn't longer). Clamped to [MIN_FSCX_SCALE, MAX_FSCX_SCALE] so large
    size differences don't squash the text illegibly.
    """
    old_len = len(old_visible.strip())
    new_len = len(new_visible.strip())
    if old_len == 0 or new_len <= old_len:
        return None
    full_scale = 100 * old_len / new_len
    scale = round(100 - (100 - full_scale) * WIDTH_COMPENSATION_STRENGTH)
    scale = max(MIN_FSCX_SCALE, min(MAX_FSCX_SCALE, scale))
    return scale if scale < 100 else None


def iter_text_segments(lines: List[str], ext: str):
    """
    Yield (line_index, prefix, text_segment, suffix) for every line that
    contains searchable dialogue text. Reconstructed line = prefix + new_text + suffix.
    For simple formats (srt/vtt) prefix/suffix are "" and text_segment is the whole line.
    For ass/ssa, only the Text field of Dialogue/Comment lines is searchable.
    """
    for i, line in enumerate(lines):
        stripped = line.strip()

        if ext in (".ass", ".ssa"):
            m = ASS_DIALOGUE_RE.match(line) or ASS_COMMENT_RE.match(line)
            if m:
                yield i, m.group(1), m.group(2), ""
            continue

        # .srt / .vtt
        if not stripped:
            continue
        if SRT_INDEX_RE.match(stripped):
            continue
        if TIMESTAMP_RE.search(stripped):
            continue
        if ext == ".vtt" and stripped.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        yield i, "", line, ""


def apply_known_replacements(text: str, replacements: Dict[str, str]) -> Tuple[str, List[str]]:
    """Case-insensitive substring replace. Returns (new_text, matched_keys)."""
    matched = []
    new_text = text
    for old, new in replacements.items():
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        if pattern.search(new_text):
            matched.append(old)
            new_text = pattern.sub(lambda _m, new=new: new, new_text)
    return new_text, matched


def flush_match_records(rel: Path, records: List[Dict[str, Any]], log_lines: List[str]) -> None:
    """
    Print and log the buffered match/asked/skip records for one subtitle
    file, collapsing runs of matches that are identical in every way
    except line number/timestamp into a single grouped entry with a line
    range and occurrence count. This covers both:
      - stacked-layer typesetting (several adjacent Dialogue lines at the
        *same* timestamp for a glow/color effect), and
      - frame-stepped reveal/wipe animations (many Dialogue lines with
        slightly *increasing* timestamps, often with unrelated lines for
        other simultaneous signs interleaved between them, breaking raw
        line-number adjacency).
    Grouping is based on timing continuity (see TIME_GROUPING_TOLERANCE_SECONDS)
    rather than strict "next line number" adjacency, so interleaved
    unrelated lines and tiny timing increments don't fragment the log.
    """
    i, n = 0, len(records)
    while i < n:
        j = i
        cur = records[i]
        group_end_seconds = None
        bounds = parse_timestamp_bounds(cur["timestamp"])
        if bounds:
            group_end_seconds = bounds[1]

        while j + 1 < n:
            nxt = records[j + 1]
            if not (
                nxt["kind"] == cur["kind"]
                and nxt["old"] == cur["old"]
                and nxt["new"] == cur["new"]
                and nxt["note"] == cur["note"]
                and nxt["scale"] == cur["scale"]
                and nxt["line_no"] - records[j]["line_no"] <= MAX_LINE_GAP_FOR_GROUPING
            ):
                break

            nxt_bounds = parse_timestamp_bounds(nxt["timestamp"])
            if group_end_seconds is not None and nxt_bounds is not None:
                if nxt_bounds[0] > group_end_seconds + TIME_GROUPING_TOLERANCE_SECONDS:
                    break
                group_end_seconds = max(group_end_seconds, nxt_bounds[1])
            elif nxt["line_no"] != records[j]["line_no"] + 1:
                # No usable timestamp on one side -> fall back to requiring
                # strict adjacency so we don't over-merge blindly.
                break

            j += 1
        group = records[i:j + 1]
        i = j + 1

        if len(group) == 1:
            line_range = str(group[0]["line_no"] + 1)
        else:
            line_range = f"{group[0]['line_no'] + 1}-{group[-1]['line_no'] + 1}"
        count_note = f"  {c(f'(x{len(group)})', C.DIM)}" if len(group) > 1 else ""
        count_note_log = f"  (x{len(group)})" if len(group) > 1 else ""

        timestamps = [r["timestamp"] for r in group if r["timestamp"]]
        if not timestamps:
            ts_display = ""
        elif len(set(timestamps)) == 1:
            ts_display = timestamps[0]
        else:
            ts_display = f"{timestamps[0]} .. {timestamps[-1]}"
        ts_console = f"  {c(f'[{ts_display}]', C.GRAY)}" if ts_display else ""
        ts_log = f"  [{ts_display}]" if ts_display else ""

        scale = cur["scale"]
        width_note_console = f"  {c(f'[fscx {scale}%]', C.MAGENTA)}" if scale is not None else ""
        width_note_log = f"  [fscx {scale}%]" if scale is not None else ""

        if cur["kind"] == "MATCH":
            msg_console = (
                f"{c('[MATCH]', C.BOLD, C.GREEN)} {c(str(rel), C.CYAN)}:{line_range}{ts_console}"
                f"  {c(cur['note'], C.DIM)}{width_note_console}{count_note}\n"
                f"        {c('old:', C.RED)} {cur['old']}\n"
                f"        {c('new:', C.GREEN)} {cur['new']}"
            )
            msg_log = (
                f"[MATCH] {rel}:{line_range}{ts_log}  ({cur['note']}){width_note_log}{count_note_log}\n"
                f"        old: {cur['old']}\n"
                f"        new: {cur['new']}"
            )
        elif cur["kind"] == "ASKED":
            msg_console = (
                f"{c('[ASKED]', C.BOLD, C.YELLOW)} {c(str(rel), C.CYAN)}:{line_range}"
                f"{ts_console}{width_note_console}{count_note}\n"
                f"        {c('old:', C.RED)} {cur['old']}\n"
                f"        {c('new:', C.GREEN)} {cur['new']}"
            )
            msg_log = (
                f"[ASKED] {rel}:{line_range}{ts_log}{width_note_log}{count_note_log}\n"
                f"        old: {cur['old']}\n"
                f"        new: {cur['new']}"
            )
        else:  # SKIP
            msg_console = (
                f"{c('[SKIP]', C.BOLD, C.GRAY)}  {c(str(rel), C.CYAN)}:{line_range}{ts_console}"
                f"  {c(cur['note'], C.DIM)}{count_note}\n"
                f"        {c('line:', C.DIM)} {cur['old']}"
            )
            msg_log = (
                f"[SKIP]  {rel}:{line_range}{ts_log}  ({cur['note']}){count_note_log}\n"
                f"        line: {cur['old']}"
            )

        print(msg_console)
        log_lines.append(msg_log)


# --------------------------------------------------------------------------
# STAGE (optional): trim -- cut subtitle entries that run past the video
# --------------------------------------------------------------------------

SRT_TIME_LINE_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})(.*)$"
)
ASS_TIME_LINE_SPLIT_RE = re.compile(
    r"^((?:Dialogue|Comment):\s*[^,]*,)([^,]*)(,)([^,]*)(,.*)$"
)


def format_srt_like_time(seconds: float, sep: str) -> str:
    """Format seconds as HH:MM:SS<sep>mmm (sep is ',' for SRT, '.' for VTT)."""
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    h, total_ms = divmod(total_ms, 3_600_000)
    m, total_ms = divmod(total_ms, 60_000)
    s, ms = divmod(total_ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def format_ass_time(seconds: float) -> str:
    """Format seconds as ASS/SSA time: h:mm:ss.cs (centiseconds, unpadded hour)."""
    seconds = max(0.0, seconds)
    total_cs = int(round(seconds * 100))
    h, total_cs = divmod(total_cs, 360_000)
    m, total_cs = divmod(total_cs, 6_000)
    s, cs = divmod(total_cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def trim_timed_text_blocks(text: str, max_seconds: float, sep: str) -> Tuple[str, int, int]:
    """
    Trim an .srt or .vtt file's cue blocks to `max_seconds`: cues starting
    after max_seconds are dropped entirely; cues starting before but
    ending after max_seconds have their end time truncated. Cue/index
    numbering is renumbered afterward. Blocks that don't parse as a
    recognizable cue (headers, NOTE blocks, malformed content) are left
    untouched. Returns (new_text, dropped_count, truncated_count).
    """
    had_crlf = "\r\n" in text
    normalized = text.replace("\r\n", "\n")
    trailing_newline = normalized.endswith("\n")
    body = normalized.strip("\n")
    if not body:
        return text, 0, 0

    raw_blocks = re.split(r"\n{2,}", body)
    new_blocks: List[str] = []
    dropped = 0
    truncated = 0

    for block in raw_blocks:
        block_lines = block.split("\n")
        ts_idx = next((idx for idx, l in enumerate(block_lines) if TIMESTAMP_RE.search(l)), None)
        if ts_idx is None:
            new_blocks.append(block)
            continue

        m = SRT_TIME_LINE_RE.match(block_lines[ts_idx].strip())
        if not m:
            new_blocks.append(block)
            continue

        start_str, end_str, trailing = m.groups()
        start_s = parse_timestamp_value(start_str)
        end_s = parse_timestamp_value(end_str)
        if start_s is None or end_s is None:
            new_blocks.append(block)
            continue

        if start_s > max_seconds:
            dropped += 1
            continue

        if end_s > max_seconds:
            block_lines[ts_idx] = f"{start_str} --> {format_srt_like_time(max_seconds, sep)}{trailing}"
            truncated += 1

        new_blocks.append("\n".join(block_lines))

    # Renumber any purely-numeric index line at the start of a block (SRT
    # convention; VTT cues typically don't have one, so this is a no-op there).
    renumbered: List[str] = []
    counter = 1
    for block in new_blocks:
        block_lines = block.split("\n")
        if block_lines and SRT_INDEX_RE.match(block_lines[0].strip()):
            block_lines[0] = str(counter)
            counter += 1
        renumbered.append("\n".join(block_lines))

    result = "\n\n".join(renumbered) + ("\n" if trailing_newline else "")
    if had_crlf:
        result = result.replace("\n", "\r\n")
    return result, dropped, truncated


def trim_ass_lines(lines: List[str], max_seconds: float) -> Tuple[List[str], int, int]:
    """
    Trim an .ass/.ssa file's Dialogue/Comment lines to `max_seconds`: lines
    starting after max_seconds are dropped entirely; lines starting before
    but ending after max_seconds have their End field truncated. All other
    lines (styles, headers, etc.) pass through unchanged. Returns
    (new_lines, dropped_count, truncated_count).
    """
    new_lines: List[str] = []
    dropped = 0
    truncated = 0

    for line in lines:
        m = ASS_TIME_LINE_SPLIT_RE.match(line)
        if not m:
            new_lines.append(line)
            continue

        prefix, start_str, comma, end_str, rest = m.groups()
        start_s = parse_timestamp_value(start_str.strip())
        end_s = parse_timestamp_value(end_str.strip())
        if start_s is None or end_s is None:
            new_lines.append(line)
            continue

        if start_s > max_seconds:
            dropped += 1
            continue

        if end_s > max_seconds:
            line = prefix + start_str + comma + format_ass_time(max_seconds) + rest
            truncated += 1

        new_lines.append(line)

    return new_lines, dropped, truncated


def get_video_duration_seconds(mkv_path: Path) -> Optional[float]:
    """
    True duration of the video track, in seconds -- NOT the container's
    overall duration (mkvmerge -J's container.properties.duration reflects
    the *longest* track, which is exactly wrong here: an oversized
    subtitle track would make the container look longer than the video
    actually is). Instead, read the video stream's packet timestamps via
    ffprobe and take the last presentation time plus one frame's length.
    Returns None if ffprobe can't determine it (no video stream, unusual
    timestamps, etc.).
    """
    try:
        fr_proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate", "-of", "csv=p=0", str(mkv_path)],
            capture_output=True, text=True,
        )
        frame_rate = None
        fr_str = fr_proc.stdout.strip()
        if "/" in fr_str:
            num, den = fr_str.split("/", 1)
            if float(den) != 0:
                frame_rate = float(num) / float(den)

        pts_proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(mkv_path)],
            capture_output=True, text=True,
        )
        pts_values = [
            float(line) for line in pts_proc.stdout.splitlines()
            if line.strip() and line.strip().upper() != "N/A"
        ]
        if not pts_values:
            return None
        max_pts = max(pts_values)
        return max_pts + (1.0 / frame_rate if frame_rate else 0.0)
    except (OSError, ValueError):
        return None


def cmd_trim(args: argparse.Namespace) -> None:
    check_tools()
    check_ffprobe()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    extracted_root = work_dir / EXTRACTED_DIRNAME
    if not extracted_root.exists():
        sys.exit(f"No extracted subtitles found at {extracted_root}. Run the 'extract' stage first.")
    mkv_dirs = sorted(d for d in extracted_root.iterdir() if d.is_dir())
    if not mkv_dirs:
        sys.exit(f"No extracted subtitle folders found under {extracted_root}.")

    trimmed_root = work_dir / TRIMMED_DIRNAME
    edited_paths: Dict[str, Path] = {}
    any_trimmed = False

    for mkv_dir in mkv_dirs:
        meta_path = mkv_dir / TRACKS_META_FILENAME
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        source_mkv = Path(meta["source_mkv"])
        if not source_mkv.exists():
            candidate = input_dir / meta["source_name"]
            if candidate.exists():
                source_mkv = candidate
            else:
                print(c(f"WARNING: source file for {mkv_dir.name} not found "
                      f"({source_mkv}); skipping.", C.YELLOW))
                continue

        video_duration = get_video_duration_seconds(source_mkv)
        if video_duration is None:
            print(c(f"WARNING: could not determine video duration for {source_mkv.name}; skipping.", C.YELLOW))
            continue
        max_seconds = video_duration + args.margin

        print(c(f"== {source_mkv.name} ==", C.BOLD, C.BLUE))
        print(f"  video duration: {video_duration:.3f}s"
              + (f"  (+{args.margin:.3f}s margin -> cutoff {max_seconds:.3f}s)" if args.margin else ""))

        for t in meta["tracks"]:
            if not t["editable"]:
                continue
            rel = t["file"]
            src_path = work_dir / rel
            text, encoding = read_text_smart(src_path)
            ext = src_path.suffix.lower()
            label = t.get("track_name") or f"track {t['id']}"

            if ext in (".ass", ".ssa"):
                lines = text.splitlines(keepends=False)
                newline = "\r\n" if "\r\n" in text else "\n"
                new_lines, dropped, truncated = trim_ass_lines(lines, max_seconds)
                new_text = newline.join(new_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
            elif ext in (".srt", ".vtt"):
                sep = "," if ext == ".srt" else "."
                new_text, dropped, truncated = trim_timed_text_blocks(text, max_seconds, sep)
            else:
                print(c(f"  track {t['id']} ({label}): unsupported format for trimming "
                      f"({ext}); leaving as-is.", C.YELLOW))
                continue

            if dropped == 0 and truncated == 0:
                print(f"  track {t['id']} ({label}): already within duration, no changes.")
                continue

            dest_path = trimmed_root / rel
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(new_text, encoding="utf-8")
            edited_paths[rel] = dest_path
            any_trimmed = True
            entry_word = "entry" if dropped + truncated == 1 else "entries"
            print(c(f"  track {t['id']} ({label}): dropped {dropped}, truncated {truncated} {entry_word}.", C.GREEN))
        print()

    if not any_trimmed:
        print(c("No subtitle tracks needed trimming.", C.BOLD, C.GREEN))
        return

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run)


# --------------------------------------------------------------------------
# STAGE (optional): reposition -- adjust ASS/SSA coordinates for a
# resolution change (e.g. bars added when going from one aspect ratio to
# another without cropping)
# --------------------------------------------------------------------------

PLAYRESX_LINE_RE = re.compile(r"^(PlayResX:\s*)(\d+)", re.IGNORECASE)
PLAYRESY_LINE_RE = re.compile(r"^(PlayResY:\s*)(\d+)", re.IGNORECASE)
# Aegisub/libass extension: tells the renderer "these coordinates were
# authored at LayoutRes; auto-scale them to fit the actual PlayRes." If
# left pointing at the OLD resolution while we update PlayResX/Y, the
# renderer applies its own extra (and, across an aspect-ratio change,
# non-uniform) correction on top of the one we already did manually --
# so this must be kept in lockstep with PlayResX/Y whenever present.
LAYOUTRESX_LINE_RE = re.compile(r"^(LayoutResX:\s*)(\d+)", re.IGNORECASE)
LAYOUTRESY_LINE_RE = re.compile(r"^(LayoutResY:\s*)(\d+)", re.IGNORECASE)
STYLE_FORMAT_LINE_RE = re.compile(r"^Format:\s*(.*)$")
STYLE_LINE_RE = re.compile(r"^(Style:\s*)(.*)$")
DIALOGUE_STYLE_RE = re.compile(r"^(?:Dialogue|Comment):\s*[^,]*,[^,]*,[^,]*,([^,]*),")

TAG_BLOCK_RE = re.compile(r"\{([^}]*)\}")
POS_TAG_RE = re.compile(r"\\pos\(\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*\)")
ORG_TAG_RE = re.compile(r"\\org\(\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*\)")
MOVE_ARGS_RE = re.compile(
    r"\\move\(\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*"
    r"(?:,\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*)?\)"
)
CLIP_FULL_RE = re.compile(r"\\(i?clip)\(([^)]*)\)")
CLIP_RECT_RE = re.compile(r"^[\d.+-]+(?:\s*,\s*[\d.+-]+){3}$")
CLIP_SCALE_PREFIX_RE = re.compile(r"^(\d+)\s*,\s*(.*)$", re.DOTALL)
FS_TAG_RE = re.compile(r"\\fs(?![cp])([\d.]+)")          # \fs<n>, not \fscx/\fscy/\fsp
BORD_TAG_RE = re.compile(r"\\(x?bord)([\d.]+)")
SHAD_TAG_RE = re.compile(r"\\(x?shad|y?shad)([\d.]+)")
FSP_TAG_RE = re.compile(r"\\fsp([\d.]+)")

# Style (v4+) fields that represent an absolute pixel *position* (need
# scale + offset) vs. a pixel *size* (need scale only).
STYLE_MARGIN_FIELDS_X = ("MarginL", "MarginR")
STYLE_MARGIN_FIELDS_Y = ("MarginV",)
STYLE_SIZE_FIELDS = ("Fontsize", "Outline", "Shadow", "Spacing")


def fmt_num(v: float) -> str:
    """Format a transformed coordinate/size compactly (no needless trailing zeros)."""
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.3f}".rstrip("0").rstrip(".")


def transform_clip_content(content: str, scale: float, offset_x: float, offset_y: float) -> Optional[str]:
    """
    Transform the argument list of a \\clip(...)/\\iclip(...) tag for a
    resolution change. Handles the two safe, unambiguous forms:
      - Rectangular: "x1,y1,x2,y2" -- transform both corner points.
      - Vector drawing path with NO leading scale argument, e.g.
        "m 100 100 l 200 100 200 200" -- every x/y in the path is in the
        same units as everything else, so each pair gets the same
        scale+offset transform as \\pos.
    A vector path WITH a leading drawing-scale argument (e.g.
    "\\clip(2,m 100 100 ...)") changes what unit the coordinates are in,
    which isn't safe to reinterpret generically, so that scale prefix is
    left untouched (only the coordinates after it are still transformed --
    they're still plain PlayRes-space coordinates regardless of the
    drawing scale, which only affects how finely curves are subdivided).
    Returns None if the content doesn't match either recognizable form
    (caller should treat this as "couldn't transform, leave as-is and warn").
    """
    stripped = content.strip()
    if not stripped:
        return None

    if CLIP_RECT_RE.match(stripped):
        parts = [p.strip() for p in stripped.split(",")]
        try:
            x1, y1, x2, y2 = (float(p) for p in parts)
        except ValueError:
            return None
        return (
            f"{fmt_num(x1 * scale + offset_x)},{fmt_num(y1 * scale + offset_y)},"
            f"{fmt_num(x2 * scale + offset_x)},{fmt_num(y2 * scale + offset_y)}"
        )

    scale_prefix = ""
    drawing = stripped
    m_scale = CLIP_SCALE_PREFIX_RE.match(stripped)
    if m_scale and re.search(r"[a-zA-Z]", m_scale.group(2)):
        scale_prefix = m_scale.group(1) + ","
        drawing = m_scale.group(2)

    if not re.search(r"[a-zA-Z]", drawing):
        return None  # not a drawing path and not a plain rect -- unrecognized form

    tokens = drawing.split()
    new_tokens: List[str] = []
    coord_index = 0
    for tok in tokens:
        try:
            val = float(tok)
        except ValueError:
            new_tokens.append(tok)  # drawing command letter (m/l/b/s/p/c...), unchanged
            continue
        if coord_index % 2 == 0:
            new_tokens.append(fmt_num(val * scale + offset_x))
        else:
            new_tokens.append(fmt_num(val * scale + offset_y))
        coord_index += 1

    if coord_index == 0 or coord_index % 2 != 0:
        return None  # no coordinates found, or an odd count -- malformed/unexpected

    return scale_prefix + " ".join(new_tokens)


def compute_letterbox_transform(
    old_res: Tuple[int, int], new_res: Tuple[int, int]
) -> Tuple[float, float, float]:
    """
    Given the subtitle's original resolution and the new video's
    resolution, compute (scale, offset_x, offset_y) assuming the original
    content is centered and letterboxed/pillarboxed (not cropped or
    stretched) to fit inside the new frame -- i.e. the standard "contain"
    fit. scale=1 and a pure offset (like your 1440x1080 -> 1920x1080
    pillarbox case) is the common special case where the new frame is
    simply padded without resizing the content at all.
    """
    old_w, old_h = old_res
    new_w, new_h = new_res
    scale = min(new_w / old_w, new_h / old_h)
    offset_x = (new_w - old_w * scale) / 2
    offset_y = (new_h - old_h * scale) / 2
    return scale, offset_x, offset_y


def compute_crop_transform(
    old_res: Tuple[int, int], content_res: Tuple[int, int], new_res: Tuple[int, int]
) -> Tuple[float, float, float]:
    """
    The opposite of compute_letterbox_transform: use this when the new
    video has padding REMOVED (cropped out) rather than added, e.g. going
    from a pillarboxed 1920x1080 frame back down to a 960x720 frame that
    holds just the actual content. `content_res` is the real content's
    size within the old (padded) frame -- content is assumed centered, as
    it is for any normal letterbox/pillarbox. The content rectangle is
    cropped out of old_res and rescaled to exactly fill new_res.

    Reuses the same (scale, offset_x, offset_y) representation as
    compute_letterbox_transform -- the offsets just come out negative
    here instead of positive, since coordinates are being shifted toward
    the frame origin (padding removed) rather than away from it.

    Raises ValueError if content_res's aspect ratio doesn't reasonably
    match new_res's -- that would mean new_res isn't actually "just the
    content, rescaled," and scale would have to differ in x vs y (this
    tool only supports uniform scaling, matching how \\fscx/\\fscy already
    work elsewhere in this script).
    """
    old_w, old_h = old_res
    content_w, content_h = content_res
    new_w, new_h = new_res
    scale_x = new_w / content_w
    scale_y = new_h / content_h
    if abs(scale_x - scale_y) > 0.01 * max(scale_x, scale_y):
        raise ValueError(
            f"--new-res {new_w}x{new_h} isn't the same aspect ratio as "
            f"--old-content-res {content_w}x{content_h} "
            f"(would need non-uniform x/y scaling: {scale_x:.4f} vs {scale_y:.4f})"
        )
    scale = (scale_x + scale_y) / 2
    crop_x = (old_w - content_w) / 2
    crop_y = (old_h - content_h) / 2
    offset_x = -crop_x * scale
    offset_y = -crop_y * scale
    return scale, offset_x, offset_y


def find_ass_play_res(lines: List[str]) -> Optional[Tuple[int, int]]:
    x = y = None
    for line in lines:
        mx = PLAYRESX_LINE_RE.match(line)
        if mx:
            x = int(mx.group(2))
        my = PLAYRESY_LINE_RE.match(line)
        if my:
            y = int(my.group(2))
        if x is not None and y is not None:
            return x, y
    return None


WRAPSTYLE_LINE_RE = re.compile(r"^WrapStyle:\s*(\d+)", re.IGNORECASE)


def find_ass_wrap_style(lines: List[str]) -> Optional[int]:
    """
    WrapStyle 0/1/3 all auto-wrap text to fit the available width (PlayResX
    - MarginL - MarginR) -- clamping a negative margin to 0 there just
    widens that available width, it can't cause text to run off the frame
    edges (though lines may wrap onto more lines than originally authored).
    WrapStyle 2 disables auto-wrapping entirely -- only an explicit \\N
    breaks a line -- so a long unbroken line genuinely can extend past the
    frame edges with no safety net. Returns None if not found (spec
    default is 0 in that case).
    """
    for line in lines:
        m = WRAPSTYLE_LINE_RE.match(line)
        if m:
            return int(m.group(1))
    return None


def extract_non_dialogue_lines(lines: List[str], dialogue_style_names: List[str]) -> Tuple[List[str], int]:
    """
    Build a "signs-only" variant of an ASS/SSA file: every non-Events
    line (Script Info, Styles, Format headers, etc.) is kept as-is so the
    result is still a complete, valid, self-contained file, but Events
    section Dialogue/Comment lines are kept only if their Style field is
    NOT in `dialogue_style_names` -- i.e. everything that isn't one of
    the show's known spoken-dialogue styles (signs, titles, karaoke, and
    any other named style) survives. Returns (new_lines, kept_count).
    """
    new_lines: List[str] = []
    kept = 0
    for line in lines:
        if line.startswith("Dialogue:") or line.startswith("Comment:"):
            m = DIALOGUE_STYLE_RE.match(line)
            style_name = m.group(1).strip() if m else None
            if style_name is not None and style_name in dialogue_style_names:
                continue  # this is a dialogue line -- drop it from the signs-only copy
            kept += 1
        new_lines.append(line)
    return new_lines, kept


def sync_styles(
    lines: List[str],
    style_names: List[str],
    fixed_fields: Dict[str, str],
    scaled_fields: Dict[str, float],
    scale: float,
) -> Tuple[List[str], List[Tuple[str, Dict[str, Tuple[str, str]]]]]:
    """
    Rewrite the named [V4+ Styles] entries in `style_names` so their
    fixed_fields match exactly and their scaled_fields match (scaled by
    `scale`, which the caller computes from this file's own PlayResY vs.
    the rule set's reference_res). Name, Italic, and Alignment are always
    preserved from the file's existing definition -- they're what make
    each named style distinct, not part of the "look."

    Returns (new_lines, changes) where changes is
    [(style_name, {field: (old_value, new_value)})] for every style that
    was actually found and touched, listing only fields whose value
    changed (so a style already matching the target shows an empty dict).
    """
    new_lines: List[str] = []
    changes: List[Tuple[str, Dict[str, Tuple[str, str]]]] = []
    style_fields: Optional[List[str]] = None

    for line in lines:
        fmt_m = STYLE_FORMAT_LINE_RE.match(line)
        if fmt_m and style_fields is None:
            candidate_fields = [f.strip() for f in fmt_m.group(1).split(",")]
            if "MarginL" in candidate_fields or "Fontsize" in candidate_fields:
                style_fields = candidate_fields
            new_lines.append(line)
            continue

        style_m = STYLE_LINE_RE.match(line)
        if style_m and style_fields:
            prefix, data = style_m.groups()
            parts = [p.strip() for p in data.split(",")]
            if len(parts) == len(style_fields):
                field_index = {name: i for i, name in enumerate(style_fields)}
                name_idx = field_index.get("Name")
                this_name = parts[name_idx] if name_idx is not None else None

                if this_name in style_names:
                    field_changes: Dict[str, Tuple[str, str]] = {}

                    for field, value in fixed_fields.items():
                        idx = field_index.get(field)
                        if idx is None:
                            continue
                        if parts[idx] != value:
                            field_changes[field] = (parts[idx], value)
                        parts[idx] = value

                    for field, ref_value in scaled_fields.items():
                        idx = field_index.get(field)
                        if idx is None:
                            continue
                        new_value = fmt_num(ref_value * scale)
                        if parts[idx] != new_value:
                            field_changes[field] = (parts[idx], new_value)
                        parts[idx] = new_value

                    changes.append((this_name, field_changes))
                    new_lines.append(prefix + ",".join(parts))
                    continue

        new_lines.append(line)

    return new_lines, changes


def reposition_ass_lines(
    lines: List[str],
    scale: float,
    offset_x: float,
    offset_y: float,
    new_res: Tuple[int, int],
) -> Tuple[
    List[str], Dict[str, int], List[Tuple[int, str]], List[Tuple[int, str, str]],
    List[Tuple[str, str, float, float]], List[Tuple[int, str, float, float]],
    Dict[str, List[int]],
]:
    """
    Transform an ASS/SSA file's lines for a resolution change:
      - PlayResX/PlayResY and LayoutResX/LayoutResY headers -> new_res
        (kept in lockstep -- a stale LayoutRes makes libass apply its own,
        possibly non-uniform, extra auto-scale on top of this transform)
      - Style Fontsize/Outline/Shadow/Spacing -> scaled
      - Style MarginL/MarginR/MarginV -> scaled + offset, clamped to 0 (and
        flagged) if that comes out negative -- which happens when a crop
        removes more width/height than the original margin accounted for,
        meaning that style's word-wrapped text was relying on space that
        no longer exists in the new frame
      - inline \\pos/\\org -> scaled + offset
      - inline \\fs/\\bord/\\xbord/\\shad/\\xshad/\\yshad/\\fsp -> scaled
      - inline \\move(x1,y1,x2,y2[,t1,t2]) -> both points scaled + offset,
        timing (if present) left untouched
      - inline \\clip(...)/\\iclip(...) -> transformed via
        transform_clip_content (rectangular form, or a vector drawing path
        with no leading scale argument); left untouched with a warning if
        the content doesn't match either recognizable form

    Every transformed \\pos and \\move endpoint is also checked against the
    new frame bounds [0, new_res] -- points landing outside it (most often
    from a crop that removed the part of the frame a sign/typesetting
    element was anchored to) are reported rather than silently accepted,
    since there's no way to automatically "fix" a position or word-wrap
    that no longer fits -- that needs a human decision (re-wrap the text
    ourselves, re-position it, or accept it'll be partly off-frame).

    Returns (new_lines, counts, warnings, complex_changes, margin_clamps, out_of_bounds):
      - counts tallies how many of each tag type were transformed.
      - warnings is [(line_no, raw_line)] for \\move/\\clip/\\iclip content
        that couldn't be confidently parsed and was left unchanged.
      - complex_changes is [(line_no, old_line, new_line)] for every line
        where a \\move or \\clip/\\iclip WAS transformed -- these involve
        more moving parts than a plain \\pos, so callers (dry-run in
        particular) should surface the before/after for manual spot-checking
        after the real merge.
      - margin_clamps is [(style_name, field_name, old_value, negative_value)]
        for every style margin that came out negative and was clamped to 0.
      - out_of_bounds is [(line_no, tag, x, y)] for every transformed \\pos
        or \\move endpoint that landed outside the new frame.
      - style_usage is {style_name: [line_no, ...]} for every Dialogue/Comment
        line, letting callers report which specific lines use a style that
        showed up in margin_clamps.
    """
    counts = {"playres": 0, "style_lines": 0, "pos": 0, "org": 0, "sizes": 0, "move": 0, "clip": 0}
    warnings: List[Tuple[int, str]] = []
    complex_changes: List[Tuple[int, str, str]] = []
    margin_clamps: List[Tuple[str, str, float, float]] = []
    out_of_bounds: List[Tuple[int, str, float, float]] = []
    style_usage: Dict[str, List[int]] = {}
    new_lines: List[str] = []
    style_fields: Optional[List[str]] = None

    def check_bounds(line_no: int, tag: str, x: float, y: float) -> None:
        if x < 0 or x > new_res[0] or y < 0 or y > new_res[1]:
            out_of_bounds.append((line_no, tag, x, y))

    for line_no, line in enumerate(lines):
        mx = PLAYRESX_LINE_RE.match(line)
        if mx:
            new_lines.append(f"{mx.group(1)}{new_res[0]}")
            counts["playres"] += 1
            continue
        my = PLAYRESY_LINE_RE.match(line)
        if my:
            new_lines.append(f"{my.group(1)}{new_res[1]}")
            counts["playres"] += 1
            continue
        lx = LAYOUTRESX_LINE_RE.match(line)
        if lx:
            new_lines.append(f"{lx.group(1)}{new_res[0]}")
            counts["playres"] += 1
            continue
        ly = LAYOUTRESY_LINE_RE.match(line)
        if ly:
            new_lines.append(f"{ly.group(1)}{new_res[1]}")
            counts["playres"] += 1
            continue

        fmt_m = STYLE_FORMAT_LINE_RE.match(line)
        if fmt_m and style_fields is None:
            # Only meaningful right after a "[V4+ Styles]"/"[V4 Styles]" section
            # header, but tracking the most recent Format: line is a reasonable
            # proxy without needing full section-aware parsing.
            candidate_fields = [f.strip() for f in fmt_m.group(1).split(",")]
            if "MarginL" in candidate_fields or "Fontsize" in candidate_fields:
                style_fields = candidate_fields
            new_lines.append(line)
            continue

        style_m = STYLE_LINE_RE.match(line)
        if style_m and style_fields:
            prefix, data = style_m.groups()
            parts = [p.strip() for p in data.split(",")]
            if len(parts) == len(style_fields):
                field_index = {name: i for i, name in enumerate(style_fields)}
                style_name = parts[field_index["Name"]] if "Name" in field_index else "?"

                def set_scaled(name: str) -> None:
                    idx = field_index.get(name)
                    if idx is None:
                        return
                    try:
                        parts[idx] = fmt_num(float(parts[idx]) * scale)
                    except ValueError:
                        pass

                def set_scaled_offset(name: str, offset: float) -> None:
                    idx = field_index.get(name)
                    if idx is None:
                        return
                    try:
                        old_val = float(parts[idx])
                    except ValueError:
                        return
                    new_val = old_val * scale + offset
                    if new_val < 0:
                        margin_clamps.append((style_name, name, old_val, new_val))
                        new_val = 0.0
                    parts[idx] = fmt_num(new_val)

                for name in STYLE_SIZE_FIELDS:
                    set_scaled(name)
                for name in STYLE_MARGIN_FIELDS_X:
                    set_scaled_offset(name, offset_x)
                for name in STYLE_MARGIN_FIELDS_Y:
                    set_scaled_offset(name, offset_y)

                new_lines.append(prefix + ",".join(parts))
                counts["style_lines"] += 1
                continue
            # Field count mismatch -- can't safely map, leave the style untouched.
            new_lines.append(line)
            continue

        if line.startswith("Dialogue:") or line.startswith("Comment:"):
            style_use_m = DIALOGUE_STYLE_RE.match(line)
            if style_use_m:
                style_usage.setdefault(style_use_m.group(1).strip(), []).append(line_no)

            had_move_or_clip = bool(MOVE_ARGS_RE.search(line) or CLIP_FULL_RE.search(line))
            line_had_failure = [False]  # mutable flag closures can set

            def repl_move(mm: "re.Match[str]") -> str:
                counts["move"] += 1
                x1 = float(mm.group(1)) * scale + offset_x
                y1 = float(mm.group(2)) * scale + offset_y
                x2 = float(mm.group(3)) * scale + offset_x
                y2 = float(mm.group(4)) * scale + offset_y
                check_bounds(line_no, "move (start)", x1, y1)
                check_bounds(line_no, "move (end)", x2, y2)
                if mm.group(5) is not None and mm.group(6) is not None:
                    return f"\\move({fmt_num(x1)},{fmt_num(y1)},{fmt_num(x2)},{fmt_num(y2)},{mm.group(5)},{mm.group(6)})"
                return f"\\move({fmt_num(x1)},{fmt_num(y1)},{fmt_num(x2)},{fmt_num(y2)})"

            def repl_clip(mm: "re.Match[str]") -> str:
                tag_name, content = mm.group(1), mm.group(2)
                new_content = transform_clip_content(content, scale, offset_x, offset_y)
                if new_content is None:
                    line_had_failure[0] = True
                    return mm.group(0)  # leave this specific tag unchanged
                counts["clip"] += 1
                return f"\\{tag_name}({new_content})"

            def repl_block(m: "re.Match[str]") -> str:
                content = m.group(1)

                def repl_pos(mm: "re.Match[str]") -> str:
                    counts["pos"] += 1
                    x = float(mm.group(1)) * scale + offset_x
                    y = float(mm.group(2)) * scale + offset_y
                    check_bounds(line_no, "pos", x, y)
                    return f"\\pos({fmt_num(x)},{fmt_num(y)})"

                def repl_org(mm: "re.Match[str]") -> str:
                    counts["org"] += 1
                    x = float(mm.group(1)) * scale + offset_x
                    y = float(mm.group(2)) * scale + offset_y
                    return f"\\org({fmt_num(x)},{fmt_num(y)})"

                def repl_size(mm: "re.Match[str]", tag: str, has_prefix: bool = False) -> str:
                    counts["sizes"] += 1
                    if has_prefix:
                        val = float(mm.group(2)) * scale
                        return f"\\{mm.group(1)}{fmt_num(val)}"
                    val = float(mm.group(1)) * scale
                    return f"\\{tag}{fmt_num(val)}"

                content = POS_TAG_RE.sub(repl_pos, content)
                content = ORG_TAG_RE.sub(repl_org, content)
                content = MOVE_ARGS_RE.sub(repl_move, content)
                content = CLIP_FULL_RE.sub(repl_clip, content)
                content = FS_TAG_RE.sub(lambda mm: repl_size(mm, "fs"), content)
                content = BORD_TAG_RE.sub(lambda mm: repl_size(mm, "", has_prefix=True), content)
                content = SHAD_TAG_RE.sub(lambda mm: repl_size(mm, "", has_prefix=True), content)
                content = FSP_TAG_RE.sub(lambda mm: repl_size(mm, "fsp"), content)
                return "{" + content + "}"

            new_line = TAG_BLOCK_RE.sub(repl_block, line)

            if line_had_failure[0]:
                warnings.append((line_no, line))
            elif had_move_or_clip and new_line != line:
                complex_changes.append((line_no, line, new_line))

            new_lines.append(new_line)
            continue

        new_lines.append(line)

    return new_lines, counts, warnings, complex_changes, margin_clamps, out_of_bounds, style_usage


def cmd_reposition(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    extracted_root = work_dir / EXTRACTED_DIRNAME
    if not extracted_root.exists():
        sys.exit(f"No extracted subtitles found at {extracted_root}. Run the 'extract' stage first.")
    mkv_dirs = sorted(d for d in extracted_root.iterdir() if d.is_dir())
    if not mkv_dirs:
        sys.exit(f"No extracted subtitle folders found under {extracted_root}.")

    new_res = args.new_res
    forced_old_res = args.old_res

    repositioned_root = work_dir / REPOSITIONED_DIRNAME
    edited_paths: Dict[str, Path] = {}
    any_repositioned = False
    total_warnings: List[Tuple[str, int, str]] = []
    total_complex_changes: List[Tuple[str, int, str, str]] = []
    total_margin_clamps: List[Tuple[str, str, str, float, float, int, str, str, str, Optional[int]]] = []
    total_out_of_bounds: List[Tuple[str, int, str, float, float]] = []

    for mkv_dir in mkv_dirs:
        meta_path = mkv_dir / TRACKS_META_FILENAME
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(c(f"== {meta.get('source_name', mkv_dir.name)} ==", C.BOLD, C.BLUE))

        editable_tracks = [t for t in meta["tracks"] if t["editable"]]
        matching_tracks = [
            t for t in editable_tracks
            if track_matches_filter(t["id"], t.get("track_name", ""), args.track_id, args.track_name)
        ]
        if editable_tracks and not matching_tracks and (args.track_id or args.track_name):
            print(c("  no tracks match --track-id/--track-name; skipped.", C.YELLOW))
            print()
            continue

        for t in matching_tracks:
            rel = t["file"]
            src_path = work_dir / rel
            ext = src_path.suffix.lower()
            label = t.get("track_name") or f"track {t['id']}"

            if ext not in (".ass", ".ssa"):
                print(f"  track {t['id']} ({label}): {ext} has no absolute positioning to adjust, skipped.")
                continue

            text, encoding = read_text_smart(src_path)
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines(keepends=False)

            old_res = forced_old_res or find_ass_play_res(lines)
            if old_res is None:
                print(c(f"  track {t['id']} ({label}): no PlayResX/PlayResY found and no --old-res "
                      "given; skipped.", C.YELLOW))
                continue
            wrap_style = find_ass_wrap_style(lines)

            if args.old_content_res:
                try:
                    scale, offset_x, offset_y = compute_crop_transform(old_res, args.old_content_res, new_res)
                except ValueError as e:
                    print(c(f"  track {t['id']} ({label}): {e}; skipped.", C.YELLOW))
                    continue
            else:
                scale, offset_x, offset_y = compute_letterbox_transform(old_res, new_res)
            new_lines, counts, warnings, complex_changes, margin_clamps, out_of_bounds, style_usage = reposition_ass_lines(
                lines, scale, offset_x, offset_y, new_res
            )

            for line_no, raw_line in warnings:
                total_warnings.append((rel, line_no + 1, raw_line))
            for line_no, old_line, new_line in complex_changes:
                total_complex_changes.append((rel, line_no + 1, old_line, new_line))

            # Merge MarginL/MarginR clamps for the same style into one entry
            # when their values match (always true for a symmetric crop),
            # and drop clamps for styles nothing actually uses -- both are
            # pure noise, not information loss.
            merged_clamps: Dict[Tuple[str, float, float], List[str]] = {}
            for style_name, field_name, old_val, neg_val in margin_clamps:
                if not style_usage.get(style_name):
                    continue
                merged_clamps.setdefault((style_name, old_val, neg_val), []).append(field_name)

            for (style_name, old_val, neg_val), field_names in merged_clamps.items():
                affected = sorted(style_usage[style_name])
                line_range = f"{affected[0] + 1}-{affected[-1] + 1}" if len(affected) > 1 else str(affected[0] + 1)
                ts_first = get_timestamp(lines, affected[0], ext)
                ts_last = get_timestamp(lines, affected[-1], ext)
                total_margin_clamps.append((
                    rel, style_name, "/".join(field_names), old_val, neg_val,
                    len(affected), line_range, ts_first, ts_last, wrap_style,
                ))
            for line_no, tag, x, y in out_of_bounds:
                total_out_of_bounds.append((rel, line_no + 1, tag, x, y))

            if warnings:
                print(c(f"  track {t['id']} ({label}): {len(warnings)} line(s) with \\move/\\clip/\\iclip "
                      "-- NOT adjusted, needs manual review (see warnings below).", C.YELLOW))
            if complex_changes:
                print(c(f"  track {t['id']} ({label}): {len(complex_changes)} \\move/\\clip/\\iclip line(s) "
                      "adjusted -- worth spot-checking (see list below).", C.MAGENTA))
            if margin_clamps:
                any_no_wrap = wrap_style == 2
                overflow_note = (
                    "text can genuinely extend past the frame edges (WrapStyle 2, no auto-wrap)"
                    if any_no_wrap else
                    "text will still auto-wrap but onto more lines than before"
                )
                print(c(f"  track {t['id']} ({label}): {len(margin_clamps)} style margin(s) went negative "
                      f"and were clamped to 0 (see list below) -- {overflow_note}.", C.YELLOW))
            if out_of_bounds:
                print(c(f"  track {t['id']} ({label}): {len(out_of_bounds)} \\pos/\\move point(s) landed "
                      "outside the new frame (see list below).", C.YELLOW))

            transformed_anything = counts["style_lines"] or counts["pos"] or counts["org"] or counts["sizes"] or counts["move"] or counts["clip"]
            if not transformed_anything and not warnings:
                print(f"  track {t['id']} ({label}): nothing to adjust.")
                continue

            print(c(
                f"  track {t['id']} ({label}): {old_res[0]}x{old_res[1]} -> {new_res[0]}x{new_res[1]}"
                f"  (scale {scale:.3f}, offset {offset_x:+.1f},{offset_y:+.1f})"
                f"  -- {counts['style_lines']} style(s), {counts['pos']} \\pos, {counts['org']} \\org, "
                f"{counts['move']} \\move, {counts['clip']} \\clip/\\iclip, {counts['sizes']} size tag(s) adjusted.",
                C.GREEN,
            ))

            new_text = newline.join(new_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
            dest_path = repositioned_root / rel
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(new_text, encoding="utf-8")
            edited_paths[rel] = dest_path
            any_repositioned = True
        print()

    if args.condensed:
        files_with_items: Dict[str, Dict[str, Any]] = {}

        def file_entry(rel: str) -> Dict[str, Any]:
            return files_with_items.setdefault(rel, {"complex": 0, "clamped_styles": set(), "oob": 0, "warn": 0})

        for rel, *_ in total_complex_changes:
            file_entry(rel)["complex"] += 1
        for rel, style_name, *_ in total_margin_clamps:
            file_entry(rel)["clamped_styles"].add(style_name)
        for rel, *_ in total_out_of_bounds:
            file_entry(rel)["oob"] += 1
        for rel, *_ in total_warnings:
            file_entry(rel)["warn"] += 1

        if files_with_items:
            print(c(f"\n{len(files_with_items)} file(s) have items worth reviewing:", C.BOLD, C.YELLOW))
            for rel in sorted(files_with_items):
                s = files_with_items[rel]
                parts = []
                if s["complex"]:
                    parts.append(f"{s['complex']} \\move/\\clip adjusted")
                if s["clamped_styles"]:
                    parts.append(f"{len(s['clamped_styles'])} style(s) clamped ({', '.join(sorted(s['clamped_styles']))})")
                if s["oob"]:
                    parts.append(f"{s['oob']} pos/move out of bounds")
                if s["warn"]:
                    parts.append(f"{s['warn']} unparseable move/clip (needs manual review)")
                print(c(f"  {rel}", C.CYAN) + ": " + "; ".join(parts))
            print()
    else:
        if total_complex_changes:
            print(c(
                f"\n{len(total_complex_changes)} \\move/\\clip/\\iclip line(s) were adjusted -- "
                "spot-check these after merging (timed motion and drawn masks have more moving parts "
                "than a plain \\pos):",
                C.BOLD, C.MAGENTA,
            ))
            for rel, line_no, old_line, new_line in total_complex_changes:
                print(c(f"  {rel}:{line_no}", C.CYAN))
                print(f"    {c('old:', C.RED)} {old_line.strip()}")
                print(f"    {c('new:', C.GREEN)} {new_line.strip()}")
            print()

        if total_margin_clamps:
            no_wrap_present = any(ws == 2 for *_, ws in total_margin_clamps)
            if no_wrap_present:
                header = (
                    f"\n{len(total_margin_clamps)} style margin(s) went negative after the transform and were "
                    "clamped to 0. At least one affected file has WrapStyle 2 (word-wrap disabled), where this "
                    "genuinely means long lines in that style can extend past the frame edges with no automatic "
                    "wrapping to catch them -- worth checking closely for those files (noted below):"
                )
            else:
                header = (
                    f"\n{len(total_margin_clamps)} style margin(s) went negative after the transform and were "
                    "clamped to 0. All affected files auto-wrap (WrapStyle 0/1/3), so text will still break onto "
                    "more lines automatically rather than run off the frame edges -- but since the effective wrap "
                    "width shrank a lot, captions may now wrap onto noticeably more lines than originally "
                    "authored (taller, more cramped blocks), which is worth a visual spot-check:"
                )
            print(c(header, C.BOLD, C.YELLOW))
            for rel, style_name, field_name, old_val, neg_val, count, line_range, ts_first, ts_last, wrap_style in total_margin_clamps:
                ts_note = ""
                if ts_first and ts_last:
                    ts_note = f"  [{ts_first}]" if ts_first == ts_last else f"  [{ts_first} .. {ts_last}]"
                wrap_note = "  [WrapStyle 2: NO auto-wrap]" if wrap_style == 2 else ""
                print(
                    c(f"  {rel}", C.CYAN)
                    + f"  style '{style_name}' {field_name}: {fmt_num(old_val)} -> would be {fmt_num(neg_val)}, clamped to 0"
                    + c(wrap_note, C.RED)
                )
                print(f"      affects {count} line(s): {line_range}{ts_note}")
            print()

        if total_out_of_bounds:
            print(c(
                f"\n{len(total_out_of_bounds)} \\pos/\\move point(s) landed outside the new "
                f"{new_res[0]}x{new_res[1]} frame after the transform -- most often this means a sign/"
                "typesetting element was anchored to a part of the picture that got cropped away. These "
                "still need a manual decision (reposition, or accept it'll render partly/fully off-frame):",
                C.BOLD, C.YELLOW,
            ))
            for rel, line_no, tag, x, y in total_out_of_bounds:
                print(c(f"  {rel}:{line_no}", C.CYAN) + f"  \\{tag}: ({fmt_num(x)}, {fmt_num(y)})")
            print()

        if total_warnings:
            print(c(f"\n{len(total_warnings)} line(s) need manual review (contain \\move/\\clip/\\iclip "
                  "that couldn't be parsed and were left unchanged):", C.BOLD, C.YELLOW))
            for rel, line_no, raw_line in total_warnings:
                print(c(f"  {rel}:{line_no}", C.CYAN))
                print(f"    {raw_line.strip()}")
            print()

    if not any_repositioned:
        print(c("No subtitle tracks needed repositioning.", C.BOLD, C.GREEN))
        return

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run)


# --------------------------------------------------------------------------
# STAGE (optional): style-sync -- make named styles match a per-show look
# --------------------------------------------------------------------------

def cmd_style_sync(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    extracted_root = work_dir / EXTRACTED_DIRNAME
    if not extracted_root.exists():
        sys.exit(f"No extracted subtitles found at {extracted_root}. Run the 'extract' stage first.")
    mkv_dirs = sorted(d for d in extracted_root.iterdir() if d.is_dir())
    if not mkv_dirs:
        sys.exit(f"No extracted subtitle folders found under {extracted_root}.")

    show_key = args.show.strip().lower()
    if show_key not in STYLE_SYNC_RULES:
        available = ", ".join(sorted(STYLE_SYNC_RULES))
        sys.exit(f"Unknown --show '{args.show}'. Available shows: {available}")
    rule = STYLE_SYNC_RULES[show_key]
    reference_res = rule["reference_res"]
    style_names = rule["style_names"]
    fixed_fields = rule["fixed_fields"]
    scaled_fields = rule["scaled_fields"]
    required_fonts = rule.get("fonts", [])

    attach_files: List[Path] = []
    if required_fonts:
        fonts_dir = Path(args.fonts_dir).resolve() if args.fonts_dir else default_fonts_dir()
        if not fonts_dir.exists():
            print(c(f"WARNING: fonts directory not found ({fonts_dir}); "
                  f"required font(s) for '{show_key}' will NOT be attached: {', '.join(required_fonts)}",
                  C.YELLOW))
        else:
            missing_fonts = []
            for font_name in required_fonts:
                font_path = fonts_dir / font_name
                if font_path.exists():
                    attach_files.append(font_path)
                else:
                    missing_fonts.append(font_name)
            if attach_files:
                print(f"Will attach {len(attach_files)} font file(s) from {fonts_dir}: "
                      f"{', '.join(f.name for f in attach_files)}")
            if missing_fonts:
                print(c(f"WARNING: font file(s) not found in {fonts_dir}, will NOT be attached: "
                      f"{', '.join(missing_fonts)}", C.YELLOW))
        print()

    synced_root = work_dir / STYLE_SYNCED_DIRNAME
    signs_root = work_dir / STYLE_SYNCED_DIRNAME / "signs"
    edited_paths: Dict[str, Path] = {}
    added_subs_by_mkv: Dict[str, List[Dict[str, Any]]] = {}
    any_synced = False
    any_signs_generated = False

    for mkv_dir in mkv_dirs:
        meta_path = mkv_dir / TRACKS_META_FILENAME
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(c(f"== {meta.get('source_name', mkv_dir.name)} ==", C.BOLD, C.BLUE))

        editable_tracks = [t for t in meta["tracks"] if t["editable"]]
        matching_tracks = [
            t for t in editable_tracks
            if track_matches_filter(t["id"], t.get("track_name", ""), args.track_id, args.track_name)
        ]
        if editable_tracks and not matching_tracks and (args.track_id or args.track_name):
            print(c("  no tracks match --track-id/--track-name; skipped.", C.YELLOW))
            print()
            continue

        for t in matching_tracks:
            rel = t["file"]
            src_path = work_dir / rel
            ext = src_path.suffix.lower()
            label = t.get("track_name") or f"track {t['id']}"

            if ext not in (".ass", ".ssa"):
                print(f"  track {t['id']} ({label}): {ext} has no [V4+ Styles] section to sync, skipped.")
                continue

            text, encoding = read_text_smart(src_path)
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines(keepends=False)

            play_res = find_ass_play_res(lines)
            if play_res is None:
                print(c(f"  track {t['id']} ({label}): no PlayResX/PlayResY found; skipped.", C.YELLOW))
                continue
            scale = play_res[1] / reference_res[1]

            new_lines, changes = sync_styles(lines, style_names, fixed_fields, scaled_fields, scale)

            found_names = {name for name, _ in changes}
            missing = [n for n in style_names if n not in found_names]
            touched = [(name, fc) for name, fc in changes if fc]

            if missing:
                print(c(f"  track {t['id']} ({label}): style(s) not found in this file, skipped: "
                      f"{', '.join(missing)}", C.DIM))

            if touched:
                print(c(
                    f"  track {t['id']} ({label}): {play_res[0]}x{play_res[1]} vs. reference "
                    f"{reference_res[0]}x{reference_res[1]}  (scale {scale:.3f}) -- "
                    f"{len(touched)}/{len(found_names)} style(s) updated:",
                    C.GREEN,
                ))
                for name, field_changes in touched:
                    field_strs = [f"{f}: {old} -> {new}" for f, (old, new) in field_changes.items()]
                    print(f"      {c(name, C.CYAN)}  " + ", ".join(field_strs))

                new_text = newline.join(new_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
                dest_path = synced_root / rel
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_text(new_text, encoding="utf-8")
                edited_paths[rel] = dest_path
                any_synced = True
            else:
                print(f"  track {t['id']} ({label}): {len(found_names)} style(s) already match, nothing to sync.")

            if args.generate_signs_track:
                signs_lines, kept = extract_non_dialogue_lines(new_lines, style_names)
                if kept == 0:
                    print(f"  track {t['id']} ({label}): no non-dialogue-style lines found, no signs track generated.")
                else:
                    signs_text = newline.join(signs_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
                    signs_dest = signs_root / rel
                    signs_dest.parent.mkdir(parents=True, exist_ok=True)
                    signs_dest.write_text(signs_text, encoding="utf-8")

                    signs_label = f"{label} - Signs" if label and label != f"track {t['id']}" else "Signs"
                    added_subs_by_mkv.setdefault(mkv_dir.name, []).append({
                        "path": signs_dest,
                        "language": t.get("language_ietf") or t.get("language") or "und",
                        "track_name": signs_label,
                        "default": False,
                        "forced": False,
                    })
                    any_signs_generated = True
                    print(c(f"  track {t['id']} ({label}): generated signs track "
                          f"'{signs_label}' with {kept} line(s).", C.MAGENTA))
        print()

    if not any_synced and not any_signs_generated and not attach_files:
        print(c("No subtitle tracks needed style syncing, and no signs track was generated.", C.BOLD, C.GREEN))
        return

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run,
                      added_subs_by_mkv, attach_files)


def cmd_plan(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir).resolve()
    extracted_root = work_dir / EXTRACTED_DIRNAME
    if not extracted_root.exists():
        sys.exit(f"No extracted subtitles found at {extracted_root}. Run the 'extract' stage first.")

    show_key = args.show.strip().lower()
    if show_key not in SHOW_RULES:
        available = ", ".join(sorted(SHOW_RULES))
        sys.exit(f"Unknown --show '{args.show}'. Available shows: {available}")
    show_config = SHOW_RULES[show_key]

    replacements = dict(show_config["replacements"])
    width_compensated_rules = show_config["width_compensated_rules"]
    watch_word = show_config["watch_word"]
    if args.rules_file:
        extra = json.loads(Path(args.rules_file).read_text(encoding="utf-8"))
        replacements.update(extra)

    sub_files = sorted(
        p for p in extracted_root.rglob("*")
        if p.is_file() and p.suffix.lower() in TEXT_EDITABLE_EXTS
    )
    if not sub_files:
        sys.exit(f"No text-editable subtitle files found under {extracted_root}.")

    plan_entries: List[Dict[str, Any]] = []
    log_lines: List[str] = []
    session_watchword_answers: Dict[str, str] = {}  # exact text -> chosen replacement

    print(c(f"Scanning {len(sub_files)} subtitle file(s) for replacements...\n", C.BOLD))

    for sub_path in sub_files:
        rel = sub_path.relative_to(work_dir)
        text, encoding = read_text_smart(sub_path)
        lines = text.splitlines(keepends=False)
        ext = sub_path.suffix.lower()

        header = f"== {rel} =="
        print(c(header, C.BOLD, C.BLUE))
        log_lines.append(("\n" if log_lines else "") + header)

        records: List[Dict[str, Any]] = []

        for line_no, prefix, segment, suffix in iter_text_segments(lines, ext):
            is_ass = ext in (".ass", ".ssa")
            if is_ass:
                leading_tags, visible = split_leading_ass_tags(segment)
                search_text = strip_inline_ass_tags(visible)
            else:
                leading_tags, search_text = "", segment

            new_search_text, matched_keys = apply_known_replacements(search_text, replacements)
            watchword_hit = re.search(re.escape(watch_word), new_search_text, re.IGNORECASE)
            timestamp = get_timestamp(lines, line_no, ext)

            if matched_keys:
                width_tag = ""
                scale = None
                if is_ass and any(k in width_compensated_rules for k in matched_keys):
                    scale = compute_width_compensation(search_text, new_search_text)
                    if scale is not None:
                        width_tag = f"{{\\fscx{scale}}}"

                new_line = prefix + leading_tags + width_tag + new_search_text + suffix
                rules_str = ", ".join(f"'{k}' -> '{replacements[k]}'" for k in matched_keys)
                plan_entries.append(
                    {
                        "file": str(rel),
                        "line_no": line_no,
                        "old_line": lines[line_no],
                        "new_line": new_line,
                        "reason": "rule:" + ",".join(matched_keys),
                    }
                )
                records.append(
                    {
                        "kind": "MATCH",
                        "line_no": line_no,
                        "timestamp": timestamp,
                        "old": search_text.strip(),
                        "new": new_search_text.strip(),
                        "note": rules_str,
                        "scale": scale,
                    }
                )
                continue

            if watchword_hit:
                # Contains the show's watch word but not covered by a known rule -> ask the user.
                original_search_text = search_text
                if original_search_text in session_watchword_answers:
                    chosen = session_watchword_answers[original_search_text]
                else:
                    print(c(f"\n--- Unmapped line containing '{watch_word}' ---", C.BOLD, C.YELLOW))
                    print(f"{c('File:', C.DIM)} {rel}")
                    print(f"{c('Line:', C.DIM)} {line_no + 1}")
                    if timestamp:
                        print(f"{c('Time:', C.DIM)} {timestamp}")
                    print(f"{c('Text:', C.DIM)} {original_search_text.strip()}")
                    if is_ass and leading_tags:
                        print(c("(ASS override tags around this text are preserved automatically)", C.DIM))
                    chosen = input(
                        c("Replacement text (leave empty to skip this line): ", C.YELLOW)
                    ).strip()
                    session_watchword_answers[original_search_text] = chosen

                if not chosen:
                    records.append(
                        {
                            "kind": "SKIP",
                            "line_no": line_no,
                            "timestamp": timestamp,
                            "old": original_search_text.strip(),
                            "new": "",
                            "note": "(no replacement given)",
                            "scale": None,
                        }
                    )
                    continue

                width_tag = ""
                scale = None
                if is_ass:
                    scale = compute_width_compensation(original_search_text, chosen)
                    if scale is not None:
                        width_tag = f"{{\\fscx{scale}}}"

                new_line = prefix + leading_tags + width_tag + chosen + suffix
                plan_entries.append(
                    {
                        "file": str(rel),
                        "line_no": line_no,
                        "old_line": lines[line_no],
                        "new_line": new_line,
                        "reason": f"watchword:{watch_word}",
                    }
                )
                records.append(
                    {
                        "kind": "ASKED",
                        "line_no": line_no,
                        "timestamp": timestamp,
                        "old": original_search_text.strip(),
                        "new": chosen.strip(),
                        "note": "",
                        "scale": scale,
                    }
                )

        flush_match_records(rel, records, log_lines)
        print()

    plan_path = work_dir / PLAN_FILENAME
    plan_path.write_text(json.dumps(plan_entries, indent=2), encoding="utf-8")
    (work_dir / PLAN_LOG_FILENAME).write_text("\n".join(log_lines), encoding="utf-8")

    print(c(f"\nPlan complete: {len(plan_entries)} replacement(s) queued.", C.BOLD, C.GREEN))
    print(f"  Plan file: {plan_path}")
    print(f"  Log file:  {work_dir / PLAN_LOG_FILENAME}")


# --------------------------------------------------------------------------
# STAGE 3: merge
# --------------------------------------------------------------------------

def apply_plan_to_files(work_dir: Path, plan_entries: List[Dict[str, Any]]) -> Dict[str, Path]:
    """
    Writes edited copies of every subtitle file touched by the plan into
    work_dir/EDITED_DIRNAME, preserving the extracted/ directory layout.
    Returns a map of relative-path-string -> edited file Path.
    """
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for entry in plan_entries:
        by_file.setdefault(entry["file"], []).append(entry)

    edited_root = work_dir / EDITED_DIRNAME
    edited_paths: Dict[str, Path] = {}

    for rel_str, entries in by_file.items():
        src_path = work_dir / rel_str
        text, encoding = read_text_smart(src_path)
        lines = text.splitlines(keepends=False)
        newline = "\r\n" if "\r\n" in text else "\n"

        applied, mismatched = 0, 0
        for e in entries:
            ln = e["line_no"]
            if 0 <= ln < len(lines) and lines[ln] == e["old_line"]:
                lines[ln] = e["new_line"]
                applied += 1
            else:
                mismatched += 1
                print(c(
                    f"  WARNING: {rel_str}:{ln + 1} no longer matches the planned "
                    "original text; skipping this replacement (re-run 'plan' if the "
                    "subtitle files changed).", C.YELLOW
                ))

        dest_path = edited_root / rel_str
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(newline.join(lines) + (newline if text.endswith(("\n", "\r\n")) else ""),
                              encoding="utf-8")
        edited_paths[rel_str] = dest_path
        print(f"  {rel_str}: applied {applied}/{len(entries)} replacement(s)"
              + (f", {mismatched} skipped" if mismatched else ""))

    return edited_paths


def build_mkvmerge_command(
    source_mkv: Path,
    tracks_meta: List[Dict[str, Any]],
    replaced_track_ids: Dict[int, Path],
    title: str,
    output_path: Path,
    added_subs: Optional[List[Dict[str, Any]]] = None,
    attach_files: Optional[List[Path]] = None,
) -> List[str]:
    """
    Constructs an mkvmerge command that reproduces `source_mkv` but with:
      - the subtitle tracks in `replaced_track_ids` (id -> edited file
        path) swapped for externally supplied files, preserving
        language/name/default/forced flags and original track order,
      - each entry in `added_subs` (optional) appended as a brand-new
        subtitle track that didn't exist in the source at all -- each a
        dict with "path" (Path), "language", "track_name", "default"
        (bool), "forced" (bool), and
      - each file in `attach_files` (optional) added as a new font/file
        attachment (mkvmerge auto-detects the right mimetype for common
        font extensions from the filename), alongside source_mkv's own
        existing attachments, which are kept automatically by default.
    All three can be empty/omitted; at least one of replaced_track_ids,
    added_subs, or attach_files must be non-empty for there to be any
    reason to call this (the caller is expected to short-circuit a no-op
    case to a plain copy).
    """
    cmd: List[str] = ["mkvmerge", "-o", str(output_path)]

    if title:
        cmd += ["--title", title]

    # Exclude the replaced subtitle tracks from the main source file; keep
    # everything else (video, audio, untouched subtitles, attachments,
    # chapters, tags) exactly as-is by default. Only needed when something
    # is actually being replaced -- an add-only run (new signs track, no
    # replacements) keeps every original track as-is.
    # mkvmerge syntax: -s !2,3  (exclude these subtitle track IDs, keep the rest)
    if replaced_track_ids:
        cmd += ["-s", "!" + ",".join(str(tid) for tid in sorted(replaced_track_ids))]
    cmd.append(str(source_mkv))

    # Each replaced track is appended as its own external-file input, in
    # its original track ID order, carrying over language/name/flags.
    # NOTE: mkvmerge places appended files' tracks after the source file's
    # remaining tracks, so replaced subtitle tracks may shift to the end of
    # the track list (e.g. in a player's subtitle menu). Their language,
    # name, default/forced flags, and content are otherwise identical to
    # the originals they replace.
    for tid in sorted(replaced_track_ids):
        meta = next(t for t in tracks_meta if t["id"] == tid)
        lang = meta.get("language_ietf") or meta.get("language") or "und"
        name = meta.get("track_name", "")
        default_flag = "yes" if meta.get("default_track") else "no"
        forced_flag = "yes" if meta.get("forced_track") else "no"
        edited_path = replaced_track_ids[tid]

        cmd += ["--language", f"0:{lang}"]
        if name:
            cmd += ["--track-name", f"0:{name}"]
        cmd += ["--default-track-flag", f"0:{default_flag}"]
        cmd += ["--forced-display-flag", f"0:{forced_flag}"]
        cmd += ["--sub-charset", "0:UTF-8"]
        cmd.append(str(edited_path))

    # Brand-new tracks that didn't exist in the source at all.
    for sub in added_subs or []:
        cmd += ["--language", f"0:{sub.get('language') or 'und'}"]
        if sub.get("track_name"):
            cmd += ["--track-name", f"0:{sub['track_name']}"]
        cmd += ["--default-track-flag", f"0:{'yes' if sub.get('default') else 'no'}"]
        cmd += ["--forced-display-flag", f"0:{'yes' if sub.get('forced') else 'no'}"]
        cmd += ["--sub-charset", "0:UTF-8"]
        cmd.append(str(sub["path"]))

    # Extra font (or other) files to attach, on top of whatever attachments
    # source_mkv already carries (mkvmerge keeps those by default).
    for font_path in attach_files or []:
        mime_type = FONT_MIME_TYPES.get(font_path.suffix.lower())
        if mime_type:
            cmd += ["--attachment-mime-type", mime_type]
        cmd += ["--attach-file", str(font_path)]

    return cmd


def rebuild_all_mkvs(
    work_dir: Path,
    input_dir: Path,
    output_dir: Path,
    edited_paths: Dict[str, Path],
    in_place: bool,
    dry_run: bool = False,
    added_subs_by_mkv: Optional[Dict[str, List[Dict[str, Any]]]] = None,
    attach_files: Optional[List[Path]] = None,
) -> None:
    """
    Shared rebuild step used by 'merge' (edited subtitle files come from
    applying a replacement plan), 'trim' (edited subtitle files come from
    cutting entries past the video's duration), 'reposition' (edited
    subtitle files come from adjusting coordinates for a resolution
    change), and 'style-sync' (edited subtitle files come from syncing
    named styles, optionally with a new signs-only track generated
    alongside via `added_subs_by_mkv`). For every extracted mkv, swaps in
    whichever subtitle tracks have an edited file in `edited_paths`
    (keyed by the track's relative path under work_dir), appends any
    brand-new tracks listed for that mkv in `added_subs_by_mkv` (keyed by
    the mkv's extracted-subfolder name, i.e. mkv_dir.name), and leaves
    everything else untouched, writing the result to output_dir (or
    moving it over the original with --in-place). With dry_run=True,
    prints what would happen but never touches input_dir or output_dir --
    no mkvmerge invocation, no file copies, no --in-place move. The
    edited/added subtitle files themselves have already been written to
    the work directory by this point regardless, so they're still there
    to inspect.
    """
    extracted_root = work_dir / EXTRACTED_DIRNAME
    mkv_dirs = sorted(d for d in extracted_root.iterdir() if d.is_dir())

    for mkv_dir in mkv_dirs:
        meta_path = mkv_dir / TRACKS_META_FILENAME
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        source_mkv = Path(meta["source_mkv"])
        if not source_mkv.exists():
            # fall back to matching by filename inside input_dir, in case the
            # tree was moved since extraction.
            candidate = input_dir / meta["source_name"]
            if candidate.exists():
                source_mkv = candidate
            else:
                print(c(f"WARNING: source file for {mkv_dir.name} not found "
                      f"({source_mkv}); skipping.", C.YELLOW))
                continue

        tracks_meta = meta["tracks"]

        replaced_track_ids: Dict[int, Path] = {}
        for t in tracks_meta:
            rel = t["file"]
            if rel in edited_paths:
                replaced_track_ids[t["id"]] = edited_paths[rel]

        added_subs = (added_subs_by_mkv or {}).get(mkv_dir.name, [])

        output_path = output_dir / source_mkv.name
        prefix = c("[DRY RUN] would ", C.MAGENTA) if dry_run else ""

        if not replaced_track_ids and not added_subs and not attach_files:
            print(f"{prefix}{source_mkv.name}: no subtitle changes, {'copy' if dry_run else 'copying'} through unchanged.")
            if not dry_run:
                shutil.copy2(source_mkv, output_path)
            continue

        action_parts = []
        if replaced_track_ids:
            action_parts.append(f"{len(replaced_track_ids)} subtitle track(s) replaced")
        if added_subs:
            action_parts.append(f"{len(added_subs)} new subtitle track(s) added")
        if attach_files:
            action_parts.append(f"{len(attach_files)} font file(s) attached")
        print(f"{prefix}{source_mkv.name}: {'rebuild' if dry_run else 'rebuilding'} with "
              f"{', '.join(action_parts)} -> {output_path.name}")
        if dry_run:
            continue

        cmd = build_mkvmerge_command(
            source_mkv=source_mkv,
            tracks_meta=tracks_meta,
            replaced_track_ids=replaced_track_ids,
            title=meta.get("title", ""),
            output_path=output_path,
            added_subs=added_subs,
            attach_files=attach_files,
        )
        run(cmd)

    if dry_run:
        print(c("\nDry run complete. No .mkv files were written or modified.", C.BOLD, C.MAGENTA))
        print(f"Edited subtitle files (for inspection only) are under: {work_dir}")
        return

    print(f"\nDone. Rebuilt files written to: {output_dir}")
    if in_place:
        print("Replacing originals (--in-place)...")
        for out_file in output_dir.glob("*.mkv"):
            target = input_dir / out_file.name
            backup = target.with_suffix(target.suffix + ".bak")
            if target.exists() and not backup.exists():
                shutil.move(str(target), str(backup))
            shutil.move(str(out_file), str(target))
        print("Originals replaced (backups saved as *.mkv.bak).")


def cmd_merge(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    plan_path = work_dir / PLAN_FILENAME
    if not plan_path.exists():
        sys.exit(f"No plan file found at {plan_path}. Run the 'plan' stage first.")
    plan_entries = json.loads(plan_path.read_text(encoding="utf-8"))

    if not plan_entries:
        print("Plan is empty - nothing to merge. Copying source files through unchanged is skipped; "
              "run 'extract'/'plan' again if you expected replacements.")
        return

    print("Applying plan to subtitle files...")
    edited_paths = apply_plan_to_files(work_dir, plan_entries)
    print()

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_resolution(value: str) -> Tuple[int, int]:
    m = re.match(r"^(\d+)\s*[xX]\s*(\d+)$", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"expected WxH (e.g. 1920x1080), got '{value}'")
    return int(m.group(1)), int(m.group(2))


def parse_int_list(value: str) -> List[int]:
    try:
        return [int(v.strip()) for v in value.split(",") if v.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected comma-separated integers (e.g. 2,3), got '{value}'")


def parse_str_list(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def track_matches_filter(track_id: int, track_name: str, id_filter: Optional[List[int]],
                          name_filter: Optional[List[str]]) -> bool:
    """
    True if a track passes the given --track-id/--track-name filters.
    Each filter is OR'd within itself (any listed id, any listed name
    substring) but AND'd against the other filter if both are given.
    No filters given at all -> everything matches (default: all tracks).
    """
    if id_filter is not None and track_id not in id_filter:
        return False
    if name_filter is not None:
        name_lower = (track_name or "").lower()
        if not any(n.lower() in name_lower for n in name_filter):
            return False
    return True


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract, plan, and merge subtitle text replacements across a folder of MKV files.",
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

    p_plan = sub.add_parser("plan", help="Scan extracted subtitles and build a replacement plan.")
    p_plan.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                         help=f"Scratch/work directory used by 'extract' (default: ./{DEFAULT_WORK_DIR}).")
    p_plan.add_argument("--show", required=True, choices=sorted(SHOW_RULES),
                         help="Which show's built-in rule set to use.")
    p_plan.add_argument("--rules-file", default=None,
                         help="Optional JSON file of extra {\"find\": \"replace\"} rules "
                              "to add on top of the selected show's built-in rules.")
    p_plan.set_defaults(func=cmd_plan)

    p_merge = sub.add_parser("merge", help="Apply the plan and rebuild the mkv files.")
    p_merge.add_argument("-i", "--input-dir", default=DEFAULT_INPUT_DIR,
                          help=f"Folder containing the original .mkv files (default: {DEFAULT_INPUT_DIR}).")
    p_merge.add_argument("-w", "--work-dir", default=DEFAULT_WORK_DIR,
                          help=f"Scratch/work directory used by 'extract'/'plan' (default: ./{DEFAULT_WORK_DIR}).")
    p_merge.add_argument("-o", "--output-dir", default=DEFAULT_OUTPUT_DIR,
                          help=f"Where rebuilt .mkv files are written (default: ./{DEFAULT_OUTPUT_DIR}).")
    p_merge.add_argument("--in-place", action="store_true",
                          help="After building, move the rebuilt files over the originals in "
                               "--input-dir (originals are backed up as *.mkv.bak).")
    p_merge.add_argument("--dry-run", action="store_true",
                          help="Show what would be rebuilt without touching any .mkv files. Still "
                               "writes the edited subtitle files to the work directory for inspection.")
    p_merge.set_defaults(func=cmd_merge)

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

    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    if getattr(args, "no_color", False):
        set_color_enabled(False)
    args.func(args)


if __name__ == "__main__":
    main()
