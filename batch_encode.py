#!/usr/bin/env python3
"""
Batch-encode every video file in a folder with ffmpeg (x265 or AV1,
CRF-based). All audio, subtitle, and attachment streams are copied through
unchanged -- no track selection/remuxing step, no per-series metadata
editing.

Run with -h/--help to see all options. Every option has a sensible default,
so `python3 batch_encode.py --source-dir "./MySeries"` is enough to get
going.

Requires: ffmpeg, python3 (3.8+).
Optional: pip install colorama  (improves color support on old Windows
consoles; modern Windows Terminal/PowerShell work fine without it)
"""

import argparse
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_X265_PARAMS = "aq-mode=3:psy-rd=1.3:aq-strength=0.8:bframes=6:psy-rdoq=3:no-sao=1"
DEFAULT_AV1_PARAMS = "tune=1:film-grain=0:enable-tf=0:enable-overlays=1:enable-qm=0"

# ---- terminal color helpers ----
try:
    import colorama
    colorama.init()
except ImportError:
    pass


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


COLOR_ENABLED = True


def c(text, color):
    if not COLOR_ENABLED:
        return text
    return f"{color}{text}{C.RESET}"


def header(text):
    print(c(f"== {text} ==", C.BOLD + C.CYAN))


def info(text):
    print(f"  -> {text}")


def ok(text):
    print(c(f"  {text}", C.GREEN))


def warn(text):
    print(c(f"  {text}", C.YELLOW))


def fail(text):
    print(c(f"  {text}", C.BOLD + C.RED))


def dim(text):
    print(c(text, C.GRAY))


def parse_args():
    p = argparse.ArgumentParser(
        description="Batch-encode a folder of video files with ffmpeg (x265/AV1).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    p.add_argument("--source-dir", type=Path, default=Path("."),
                    help="Folder containing source video files (searched non-recursively)")
    p.add_argument("--output-dir", type=Path, default=Path("./encoded"),
                    help="Folder to write encoded files to")
    p.add_argument("--extensions", default=".mkv,.mp4,.m2ts,.ts",
                    help="Comma-separated list of source file extensions to match")
    p.add_argument("--ffmpeg", default="ffmpeg",
                    help="ffmpeg binary name or full path")

    p.add_argument("--dry-run", action="store_true",
                    help="Print what would happen without encoding or writing anything")
    p.add_argument("--no-color", action="store_true",
                    help="Disable colored terminal output")

    p.add_argument("--codec", choices=["x265", "av1"], required=True,
                    help="Video codec to encode with")
    p.add_argument("--crf", type=str, required=True,
                    help="CRF value")
    p.add_argument("--preset", type=str, default=None,
                    help="Encoder preset (default: 'slow' for x265, '4' for AV1)")
    p.add_argument("--x265-params", default=DEFAULT_X265_PARAMS,
                    help="Extra -x265-params string (only used with --codec x265)")
    p.add_argument("--av1-params", default=DEFAULT_AV1_PARAMS,
                    help="Extra -svtav1-params string (only used with --codec av1)")

    p.add_argument("--fps-mode", choices=["cfr", "vfr"], required=True,
                    help="'cfr' forces a fixed frame rate (safe default for unfamiliar "
                         "sources); 'vfr' preserves the source's real per-frame timestamps "
                         "-- only use after confirming (via ffprobe) that a source's VFR "
                         "content actually matters")
    p.add_argument("--fps", default="24000/1001",
                    help="Target frame rate when --fps-mode is 'cfr' (e.g. 24000/1001, 25/1)")

    p.add_argument("--color-tag", action="store_true",
                    help="Force BT.709 color tags on output (color_range=tv, "
                         "color_trc/colorspace/color_primaries=bt709). Off by default -- "
                         "only enable after confirming with ffprobe that the source is "
                         "actually BT.709; forcing the wrong tag will shift colors on playback.")

    p.add_argument("--output-suffix", default=None,
                    help="Text appended to output filename before the extension "
                         "(default: ' [<codec>]')")
    p.add_argument("--output-ext", default=".mkv",
                    help="Output file extension")

    p.add_argument("--verify-frames", action="store_true",
                    help="After each encode, also compare exact frame counts between "
                         "source and output (in addition to the default duration check). "
                         "Requires fully decoding both files, so it's slow on long videos "
                         "-- off by default.")
    p.add_argument("--skip-verify", action="store_true",
                    help="Skip the post-encode duration sanity check entirely")

    args = p.parse_args()

    # resolve codec-dependent defaults
    if args.preset is None:
        args.preset = "slow" if args.codec == "x265" else "4"
    if args.output_suffix is None:
        args.output_suffix = f" [{args.codec}]"

    args.extensions = {e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
                        for e in args.extensions.split(",") if e.strip()}

    if args.fps_mode == "cfr":
        if not re.fullmatch(r"\d+(/\d+)?", args.fps.strip()):
            p.error(f"--fps '{args.fps}' doesn't look like a valid rate. "
                    f"Expected an integer (e.g. 25) or fraction (e.g. 24000/1001).")

    return args


def find_ffmpeg(ffmpeg_name):
    on_path = shutil.which(ffmpeg_name)
    if on_path:
        return on_path

    system = platform.system()
    candidates = []
    if system == "Windows":
        candidates = [r"C:\ffmpeg\bin\ffmpeg.exe"]
    elif system == "Darwin":
        candidates = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    elif system == "Linux":
        candidates = ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg"]

    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def check_prereqs(args):
    ffmpeg_path = find_ffmpeg(args.ffmpeg)
    if not ffmpeg_path:
        fail(f"ffmpeg not found (looked for '{args.ffmpeg}' on PATH and common install locations).")
        print("Install ffmpeg and ensure it's on PATH, or pass --ffmpeg with its full path.")
        sys.exit(1)

    if not args.source_dir.exists():
        fail(f"Source directory not found: {args.source_dir}")
        sys.exit(1)

    if not args.dry_run:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    return ffmpeg_path


def find_source_files(args):
    return [
        f for f in sorted(args.source_dir.iterdir())
        if f.is_file() and f.suffix.lower() in args.extensions
    ]


def build_output_path(args, source_file):
    return args.output_dir / f"{source_file.stem}{args.output_suffix}{args.output_ext}"


def build_encode_command(args, ffmpeg_path, source_file, out_file):
    cmd = [
        ffmpeg_path,
        "-analyzeduration", "100M",
        "-probesize", "50M",
        "-i", str(source_file),
    ]

    if args.fps_mode == "vfr":
        cmd += ["-enc_time_base", "demux"]

    cmd += [
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map", "0:s?",
        "-map", "0:t?",
    ]

    color_args = []
    if args.color_tag:
        color_args = [
            "-color_range", "tv",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_primaries", "bt709",
        ]

    if args.codec == "x265":
        cmd += [
            "-c:v", "libx265", "-preset", args.preset, "-crf", args.crf,
            "-pix_fmt", "yuv420p10le", "-profile:v", "main10",
            "-fps_mode", args.fps_mode,
        ]
        if args.fps_mode == "cfr":
            cmd += ["-r", args.fps]
        cmd += color_args
        cmd += ["-x265-params", args.x265_params]
    else:  # av1
        cmd += [
            "-c:v", "libsvtav1", "-preset", args.preset, "-crf", args.crf,
            "-pix_fmt", "yuv420p10le",
            "-fps_mode", args.fps_mode,
        ]
        if args.fps_mode == "cfr":
            cmd += ["-r", args.fps]
        cmd += color_args
        cmd += ["-svtav1-params", args.av1_params]

    cmd += [
        "-c:a", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        str(out_file),
    ]
    return cmd


def run_ffmpeg(cmd):
    process = subprocess.Popen(
        cmd, stdout=None, stderr=None, text=True, stdin=subprocess.DEVNULL,
    )
    try:
        process.wait()
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    return process.returncode == 0


def get_duration(path):
    """Container duration in seconds, from metadata -- fast, no decoding."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def get_frame_count(path):
    """Exact video frame count -- requires fully decoding the file, so slow."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def verify_encode(args, source_file, out_file):
    """Post-encode sanity check: duration always, frame count if requested.
    Prints a warning (doesn't fail the run) if something looks off, since a
    small mismatch isn't necessarily fatal but is worth a human's attention."""
    if args.skip_verify:
        return

    src_dur = get_duration(source_file)
    out_dur = get_duration(out_file)
    if src_dur is None or out_dur is None:
        warn("could not verify duration (ffprobe read failed)")
    else:
        diff = abs(src_dur - out_dur)
        if diff > 0.5:
            warn(f"duration mismatch: source {src_dur:.3f}s vs output {out_dur:.3f}s "
                 f"(diff {diff:.3f}s) -- worth checking this file")
        else:
            dim(f"    duration OK: {out_dur:.3f}s (source {src_dur:.3f}s)")

    if args.verify_frames:
        src_frames = get_frame_count(source_file)
        out_frames = get_frame_count(out_file)
        if src_frames is None or out_frames is None:
            warn("could not verify frame count (ffprobe read failed)")
        elif src_frames != out_frames:
            warn(f"frame count mismatch: source {src_frames} vs output {out_frames} "
                 f"-- worth checking this file")
        else:
            dim(f"    frame count OK: {out_frames} frames")


def encode_file(args, ffmpeg_path, source_file):
    out_file = build_output_path(args, source_file)

    if out_file.exists():
        warn(f"already exists, skipping: {out_file.name}")
        return True

    cmd = build_encode_command(args, ffmpeg_path, source_file, out_file)

    if args.dry_run:
        info(f"[dry run] would encode ({args.codec}): {source_file.name} -> {out_file.name}")
        dim("    " + " ".join(str(x) for x in cmd))
        return True

    info(f"encoding ({args.codec}, preset {args.preset}, crf {args.crf}): "
         f"{source_file.name} -> {out_file.name}")

    try:
        success = run_ffmpeg(cmd)
    except KeyboardInterrupt:
        out_file.unlink(missing_ok=True)
        fail(f"interrupted -- removed partial output: {out_file.name}")
        raise

    if not success:
        fail(f"FAILED: {source_file.name}")
        out_file.unlink(missing_ok=True)
        return False

    ok(f"done: {out_file.name}")
    verify_encode(args, source_file, out_file)
    return True


def main():
    global COLOR_ENABLED
    args = parse_args()
    COLOR_ENABLED = not args.no_color

    ffmpeg_path = check_prereqs(args)
    source_files = find_source_files(args)

    if not source_files:
        fail(f"No source files found in {args.source_dir} matching {sorted(args.extensions)}.")
        sys.exit(1)

    mode_label = " (DRY RUN -- no files will be written)" if args.dry_run else ""
    print(c(f"Found {len(source_files)} file(s) to encode.{mode_label}", C.BOLD))
    print()
    print(c("-- Config --", C.BOLD))
    dim(f"  source dir:   {args.source_dir}")
    dim(f"  output dir:   {args.output_dir}")
    dim(f"  extensions:   {', '.join(sorted(args.extensions))}")
    dim(f"  codec:        {args.codec}  (preset {args.preset}, crf {args.crf})")
    fps_desc = f"{args.fps_mode}" + (f" ({args.fps})" if args.fps_mode == "cfr" else "")
    dim(f"  fps mode:     {fps_desc}")
    dim(f"  color tags:   {'forced BT.709' if args.color_tag else 'not set (passthrough)'}")
    dim(f"  output name:  <name>{args.output_suffix}{args.output_ext}")
    verify_desc = "off" if args.skip_verify else ("duration + frame count" if args.verify_frames else "duration only")
    dim(f"  verification: {verify_desc}")
    print()

    failures = []
    try:
        for source_file in source_files:
            header(source_file.name)
            success = encode_file(args, ffmpeg_path, source_file)
            if not success:
                failures.append(source_file.name)
            print()
    except KeyboardInterrupt:
        print()
        fail("Interrupted by user -- stopping batch.")
        sys.exit(130)

    if args.dry_run:
        print(c("Dry run complete. No files were written.", C.BOLD + C.CYAN))
    else:
        print(c("All files processed.", C.BOLD))
        if failures:
            fail(f"{len(failures)} failure(s):")
            for f in failures:
                fail(f"  - {f}")


if __name__ == "__main__":
    main()
