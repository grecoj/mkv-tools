#!/usr/bin/env python3
"""
opus_convert.py

Batch-convert lossless audio tracks (FLAC, DTS-HD Master Audio, Dolby
TrueHD, PCM) in .mkv files to Opus. All other tracks (video, subtitles, lossy
audio, attachments, chapters) are preserved via stream copy -- nothing
is dropped.

Bitrate mapping (by channel count/layout of each lossless track), per --quality tier:
    2.0 (stereo)    -> medium: 160k   high (default): 192k
    2.1 (L/R/LFE)   -> medium: 192k   high (default): 224k
    3.0 (L/R/C)     -> medium: 256k   high (default): 288k
    3.1 (L/R/C/LFE) -> medium: 288k   high (default): 320k
    5.1 (6ch)       -> medium: 384k   high (default): 448k
    7.1 (8ch)       -> medium: 448k   high (default): 510k
    other           -> copied as-is (unknown target bitrate), flagged

Non-lossless audio (AC3, DTS core, existing Opus, AAC, etc.) is always
copied as-is and flagged. Plain "DTS" (the lossy core, without the "MA"
extension) is treated as lossy and copied rather than re-encoded.

3-channel lossless tracks are ambiguous (could be L/R/LFE "2.1" or L/R/C
"3.0") and require --three-channel-layout to be passed explicitly; a
4-channel track is similarly ambiguous (could be L/R/C/LFE "3.1", or
"quad"/"4.0", which have no LFE at all) and requires
--four-channel-layout. Neither is auto-detected (see plan_audio_tracks
docstring for why -- ffmpeg's own encoders can write a misleading
default guess into a file's metadata that's indistinguishable from a
genuine one).

Usage:
    python3 opus_convert.py [-i INPUT_DIR] [-o OUTPUT_DIR] [-r] [--quality high|medium] [--dry-run]
    python3 opus_convert.py --restore [-i INPUT_DIR] [-o OUTPUT_DIR] [-y] [--dry-run]

    -i, --input-directory   Directory containing .mkv files (default: current directory)
    -o, --output-directory  Directory to write converted files to (default: current directory)
    -r, --recursive         Also search subdirectories; output and backup-audio/ mirror
                             the same subdirectory structure as the input
    --quality               "high" (default) or "medium" bitrate tier -- see table above.
                             Omitting --quality entirely means high; "--quality" given with
                             no value is a usage error (must be "high" or "medium").
    --restore               Move files back from backup-audio/ to input_dir, overwriting
                             the converted versions currently there. Prompts for
                             confirmation unless -y/--yes is given.

If the input and output directories are the same (the default), files are
converted in place: each original file is moved into ./backup-audio
(relative to the output directory) before its replacement is written to
the original path. Files that have no lossless audio tracks left to
convert are skipped entirely, so re-running the tool is safe/idempotent.

Requires: ffmpeg, ffprobe on PATH. mkvpropedit (from mkvtoolnix) is used
if available to refresh track statistics tags after conversion, since
ffmpeg otherwise carries over the original BPS tag onto the new Opus
track and tools like MediaInfo will display the stale bitrate.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


def supports_color() -> bool:
    return sys.stdout.isatty()


def c(text: str, color: str) -> str:
    if not supports_color():
        return text
    return f"{color}{text}{C.RESET}"


def ffprobe_streams(path: Path) -> list:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe returned a non-zero exit code")
    return json.loads(result.stdout).get("streams", [])


def is_lossless_source(codec: str, profile: str) -> bool:
    """
    True for audio codecs/profiles that are lossless and worth
    re-encoding to Opus: FLAC, TrueHD, PCM (any variant -- ffprobe
    reports these as "pcm_s16le", "pcm_s24le", etc.), and DTS-HD Master
    Audio (but not the plain lossy DTS core, which reports the same
    codec_name "dts").
    """
    if codec in ("flac", "truehd") or codec.startswith("pcm_"):
        return True
    if codec == "dts":
        profile_upper = (profile or "").upper()
        return "MA" in profile_upper or "MASTER AUDIO" in profile_upper
    return False


def bitrate_for_channels(
    channels: int, quality: str,
    three_channel_layout: str | None = None, four_channel_layout: str | None = None,
) -> str | None:
    high = quality == "high"
    if channels == 2:
        return "192k" if high else "160k"
    if channels == 3:
        if three_channel_layout == "2.1":
            # LFE is band-limited (see LFE_LOWPASS_HZ) so it costs very
            # little to encode -- this is essentially stereo plus a
            # cheap third channel, not three full-bandwidth channels.
            return "224k" if high else "192k"
        if three_channel_layout == "3.0":
            # Three full-bandwidth channels (L/R/C); more demanding than
            # stereo but with no surrounds/LFE to offload bits to.
            return "288k" if high else "256k"
        return None  # layout not yet known -- caller handles this as 'blocked'
    if channels == 4:
        if four_channel_layout == "3.1":
            # Three full-bandwidth channels (L/R/C) plus a band-limited
            # LFE channel -- same reasoning as 3.0 plus the small LFE
            # increment seen in the 2.0 -> 2.1 step.
            return "320k" if high else "288k"
        return None  # layout not yet known -- caller handles this as 'blocked'
    if channels == 6:
        return "448k" if high else "384k"
    if channels == 8:
        return "510k" if high else "448k"
    return None


def target_layout_for_channels(
    channels: int, three_channel_layout: str | None, four_channel_layout: str | None = None,
) -> str | None:
    """Standard Opus-compatible channel layout name, if channel count needs one."""
    if channels == 3:
        return three_channel_layout  # None means "not yet specified" -- caller must handle
    if channels == 4:
        return four_channel_layout  # same
    return {6: "5.1", 8: "7.1"}.get(channels)


# Matches lossless-codec names that might appear in an existing track title,
# ordered most-specific first so "DTS-HD MA" isn't partially matched by "DTS".
CODEC_NAME_PATTERN = re.compile(r"DTS-HD\s*MA|TrueHD|DTS|FLAC", re.IGNORECASE)

# Common ISO 639-2 (and a few 639-1) language codes as seen in MKV tags.
LANGUAGE_NAMES = {
    "eng": "English", "en": "English",
    "jpn": "Japanese", "ja": "Japanese",
    "spa": "Spanish", "es": "Spanish",
    "fre": "French", "fra": "French", "fr": "French",
    "ger": "German", "deu": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese",
    "kor": "Korean", "ko": "Korean",
    "ara": "Arabic", "ar": "Arabic",
    "hin": "Hindi", "hi": "Hindi",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch",
    "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "no": "Norwegian",
    "dan": "Danish", "da": "Danish",
    "fin": "Finnish", "fi": "Finnish",
    "pol": "Polish", "pl": "Polish",
    "tur": "Turkish", "tr": "Turkish",
    "gre": "Greek", "ell": "Greek", "el": "Greek",
    "heb": "Hebrew", "he": "Hebrew",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "ind": "Indonesian", "id": "Indonesian",
    "ces": "Czech", "cze": "Czech", "cs": "Czech",
    "hun": "Hungarian", "hu": "Hungarian",
    "ron": "Romanian", "rum": "Romanian", "ro": "Romanian",
    "ukr": "Ukrainian", "uk": "Ukrainian",
}


def language_name(language_code: str) -> str:
    """
    Human-readable language name for a common ISO 639 code. Always
    returns something usable: "und" (or missing) maps to "Undetermined",
    and any other unrecognized code is title-cased as a best effort
    (e.g. an unlisted code "xyz" -> "Xyz") rather than giving up.
    """
    if not language_code or language_code.strip().lower() == "und":
        return "Undetermined"
    code = language_code.strip().lower()
    return LANGUAGE_NAMES.get(code, language_code.strip().title())


def compute_title(original_title: str, layout: str | None, language: str = "", force_rename: bool = False) -> str:
    """
    If the source title mentions the original codec (FLAC/DTS/TrueHD),
    rename that portion to "Opus" so e.g. "Japanese DTS-HD MA 5.1"
    becomes "Japanese Opus 5.1". If there's no title at all, build one
    from the language and channel layout instead, e.g. "English 5.1" --
    always in this format, even for an unrecognized/missing language
    code (see language_name). If a title exists but doesn't mention a
    codec name (e.g. "English 2.0", "Commentary"), it's left completely
    unchanged -- there's nothing recognizable to rename, and it
    shouldn't be discarded.

    force_rename overrides all of the above: always returns the
    "{Language} {Layout}" form (e.g. "English 2.0"), replacing any
    existing title regardless of what it says.
    """
    layout_label = layout or "2.0"

    if force_rename:
        return f"{language_name(language)} {layout_label}"

    if not original_title:
        return f"{language_name(language)} {layout_label}"

    if CODEC_NAME_PATTERN.search(original_title):
        return CODEC_NAME_PATTERN.sub("Opus", original_title)

    return original_title


def plan_audio_tracks(
    streams: list, quality: str,
    three_channel_layout: str | None, four_channel_layout: str | None = None,
) -> list:
    """
    Build a per-audio-track plan. Each entry:
        audio_index: 0-based index among audio streams only (matches ffmpeg's a:N)
        codec, profile, channels, language
        action: 'convert' | 'copy' | 'blocked'
        reason: human-readable explanation
        bitrate: target bitrate string, or None if copying/blocked
        layout: target channel layout for the aformat filter, or None

    A 3-channel lossless track is ambiguous (could be L/R/LFE "2.1" or
    L/R/C "3.0") and a 4-channel one similarly so (L/R/C/LFE "3.1" vs.
    "quad"/"4.0", which have no LFE at all) -- both are marked 'blocked'
    unless the corresponding --N-channel-layout flag was explicitly
    provided by the caller. This is deliberately NOT auto-detected from
    the source's reported channel_layout: testing showed ffmpeg's own
    encoders can silently write an incorrect guess (e.g. "2.1" for a
    3-channel FLAC with no explicit layout given, when the FLAC spec's
    actual default is "3.0"/L-C-R) that is indistinguishable from a
    genuine, correctly-tagged layout. Trusting that blindly risks
    silently applying the wrong transform (e.g. low-passing a genuine
    center or surround channel as if it were LFE) with no way to detect
    the mistake afterward.
    """
    plan = []
    audio_idx = 0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue

        codec = s.get("codec_name", "unknown")
        profile = s.get("profile", "")
        channels = s.get("channels", 0)
        tags = s.get("tags", {})
        language = tags.get("language", "und")
        title = tags.get("title", "")

        entry = {
            "audio_index": audio_idx,
            "codec": codec,
            "profile": profile,
            "channels": channels,
            "language": language,
            "title": title,
        }

        if not is_lossless_source(codec, profile):
            entry["action"] = "copy"
            label = f"{codec}" + (f" ({profile})" if profile else "")
            entry["reason"] = f"not a lossless source (codec={label})"
            entry["bitrate"] = None
            entry["layout"] = None
        elif channels == 3 and three_channel_layout is None:
            entry["action"] = "blocked"
            entry["reason"] = (
                "3-channel lossless track is ambiguous (L/R/LFE vs L/R/C) -- "
                "pass --three-channel-layout 2.1 or --three-channel-layout 3.0"
            )
            entry["bitrate"] = None
            entry["layout"] = None
        elif channels == 4 and four_channel_layout is None:
            entry["action"] = "blocked"
            entry["reason"] = (
                "4-channel lossless track is ambiguous (L/R/C/LFE vs quad/other 4ch layouts) -- "
                "pass --four-channel-layout 3.1"
            )
            entry["bitrate"] = None
            entry["layout"] = None
        else:
            bitrate = bitrate_for_channels(channels, quality, three_channel_layout, four_channel_layout)
            if bitrate is None:
                entry["action"] = "copy"
                entry["reason"] = f"lossless source with unsupported channel count ({channels}ch)"
                entry["bitrate"] = None
                entry["layout"] = None
            else:
                entry["action"] = "convert"
                label = f"{codec}" + (f" ({profile})" if profile else "")
                entry["reason"] = f"{label} {channels}ch"
                entry["bitrate"] = bitrate
                entry["layout"] = target_layout_for_channels(channels, three_channel_layout, four_channel_layout)

        plan.append(entry)
        audio_idx += 1
    return plan


LFE_LOWPASS_HZ = 120  # standard-ish LFE crossover point; keeps mapping-family-255
                       # from spending bits encoding a "full-range" LFE channel


STREAM_TYPE_SPECIFIER = {"video": "v", "audio": "a", "subtitle": "s", "attachment": "t"}


def build_disposition_args(streams: list) -> list:
    """
    Explicitly re-apply each stream's original disposition flags via
    -disposition:TYPE:N. Needed because ffmpeg was observed to
    auto-promote the first stream of a type to 'default' when mapping
    multiple streams of that type where none were originally marked
    default (confirmed for subtitles; the same pattern showed up
    unnoticed on an attachment stream in an earlier session too) --
    relying on implicit per-stream disposition copy-through isn't safe
    for files where no stream of a type has an explicit default.
    """
    args = []
    type_counters = {t: 0 for t in STREAM_TYPE_SPECIFIER}
    for s in streams:
        codec_type = s.get("codec_type")
        if codec_type not in STREAM_TYPE_SPECIFIER:
            continue
        idx = type_counters[codec_type]
        type_counters[codec_type] += 1
        disposition = s.get("disposition", {})
        flags = [name for name, val in disposition.items() if val]
        value = "+".join(flags) if flags else "0"
        args += [f"-disposition:{STREAM_TYPE_SPECIFIER[codec_type]}:{idx}", value]
    return args


def build_ffmpeg_cmd(src: Path, dst: Path, plan: list, streams: list, force_rename: bool = False) -> list:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        # "?" means map this type if present, skip silently if not.
        "-map", "0:v?",
        "-map", "0:a?",
        "-map", "0:s?",
        "-map", "0:t?",
        # No blanket "-c:a copy" default -- every audio stream gets its own
        # explicit -c:a:N directive below (copy or libopus) instead.
        "-c:v", "copy",
        "-c:s", "copy",
        "-c:t", "copy",
        "-bitexact",
        *build_disposition_args(streams),
    ]
    for entry in plan:
        ai = entry["audio_index"]
        if entry["action"] != "convert":
            cmd += [f"-c:a:{ai}", "copy"]
            continue
        if entry["layout"] in ("2.1", "3.1"):
            # Mapping family 255 (see below) treats every channel as a
            # generic full-range signal -- it has no concept of "LFE", so
            # it will spend bits on frequencies the subwoofer channel
            # shouldn't contain. channelmap normalizes the layout tag and
            # a per-channel lowpass (targeting just the LFE channel, via
            # the "channels" option) removes anything above the crossover
            # point so there's nothing there to spend bits on.
            chmap = (
                "FL-FL|FR-FR|LFE-LFE" if entry["layout"] == "2.1"
                else "FL-FL|FR-FR|FC-FC|LFE-LFE"
            )
            filt = f"channelmap={chmap}:{entry['layout']},lowpass=f={LFE_LOWPASS_HZ}:channels=LFE"
            cmd += [f"-filter:a:{ai}", filt]
            # Neither layout has a standard Opus mapping-family-1 shape
            # (family 1's 3-channel case is Vorbis-order L/C/R, and its
            # 4-channel case is "quad" -- neither has an LFE channel at
            # all). Mapping family 255 treats channels as
            # independent/discrete instead, preserving them exactly as
            # given.
            cmd += [f"-mapping_family:a:{ai}", "255"]
        elif entry["layout"] == "3.0":
            # L/R/C maps cleanly onto Opus's mapping family 1 (Vorbis
            # channel order); ffmpeg reorders to match automatically.
            # Using family 1 explicitly (rather than 255) lets the
            # encoder use stereo coupling between channels, which is
            # more bitrate-efficient than coding each one independently.
            cmd += [f"-filter:a:{ai}", "aformat=channel_layouts=3.0"]
            cmd += [f"-mapping_family:a:{ai}", "1"]
        elif entry["layout"]:
            # Some sources tag surround channels with a non-default order
            # (e.g. 5.1(side), or DTS's C L R Ls Rs LFE ordering); libopus
            # only accepts the standard mapping, so force it explicitly.
            cmd += [f"-filter:a:{ai}", f"aformat=channel_layouts={entry['layout']}"]
        elif entry["channels"] == 2:
            # Plain stereo. Family 0 is the standard mapping for 1-2
            # channels per RFC 7845 -- setting it explicitly rather than
            # relying on libopus's auto-selection costs nothing and
            # removes any ambiguity. (Note: this alone does not fix
            # ffmpeg 9.0.1's separate bug where running multiple
            # simultaneous libopus encoder instances in one process
            # produces audio that decodes fine via ffmpeg itself but
            # plays silent in other players -- that requires ffmpeg
            # 8.x or earlier until it's fixed upstream.)
            cmd += [f"-mapping_family:a:{ai}", "0"]
        cmd += [f"-c:a:{ai}", "libopus", f"-b:a:{ai}", entry["bitrate"]]
        title = compute_title(entry["title"], entry["layout"], entry["language"], force_rename)
        cmd += [f"-metadata:s:a:{ai}", f"title={title}"]
    cmd.append(str(dst))
    return cmd


def print_plan(fname: str, plan: list, dry_run: bool, force_rename: bool = False) -> None:
    prefix = "[DRY RUN] " if dry_run else ""
    print(c(f"{prefix}{fname}", C.BOLD + C.CYAN))
    if not plan:
        print(c("  no audio tracks found", C.YELLOW))
        return
    for e in plan:
        tag = f"audio[{e['audio_index']}] ({e['language']}, {e['channels']}ch, {e['codec']})"
        if e["action"] == "convert":
            new_title = compute_title(e["title"], e["layout"], e["language"], force_rename)
            print(c(f"  {tag} -> Opus @ {e['bitrate']} | title: \"{new_title}\"", C.GREEN))
        elif e["action"] == "blocked":
            print(c(f"  {tag} -> BLOCKED ({e['reason']})", C.RED))
        else:
            print(c(f"  {tag} -> COPY ({e['reason']})", C.YELLOW))


def get_ffmpeg_major_version() -> int | None:
    """Best-effort parse of ffmpeg's major version number, or None if it can't be determined."""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        match = re.search(r"ffmpeg version (\d+)\.", result.stdout)
        return int(match.group(1)) if match else None
    except Exception:
        return None


def refresh_track_stats(path: Path, warnings: list) -> None:
    if shutil.which("mkvpropedit") is None:
        return  # already warned once globally
    result = subprocess.run(
        ["mkvpropedit", str(path), "--add-track-statistics-tags"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        warnings.append(f"{path.name}: mkvpropedit failed to refresh stats: {result.stderr.strip()}")


def collect_mkv_files(input_dir: Path, output_dir: Path, same_dir: bool, recursive: bool) -> list:
    """
    Find .mkv files under input_dir (optionally recursive), returning a
    sorted list of (absolute_path, relative_path) pairs. relative_path is
    used to preserve subdirectory structure in both the output and
    backup-audio locations. Excludes anything already inside a
    backup-audio/ folder, and -- when scanning recursively with a
    separate output directory -- anything already inside that output
    directory, so a previous run's results aren't picked up as new input.
    """
    pattern = input_dir.rglob("*.mkv") if recursive else input_dir.glob("*.mkv")
    output_resolved = output_dir.resolve()
    pairs = []
    for f in pattern:
        if "backup-audio" in f.parts:
            continue
        if recursive and not same_dir:
            try:
                f.resolve().relative_to(output_resolved)
                continue  # already inside the (separate) output directory
            except ValueError:
                pass
        pairs.append((f, f.relative_to(input_dir)))
    pairs.sort(key=lambda pair: str(pair[1]))
    return pairs


def restore_backup(input_dir: Path, output_dir: Path, dry_run: bool, assume_yes: bool) -> None:
    """
    Reverse a prior in-place conversion: move every file out of
    <output_dir>/backup-audio back to <input_dir>, overwriting whatever
    converted file is currently there. Recurses into subdirectories of
    backup-audio automatically and recreates the same relative structure
    under input_dir.
    """
    backup_dir = output_dir / "backup-audio"

    if not backup_dir.is_dir():
        print(c(f"Error: no backup folder found at {backup_dir}", C.RED))
        sys.exit(1)

    backed_up_files = sorted(backup_dir.rglob("*.mkv"), key=lambda p: str(p.relative_to(backup_dir)))
    if not backed_up_files:
        print(c(f"No files found in {backup_dir}", C.YELLOW))
        sys.exit(0)

    print(c(f"Found {len(backed_up_files)} backed-up file(s) in {backup_dir}", C.CYAN))
    for f in backed_up_files:
        rel = f.relative_to(backup_dir)
        target = input_dir / rel
        overwrite_note = " (will overwrite current file)" if target.exists() else ""
        print(f"  {rel}{overwrite_note}")

    if dry_run:
        print()
        print(c("[DRY RUN] No files were restored.", C.GRAY))
        return

    if not assume_yes:
        answer = input(c(f"\nRestore {len(backed_up_files)} file(s), overwriting the converted versions? [y/N] ", C.YELLOW))
        if answer.strip().lower() not in ("y", "yes"):
            print(c("Aborted, nothing was restored.", C.YELLOW))
            return

    errors = []
    restored_count = 0
    for f in backed_up_files:
        rel = f.relative_to(backup_dir)
        target = input_dir / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
            restored_count += 1
        except OSError as e:
            errors.append(f"{rel}: failed to restore: {e}")

    # Clean up now-empty subdirectories, deepest first, then backup_dir itself.
    for d in sorted((p for p in backup_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    try:
        backup_dir.rmdir()
    except OSError:
        pass

    print()
    print(c("===== Summary =====", C.BOLD))
    print(f"Restored: {restored_count}")

    if errors:
        print()
        print(c("===== ERRORS =====", C.BOLD + C.RED))
        for e in errors:
            print(c(f"  \u2717 {e}", C.RED))
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batch convert FLAC audio in MKV files to Opus (keeps all other tracks).",
    )
    parser.add_argument("-i", "--input-directory", dest="input_dir", default=".", help="Directory containing .mkv files (default: current directory)")
    parser.add_argument("-o", "--output-directory", dest="output_dir", default=".", help="Output directory (default: current directory)")
    parser.add_argument("-r", "--recursive", action="store_true", help="Also search subdirectories for .mkv files")
    parser.add_argument(
        "--quality", choices=["high", "medium"], default="high",
        help="Bitrate tier: high = 192k/2.0, 224k/2.1, 288k/3.0, 448k/5.1, 510k/7.1; "
             "medium = 160k/2.0, 192k/2.1, 256k/3.0, 384k/5.1, 448k/7.1 (default: high)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without converting/restoring anything")
    parser.add_argument("--rename", action="store_true", help='Always set the audio track title to "{Language} {Layout}" (e.g. "English 2.0"), replacing any existing title')
    parser.add_argument("--restore", action="store_true", help="Restore original files from backup-audio/, overwriting the converted versions")
    parser.add_argument("-y", "--yes", action="store_true", help="Don't prompt for confirmation when restoring")
    parser.add_argument(
        "--three-channel-layout", choices=["2.1", "3.0"], default=None,
        help="Required if any 3-channel lossless track is present: '2.1' for L/R/LFE, '3.0' for L/R/C",
    )
    parser.add_argument(
        "--four-channel-layout", choices=["3.1"], default=None,
        help="Required if any 4-channel lossless track is present: '3.1' for L/R/C/LFE",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)

    if not input_dir.is_dir():
        print(c(f"Error: input directory not found: {input_dir}", C.RED))
        sys.exit(1)

    if args.restore:
        restore_backup(input_dir, output_dir, args.dry_run, args.yes)
        return

    same_dir = input_dir.resolve() == output_dir.resolve()
    backup_dir = (output_dir / "backup-audio") if same_dir else None

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print(c("Error: ffmpeg/ffprobe not found on PATH", C.RED))
        sys.exit(1)

    warnings = []

    ffmpeg_major_version = get_ffmpeg_major_version()
    if ffmpeg_major_version is not None and ffmpeg_major_version >= 9:
        warnings.append(
            f"Detected ffmpeg {ffmpeg_major_version}.x. ffmpeg 9.0.1 was confirmed to produce "
            "files where multiple simultaneous libopus-encoded tracks decode fine via ffmpeg "
            "itself (e.g. volumedetect) but play silent in real players -- affects any file "
            "converting 2+ audio tracks at once. If converted files play silent, downgrade to "
            "ffmpeg 8.x (e.g. `brew install ffmpeg@8`) until this is fixed upstream."
        )

    if shutil.which("mkvpropedit") is None:
        warnings.append(
            "mkvpropedit not found -- converted files will keep a stale bitrate tag "
            "from the source track (MediaInfo will show the old bitrate even though "
            "the audio itself was re-encoded correctly). Install mkvtoolnix to fix this."
        )

    mkv_pairs = collect_mkv_files(input_dir, output_dir, same_dir, args.recursive)
    if not mkv_pairs:
        print(c(f"No .mkv files found in {input_dir}", C.YELLOW))
        sys.exit(0)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        if same_dir:
            backup_dir.mkdir(parents=True, exist_ok=True)
            print(c(f"Moving {len(mkv_pairs)} file(s) into {backup_dir}/ ...", C.GRAY))
            moved_pairs = []
            for f, rel in mkv_pairs:
                dest = backup_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(dest))
                moved_pairs.append((dest, rel))
            mkv_pairs = moved_pairs  # now point at their backed-up location
            print()

    errors = []
    converted_count = 0
    skipped_count = 0

    for f, rel in mkv_pairs:
        # f's current location: original spot for separate dirs, or
        # backup_dir for same-dir mode (already moved above). rel is the
        # path relative to input_dir, used to mirror subdirectory
        # structure into both the output and backup locations.
        display_name = str(rel)
        out_path = output_dir / rel
        original_path = input_dir / rel

        # For separate in/out dirs, a pre-existing output means this file
        # was already handled by a prior run.
        if not args.dry_run and not same_dir and out_path.exists():
            print(c(f"Skipping (already converted): {display_name}", C.GRAY))
            skipped_count += 1
            continue

        try:
            streams = ffprobe_streams(f)
        except Exception as e:
            errors.append(f"{display_name}: ffprobe failed: {e}")
            if same_dir and not args.dry_run:
                # Restore so a bad file isn't left stranded in backup only.
                shutil.move(str(f), str(original_path))
            continue

        plan = plan_audio_tracks(streams, args.quality, args.three_channel_layout, args.four_channel_layout)
        print_plan(display_name, plan, args.dry_run, args.rename)

        blocked = [e for e in plan if e["action"] == "blocked"]
        if blocked:
            for e in blocked:
                errors.append(f"{display_name}: audio[{e['audio_index']}]: {e['reason']}")
            if same_dir and not args.dry_run:
                shutil.move(str(f), str(original_path))
            continue

        needs_conversion = any(e["action"] == "convert" for e in plan)
        if not needs_conversion:
            print(c("  Nothing to convert here, skipping", C.GRAY))
            skipped_count += 1
            if same_dir and not args.dry_run:
                # No replacement is being written, so put it back where it was.
                shutil.move(str(f), str(original_path))
            continue

        if args.dry_run:
            if same_dir:
                print(c(f"  Would move original -> {backup_dir / rel}, write result to {out_path}", C.GRAY))
            continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_ffmpeg_cmd(f, out_path, plan, streams, args.rename)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            errors.append(f"{display_name}: ffmpeg failed: {result.stderr.strip()[-500:]}")
            if out_path.exists():
                out_path.unlink()
            if same_dir:
                # Restore the original so a failed run doesn't leave the
                # file missing from its expected location.
                shutil.move(str(f), str(original_path))
            continue

        refresh_track_stats(out_path, warnings)

        print(c(f"  Done -> {out_path}", C.BOLD + C.GREEN))
        converted_count += 1

    print()
    print(c("===== Summary =====", C.BOLD))
    if args.dry_run:
        print(f"Files scanned: {len(mkv_pairs)}")
    else:
        print(f"Converted: {converted_count}")
        print(f"Skipped (already done): {skipped_count}")

    if warnings:
        print()
        print(c("===== Warnings =====", C.BOLD + C.YELLOW))
        for w in warnings:
            print(c(f"  ! {w}", C.YELLOW))

    if errors:
        print()
        print(c("===== ERRORS =====", C.BOLD + C.RED))
        for e in errors:
            print(c(f"  \u2717 {e}", C.RED))
        sys.exit(1)


if __name__ == "__main__":
    main()
