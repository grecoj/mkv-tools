#!/usr/bin/env python3
"""
mkv_sync_tool.py - Check temporal sync between two folders of matched video
files (e.g. two different encodings of the same series) by cross-correlating
a shared-language audio track, then copy subtitle and/or audio tracks from
one encoding into the other using the measured offset. Everything is
lossless: tracks are only copied and re-timed by mkvmerge, never re-encoded.

Subcommands:

  check      Compare each matched episode pair and write a CSV report.
             Each pair gets one status:
               SYNCED          constant offset under 10 ms
               FIXED_OFFSET    constant offset (the offset is reported)
               STRETCHED       constant offset after correcting a known
                               frame-rate ratio (PAL speedup 25 vs 23.976,
                               25 vs 24, or 24 vs 23.976); the ratio is
                               reported
               DRIFT_DETECTED  offset varies across the episode and no single
                               offset (or known ratio) explains it, e.g. the two
                               files are cut differently. The note column says
                               how it changes; a frame-rate ratio is still
                               reported if one was found. (--force muxes using
                               the offset at the start of the episode)
               LOW_CONFIDENCE  too few clips matched well enough to trust
               ERROR           something failed (see the error column)

               python3 mkv_sync_tool.py check -d1 /path/encodeA -d2 /path/encodeB

  locate     (slow, optional) For DRIFT_DETECTED pairs, scan the whole episode and
             report exactly where the offset changes, how big each change is,
             and which release has the extra material. Weak or doubtful steps
             are marked UNCERTAIN. Writes a copy of the report with a readable
             timeline in the note column and details in the steps column.

               python3 mkv_sync_tool.py locate --report sync_report.csv

  retime-video  For DRIFT_DETECTED pairs whose changes are splices inside black screen
             (file1's black stretch is longer or shorter than file2's by exactly
             the step), write a copy of file2's video with only its timestamps
             changed: the black frames are stretched or squeezed so both files
             share one timeline. No re-encode. The result is video only; copy the
             audio (any language) and subtitles from file1 onto it with mux-both,
             which then needs just one constant offset.

               python3 mkv_sync_tool.py retime-video --report sync_report_steps.csv

  mux-subs   Copy subtitle track(s) from --source files into the matching
             --dest files, applying the measured offset (and ratio, for
             STRETCHED pairs). -s/--source and -d/--dest must be the two
             folders originally given to `check` (either order).

               python3 mkv_sync_tool.py mux-subs -s /path/encodeA -d /path/encodeB --output-dir ./muxed

  mux-audio  Same, for audio track(s). STRETCHED pairs are skipped: audio
             can't be re-timed by a ratio without re-encoding.

  mux-both   Subtitles and audio in one mkvmerge pass. On a STRETCHED pair
             only the subtitles are copied.

All mux-* commands pass --stop-after-video-ends to mkvmerge so copied tracks
are truncated to the target's video length.

The --output-dir always ends up with every video file from --dest: files that
couldn't be muxed (skipped, failed, or without a partner) are copied through
unchanged, and files already present in the output folder are left alone.

Whole series, file1 = the release whose audio/subs you want, file2 = the picture you keep
(equal frame rates; file1 is taken as the reference for sync):

  1. check -d1 SRC -d2 DST --video                   sync of every pair; picture offsets for clean ones
  2. locate --report sync_report.csv --video          where each DRIFT_DETECTED pair changes (slow)
  3. retime-video --report sync_report_steps.csv      (--dry-run first) retime file2's video, lossless
  4. mux-both -s SRC -d retimed --report sync_report_retimed.csv --output-dir muxed
  5. mux-both -s SRC -d DST --report sync_report.csv --output-dir muxed --exclude-dest-tracks
                                                      the clean pairs; run AFTER step 4 (it never
                                                      overwrites files already in muxed/)

Requires: ffmpeg, ffprobe, mkvmerge (MKVToolNix) on PATH, and numpy + scipy.
"""

import argparse
import csv
import functools
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

try:
    import numpy as np
    from scipy.signal import correlate, resample_poly
except ImportError:
    sys.exit("This tool requires numpy and scipy: pip install numpy scipy")

# --------------------------------------------------------------------------
# Color helpers - disabled automatically when stdout isn't a terminal or
# NO_COLOR is set (https://no-color.org).
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
    "SYNCED": "green", "FIXED_OFFSET": "yellow", "STRETCHED": "cyan",
    "DRIFT_DETECTED": "red", "LOW_CONFIDENCE": "red", "ERROR": "red",
}


def status_text(status):
    return c(status, STATUS_COLOR.get(status, "reset"))


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SR = 48000                 # sample rate used for measurement (anime audio is almost always 48 kHz)
SYNCED_MS = 10.0           # constant offsets under this are reported as SYNCED
MIN_PROMINENCE = 1.5       # best correlation peak must beat the runner-up by this factor
PEAK_GUARD_S = 0.05        # ignore this much around the best peak when finding the runner-up

# Known frame-rate ratios (how much faster file2 plays than file1).
# 1001/960 = 25 / 23.976 (PAL speedup), 25/24 = PAL speedup of a 24 fps
# master, 1001/1000 = 24 / 23.976. Each also appears inverted for the case
# where file1 is the sped-up one.
STRETCH_RATIOS = [
    Fraction(1001, 960), Fraction(960, 1001),
    Fraction(25, 24), Fraction(24, 25),
    Fraction(1001, 1000), Fraction(1000, 1001),
]

LANG_PRIORITY = ["jpn", "eng"]  # preferred languages for the sync measurement

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".webm"}

# --------------------------------------------------------------------------
# Subprocess / probing helpers
# --------------------------------------------------------------------------


def run_cmd(cmd, ok_codes=(0,)):
    """Run a command, returning stdout bytes. On failure raise RuntimeError
    that includes the tail of stderr (the default CalledProcessError text
    hides it)."""
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode not in ok_codes:
        err = p.stderr.decode(errors="replace").strip().splitlines()
        detail = " | ".join(err[-3:]) if err else f"exit code {p.returncode}"
        raise RuntimeError(f"{cmd[0]} failed: {detail}")
    return p.stdout


def require_tools(*names):
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        sys.exit(f"Missing required tool(s) on PATH: {', '.join(missing)}")


def probe_duration(path: Path) -> float:
    out = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.decode().strip())


def probe_start_time(path: Path) -> float:
    """Container start time (can be slightly negative, e.g. AAC priming, or
    positive if the video starts late). ffmpeg's `-ss` is relative to this,
    but mkvmerge's --sync works on the raw container timestamps, so the
    measured offset has to be corrected by it."""
    out = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=start_time",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


@functools.lru_cache(maxsize=None)
def mkv_info(path_str: str) -> dict:
    # mkvmerge exits 1 for warnings, which still produces valid JSON.
    return json.loads(run_cmd(["mkvmerge", "-J", path_str], ok_codes=(0, 1)))


def identify_tracks(path: Path, track_type: str):
    return [t for t in mkv_info(str(path)).get("tracks", []) if t.get("type") == track_type]


# --------------------------------------------------------------------------
# Episode matching
# --------------------------------------------------------------------------

_RE_SE = re.compile(r"\bS(\d{1,2})[\s._-]?E(\d{1,3})\b", re.I)
_RE_NXM = re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I)            # 1x01 (not 1920x1080)
_RE_EP = re.compile(r"(?<![A-Za-z])E(?:P(?:ISODE)?)?[\s._-]?(\d{1,3})(?!\d)", re.I)
_RE_NUM = re.compile(r"(?<![\w.])(\d{1,3})(?:v\d+)?(?!\w)")        # standalone number token


def episode_key(filename: str):
    """Extract a matchable (season, episode) key from a filename, or None."""
    stem = Path(filename).stem
    m = _RE_SE.search(stem)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _RE_NXM.search(stem)
    if m:
        return (int(m.group(1)), int(m.group(2)))

    # Drop [group]/(tag) blocks and resolution/codec tokens before looking for
    # a bare episode number, so "[1080p]" or "x264" can't be mistaken for one.
    stripped = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
    stripped = re.sub(r"\b\d{3,4}[pi]\b|\b[xh]\.?26[45]\b", " ", stripped, flags=re.I)
    stripped = stripped.replace("_", " ")
    if " " not in stripped.strip():
        stripped = stripped.replace(".", " ")
    m = _RE_EP.search(stripped)
    if m:
        return (0, int(m.group(1)))
    nums = _RE_NUM.findall(stripped)
    if nums:
        return (0, int(nums[-1]))     # last number: "Show 2 - 05" -> 5
    return None


def list_videos(directory: Path):
    return sorted((p for p in directory.iterdir() if p.suffix.lower() in VIDEO_EXTS),
                  key=lambda p: p.name)


def pair_files(dir1: Path, dir2: Path):
    files1, files2 = list_videos(dir1), list_videos(dir2)
    keys1 = {f: episode_key(f.name) for f in files1}
    keys2 = {f: episode_key(f.name) for f in files2}

    if files1 and files2 and all(k is not None for k in keys1.values()) \
            and all(k is not None for k in keys2.values()):
        g1, g2 = defaultdict(list), defaultdict(list)
        for f, k in keys1.items():
            g1[k].append(f)
        for f, k in keys2.items():
            g2[k].append(f)

        pairs = []
        for k in sorted(g1):
            if k in g2 and len(g1[k]) == 1 and len(g2[k]) == 1:
                pairs.append((g1[k][0], g2[k][0]))
            elif k in g2:
                print(f"Warning: episode key {k} is ambiguous "
                      f"({[f.name for f in g1[k]]} vs {[f.name for f in g2[k]]}) - skipped",
                      file=sys.stderr)
        only1 = [f.name for k in sorted(g1) if k not in g2 for f in g1[k]]
        only2 = [f.name for k in sorted(g2) if k not in g1 for f in g2[k]]
        if only1:
            print(f"Warning: no match in dir2 for: {only1}", file=sys.stderr)
        if only2:
            print(f"Warning: no match in dir1 for: {only2}", file=sys.stderr)
        return pairs

    if len(files1) != len(files2):
        sys.exit("Could not detect episode numbers reliably and folder file counts differ "
                 f"({len(files1)} vs {len(files2)}). Rename files to include episode numbers "
                 "(e.g. S01E01) or make counts match.")
    print("Note: pairing by sorted filename order (no episode numbers detected).",
          file=sys.stderr)
    return list(zip(files1, files2))


# --------------------------------------------------------------------------
# Audio measurement
# --------------------------------------------------------------------------


def pick_audio_tracks(f1: Path, f2: Path, preferred_lang=None):
    """Choose one audio track in each file, in a language present in BOTH, to
    measure sync with. Returns (pos1, pos2, lang) where pos is the 0-based
    position in that file's audio-track list (ffmpeg's `0:a:N`). Uses
    mkvmerge for language info; ffprobe often reports 'und'."""
    s1, s2 = identify_tracks(f1, "audio"), identify_tracks(f2, "audio")
    if not s1 or not s2:
        raise RuntimeError(f"No audio tracks found in {f1.name if not s1 else f2.name}")

    def lang_of(t):
        return t.get("properties", {}).get("language", "und")

    langs1, langs2 = [lang_of(t) for t in s1], [lang_of(t) for t in s2]
    common = set(langs1) & set(langs2)

    if preferred_lang:
        if preferred_lang not in common:
            raise RuntimeError(
                f"--audio-lang {preferred_lang} not in both files "
                f"({f1.name}: {sorted(set(langs1))}, {f2.name}: {sorted(set(langs2))})")
        lang = preferred_lang
    else:
        usable = (common - {"und"}) or common
        if not usable:
            raise RuntimeError(
                f"No common audio language ({sorted(set(langs1))} vs {sorted(set(langs2))}) "
                "- pass --audio-lang if the tags are wrong")
        order = {l: i for i, l in enumerate(LANG_PRIORITY)}
        first_seen = {l: i for i, l in reversed(list(enumerate(langs1)))}
        lang = min(usable, key=lambda l: (order.get(l, len(order)), first_seen.get(l, 0)))

    def best_pos(tracks):
        def rank(item):
            pos, t = item
            props = t.get("properties", {})
            commentary = "comment" in props.get("track_name", "").lower()
            return (commentary, not props.get("default_track", False), pos)
        cands = [(i, t) for i, t in enumerate(tracks) if lang_of(t) == lang]
        return min(cands, key=rank)[0]

    return best_pos(s1), best_pos(s2), lang


def q(x: float) -> float:
    """Quantize to ms so the start time we ask ffmpeg for is exactly the one we use in the math."""
    return round(x, 3)


def extract_audio(path: Path, start: float, length: float, audio_pos: int):
    """Decode a mono 48 kHz float clip straight into memory."""
    out = run_cmd([
        "ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", str(path),
        "-t", f"{length:.3f}", "-map", f"0:a:{audio_pos}", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(SR), "-f", "f32le", "-",
    ])
    return np.frombuffer(out, dtype="<f4").astype(np.float64)


def stretch(x, ratio: Fraction):
    """Time-stretch by `ratio` (duration multiplies by ratio) via plain
    resampling - this is only for measurement, never for output files."""
    if ratio == 1:
        return x
    return resample_poly(x, ratio.numerator, ratio.denominator)


def ncc_search(ref, dist):
    """Slide `ref` along `dist` and return (best_index, ncc, prominence, at_edge).
    ncc is a true normalized cross-correlation in [-1, 1] (each window is
    normalized by its own energy); prominence is best / runner-up peak."""
    n = len(ref)
    if n == 0 or len(dist) < n:
        return None
    ref = ref - ref.mean()
    dist = dist - dist.mean()
    eref = float(ref @ ref)
    if eref < 1e-9:
        return None
    corr = correlate(dist, ref, mode="valid", method="fft")
    cs = np.concatenate(([0.0], np.cumsum(dist * dist)))
    window_energy = cs[n:] - cs[:-n]
    denom = np.sqrt(np.maximum(window_energy, 0.0) * eref)
    if denom.max() <= 0:
        return None
    denom = np.maximum(denom, denom.max() * 1e-3)   # don't let near-silent windows blow up
    ncc = corr / denom

    k = int(np.argmax(ncc))
    best = float(ncc[k])
    g = int(PEAK_GUARD_S * SR)
    lo, hi = max(0, k - g), min(len(ncc), k + g + 1)
    rest = np.concatenate((ncc[:lo], ncc[hi:]))
    second = float(rest.max()) if rest.size else 0.0
    prominence = min(best / second, 99.0) if second > 1e-6 else 99.0
    at_edge = k <= 0 or k >= len(ncc) - 1      # best match at the search boundary: true offset is probably outside it
    return k, best, prominence, at_edge


def measure_point(ref, f2, idx2, t1, clip, max_offset, ratio: Fraction, dur2, st1, st2, center=0.0):
    """Find where the ref clip (file1 at t1) sits in file2, assuming file2 is
    `ratio` times faster than file1. Returns the offset in the *stretched*
    domain: file2_time = (file1_time + offset) / ratio. For ratio 1 that is
    simply 'positive = file2 is delayed'. Times are raw container timestamps
    (what mkvmerge shifts), so each file's start time (st1, st2) is added to
    the relative `-ss` positions. The search window is centered on `center`
    seconds of offset (0 by default) and extends `max_offset` either side."""
    r = float(ratio)
    w0 = q(max(0.0, (t1 + center - max_offset) / r))
    w1 = q(min(dur2, (t1 + clip + center + max_offset) / r))
    if (w1 - w0) * r < len(ref) / SR + 0.05:
        return None
    dist = stretch(extract_audio(f2, w0, w1 - w0, idx2), ratio)
    res = ncc_search(ref, dist)
    if res is None:
        return None
    k, ncc, prom, edge = res
    return {"offset_s": r * (w0 + st2) + k / SR - (t1 + st1),
            "ncc": ncc, "prom": prom, "edge": edge}


def sample_times(shorter, clip, margin, n):
    n = max(2, n)
    if shorter < 2 * margin + clip:
        margin = max(0.0, (shorter - clip) / 3)
    earliest = margin
    latest = max(margin, shorter - margin - clip)
    if latest - earliest < 1.0:
        return [q(earliest)]
    return [q(earliest + i * (latest - earliest) / (n - 1)) for i in range(n)]


def summarize(points):
    valid = [p for p in points if p["valid"]]
    if not valid:
        return {"n_valid": 0, "offset_s": None, "spread_ms": None}
    offs = [p["offset_s"] for p in valid]
    return {"n_valid": len(valid), "offset_s": statistics.median(offs),
            "spread_ms": (max(offs) - min(offs)) * 1000.0}


def ms(seconds: float, digits: int = 1) -> float:
    """Seconds -> milliseconds rounded for display; never returns -0.0."""
    return round(seconds * 1000.0, digits) + 0.0


def describe_drift(points, drift_ms):
    """One-line description of how the offset changes across the episode."""
    v = sorted((p for p in points if p["valid"]), key=lambda p: p["time_s"])
    jumps = [(a, b) for a, b in zip(v, v[1:])
             if abs(b["offset_s"] - a["offset_s"]) * 1000.0 > drift_ms]
    if not jumps:
        return "offset changes gradually across the episode (not a known frame-rate ratio)"
    if len(jumps) == 1:
        a, b = jumps[0]
        return (f"offset steps from {ms(a['offset_s'], 0):+.0f} ms to {ms(b['offset_s'], 0):+.0f} ms "
                f"between t={a['time_s']:.0f}s and t={b['time_s']:.0f}s "
                f"(usually means the two files are cut differently there)")
    return f"offset changes {len(jumps)} times across the episode"


def check_pair(f1: Path, f2: Path, args):
    """Returns (ratio, points, summary, status, note)."""
    dur1, dur2 = probe_duration(f1), probe_duration(f2)
    st1, st2 = probe_start_time(f1), probe_start_time(f2)
    idx1, idx2, lang = pick_audio_tracks(f1, f2, args.audio_lang)
    print(f"    measuring on '{lang}' audio (track {idx1} / track {idx2})")

    times = sample_times(min(dur1, dur2), args.clip_length, args.margin, args.sample_points)
    need = max(2, math.ceil(len(times) / 2))
    refs = {}

    def run_ratio(ratio):
        pts = []
        for t in times:
            if t not in refs:
                refs[t] = extract_audio(f1, t, args.clip_length, idx1)
            ref = refs[t]
            m = None
            if len(ref) >= args.clip_length * SR * 0.9:
                m = measure_point(ref, f2, idx2, t, args.clip_length, args.max_offset, ratio, dur2, st1, st2)
            if m is None:
                pts.append({"time_s": t, "offset_s": 0.0, "ncc": 0.0, "prom": 0.0, "valid": False})
            else:
                m["valid"] = (m["ncc"] >= args.min_confidence and m["prom"] >= MIN_PROMINENCE
                              and not m["edge"])
                m["time_s"] = t
                pts.append(m)
        return pts

    def acceptable(s):
        return s["n_valid"] >= need and s["spread_ms"] <= args.drift_ms

    one = Fraction(1)
    pts1 = run_ratio(one)
    cands = [(one, pts1, summarize(pts1))]
    chosen = cands[0]

    if not acceptable(chosen[2]):
        print("    no consistent offset at 1:1 - trying known frame-rate ratios...")
        for ratio in STRETCH_RATIOS:
            pts = run_ratio(ratio)
            cands.append((ratio, pts, summarize(pts)))
        good = [cd for cd in cands if acceptable(cd[2])]
        if good:
            chosen = max(good, key=lambda cd: (cd[2]["n_valid"], -cd[2]["spread_ms"]))
        else:
            # Nothing is fully consistent (e.g. the two releases are cut differently).
            # Report the candidate that matched the most clips rather than blindly
            # the 1:1 attempt: with a real frame-rate difference the 1:1 attempt is
            # pure noise and would hide what is actually going on. Ties go to 1:1,
            # which comes first.
            chosen = max(cands, key=lambda cd: (
                cd[2]["n_valid"], -(cd[2]["spread_ms"] if cd[2]["spread_ms"] is not None else 1e9)))

    ratio, pts, summ = chosen
    if summ["n_valid"] < 2:
        status = "LOW_CONFIDENCE"
    elif summ["spread_ms"] > args.drift_ms:
        status = "DRIFT_DETECTED"
    elif summ["n_valid"] < need:
        status = "LOW_CONFIDENCE"
    elif ratio != 1:
        status = "STRETCHED"
    elif abs(summ["offset_s"]) * 1000.0 < SYNCED_MS:
        status = "SYNCED"
    else:
        status = "FIXED_OFFSET"
    if status == "LOW_CONFIDENCE":
        ratio, pts, summ = cands[0]      # a ratio backed by fewer than 2 clips isn't trustworthy
    note = ""
    if status == "DRIFT_DETECTED":
        # No single offset is right for the whole episode. Report the one at
        # the start (what --force uses), not a median that fits neither end.
        first = next(p for p in pts if p["valid"])
        summ = dict(summ, offset_s=first["offset_s"])
        note = describe_drift(pts, args.drift_ms)
    return ratio, pts, summ, status, note


# --------------------------------------------------------------------------
# `check` subcommand
# --------------------------------------------------------------------------

REPORT_FIELDS = ["file1", "file2", "status", "ratio", "offset_ms", "picture_ms", "av_ms", "spread_ms",
                 "valid_points", "total_points", "min_confidence", "points", "note", "steps", "error"]


def cmd_check(args):
    require_tools("ffmpeg", "ffprobe", "mkvmerge")
    pairs = pair_files(args.dir1, args.dir2)
    if not pairs:
        sys.exit("No matched pairs found.")
    print(f"Found {len(pairs)} matched pair(s). Checking sync...\n")

    counts = defaultdict(int)
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for f1, f2 in pairs:
            row = {k: "" for k in REPORT_FIELDS}
            row.update(file1=str(f1.resolve()), file2=str(f2.resolve()))
            print(f"  {c(f1.name, 'cyan')}  <->  {c(f2.name, 'cyan')}")
            try:
                ratio, pts, summ, status, note = check_pair(f1, f2, args)
            except Exception as e:
                print(f"    {c('ERROR', 'red')}: {e}", file=sys.stderr)
                row.update(status="ERROR", error=str(e))
            else:
                for p in pts:
                    tag = "" if p["valid"] else c("  (ignored: weak match)", "dim")
                    print(f"    t={p['time_s']:.1f}s -> offset {ms(p['offset_s']):+.1f} ms "
                          f"(corr {p['ncc']:.2f}, prominence {p['prom']:.1f}){tag}")
                if summ["n_valid"]:
                    extra = f", ratio {ratio}" if ratio != 1 else ""
                    what = "start offset" if status == "DRIFT_DETECTED" else "offset"
                    print(f"    {what} {ms(summ['offset_s']):+.1f} ms, spread "
                          f"{summ['spread_ms']:.1f} ms{extra} -> {status_text(status)}")
                else:
                    print(f"    no clip matched, even after trying known frame-rate ratios -> "
                          f"{status_text(status)} (try a larger --max-offset or lower "
                          f"--min-confidence, or the audio may be a different mix)")
                if note:
                    print(f"    {c(note, 'dim')}")
                valid = [p for p in pts if p["valid"]]
                if (getattr(args, "video", False) and ratio == 1 and summ["n_valid"]
                        and status in ("SYNCED", "FIXED_OFFSET")):
                    s1_, s2_ = probe_start_time(f1), probe_start_time(f2)
                    pv = [r_["P"] for r_ in (picture_offset(f1, f2, p["time_s"], args.clip_length, s1_, s2_,
                                                            p["offset_s"]) for p in valid) if r_]
                    if pv:
                        pic_ms = ms(statistics.median(pv))
                        row.update(picture_ms=pic_ms, av_ms=round(ms(summ["offset_s"]) - pic_ms, 1))
                        print(f"    picture offset {pic_ms:+.1f} ms ({pic_ms / FRAME_MS:+.1f} frames); "
                              f"audio-picture {row['av_ms']:+.1f} ms")
                    else:
                        print("    picture offset: not measurable (static or ambiguous scenes)")
                row.update(
                    status=status, ratio=str(ratio), note=note,
                    offset_ms=ms(summ["offset_s"]) if summ["n_valid"] else "",
                    spread_ms=round(summ["spread_ms"], 1) if summ["n_valid"] else "",
                    valid_points=summ["n_valid"], total_points=len(pts),
                    min_confidence=round(min((p["ncc"] for p in valid), default=0.0), 3),
                    points=";".join(f"{p['time_s']}:{ms(p['offset_s']):.1f}:{p['ncc']:.3f}:"
                                    f"{int(p['valid'])}" for p in pts),
                )
            counts[row["status"]] += 1
            writer.writerow(row)
            fh.flush()       # keep partial results if the run is interrupted

    print(f"\nReport written to {args.out}")
    ok = counts["SYNCED"] + counts["FIXED_OFFSET"] + counts["STRETCHED"]
    bad = counts["DRIFT_DETECTED"] + counts["LOW_CONFIDENCE"] + counts["ERROR"]
    print(f"Summary: {c(str(ok) + ' usable', 'green')} "
          f"(of which {counts['STRETCHED']} stretched), "
          f"{c(str(bad) + ' need attention', 'red' if bad else 'dim')}")


# --------------------------------------------------------------------------
# `locate` subcommand: find exactly where the offset changes
# --------------------------------------------------------------------------

SCAN_CLIP_S = 10.0        # length of each coarse-scan clip
FINE_WINDOW_S = 0.2       # window used to classify audio as "offset A" or "offset B"
FINE_HOP_S = 0.01
FINE_WINDOW2_S = 0.05     # second-pass window and hop, used only around the seam
FINE_HOP2_S = 0.002
FINE_PAD_S = 0.4          # how far around the first-pass zone the second pass looks
FINE_BASE = 0.1           # correlation below this counts as no evidence (noise floor)
FINE_TOL = 1.5            # seam positions within this much evidence of the best are "the zone"
FINE_CONF = 0.45          # a window must correlate at least this well at one offset...
FINE_MARGIN = 0.25        # ...and beat the other offset by this much to count as evidence


def fmt_t(seconds: float, decimals: int = 1) -> str:
    scale = 10 ** decimals
    units = int(round(max(seconds, 0.0) * scale))
    m, rem = divmod(units, 60 * scale)
    width = 3 + decimals if decimals else 2
    return f"{m}:{rem / scale:0{width}.{decimals}f}"


def av_report(steps, segments, st1):
    """Lines comparing the audio offset with the picture offset per segment, and a verdict."""
    if not any("picture_ms" in sg for sg in segments):
        return []
    lines, start = ["audio vs picture (file2 minus file1):"], 0.0
    bounds = []
    for i, seg in enumerate(segments):
        end = max(steps[i]["at"] + st1, start) if i < len(steps) else None
        bounds.append((start, end))
        if "picture_ms" in seg:
            lines.append(f"  {fmt_t(start)}-{fmt_t(end) if end is not None else 'end':>7}  "
                         f"audio {seg['offset_ms']:+7.1f} ms   picture {seg['picture_ms']:+7.1f} ms "
                         f"({seg['picture_ms'] / FRAME_MS:+.1f} frames)   audio-picture {seg['av_ms']:+6.1f} ms")
        else:
            lines.append(f"  {fmt_t(start)}-{fmt_t(end) if end is not None else 'end':>7}  "
                         f"audio {seg['offset_ms']:+7.1f} ms   picture: not measurable (static or ambiguous)")
        if end is not None:
            start = end
    for i, seg in enumerate(segments):
        if seg.get("picture_spread_ms", 0) > 25.0:
            lines.append(f"  !! the picture offset varies by {seg['picture_spread_ms']:.0f} ms inside the segment "
                         f"starting at {fmt_t(bounds[i][0])}: a picture-only change that the audio doesn't show")
    have = [(i, sg["av_ms"]) for i, sg in enumerate(segments) if "av_ms" in sg]
    jumps = []
    for (i, a), (j, b) in zip(have, have[1:]):
        if abs(b - a) > 25.0:
            jumps.append(f"audio-minus-picture changes by {b - a:+.0f} ms near "
                         f"{fmt_t(bounds[j][0])}")
    if jumps:
        lines.append("  -> " + "; ".join(jumps) + ". Frame rounding explains at most about +/-21 ms, so "
                     "in one of the two releases the audio is out of sync with its own picture there "
                     "(this compares the releases; it can't say which one).")
    elif have:
        spread = max(b for _, b in have) - min(b for _, b in have)
        lines.append(f"  -> audio-minus-picture varies by only {spread:.0f} ms across the episode: the two "
                     f"releases agree about audio vs picture (differences are frame rounding).")
    return lines


def timeline_text(steps, segments, st1):
    """e.g. '0:00-1:56 +5 ms | 1:56-21:45 -29 ms (1 clip) | ~21:45-end -43 ms'
    (a leading ~ marks an uncertain boundary)."""
    parts, start, mark = [], 0.0, ""
    for i, seg in enumerate(segments):
        end = max(steps[i]["at"] + st1, start) if i < len(steps) else None
        label = (f"{mark}{fmt_t(start)}-{fmt_t(end) if end is not None else 'end'} "
                 f"{seg['offset_ms']:+.0f} ms")
        if seg["clips"] == 1:
            label += " (1 clip)"
        parts.append(label)
        if end is not None:
            stp = steps[i]
            width = stp["zone_end"] - stp["zone_start"]
            start = end
            mark = ("~" if stp["uncertain"] else "")
            if stp["uncertain"] and width > 1.0:
                parts[-1] += f" (boundary +/-{width / 2:.0f}s)"
    return " | ".join(parts)


def black_ranges(path: Path, start: float, end: float, pix_th: float = 0.10, min_len: float = 0.1):
    """Black-screen stretches between `start` and `end` (seconds, relative like
    extract_audio's start). Used only as a hint about where a splice may be. min_len is
    the shortest stretch reported (0.04 s = one frame at 23.976 fps)."""
    start = max(0.0, start)
    try:
        err = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
             "-i", str(path), "-an", "-sn", "-vf", f"scale=160:-2,blackdetect=d={min_len}:pix_th={pix_th}",
             "-f", "null", "-"], capture_output=True).stderr.decode(errors="replace")
    except OSError:
        return []
    return [(start + float(a), start + float(b))
            for a, b in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)", err)]


def match_black(f1, f2, st1, st2, z0, z1, off_a, off_b, with_file2=True):
    """Black-screen stretches near a located step (z0..z1 are file1 container
    seconds). A splice inside black screen shows up as the two files' black
    stretches differing in length by exactly the size of the step, so also try
    to pair each file1 stretch with the one in file2 where it should be.
    Returns {"c1": [file1 stretches], "pair": best pairing or None}; times are
    container seconds."""
    def in_zone(rs):
        return [(a + st1, b + st1) for a, b in rs if b + st1 > z0 - 1.0 and a + st1 < z1 + 1.0]

    c1 = in_zone(black_ranges(f1, z0 - 3.0 - st1, z1 + 3.0 - st1))
    if not c1:                      # a black of one or two frames (< 0.1 s) is easy to miss
        c1 = in_zone(black_ranges(f1, z0 - 3.0 - st1, z1 + 3.0 - st1, min_len=0.04))
    out = {"c1": c1, "pair": None}
    if not c1 or not with_file2:
        return out
    delta = off_b - off_a
    # file2 may be noisier or have a raised black level, so retry with looser thresholds when
    # the strict one finds nothing that fits. A looser threshold also counts dark fade-in frames
    # as black, so such a pair is marked "loose" and must not be trusted blindly. file2's stretch
    # may be shorter than 0.1 s (a splice that removes black frames), hence min_len 0.04.
    for pix_th in (0.10, 0.25, 0.40):
        for s1, e1 in c1:
            exp0, exp1 = s1 + off_a, e1 + off_b            # where file2's stretch should be
            lo, hi = min(exp0, exp1) - 3.0, max(exp0, exp1) + 3.0
            for a, b in black_ranges(f2, lo - st2, hi - st2, pix_th, min_len=0.04):
                s2, e2 = a + st2, b + st2
                if e2 < min(exp0, exp1) - 1.0 or s2 > max(exp0, exp1) + 1.0:
                    continue
                err = abs((e1 - s1) - (e2 - s2) + delta)   # file1 should be longer by -delta
                if out["pair"] is None or err < out["pair"]["err"]:
                    out["pair"] = {"b1": (s1, e1), "b2": (s2, e2), "err": err, "loose": pix_th > 0.10,
                                   "off_start": s2 - s1, "off_end": e2 - e1}
        if out["pair"] is not None and out["pair"]["err"] <= 0.06:
            break
    return out


VID_W, VID_H = 64, 36
FRAME_MS = 1001 / 24.0                          # one frame at 23.976 fps, in ms


def frame_signal(path: Path, start_rel: float, length: float, st: float):
    """Decode a short clip to tiny grey frames. Returns (container_pts_seconds, frames);
    container time = seek position + the frame's own time + the file's start time.
    -fps_mode passthrough matters: by default ffmpeg duplicates or drops frames to hold a
    constant rate, which would shift every later frame against its timestamp."""
    start_rel = max(0.0, start_rel)
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start_rel:.3f}", "-t", f"{length:.3f}",
         "-i", str(path), "-an", "-sn", "-fps_mode", "passthrough", "-vf",
         f"scale={VID_W}:{VID_H}:flags=area,format=gray,showinfo", "-f", "rawvideo", "-"],
        capture_output=True)
    pts = [float(x) for x in re.findall(r"pts_time:\s*(-?[\d.]+)", p.stderr.decode(errors="replace"))]
    k = min(len(p.stdout) // (VID_W * VID_H), len(pts))
    frames = np.frombuffer(p.stdout, np.uint8)[:k * VID_W * VID_H].reshape(k, -1).astype(np.float32)
    return start_rel + np.array(pts[:k]) + st, frames


def norm_frames(fr):
    """Per-frame zero-mean, unit-length vectors (so encoder quality and levels don't matter);
    flat or black frames carry no information and are flagged not-ok."""
    fr = fr - fr.mean(axis=1, keepdims=True)
    sd = np.linalg.norm(fr, axis=1, keepdims=True)
    return np.where(sd > 1.0, fr / np.maximum(sd, 1e-9), 0.0), sd[:, 0] > 1.0


PIC_MARGIN_OK = 0.04         # best frame lag beats the runner-up by this much: clear
PIC_MARGIN_MIN = 0.008       # below this a lag is never trusted, however consistent
PIC_DIFF_EPS = 0.004         # per-frame match difference below this is encoder noise
PIC_INFORMATIVE_MIN = 10     # frames where nearby lags differ, needed to pin the lag
PIC_AGREE_MIN = 0.9          # share of those frames that must favour the best lag


def lag_consistency(sim, ok1, k, n1, n2):
    """Sign test: over the frames where lag k and a lag within 3 frames of it match differently
    (by more than encoder noise), how many favour k? Returns (frames, share) for the worst
    neighbouring lag. A lag pinned by drawing changes wins on nearly every such frame; in a
    static clip there are few of them and they split evenly."""
    dk = np.diagonal(sim, offset=k)[ok1]
    worst = (10 ** 9, 1.0)
    for k2 in range(max(0, k - 3), min(n2 - n1, k + 3) + 1):
        if k2 == k:
            continue
        d = dk - np.diagonal(sim, offset=k2)[ok1]
        pos, neg = int((d > PIC_DIFF_EPS).sum()), int((d < -PIC_DIFF_EPS).sum())
        share = pos / (pos + neg) if pos + neg else 0.0
        if (pos + neg, share) < worst or share < worst[1]:
            worst = (min(worst[0], pos + neg), min(worst[1], share))
    return worst


def picture_offset(f1, f2, t1, clip, st1, st2, audio_off, slack=2.5, why=None):
    """Offset (file2 - file1, seconds) of the PICTURE at file1 time t1. Every frame of
    the file1 clip is compared with the frames of file2 (normalised per frame, so
    encoder quality and levels don't matter) to find which frame lag makes the pictures
    agree; the offset is then read from the matched frames' own timestamps. Independent
    of audio. Returns None for static or ambiguous clips (e.g. a held still image); if
    `why` is a dict it receives {"reason": ..., "best": best match} for those."""
    def fail(msg, best=None):
        if why is not None:
            why.update(reason=msg, best=best)
        return None

    c1, fr1 = frame_signal(f1, t1, clip, st1)
    c2, fr2 = frame_signal(f2, t1 + st1 + audio_off - st2 - slack, clip + 2 * slack, st2)
    if len(fr1) < 48 or len(fr2) < len(fr1) + 4:
        return fail(f"too few frames decoded ({len(fr1)} in file1, {len(fr2)} in file2)")

    z1, ok1 = norm_frames(fr1)
    z2, _ = norm_frames(fr2)
    if ok1.sum() < 40:
        return fail(f"picture is black or flat ({int(ok1.sum())} usable frames)")
    sim = z1 @ z2.T                                     # per-frame correlation, file1 frame x file2 frame
    n1, n2 = sim.shape
    score = np.array([float(np.diagonal(sim, offset=k)[ok1].mean()) for k in range(n2 - n1 + 1)])
    k = int(np.argmax(score))
    others = np.delete(score, k)
    if score[k] < 0.7:
        return fail(f"the pictures look different (best match {score[k]:.2f}, needs 0.7)", float(score[k]))
    margin = score[k] - float(others.max()) if others.size else 1.0
    if margin < PIC_MARGIN_OK:
        # Anime is drawn on 2s/3s, so a lag one frame off can match almost as well as the right one
        # even in a moving scene. What separates that from a really static picture is consistency:
        # the best lag must beat each nearby lag on (nearly) every frame where the drawing changes,
        # which a paired per-frame test measures; static clips only differ by encoder noise.
        n_inf, share = lag_consistency(sim, ok1, k, n1, n2)
        if margin < PIC_MARGIN_MIN or n_inf < PIC_INFORMATIVE_MIN or share < PIC_AGREE_MIN:
            return fail(f"neighbouring frame lags match as well (margin {margin:.3f}; only {n_inf} frames tell "
                        f"them apart, {share * 100:.0f}% favour the best): nearly static picture", float(score[k]))
    idx = np.flatnonzero(ok1)
    diffs = c2[idx + k] - c1[idx]                       # matched frames: file2 time - file1 time
    if diffs.std() > 0.01:
        return fail(f"frame times not constant (spread {diffs.std() * 1000:.0f} ms): variable frame rate, "
                    f"or a splice inside the clip", float(score[k]))
    return {"P": float(np.median(diffs)), "ncc": float(score[k])}


def find_junction(f1, f2, st1, st2, z0, z1, p_a, p_b, debug_prefix=None):
    """Frame-exact location of a PICTURE step that is not in black screen. z0..z1 (file1
    container seconds) bracket it; p_a/p_b are the picture offsets (file2 minus file1,
    seconds) before and after. For every file1 frame, compare how well it matches the
    file2 frame the "before" offset points at and the one the "after" offset points at;
    the junction is the single change point that best explains all frames. Frames that
    look the same under both hypotheses (stills, holds) carry no evidence, so the result
    says how many frames the junction is ambiguous over. The window is widened step by
    step until there is enough evidence. On failure returns {"error": reason}."""
    reason = "no frames could be decoded"
    for pad in (4.0, 10.0, 20.0):
        lo, hi = z0 - pad, z1 + pad
        if hi - lo > 45.0:
            mid = (z0 + z1) / 2
            lo, hi = mid - 22.5, mid + 22.5
        c1, fr1 = frame_signal(f1, max(0.0, lo - st1), hi - lo, st1)
        lo_p, hi_p = min(p_a, p_b), max(p_a, p_b)
        c2, fr2 = frame_signal(f2, lo + lo_p - st2 - 1.0, (hi - lo) + (hi_p - lo_p) + 2.0, st2)
        if len(fr1) < 48 or len(fr2) < 48:
            reason = "too few frames could be decoded around it"
            continue
        z1n, ok1 = norm_frames(fr1)
        z2n, _ = norm_frames(fr2)
        sim = z1n @ z2n.T
        n = len(sim)

        def frame_at(offset):                   # the file2 frame each file1 frame lines up with
            j = np.clip(np.searchsorted(c2, c1 + offset), 1, len(c2) - 1)
            return np.where(np.abs(c2[j - 1] - (c1 + offset)) < np.abs(c2[j] - (c1 + offset)), j - 1, j)

        ja, jb_ = frame_at(p_a), frame_at(p_b)
        s_a, s_b = sim[np.arange(n), ja], sim[np.arange(n), jb_]
        valid = ok1 & (np.maximum(s_a, s_b) >= 0.5)
        d = np.where(valid, s_b - s_a, 0.0)     # > 0: the frame looks like the "after" picture
        if debug_prefix:
            np.savetxt(f"{debug_prefix}_pad{pad:g}.csv", np.column_stack([c1 + 0, s_a, s_b, d, valid]),
                       delimiter=",", comments="", fmt="%.5f", header="t1_container_s,sim_before,sim_after,diff,valid")
        if valid.sum() < 30:
            reason = ("the pictures look too different between the two files around it "
                      "(best match under 0.5), or the frames are black/flat")
            continue
        n_a, n_b = int((d < -0.01).sum()), int((d > 0.01).sum())
        if n_a < 6 or n_b < 6:
            reason = (f"only {n_a} frames before and {n_b} after tell the two offsets apart: "
                      f"the picture is nearly static around it")
            continue
        cs = np.concatenate(([0.0], np.cumsum(d)))
        k0 = int(np.argmin(cs))                 # frames before k0 are "before", from k0 on "after"
        plateau = np.flatnonzero(cs <= cs[k0] + 0.05)
        run = plateau[(plateau >= k0 - 60) & (plateau <= k0 + 60)]
        k_lo, k_hi = int(run.min()), int(run.max())
        k = (k_lo + k_hi) // 2
        jb = int(jb_[min(max(k, 0), n - 1)])
        if k < 1 or k >= n or jb < 1:
            reason = "the change point sits at the edge of the examined frames"
            continue
        return {"t1_last_a": float(c1[k - 1]), "t1_first_b": float(c1[k]), "span_frames": k_hi - k_lo,
                "t2_first_b": float(c2[jb]), "p_a": float(p_a), "p_b": float(p_b), "n_a": n_a, "n_b": n_b}
    return {"error": reason}


def align(dist, lag, n):
    """dist shifted so that out[i] = dist[i + lag] (zero padded), length n."""
    d = np.zeros(n)
    lo, hi = max(0, -lag), min(n, len(dist) - lag)
    if hi > lo:
        d[lo:hi] = dist[lo + lag:hi + lag]
    return d


def lag_ncc(ref, d, win, hop):
    """Windowed normalized correlation of ref against the aligned signal d.
    Returns (window_starts, ncc, ref_energy, d_energy). Cumulative sums make
    sliding a short window along a long clip cheap."""
    n = len(ref)

    def cs(x):
        return np.concatenate(([0.0], np.cumsum(x)))
    cp, c1, c2 = cs(ref * d), cs(ref * ref), cs(d * d)
    starts = np.arange(0, n - win + 1, hop)
    sp = cp[starts + win] - cp[starts]
    s1 = c1[starts + win] - c1[starts]
    s2 = c2[starts + win] - c2[starts]
    denom = np.sqrt(np.maximum(s1, 0.0) * np.maximum(s2, 0.0))
    ncc = np.where(denom > 1e-12, sp / np.maximum(denom, 1e-12), 0.0)
    return starts, ncc, s1, s2


def locate_step(f1, f2, idx1, idx2, ratio, st1, st2, dur2, t_a, t_b, off_a, off_b,
                debug_prefix=None):
    """Find where, between coarse clips at t_a and t_b (file1 relative seconds),
    the offset changes from off_a to off_b (seconds).

    The model: file1 samples before the seam match file2 at offset A and
    samples after it match at offset B. If B < A, file1 additionally has
    |B - A| seconds right after the seam that file2 lacks (a "dead" stretch
    matching neither). The seam is the position that maximises the total
    correlation of both parts, which uses all the audio on both sides instead
    of thresholded windows. Where the audio around the seam is quiet the score
    is flat, so the zone returned is every position within a small tolerance of
    the best score (a few ms in loud audio, the whole quiet stretch otherwise).
    Returns times in file1 relative seconds."""
    r = float(ratio)
    i0, i1 = q(max(0.0, t_a)), q(t_b + SCAN_CLIP_S)
    ref = extract_audio(f1, i0, i1 - i0, idx1)
    pad = 1.0
    w0 = q(max(0.0, (i0 + st1 + min(off_a, off_b)) / r - st2 - pad))
    w1 = q(min(dur2, (i1 + st1 + max(off_a, off_b)) / r - st2 + pad))
    dist = stretch(extract_audio(f2, w0, w1 - w0, idx2), ratio)
    ref, dist = ref - ref.mean(), dist - dist.mean()
    n = len(ref)

    def lag(o):
        return int(round((i0 + st1 + o - r * (w0 + st2)) * SR))

    d_a, d_b = align(dist, lag(off_a), n), align(dist, lag(off_b), n)

    # Per-window evidence for each offset. Each window is normalised by its own
    # energy, so quiet dialogue counts as much as loud music (raw correlation
    # sums are dominated by whatever is loudest, e.g. an OP).
    win, hop = int(FINE_WINDOW_S * SR), int(FINE_HOP_S * SR)
    starts, na, e1, ea = lag_ncc(ref, d_a, win, hop)
    _, nb, _, eb = lag_ncc(ref, d_b, win, hop)
    # Near-silent windows must not count: the correlation of two almost
    # constant signals is meaningless (it can read 1.0). The energy floor comes
    # from the whole interval so that a quiet neighbourhood can't lower it.
    floor = max(1e-12, 0.02 * float(np.median(e1)))
    ref_loud = e1 > floor
    ok_a, ok_b = ref_loud & (ea > floor), ref_loud & (eb > floor)
    lab_a = ok_a & (na >= FINE_CONF) & (na - nb >= FINE_MARGIN)
    lab_b = ok_b & (nb >= FINE_CONF) & (nb - na >= FINE_MARGIN)
    ev_a = np.where(ok_a, np.clip(na - FINE_BASE, 0.0, None), 0.0)
    ev_b = np.where(ok_b, np.clip(nb - FINE_BASE, 0.0, None), 0.0)

    # Maximum-likelihood seam: windows before it should match A, windows after it
    # (and after the dead stretch, if file1 has extra material) should match B.
    kk = len(na)
    cea = np.concatenate(([0.0], np.cumsum(ev_a)))
    ceb = np.concatenate(([0.0], np.cumsum(ev_b)))
    dead_w = int(round(-(off_b - off_a) * SR / hop)) if off_b < off_a else 0
    j = np.arange(kk + 1)
    score = cea[j] + (ceb[kk] - ceb[np.minimum(j + dead_w, kk)])
    jb = int(np.argmax(score))
    best = float(score[jb])
    ok = np.flatnonzero(score >= best - FINE_TOL)
    breaks = np.flatnonzero(np.diff(ok) > 1)
    run_starts = np.concatenate(([0], breaks + 1))
    run_ends = np.concatenate((breaks, [len(ok) - 1]))
    rid = int(np.searchsorted(breaks, int(np.searchsorted(ok, jb)), side="left"))
    j0, j1 = int(ok[run_starts[rid]]), int(ok[run_ends[rid]])

    def seam(jv):      # seam lies between the centres of windows jv-1 and jv
        return i0 + (jv * hop + win / 2 - hop / 2) / SR

    base = max(float(score[0]), float(score[kk]))      # "no step at all" alternatives
    gain = (best - base) / best if best > 1e-12 else 0.0
    abs_gain = best - base
    zone_start, zone_end = seam(j0), seam(j1)
    edge = jb <= 3 or jb >= kk - 3                      # stuck at the edge of the scanned stretch
    # Does each side of the seam actually match its own offset? (fraction of the
    # non-silent windows on that side that clearly agree with it)
    after = min(kk, jb + dead_w)
    la, lb = int(ref_loud[:jb].sum()), int(ref_loud[after:].sum())
    n_a, n_b = int(lab_a[:jb].sum()), int(lab_b[after:].sum())
    match = min(n_a / la if la else 0.0, n_b / lb if lb else 0.0)

    # Bracket: from the last audio that clearly matches A to the first audio that
    # clearly matches B. Where the splice sits in a silent (or non-matching)
    # stretch, the audio can't say where inside it the change happened, so the
    # honest answer is the whole stretch.
    smooth = np.ones(100) / 100.0                       # ~1 s majority vote, ignores stray windows
    pres_a = np.convolve(lab_a.astype(float), smooth, mode="same") >= 0.5
    pres_b = np.convolve(lab_b.astype(float), smooth, mode="same") >= 0.5
    ia = np.flatnonzero(pres_a)
    i_a_end = int(ia[-1]) if ia.size else -1
    ib = np.flatnonzero(pres_b)
    ib = ib[ib > i_a_end]
    i_b_start = int(ib[0]) if ib.size else kk - 1
    ca = np.flatnonzero(lab_a[:min(kk, i_a_end + 51)])
    lo_b = max(0, i_b_start - 50)
    cb = np.flatnonzero(lab_b[lo_b:]) + lo_b

    def centre(i):
        return i0 + (i * hop + win / 2) / SR
    bracket_start = centre(int(ca[-1])) - FINE_WINDOW_S / 2 if ca.size else i0
    bracket_end = centre(int(cb[0])) + FINE_WINDOW_S / 2 if cb.size else i1
    q0, q1 = int((bracket_start - i0) * SR / hop), int((bracket_end - i0) * SR / hop) + 1
    quiet = bool(q1 > q0 and float((~ref_loud[q0:q1]).mean()) >= 0.7)

    if debug_prefix:
        t_c = i0 + (starts + win / 2) / SR + st1
        np.savetxt(debug_prefix + ".csv",
                   np.column_stack([t_c, na, nb, e1, ea, eb, lab_a, lab_b, ev_a, ev_b]),
                   delimiter=",", comments="", fmt="%.5f",
                   header="t_container_s,ncc_a,ncc_b,ref_energy,a_energy,b_energy,lab_a,lab_b,ev_a,ev_b")

    # Second pass on just the neighbourhood with short windows. A long window
    # straddling the seam is dominated by its louder side, which would bias the
    # position by up to half a window.
    win2, hop2 = int(FINE_WINDOW2_S * SR), int(FINE_HOP2_S * SR)
    dead_s = -(off_b - off_a) if off_b < off_a else 0.0
    r0 = max(0, int((zone_start - FINE_PAD_S - i0) * SR))
    r1 = min(n, int((zone_end + dead_s + FINE_PAD_S - i0) * SR))
    if not edge and r1 - r0 > 8 * win2:
        sr_, sa_, sb_ = ref[r0:r1], d_a[r0:r1], d_b[r0:r1]
        _, na2, e2, ea2 = lag_ncc(sr_, sa_, win2, hop2)
        _, nb2, _, eb2 = lag_ncc(sr_, sb_, win2, hop2)
        floor2 = floor * win2 / win                  # same absolute floor, scaled to the shorter window
        ev_a2 = np.where((e2 > floor2) & (ea2 > floor2), np.clip(na2 - FINE_BASE, 0.0, None), 0.0)
        ev_b2 = np.where((e2 > floor2) & (eb2 > floor2), np.clip(nb2 - FINE_BASE, 0.0, None), 0.0)
        k2 = len(na2)
        cea2 = np.concatenate(([0.0], np.cumsum(ev_a2)))
        ceb2 = np.concatenate(([0.0], np.cumsum(ev_b2)))
        dead_w2 = int(round(dead_s * SR / hop2))
        j2 = np.arange(k2 + 1)
        score2 = cea2[j2] + (ceb2[k2] - ceb2[np.minimum(j2 + dead_w2, k2)])
        jb2 = int(np.argmax(score2))
        ok2 = np.flatnonzero(score2 >= float(score2[jb2]) - FINE_TOL)
        br2 = np.flatnonzero(np.diff(ok2) > 1)
        rs2 = np.concatenate(([0], br2 + 1))
        re2 = np.concatenate((br2, [len(ok2) - 1]))
        rid2 = int(np.searchsorted(br2, int(np.searchsorted(ok2, jb2)), side="left"))
        a2, b2 = int(ok2[rs2[rid2]]), int(ok2[re2[rid2]])

        def seam2(jv):
            return i0 + (r0 + jv * hop2 + win2 / 2 - hop2 / 2) / SR
        if 3 < jb2 < k2 - 3:                             # refinement is only trusted away from its own edges
            # A window that straddles the seam is dominated by its louder side,
            # so the estimate can lean up to half a window either way; pad the
            # zone by that much so it always contains the true position.
            zone_start, zone_end = seam2(a2) - win2 / 2 / SR, seam2(b2) + win2 / 2 / SR
    uncertain = bool(match < 0.25 or abs_gain < 100 or n_a == 0 or n_b == 0
                     or zone_end - zone_start > 2.0 or edge)
    if uncertain and bracket_end > bracket_start:
        zone_start, zone_end = bracket_start, bracket_end      # report the whole stretch
    return {"at": (zone_start + zone_end) / 2, "zone_start": zone_start, "zone_end": zone_end,
            "gain": gain, "abs_gain": abs_gain, "match": match, "n_a": n_a, "n_b": n_b,
            "edge": edge, "uncertain": uncertain, "quiet": quiet,
            "bracket": (bracket_start, bracket_end)}


PIC_STEP_TOL = 0.02          # picture offsets of two clips in one audio segment differing by more: a picture-only step
REFINE_GAP_S = 16.0          # narrow the stretch to search for a picture-only junction down to about this


def pin_step_junction(f1, f2, st1, st2, res, pa, pb, debug_prefix=None):
    """Set res["junction"] for an audio-located step from the picture offsets measured before
    (pa) and after (pb) it: None if the black screen explains it or the picture doesn't change,
    a junction dict if pinned, {"error": reason} if not."""
    res["junction"] = None
    res.pop("junction_hint", None)
    if pa and pb:
        p_a, p_b = statistics.median(pa[-8:]), statistics.median(pb[:8])
        bp = res["blackpair"]
        d_black = (bp["off_end"] - bp["off_start"]) if bp else 0.0
        explained = bool(bp and abs(d_black) > 0.025 and abs(d_black - (p_b - p_a)) <= 0.06)
        if abs(p_b - p_a) > 0.025 and not explained:
            res["junction"] = find_junction(f1, f2, st1, st2, res["zone_start"] + st1, res["zone_end"] + st1,
                                            p_a, p_b, debug_prefix=debug_prefix)
            res["junction_hint"] = (p_a, p_b)
    else:
        res["junction"] = {"error": "no picture offset could be measured on "
                                    + ("either side" if not pa and not pb else "one side") + " of the step"}


def refine_picture_gap(f1, f2, st1, st2, ta, p_a, tb, p_b, a_off):
    """The picture offset is p_a in the clip at ta and p_b in the clip at tb (file1 relative
    seconds, SCAN_CLIP_S long). Probe extra clips in between to narrow down where it changes.
    Returns (lo, hi), file1 relative seconds, between which the change happens."""
    lo, hi = ta + SCAN_CLIP_S, tb
    for _ in range(10):
        gap = hi - lo
        if gap <= REFINE_GAP_S or gap < SCAN_CLIP_S:
            break
        moved = False
        for frac in (0.5, 0.33, 0.67):
            t = q(lo + (gap - SCAN_CLIP_S) * frac)
            r_ = picture_offset(f1, f2, t, SCAN_CLIP_S, st1, st2, a_off)
            if r_ is None:
                continue
            if abs(r_["P"] - p_a) <= PIC_STEP_TOL / 2:
                lo, moved = t + SCAN_CLIP_S, True
            elif abs(r_["P"] - p_b) <= PIC_STEP_TOL / 2:
                hi, moved = t, True
            else:
                continue
            break
        if not moved:
            break
    return lo, hi


def picture_only_step(f1, f2, st1, st2, a_off, ta, p_a, tb, p_b, debug_prefix=None):
    """Locate a change of the picture offset that the audio doesn't show, between the
    clips at ta and tb. Returns a step dict like locate_step's (before_ms == after_ms)."""
    lo, hi = refine_picture_gap(f1, f2, st1, st2, ta, p_a, tb, p_b, a_off)
    jn = find_junction(f1, f2, st1, st2, lo + st1, hi + st1, p_a, p_b, debug_prefix=debug_prefix)
    if "error" in jn:
        zs, ze, uncertain = lo, hi, True
    else:
        zs, ze, uncertain = jn["t1_last_a"] - st1, jn["t1_first_b"] - st1, False
    return {"at": (zs + ze) / 2, "zone_start": zs, "zone_end": ze, "gain": 0.0, "abs_gain": 0.0, "match": 1.0,
            "n_a": 0, "n_b": 0, "edge": False, "uncertain": uncertain, "quiet": False, "bracket": (zs, ze),
            "before_ms": ms(a_off), "after_ms": ms(a_off), "black": [], "blackpair": None,
            "junction": jn, "junction_hint": (p_a, p_b), "picture_only": True}


def split_runs_by_picture(f1, f2, st1, st2, runs, steps, pic, debug_dir=None, stem=""):
    """The audio can't see a splice that only touches the picture. Inside every audio segment, look
    for neighbouring clips whose picture offsets differ, find where, and split the segment there.
    Returns (runs, steps) with the new picture-only steps inserted (aligned as before)."""
    out_runs, out_steps, from_split = [], [], []
    n_new = 0
    for ri, run in enumerate(runs):
        if ri > 0:
            out_steps.append(steps[ri - 1])
        pics = [(t, pic[t]["P"]) for t, _ in run if t in pic]
        cuts = [(a, b) for a, b in zip(pics, pics[1:]) if abs(b[1] - a[1]) > PIC_STEP_TOL]
        if not cuts:
            out_runs.append(run)
            from_split.append(False)
            continue
        a_off = statistics.median(o for _, o in run)
        rest = list(run)
        for (ta, p_a), (tb, p_b) in cuts:
            n_new += 1
            stp = picture_only_step(f1, f2, st1, st2, a_off, ta, p_a, tb, p_b,
                                    debug_prefix=str(debug_dir / f"{stem}_picjunction{n_new}") if debug_dir else None)
            split = min(max(stp["zone_end"], ta + SCAN_CLIP_S), tb)
            out_runs.append([pt for pt in rest if pt[0] + SCAN_CLIP_S <= split])
            from_split.append(True)
            out_steps.append(stp)
            rest = [pt for pt in rest if pt[0] >= split]
        out_runs.append(rest)
        from_split.append(True)
    return out_runs, out_steps, from_split


def pics_between(pic, lo, hi):
    """Picture offsets (seconds) of the measured clips that lie fully inside lo..hi (file1 relative), by time."""
    return [pic[t]["P"] for t in sorted(pic) if t >= lo and t + SCAN_CLIP_S <= hi]


def probe_unmeasured_segments(f1, f2, st1, st2, runs, steps, pic, end_s):
    """Segments where none of the coarse clips gave a picture offset (a static scene happened to be
    sampled) get a few extra probes, nearest the steps first. Adds to `pic`; returns the indices of
    the segments that gained a measurement."""
    gained = set()
    for si, run in enumerate(runs):
        lo = steps[si - 1]["zone_end"] if si > 0 else 0.0
        hi = steps[si]["zone_start"] if si < len(steps) else end_s
        if pics_between(pic, lo, hi) or hi - lo < SCAN_CLIP_S + 1.0:
            continue
        a_off = statistics.median(o for _, o in run)
        span = hi - lo - SCAN_CLIP_S
        tried = 0
        for frac_t in (lo + 0.5, lo + span - 0.5, lo + span / 2, lo + span / 4, lo + 3 * span / 4):
            t = q(min(max(frac_t, lo), lo + span))
            if any(abs(t - x) < 2.0 for x in pic):
                continue
            tried += 1
            r_ = picture_offset(f1, f2, t, SCAN_CLIP_S, st1, st2, a_off)
            if r_:
                pic[t] = r_
                gained.add(si)
                if len(pics_between(pic, lo, hi)) >= 2:
                    break
            if tried >= 5:
                break
    return gained


def scan_pair(f1, f2, ratio, row, args):
    """Coarse scan + refinement for one DRIFT_DETECTED pair.
    Returns (steps, segments, note)."""
    dur1, dur2 = probe_duration(f1), probe_duration(f2)
    st1, st2 = probe_start_time(f1), probe_start_time(f2)
    idx1, idx2, lang = pick_audio_tracks(f1, f2, args.audio_lang)
    r = float(ratio)

    # Follow the offsets `check` already saw instead of searching +-10 s blindly.
    seen = [float(p.split(":")[1]) / 1000.0 for p in row["points"].split(";")
            if p.split(":")[3] == "1"]
    center = (min(seen) + max(seen)) / 2.0
    half = (max(seen) - min(seen)) / 2.0 + args.search_slack

    limit = min(dur1, r * dur2) - SCAN_CLIP_S - 1.0
    times, t = [], args.scan_start
    while t <= limit:
        times.append(q(t))
        t += args.scan_spacing
    if times and limit - times[-1] > 5.0:
        times.append(q(limit))          # catch changes in the final seconds
    print(f"    scanning {len(times)} clips on '{lang}' audio "
          f"(every {args.scan_spacing:g}s, search window {center * 1000:+.0f} ms +/- {half * 1000:.0f} ms)...")

    debug_dir = Path(args.debug_dir) if getattr(args, "debug_dir", None) else None
    stem = re.sub(r"[^\w.-]+", "_", f1.stem)
    pts, measured = [], []
    for t in times:
        ref = extract_audio(f1, t, SCAN_CLIP_S, idx1)
        if len(ref) < SCAN_CLIP_S * SR * 0.9:
            continue
        m = measure_point(ref, f2, idx2, t, SCAN_CLIP_S, half, ratio, dur2, st1, st2, center=center)
        good = bool(m and m["ncc"] >= args.min_confidence and m["prom"] >= MIN_PROMINENCE
                    and not m["edge"])
        if m:
            measured.append((t + st1, m["offset_s"] * 1000.0, m["ncc"], m["prom"], int(good)))
        if good:
            pts.append((t, m["offset_s"]))
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)
        np.savetxt(debug_dir / f"{stem}_coarse.csv", np.array(measured).reshape(-1, 5), delimiter=",",
                   comments="", fmt="%.4f", header="t_container_s,offset_ms,ncc,prominence,valid")
    if len(pts) < 2:
        return [], [], "locate: too few clips matched to map the episode"

    pic = {}
    if getattr(args, "video", False):
        if ratio != 1:
            print("    (picture check skipped: the files have different frame rates)")
        else:
            print(f"    measuring the picture offset in {len(pts)} clips...")
            pic_log = []
            for t, a_off in pts:
                why = {}
                r_ = picture_offset(f1, f2, t, SCAN_CLIP_S, st1, st2, a_off, why=why)
                if r_:
                    pic[t] = r_
                pic_log.append((t + st1, r_["P"] * 1000.0 if r_ else float("nan"),
                                r_["ncc"] if r_ else (why.get("best") or float("nan")), int(bool(r_)), why.get("reason", "")))
            if debug_dir:
                with open(debug_dir / f"{stem}_picture_clips.csv", "w") as fh:
                    fh.write("t_container_s,picture_offset_ms,match,valid,reason\n")
                    for tt, pp, nn, vv, rr in pic_log:
                        fh.write(f"{tt:.3f},{pp:.2f},{nn:.3f},{vv},\"{rr}\"\n")

    # split into runs of consecutive clips with (nearly) the same offset
    runs = [[pts[0]]]
    for pt in pts[1:]:
        if abs(pt[1] - runs[-1][-1][1]) * 1000.0 > args.step_ms:
            runs.append([pt])
        else:
            runs[-1].append(pt)

    steps = []
    for n_step, (prev, nxt) in enumerate(zip(runs, runs[1:]), 1):
        off_a = statistics.median(o for _, o in prev[-3:])
        off_b = statistics.median(o for _, o in nxt[:3])
        res = locate_step(f1, f2, idx1, idx2, ratio, st1, st2, dur2,
                          prev[-1][0], nxt[0][0], off_a, off_b,
                          debug_prefix=str(debug_dir / f"{stem}_step{n_step}") if debug_dir else None)
        res.update(before_ms=ms(off_a), after_ms=ms(off_b))
        # Is the picture black where the change is? Then compare how long it stays
        # black in each file: a splice there makes the durations differ by the step.
        bm = match_black(f1, f2, st1, st2, res["zone_start"] + st1, res["zone_end"] + st1,
                         off_a, off_b, with_file2=(ratio == 1))
        res["black"] = [(a - st1, b - st1) for a, b in bm["c1"]]
        res["blackpair"] = bm["pair"]
        # A picture step that black-screen lengths don't explain: pin the junction frame.
        # Hints come from every clip of the neighbouring segments (not just the last few,
        # which may be static and unmeasurable).
        res["junction"] = None
        if getattr(args, "video", False) and ratio == 1:
            pin_step_junction(f1, f2, st1, st2, res,
                              [pic[t]["P"] for t, _ in prev if t in pic],
                              [pic[t]["P"] for t, _ in nxt if t in pic],
                              debug_prefix=str(debug_dir / f"{stem}_junction{n_step}") if debug_dir else None)
        steps.append(res)

    # A single clip whose offset differs from both neighbours, with neither
    # surrounding step convincing, is a bad match, not a real short segment.
    dropped = []
    i = 1
    while i < len(runs) - 1:
        a_off = statistics.median(o for _, o in runs[i - 1])
        c_off = statistics.median(o for _, o in runs[i + 1])
        if (len(runs[i]) == 1 and abs(a_off - c_off) * 1000.0 <= 1.5 * args.step_ms
                and steps[i - 1]["uncertain"] and steps[i]["uncertain"]):
            dropped.append(runs[i][0][0])
            runs[i - 1:i + 2] = [runs[i - 1] + runs[i + 1]]
            del steps[i - 1:i + 1]
        else:
            i += 1

    do_pic = bool(getattr(args, "video", False)) and ratio == 1
    from_split = [False] * len(runs)
    if pic:
        # picture-only splices: a change of the picture offset inside an audio segment
        runs, steps, from_split = split_runs_by_picture(f1, f2, st1, st2, runs, steps, pic, debug_dir, stem)
    probed = set()
    if do_pic:
        probed = probe_unmeasured_segments(f1, f2, st1, st2, runs, steps, pic, min(dur1, dur2) - 1.0)
        for k, stp in enumerate(steps):        # an audio step next to a changed segment gets fresh hints
            if stp.get("picture_only") or not (from_split[k] or from_split[k + 1] or k in probed or k + 1 in probed):
                continue
            pin_step_junction(f1, f2, st1, st2, stp,
                              pics_between(pic, steps[k - 1]["zone_end"] if k > 0 else -1e9, stp["zone_start"]),
                              pics_between(pic, stp["zone_end"], steps[k + 1]["zone_start"] if k + 1 < len(steps) else 1e9),
                              debug_prefix=str(debug_dir / f"{stem}_junction{k + 1}b") if debug_dir else None)

    segments = []
    for si, run in enumerate(runs):
        seg = {"offset_ms": ms(statistics.median(o for _, o in run)), "clips": len(run)}
        # picture offsets only from clips that lie fully inside this segment, so a clip
        # straddling a splice can't masquerade as a change inside it
        lo = steps[si - 1]["zone_end"] if si > 0 else -1e9
        hi = steps[si]["zone_start"] if si < len(steps) else 1e9
        pv = pics_between(pic, lo, hi)
        if not pv:
            pv = [pic[t]["P"] for t, _ in run if t in pic]
        if pv:
            seg["picture_ms"] = ms(statistics.median(pv))
            seg["picture_clips"] = len(pv)
            seg["picture_spread_ms"] = ms(max(pv) - min(pv))
            seg["av_ms"] = round(seg["offset_ms"] - seg["picture_ms"], 1)
        segments.append(seg)
    note = ""
    if dropped:
        note = ("ignored single-clip mismatch at " + ", ".join(fmt_t(t + st1) for t in dropped)
                + " (not supported by the surrounding audio)")
    if not steps:
        slope = float(np.polyfit([t for t, _ in pts], [o for _, o in pts], 1)[0]) * 1000.0 * 60.0
        note = ((note + "; " if note else "") +
                f"no step found at {args.scan_spacing:g}s spacing; offset drifts gradually "
                f"(about {slope:+.1f} ms/min)")
    return steps, segments, note


def cmd_locate(args):
    require_tools("ffmpeg", "ffprobe", "mkvmerge")
    with open(args.report, newline="") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    if not rows or "points" not in fields:
        sys.exit(f"{args.report} doesn't look like a `check` report.")
    for extra in ("note", "steps"):
        if extra not in fields:
            fields.append(extra)

    only = [x.strip().lower() for x in args.only.split(",")] if args.only else []

    def wanted(row):
        if row["status"] != "DRIFT_DETECTED":
            return False
        names = (Path(row["file1"]).name + " " + Path(row["file2"]).name).lower()
        return not only or any(x in names for x in only)

    todo = sum(1 for r in rows if wanted(r))
    print(f"Locating steps in {todo} of {len(rows)} pair(s) (DRIFT_DETECTED only)...\n")

    done = 0
    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            if wanted(row):
                f1, f2 = Path(row["file1"]), Path(row["file2"])
                print(f"  {c(f1.name, 'cyan')}  <->  {c(f2.name, 'cyan')}")
                try:
                    ratio = Fraction(row.get("ratio") or "1")
                    st1 = probe_start_time(f1)
                    steps, segments, note = scan_pair(f1, f2, ratio, row, args)
                except Exception as e:
                    print(f"    {c('ERROR', 'red')}: {e}", file=sys.stderr)
                    row["note"] = f"locate failed: {e}"
                else:
                    lines = []
                    for stp in steps:
                        d = stp["after_ms"] - stp["before_ms"]
                        at, z0, z1 = stp["at"] + st1, stp["zone_start"] + st1, stp["zone_end"] + st1
                        if stp.get("picture_only"):
                            p_a, p_b = stp["junction_hint"]
                            where = (f"step somewhere in {fmt_t(z0)}-{fmt_t(z1)}" if stp["uncertain"]
                                     else f"step at {fmt_t(at, 2)}")
                            lines.append(f"picture-only {where}: the audio offset stays {stp['before_ms']:+.1f} ms but the "
                                         f"picture offset goes {p_a * 1000:+.1f} -> {p_b * 1000:+.1f} ms")
                            continue
                        if stp["uncertain"]:
                            kind = "silent" if stp["quiet"] else "non-matching"
                            blk = ""
                            if stp["black"]:
                                blk = "; black screen in file1 " + ", ".join(
                                    f"{fmt_t(x + st1)}-{fmt_t(y + st1)}" for x, y in stp["black"])
                            where = (f"step somewhere in {fmt_t(z0)}-{fmt_t(z1)} "
                                     f"({z1 - z0:.1f}s of {kind} audio{blk})")
                        else:
                            where = f"step at {fmt_t(at, 2)} (zone {fmt_t(z0, 2)}-{fmt_t(z1, 2)})"
                        more = (f"file1 has ~{abs(d):.0f} ms that file2 lacks" if d < 0
                                else f"file2 has ~{d:.0f} ms that file1 lacks")
                        why = ""
                        if stp["uncertain"]:
                            bits = []
                            if stp["abs_gain"] < 100:
                                bits.append("barely better than no step")
                            if stp["match"] < 0.25:
                                bits.append("one side matches poorly")
                            if stp["edge"]:
                                bits.append("not localised")
                            hint = ("; the audio can't say where inside it" if stp["quiet"]
                                    else "; position not determined by the audio")
                            why = c(f"  UNCERTAIN ({', '.join(bits)}{hint})" if bits
                                    else f"  UNCERTAIN ({hint[2:]})", "yellow")
                        lines.append(f"{where}: {stp['before_ms']:+.1f} -> {stp['after_ms']:+.1f} ms; "
                                     f"{more}{why}")
                        bp = stp["blackpair"]
                        if bp:
                            l1, l2 = bp["b1"][1] - bp["b1"][0], bp["b2"][1] - bp["b2"][0]
                            ok_ = bp["err"] <= 0.06
                            verdict = c("explains the step", "green") if ok_ else c("does not explain it", "yellow")
                            if bp.get("loose"):
                                verdict += c(" (file2's stretch only shows at a looser black threshold)", "yellow")
                            lines.append(
                                f"  black screen: file1 {fmt_t(bp['b1'][0])}-{fmt_t(bp['b1'][1])} "
                                f"({l1:.2f}s), file2 {fmt_t(bp['b2'][0])}-{fmt_t(bp['b2'][1])} ({l2:.2f}s): "
                                f"file1 longer by {(l1 - l2) * 1000:+.0f} ms vs the step's {-d:+.0f} ms -> {verdict}")
                            lines.append(
                                f"  picture offset across it: {bp['off_start'] * 1000:+.0f} -> "
                                f"{bp['off_end'] * 1000:+.0f} ms; audio offset: "
                                f"{stp['before_ms']:+.0f} -> {stp['after_ms']:+.0f} ms")
                            if (not ok_ and abs(bp["off_start"] * 1000 - stp["before_ms"]) <= 60
                                    and abs(bp["off_end"] * 1000 - stp["before_ms"]) <= 60):
                                lines.append("  the picture offset does not change across it, so this step is "
                                             "audio-only or happens after the black screen")
                    for stp in steps:
                        jn = stp.get("junction")
                        if jn and "error" in jn:
                            lines.append(f"picture junction near {fmt_t(stp['at'] + st1, 2)}: NOT pinned - {jn['error']}")
                        elif jn:
                            dpic = jn["p_b"] - jn["p_a"]
                            lines.append(
                                f"picture junction (frame-exact) near {fmt_t(stp['at'] + st1, 2)}: between "
                                f"{fmt_t(jn['t1_last_a'], 2)} and {fmt_t(jn['t1_first_b'], 2)} in file1 "
                                f"({jn['span_frames']} frame{'s' if jn['span_frames'] != 1 else ''} apart); picture offset "
                                f"{jn['p_a'] * 1000:+.0f} -> {jn['p_b'] * 1000:+.0f} ms: file2 "
                                f"{'lacks' if dpic < 0 else 'has'} {abs(dpic) * 1000 / FRAME_MS:.1f} frames here")
                    timeline = timeline_text(steps, segments, st1)
                    print(f"    {c('timeline', 'bold')}: {timeline}")
                    for ln in av_report(steps, segments, st1):
                        print(f"    {ln}")
                    for ln in lines:
                        print(f"      {ln}")
                    if note:
                        print(f"    {c(note, 'dim')}")
                    row["steps"] = json.dumps({
                        "timeline": "file1 container seconds", "ratio": str(ratio),
                        "segments": segments,
                        "steps": [{"at_s": round(stp["at"] + st1, 3),
                                   "from_s": round(stp["zone_start"] + st1, 3),
                                   "to_s": round(stp["zone_end"] + st1, 3),
                                   "before_ms": stp["before_ms"], "after_ms": stp["after_ms"],
                                   "uncertain": stp["uncertain"], "silent": stp["quiet"],
                                   "picture_only": bool(stp.get("picture_only")),
                                   "black_s": [[round(x + st1, 3), round(y + st1, 3)]
                                               for x, y in stp["black"]],
                                   "black_pair": ({"file1_s": [round(v, 3) for v in stp["blackpair"]["b1"]],
                                                   "file2_s": [round(v, 3) for v in stp["blackpair"]["b2"]],
                                                   "err_ms": round(stp["blackpair"]["err"] * 1000),
                                                   "loose": bool(stp["blackpair"].get("loose"))}
                                                  if stp["blackpair"] else None),
                                   "match": round(stp["match"], 2),
                                   "evidence": round(stp["abs_gain"]),
                                   "junction": ({k: (round(v, 4) if isinstance(v, float) else v)
                                                 for k, v in stp["junction"].items()}
                                                if stp.get("junction") else None)}
                                  for stp in steps]})
                    row["note"] = timeline + (f" | {note}" if note else "")
                done += 1
            writer.writerow(row)
            fh.flush()

    print(f"\nReport written to {args.out} ({done} episode(s) analysed)")


# --------------------------------------------------------------------------
# `retime-video` subcommand: make black-screen stretches match between the files
# --------------------------------------------------------------------------

def video_pts(path: Path):
    """Presentation timestamps (container seconds) of every frame of the first video track."""
    out = run_cmd(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                   "packet=pts_time", "-of", "csv=p=0", str(path)]).decode().split()
    return sorted(float(x.strip(",")) for x in out if x.strip(",") not in ("", "N/A"))


def retime_pts(pts, edits):
    """edits: sorted, non-overlapping (start, end, new_length) black stretches in
    container seconds. Frames inside a stretch are spread uniformly over the new
    length; frames after it move by the change in length."""
    res = []
    for t in pts:
        shift, new_t = 0.0, None
        for b0, b1, new_len in edits:
            if t >= b1:
                shift += new_len - (b1 - b0)
            elif t >= b0:
                new_t = b0 + shift + (t - b0) * (new_len / (b1 - b0))
                break
            else:
                break
        res.append(new_t if new_t is not None else t + shift)
    return res


def plan_retime(f1, f2, data, args):
    """Work out the timestamp edits that make file2's picture timeline match file1's.
    Returns (edits, ignored, problems, unchanged). An edit is a dict:
      {"kind": "black", ...}     lengthen/shorten a black stretch of file2 to file1's length
      {"kind": "junction", ...}  at a frame-exact picture junction, hold the last frame before it
                                 (file2 lacks frames) or squeeze the stretch before it (file2 has extra)
    --align picture (default) uses only picture measurements, so it doesn't depend on either file's
    audio being in sync with its picture; --align audio sizes black-screen edits from the audio steps."""
    st1, st2 = probe_start_time(f1), probe_start_time(f2)
    dur1 = probe_duration(f1)
    segs = data.get("segments", [])
    edits, ignored, problems, unchanged = [], [], [], []
    if args.align == "picture":
        for si, seg in enumerate(segs):
            if seg.get("picture_spread_ms", 0) > 60.0:
                start = data["steps"][si - 1]["at_s"] if si > 0 else 0.0
                problems.append(f"the picture offset varies by {seg['picture_spread_ms']:.0f} ms inside the segment "
                                f"starting at {fmt_t(start)}: a picture-only splice that has not been located")
    bms = [match_black(f1, f2, st1, st2, x["from_s"], x["to_s"], x["before_ms"] / 1000.0, x["after_ms"] / 1000.0)
           for x in data["steps"]]

    def add_black(edit):
        """Add a black-stretch edit; if an earlier step already edits the same stretch, merge the two
        (the total picture change across both steps decides the new length) instead of overlapping."""
        for e in edits:
            if e["kind"] == "black" and e["b0"] < edit["b1"] and edit["b0"] < e["b1"]:
                if e.get("pic_a") is not None and edit.get("pic_b") is not None:
                    e["new_len"] = (e["b1"] - e["b0"]) - (edit["pic_b"] - e["pic_a"])
                    e["merged"] = e.get("merged", 1) + 1
                return
        edits.append(edit)

    for i, stp in enumerate(data["steps"]):
        off_a, off_b = stp["before_ms"] / 1000.0, stp["after_ms"] / 1000.0
        delta = off_b - off_a
        when = fmt_t(stp["at_s"])
        bm = bms[i]
        pair = bm["pair"]
        jn_raw = stp.get("junction")
        jn = jn_raw if (jn_raw and "error" not in jn_raw) else None
        jn_reason = jn_raw.get("error") if jn_raw and "error" in jn_raw else None
        pic_a = segs[i].get("picture_ms") if i < len(segs) else None
        pic_b = segs[i + 1].get("picture_ms") if i + 1 < len(segs) else None
        d_seg = (pic_b - pic_a) / 1000.0 if pic_a is not None and pic_b is not None else None
        change = (jn["p_b"] - jn["p_a"]) if jn else d_seg          # picture offset change across the step
        # Nearest segments with a picture offset on each side: when the segments next to the step
        # can't be measured (static scenes) but the offset is the same before and after the whole
        # stretch, none of the steps inside it changes the picture (unless two edits cancel out).
        a_idx = next((j for j in range(min(i, len(segs) - 1), -1, -1) if segs[j].get("picture_ms") is not None), None)
        b_idx = next((j for j in range(i + 1, len(segs)) if segs[j].get("picture_ms") is not None), None)
        if (args.align == "picture" and jn is None and a_idx is not None and b_idx is not None
                and (a_idx < i or b_idx > i + 1)):
            net = (segs[b_idx]["picture_ms"] - segs[a_idx]["picture_ms"]) / 1000.0
            d_black_ = (pair["off_end"] - pair["off_start"]) if pair else 0.0
            if abs(net) <= 0.02 and abs(d_black_) <= 0.025:
                unchanged.append(dict(stp, inferred=(segs[a_idx]["picture_ms"], segs[b_idx]["picture_ms"])))
                continue
            # The black stretches of the steps in the group add up to something the picture offsets
            # before and after the whole group contradict (a black boundary one frame off, say):
            # trust the measured picture offsets.
            grp = [(j, bms[j]["pair"]["off_end"] - bms[j]["pair"]["off_start"]) for j in range(a_idx, b_idx)
                   if bms[j]["pair"] and abs(bms[j]["pair"]["off_end"] - bms[j]["pair"]["off_start"]) > 0.025]
            if abs(net) <= 0.02 and abs(sum(d for _, d in grp)) > 0.025 and len(grp) == 1 and grp[0][0] == i:
                unchanged.append(dict(stp, inferred=(segs[a_idx]["picture_ms"], segs[b_idx]["picture_ms"]),
                                      conflict=d_black_))
                continue
            # The net change over the group is accounted for by black edits of other steps in it:
            # this step (no black change of its own) leaves the picture as it is.
            if grp and abs(net) > 0.02 and abs(sum(d for _, d in grp) - net) <= 0.025 and abs(d_black_) <= 0.025:
                unchanged.append(dict(stp, inferred=(segs[a_idx]["picture_ms"], segs[b_idx]["picture_ms"])))
                continue

        if args.align == "picture" and (pair or change is not None):
            d_black = (pair["off_end"] - pair["off_start"]) if pair else 0.0
            if pair and abs(d_black) > 0.025 and change is not None and abs(change) <= 0.02:
                unchanged.append(dict(stp, conflict=d_black, inferred=(pic_a, pic_b)) if pic_a is not None and pic_b is not None
                                 else stp)         # the picture doesn't change; the black stretches differ by a frame
            elif pair and abs(d_black) > 0.025 and (change is None or abs(change - d_black) <= 0.06):
                b0, b1 = pair["b2"]
                new_len = pair["b1"][1] - pair["b1"][0]            # same length as file1's
                if change is not None:
                    # The measured picture offsets say exactly how far the picture after the black has to move;
                    # file1's black length can be a frame off that (a fade-in frame counted as black in one
                    # file only), which would leave the rest of the episode a frame out.
                    new_len = (b1 - b0) - change
                if pair.get("loose") and change is None:
                    # A loose threshold also counts dark fade-in frames as black, so the two stretches
                    # may differ in length for that reason alone; with no picture offset on either
                    # side there is nothing to check the pair against.
                    problems.append(f"step near {when}: file2's black stretch ({b1 - b0:.2f}s) only matches at a "
                                    f"loose black threshold and the picture offset couldn't be measured on either "
                                    f"side of the step, so the pairing with file1's ({new_len:.2f}s) can't be checked")
                elif new_len < 0.04 or (b1 - b0 >= 0.5 and not 0.3 <= new_len / (b1 - b0) <= 3.0):
                    problems.append(f"step near {when}: file2's black stretch ({b1 - b0:.2f}s) can't be "
                                    f"turned into file1's ({new_len:.2f}s) without too large a change")
                else:
                    add_black({"kind": "black", "b0": b0, "b1": b1, "new_len": new_len, "stp": stp, "pair": pair,
                               "pic_a": (jn["p_a"] if jn else (pic_a / 1000.0 if pic_a is not None else None)),
                               "pic_b": (jn["p_b"] if jn else (pic_b / 1000.0 if pic_b is not None else None)),
                               "by_picture": change is not None})
            elif jn and abs(jn["p_b"] - jn["p_a"]) > 0.02:
                width = stp["to_s"] - stp["from_s"]
                if jn["span_frames"] > 48:
                    if (args.junction_fallback == "audio" and not stp.get("uncertain")
                            and width <= args.junction_fallback_max):
                        t1 = (stp["from_s"] + stp["to_s"]) / 2
                        edits.append({"kind": "junction", "t2": t1 + jn["p_a"], "shift": jn["p_a"] - jn["p_b"],
                                      "stp": stp, "jn": None, "fallback": width})
                    else:
                        problems.append(f"step near {when}: the junction frame can't be pinned down "
                                        f"({jn['span_frames']} frames of ambiguous picture)")
                else:
                    edits.append({"kind": "junction", "t2": jn["t2_first_b"], "shift": jn["p_a"] - jn["p_b"],
                                  "stp": stp, "jn": jn})
            elif change is not None and abs(change) > 0.06:
                width = stp["to_s"] - stp["from_s"]
                if (args.junction_fallback == "audio" and pic_a is not None and pic_b is not None
                        and not stp.get("uncertain") and width <= args.junction_fallback_max):
                    # The picture can't pin it (e.g. nearly static scene), but the audio located the
                    # step to within a short stretch: hold/squeeze there. Error is bounded by that stretch.
                    t1 = (stp["from_s"] + stp["to_s"]) / 2
                    edits.append({"kind": "junction", "t2": t1 + pic_a / 1000.0, "shift": (pic_a - pic_b) / 1000.0,
                                  "stp": stp, "jn": None, "fallback": width})
                else:
                    problems.append(f"step near {when}: the picture offset changes by {change * 1000:+.0f} ms but it "
                                    f"is neither in a black screen nor pinned to a frame"
                                    + (f" ({jn_reason})" if jn_reason else " (run `locate --video`)"))
            else:
                unchanged.append(stp)
            continue

        # audio-based sizing (also the fallback when there is no picture information)
        if pair and pair["err"] <= args.black_tol_ms / 1000.0:
            b0, b1 = pair["b2"]
            new_len = (b1 - b0) - delta               # file2 must gain what file1 has extra
            if new_len < 0.1 or new_len / (b1 - b0) < 0.3:
                problems.append(f"step near {when}: file2's black stretch ({b1 - b0:.2f}s) is too short "
                                f"to absorb {delta * 1000:+.0f} ms")
            else:
                add_black({"kind": "black", "b0": b0, "b1": b1, "new_len": new_len, "stp": stp, "pair": pair})
        elif abs(delta) * 1000.0 <= args.small_step_ms:
            ignored.append(stp)
        elif args.tail_seconds and stp["at_s"] >= dur1 - args.tail_seconds:
            ignored.append(stp)          # accepted by the user: unexplained, but in the final seconds
        elif not bm["c1"]:
            problems.append(f"step near {when} ({delta * 1000:+.0f} ms): the picture is not black there "
                            f"(file1 shows no black screen), so it can't be fixed by retiming black frames; "
                            f"run `locate --video` to pin it to a frame")
        elif not pair:
            problems.append(f"step near {when} ({delta * 1000:+.0f} ms): black screen in file1 but no "
                            f"matching black stretch found in file2")
        else:
            diff = ((pair["b1"][1] - pair["b1"][0]) - (pair["b2"][1] - pair["b2"][0])) * 1000
            problems.append(f"step near {when}: the black stretches differ by {diff:+.0f} ms but the step is "
                            f"{-delta * 1000:+.0f} ms (picture offset across the black screen: "
                            f"{pair['off_start'] * 1000:+.0f} -> {pair['off_end'] * 1000:+.0f} ms, audio offset: "
                            f"{off_a * 1000:+.0f} -> {off_b * 1000:+.0f} ms). Run `locate --video` to compare "
                            f"picture and audio, or use --tail-seconds if this is in the last seconds")
    return edits, ignored, problems, unchanged


def intervals_from_edits(edits, pts, squeeze_s):
    """Turn planned edits into (start, end, new_length) intervals on real frame timestamps."""
    import bisect

    def snap(t):
        i = bisect.bisect_left(pts, t)
        cands = [j for j in (i - 1, i) if 0 <= j < len(pts)]
        return min(cands, key=lambda j: abs(pts[j] - t))

    out = []
    for e in edits:
        if e["kind"] == "black":
            b0, b1 = pts[snap(e["b0"])], pts[snap(e["b1"])]
            out.append((b0, b1, e["new_len"]))
        else:
            jb = snap(e["t2"])
            t_j = pts[jb]
            if e["shift"] > 0:                                   # hold the last frame before the junction
                b0 = pts[jb - 1]
            else:                                                # squeeze the stretch before it
                b0 = pts[bisect.bisect_left(pts, t_j - squeeze_s)]
            out.append((b0, t_j, (t_j - b0) + e["shift"]))
    out.sort()
    for x, y in zip(out, out[1:]):
        if x[1] > y[0]:
            raise ValueError("two edits overlap")
    return out


def cmd_retime(args):
    require_tools("ffmpeg", "ffprobe", "mkvmerge")
    with open(args.report, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "steps" not in rows[0]:
        sys.exit(f"{args.report} has no step locations - run `locate` first.")
    only = [x.strip().lower() for x in args.only.split(",")] if args.only else []
    args.out_dir.mkdir(parents=True, exist_ok=True)

    out_rows, done, skipped = [], 0, 0
    for row in rows:
        if row["status"] != "DRIFT_DETECTED" or not row.get("steps"):
            continue
        f1, f2 = Path(row["file1"]), Path(row["file2"])
        if only and not any(x in (f1.name + " " + f2.name).lower() for x in only):
            continue
        print(f"  {c(f1.name, 'cyan')}  ->  retime {c(f2.name, 'cyan')}")
        data = json.loads(row["steps"])
        if Fraction(data.get("ratio", "1")) != 1:
            print("    skipped: the files have different frame rates; audio can't be copied across "
                  "losslessly anyway")
            skipped += 1
            continue
        try:
            edits, ignored, problems, unchanged = plan_retime(f1, f2, data, args)
        except Exception as e:
            print(f"    {c('ERROR', 'red')}: {e}", file=sys.stderr)
            skipped += 1
            continue
        if problems:
            for pr in problems:
                print(f"    {c('cannot fix', 'red')}: {pr}")
            skipped += 1
            continue
        for e in edits:
            if e["kind"] == "black":
                pair = e["pair"]
                d1 = pair["b1"][1] - pair["b1"][0]
                print(f"    black {fmt_t(pair['b1'][0])}-{fmt_t(pair['b1'][1])} in file1 ({d1:.2f}s) <-> "
                      f"{fmt_t(e['b0'])}-{fmt_t(e['b1'])} in file2 ({e['b1'] - e['b0']:.2f}s): set file2's to "
                      f"{e['new_len']:.2f}s ({(e['new_len'] - (e['b1'] - e['b0'])) * 1000:+.0f} ms)"
                      + (" [sized from the measured picture offsets]" if e.get("by_picture") and abs((e['new_len'] - (e['b1'] - e['b0'])) - (d1 - (e['b1'] - e['b0']))) > 0.02 else "")
                      + (f" [{e['merged']} steps on this stretch merged]" if e.get("merged") else ""))
            else:
                what = (f"hold the last frame before it for {e['shift'] * 1000:+.0f} ms" if e["shift"] > 0 else
                        f"squeeze the {args.squeeze_seconds:g}s before it by {-e['shift'] * 1000:.0f} ms "
                        f"(they play {-e['shift'] / args.squeeze_seconds * 100:.1f}% faster)")
                warn = ""
                if e.get("fallback") is not None:
                    warn = c(f"  [placed from the audio zone, +/-{e['fallback'] / 2:.2f}s: the picture alone "
                             f"couldn't pin it]", "yellow")
                print(f"    picture junction at {fmt_t(e['t2'], 2)} in file2: {what}{warn}")
        residual = 0.0
        run = 0.0
        for stp in sorted(ignored, key=lambda x: x["at_s"]):
            run += stp["after_ms"] - stp["before_ms"]
            residual = max(residual, abs(run))
            print(f"    ignoring step near {fmt_t(stp['at_s'])} "
                  f"({stp['after_ms'] - stp['before_ms']:+.0f} ms, not fixable by retiming black screen)")
        for stp in unchanged:
            if stp.get("conflict") is not None and stp.get("inferred"):
                ia, ib = stp["inferred"]
                print(f"    step near {fmt_t(stp['at_s'])}: the black stretches differ by {stp['conflict'] * 1000:+.0f} ms "
                      f"but the picture offset is {ia:+.1f} ms before and {ib:+.1f} ms after, so no video edit "
                      f"(the black boundary is probably a frame off)")
            elif stp.get("inferred"):
                ia, ib = stp["inferred"]
                print(f"    step near {fmt_t(stp['at_s'])} ({stp['after_ms'] - stp['before_ms']:+.0f} ms in the audio): "
                      f"the picture couldn't be measured next to it, but its offset is {ia:+.1f} ms before and "
                      f"{ib:+.1f} ms after the stretch, so no video edit is assumed "
                      f"(two equal and opposite picture edits inside it would not be noticed)")
            else:
                print(f"    step near {fmt_t(stp['at_s'])} ({stp['after_ms'] - stp['before_ms']:+.0f} ms in the audio): "
                      f"the picture offset does not change there, so no video edit is needed")
        seg0 = data["segments"][0]
        first_pic = next((k for k, sg in enumerate(data["segments"]) if sg.get("picture_ms") is not None), None)
        if args.align == "picture" and seg0.get("picture_ms") is not None:
            a0 = seg0["picture_ms"]
            how = "pictures aligned"
        elif args.align == "picture" and first_pic is not None:
            # The first segment's picture couldn't be measured. The retimed video has the same offset
            # everywhere, which is that segment's measured offset plus what the edits before it moved.
            moved = 0.0
            for e in edits:
                if any(x is e["stp"] for x in data["steps"][:first_pic]):
                    moved += (e["new_len"] - (e["b1"] - e["b0"])) if e["kind"] == "black" else e["shift"]
            a0 = data["segments"][first_pic]["picture_ms"] + moved * 1000.0
            how = f"pictures aligned, first segment's picture offset inferred from the segment at step {first_pic}"
        else:
            a0 = seg0["offset_ms"]
            how = "audio-based"
        n_black = sum(1 for e in edits if e["kind"] == "black")
        note = (f"video retimed: {n_black} black stretch(es), {len(edits) - n_black} frame junction(s) "
                f"({how})")
        if residual:
            note += f"; up to {residual:.0f} ms residual from ignored small steps"
        out_path = args.out_dir / f2.name
        pts = video_pts(f2)
        if args.dry_run:
            print(f"    {c('[DRY RUN]', 'dim')} would write {out_path} (copy audio/subs with offset {a0:+.1f} ms)")
            continue
        try:
            new = retime_pts(pts, intervals_from_edits(edits, pts, args.squeeze_seconds))
        except ValueError as e:
            print(f"    {c('ERROR', 'red')}: {e}; not written", file=sys.stderr)
            skipped += 1
            continue
        if any(b <= a for a, b in zip(new, new[1:])):
            print(f"    {c('ERROR', 'red')}: retimed timestamps are not increasing; not written", file=sys.stderr)
            skipped += 1
            continue
        tmp, ts_file = out_path.with_name(out_path.name + ".part"), out_path.with_name(out_path.name + ".ts.txt")
        ts_file.write_text("# timestamp format v2\n" + "\n".join(f"{t * 1000:.6f}" for t in new) + "\n")
        vid = identify_tracks(f2, "video")[0]["id"]
        cmd = ["mkvmerge", "-o", str(tmp), "--no-audio", "--no-subtitles", "--no-chapters",
               "--no-attachments", "--timestamps", f"{vid}:{ts_file}", str(f2)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        ts_file.unlink(missing_ok=True)
        if res.returncode not in (0, 1):
            tmp.unlink(missing_ok=True)
            print(f"    {c('ERROR', 'red')} muxing: {res.stderr or res.stdout}", file=sys.stderr)
            skipped += 1
            continue
        os.replace(tmp, out_path)
        print(f"    {c('Written', 'green')}: {out_path}  (video only; copy audio/subs from file1 with offset {a0:+.1f} ms)")
        new_row = {k: "" for k in REPORT_FIELDS}
        new_row.update(file1=row["file1"], file2=str(out_path.resolve()), status="FIXED_OFFSET",
                       ratio="1", offset_ms=a0, spread_ms=0.0, note=note)
        out_rows.append(new_row)
        done += 1

    if not args.dry_run:
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
            w.writeheader()
            w.writerows(out_rows)
    print(f"\nDone. {done} retimed, {skipped} skipped.")
    if out_rows:
        print(f"Next: python3 mkv_sync_tool.py mux-both -s <folder with the audio/subs you want> "
              f"-d {args.out_dir} --report {args.out} --output-dir muxed")


# --------------------------------------------------------------------------
# mux subcommands
# --------------------------------------------------------------------------

TRACK_TYPE_FLAG = {"subtitles": "--subtitle-tracks", "audio": "--audio-tracks"}
_REGEN_UIDS = {"supported": True}   # flipped off if mkvmerge rejects --regenerate-track-uids


def mux_pair(source: Path, target: Path, output: Path, delay_ms: float, stretch_by: Fraction,
             track_types, track_ids_by_type=None, track_name_by_type=None,
             dry_run=False, exclude_dest_tracks=False):
    """Copy track(s) of the given types from source into target.
    Source timestamps are mapped as: new = old * stretch_by + delay_ms
    (mkvmerge's --sync semantics). `stretch_by` must be 1 for audio.
    Returns True on success, False on error, None if there was nothing to copy."""
    track_ids_by_type = track_ids_by_type or {}
    track_name_by_type = track_name_by_type or {}

    flag_args, sync_ids = [], []
    selected_types = []      # types that actually have tracks to copy
    stretch_note = f", stretch {stretch_by}" if stretch_by != 1 else ""

    def describe(tracks):
        parts = []
        for t in tracks:
            props = t.get("properties", {})
            name = props.get("track_name", "")
            parts.append(f"{t['id']}: {props.get('language', 'und')}" + (f" '{name}'" if name else ""))
        return "; ".join(parts) if parts else "none"

    for track_type in track_types:
        all_tracks = identify_tracks(source, track_type)
        tracks = all_tracks

        # A filter the user asked for that matches nothing is an error: if we
        # skipped just that type, mkvmerge would fall back to copying EVERY
        # track of it (without these filters), which is never what was meant.
        wanted_ids = track_ids_by_type.get(track_type)
        if wanted_ids:
            tracks = [t for t in tracks if t["id"] in wanted_ids]
            if not tracks:
                print(f"    {c('ERROR', 'red')}: none of the requested {track_type} track IDs "
                      f"{wanted_ids} exist in {source.name} (available {track_type}: "
                      f"{describe(all_tracks)}). Nothing muxed for this file.", file=sys.stderr)
                return False
        name_match = track_name_by_type.get(track_type)
        if name_match:
            needle = name_match.lower()
            tracks = [t for t in tracks
                      if needle in t.get("properties", {}).get("track_name", "").lower()]
            if not tracks:
                print(f"    {c('ERROR', 'red')}: no {track_type} track in {source.name} has a name "
                      f"matching '{name_match}' (available {track_type}: {describe(all_tracks)}). "
                      f"Nothing muxed for this file.", file=sys.stderr)
                return False
        if not tracks:
            print(f"    No {track_type} tracks found in {source.name}.")
            continue

        selected_types.append(track_type)
        ids = [t["id"] for t in tracks]
        flag_args += [TRACK_TYPE_FLAG[track_type], ",".join(str(i) for i in ids)]
        sync_ids += ids

        for t in tracks:
            lang = t.get("properties", {}).get("language", "und")
            name = t.get("properties", {}).get("track_name", "")
            verb = c("Would copy", "dim") if dry_run else c("Copying", "cyan")
            label = f"{lang}, {name}" if name else lang
            print(f"    {verb} {track_type} track {t['id']} ({label}) "
                  f"with sync offset {delay_ms:+.1f}ms{stretch_note}")

    if not selected_types:
        print(f"    Nothing to copy from {source.name}, skipping.")
        return None      # not an error: counted as skipped by the caller

    # Track selection options only choose WHICH tracks of a type to take; they
    # don't exclude other types, so exclude everything we didn't select.
    for excluded_type in ("video", "audio", "subtitles"):
        if excluded_type not in selected_types:
            flag_args.append(f"--no-{excluded_type}")
    flag_args.append("--no-chapters")
    if "subtitles" not in selected_types:
        flag_args.append("--no-attachments")   # keep fonts only when copying subtitles

    target_flag_args = []
    if exclude_dest_tracks:
        # Only drop the target's tracks of types we are really replacing.
        for track_type in selected_types:
            target_flag_args.append(f"--no-{track_type}")
            print(f"    Dropping {target.name}'s own {track_type} track(s)")

    tmp_out = output.with_name(output.name + ".part")

    def build_cmd(out_path):
        # --regenerate-track-uids gives source's tracks fresh random UIDs so a
        # source with low-entropy UIDs (1, 2, 3...) can't collide with the
        # target's. Older mkvmerge versions don't have it (see below).
        cmd = ["mkvmerge", "-o", str(out_path), "--stop-after-video-ends"] + target_flag_args \
            + [str(target)] + flag_args
        if _REGEN_UIDS["supported"]:
            cmd.append("--regenerate-track-uids")
        for i in sync_ids:
            spec = f"{i}:{round(delay_ms)}"
            if stretch_by != 1:
                spec += f",{stretch_by.numerator}/{stretch_by.denominator}"
            cmd += ["--sync", spec]
        cmd += [str(source)]
        return cmd

    if dry_run:
        print(f"    {c('[DRY RUN]', 'dim')} would run: {' '.join(build_cmd(output))}")
        return True

    # Write to a .part file and rename on success, so a failed or interrupted
    # run never leaves a truncated file that looks like a finished episode (or
    # clobbers a good one from an earlier run).
    result = subprocess.run(build_cmd(tmp_out), capture_output=True, text=True)
    if (result.returncode not in (0, 1) and _REGEN_UIDS["supported"]
            and "--regenerate-track-uids" in (result.stdout + result.stderr)):
        # This mkvmerge predates the option and treated it as a filename.
        _REGEN_UIDS["supported"] = False
        print(f"    {c('Note', 'yellow')}: this mkvmerge has no --regenerate-track-uids; "
              f"retrying without it (UID collisions are unlikely but possible)")
        result = subprocess.run(build_cmd(tmp_out), capture_output=True, text=True)
    if result.returncode not in (0, 1):      # mkvmerge uses 1 for warnings
        tmp_out.unlink(missing_ok=True)
        print(f"    {c('ERROR', 'red')} muxing {output.name}:\n{result.stderr or result.stdout}",
              file=sys.stderr)
        return False
    os.replace(tmp_out, output)
    if result.returncode == 1:
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


def copy_unchanged(src: Path, dst: Path):
    """Copy a file into the output folder via a .part file (same reasoning as mux_pair)."""
    part = dst.with_name(dst.name + ".part")
    try:
        shutil.copy2(src, part)
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


def _parse_ids(text):
    return [int(x.strip()) for x in text.split(",")] if text else None


def cmd_mux_generic(args, track_types):
    require_tools("mkvmerge")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ids, names = {}, {}
    if len(track_types) == 1:
        t = track_types[0]
        if getattr(args, "track_ids", None):
            ids[t] = _parse_ids(args.track_ids)
        if getattr(args, "track_name", None):
            names[t] = args.track_name
    else:
        if getattr(args, "sub_track_ids", None):
            ids["subtitles"] = _parse_ids(args.sub_track_ids)
        if getattr(args, "audio_track_ids", None):
            ids["audio"] = _parse_ids(args.audio_track_ids)
        if getattr(args, "sub_track_name", None):
            names["subtitles"] = args.sub_track_name
        if getattr(args, "audio_track_name", None):
            names["audio"] = args.audio_track_name

    dry_run = args.dry_run
    if dry_run:
        print("[DRY RUN] no files will be written\n")

    source_dir, dest_dir = args.source.resolve(), args.dest.resolve()
    if args.output_dir.resolve() in (source_dir, dest_dir):
        sys.exit("--output-dir must be a different folder from --source and --dest.")
    with open(args.report, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if rows and "offset_ms" not in rows[0]:
        sys.exit(f"{args.report} is in an older report format - re-run `check` to regenerate it.")

    processed = skipped = failed = 0
    written = set()          # output names produced (or planned, in a dry run) by this run

    for row in rows:
        status = row["status"]
        f1, f2 = Path(row["file1"]), Path(row["file2"])

        if status == "ERROR":
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: sync check errored")
            skipped += 1
            continue

        if f1.parent == source_dir and f2.parent == dest_dir:
            source, target, source_is_file1 = f1, f2, True
        elif f2.parent == source_dir and f1.parent == dest_dir:
            source, target, source_is_file1 = f2, f1, False
        else:
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: doesn't match --source/--dest "
                  f"(found in {f1.parent} and {f2.parent})")
            skipped += 1
            continue

        if status in ("DRIFT_DETECTED", "LOW_CONFIDENCE") and not args.force:
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: {status_text(status)} "
                  f"(pass --force to mux anyway using the start offset only)")
            skipped += 1
            continue
        if row["offset_ms"] == "":
            print(f"{c('Skipping', 'yellow')} {f1.name} / {f2.name}: no usable offset in the report")
            skipped += 1
            continue

        off_ms = float(row["offset_ms"])
        pic = row.get("picture_ms", "")
        if args.align == "picture" and pic not in ("", None) and status in ("SYNCED", "FIXED_OFFSET"):
            p_ms = float(pic)
            if abs(p_ms - off_ms) <= 150.0:
                if abs(p_ms - off_ms) > 21.0:
                    print(f"{c('Note', 'yellow')}: {f1.name}: placing by picture ({p_ms:+.1f} ms) instead of "
                          f"audio ({off_ms:+.1f} ms)")
                off_ms = p_ms
            else:
                print(f"{c('Warning', 'yellow')}: {f1.name}: picture offset {p_ms:+.1f} ms and audio offset "
                      f"{off_ms:+.1f} ms disagree by more than 150 ms; using the audio offset")
        ratio = Fraction(row.get("ratio") or "1")
        # `check` measured: file2_time = (file1_time + offset) / ratio.
        if source_is_file1:      # map file1 timestamps onto file2's timeline
            stretch_by, delay_ms = Fraction(1) / ratio, off_ms / float(ratio)
        else:                    # map file2 timestamps onto file1's timeline
            stretch_by, delay_ms = ratio, -off_ms
        if args.no_sync:
            stretch_by, delay_ms = Fraction(1), 0.0

        types = list(track_types)
        if stretch_by != 1 and "audio" in types:
            types.remove("audio")
            print(f"{c('Note', 'yellow')}: {f1.name} / {f2.name} is stretched ({ratio}); audio can't be "
                  f"re-timed losslessly, so it is not copied.")
            if not types:
                skipped += 1
                continue

        output = args.output_dir / target.name
        if output.resolve() == target.resolve():
            print(f"{c('ERROR', 'red')}: output {output} would overwrite the destination file "
                  f"- choose a different --output-dir", file=sys.stderr)
            failed += 1
            continue

        stretch_txt = f", stretch {stretch_by}" if stretch_by != 1 else ""
        forced_txt = " - forced to 0 via --no-sync" if args.no_sync else ""
        print(f"{c(source.name, 'cyan')} -> {c(target.name, 'cyan')}  "
              f"(sync {delay_ms:+.1f}ms{stretch_txt}{forced_txt})")
        if not source.exists() or not target.exists():
            print(f"    {c('ERROR', 'red')}: missing file(s) on disk, skipping.", file=sys.stderr)
            failed += 1
            continue

        try:
            ok = mux_pair(source, target, output, delay_ms, stretch_by, types, ids, names,
                          dry_run, args.exclude_dest_tracks)
        except Exception as e:
            print(f"    {c('ERROR', 'red')}: {e}", file=sys.stderr)
            ok = False
        if ok:
            processed += 1
            written.add(target.name)
        elif ok is None:
            skipped += 1
        else:
            failed += 1

    # Make sure the output folder holds every episode: anything from --dest
    # that wasn't muxed above (skipped, failed, or never paired) is copied
    # through unchanged. An existing file in the output folder is never
    # overwritten by a pass-through copy.
    passthrough = already = 0
    for f in list_videos(dest_dir):
        if f.name in written:
            continue
        out = args.output_dir / f.name
        if out.exists():
            already += 1
            continue
        if dry_run:
            print(f"{c('[DRY RUN]', 'dim')} would copy unchanged: {f.name}")
            passthrough += 1
            continue
        try:
            copy_unchanged(f, out)
        except Exception as e:
            print(f"{c('ERROR', 'red')}: could not copy {f.name} to {args.output_dir}: {e}",
                  file=sys.stderr)
            failed += 1
            continue
        print(f"{c('Copied unchanged', 'yellow')}: {f.name}")
        passthrough += 1

    verb = "would be muxed" if dry_run else "muxed"
    print(f"\nDone. {c(str(processed) + ' ' + verb, 'green')}, "
          f"{c(str(skipped) + ' skipped', 'yellow' if skipped else 'dim')}, "
          f"{c(str(failed) + ' failed', 'red' if failed else 'dim')}; "
          f"{passthrough} {'would be ' if dry_run else ''}copied unchanged"
          f"{f', {already} already in output folder (left as is)' if already else ''}.")


def cmd_mux(args):
    cmd_mux_generic(args, ["subtitles"])


def cmd_mux_audio(args):
    cmd_mux_generic(args, ["audio"])


def cmd_mux_both(args):
    cmd_mux_generic(args, ["subtitles", "audio"])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_mux_args(p, what, both=False):
    p.add_argument("--report", default="sync_report.csv", type=Path,
                   help="CSV from the `check` subcommand (default: sync_report.csv)")
    p.add_argument("-s", "--source", required=True, type=Path,
                   help=f"Folder whose files have the {what} to copy FROM "
                        "(one of the two folders originally passed to `check`)")
    p.add_argument("-d", "--dest", required=True, type=Path,
                   help=f"Folder whose files will receive the copied {what} "
                        "(the other folder originally passed to `check`)")
    p.add_argument("--output-dir", default="muxed", type=Path,
                   help="Directory to write muxed files to; every file from --dest ends up here, "
                        "unmuxable ones copied unchanged (default: muxed)")
    if both:
        p.add_argument("--sub-track-ids", type=str, default=None,
                       help="Comma-separated subtitle track IDs to copy (default: all)")
        p.add_argument("--audio-track-ids", type=str, default=None,
                       help="Comma-separated audio track IDs to copy (default: all)")
        p.add_argument("--sub-track-name", type=str, default=None,
                       help="Only copy subtitle tracks whose name contains this text (case-insensitive)")
        p.add_argument("--audio-track-name", type=str, default=None,
                       help="Only copy audio tracks whose name contains this text (case-insensitive)")
    else:
        kind = "subtitle" if what == "subtitles" else "audio"
        p.add_argument("--track-ids", type=str, default=None,
                       help=f"Comma-separated {kind} track IDs to copy (default: all)")
        p.add_argument("--track-name", type=str, default=None,
                       help=f"Only copy {kind} tracks whose name contains this text (case-insensitive)")
    p.add_argument("--force", action="store_true",
                   help="Also process DRIFT_DETECTED / LOW_CONFIDENCE pairs (risky: uses only the offset measured at the start)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be muxed without writing any output files")
    p.add_argument("--exclude-dest-tracks", action="store_true",
                   help=f"Drop the destination file's own {what} track(s) so the copied ones replace them")
    p.add_argument("--align", choices=["picture", "audio"], default="picture",
                   help="Where `check --video` measured a picture offset, place the copied tracks by it "
                        "(default; correct when file1 is the reference). audio: always use the audio offset")
    p.add_argument("--no-sync", action="store_true",
                   help="Copy tracks with zero delay and no stretch, ignoring what `check` measured")


def cmd_verify(args):
    """Independent check of retimed videos: measure the picture offset between file1 and the retimed
    file at many points and say whether it is constant and equals the offset the audio will be copied with.
    One line per episode."""
    require_tools("ffmpeg", "ffprobe")
    with open(args.report, newline="") as fh:
        rows = list(csv.DictReader(fh))
    steps_by_file1 = {}
    if args.steps:
        with open(args.steps, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("steps"):
                    steps_by_file1[r["file1"]] = json.loads(r["steps"])
    only = [x.strip().lower() for x in args.only.split(",")] if args.only else []
    tol = args.tolerance_ms / 1000.0
    results, n_ok, n_bad, n_unk = [], 0, 0, 0
    for row in rows:
        f1, f2 = Path(row["file1"]), Path(row["file2"])
        if only and not any(x in (f1.name + " " + f2.name).lower() for x in only):
            continue
        if not f2.exists() or not f1.exists():
            print(f"  {f2.name}: {c('SKIPPED', 'yellow')} (file not found)")
            continue
        st1, st2 = probe_start_time(f1), probe_start_time(f2)
        dur = min(probe_duration(f1), probe_duration(f2))
        expected = float(row["offset_ms"]) / 1000.0 if row.get("offset_ms") not in (None, "") else None
        times = [q(5.0 + i * args.spacing) for i in range(int(max(0.0, dur - 25.0) // args.spacing) + 1)]
        step_zones = []
        for stp in (steps_by_file1.get(row["file1"]) or {}).get("steps", []):
            z0, z1 = stp["from_s"] - st1, stp["to_s"] - st1            # steps are stored in container time
            step_zones.append((z0, z1))
            times += [q(z0 - 10.5), q(z1 + 0.5)]                       # a clip just before and just after each edit
        times = sorted({t for t in times if 0.0 <= t <= dur - 16.0})
        meas, fails = [], 0
        for t in times:
            r_ = picture_offset(f1, f2, t, SCAN_CLIP_S, st1, st2, 0.0, slack=3.0)
            if r_:
                meas.append((t, r_["P"]))
            else:
                fails += 1
        results += [(f2.name, t, p_) for t, p_ in meas]
        if len(meas) < 2:
            verdict, n_unk = c("INCONCLUSIVE", "yellow"), n_unk + 1
            detail = f"only {len(meas)} of {len(times)} probes measurable (static or very dark picture)"
        else:
            ref = statistics.median(p_ for _, p_ in meas)
            bad = [(t, p_) for t, p_ in meas if abs(p_ - ref) > tol]
            notes = []
            if expected is not None and abs(ref - expected) > tol:
                bad_offset = f"picture offset is {ref * 1000:+.1f} ms but the audio will be copied with {expected * 1000:+.1f} ms"
                notes.append(bad_offset)
            uncovered = [z0 for z0, z1 in step_zones
                         if not (any(t + SCAN_CLIP_S <= z0 + 0.5 for t, _ in meas) and any(t >= z1 - 0.5 for t, _ in meas))]
            if bad or (expected is not None and abs(ref - expected) > tol):
                verdict, n_bad = c("PROBLEM", "red"), n_bad + 1
                runs = []
                for t, p_ in bad:
                    if runs and t - runs[-1][1] <= args.spacing * 1.5 and abs(p_ - runs[-1][2]) < tol:
                        runs[-1][1] = t
                    else:
                        runs.append([t, t, p_])
                notes += [f"{fmt_t(a)}-{fmt_t(b + SCAN_CLIP_S)}: {(p_ - ref) * 1000:+.0f} ms off" for a, b, p_ in runs[:4]]
                if bad:
                    notes.insert(0, f"{len(bad)} of {len(meas)} probes differ by more than {args.tolerance_ms:.0f} ms "
                                    f"from the typical {ref * 1000:+.1f} ms")
                detail = "; ".join(notes)
            else:
                verdict, n_ok = c("OK", "green"), n_ok + 1
                detail = (f"{len(meas)} of {len(times)} probes measurable, picture offset {ref * 1000:+.1f} ms "
                          f"(worst deviation {max(abs(p_ - ref) for _, p_ in meas) * 1000:.1f} ms)")
                if uncovered:
                    detail += f"; no measurable picture next to the edit at {', '.join(fmt_t(z) for z in uncovered[:4])}"
        print(f"  {verdict}  {f2.name}: {detail}")
    print(f"\nSummary: {n_ok} OK, {n_bad} PROBLEM, {n_unk} INCONCLUSIVE")
    if args.out:
        with open(args.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "t_file1_s", "picture_offset_ms"])
            w.writerows((n, f"{t:.2f}", f"{p_ * 1000:.2f}") for n, t, p_ in results)
    if n_bad:
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(prog="mkv_sync_tool.py", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    check_p = sub.add_parser("check", help="Check sync between two folders of matched episodes")
    check_p.add_argument("-d1", "--dir1", required=True, type=Path, help="First encoding folder")
    check_p.add_argument("-d2", "--dir2", required=True, type=Path, help="Second encoding folder")
    check_p.add_argument("--out", default="sync_report.csv", type=Path, help="CSV report output path")
    check_p.add_argument("--clip-length", type=float, default=20.0,
                         help="Seconds of audio to compare at each sample point (default 20)")
    check_p.add_argument("--margin", type=float, default=90.0,
                         help="Seconds to skip at the start and before the end (default 90)")
    check_p.add_argument("--sample-points", type=int, default=5,
                         help="Evenly spaced points across the runtime to sample (default 5)")
    check_p.add_argument("--video", action="store_true",
                         help="Also measure the PICTURE offset for constant-offset pairs (frame matching), "
                              "so mux can place file1's audio by picture instead of by audio")
    check_p.add_argument("--audio-lang", type=str, default=None,
                         help="Language track to measure with (e.g. 'jpn'); must exist in BOTH files. "
                              "Default: jpn, then eng, then the first language common to both.")
    check_p.add_argument("--max-offset", type=float, default=10.0,
                         help="Largest offset in seconds to search for at each point (default 10)")
    check_p.add_argument("--min-confidence", type=float, default=0.3,
                         help="Minimum normalized correlation (0-1) for a clip to count (default 0.3)")
    check_p.add_argument("--drift-ms", type=float, default=20.0,
                         help="Offsets that differ by more than this across the episode count as drift (default 20)")
    check_p.set_defaults(func=cmd_check)

    loc = sub.add_parser("locate", help="Find exactly where the offset changes in DRIFT_DETECTED "
                                       "episodes (slow; run after `check`)")
    loc.add_argument("--report", default="sync_report.csv", type=Path,
                     help="CSV from `check` (default: sync_report.csv)")
    loc.add_argument("--out", default="sync_report_steps.csv", type=Path,
                     help="Where to write the report with step locations added "
                          "(every row is kept; default: sync_report_steps.csv)")
    loc.add_argument("--only", type=str, default=None,
                     help="Only analyse pairs whose filenames contain one of these comma-separated "
                          "texts (e.g. S01E05,S01E07)")
    loc.add_argument("--scan-spacing", type=float, default=30.0,
                     help="Seconds between coarse clips (default 30). Smaller finds shorter "
                          "mismatched segments but is slower.")
    loc.add_argument("--scan-start", type=float, default=5.0,
                     help="Where the coarse scan starts, in seconds (default 5)")
    loc.add_argument("--step-ms", type=float, default=10.0,
                     help="Offset change between consecutive clips that counts as a step (default 10)")
    loc.add_argument("--search-slack", type=float, default=2.0,
                     help="Seconds beyond the offsets `check` saw to search for matches (default 2)")
    loc.add_argument("--audio-lang", type=str, default=None,
                     help="Language track to measure with (default: same choice as `check`)")
    loc.add_argument("--video", action="store_true",
                     help="Also measure the PICTURE offset (frame matching) in each clip and compare it with "
                          "the audio offset, to see whether either release is out of sync with its own "
                          "picture. Slower; equal frame rates only")
    loc.add_argument("--debug-dir", type=str, default=None,
                     help="Write the raw per-clip and per-window evidence for each step here "
                          "(CSV files), to diagnose a result that looks wrong")
    loc.add_argument("--min-confidence", type=float, default=0.3,
                     help="Minimum normalized correlation (0-1) for a clip to count (default 0.3)")
    loc.set_defaults(func=cmd_locate)

    rt = sub.add_parser("retime-video", help="Fix DRIFT_DETECTED pairs whose steps are splices in black "
                                            "screen by retiming the target's video (lossless)")
    rt.add_argument("--report", default="sync_report_steps.csv", type=Path,
                    help="Report from `locate` (default: sync_report_steps.csv)")
    rt.add_argument("--out-dir", default="retimed", type=Path,
                    help="Where to write the retimed video-only files (default: retimed)")
    rt.add_argument("--out", default="sync_report_retimed.csv", type=Path,
                    help="Report for the retimed files, to feed to mux-* (default: sync_report_retimed.csv)")
    rt.add_argument("--only", type=str, default=None,
                    help="Only pairs whose filenames contain one of these comma-separated texts")
    rt.add_argument("--black-tol-ms", type=float, default=60.0,
                    help="How closely the black stretches' length difference must match the step (default 60)")
    rt.add_argument("--small-step-ms", type=float, default=45.0,
                    help="Steps up to this size that are not in black screen are ignored (about one frame; default 45)")
    rt.add_argument("--align", choices=["picture", "audio"], default="picture",
                    help="picture (default): make file2's pictures match file1's, which doesn't depend on "
                         "either file's audio being in sync with its picture (use `locate --video` first for "
                         "the best result). audio: size the edits from the audio steps")
    rt.add_argument("--junction-fallback", choices=["audio", "none"], default="audio",
                    help="If the picture can't pin a junction frame (e.g. a nearly static scene), place the "
                         "hold/squeeze from the audio-located zone when it is narrow and certain (default); "
                         "none: refuse the pair instead")
    rt.add_argument("--junction-fallback-max", type=float, default=1.0,
                    help="Widest audio zone, in seconds, that may be used for that fallback (default 1)")
    rt.add_argument("--squeeze-seconds", type=float, default=2.0,
                    help="Where file2 has extra frames at a junction, spread the removal over this many "
                         "seconds before it (default 2; the frames play a few percent faster)")
    rt.add_argument("--tail-seconds", type=float, default=0.0,
                    help="Also accept steps that retiming can't explain if they fall in the last N "
                         "seconds of the episode (e.g. 30); the leftover error is noted in the report")
    rt.add_argument("--dry-run", action="store_true", help="Show the plan without writing files")
    rt.set_defaults(func=cmd_retime)

    vf = sub.add_parser("verify-video", help="Check retimed videos against file1: one line per episode "
                                             "(run after `retime-video`)")
    vf.add_argument("--report", default="sync_report_retimed.csv", type=Path,
                    help="Report written by retime-video (default sync_report_retimed.csv)")
    vf.add_argument("--steps", type=Path, default=None,
                    help="sync_report_steps.csv from `locate`: also probe just before and after every edit")
    vf.add_argument("--spacing", type=float, default=45.0,
                    help="Seconds between probes (default 45; 10 s of picture is compared at each)")
    vf.add_argument("--tolerance-ms", type=float, default=25.0,
                    help="Deviation from the typical offset that counts as a problem (default 25, about half a frame)")
    vf.add_argument("--only", default="", help="Only episodes whose file name contains this (comma separated)")
    vf.add_argument("--out", type=Path, default=None, help="Also write every probe to this CSV")
    vf.set_defaults(func=cmd_verify)

    p = sub.add_parser("mux-subs", help="Copy subtitle track(s) using a report from `check`")
    add_mux_args(p, "subtitles")
    p.set_defaults(func=cmd_mux)

    p = sub.add_parser("mux-audio", help="Copy audio track(s) using a report from `check`")
    add_mux_args(p, "audio")
    p.set_defaults(func=cmd_mux_audio)

    p = sub.add_parser("mux-both", help="Copy both subtitle and audio track(s) using a report from `check`")
    add_mux_args(p, "subs+audio", both=True)
    p.set_defaults(func=cmd_mux_both)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()