#!/usr/bin/env python3
"""
mkv_sync_tool.py - Check temporal sync between two folders of matched video
files (e.g. two different encodings of the same series), and copy subtitle
tracks from one encoding into the other using the measured offsets.

Subcommands:

  check   Compare each matched episode pair and report whether they're
          SYNCED, have a FIXED_OFFSET, or show DRIFT_DETECTED. Supports
          two detection methods (--method):
            audio (default) - waveform cross-correlation on a shared-
              language dialogue track. More reliable than the video
              method, which can be confidently wrong on shows with a lot
              of held static shots or repeated animation cycles (common
              in limited-animation anime) since near-identical frames
              recur at multiple points in the episode. Requires
              numpy+scipy and mkvmerge.
            video - perceptual-hash frame matching. Faster and has no
              audio-language requirement, but susceptible to the false-
              match failure mode above.

              python3 mkv_sync_tool.py check \\
                  --dir1 /path/encodeA --dir2 /path/encodeB

  mux-subs  Use a report from `check` to copy subtitle track(s) from one
            folder's files into the matching files in the other folder,
            applying the correct sync offset automatically. --source and
            --dest must be the same two folders originally passed to
            `check` as --dir1/--dir2 (in either order) - `check` records
            each file's full resolved path in the report, so mux-* match
            rows to --source/--dest by directory rather than needing
            --dir1/--dir2 repeated in the same order.

                python3 mkv_sync_tool.py mux-subs \\
                    --report sync_report.csv \\
                    --source /path/encodeA --dest /path/encodeB \\
                    --output-dir ./muxed

  mux-audio Same as `mux-subs`, but copies audio track(s) instead.

                python3 mkv_sync_tool.py mux-audio \\
                    --report sync_report.csv \\
                    --source /path/encodeA --dest /path/encodeB \\
                    --output-dir ./muxed

  mux-both  Copies both subtitle and audio track(s) from one source in a
            single mkvmerge pass.

                python3 mkv_sync_tool.py mux-both \\
                    --report sync_report.csv \\
                    --source /path/encodeA --dest /path/encodeB \\
                    --output-dir ./muxed

All three mux-* commands pass --stop-after-video-ends to mkvmerge, so
the copied track(s) are truncated to match the target file's video
length instead of running past the end if the source file is longer.

Requires: ffmpeg, ffprobe, `pip install video-offset-finder`, and
mkvmerge (MKVToolNix) on PATH for the mux-* subcommands.
"""

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

try:
    import numpy as np
    from scipy.signal import correlate
    HAVE_AUDIO_DEPS = True
except ImportError:
    HAVE_AUDIO_DEPS = False

# --------------------------------------------------------------------------
# Color helpers - disabled automatically when stdout isn't a terminal or
# NO_COLOR is set (https://no-color.org), so piping/redirecting output
# still produces clean, uncolored text.
# --------------------------------------------------------------------------

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

_COLORS = {
    "green": "\033[32m", "red": "\033[31m", "yellow": "\033[33m",
    "cyan": "\033[36m", "bold": "\033[1m", "dim": "\033[2m", "reset": "\033[0m",
}


def c(text, color):
    if not USE_COLOR:
        return text
    return f"{_COLORS[color]}{text}{_COLORS['reset']}"


STATUS_COLOR = {
    "SYNCED": "green", "FIXED_OFFSET": "yellow",
    "DRIFT_DETECTED": "red", "ERROR": "red",
}


def status_text(status):
    return c(status, STATUS_COLOR.get(status, "reset"))


VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".webm"}

EP_PATTERNS = [
    re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})"),   # S01E01
    re.compile(r"(\d{1,2})[xX](\d{1,3})"),        # 1x01
    re.compile(r"[Ee][Pp]?\.?\s?(\d{1,3})"),      # E01 / Ep01 / Ep.01
]
NUMBER_FALLBACK = re.compile(r"(\d{1,4})")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def episode_key(filename: str):
    """Extract a sortable/matchable episode key from a filename."""
    for pat in EP_PATTERNS:
        m = pat.search(filename)
        if m:
            groups = m.groups()
            if len(groups) == 2:
                return (int(groups[0]), int(groups[1]))
            return (0, int(groups[0]))
    m = NUMBER_FALLBACK.search(filename)
    if m:
        return (0, int(m.group(1)))
    return None


def list_videos(directory: Path):
    return sorted(
        [p for p in directory.iterdir() if p.suffix.lower() in VIDEO_EXTS],
        key=lambda p: p.name,
    )


def pair_files(dir1: Path, dir2: Path):
    files1 = list_videos(dir1)
    files2 = list_videos(dir2)

    keyed1 = {f: episode_key(f.name) for f in files1}
    keyed2 = {f: episode_key(f.name) for f in files2}

    if all(v is not None for v in keyed1.values()) and all(
        v is not None for v in keyed2.values()
    ):
        by_key2 = {}
        for f, k in keyed2.items():
            by_key2.setdefault(k, f)
        pairs = []
        unmatched = []
        for f, k in keyed1.items():
            if k in by_key2:
                pairs.append((f, by_key2[k]))
            else:
                unmatched.append(f)
        if unmatched:
            print(
                f"Warning: {len(unmatched)} file(s) in dir1 had no episode "
                f"match in dir2: {[f.name for f in unmatched]}",
                file=sys.stderr,
            )
        return sorted(pairs, key=lambda p: keyed1[p[0]])

    # Fallback: sorted-order pairing
    if len(files1) != len(files2):
        sys.exit(
            "Could not detect episode numbers reliably and folder file "
            f"counts differ ({len(files1)} vs {len(files2)}). Rename files "
            "to include episode numbers (e.g. S01E01) or make counts match."
        )
    print(
        "Note: pairing by sorted filename order (no episode numbers "
        "detected in one or both folders).",
        file=sys.stderr,
    )
    return list(zip(files1, files2))


# --------------------------------------------------------------------------
# `check` subcommand
# --------------------------------------------------------------------------

def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def detect_crop(path: Path, probe_at: float, probe_len: float = 8.0):
    """Sample the video around `probe_at` seconds and detect black-bar
    cropping (pillarboxing/letterboxing) via ffmpeg's cropdetect filter.
    Returns an ffmpeg crop=W:H:X:Y string, or None if no bars are found."""
    result = subprocess.run(
        [
            "ffmpeg", "-ss", f"{probe_at:.2f}", "-i", str(path),
            "-t", f"{probe_len:.2f}", "-vf", "cropdetect=24:2:0",
            "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
    if not matches:
        return None
    w, h, x, y = matches[-1]  # last detection is the most settled reading
    return f"crop={w}:{h}:{x}:{y}"


def get_video_filter_prefix(path: Path, duration: float) -> str:
    """Build a per-file filter prefix that crops away any pillarboxing/
    letterboxing and corrects for a non-square pixel aspect ratio (e.g. an
    anamorphic 1440x1080 stream), so two differently-stored encodings of
    the same picture end up geometrically aligned before comparison.
    Without this, e.g. a 1440x1080 anamorphic file vs. a 1920x1080
    pillarboxed file show the same real picture at different sizes/
    positions in the frame, which the offset finder reads as noise -
    manifesting as a huge, inconsistent ("drifting") offset."""
    probe_at = max(0.0, min(duration * 0.25, duration - 10))
    crop = detect_crop(path, probe_at)
    # setsar=1 bakes any non-1:1 sample aspect ratio into the pixel grid,
    # so an anamorphic stream is un-squeezed to its true displayed shape.
    return (f"{crop},setsar=1," if crop else "setsar=1,")


def make_clip(src: Path, dst: Path, start: float, length: float, vf_prefix: str = ""):
    # Re-encode (rather than -c copy) so the seek is frame-accurate. With
    # stream copy, ffmpeg can only start the output on a keyframe, and two
    # different encodings almost always have different keyframe intervals
    # - so a -c copy clip's *actual* start time silently differs between
    # file1 and file2, by a different amount at each cut point. That looks
    # exactly like drift even when the source files are perfectly synced.
    # A cheap re-encode of a short clip is fast enough that this isn't a
    # meaningful cost, and downscaling also normalizes away resolution
    # differences between the two encodings before comparison.
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src),
            "-t", f"{length:.2f}", "-an", "-sn", "-map", "0:v:0",
            "-vf", f"{vf_prefix}scale=480:-2", "-c:v", "libx264",
            "-preset", "ultrafast", "-crf", "20", str(dst),
        ],
        capture_output=True, check=True,
    )



def run_offset_finder(ref: Path, dist: Path) -> dict:
    out = subprocess.run(
        ["video-offset-finder", str(ref), str(dist), "-q"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "video-offset-finder failed")
    return json.loads(out.stdout)


AUDIO_SR = 44100


def pick_common_audio_language(f1: Path, f2: Path, preferred_lang: str = None):
    """Find a language present in both files' audio tracks to use for the
    sync measurement itself (independent of which language track(s) will
    later be muxed/copied). Uses mkvmerge (like identify_tracks(), used
    elsewhere for muxing) rather than ffprobe: some muxers write language
    info in a way ffprobe's simple stream-tag reader misses (reporting
    'und'), while mkvmerge - like MediaInfo - reads it correctly.
    Returns (audio_relative_pos_in_f1, audio_relative_pos_in_f2, language)
    or raises if no common language is found."""
    s1 = identify_tracks(f1, "audio")
    s2 = identify_tracks(f2, "audio")
    if not s1 or not s2:
        raise RuntimeError(f"No audio tracks found in {f1.name if not s1 else f2.name}")

    langs1 = [t.get("properties", {}).get("language", "und") for t in s1]
    langs2 = [t.get("properties", {}).get("language", "und") for t in s2]

    if preferred_lang:
        if preferred_lang not in langs1 or preferred_lang not in langs2:
            raise RuntimeError(
                f"Requested --audio-lang {preferred_lang} not found in both files "
                f"(file1 has {sorted(set(langs1))}, file2 has {sorted(set(langs2))})"
            )
        lang = preferred_lang
    else:
        common = set(langs1) & set(langs2)
        if not common:
            raise RuntimeError(
                f"No common audio language between {f1.name} ({sorted(set(langs1))}) "
                f"and {f2.name} ({sorted(set(langs2))}) - pass --audio-lang, or use "
                f"--method video instead."
            )
        lang = sorted(common)[0]

    # Position within the audio-only track list (0-based) - this is what
    # ffmpeg's "-map 0:a:N" stream specifier expects, and lines up with
    # mkvmerge's audio track order since both reflect the container's own
    # physical track order.
    pos1 = langs1.index(lang)
    pos2 = langs2.index(lang)
    return pos1, pos2, lang


def make_audio_clip(src: Path, dst_wav: Path, start: float, length: float, audio_pos: int = 0):
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", f"{start:.2f}", "-i", str(src),
            "-t", f"{length:.2f}", "-map", f"0:a:{audio_pos}",
            "-ac", "1", "-ar", str(AUDIO_SR), "-f", "wav", str(dst_wav),
        ],
        capture_output=True, check=True,
    )



def run_audio_offset_finder(ref_wav: Path, dist_wav: Path) -> dict:
    """Cross-correlate two mono WAV clips. Sign convention matches
    run_offset_finder: positive offset_seconds means the second clip
    (dist) is delayed relative to the first (ref). Confidence here is a
    normalized correlation coefficient (0-1, higher = better match) -
    the OPPOSITE convention from the video method's Hamming distance
    (lower = better) - callers must not compare the two numerically."""
    with wave.open(str(ref_wav), "rb") as w:
        ref = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    with wave.open(str(dist_wav), "rb") as w:
        dist = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)

    if ref.size == 0 or dist.size == 0 or np.allclose(ref, 0) or np.allclose(dist, 0):
        return {"offset_seconds": 0.0, "confidence": 0.0}

    corr = correlate(dist, ref, mode="full")
    lag = int(corr.argmax()) - (len(ref) - 1)
    offset_seconds = lag / AUDIO_SR

    peak = corr[corr.argmax()]
    norm = np.sqrt(np.sum(ref.astype(np.float64) ** 2) * np.sum(dist.astype(np.float64) ** 2))
    confidence = float(peak / norm) if norm > 0 else 0.0

    return {"offset_seconds": offset_seconds, "confidence": confidence}


def check_pair(f1: Path, f2: Path, clip_len: float, margin: float, tmpdir: Path,
                no_crop_detect: bool = False, sample_points: int = 2, method: str = "video",
                audio_lang: str = None):
    dur1 = ffprobe_duration(f1)
    dur2 = ffprobe_duration(f2)
    shorter = min(dur1, dur2)

    if shorter < 2 * margin + clip_len:
        margin = max(0.0, (shorter - clip_len) / 3)

    earliest = margin
    latest = max(margin, shorter - margin - clip_len)
    sample_points = max(2, sample_points)
    if sample_points == 2 or latest <= earliest:
        times = [earliest, latest]
    else:
        step = (latest - earliest) / (sample_points - 1)
        times = [earliest + i * step for i in range(sample_points)]

    if method == "audio":
        idx1, idx2, lang = pick_common_audio_language(f1, f2, audio_lang)
        print(f"    using shared audio language '{lang}' (track {idx1} / track {idx2}) for sync measurement")
        results = []
        for idx, t in enumerate(times):
            c1, c2 = tmpdir / f"p{idx}_1.wav", tmpdir / f"p{idx}_2.wav"
            make_audio_clip(f1, c1, t, clip_len, idx1)
            make_audio_clip(f2, c2, t, clip_len, idx2)
            res = run_audio_offset_finder(c1, c2)
            results.append({"time_s": round(t, 2), "offset_s": res["offset_seconds"],
                             "confidence": res["confidence"], "fps_used": 24.0})
            c1.unlink(missing_ok=True)
            c2.unlink(missing_ok=True)
        return results

    # Detect crop/PAR once per file (not once per clip) - the black bars
    # and pixel aspect ratio don't change over the course of an episode.
    if no_crop_detect:
        vf1 = vf2 = "setsar=1,"
    else:
        vf1 = get_video_filter_prefix(f1, dur1)
        vf2 = get_video_filter_prefix(f2, dur2)
    print(f"    filter1: {vf1.rstrip(',')}  |  filter2: {vf2.rstrip(',')}")

    results = []
    for idx, t in enumerate(times):
        c1, c2 = tmpdir / f"p{idx}_1.mkv", tmpdir / f"p{idx}_2.mkv"
        make_clip(f1, c1, t, clip_len, vf1)
        make_clip(f2, c2, t, clip_len, vf2)
        res = run_offset_finder(c1, c2)
        results.append({"time_s": round(t, 2), "offset_s": res["offset_seconds"],
                         "confidence": res["confidence"], "fps_used": res["fps_used"]})
        c1.unlink(missing_ok=True)
        c2.unlink(missing_ok=True)

    return results


SYNCED_THRESHOLD_S = 0.010  # offsets under this are reported as SYNCED rather than FIXED_OFFSET


def classify(offsets_s, fps: float) -> str:
    spread_frames = (max(offsets_s) - min(offsets_s)) * fps
    if abs(offsets_s[0]) < SYNCED_THRESHOLD_S and spread_frames < 1:
        return "SYNCED"
    if spread_frames < 1:
        return "FIXED_OFFSET"
    return "DRIFT_DETECTED"


def cmd_check(args):
    if args.method == "audio":
        if not HAVE_AUDIO_DEPS:
            sys.exit("--method audio requires numpy and scipy: "
                     "pip install numpy scipy --break-system-packages")
        if shutil.which("mkvmerge") is None:
            sys.exit("--method audio requires mkvmerge (MKVToolNix) on PATH, "
                      "to reliably read audio track languages.")

    pairs = pair_files(args.dir1, args.dir2)
    if not pairs:
        sys.exit("No matched pairs found.")

    print(f"Found {len(pairs)} matched pair(s). Checking sync ({args.method} method)...\n")

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for f1, f2 in pairs:
            f1_path, f2_path = str(f1.resolve()), str(f2.resolve())
            print(f"  {c(f1.name, 'cyan')}  <->  {c(f2.name, 'cyan')}")
            try:
                results = check_pair(f1, f2, args.clip_length, args.margin, tmpdir,
                                      args.no_crop_detect, args.sample_points, args.method,
                                      args.audio_lang)
            except Exception as e:
                print(f"    {c('ERROR', 'red')}: {e}", file=sys.stderr)
                rows.append({
                    "file1": f1_path, "file2": f2_path, "status": "ERROR",
                    "start_offset_s": "", "end_offset_s": "",
                    "start_offset_ms": "", "drift_frames": "",
                    "confidence_start": "", "confidence_end": "",
                    "points": "", "error": str(e),
                })
                continue

            fps = results[0]["fps_used"]
            offsets = [r["offset_s"] for r in results]
            status = classify(offsets, fps)
            drift_frames = round((max(offsets) - min(offsets)) * fps, 2)

            for r in results:
                print(f"    t={r['time_s']:.1f}s -> offset {r['offset_s']:+.3f}s "
                      f"(confidence {r['confidence']:.3f})")
            print(f"    spread: {drift_frames} frames -> {status_text(status)}")

            points_str = ";".join(
                f"{r['time_s']}:{round(r['offset_s'], 4)}:{round(r['confidence'], 3)}"
                for r in results
            )

            rows.append({
                "file1": f1_path, "file2": f2_path, "status": status,
                "start_offset_s": round(offsets[0], 4),
                "end_offset_s": round(offsets[-1], 4),
                "start_offset_ms": round(offsets[0] * 1000, 1),
                "drift_frames": drift_frames,
                "confidence_start": round(results[0]["confidence"], 3),
                "confidence_end": round(results[-1]["confidence"], 3),
                "points": points_str,
                "error": "",
            })

    with open(args.out, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nReport written to {args.out}")
    synced = sum(1 for r in rows if r["status"] in ("SYNCED", "FIXED_OFFSET"))
    drifted = sum(1 for r in rows if r["status"] == "DRIFT_DETECTED")
    errors = sum(1 for r in rows if r["status"] == "ERROR")
    print(f"Summary: {c(str(synced) + ' OK', 'green')} (synced or fixed offset), "
          f"{c(str(drifted) + ' drifting', 'red' if drifted else 'dim')}, "
          f"{c(str(errors) + ' errors', 'red' if errors else 'dim')}")


# --------------------------------------------------------------------------
# `mux-subs` / `mux-audio` subcommands
# --------------------------------------------------------------------------

# mkvmerge track type -> the --XXX-tracks flag used to select tracks,
# and the suffix used for output filenames.
TRACK_TYPE_FLAG = {
    "subtitles": "--subtitle-tracks",
    "audio": "--audio-tracks",
}
TRACK_TYPE_SUFFIX = {
    "subtitles": "with_subs",
    "audio": "with_audio",
}


def identify_tracks(path: Path, track_type: str):
    out = subprocess.run(
        ["mkvmerge", "-J", str(path)],
        capture_output=True, text=True, check=True,
    )
    info = json.loads(out.stdout)
    return [t for t in info.get("tracks", []) if t.get("type") == track_type]


def mux_pair(source: Path, target: Path, output: Path, offset_ms: float,
             track_types, track_ids_by_type=None, track_name_by_type=None,
             dry_run=False, exclude_dest_tracks=False):
    """track_types: list of 'subtitles' and/or 'audio'.
    track_ids_by_type: optional dict {track_type: [ids]} to filter each type.
    track_name_by_type: optional dict {track_type: substring} - only tracks
    whose track name/title contains this substring (case-insensitive) are
    copied for that type.
    exclude_dest_tracks: if True, drop target's own track(s) of the type(s)
    being copied (so the copied tracks replace rather than sit alongside
    whatever the target already had of that type)."""
    track_ids_by_type = track_ids_by_type or {}
    track_name_by_type = track_name_by_type or {}

    flag_args = []
    sync_ids = []
    any_tracks = False

    for track_type in track_types:
        tracks = identify_tracks(source, track_type)
        wanted_ids = track_ids_by_type.get(track_type)
        if wanted_ids:
            tracks = [t for t in tracks if t["id"] in wanted_ids]
            if not tracks:
                print(f"    None of the requested {track_type} track IDs "
                      f"{wanted_ids} found in {source.name}.")
                continue

        name_match = track_name_by_type.get(track_type)
        if name_match:
            needle = name_match.lower()
            tracks = [t for t in tracks
                      if needle in t.get("properties", {}).get("track_name", "").lower()]
            if not tracks:
                print(f"    No {track_type} tracks with a name matching "
                      f"'{name_match}' found in {source.name}.")
                continue

        if not tracks:
            print(f"    No {track_type} tracks found in {source.name}.")
            continue

        any_tracks = True
        ids = [t["id"] for t in tracks]
        flag_args += [TRACK_TYPE_FLAG[track_type], ",".join(str(i) for i in ids)]
        sync_ids += ids

        for t in tracks:
            lang = t.get("properties", {}).get("language", "und")
            name = t.get("properties", {}).get("track_name", "")
            verb = c("Would copy", "dim") if dry_run else c("Copying", "cyan")
            print(f"    {verb} {track_type} track {t['id']} ({lang}"
                  f"{', ' + name if name else ''}) with sync offset {offset_ms:+.1f}ms")

    if not any_tracks:
        print(f"    Nothing to copy from {source.name}, skipping.")
        return False

    # --audio-tracks/--subtitle-tracks only restrict WHICH tracks of that
    # type get pulled from `source` - they don't exclude other track
    # types. Without explicitly excluding everything we didn't ask for,
    # mkvmerge copies source's video and any track type not requested
    # (e.g. mux-audio would silently also copy source's subtitles).
    for excluded_type in ("video", "audio", "subtitles"):
        if excluded_type not in track_types:
            flag_args.append(f"--no-{excluded_type}")
    flag_args.append("--no-chapters")
    if "subtitles" not in track_types:
        # Keep attachments (fonts) only when copying subtitles, which may
        # depend on them; otherwise drop them so they don't tag along.
        flag_args.append("--no-attachments")
    # Force fresh random UIDs for source's tracks rather than keeping
    # whatever it originally had, so a source file with low-entropy UIDs
    # (e.g. sequential 1, 2, 3...) can't collide with the target's.
    flag_args.append("--regenerate-track-uids")

    # If replacing rather than adding, drop the target's own track(s) of
    # the type(s) being copied. These options must appear right before
    # the target filename to apply to it (unlike flag_args above, which
    # apply to `source` since they precede it instead).
    target_flag_args = []
    if exclude_dest_tracks:
        for track_type in track_types:
            target_flag_args.append(f"--no-{track_type}")
        for t in track_types:
            print(f"    Dropping {target.name}'s own {t} track(s)")

    # --stop-after-video-ends truncates all appended/added tracks (the
    # audio and/or subtitle tracks we're pulling in from `source`) once the
    # video track from `target` ends, so a longer source file doesn't
    # leave trailing audio/subs hanging past the end of the video.
    cmd = ["mkvmerge", "-o", str(output), "--stop-after-video-ends"] + \
          target_flag_args + [str(target)] + flag_args
    for i in sync_ids:
        cmd += ["--sync", f"{i}:{round(offset_ms)}"]
    cmd += [str(source)]

    if dry_run:
        print(f"    {c('[DRY RUN]', 'dim')} would run: {' '.join(cmd)}")
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):  # mkvmerge uses 1 for warnings
        print(f"    {c('ERROR', 'red')} muxing {output.name}:\n{result.stderr or result.stdout}",
              file=sys.stderr)
        return False
    if result.returncode == 1:
        # mkvmerge writes warnings to stdout, prefixed with "Warning:"
        warning_lines = "\n".join(
            line for line in (result.stdout or "").splitlines()
            if line.strip().lower().startswith("warning")
        ) or (result.stdout or result.stderr).strip()
        print(f"    {c('Muxed with warnings', 'yellow')}: {output.name}")
        for line in warning_lines.splitlines():
            print(f"      {c(line, 'yellow')}")
    else:
        print(f"    {c('Muxed', 'green')}: {output.name}")
    return True


def cmd_mux_generic(args, track_types):
    args.output_dir.mkdir(parents=True, exist_ok=True)

    track_ids_by_type = {}
    if len(track_types) == 1:
        if getattr(args, "track_ids", None):
            track_ids_by_type[track_types[0]] = [
                int(x.strip()) for x in args.track_ids.split(",")
            ]
    else:
        if getattr(args, "sub_track_ids", None):
            track_ids_by_type["subtitles"] = [
                int(x.strip()) for x in args.sub_track_ids.split(",")
            ]
        if getattr(args, "audio_track_ids", None):
            track_ids_by_type["audio"] = [
                int(x.strip()) for x in args.audio_track_ids.split(",")
            ]

    track_name_by_type = {}
    if len(track_types) == 1:
        if getattr(args, "track_name", None):
            track_name_by_type[track_types[0]] = args.track_name
    else:
        if getattr(args, "sub_track_name", None):
            track_name_by_type["subtitles"] = args.sub_track_name
        if getattr(args, "audio_track_name", None):
            track_name_by_type["audio"] = args.audio_track_name

    dry_run = getattr(args, "dry_run", False)
    if dry_run:
        print("[DRY RUN] no files will be written\n")

    source_dir = args.source.resolve()
    dest_dir = args.dest.resolve()

    with open(args.report, newline="") as f:
        rows = list(csv.DictReader(f))

    processed = skipped = failed = 0

    for row in rows:
        status = row["status"]
        f1 = Path(row["file1"])
        f2 = Path(row["file2"])

        if status == "ERROR":
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: sync check errored")
            skipped += 1
            continue

        # Figure out which of file1/file2 lives in --source vs --dest by
        # matching parent directories against the resolved paths `check`
        # recorded - this is what lets --source/--dest be plain directory
        # paths instead of needing the original --dir1/--dir2 order redone.
        if f1.parent == source_dir and f2.parent == dest_dir:
            source, target = f1, f2
            sign = 1
        elif f2.parent == source_dir and f1.parent == dest_dir:
            source, target = f2, f1
            sign = -1
        else:
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: doesn't match --source/--dest "
                  f"(found in {f1.parent} and {f2.parent}) - was this pair checked "
                  f"with a different --dir1/--dir2?")
            skipped += 1
            continue

        if status == "DRIFT_DETECTED" and not args.force:
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: {status_text('DRIFT_DETECTED')} "
                  f"(pass --force to mux anyway using the start offset only)")
            skipped += 1
            continue

        # offset_seconds from `check` is: how much file2 is delayed
        # relative to file1 (positive = file2 starts later).
        start_offset_ms = float(row["start_offset_ms"])
        no_sync = getattr(args, "no_sync", False)
        sync_ms = 0.0 if no_sync else sign * start_offset_ms

        suffix = "_".join(TRACK_TYPE_SUFFIX[t] for t in track_types) \
            if len(track_types) > 1 else TRACK_TYPE_SUFFIX[track_types[0]]
        out_name = target.stem + "." + suffix + target.suffix
        output = args.output_dir / out_name

        print(f"{c(source.name, 'cyan')} -> {c(target.name, 'cyan')}  (sync {sync_ms:+.1f}ms"
              f"{' - forced to 0 via --no-sync' if no_sync and (sign * start_offset_ms) != 0 else ''})")
        if not source.exists() or not target.exists():
            print(f"    {c('ERROR', 'red')}: missing file(s) on disk, skipping.", file=sys.stderr)
            failed += 1
            continue

        ok = mux_pair(source, target, output, sync_ms, track_types,
                      track_ids_by_type, track_name_by_type, dry_run,
                      getattr(args, "exclude_dest_tracks", False))
        if ok:
            processed += 1
        else:
            failed += 1

    verb = "would be muxed" if dry_run else "muxed"
    print(f"\nDone. {c(str(processed) + ' ' + verb, 'green')}, "
          f"{c(str(skipped) + ' skipped', 'yellow' if skipped else 'dim')}, "
          f"{c(str(failed) + ' failed', 'red' if failed else 'dim')}.")


def cmd_mux(args):
    cmd_mux_generic(args, ["subtitles"])


def cmd_mux_audio(args):
    cmd_mux_generic(args, ["audio"])


def cmd_mux_both(args):
    cmd_mux_generic(args, ["subtitles", "audio"])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog="mkv_sync_tool.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Check sync between two folders of matched episodes")
    check_p.add_argument("--dir1", required=True, type=Path, help="First encoding folder")
    check_p.add_argument("--dir2", required=True, type=Path, help="Second encoding folder")
    check_p.add_argument("--out", default="sync_report.csv", type=Path, help="CSV report output path")
    check_p.add_argument("--clip-length", type=float, default=20.0, help="Seconds of each clip to compare (default 20)")
    check_p.add_argument("--margin", type=float, default=90.0, help="Seconds to skip from start/before end (default 90)")
    check_p.add_argument("--no-crop-detect", action="store_true",
                          help="Skip black-bar crop detection (diagnostic: isolates whether crop detection is causing spurious drift)")
    check_p.add_argument("--sample-points", type=int, default=2,
                          help="Number of evenly spaced points across the runtime to sample (default 2 = start+end; use more, e.g. 5, to localize where drift occurs)")
    check_p.add_argument("--method", choices=["video", "audio"], default="audio",
                          help="Detection method. 'audio' (default) cross-correlates a shared-language dialogue track - more reliable than the video method, which can be confidently fooled by repeated/held animation frames. Requires numpy+scipy and mkvmerge. "
                               "'video' uses perceptual frame hashing instead - faster and has no audio-language requirement, but is susceptible to that failure mode.")
    check_p.add_argument("--audio-lang", type=str, default=None,
                          help="For --method audio: which language track to use for the sync measurement (e.g. 'jpn'). "
                               "Only needs to be a language present in BOTH files - it doesn't have to be the track(s) you'll eventually copy. "
                               "Default: auto-detect the first language common to both files.")
    check_p.set_defaults(func=cmd_check)

    mux_p = sub.add_parser("mux-subs", help="Copy subtitle track(s) using a report from `check`")
    mux_p.add_argument("--report", default="sync_report.csv", type=Path, help="CSV from the `check` subcommand (default: sync_report.csv)")
    mux_p.add_argument("--source", required=True, type=Path,
                        help="Folder whose files have the subtitles to copy FROM (must be one of the two folders originally passed to `check`)")
    mux_p.add_argument("--dest", required=True, type=Path,
                        help="Folder whose files will receive the copied subtitles (the other folder originally passed to `check`)")
    mux_p.add_argument("--output-dir", default="muxed", type=Path, help="Directory to write muxed files to (default: muxed)")
    mux_p.add_argument("--track-ids", type=str, default=None,
                        help="Comma-separated subtitle track IDs to copy (default: all)")
    mux_p.add_argument("--track-name", type=str, default=None,
                        help="Only copy subtitle tracks whose name/title contains this text (case-insensitive substring match)")
    mux_p.add_argument("--force", action="store_true",
                        help="Also process DRIFT_DETECTED pairs (risky: uses only the start offset)")
    mux_p.add_argument("--dry-run", action="store_true",
                        help="Print what would be muxed without writing any output files")
    mux_p.add_argument("--exclude-dest-tracks", action="store_true",
                        help="Drop the destination file's own subtitle track(s) so the copied ones replace them instead of sitting alongside them")
    mux_p.add_argument("--no-sync", action="store_true",
                        help="Copy tracks with zero delay, ignoring the offset measured by `check`")
    mux_p.set_defaults(func=cmd_mux)

    mux_audio_p = sub.add_parser("mux-audio", help="Copy audio track(s) using a report from `check`")
    mux_audio_p.add_argument("--report", default="sync_report.csv", type=Path, help="CSV from the `check` subcommand (default: sync_report.csv)")
    mux_audio_p.add_argument("--source", required=True, type=Path,
                              help="Folder whose files have the audio to copy FROM (must be one of the two folders originally passed to `check`)")
    mux_audio_p.add_argument("--dest", required=True, type=Path,
                              help="Folder whose files will receive the copied audio (the other folder originally passed to `check`)")
    mux_audio_p.add_argument("--output-dir", default="muxed", type=Path, help="Directory to write muxed files to (default: muxed)")
    mux_audio_p.add_argument("--track-ids", type=str, default=None,
                              help="Comma-separated audio track IDs to copy (default: all)")
    mux_audio_p.add_argument("--track-name", type=str, default=None,
                              help="Only copy audio tracks whose name/title contains this text (case-insensitive substring match)")
    mux_audio_p.add_argument("--force", action="store_true",
                              help="Also process DRIFT_DETECTED pairs (risky: uses only the start offset)")
    mux_audio_p.add_argument("--dry-run", action="store_true",
                              help="Print what would be muxed without writing any output files")
    mux_audio_p.add_argument("--exclude-dest-tracks", action="store_true",
                              help="Drop the destination file's own audio track(s) so the copied ones replace them instead of sitting alongside them")
    mux_audio_p.add_argument("--no-sync", action="store_true",
                              help="Copy tracks with zero delay, ignoring the offset measured by `check`")
    mux_audio_p.set_defaults(func=cmd_mux_audio)

    mux_both_p = sub.add_parser("mux-both", help="Copy both subtitle and audio track(s) using a report from `check`")
    mux_both_p.add_argument("--report", default="sync_report.csv", type=Path, help="CSV from the `check` subcommand (default: sync_report.csv)")
    mux_both_p.add_argument("--source", required=True, type=Path,
                             help="Folder whose files have the subs+audio to copy FROM (must be one of the two folders originally passed to `check`)")
    mux_both_p.add_argument("--dest", required=True, type=Path,
                             help="Folder whose files will receive the copied tracks (the other folder originally passed to `check`)")
    mux_both_p.add_argument("--output-dir", default="muxed", type=Path, help="Directory to write muxed files to (default: muxed)")
    mux_both_p.add_argument("--sub-track-ids", type=str, default=None,
                             help="Comma-separated subtitle track IDs to copy (default: all)")
    mux_both_p.add_argument("--audio-track-ids", type=str, default=None,
                             help="Comma-separated audio track IDs to copy (default: all)")
    mux_both_p.add_argument("--sub-track-name", type=str, default=None,
                             help="Only copy subtitle tracks whose name/title contains this text (case-insensitive substring match)")
    mux_both_p.add_argument("--audio-track-name", type=str, default=None,
                             help="Only copy audio tracks whose name/title contains this text (case-insensitive substring match)")
    mux_both_p.add_argument("--force", action="store_true",
                             help="Also process DRIFT_DETECTED pairs (risky: uses only the start offset)")
    mux_both_p.add_argument("--dry-run", action="store_true",
                             help="Print what would be muxed without writing any output files")
    mux_both_p.add_argument("--exclude-dest-tracks", action="store_true",
                             help="Drop the destination file's own subtitle+audio track(s) so the copied ones replace them instead of sitting alongside them")
    mux_both_p.add_argument("--no-sync", action="store_true",
                             help="Copy tracks with zero delay, ignoring the offset measured by `check`")
    mux_both_p.set_defaults(func=cmd_mux_both)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
