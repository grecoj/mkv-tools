#!/usr/bin/env python3
"""
Batch HEVC encode script (platform-agnostic).

Encodes all .mkv files in the current directory using x265 settings matched
to a reference season encode, copying audio and subtitle streams as-is.

Usage:
    python batch_encode.py
    python batch_encode.py --input /path/to/season --output /path/to/season/encode
    python batch_encode.py --recursive
    python batch_encode.py -r --input /path/to/show

    python batch_encode.py --detect-crop
    python batch_encode.py --crop 1920x800
    python batch_encode.py --crop auto

    python batch_encode.py --check-fps

With --recursive, subfolders of the input folder are searched too, and the
folder structure is mirrored in the output folder (and in its logs folder).
The output folder is never scanned, even when it sits inside the input folder.

Cropping:
    --detect-crop   Checks every file for black bars, prints the result for each
                    one plus a suggested crop size, then exits without encoding.
                    Takes the same --input / --recursive options as an encode.
    --crop WxH      Crops every file to that size, centered (e.g. 1920x800).
    --crop auto     Detects black bars per file and crops each one exactly.
    Without --crop, no cropping is applied.

Frame rate:
    Output is encoded at FRAME_RATE (quick settings). Each source is checked
    first, and a file that doesn't match is skipped and reported rather than
    having frames dropped or duplicated. The check has two parts: the frame
    rate the stream declares, and the actual frame timestamps, which catch
    files whose spacing genuinely varies (hybrid or telecined sources).
    Small timestamp jitter, which can make tools like MediaInfo say "Variable",
    is not counted as variation; the encode writes constant-rate timestamps.
    --check-fps runs just this check on every file and exits without encoding.

Logging:
    Encode runs append everything they print to <output>/logs/run.log (settings
    used, per-file frame rate and crop details, output sizes, duplicated and
    dropped frame counts, totals), alongside the per-file ffmpeg logs and
    failed.log.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from fractions import Fraction

# ---------------------------------------------------------------------------
# Quick settings — edit these to tune the encode.
# ---------------------------------------------------------------------------
CRF = 14            # lower = higher quality / larger files
PSY_RD = 2.00       # psychovisual rate-distortion strength
PSY_RDOQ = 2.20     # psychovisual RDOQ strength
FRAME_RATE = "24000/1001"   # output frame rate; each source must match it or the file is skipped
# ---------------------------------------------------------------------------

# x265 params matched to the reference season encode.
# NOTE: hist-threshold intentionally omitted — both the Mac (Homebrew) and
# Windows ffmpeg builds tested rejected it as an unknown option despite a
# current x265 version. hist-scenecut and scenecut-bias are kept in since
# they haven't actually been confirmed broken on your builds — if either
# throws "Unknown option" too, remove it the same way.
X265_PARAMS = (
    "frame-threads=4:wpp=1:pmode=0:pme=0:min-keyint=23:keyint=240:"
    "bframes=12:b-adapt=2:b-pyramid=1:bframe-bias=0:rc-lookahead=60:scenecut=40:"
    "hist-scenecut=1:scenecut-bias=5.0:ctu=64:min-cu-size=8:max-tu-size=32:"
    "tu-inter-depth=2:tu-intra-depth=2:"
    "rdoq-level=2:signhide=1:max-merge=5:limit-refs=1:me=3:subme=5:merange=57:"
    "temporal-mvp=1:weightp=1:weightb=1:deblock=-2,-2:sao=0:rd=3:b-intra=1:"
    f"psy-rd={PSY_RD:.2f}:psy-rdoq={PSY_RDOQ:.2f}:cbqpoffs=-2:crqpoffs=-2:crf={CRF}:qcomp=0.73:qpstep=4:"
    "ipratio=1.40:pbratio=1.30:aq-mode=3:aq-strength=0.85:qg-size=32:qpmax=69:qpmin=0:"
    "sar=1:range=limited:colorprim=bt709:transfer=bt709:colormatrix=bt709:"
    "log2-max-poc-lsb=8:hrd=1:slices=1:ref=5"
)

# Crop detection tuning (rarely needs changing).
CROP_SAMPLES = 12          # number of spots in each file to sample
CROP_SAMPLE_SECONDS = 3    # length of each sample
CROP_LIMIT = 24            # cropdetect black threshold
CROP_ROUND = 2             # crop values rounded to a multiple of this

FRAME_RATE_TOLERANCE = 0.01   # fps difference still counted as a match

# Frame timestamp check. A frame-to-frame gap counts as regular if it is a whole
# number of frame durations, give or take VFR_JITTER_MS. That allowance covers
# MKV's 1 ms rounding and small timestamp noise (about 15% of a frame); a real
# rate change, like 29.97 fps inside a 23.976 fps file, is off by around 8 ms.
VFR_JITTER_MS = 6.0
VFR_MIN_IRREGULAR = 10               # a file is only flagged above this many odd gaps...
VFR_MAX_IRREGULAR_FRACTION = 0.001   # ...and above this fraction of its frames
FPS_REPORT_MAX_ITEMS = 5             # gaps / off-grid spots listed per file in reports


def check_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"ERROR: {tool} not found on PATH.")


def format_duration(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_time(seconds: float) -> str:
    """HH:MM:SS.mmm"""
    seconds = max(seconds, 0.0)
    whole = int(seconds)
    h, rem = divmod(whole, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}.{int((seconds - whole) * 1000):03d}"


def format_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TiB"


def ffmpeg_version() -> str:
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL).stdout
        return out.splitlines()[0].split(" Copyright")[0]
    except (OSError, IndexError):
        return "unknown"


class Tee:
    """Write to several streams at once (console and the run log)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()


def normalize(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_within(path: str, parent: str) -> bool:
    """True if path is parent itself or anywhere underneath it."""
    path, parent = normalize(path), normalize(parent)
    return path == parent or path.startswith(parent + os.sep)


def find_mkv_files(input_dir: str, output_dir: str, recursive: bool) -> list:
    """Return .mkv paths relative to input_dir, never descending into output_dir."""
    found = []

    if not recursive:
        for f in os.listdir(input_dir):
            if f.lower().endswith(".mkv") and os.path.isfile(os.path.join(input_dir, f)):
                found.append(f)
        return sorted(found)

    for root, dirs, files in os.walk(input_dir, topdown=True):
        # Prune the output directory (and its logs) so encoded files are never re-encoded.
        dirs[:] = sorted(d for d in dirs if not is_within(os.path.join(root, d), output_dir))
        for f in files:
            if f.lower().endswith(".mkv"):
                full = os.path.join(root, f)
                found.append(os.path.relpath(full, input_dir))
    return sorted(found)


# ---------------------------------------------------------------------------
# Frame rate validation
# ---------------------------------------------------------------------------

def parse_rate(text: str):
    """Parse an ffprobe rate like '24000/1001' into a Fraction, or None if unusable."""
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def check_declared_rate(input_path: str):
    """Return an error message if the frame rate the stream declares doesn't match FRAME_RATE, else None."""
    target = Fraction(FRAME_RATE)
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=r_frame_rate,avg_frame_rate",
             "-of", "json", input_path],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        stream = json.loads(out)["streams"][0]
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError):
        return "could not read the source frame rate"

    rates = [r for r in (parse_rate(stream.get("r_frame_rate", "")),
                         parse_rate(stream.get("avg_frame_rate", ""))) if r]
    if not rates:
        return "could not read the source frame rate"

    if any(abs(float(r) - float(target)) <= FRAME_RATE_TOLERANCE for r in rates):
        return None

    found = " / ".join(sorted({f"{float(r):.3f}" for r in rates}))
    return (f"source frame rate is {found} fps but FRAME_RATE is {FRAME_RATE} "
            f"({float(target):.3f} fps)")


def read_timestamps(input_path: str):
    """Return the sorted presentation times (seconds) of every video frame, or None on failure."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", input_path],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
    except subprocess.CalledProcessError:
        return None

    times = []
    for line in out.splitlines():
        try:
            times.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue    # packets without a timestamp show as N/A
    times.sort()
    return times


def analyze_frame_rate(input_path: str) -> dict:
    """Check a source against FRAME_RATE.

    Returns {"problem": message or None, "note": short summary, "details": lines}.
    First the declared rate is checked, then the actual frame timestamps, which
    catches files whose spacing really varies even when the declared rate looks
    right. Gaps that are a whole number of frame durations (within VFR_JITTER_MS)
    are normal; a gap of several frames just means the source skips frames, which
    the encode fills by repeating one. details lists where gaps and off-grid
    frames are, so they can be found in the source.
    """
    empty = {"problem": None, "note": "", "details": []}

    problem = check_declared_rate(input_path)
    if problem:
        return {**empty, "problem": problem}

    times = read_timestamps(input_path)
    if not times:
        return {**empty, "problem": "could not read the frame timestamps"}
    if len(times) < 2:
        return {**empty, "note": "too few frames to check spacing"}

    target_fps = float(Fraction(FRAME_RATE))
    frame_ms = 1000 / target_fps
    gap_list = []     # (time before the gap, frames skipped, ms between the two frames)
    bad_list = []     # (time of the frame, ms since the previous frame)

    for prev, cur in zip(times, times[1:]):
        delta = (cur - prev) * 1000
        k = round(delta / frame_ms)
        if k >= 1 and abs(delta - k * frame_ms) <= VFR_JITTER_MS:
            if k > 1:
                gap_list.append((prev, k - 1, delta))
        else:
            bad_list.append((cur, delta))

    frames = len(times)
    more = FPS_REPORT_MAX_ITEMS

    details = []
    for t, skipped, delta in gap_list[:more]:
        details.append(f"gap after {format_time(t)}: {skipped} frame(s) skipped "
                       f"({delta:.0f} ms between frames)")
    if len(gap_list) > more:
        details.append(f"... and {len(gap_list) - more} more gap(s)")
    for t, delta in bad_list[:more]:
        details.append(f"off-grid at {format_time(t)}: {delta:.1f} ms after the previous frame "
                       f"(expected {frame_ms:.1f} ms)")
    if len(bad_list) > more:
        details.append(f"... and {len(bad_list) - more} more off-grid frame(s)")

    allowed = max(VFR_MIN_IRREGULAR, int(frames * VFR_MAX_IRREGULAR_FRACTION))
    if len(bad_list) > allowed:
        return {
            "problem": (f"frame spacing is variable: {len(bad_list)} of {frames} frames "
                        f"({len(bad_list) / frames:.1%}) are off the {target_fps:.3f} fps grid, "
                        f"first near {format_time(bad_list[0][0])}; encoding at {FRAME_RATE} "
                        f"would drop or duplicate frames"),
            "note": "",
            "details": details,
        }

    note = f"{frames:,} frames"
    if gap_list:
        note += f", {len(gap_list)} gap(s) where the source skips frames"
    if bad_list:
        note += f", {len(bad_list)} slightly off-grid frame(s), below the flag threshold"
    return {"problem": None, "note": note, "details": details}


def run_check_fps(files: list, input_dir: str):
    """--check-fps: check every file against FRAME_RATE and report, without encoding."""
    target_fps = float(Fraction(FRAME_RATE))
    print(f"Checking {len(files)} file(s) against {FRAME_RATE} ({target_fps:.3f} fps)...\n")

    bad = []
    for i, rel_path in enumerate(files, 1):
        prefix = f"[{i}/{len(files)}] {rel_path}: "
        result = analyze_frame_rate(os.path.join(input_dir, rel_path))
        if result["problem"]:
            print(f"{prefix}MISMATCH - {result['problem']}")
            bad.append(rel_path)
        else:
            print(f"{prefix}OK" + (f" ({result['note']})" if result["note"] else ""))
        for line in result["details"]:
            print(f"      {line}")

    print()
    if bad:
        print(f"{len(bad)} of {len(files)} file(s) don't match and would be skipped by an encode:")
        for rel_path in bad:
            print(f"  {rel_path}")
    else:
        print(f"All {len(files)} file(s) match.")


# ---------------------------------------------------------------------------
# Cropping
# ---------------------------------------------------------------------------

def parse_crop_arg(value: str):
    """Argparse type for --crop: 'auto' or a WxH size such as 1920x800."""
    if value.lower() == "auto":
        return "auto"
    m = re.fullmatch(r"(\d+)\s*[xX:]\s*(\d+)", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"'{value}' is not 'auto' or a size like 1920x800")
    w, h = int(m.group(1)), int(m.group(2))
    if w <= 0 or h <= 0:
        raise argparse.ArgumentTypeError("crop width and height must be greater than 0")
    if w % 2 or h % 2:
        raise argparse.ArgumentTypeError("crop width and height must be even numbers (4:2:0 video)")
    return (w, h)


def probe_video(input_path: str):
    """Return (duration_seconds, width, height), or None if the probe fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "json", input_path],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        data = json.loads(out)
        stream = data["streams"][0]
        return float(data["format"]["duration"]), int(stream["width"]), int(stream["height"])
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError, ValueError):
        return None


def measure_crop(input_path: str):
    """Sample the file with cropdetect.

    Returns (width, height, (left, top, right, bottom)) where the box is the
    picture area, or None if detection failed. Each sample reports a box of
    non-black picture; the result is the union of all of them, so a dark scene
    that makes one sample over-crop can't cut into picture seen in the others.
    """
    info = probe_video(input_path)
    if info is None:
        return None
    duration, width, height = info

    # Sample between 10% and 90% of the runtime to avoid intros and credits.
    first = duration * 0.10
    last = duration * 0.90 - CROP_SAMPLE_SECONDS
    if last <= first:
        points = [max(0.0, duration / 2 - CROP_SAMPLE_SECONDS / 2)]
    else:
        step = (last - first) / max(CROP_SAMPLES - 1, 1)
        points = [first + step * i for i in range(CROP_SAMPLES)]

    boxes = []
    for t in points:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin",
             "-ss", f"{t:.3f}", "-i", input_path, "-t", str(CROP_SAMPLE_SECONDS),
             "-map", "0:v:0",
             "-vf", f"cropdetect=limit={CROP_LIMIT}:round={CROP_ROUND}",
             "-an", "-sn", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        matches = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr)
        if matches:
            w, h, x, y = map(int, matches[-1])
            boxes.append((x, y, x + w, y + h))

    if not boxes:
        return None

    left = min(b[0] for b in boxes)
    top = min(b[1] for b in boxes)
    right = max(b[2] for b in boxes)
    bottom = max(b[3] for b in boxes)
    return width, height, (left, top, right, bottom)


def centered_size(width: int, height: int, box) -> tuple:
    """Largest centered crop size that keeps all the picture in the box.

    Uses the smaller bar on each axis, so a file with uneven bars keeps a
    sliver of the larger bar rather than losing picture.
    """
    left, top, right, bottom = box
    w = width - 2 * min(left, width - right)
    h = height - 2 * min(top, height - bottom)
    return w - w % 2, h - h % 2


def resolve_crop(input_path: str, crop_arg):
    """Work out the crop for one file and print it.

    Returns (crop_value, error). crop_value is the argument for ffmpeg's crop
    filter ('w:h:x:y' or 'w:h' for centered), or None for no crop. error is a
    message if the file should be skipped.
    """
    if crop_arg is None:
        return None, None

    if crop_arg == "auto":
        result = measure_crop(input_path)
        if result is None:
            return None, "crop detection failed"
        width, height, box = result
        left, top, right, bottom = box
        w, h = right - left, bottom - top
        if (w, h) == (width, height):
            print(f"Crop:     none needed ({width}x{height})")
            return None, None
        print(f"Crop:     {w}x{h} (from {width}x{height}, offset {left},{top})")
        return f"{w}:{h}:{left}:{top}", None

    w, h = crop_arg
    info = probe_video(input_path)
    if info is not None:
        _, width, height = info
        if w > width or h > height:
            return None, f"crop {w}x{h} is larger than the source {width}x{height}"
        if (w, h) == (width, height):
            print(f"Crop:     none needed ({width}x{height})")
            return None, None
        print(f"Crop:     {w}x{h} centered (from {width}x{height})")
    else:
        print(f"Crop:     {w}x{h} centered")
    return f"{w}:{h}", None


def run_detect(files: list, input_dir: str):
    """--detect-crop: report black bars for every file and suggest a crop size."""
    print(f"Checking {len(files)} file(s) for black bars...\n")

    groups = {}          # (source w, source h) -> list of centered (w, h) per file
    any_uneven = False

    for i, rel_path in enumerate(files, 1):
        prefix = f"[{i}/{len(files)}] {rel_path}: "
        result = measure_crop(os.path.join(input_dir, rel_path))
        if result is None:
            print(prefix + "detection failed")
            continue

        width, height, box = result
        left, top, right, bottom = box
        bars = (top, height - bottom, left, width - right)   # top, bottom, left, right
        uneven = bars[0] != bars[1] or bars[2] != bars[3]
        any_uneven = any_uneven or uneven

        size = centered_size(width, height, box)
        groups.setdefault((width, height), []).append(size)

        print(f"{prefix}{width}x{height} -> {right - left}x{bottom - top}  "
              f"(bars: top {bars[0]}, bottom {bars[1]}, left {bars[2]}, right {bars[3]})"
              + ("  [uneven]" if uneven else ""))

    print()
    if not groups:
        print("No crop could be suggested.")
        return

    for (width, height), sizes in groups.items():
        # The largest size across files, so no file loses picture.
        w = max(s[0] for s in sizes)
        h = max(s[1] for s in sizes)
        distinct = sorted(set(sizes), reverse=True)

        counts = ", ".join(f"{sw}x{sh} x{sizes.count((sw, sh))}" for sw, sh in distinct)

        if (w, h) == (width, height):
            if len(distinct) > 1:
                print(f"{width}x{height} sources: no single crop fits every file, because at least "
                      f"one file has no bars ({counts}).")
            else:
                print(f"{width}x{height} sources: no cropping needed.")
            continue

        print(f"Suggested crop for {width}x{height} sources: {w}x{h}")
        print(f"  Use: --crop {w}x{h}")
        if len(distinct) > 1:
            print(f"  Results varied between files ({counts}); the suggestion keeps the largest picture.")

    if any_uneven:
        print("\nSome files have uneven bars. --crop auto crops each file exactly, "
              "while a --crop size is always centered.")


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def report_failures(failures: list, log_dir: str):
    """Print the failed files and append them to <log_dir>/failed.log."""
    print(f"{len(failures)} file(s) failed:")
    for rel_path, reason in failures:
        print(f"  {rel_path}: {reason}")

    failed_log = os.path.join(log_dir, "failed.log")
    with open(failed_log, "a", encoding="utf-8") as fh:
        fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {len(failures)} file(s) failed\n")
        for rel_path, reason in failures:
            fh.write(f"  {rel_path}: {reason}\n")
        fh.write("\n")
    print(f"Saved to {failed_log}")


def read_dup_drop(log_path: str):
    """Return (duplicated, dropped) frame counts from an ffmpeg report log, or None if unreadable.

    ffmpeg only prints dup= and drop= in its progress line once one of them is
    non-zero, so a readable log with no such line means 0 and 0.
    """
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(fh.tell() - 200_000, 0))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    matches = re.findall(r"dup=(\d+)\s+drop=(\d+)", tail)
    if not matches:
        return 0, 0
    dup, drop = matches[-1]
    return int(dup), int(drop)


def encode_file(input_path: str, output_path: str, log_path: str, crop=None) -> bool:
    env = os.environ.copy()
    # FFREPORT uses ':' as its option separator, so a Windows drive letter
    # (e.g. F:\...) must have its colon escaped, and backslashes are
    # converted to forward slashes to avoid further escaping issues.
    safe_log_path = log_path.replace("\\", "/").replace(":", r"\:")
    env["FFREPORT"] = f"file={safe_log_path}:level=32"

    vf_args = ["-vf", f"crop={crop}"] if crop else []

    cmd = [
        "ffmpeg",
        "-i", input_path,
        "-map", "0:v:0", "-map", "0:a", "-map", "0:s?",
        *vf_args,
        "-pix_fmt", "yuv420p10le",
        "-c:v", "libx265",
        "-x265-params", X265_PARAMS,
        "-r", FRAME_RATE,
        "-c:a", "copy",
        "-c:s", "copy",
        output_path,
    ]

    result = subprocess.run(cmd, env=env)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description="Batch HEVC encode a folder of .mkv files.")
    parser.add_argument("--input", default=".", help="Folder containing source .mkv files (default: current directory)")
    parser.add_argument("--output", default=None, help="Output folder (default: <input>/encode)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Also encode .mkv files in subfolders, mirroring the folder structure in the output folder")
    parser.add_argument("--detect-crop", action="store_true",
                        help="Check all files for black bars, print a suggested crop size, and exit without encoding")
    parser.add_argument("--check-fps", action="store_true",
                        help="Check every file's frame rate against FRAME_RATE and exit without encoding")
    parser.add_argument("--crop", type=parse_crop_arg, default=None, metavar="WxH|auto",
                        help="Crop in the final encode: a size such as 1920x800 (centered), "
                             "or 'auto' to detect and crop each file exactly. Default: no crop")
    args = parser.parse_args()

    if args.detect_crop and args.crop is not None:
        parser.error("--detect-crop only reports; it can't be combined with --crop")
    if args.check_fps and (args.detect_crop or args.crop is not None):
        parser.error("--check-fps only reports; it can't be combined with --detect-crop or --crop")

    check_ffmpeg()

    input_dir = os.path.abspath(args.input)
    output_dir = os.path.abspath(args.output) if args.output else os.path.join(input_dir, "encode")
    log_dir = os.path.join(output_dir, "logs")

    files = find_mkv_files(input_dir, output_dir, args.recursive)

    if not files:
        print(f"No .mkv files found in {input_dir}")
        return

    if args.detect_crop:
        run_detect(files, input_dir)
        return

    if args.check_fps:
        run_check_fps(files, input_dir)
        return

    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)

    # Everything printed from here on is also appended to <output>/logs/run.log.
    run_log = os.path.join(log_dir, "run.log")
    log_fh = open(run_log, "a", encoding="utf-8", buffering=1)
    sys.stdout = Tee(sys.__stdout__, log_fh)

    if args.crop is None:
        crop_label = "none"
    elif args.crop == "auto":
        crop_label = "auto"
    else:
        crop_label = f"{args.crop[0]}x{args.crop[1]}"

    print("#" * 44)
    print(f"Run started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Input:    {input_dir}" + (" (recursive)" if args.recursive else ""))
    print(f"Output:   {output_dir}")
    print(f"Settings: crf={CRF}, psy-rd={PSY_RD:.2f}, psy-rdoq={PSY_RDOQ:.2f}, "
          f"frame rate={FRAME_RATE}, crop={crop_label}")
    print(f"ffmpeg:   {ffmpeg_version()}")
    log_fh.write(f"x265 params: {X265_PARAMS}\n")   # long, so log file only
    print()

    print(f"Found {len(files)} file(s) to encode.\n")

    failures = []
    encoded = skipped = 0
    total_in = total_out = 0
    total_dup = total_drop = 0
    total_time = 0.0

    for rel_path in files:
        input_path = os.path.join(input_dir, rel_path)
        output_path = os.path.join(output_dir, rel_path)
        log_path = os.path.join(log_dir, os.path.splitext(rel_path)[0] + ".log")

        if os.path.exists(output_path):
            print(f"Skipping (already exists): {rel_path}")
            skipped += 1
            continue

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)

        print("=" * 44)
        print(f"Encoding: {rel_path}")
        print(f"Output:   {os.path.relpath(output_path, output_dir)}")
        print(f"Started:  {time.strftime('%Y-%m-%d %H:%M:%S')}")

        rate = analyze_frame_rate(input_path)
        error = rate["problem"]
        if not error:
            print(f"Rate:     {float(Fraction(FRAME_RATE)):.3f} fps ({rate['note']})")
        for line in rate["details"]:
            print(f"            {line}")

        crop = None
        if not error:
            crop, error = resolve_crop(input_path, args.crop)
        print("=" * 44)
        if error:
            print(f"FAILED: {rel_path} ({error})\n")
            failures.append((rel_path, error))
            continue

        start = time.time()
        success = encode_file(input_path, output_path, log_path, crop)
        elapsed_s = time.time() - start

        if success:
            in_size = os.path.getsize(input_path)
            out_size = os.path.getsize(output_path)
            encoded += 1
            total_in += in_size
            total_out += out_size
            total_time += elapsed_s
            ratio = f"{out_size / in_size:.0%}" if in_size else "n/a"
            print(f"Finished: {rel_path} in {format_duration(elapsed_s)} "
                  f"({format_size(out_size)}, {ratio} of the {format_size(in_size)} source)")

            counts = read_dup_drop(log_path)
            if counts is None:
                print("Frames:   duplicated/dropped counts unavailable (could not read the ffmpeg log)")
            else:
                dup, drop = counts
                total_dup += dup
                total_drop += drop
                print(f"Frames:   {dup} duplicated, {drop} dropped"
                      + ("  <-- frames were dropped, check this file" if drop else ""))
            print()
        else:
            print(f"FAILED: {rel_path} (see log: {log_path})\n")
            failures.append((rel_path, f"ffmpeg encode failed, see {log_path}"))

    print("All done.")
    summary = f"Encoded {encoded}, skipped {skipped}, failed {len(failures)}."
    if encoded and total_in:
        summary += (f" Total {format_size(total_in)} -> {format_size(total_out)} "
                    f"({total_out / total_in:.0%}) in {format_duration(total_time)}.")
    print(summary)
    if encoded:
        print(f"Frames duplicated: {total_dup}, dropped: {total_drop}.")
    if failures:
        print()
        report_failures(failures, log_dir)
    print(f"Run log: {run_log}")

    sys.stdout = sys.__stdout__
    log_fh.close()


if __name__ == "__main__":
    main()