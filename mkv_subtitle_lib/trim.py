"""Stage: trim -- cut subtitle entries that run past the end of the video.
"""

from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from .common import (
    C,
    EXTRACTED_DIRNAME,
    SRT_INDEX_RE,
    TIMESTAMP_RE,
    TRACKS_META_FILENAME,
    TRIMMED_DIRNAME,
    c,
    check_ffprobe,
    check_tools,
    parse_timestamp_value,
    read_text_smart,
    rebuild_all_mkvs,
)


# Default buffer (seconds) added to the video's duration before trimming,
# so a caption that legitimately ends right at (or a hair past, due to
# rounding) the last video frame isn't needlessly cut.
DEFAULT_TRIM_MARGIN_SECONDS = 0.05


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
