#!/usr/bin/env python3
"""
Batch HEVC encode script (platform-agnostic).

Encodes all .mkv files in the current directory using x265 settings matched
to a reference season encode, copying audio, subtitle and attachment (font, cover) streams as-is.

Usage:
    python batch_encode.py
    python batch_encode.py --input /path/to/season --output /path/to/season/encode
    python batch_encode.py --recursive
    python batch_encode.py -r --input /path/to/show

    python batch_encode.py --detect-crop
    python batch_encode.py --crop 1920x800
    python batch_encode.py --crop auto

    python batch_encode.py --check-fps

    python batch_encode.py --auto-vfr
    python batch_encode.py -ss 00:16:20 -t 60
    python batch_encode.py --dry-run
    python batch_encode.py --preset 15
    python batch_encode.py --crf 16 --psy-rd 1.5 --psy-rdoq 2.0
    python batch_encode.py --frame-rate 25    (or --fps 25)

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
    Output is encoded at FRAME_RATE (quick settings, or --frame-rate). Each source is checked
    first, and a file that doesn't match is skipped and reported rather than
    having frames dropped or duplicated. The check has two parts: the frame
    rate the stream declares, and the actual frame timestamps, which catch
    files whose spacing genuinely varies (hybrid or telecined sources).
    Small timestamp jitter, which can make tools like MediaInfo say "Variable",
    is not counted as variation; the encode writes constant-rate timestamps.
    --check-fps runs just this check on every file and exits without encoding.

    --auto-vfr changes what happens to a file whose frame spacing genuinely
    varies, such as 23.976 fps mixed with native 30 fps sections. Instead of
    being skipped, it is encoded as VFR (-fps_mode vfr -enc_time_base demux),
    which keeps every frame at its original time. This is decided per file, so
    a folder can mix both kinds: files that match FRAME_RATE are still encoded
    at constant FRAME_RATE, and a file at a steady but different rate (say a
    plain 25 fps file) is still skipped. Needs ffmpeg 5.1 or newer. --check-fps
    and --dry-run both show which mode each file would get.

Settings:
    CRF, PSY_RD, PSY_RDOQ and FRAME_RATE below are the defaults. Each can be
    overridden for a single run with --crf, --psy-rd, --psy-rdoq and
    --frame-rate, or --fps for short (a fraction such as 24000/1001, or a number such as 25;
    23.976, 29.97 and 59.94 are accepted as shorthand for the exact NTSC rates).

    --preset N sets CRF, psy-rd and psy-rdoq together from
    the PRESETS table, e.g. --preset 15. --psy-rd and --psy-rdoq still
    override a preset's value; --crf can't be combined with it.

Color tags:
    The output's color tags (primaries, transfer, matrix, range) are read from
    each source and matched, so SD (BT.601) and full-range sources aren't
    mislabeled as BT.709 limited. Tags a source leaves unset are filled in from
    its resolution, the way players treat untagged video (480p and below:
    BT.601 NTSC, 576p: BT.601 PAL, anything larger: BT.709). HDR sources (PQ or
    HLG) and tag values x265 has no equivalent for are skipped and reported,
    since this script doesn't carry HDR metadata.

Safety:
    Each file is encoded to <name>.part.mkv beside its final path and only
    renamed into place once ffmpeg finishes and the result passes a check
    against the source: same duration (within VERIFY_DURATION_TOLERANCE
    seconds) and the same number of audio, subtitle and attachment streams. A
    failed, invalid or interrupted encode leaves no partial file behind, so a
    rerun redoes it instead of skipping it as "already exists". Ctrl-C removes
    the unfinished file, still prints the summary, and exits with code 130.

Test clips:
    -ss START -t DURATION encodes just that part of each file, for trying out
    settings. Give both or neither. START is HH:MM:SS, MM:SS or seconds;
    DURATION is seconds or a time. ffmpeg is called as
    ffmpeg -ss START -i input -t DURATION ... (fast, accurate seeking). The clip
    is written to the usual output folder as
    test_<name>_crf_<crf>_psy-rd_<n>_psy-rdoq_<n>.mkv, for example
    test_Show S01E01_crf_14_psy-rd_2.0_psy-rdoq_2.2.mkv, so clips made with
    different settings sit side by side. Two clips of the same file with the
    same settings share a name, so the second is skipped as already existing.
    The frame-rate, color and crop checks still look at the whole file, so a
    test runs in the same mode (CFR, or VFR with --auto-vfr) as the full encode
    would. Audio, subtitles and attachments are cut at the start point too, so
    they stay in sync. The check after each encode expects the clip's length,
    not the whole file's. Can't be combined with --check-fps or --detect-crop.

Dry run:
    --dry-run makes every check an encode would (frame rate, color tags, crop)
    and prints what would happen to each file, then a count of files that would
    be encoded, skipped or failed. It creates no folders, logs or files.

Logging:
    Encode runs append everything they print to <output>/logs/run.log (settings
    used, per-file frame rate and crop details, output sizes, duplicated and
    dropped frame counts, totals), alongside the per-file ffmpeg logs and
    failed.log.

Console color:
    Console output is colored when attached to a terminal: cyan for headers,
    green for success, orange for warnings, yellow for notes, red for errors.
    --no-color turns it off (it is also off when output is piped, or when the
    NO_COLOR environment variable is set). The run log never contains color codes.
"""

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from fractions import Fraction

# ---------------------------------------------------------------------------
# Quick settings — defaults for the encode. Each can also be overridden for a
# single run with --crf, --psy-rd, --psy-rdoq and --frame-rate.
# ---------------------------------------------------------------------------
CRF = 14            # lower = higher quality / larger files
PSY_RD = 2.00       # psychovisual rate-distortion strength
PSY_RDOQ = 2.20     # psychovisual RDOQ strength
FRAME_RATE = "24000/1001"   # output frame rate; each source must match it or the file is skipped

# Named sets for --preset, keyed by CRF: (psy-rd, psy-rdoq). Add more sets here.
PRESETS = {
    14: (2.0, 2.2),
    15: (1.8, 2.0),
}
# ---------------------------------------------------------------------------

# x265 params matched to the reference season encode.
# NOTE: hist-threshold intentionally omitted — both the Mac (Homebrew) and
# Windows ffmpeg builds tested rejected it as an unknown option despite a
# current x265 version. hist-scenecut and scenecut-bias are kept in since
# they haven't actually been confirmed broken on your builds — if either
# throws "Unknown option" too, remove it the same way.
def build_x265_params(color: dict) -> str:
    """x265 params string. color holds the VUI tags (see resolve_color); the rest is fixed."""
    return (
        "frame-threads=4:wpp=1:pmode=0:pme=0:min-keyint=23:keyint=240:"
        "bframes=12:b-adapt=2:b-pyramid=1:bframe-bias=0:rc-lookahead=60:scenecut=40:"
        "hist-scenecut=1:scenecut-bias=5.0:ctu=64:min-cu-size=8:max-tu-size=32:"
        "tu-inter-depth=2:tu-intra-depth=2:"
        "rdoq-level=2:signhide=1:max-merge=5:limit-refs=1:me=3:subme=5:merange=57:"
        "temporal-mvp=1:weightp=1:weightb=1:deblock=-2,-2:sao=0:rd=3:b-intra=1:"
        f"psy-rd={PSY_RD:.2f}:psy-rdoq={PSY_RDOQ:.2f}:cbqpoffs=-2:crqpoffs=-2:crf={CRF}:qcomp=0.73:qpstep=4:"
        "ipratio=1.40:pbratio=1.30:aq-mode=3:aq-strength=0.85:qg-size=32:qpmax=69:qpmin=0:"
        f"sar=1:range={color['range']}:colorprim={color['primaries']}:transfer={color['transfer']}:colormatrix={color['matrix']}:"
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

# Output check after each encode: durations may differ by up to this many seconds.
VERIFY_DURATION_TOLERANCE = 1.0


# ---------------------------------------------------------------------------
# Color output
# ---------------------------------------------------------------------------
# USE_COLOR is resolved once in main() based on terminal detection, --no-color,
# and the NO_COLOR env var convention (https://no-color.org/). All the c_*()
# helpers below read it at call time, so it just needs to be set before any
# output happens.

USE_COLOR = False

ANSI_RE = re.compile(r"\033\[[0-9;]*m")


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    ORANGE = "\033[38;5;208m"  # 256-color; warmer/more orange than basic YELLOW


def _colorize(text, *codes):
    if not USE_COLOR or not text:
        return text
    return "".join(codes) + str(text) + _Ansi.RESET


def c_warn(text):      return _colorize(text, _Ansi.ORANGE)
def c_note(text):      return _colorize(text, _Ansi.YELLOW)
def c_err(text):       return _colorize(text, _Ansi.RED, _Ansi.BOLD)
def c_header(text):    return _colorize(text, _Ansi.CYAN, _Ansi.BOLD)
def c_banner(text):    return _colorize(text, _Ansi.MAGENTA, _Ansi.BOLD)
def c_dim(text):       return _colorize(text, _Ansi.DIM)
def c_success(text):   return _colorize(text, _Ansi.GREEN, _Ansi.BOLD)


def check_ffmpeg():
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            print(c_err(f"ERROR: {tool} not found on PATH."), file=sys.stderr)
            sys.exit(1)


def check_ffmpeg_options():
    """Exit with a clear message if this ffmpeg lacks the options --auto-vfr needs."""
    try:
        help_text = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"], capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL).stdout
    except OSError:
        return
    missing = [opt for opt in ("-fps_mode", "-enc_time_base") if opt not in help_text]
    if missing:
        print(c_err(f"ERROR: this ffmpeg doesn't support {' or '.join(missing)}, which --auto-vfr needs "
                    f"(ffmpeg 5.1 or newer)."), file=sys.stderr)
        sys.exit(1)


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
    """Write to the console and the run log at once; color codes are stripped from the log."""

    def __init__(self, console, log):
        self.console = console
        self.log = log

    def write(self, data):
        self.console.write(data)
        self.log.write(ANSI_RE.sub("", data))

    def flush(self):
        self.console.flush()
        self.log.flush()


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


def analyze_timing(input_path: str):
    """Look at a file's frame timestamps on their own, without reference to FRAME_RATE.

    Returns None if they can't be read or there are too few frames. Otherwise a
    dict with "variable" (True if the spacing really varies) and, when it does,
    "summary" and "details" text for the console. A file is variable when more
    than the usual allowance of its frame gaps don't fit the file's own main
    spacing; a steady rate, even one other than FRAME_RATE, is not variable.
    """
    times = read_timestamps(input_path)
    if not times or len(times) < 3:
        return None

    deltas = [(cur - prev) * 1000 for prev, cur in zip(times, times[1:])]
    middle = statistics.median(deltas)
    if middle < 1:
        return None
    typical = [d for d in deltas if abs(d - middle) <= VFR_JITTER_MS]
    main_ms = sum(typical) / len(typical)   # averaged, so MKV's 1 ms rounding doesn't build up

    irregular = []   # (time of the frame, ms since the previous frame)
    for cur, delta in zip(times[1:], deltas):
        k = round(delta / main_ms)
        if not (k >= 1 and abs(delta - k * main_ms) <= VFR_JITTER_MS):
            irregular.append((cur, delta))

    frames = len(times)
    allowed = max(VFR_MIN_IRREGULAR, int(frames * VFR_MAX_IRREGULAR_FRACTION))
    if len(irregular) <= allowed:
        return {"variable": False}

    odd = [d for _, d in irregular]
    odd_mid = statistics.median(odd)
    close = [d for d in odd if abs(d - odd_mid) <= VFR_JITTER_MS]
    other_ms = sum(close) / len(close)
    summary = (f"mostly {main_ms:.1f} ms spacing ({1000 / main_ms:.3f} fps) with sections around "
               f"{other_ms:.1f} ms ({1000 / other_ms:.3f} fps); "
               f"{len(irregular)} of {frames:,} frames are irregular")
    details = [f"irregular at {format_time(t)}: {d:.1f} ms after the previous frame "
               f"(main spacing {main_ms:.1f} ms)" for t, d in irregular[:FPS_REPORT_MAX_ITEMS]]
    if len(irregular) > FPS_REPORT_MAX_ITEMS:
        details.append(f"... and {len(irregular) - FPS_REPORT_MAX_ITEMS} more irregular frame(s)")
    return {"variable": True, "summary": summary, "details": details}


def resolve_frame_timing(input_path: str, auto_vfr: bool):
    """Decide how a file is encoded: returns (mode, rate), mode being "cfr" or "vfr".

    rate has the same shape as analyze_frame_rate's result. With auto_vfr, a file
    that fails the constant-rate check only because its frame spacing varies is
    switched to "vfr" (and rate["problem"] is cleared). Everything else keeps the
    CFR result, including files at a steady rate that just isn't FRAME_RATE.
    """
    rate = analyze_frame_rate(input_path)
    if not rate["problem"]:
        return "cfr", rate

    timing = analyze_timing(input_path)
    if timing and timing["variable"]:
        if auto_vfr:
            return "vfr", {"problem": None, "note": f"variable frame rate, {timing['summary']}",
                           "details": timing["details"]}
        rate = {**rate, "problem": rate["problem"] + " (--auto-vfr would encode it with its original timing)"}
    return "cfr", rate


def run_check_fps(files: list, input_dir: str, auto_vfr: bool = False):
    """--check-fps: check every file against FRAME_RATE and report, without encoding."""
    target_fps = float(Fraction(FRAME_RATE))
    print(c_header(f"Checking {len(files)} file(s) against {FRAME_RATE} ({target_fps:.3f} fps)...") + "\n")

    bad = []
    vfr_count = 0
    for i, rel_path in enumerate(files, 1):
        prefix = f"[{i}/{len(files)}] {rel_path}: "
        mode, result = resolve_frame_timing(os.path.join(input_dir, rel_path), auto_vfr)
        if result["problem"]:
            print(f"{prefix}{c_err('MISMATCH')} - {result['problem']}")
            bad.append(rel_path)
        elif mode == "vfr":
            vfr_count += 1
            print(f"{prefix}{c_note('VFR')} ({result['note']}); would be encoded with its original timing")
        else:
            print(f"{prefix}{c_success('OK')}" + (f" ({result['note']})" if result["note"] else ""))
        for line in result["details"]:
            print(c_note(f"      {line}"))

    print()
    if bad:
        print(c_err(f"{len(bad)} of {len(files)} file(s) don't match and would be skipped by an encode:"))
        for rel_path in bad:
            print(c_err(f"  {rel_path}"))
    else:
        print(c_success(f"All {len(files)} file(s) can be encoded.") if vfr_count == 0 else
              c_success(f"All {len(files)} file(s) can be encoded ({vfr_count} as VFR)."))


# ---------------------------------------------------------------------------
# Color tags
# ---------------------------------------------------------------------------

# ffprobe's names for each tag, mapped to the names x265 takes. A value that
# isn't listed (and isn't "untagged" or HDR) makes the file get skipped.
COLOR_PRIMARIES = {name: name for name in
                   ("bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020")}
COLOR_TRANSFERS = {"bt709": "bt709", "gamma22": "bt470m", "gamma28": "bt470bg",
                   "smpte170m": "smpte170m", "smpte240m": "smpte240m", "linear": "linear",
                   "iec61966-2-1": "iec61966-2-1", "bt2020-10": "bt2020-10", "bt2020-12": "bt2020-12"}
COLOR_MATRICES = {name: name for name in
                  ("bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "bt2020nc", "bt2020c")}
HDR_TRANSFERS = {"smpte2084": "PQ", "arib-std-b67": "HLG"}
UNTAGGED = {"", "unknown", "unspecified", "reserved"}


def default_color_for_height(height: int) -> dict:
    """Tags to assume for an untagged source, the way players do: by resolution."""
    if height <= 480:
        return {"primaries": "smpte170m", "transfer": "smpte170m", "matrix": "smpte170m"}
    if height <= 576:
        return {"primaries": "bt470bg", "transfer": "smpte170m", "matrix": "bt470bg"}
    return {"primaries": "bt709", "transfer": "bt709", "matrix": "bt709"}


def probe_color(input_path: str):
    """Return the first video stream's height and color tags as a dict, or None if the probe fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height,color_range,color_space,color_transfer,color_primaries",
             "-of", "json", input_path],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        stream = json.loads(out)["streams"][0]
        stream["height"] = int(stream["height"])
        return stream
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError, ValueError):
        return None


def resolve_color(input_path: str):
    """Work out the output color tags for one file and print them.

    Returns (color, error). color has primaries, transfer, matrix and range, in
    x265's names. error is a message if the file should be skipped (HDR, or a
    tag value with no x265 equivalent).
    """
    info = probe_color(input_path)
    if info is None:
        return None, "could not read the source color tags"

    height = info["height"]
    transfer_tag = (info.get("color_transfer") or "").lower()
    if transfer_tag in HDR_TRANSFERS:
        return None, (f"source is HDR ({HDR_TRANSFERS[transfer_tag]}, {transfer_tag}); "
                      f"this script doesn't carry HDR metadata")

    defaults = default_color_for_height(height)
    fields = (("primaries", "color_primaries", COLOR_PRIMARIES),
              ("transfer", "color_transfer", COLOR_TRANSFERS),
              ("matrix", "color_space", COLOR_MATRICES))
    color = {}
    assumed = 0
    for key, probe_key, table in fields:
        tag = (info.get(probe_key) or "").lower()
        if tag in UNTAGGED:
            color[key] = defaults[key]
            assumed += 1
        elif tag in table:
            color[key] = table[tag]
        else:
            return None, f"unsupported {probe_key.replace('_', ' ')} tag '{tag}'"
    color["range"] = "full" if (info.get("color_range") or "").lower() == "pc" else "limited"

    tags = f"{color['primaries']} / {color['transfer']} / {color['matrix']}, {color['range']} range"
    if assumed == 3:
        print(c_note(f"Color:    {tags} (untagged, assumed for {height}p)"))
    elif assumed:
        print(c_note(f"Color:    {tags} ({assumed} of 3 tags missing, filled in for {height}p)"))
    else:
        print(f"Color:    {tags} (from source)")
    return color, None


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
    print(c_header(f"Checking {len(files)} file(s) for black bars...") + "\n")

    groups = {}          # (source w, source h) -> list of centered (w, h) per file
    any_uneven = False

    for i, rel_path in enumerate(files, 1):
        prefix = f"[{i}/{len(files)}] {rel_path}: "
        result = measure_crop(os.path.join(input_dir, rel_path))
        if result is None:
            print(prefix + c_err("detection failed"))
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
              + (c_note("  [uneven]") if uneven else ""))

    print()
    if not groups:
        print(c_warn("No crop could be suggested."))
        return

    for (width, height), sizes in groups.items():
        # The largest size across files, so no file loses picture.
        w = max(s[0] for s in sizes)
        h = max(s[1] for s in sizes)
        distinct = sorted(set(sizes), reverse=True)

        counts = ", ".join(f"{sw}x{sh} x{sizes.count((sw, sh))}" for sw, sh in distinct)

        if (w, h) == (width, height):
            if len(distinct) > 1:
                print(c_warn(f"{width}x{height} sources: no single crop fits every file, because at least "
                             f"one file has no bars ({counts})."))
            else:
                print(f"{width}x{height} sources: no cropping needed.")
            continue

        print(c_header(f"Suggested crop for {width}x{height} sources: {w}x{h}"))
        print("  Use: " + c_success(f"--crop {w}x{h}"))
        if len(distinct) > 1:
            print(f"  Results varied between files ({counts}); the suggestion keeps the largest picture.")

    if any_uneven:
        print("\n" + c_note("Some files have uneven bars. --crop auto crops each file exactly, "
                            "while a --crop size is always centered."))


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def report_failures(failures: list, log_dir: str, save: bool = True):
    """Print the failed files and append them to <log_dir>/failed.log."""
    print(c_err(f"{len(failures)} file(s) failed:"))
    for rel_path, reason in failures:
        print(c_err(f"  {rel_path}: {reason}"))

    if not save:
        return

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


def partial_path(output_path: str) -> str:
    """Where an encode is written until it passes verification: <name>.part.mkv beside the final path."""
    return os.path.splitext(output_path)[0] + ".part.mkv"


def remove_quietly(path: str):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(c_warn(f"Could not remove {path}: {exc}"))


def probe_streams(path: str):
    """Return (duration_seconds, {codec_type: count}) for a file, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration",
             "-of", "json", path],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout
        data = json.loads(out)
        duration = float(data["format"]["duration"])
        counts = {}
        for stream in data.get("streams", []):
            kind = stream.get("codec_type", "unknown")
            counts[kind] = counts.get(kind, 0) + 1
        return duration, counts
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, ValueError):
        return None


def verify_output(input_path: str, output_path: str, clip=None):
    """Check a finished encode against its source.

    Returns (problem, note): problem is a message if the output looks wrong
    (None if fine); note is a short summary for the console when it is fine.
    The output must last as long as the source (within VERIFY_DURATION_TOLERANCE
    seconds) and keep every audio, subtitle and attachment stream. For a -ss/-t
    clip, the expected length is the clip's (shorter if it runs past the end).
    """
    src = probe_streams(input_path)
    out = probe_streams(output_path)
    if out is None:
        return "could not read the finished encode to verify it", ""
    if src is None:
        return "could not read the source to verify the encode against", ""

    src_duration, src_counts = src
    out_duration, out_counts = out

    problems = []
    if clip:
        expected = min(clip["t_s"], src_duration - clip["ss_s"])
        if abs(out_duration - expected) > VERIFY_DURATION_TOLERANCE:
            problems.append(f"duration is {out_duration:.1f}s but the clip should be {expected:.1f}s")
    elif abs(out_duration - src_duration) > VERIFY_DURATION_TOLERANCE:
        problems.append(f"duration is {out_duration:.1f}s but the source is {src_duration:.1f}s")
    for kind in ("audio", "subtitle", "attachment"):
        have, want = out_counts.get(kind, 0), src_counts.get(kind, 0)
        if have != want:
            problems.append(f"{have} {kind} stream(s) but the source has {want}")
    if problems:
        return "output failed verification: " + "; ".join(problems), ""

    kept = ", ".join(f"{out_counts.get(kind, 0)} {kind}" for kind in ("audio", "subtitle", "attachment"))
    return None, f"OK (duration matches; {kept} stream(s) kept)"


def encode_file(input_path: str, output_path: str, log_path: str, crop=None, color=None, mode="cfr", clip=None):
    """Encode one file. Returns (error, note): error is None on success, otherwise a message.

    The encode is written to <name>.part.mkv, checked against the source, and
    only then renamed to its final name. Whatever goes wrong, including Ctrl-C,
    the partial file is removed, so it is never mistaken for a finished encode.
    """
    env = os.environ.copy()
    # FFREPORT uses ':' as its option separator, so a Windows drive letter
    # (e.g. F:\...) must have its colon escaped, and backslashes are
    # converted to forward slashes to avoid further escaping issues.
    safe_log_path = log_path.replace("\\", "/").replace(":", r"\:")
    env["FFREPORT"] = f"file={safe_log_path}:level=32"

    vf_args = ["-vf", f"crop={crop}"] if crop else []
    part_path = partial_path(output_path)
    remove_quietly(part_path)   # leftover from an earlier crash

    # CFR: constant FRAME_RATE. VFR: every frame keeps its original timestamp.
    fps_args = ["-fps_mode", "vfr", "-enc_time_base", "demux"] if mode == "vfr" else ["-r", FRAME_RATE]

    cmd = [
        "ffmpeg",
        *(["-ss", clip["ss"]] if clip else []),     # before -i: fast seek, still frame-accurate when re-encoding
        "-i", input_path,
        *(["-t", clip["t"]] if clip else []),       # after -i: length of the clip
        "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?",
        *vf_args,
        "-pix_fmt", "yuv420p10le",
        "-c:v", "libx265",
        "-x265-params", build_x265_params(color),
        *fps_args,
        "-c:a", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        # A clip must cut the copied streams at the start point too. Without this, audio and
        # subtitles begin at the keyframe before -ss and the video ends up late or out of sync.
        *(["-copypriorss", "0"] if clip else []),
        part_path,
    ]

    error, note = None, ""
    try:
        if subprocess.run(cmd, env=env).returncode != 0:
            error = f"ffmpeg encode failed, see {log_path}"
        else:
            error, note = verify_output(input_path, part_path, clip)
        if error is None:
            try:
                os.replace(part_path, output_path)
            except OSError as exc:
                error = f"could not move the finished encode into place: {exc}"
    finally:
        if os.path.exists(part_path):
            remove_quietly(part_path)
    return error, note


def number_in_range(low: float, high: float, name: str):
    """Argparse type factory: a number between low and high (ints stay ints)."""
    def parse(value: str):
        try:
            num = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"'{value}' is not a number")
        if not low <= num <= high:
            raise argparse.ArgumentTypeError(f"{name} must be between {low:g} and {high:g}")
        return int(num) if num.is_integer() else num
    return parse


FRAME_RATE_ALIASES = {"23.976": "24000/1001", "29.97": "30000/1001", "59.94": "60000/1001"}


def parse_frame_rate_arg(value: str):
    """Argparse type for --frame-rate: a fraction or number; 23.976/29.97/59.94 mean the exact NTSC rates."""
    text = FRAME_RATE_ALIASES.get(value.strip(), value.strip())
    if parse_rate(text) is None:
        raise argparse.ArgumentTypeError(f"'{value}' is not a frame rate like 24000/1001 or 25")
    return text


TIME_RE = re.compile(r"^(?:\d+:){0,2}\d+(?:\.\d+)?$")


def parse_time_arg(value: str) -> str:
    """Argparse type for -ss / -t: seconds (90, 12.5) or [[HH:]MM:]SS[.ms]. Returned as typed, for ffmpeg."""
    text = value.strip()
    if not TIME_RE.match(text):
        raise argparse.ArgumentTypeError(f"'{value}' isn't a time like 00:16:20, 16:20 or 980")
    return text


def time_to_seconds(text: str) -> float:
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def format_setting(value: float) -> str:
    """For file names: 2.0 -> '2.0', 2.2 -> '2.2', 1.75 -> '1.75'."""
    text = f"{value:.2f}".rstrip("0")
    return text + "0" if text.endswith(".") else text


def test_file_name(rel_path: str) -> str:
    """Output name for a -ss/-t test clip: test_<name>_crf_<crf>_psy-rd_<n>_psy-rdoq_<n>.mkv"""
    stem = os.path.splitext(os.path.basename(rel_path))[0]
    return (f"test_{stem}_crf_{CRF:g}_psy-rd_{format_setting(PSY_RD)}"
            f"_psy-rdoq_{format_setting(PSY_RDOQ)}.mkv")


def clip_error(input_path: str, clip: dict):
    """Return an error message if the clip starts at or after the end of the source, else None."""
    info = probe_streams(input_path)
    if info is None:
        return "could not read the source duration"
    if clip["ss_s"] >= info[0]:
        return f"-ss {clip['ss']} is past the end of the file ({format_time(info[0])})"
    return None


def parse_preset(value: str):
    """Argparse type for --preset: a CRF that has an entry in PRESETS."""
    crf = number_in_range(0, 51, "crf")(value)
    if crf not in PRESETS:
        available = ", ".join(str(key) for key in PRESETS)
        raise argparse.ArgumentTypeError(f"no preset for crf {value} (available: {available})")
    return crf


def main():
    global USE_COLOR, CRF, PSY_RD, PSY_RDOQ, FRAME_RATE

    parser = argparse.ArgumentParser(description="Batch HEVC encode a folder of .mkv files.")
    parser.add_argument("--input", default=".", help="Folder containing source .mkv files (default: current directory)")
    parser.add_argument("--output", default=None, help="Output folder (default: <input>/encode)")
    parser.add_argument("-r", "--recursive", action="store_true",
                        help="Also encode .mkv files in subfolders, mirroring the folder structure in the output folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run every check an encode would (frame rate, color tags, crop) and report what "
                             "would happen, without encoding or writing any files")
    parser.add_argument("--auto-vfr", action="store_true",
                        help="Encode files whose frame timing varies (e.g. 23.976 fps mixed with 30 fps "
                             "sections) as VFR, keeping their original timing, instead of skipping them. "
                             "Decided per file; files matching --frame-rate stay constant-rate")
    parser.add_argument("--detect-crop", action="store_true",
                        help="Check all files for black bars, print a suggested crop size, and exit without encoding")
    parser.add_argument("--check-fps", action="store_true",
                        help="Check every file's frame rate against the frame rate setting and exit without encoding")
    parser.add_argument("--crop", type=parse_crop_arg, default=None, metavar="WxH|auto",
                        help="Crop in the final encode: a size such as 1920x800 (centered), "
                             "or 'auto' to detect and crop each file exactly. Default: no crop")
    parser.add_argument("--preset", dest="preset", type=parse_preset,
                        default=None, metavar="N",
                        help="Set crf, psy-rd and psy-rdoq together from a named set, keyed by crf. "
                             "Available: " + ", ".join(f"{crf} (psy-rd {rd:.2f}, psy-rdoq {rdoq:.2f})"
                                                       for crf, (rd, rdoq) in PRESETS.items()))
    parser.add_argument("--crf", type=number_in_range(0, 51, "crf"), default=None, metavar="N",
                        help=f"x265 CRF; lower = higher quality / larger files (default: {CRF})")
    parser.add_argument("--psy-rd", type=number_in_range(0, 5, "psy-rd"), default=None, metavar="N",
                        help=f"psychovisual rate-distortion strength, 0-5 (default: {PSY_RD:.2f})")
    parser.add_argument("--psy-rdoq", type=number_in_range(0, 50, "psy-rdoq"), default=None, metavar="N",
                        help=f"psychovisual RDOQ strength, 0-50 (default: {PSY_RDOQ:.2f})")
    parser.add_argument("--frame-rate", "--fps", dest="frame_rate", type=parse_frame_rate_arg, default=FRAME_RATE, metavar="RATE",
                        help=f"Output frame rate; each source must match it or the file is skipped. "
                             f"A fraction or number such as 24000/1001 or 25 (default: {FRAME_RATE})")
    parser.add_argument("-ss", dest="clip_start", type=parse_time_arg, default=None, metavar="START",
                        help="Test clip: start at this time (HH:MM:SS, MM:SS or seconds). Needs -t. "
                             "Output is named test_<name>_crf_<n>_psy-rd_<n>_psy-rdoq_<n>.mkv")
    parser.add_argument("-t", dest="clip_duration", type=parse_time_arg, default=None, metavar="LENGTH",
                        help="Test clip: encode this much (seconds or a time). Needs -ss")
    parser.add_argument("--no-color", action="store_true",
                        help="Disable colored output (also off when not attached to a terminal, "
                             "or when the NO_COLOR environment variable is set)")
    args = parser.parse_args()

    if args.detect_crop and args.crop is not None:
        parser.error("--detect-crop only reports; it can't be combined with --crop")
    if args.check_fps and (args.detect_crop or args.crop is not None):
        parser.error("--check-fps only reports; it can't be combined with --detect-crop or --crop")
    if args.dry_run and (args.detect_crop or args.check_fps):
        parser.error("--dry-run can't be combined with --detect-crop or --check-fps, "
                     "which already report without encoding")

    if (args.clip_start is None) != (args.clip_duration is None):
        parser.error("-ss and -t must be given together")
    clip = None
    if args.clip_start is not None:
        if args.detect_crop or args.check_fps:
            parser.error("-ss/-t can't be combined with --detect-crop or --check-fps, "
                         "which report on whole files")
        clip = {"ss": args.clip_start, "t": args.clip_duration,
                "ss_s": time_to_seconds(args.clip_start), "t_s": time_to_seconds(args.clip_duration)}
        if clip["t_s"] <= 0:
            parser.error("-t must be greater than 0")

    if args.preset is not None and args.crf is not None:
        parser.error("--crf can't be combined with --preset, which sets the crf; "
                     "use --psy-rd / --psy-rdoq to adjust individual values")

    # Start from the preset (or the defaults); explicit flags override.
    if args.preset is not None:
        base_crf, (base_rd, base_rdoq) = args.preset, PRESETS[args.preset]
    else:
        base_crf, base_rd, base_rdoq = CRF, PSY_RD, PSY_RDOQ
    CRF = args.crf if args.crf is not None else base_crf
    PSY_RD = args.psy_rd if args.psy_rd is not None else base_rd
    PSY_RDOQ = args.psy_rdoq if args.psy_rdoq is not None else base_rdoq
    FRAME_RATE = args.frame_rate

    USE_COLOR = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    if USE_COLOR and os.name == "nt":
        os.system("")   # switches on ANSI escape handling in older Windows consoles

    check_ffmpeg()
    if args.auto_vfr:
        check_ffmpeg_options()

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
        run_check_fps(files, input_dir, args.auto_vfr)
        return

    # A real run also appends everything it prints to <o>/logs/run.log.
    # A dry run creates no folders and writes no files.
    log_fh = None
    run_log = None
    if args.dry_run:
        print(c_banner("=== DRY RUN: nothing will be encoded or written ===") + "\n")
    else:
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(log_dir, exist_ok=True)
        run_log = os.path.join(log_dir, "run.log")
        log_fh = open(run_log, "a", encoding="utf-8", buffering=1)
        sys.stdout = Tee(sys.__stdout__, log_fh)

    if args.crop is None:
        crop_label = "none"
    elif args.crop == "auto":
        crop_label = "auto"
    else:
        crop_label = f"{args.crop[0]}x{args.crop[1]}"

    failures = []
    encoded = skipped = would_encode = 0
    total_in = total_out = 0
    total_dup = total_drop = 0
    total_time = 0.0
    interrupted = False

    try:
        print(c_dim("#" * 44))
        print(c_header(f"Run started: {time.strftime('%Y-%m-%d %H:%M:%S')}"))
        print(f"Input:    {input_dir}" + (" (recursive)" if args.recursive else ""))
        print(f"Output:   {output_dir}")
        print(f"Settings: crf={CRF}, psy-rd={PSY_RD:.2f}, psy-rdoq={PSY_RDOQ:.2f}, "
              f"frame rate={FRAME_RATE}, crop={crop_label}" + (", auto-vfr" if args.auto_vfr else ""))
        if clip:
            print(f"Clip:     start {clip['ss']}, length {clip['t']}  (test files are named test_<name>_crf_..., "
                  f"checks still cover the whole file)")
        print(f"ffmpeg:   {ffmpeg_version()}")
        print()

        print(f"Found {len(files)} file(s) to {'check' if args.dry_run else 'encode'}.\n")

        try:
            for rel_path in files:
                input_path = os.path.join(input_dir, rel_path)
                output_path = os.path.join(output_dir, rel_path)
                if clip:
                    output_path = os.path.join(os.path.dirname(output_path), test_file_name(rel_path))
                shown = os.path.relpath(output_path, output_dir) if clip else rel_path
                log_path = os.path.join(log_dir, os.path.splitext(os.path.relpath(output_path, output_dir))[0] + ".log")

                if os.path.exists(output_path):
                    print(c_note(f"Skipping (already exists): {shown}"
                                 + (" (a test clip from an earlier run; delete it to redo)" if clip else "")))
                    skipped += 1
                    continue

                if not args.dry_run:
                    os.makedirs(os.path.dirname(output_path), exist_ok=True)
                    os.makedirs(os.path.dirname(log_path), exist_ok=True)

                print(c_dim("=" * 44))
                print(c_header(f"{'Checking' if args.dry_run else 'Encoding'}: {rel_path}"))
                print(f"Output:   {os.path.relpath(output_path, output_dir)}")
                if not args.dry_run:
                    print(f"Started:  {time.strftime('%Y-%m-%d %H:%M:%S')}")

                mode, rate = resolve_frame_timing(input_path, args.auto_vfr)
                error = rate["problem"]
                if not error and mode == "vfr":
                    print(c_note(f"Mode:     VFR, keeping the original frame timing ({rate['note']})"))
                elif not error:
                    print(f"Rate:     {float(Fraction(FRAME_RATE)):.3f} fps ({rate['note']})")
                for line in rate["details"]:
                    print(c_note(f"            {line}"))

                if not error and clip:
                    error = clip_error(input_path, clip)

                color = None
                if not error:
                    color, error = resolve_color(input_path)

                crop = None
                if not error:
                    crop, error = resolve_crop(input_path, args.crop)
                print(c_dim("=" * 44))
                if error:
                    print(c_err(f"FAILED: {rel_path} ({error})") + "\n")
                    failures.append((rel_path, error))
                    continue

                if args.dry_run:
                    print(c_dim("Dry run: would encode this file.") + "\n")
                    would_encode += 1
                    continue

                log_fh.write(f"x265 params ({rel_path}): {build_x265_params(color)}\n")   # long, so log file only

                start = time.time()
                error, verify_note = encode_file(input_path, output_path, log_path, crop, color, mode, clip)
                elapsed_s = time.time() - start

                if error is None:
                    in_size = os.path.getsize(input_path)
                    out_size = os.path.getsize(output_path)
                    encoded += 1
                    total_in += in_size
                    total_out += out_size
                    total_time += elapsed_s
                    if clip:
                        print(c_success(f"Finished: {shown} in {format_duration(elapsed_s)} "
                                        f"({format_size(out_size)} test clip)"))
                    else:
                        ratio = f"{out_size / in_size:.0%}" if in_size else "n/a"
                        print(c_success(f"Finished: {rel_path} in {format_duration(elapsed_s)} "
                                        f"({format_size(out_size)}, {ratio} of the {format_size(in_size)} source)"))
                    print(f"Verify:   {verify_note}")

                    counts = read_dup_drop(log_path)
                    if counts is None:
                        print(c_warn("Frames:   duplicated/dropped counts unavailable (could not read the ffmpeg log)"))
                    else:
                        dup, drop = counts
                        total_dup += dup
                        total_drop += drop
                        frames_line = (f"Frames:   {dup} duplicated, {drop} dropped"
                                       + ("  <-- frames were dropped, check this file" if drop else ""))
                        print(c_warn(frames_line) if drop else frames_line)
                    print()
                else:
                    print(c_err(f"FAILED: {rel_path} ({error})") + "\n")
                    failures.append((rel_path, error))
        except KeyboardInterrupt:
            interrupted = True
            print("\n" + c_warn("Interrupted (Ctrl-C). The unfinished file was removed."))

        done = "Dry run complete" if args.dry_run else "All done"
        if interrupted:
            print(c_warn("Stopped early."))
        elif failures:
            print(c_err(f"{done}, with errors (see below)."))
        else:
            print(c_success(f"{done}."))

        if args.dry_run:
            print(f"Would encode {would_encode}, skip {skipped}, fail {len(failures)}.")
        else:
            summary = f"Encoded {encoded}, skipped {skipped}, failed {len(failures)}."
            if encoded and clip:
                summary += f" Total {format_size(total_out)} in {format_duration(total_time)}."
            elif encoded and total_in:
                summary += (f" Total {format_size(total_in)} -> {format_size(total_out)} "
                            f"({total_out / total_in:.0%}) in {format_duration(total_time)}.")
            print(summary)
            if encoded:
                totals_line = f"Frames duplicated: {total_dup}, dropped: {total_drop}."
                print(c_warn(totals_line) if total_drop else totals_line)
        if failures:
            print()
            report_failures(failures, log_dir, save=not args.dry_run)
        if run_log:
            print(f"Run log: {run_log}")
    finally:
        if log_fh:
            sys.stdout = sys.__stdout__
            log_fh.close()

    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    main()