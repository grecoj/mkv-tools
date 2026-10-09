#!/usr/bin/env python3
"""Convert 25fps PAL-speedup MKVs to 23.976fps. Video is remuxed losslessly (timestamps only);
audio is slowed and re-encoded; subtitles and chapters, if present, are stretched to match."""
import argparse, json, shutil, subprocess, sys, tempfile
from pathlib import Path

STRETCH = "25025/24000"   # timestamp multiplier (25 -> 23.976)
SPEED = 24000 / 25025     # playback speed multiplier

def run(cmd, ok=(0,)):
    if subprocess.run(cmd).returncode not in ok:
        raise RuntimeError("command failed: " + " ".join(map(str, cmd)))

def identify(path):
    out = subprocess.run(["mkvmerge", "-J", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["tracks"]

def convert(src, outdir, args):
    out = outdir / src.name
    if out.exists():
        print(f"SKIP (output exists): {src.name}")
        return
    tracks = identify(src)
    video = [t for t in tracks if t["type"] == "video"]
    audio = [t for t in tracks if t["type"] == "audio"]
    subs = [t for t in tracks if t["type"] == "subtitles"]
    if not video:
        print(f"SKIP (no video track): {src.name}")
        return
    vid = video[0]["id"]
    dur = video[0]["properties"].get("default_duration")
    if dur != 40000000 and not args.force:
        print(f"SKIP (video is not 25fps, default_duration={dur}; use --force to override): {src.name}")
        return

    with tempfile.TemporaryDirectory(dir=outdir) as tmp:
        tmp = Path(tmp)
        # 1. Slow each audio track
        audio_files = []
        for n, t in enumerate(audio):
            rate = int(t["properties"].get("audio_sampling_frequency", 48000))
            if args.tempo_only:
                af = f"atempo={SPEED:.8f}"
            else:
                af = f"asetrate={rate}*24000/25025,aresample={rate}"
            f = tmp / f"audio{n}.mka"
            run(["ffmpeg", "-v", "error", "-stats", "-i", str(src), "-map", f"0:{t['id']}",
                 "-vn", "-sn", "-af", af, "-c:a", args.acodec, str(f)])
            audio_files.append((f, t["properties"]))

        # 2. Final mux: lossless video retime, slowed audio, stretched subs and chapters
        cmd = ["mkvmerge", "-o", str(out),
               "--default-duration", f"{vid}:24000/1001p",
               "--fix-bitstream-timing-information", f"{vid}:1",
               "--chapter-sync", f"0,{STRETCH}",
               "-A", "-S", str(src)]
        for f, p in audio_files:
            if p.get("language"):
                cmd += ["--language", f"0:{p['language']}"]
            if p.get("track_name"):
                cmd += ["--track-name", f"0:{p['track_name']}"]
            cmd += ["--default-track-flag", f"0:{int(p.get('default_track', False))}", str(f)]
        if subs:
            cmd += ["-D", "-A", "-M", "--no-chapters"]
            for t in subs:
                cmd += ["--sync", f"{t['id']}:0,{STRETCH}"]
            cmd += [str(src)]
        run(cmd, ok=(0, 1))  # exit code 1 = warnings only
    print(f"DONE: {out}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tempo-only", action="store_true",
                    help="slow audio without lowering pitch (use if the source audio was pitch-corrected)")
    ap.add_argument("--acodec", default="flac", help="ffmpeg audio codec for slowed audio (default: flac)")
    ap.add_argument("--outdir", default="converted", help="output folder (default: converted)")
    ap.add_argument("--force", action="store_true", help="convert even if video is not detected as 25fps")
    args = ap.parse_args()

    for tool in ("ffmpeg", "mkvmerge"):
        if not shutil.which(tool):
            sys.exit(f"{tool} not found on PATH")

    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)
    files = sorted(Path(".").glob("*.mkv"))
    if not files:
        sys.exit("No .mkv files found in the current directory")
    for f in files:
        print(f"\n== {f.name}")
        try:
            convert(f, outdir, args)
        except Exception as e:
            print(f"ERROR: {f.name}: {e}")

if __name__ == "__main__":
    main()
