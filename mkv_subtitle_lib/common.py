"""Shared infrastructure: constants, color output, mkvtoolnix helpers, ASS/SRT parsing primitives,
CLI argument helpers, and the mkvmerge rebuild step used by every stage that writes an mkv.
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
    A `mkv_subtitle_fonts/` folder next to the launcher script
    (mkv_subtitle_tool.py) -- i.e. the parent of this package folder, not the
    current working directory, and not --input-dir -- the natural place
    to keep font files shared across every show/episode you process,
    overridable per-run with --fonts-dir.
    """
    return Path(__file__).resolve().parent.parent / DEFAULT_FONTS_DIRNAME


PLAN_FILENAME = "plan.json"


PLAN_LOG_FILENAME = "plan.log"


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


def fmt_num(v: float) -> str:
    """Format a transformed coordinate/size compactly (no needless trailing zeros)."""
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.3f}".rstrip("0").rstrip(".")


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
    Shared rebuild step used by 'merge-replace' (edited subtitle files come from
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
