#!/usr/bin/env python3
"""
mkv_track_cleaner.py

Strips unwanted audio/subtitle tracks from a folder of MKV files and sets
sensible default-track flags, using mkvmerge (from MKVToolNix).

REQUIREMENTS:
    MKVToolNix must be installed and `mkvmerge` must be on your PATH.
    https://mkvtoolnix.download/

    NOTE ON MKVMERGE VERSION:
    This script uses the `--default-track-flag` option, available in
    reasonably recent MKVToolNix releases (v55+). If you're on an older
    version and it errors out, replace `--default-track-flag` with
    `--default-track` in the build_command() function below.

RULES APPLIED TO EACH FILE:
    Audio:
        - Keep only English (eng) audio tracks plus whichever other audio
          language pairs with the -l you chose: Japanese (jpn) for -l eng
          or -l jap, Chinese (chi/zho) for -l cn, German (ger/deu) for
          -l ger. All other audio languages are removed.
        - If multiple tracks exist for a kept language, --audio-include /
          --audio-exclude can narrow which of them survive (see below).
        - If -l eng: the (first remaining) English audio track is set default.
        - If -l jap: the (first remaining) Japanese audio track is set default.
        - If -l cn: the (first remaining) Chinese audio track is set default.
        - If -l ger: the (first remaining) German audio track is set default.

    Subtitles:
        - Keep only English (eng) subtitle tracks; all others are removed
          (including Japanese/Chinese/German subs, regardless of -l).
        - If multiple English subtitle tracks exist, --sub-include /
          --sub-exclude can narrow which of them survive (see below).
        - If -l eng: if a "signs" track exists (track name contains "sign"
          or "forced", case-insensitive), it is set as the ONLY default
          subtitle.
        - If -l jap, -l cn, or -l ger: if a non-signs English track exists
          and isn't already default, it is set as the ONLY default subtitle.
        - Pass --no-default to skip all of the above entirely: every kept
          subtitle track's default flag is explicitly set to no (overriding
          whatever the source file had), regardless of language mode.
        - Forced flag, by default (neither --forced nor --no-forced given):
          decided per subtitle track, independent of which one ended up
          default and independent of -l -- any kept track whose name
          contains "sign" or "forced" gets forced=yes, every other kept
          track gets forced=no. (Signs tracks are conventionally meant to
          be forced; full dialogue tracks aren't.) This always runs and
          always shows up in --dry-run output; it's never silently skipped.
        - --forced overrides that: instead, only the one subtitle track
          that ended up default (per the rules above) is forced=yes, and
          every other kept subtitle track is forced=no.
        - --no-forced overrides it the other way: every kept subtitle
          track is forced=no, full stop.

    Include/exclude filters (all optional, case-insensitive substring match
    against each track's name):
        - Filters are applied per language group, after the language
          keep/remove decision above, so they only ever narrow down within
          eng or jpn audio, or eng subtitles -- they can't pull in a track
          of an otherwise-excluded language.
        - --audio-exclude / --sub-exclude: drop any track whose name
          contains one of the given keywords.
        - --audio-include / --sub-include: from what's left after exclude
          filtering, keep only tracks whose name contains one of the given
          keywords. If that would remove every remaining track for a
          language group, the include filter is ignored for that group
          (with a note printed) rather than silently dropping the language
          entirely.
        - Each flag can be passed multiple times, and/or given a
          comma-separated list, e.g. --audio-exclude commentary,description
          or --audio-exclude commentary --audio-exclude description.
        - "commentary" is excluded by default for both audio and subtitle
          tracks, as if --audio-exclude commentary and --sub-exclude
          commentary had been passed automatically. Pass --include-commentary
          to disable this default (any --audio-exclude / --sub-exclude
          values you do pass still apply on top of that).

    Language overrides (for tracks with incorrect language metadata):
        - Some releases mislabel a track's language (e.g. an audio track
          named "English 2.0" tagged as jpn). --audio-lang-override and
          --sub-lang-override let you correct this by name, in the form
          KEYWORD=LANG (LANG is eng/en/english, jpn/jp/ja/japanese,
          chi/zho/zh/cn/chinese, or ger/deu/de/german),
          e.g. --audio-lang-override "English 2.0=eng".
        - Matching is a case-insensitive substring check against the
          track's name, same as the include/exclude filters. Repeatable.
        - The override is applied before every other decision in this
          script (language keep/remove, include/exclude filtering, default
          selection), so an overridden track is treated exactly as if its
          metadata had been correct all along. The corrected language is
          also written into the output file itself (via mkvmerge's
          --language option), so the mislabeled metadata doesn't just get
          worked around internally -- it gets actually fixed.
        - In --dry-run output, an overridden track's line shows the
          corrected language plus a "[was <original>]" note.
        - Even without any override flag, the script prints a WARNING
          (on every run, not just --dry-run) whenever a track's name
          plainly says "English", "Japanese", "Chinese", or "German" but
          its language metadata disagrees -- e.g. a track named
          "English 2.0" tagged jpn. This warning is purely informational:
          nothing is changed unless you add the matching
          --audio-lang-override / --sub-lang-override.

USAGE:
    python mkv_track_cleaner.py -l eng [-i INPUT_DIR] [-o OUTPUT_DIR] [--no-forced]
    python mkv_track_cleaner.py -l jap [-i INPUT_DIR] [-o OUTPUT_DIR] [--forced]
    python mkv_track_cleaner.py -l cn [-i INPUT_DIR] [-o OUTPUT_DIR]
    python mkv_track_cleaner.py -l ger [-i INPUT_DIR] [-o OUTPUT_DIR]
    python mkv_track_cleaner.py -l eng --audio-exclude commentary --sub-include signs
    python mkv_track_cleaner.py -l eng --audio-lang-override "English 2.0=eng"
    python mkv_track_cleaner.py -l eng -r -i INPUT_DIR -o OUTPUT_DIR
    python mkv_track_cleaner.py -l eng -f /path/to/single_episode.mkv -o OUTPUT_DIR

    -i / --input-dir     Directory containing input .mkv files (default: .).
                          Ignored if -f/--file is given.
    -f / --file           Process a single specific .mkv file instead of
                          scanning a directory. Overrides -i/--input-dir and
                          -r/--recursive entirely; -o/--output-dir still
                          applies, and same-input/output-dir detection (plus
                          the 'backup'/--restore machinery) is based on the
                          file's own containing folder, exactly as if
                          -i pointed at just that one file's directory.
                          Cannot be combined with --restore.
    -r / --recursive      Also search subdirectories of --input-dir. Output
                          files mirror the same relative subdirectory layout
                          under --output-dir (e.g. INPUT_DIR/S1/ep01.mkv ->
                          OUTPUT_DIR/S1/ep01.mkv). The 'backup' directory,
                          and --output-dir itself if it's nested inside
                          --input-dir, are always excluded from the scan --
                          so re-running the same command doesn't reprocess
                          your own output or a previous backup. Ignored if
                          -f/--file is given.
    -l / --language       'eng', 'jap'/'jpn' (both accepted, same thing),
                          'cn', or 'ger'. eng/jap both keep English+Japanese
                          dual audio; cn keeps English+Chinese; ger keeps
                          English+German. Default: 'eng'. Ignored by
                          --restore.
    -o / --output-dir    Directory to write processed files (default: current
                          directory, same as --input-dir's default).
    --dry-run            Show what would be done for each file without
                          actually running mkvmerge or writing any output.
    --forced               Mark just the resulting default subtitle track as
                          "forced" (overrides the per-track default below).
    --no-forced           Turn forced off entirely for every subtitle track
                          (overrides the per-track default below).
    --no-default          Force every kept subtitle track's default flag to
                          "no" (no subtitle track will be default at all).
    --audio-include KW    Keep only kept-language audio tracks matching KW
                          (repeatable / comma-separated).
    --audio-exclude KW    Drop audio tracks matching KW (repeatable /
                          comma-separated).
    --sub-include KW      Keep only English subtitle tracks matching KW
                          (repeatable / comma-separated).
    --sub-exclude KW      Drop English subtitle tracks matching KW
                          (repeatable / comma-separated).
    --audio-lang-override KEYWORD=LANG
                          Treat audio tracks matching KEYWORD as language
                          LANG, overriding their metadata (repeatable).
    --sub-lang-override KEYWORD=LANG
                          Treat subtitle tracks matching KEYWORD as language
                          LANG, overriding their metadata (repeatable).
    --include-commentary  Do NOT auto-exclude commentary tracks (excluded
                          by default; see the include/exclude section above).
    --no-color            Disable colored output (also auto-disabled when
                          not attached to a terminal, or when the NO_COLOR
                          environment variable is set).
    --stop-after-video-ends
                          Pass mkvmerge's own --stop-after-video-ends
                          straight through on every merge (closes the
                          output once the video track ends, discarding any
                          queued audio/subtitle past that point).
    --remove-images        Strip image attachments (cover art, folder.jpg,
                          thumbnails, etc.). Fonts and any other non-image
                          attachment are always left alone, since removing
                          fonts breaks subtitle rendering. Off by default;
                          without it, --dry-run and normal runs alike print
                          a NOTE (never an error) if image attachments are
                          found, so you know they're there.

    NOTE ON MKVMERGE VERSION (forced flag):
    This uses `--forced-display-flag`, the modern option name (MKVToolNix
    v50+). On older versions, replace it with `--forced-track` in
    build_command() below.

SAME INPUT/OUTPUT DIRECTORY:
    Since the output directory now also defaults to the current directory,
    input and output will often be the same. mkvmerge can't safely read and
    write the same file at once, so if the (resolved) input and output
    directories match, the script first moves all matched .mkv files into a
    "backup" subdirectory of the input directory, then reads from there and
    writes the newly processed files back out under their original names.

    If that "backup" subdirectory already exists, the script raises an
    error and stops rather than risk overwriting or mixing in a previous
    backup. In --dry-run mode, this check still runs (and still errors if
    "backup" already exists), but no files are actually moved.

    Under -r/--recursive, files are moved into "backup" preserving their
    original relative subdirectory structure (e.g. INPUT_DIR/S1/ep01.mkv ->
    INPUT_DIR/backup/S1/ep01.mkv), and --restore (below) reverses that the
    same way, cleaning up any now-empty subdirectories it leaves behind.

    To undo this later -- e.g. after deciding the processed files weren't
    right -- run with --restore (see below).

RESTORING A BACKUP:
    python mkv_track_cleaner.py -i INPUT_DIR --restore [--dry-run] [--yes]

    Moves every file out of INPUT_DIR/backup back into INPUT_DIR under its
    original name, then removes the now-empty backup directory. This is
    the exact inverse of the same-input/output-directory backup above.

    --restore ignores -l/--language, -o/--output-dir, and every
    track-selection flag -- it only touches file locations, not track
    contents.

    Before actually moving anything, it prompts once for confirmation --
    "Restore N file(s), overwriting the converted versions? [y/N]" --
    since restoring overwrites any processed output files sitting at the
    same paths. Anything other than y/yes (including just pressing enter)
    cancels without touching any files. Pass -y/--yes to skip the prompt
    (e.g. for scripted/non-interactive use). --dry-run never prompts,
    since nothing is touched either way.

END-OF-RUN SUMMARY:
    After all files are processed -- in both --dry-run and normal mode --
    the script prints a short "Summary" block: how many files were found,
    how many succeeded, and (if any) which ones failed and why. This is
    separate from, and printed before, the more detailed Track Layout
    Summary described below.

TRACK LAYOUT SUMMARY (--dry-run only):
    After processing all files, --dry-run prints a "Track Layout Summary"
    describing each file's *resulting* track layout (the kept audio and
    subtitle tracks, their languages, names, and default/forced flags --
    i.e. what the output would actually look like).

    If every file has the same layout, it just confirms that. If layouts
    differ, it reports the most common ("standard") layout and then lists
    every file that deviates from it, along with its own layout -- so you
    can quickly spot the one episode in a series with an extra commentary
    track, a missing subtitle track, differently-named tracks, etc. before
    running for real.
"""

import argparse
import collections
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Color output
# ---------------------------------------------------------------------------
# USE_COLOR is resolved once in main() based on terminal detection, --no-color,
# and the NO_COLOR env var convention (https://no-color.org/). All the c_*()
# helpers below read it at call time, so it just needs to be set before any
# output happens.

USE_COLOR = False


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"
    ORANGE = "\033[38;5;208m"  # 256-color; warmer/more orange than basic YELLOW
    WHITE = "\033[97m"  # bright white


def _colorize(text, *codes):
    if not USE_COLOR or not text:
        return text
    return "".join(codes) + str(text) + _Ansi.RESET


def c_keep(text):      return _colorize(text, _Ansi.GREEN, _Ansi.BOLD)
def c_remove(text):    return _colorize(text, _Ansi.RED, _Ansi.BOLD)
def c_yes(text):       return _colorize(text, _Ansi.GREEN)
def c_no(text):        return _colorize(text, _Ansi.GRAY)
def c_red(text):       return _colorize(text, _Ansi.RED)
def c_warn(text):      return _colorize(text, _Ansi.ORANGE)
def c_note(text):      return _colorize(text, _Ansi.YELLOW)
def c_dryrun(text):    return text  # plain, matches the default (uncolored) terminal text used for track detail lines
def c_err(text):       return _colorize(text, _Ansi.RED, _Ansi.BOLD)
def c_header(text):    return _colorize(text, _Ansi.CYAN, _Ansi.BOLD)
def c_banner(text):    return _colorize(text, _Ansi.MAGENTA, _Ansi.BOLD)
def c_dim(text):       return _colorize(text, _Ansi.DIM)
def c_override(text):  return _colorize(text, _Ansi.YELLOW, _Ansi.BOLD)
def c_success(text):   return _colorize(text, _Ansi.GREEN, _Ansi.BOLD)


def status_label(value: bool, changed: bool) -> str:
    """
    Color a yes/no status: green for a value that changed to yes, red for
    a value that changed to no, and dim/neutral if it didn't change at all.
    """
    text = "yes" if value else "no"
    if not changed:
        return c_dim(text)
    return c_yes(text) if value else c_red(text)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Strip unwanted audio/subtitle tracks from MKV files and set defaults."
    )
    parser.add_argument(
        "-i", "--input-dir", default=".",
        help="Directory containing input MKV files (default: current directory). "
             "Ignored if -f/--file is given."
    )
    parser.add_argument(
        "-f", "--file", default=None,
        help="Process a single specific .mkv file instead of scanning a directory. "
             "Overrides -i/--input-dir and -r/--recursive entirely; -o/--output-dir still "
             "applies (the file's own directory is used for same-input/output-dir detection "
             "and the 'backup'/--restore machinery, exactly as if -i pointed at just that "
             "one file's folder)."
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="Also search subdirectories of --input-dir for .mkv files. Output files "
             "preserve the same relative subdirectory structure under --output-dir. "
             "The 'backup' directory (and --output-dir itself, if it's nested inside "
             "--input-dir) are always skipped during the scan. Ignored if -f/--file is given."
    )
    parser.add_argument(
        "-l", "--language", default="eng", choices=["eng", "jap", "jpn", "cn", "ger"],
        help="Preferred language: 'eng', 'jap'/'jpn' (both accepted, dual English+Japanese "
             "audio), 'cn' (dual English+Chinese audio), or 'ger' (dual English+German "
             "audio). Default: 'eng'."
    )
    parser.add_argument(
        "-o", "--output-dir", default=".",
        help="Directory to write processed files (default: current directory)"
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="Restore files from --input-dir's 'backup' subdirectory (created when input "
             "and output directories were the same) back into --input-dir, then remove the "
             "now-empty backup directory. Ignores -l/--language, -o/--output-dir, and all "
             "track-selection flags. Respects --dry-run. Prompts for confirmation unless "
             "--yes is also given."
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the confirmation prompt for --restore (e.g. for scripted/non-interactive use)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be done for each file, without running mkvmerge or writing output"
    )
    parser.add_argument(
        "--forced", action="store_true", dest="forced", default=None,
        help="Mark the resulting default subtitle track as 'forced' (and no other kept "
             "subtitle track). By default (neither this nor --no-forced given), forced is "
             "instead decided per-track: signs tracks get forced=yes, everything else "
             "forced=no, regardless of -l. Pass this to override that with a single "
             "forced=yes on just the chosen default track."
    )
    parser.add_argument(
        "--no-forced", action="store_false", dest="forced", default=None,
        help="Do NOT mark any subtitle track as 'forced'. By default (neither this nor "
             "--forced given), forced is instead decided per-track: signs tracks get "
             "forced=yes, everything else forced=no, regardless of -l. Pass this to turn "
             "forced off entirely instead."
    )
    parser.add_argument(
        "--audio-include", action="append", default=None,
        help="Keep only audio tracks (within a kept language) whose name matches this keyword. "
             "Repeatable, or comma-separated (e.g. --audio-include commentary,director)"
    )
    parser.add_argument(
        "--audio-exclude", action="append", default=None,
        help="Drop audio tracks whose name matches this keyword. "
             "Repeatable, or comma-separated (e.g. --audio-exclude commentary,description)"
    )
    parser.add_argument(
        "--sub-include", action="append", default=None,
        help="Keep only English subtitle tracks whose name matches this keyword. "
             "Repeatable, or comma-separated (e.g. --sub-include signs,full)"
    )
    parser.add_argument(
        "--sub-exclude", action="append", default=None,
        help="Drop English subtitle tracks whose name matches this keyword. "
             "Repeatable, or comma-separated (e.g. --sub-exclude commentary)"
    )
    parser.add_argument(
        "--audio-lang-override", action="append", default=None, metavar="KEYWORD=LANG",
        help="Treat any audio track whose name contains KEYWORD as language LANG "
             "(eng/en/english, jpn/jp/ja/japanese, chi/zho/zh/cn/chinese, or "
             "ger/deu/de/german), overriding its (possibly wrong) language metadata. "
             "Repeatable, e.g. --audio-lang-override 'English 2.0=eng'"
    )
    parser.add_argument(
        "--sub-lang-override", action="append", default=None, metavar="KEYWORD=LANG",
        help="Treat any subtitle track whose name contains KEYWORD as language LANG "
             "(eng/en/english, jpn/jp/ja/japanese, chi/zho/zh/cn/chinese, or "
             "ger/deu/de/german), overriding its (possibly wrong) language metadata. "
             "Repeatable, e.g. --sub-lang-override 'Full Subtitles=eng'"
    )
    parser.add_argument(
        "--include-commentary", action="store_true",
        help="Do NOT auto-exclude commentary tracks. By default, audio/subtitle tracks "
             "whose name contains 'commentary' are excluded automatically, as if "
             "'commentary' had been passed to --audio-exclude and --sub-exclude; "
             "this flag disables that default."
    )
    parser.add_argument(
        "--no-default", action="store_true",
        help="Force every kept subtitle track's default flag to 'no', skipping the "
             "normal signs-track default-selection logic entirely (no subtitle track "
             "will be marked default). Does not affect audio default selection."
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable colored output (also auto-disabled when not attached to a "
             "terminal, or when the NO_COLOR environment variable is set)"
    )
    parser.add_argument(
        "--stop-after-video-ends", action="store_true",
        help="Pass mkvmerge's --stop-after-video-ends through on every merge: the output "
             "file is closed as soon as the video track ends, discarding any queued audio/"
             "subtitle packets past that point (useful when an audio track runs longer than "
             "the video)."
    )
    parser.add_argument(
        "--remove-images", action="store_true",
        help="Strip image attachments (cover art, folder.jpg, thumbnails, etc.) from the "
             "output. Non-image attachments -- fonts, in particular -- are always left "
             "alone, since removing fonts would break subtitle rendering. Off by default."
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_track_info(mkv_path: Path) -> dict:
    """Run `mkvmerge -J` on a file and return the parsed JSON."""
    result = subprocess.run(
        ["mkvmerge", "-J", str(mkv_path)],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def get_lang(track: dict) -> str:
    """Return a lowercase language code for a track, preferring the IETF tag."""
    props = track.get("properties", {}) or {}
    lang = props.get("language_ietf") or props.get("language") or "und"
    return lang.lower()


def is_english(lang: str) -> bool:
    # Handles BCP-47 codes with a region subtag too, e.g. "en-US", "en-GB".
    primary = lang.split("-")[0]
    return primary in ("eng", "en")


def is_japanese(lang: str) -> bool:
    # Handles BCP-47 codes with a region subtag too, e.g. "ja-JP".
    primary = lang.split("-")[0]
    return primary in ("jpn", "ja", "jp")


def is_chinese(lang: str) -> bool:
    # "chi" is the ISO 639-2/B code, "zho" the 639-2/T code, "zh" the
    # 639-1/BCP-47 code, "cmn" specifically Mandarin. Handles region-tagged
    # variants too, e.g. "zh-CN", "zh-Hans".
    primary = lang.split("-")[0]
    return primary in ("chi", "zho", "zh", "cmn")


def is_german(lang: str) -> bool:
    # "ger" is the ISO 639-2/B code, "deu" the 639-2/T code, "de" the
    # 639-1/BCP-47 code. Handles region-tagged variants too, e.g. "de-DE",
    # "de-AT".
    primary = lang.split("-")[0]
    return primary in ("ger", "deu", "de")


def is_signs_track(track: dict) -> bool:
    """Heuristic: a subtitle track is a 'signs' track if its name mentions
    'sign' or 'forced' (e.g. "Signs", "Signs/Songs", "Forced", "Forced Subs")."""
    name = (track.get("properties", {}) or {}).get("track_name", "") or ""
    name_l = name.lower()
    return "sign" in name_l or "forced" in name_l


_IMAGE_ATTACHMENT_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff")


def is_image_attachment(attachment: dict) -> bool:
    """
    True if an mkvmerge attachment is an image (cover art, folder.jpg,
    thumbnails, etc.) rather than something like a font. Checked by MIME
    type first (reliable when present), falling back to file extension.
    """
    content_type = (attachment.get("content_type") or "").lower()
    if content_type.startswith("image/"):
        return True
    file_name = (attachment.get("file_name") or "").lower()
    return file_name.endswith(_IMAGE_ATTACHMENT_EXTENSIONS)


def attachment_command_args(attachments: list, remove_images: bool) -> list:
    """
    Decide the mkvmerge CLI args (if any) controlling which attachments to
    keep. Only ever touches image attachments (cover art, folder.jpg,
    etc.) -- fonts and anything else are always left alone, since removing
    fonts would break subtitle rendering.

    Returns [] if nothing should change (remove_images is False, or there
    are no image attachments to begin with), ["--no-attachments"] if every
    attachment happens to be an image, or ["--attachments", "id,id,..."]
    naming just the non-image attachments to keep.
    """
    if not remove_images or not attachments:
        return []
    image_ids = [a["id"] for a in attachments if is_image_attachment(a)]
    if not image_ids:
        return []
    keep_ids = [a["id"] for a in attachments if not is_image_attachment(a)]
    if not keep_ids:
        return ["--no-attachments"]
    return ["--attachments", ",".join(str(i) for i in keep_ids)]


def is_currently_default(track: dict) -> bool:
    return bool((track.get("properties", {}) or {}).get("default_track"))


def is_currently_forced(track: dict) -> bool:
    return bool((track.get("properties", {}) or {}).get("forced_track"))


def track_name(track: dict) -> str:
    return (track.get("properties", {}) or {}).get("track_name", "") or ""


def is_within(path: Path, base: Path) -> bool:
    """True if path is base itself, or somewhere inside base."""
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


# Keywords excluded by default from both audio and subtitle tracks, unless
# --include-commentary is passed. Kept as a list (not a single constant) in
# case more default exclusions are ever warranted.
DEFAULT_EXCLUDE_KEYWORDS = ["commentary"]


def normalize_keywords(values) -> list:
    """Flatten a list of --xxx-include/exclude values (each possibly
    comma-separated) into a clean, lowercase list of keywords."""
    if not values:
        return []
    keywords = []
    for value in values:
        for part in value.split(","):
            part = part.strip().lower()
            if part:
                keywords.append(part)
    return keywords


def matches_any_keyword(name: str, keywords: list) -> bool:
    name_l = name.lower()
    return any(kw in name_l for kw in keywords)


_LANG_ALIASES = {
    "eng": "eng", "en": "eng", "english": "eng",
    "jpn": "jpn", "jp": "jpn", "ja": "jpn", "japanese": "jpn",
    "chi": "chi", "zho": "chi", "zh": "chi", "cmn": "chi",
    "cn": "chi", "chn": "chi", "chinese": "chi", "mandarin": "chi",
    "ger": "ger", "deu": "ger", "de": "ger", "german": "ger",
}


def parse_lang_overrides(values) -> list:
    """
    Parse --audio-lang-override / --sub-lang-override values of the form
    'KEYWORD=LANG' into a list of (keyword_lower, lang) tuples, where lang
    is normalized to 'eng', 'jpn', 'chi', or 'ger'.

    Raises ValueError with a human-readable message on malformed input.
    """
    overrides = []
    if not values:
        return overrides
    for value in values:
        if "=" not in value:
            raise ValueError(
                f"invalid --*-lang-override value '{value}': expected format "
                f"KEYWORD=LANG (e.g. 'English 2.0=eng')"
            )
        keyword, _, lang_raw = value.partition("=")
        keyword = keyword.strip().lower()
        lang_raw = lang_raw.strip().lower()
        if not keyword:
            raise ValueError(f"invalid --*-lang-override value '{value}': keyword part is empty")
        lang = _LANG_ALIASES.get(lang_raw)
        if lang is None:
            raise ValueError(
                f"invalid --*-lang-override value '{value}': language must be one of "
                f"eng/en/english, jpn/jp/ja/japanese, chi/zho/zh/cn/chinese, "
                f"or ger/deu/de/german, got '{lang_raw}'"
            )
        overrides.append((keyword, lang))
    return overrides


def resolve_track_lang(track: dict, overrides: list):
    """
    Determine the effective language bucket for a track ('eng', 'jpn',
    'chi', 'ger', or None for anything else), applying
    --audio-lang-override / --sub-lang-override rules (matched by
    case-insensitive substring against the track's name) before falling
    back to its language metadata.

    Returns (effective, override_from):
      - effective: 'eng', 'jpn', 'chi', 'ger', or None
      - override_from: the track's raw language code if an override
        actually changed the outcome, otherwise None
    """
    def raw_bucket(t):
        raw = get_lang(t)
        if is_english(raw):
            return "eng"
        if is_japanese(raw):
            return "jpn"
        if is_chinese(raw):
            return "chi"
        if is_german(raw):
            return "ger"
        return raw

    name_l = track_name(track).lower()
    for keyword, lang in overrides:
        if keyword in name_l:
            original_bucket = raw_bucket(track)
            override_from = get_lang(track) if original_bucket != lang else None
            return lang, override_from

    bucket = raw_bucket(track)
    return (bucket if bucket in ("eng", "jpn", "chi", "ger") else None), None


def detect_name_lang_hint(name: str):
    """
    Very conservative heuristic: if a track's name explicitly says
    "english", "japanese", "chinese", or "german", return the
    corresponding language bucket. Returns None if the name doesn't
    clearly say any of those.
    """
    name_l = name.lower()
    if "english" in name_l:
        return "eng"
    if "japanese" in name_l:
        return "jpn"
    if "chinese" in name_l:
        return "chi"
    if "german" in name_l:
        return "ger"
    return None


def find_lang_mismatches(tracks: list, overrides: list, flag_name: str) -> list:
    """
    Flag (without changing anything) tracks whose name strongly suggests a
    language that differs from their effective language (metadata, or the
    result of an --xxx-lang-override if one was already given and matched).

    This never applies a correction itself -- it only surfaces a warning
    message so the person running the script can decide whether to add an
    --{flag_name}-lang-override.

    Returns a list of warning message strings (empty if nothing suspicious
    was found).
    """
    warnings = []
    for t in tracks:
        hint = detect_name_lang_hint(track_name(t))
        if hint is None:
            continue
        effective, _ = resolve_track_lang(t, overrides)
        if effective != hint:
            raw = get_lang(t)
            warnings.append(
                f'track id {t["id"]} "{track_name(t)}" is tagged \'{raw}\' but its name suggests '
                f"'{hint}' -- consider --{flag_name}-lang-override \"{track_name(t)}={hint}\" "
                f"(not applied automatically)"
            )
    return warnings


def filter_by_keywords(tracks: list, include_keywords: list, exclude_keywords: list,
                        flag_prefix: str, group_desc: str):
    """
    Apply --xxx-exclude then --xxx-include filtering to a list of tracks
    that already belong to the same language group.

    flag_prefix is used to build the CLI flag names in messages (e.g.
    "audio" -> "--audio-include"). group_desc is a human-readable
    description of this specific group (e.g. "English audio tracks"),
    used only in the include-filter fallback note.

    Returns (kept_tracks, removed) where removed is a list of
    (track, reason) tuples describing why each dropped track was cut, for
    use in dry-run output.
    """
    removed = []

    after_exclude = []
    for t in tracks:
        if exclude_keywords and matches_any_keyword(track_name(t), exclude_keywords):
            removed.append((t, f"excluded by --{flag_prefix}-exclude"))
        else:
            after_exclude.append(t)

    if not include_keywords:
        return after_exclude, removed

    included = [t for t in after_exclude if matches_any_keyword(track_name(t), include_keywords)]
    if included:
        for t in after_exclude:
            if t not in included:
                removed.append((t, f"did not match --{flag_prefix}-include"))
        return included, removed

    if after_exclude:
        print(c_note(f"  NOTE: --{flag_prefix}-include matched none of the {group_desc}; "
                     f"ignoring that filter and keeping all of them instead"))
    return after_exclude, removed


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def pick_audio_default(primary: list, secondary: list, default_ids: set, no_default_ids: set):
    """
    Set the first track in `primary` (the -l language) as the sole audio
    default; every other kept audio track (the rest of `primary` plus all
    of `secondary`) is explicitly set to default=no.
    """
    if primary:
        default_ids.add(primary[0]["id"])
        no_default_ids.update(t["id"] for t in primary[1:])
    no_default_ids.update(t["id"] for t in secondary)


def pick_subtitle_default(candidates: list, keep_subs: list, default_ids: set, no_default_ids: set,
                           prefer_current_default: bool = False):
    """
    Choose the sole default among `keep_subs`, from `candidates` only.

    If `candidates` is empty, every kept subtitle track is explicitly set
    to default=no (matches both "-l eng, no signs track" and "-l jap, no
    non-signs track" -- nothing should be default in either case).

    Otherwise picks the first candidate, unless prefer_current_default is
    set and one of the candidates is already flagged default in the
    source, in which case that one is kept as the choice instead (used for
    -l jap, to avoid reassigning an already-correct dialogue default).
    """
    chosen = None
    if candidates:
        if prefer_current_default:
            already_default = [t for t in candidates if is_currently_default(t)]
            chosen = (already_default[0] if already_default else candidates[0])["id"]
        else:
            chosen = candidates[0]["id"]
    if chosen is not None:
        default_ids.add(chosen)
    no_default_ids.update(t["id"] for t in keep_subs if t["id"] != chosen)


# Maps each -l value to the *other* audio language bucket kept alongside
# English, and a human-readable name for it (used in filter messages).
# "eng" and "jap" both pair with Japanese (the original eng+jpn dual-audio
# behavior); "cn" pairs with Chinese and "ger" pairs with German instead,
# for English+Chinese or English+German dual-audio releases. Adding a new
# -l value just means adding an entry here -- decide_tracks() itself
# doesn't need to change.
SECONDARY_AUDIO_LANG = {
    "eng": ("jpn", "Japanese"),
    "jap": ("jpn", "Japanese"),
    "cn":  ("chi", "Chinese"),
    "ger": ("ger", "German"),
}


def decide_tracks(tracks: list, language: str,
                   audio_include=None, audio_exclude=None,
                   sub_include=None, sub_exclude=None,
                   audio_lang_override=None, sub_lang_override=None,
                   no_default_subs=False):
    """
    Given the list of track dicts from mkvmerge -J, decide:
      - which audio track ids to keep
      - which subtitle track ids to keep
      - which track ids should be forced default=yes
      - which track ids should be forced default=no
      - why any tracks were filtered out by --xxx-include/--xxx-exclude
        (removal_reasons: {track_id: reason_string}), for dry-run reporting
    """
    audio_include = audio_include or []
    audio_exclude = audio_exclude or []
    sub_include = sub_include or []
    sub_exclude = sub_exclude or []
    audio_lang_override = audio_lang_override or []
    sub_lang_override = sub_lang_override or []
    removal_reasons = {}

    audio_tracks = [t for t in tracks if t["type"] == "audio"]
    subtitle_tracks = [t for t in tracks if t["type"] == "subtitles"]

    # ---- Audio: keep only eng + the -l language's paired audio language
    # (after language overrides), then apply include/exclude filters ----
    secondary_bucket, secondary_desc = SECONDARY_AUDIO_LANG[language]

    eng_audio_all = []
    secondary_audio_all = []
    for t in audio_tracks:
        effective, _ = resolve_track_lang(t, audio_lang_override)
        if effective == "eng":
            eng_audio_all.append(t)
        elif effective == secondary_bucket:
            secondary_audio_all.append(t)

    eng_audio, eng_removed = filter_by_keywords(eng_audio_all, audio_include, audio_exclude, "audio", "English audio tracks")
    secondary_audio, secondary_removed = filter_by_keywords(
        secondary_audio_all, audio_include, audio_exclude, "audio", f"{secondary_desc} audio tracks")
    for t, reason in eng_removed + secondary_removed:
        removal_reasons[t["id"]] = reason

    keep_audio = eng_audio + secondary_audio

    default_ids = set()
    no_default_ids = set()

    if language == "eng":
        pick_audio_default(eng_audio, secondary_audio, default_ids, no_default_ids)
    else:  # jap, cn, or ger
        pick_audio_default(secondary_audio, eng_audio, default_ids, no_default_ids)

    # ---- Subtitles: keep only eng (after language overrides), then apply include/exclude filters ----
    eng_subs_all = [t for t in subtitle_tracks if resolve_track_lang(t, sub_lang_override)[0] == "eng"]
    keep_subs, subs_removed = filter_by_keywords(eng_subs_all, sub_include, sub_exclude, "sub", "English subtitle tracks")
    for t, reason in subs_removed:
        removal_reasons[t["id"]] = reason

    signs_subs = [t for t in keep_subs if is_signs_track(t)]
    non_signs_subs = [t for t in keep_subs if not is_signs_track(t)]

    if no_default_subs:
        # --no-default: skip signs-track selection entirely, nothing is default.
        no_default_ids.update(t["id"] for t in keep_subs)
    elif language == "eng":
        pick_subtitle_default(signs_subs, keep_subs, default_ids, no_default_ids)
    else:  # jap, cn, or ger
        pick_subtitle_default(non_signs_subs, keep_subs, default_ids, no_default_ids,
                               prefer_current_default=True)

    # A track can't be both forced default and forced non-default; default wins.
    no_default_ids -= default_ids

    return keep_audio, keep_subs, default_ids, no_default_ids, removal_reasons


def resolve_final_default(tid, default_ids: set, no_default_ids: set, original_default: bool) -> bool:
    """The final default=yes/no state for a track, given the explicit
    override sets from decide_tracks() and its original state in the source."""
    if tid in default_ids:
        return True
    if tid in no_default_ids:
        return False
    return original_default


def resolve_final_forced(track: dict, forced_sub_id, forced_mode) -> bool:
    """
    The final forced=yes/no state for a kept subtitle track.

    forced_mode is a tri-state:
      - None  (neither --forced nor --no-forced given): "auto" -- forced is
        decided per track by name, independent of which track ended up
        default and independent of -l: signs tracks get forced=yes,
        everything else forced=no.
      - True  (--forced given): forced=yes for just the chosen default
        subtitle track (forced_sub_id), forced=no for every other kept sub.
      - False (--no-forced given): forced=no for every kept sub, always.
    """
    if forced_mode is None:
        return is_signs_track(track)
    if forced_mode is False:
        return False
    return forced_sub_id is not None and track["id"] == forced_sub_id


def final_default_sub_id(keep_subs, default_ids, no_default_ids):
    """
    Determine which (if any) kept subtitle track will end up with the
    default flag set, after applying default_ids/no_default_ids on top of
    each track's original default_track state. Returns the track id, or
    None if no kept subtitle track ends up default.
    """
    for t in keep_subs:
        if resolve_final_default(t["id"], default_ids, no_default_ids, is_currently_default(t)):
            return t["id"]
    return None


def build_track_layout_signature(keep_audio, keep_subs, default_ids, forced_sub_id, forced_mode,
                                  audio_lang_override, sub_lang_override):
    """
    Build a compact, hashable description of a file's *resulting* track
    layout (i.e. what will actually end up in the output), for grouping
    files by shape in the end-of-run summary. Two files with the same
    signature have the same languages/names/default-forced flags for their
    kept audio and subtitle tracks, in the same order.
    """
    audio_sig = tuple(
        (resolve_track_lang(t, audio_lang_override)[0], track_name(t), t["id"] in default_ids)
        for t in keep_audio
    )
    subs_sig = tuple(
        (
            resolve_track_lang(t, sub_lang_override)[0],
            track_name(t),
            t["id"] in default_ids,
            resolve_final_forced(t, forced_sub_id, forced_mode),
        )
        for t in keep_subs
    )
    return (audio_sig, subs_sig)


def format_track_layout(sig) -> str:
    """Render a build_track_layout_signature() result as a short human-readable string."""
    audio_sig, subs_sig = sig

    audio_parts = []
    for lang, name, is_default in audio_sig:
        label = f'{lang}:"{name}"' if name else f"{lang}:(no name)"
        if is_default:
            label += " [default]"
        audio_parts.append(label)
    audio_str = ", ".join(audio_parts) if audio_parts else "(none)"

    subs_parts = []
    for lang, name, is_default, is_forced in subs_sig:
        label = f'{lang}:"{name}"' if name else f"{lang}:(no name)"
        tags = []
        if is_default:
            tags.append("default")
        if is_forced:
            tags.append("forced")
        if tags:
            label += " [" + "+".join(tags) + "]"
        subs_parts.append(label)
    subs_str = ", ".join(subs_parts) if subs_parts else "(none)"

    return f"audio=[{audio_str}]  subs=[{subs_str}]"


def build_command(mkv_path: Path, out_path: Path, keep_audio, keep_subs,
                   default_ids, no_default_ids, forced_sub_id=None, forced_mode=None,
                   lang_corrections=None, stop_after_video_ends=False, attachment_args=None):
    audio_ids = [t["id"] for t in keep_audio]
    sub_ids = [t["id"] for t in keep_subs]
    lang_corrections = lang_corrections or {}
    attachment_args = attachment_args or []

    cmd = ["mkvmerge", "-o", str(out_path)]

    if stop_after_video_ends:
        cmd += ["--stop-after-video-ends"]

    cmd += attachment_args

    if audio_ids:
        cmd += ["-a", ",".join(str(i) for i in audio_ids)]
    else:
        cmd += ["-A"]  # strip all audio

    if sub_ids:
        cmd += ["-s", ",".join(str(i) for i in sub_ids)]
    else:
        cmd += ["-S"]  # strip all subtitles

    for tid in sorted(lang_corrections):
        cmd += ["--language", f"{tid}:{lang_corrections[tid]}"]

    for tid in sorted(default_ids):
        cmd += ["--default-track-flag", f"{tid}:yes"]
    for tid in sorted(no_default_ids):
        cmd += ["--default-track-flag", f"{tid}:no"]

    for t in keep_subs:
        final_forced = resolve_final_forced(t, forced_sub_id, forced_mode)
        cmd += ["--forced-display-flag", f"{t['id']}:{'yes' if final_forced else 'no'}"]

    cmd.append(str(mkv_path))
    return cmd


def describe_track(t: dict, default_ids: set, no_default_ids: set, status: str,
                    forced_sub_id=None, forced_mode=None, remove_reason=None,
                    effective_lang=None, override_from=None) -> str:
    props = t.get("properties", {}) or {}
    name = props.get("track_name", "") or ""
    lang = effective_lang or get_lang(t)
    codec = t.get("codec", "")

    if status == "REMOVE":
        default_str = f" ({c_dim(remove_reason)})" if remove_reason else ""
    else:
        original_default = is_currently_default(t)
        final_default = resolve_final_default(t["id"], default_ids, no_default_ids, original_default)
        changed = (final_default != original_default)
        final_label = status_label(final_default, changed)
        if not changed:
            default_str = f"-> default={final_label} {c_dim('(unchanged)')}"
        else:
            default_str = f"-> default={final_label}"

    forced_str = ""
    if status != "REMOVE" and t["type"] == "subtitles":
        original_forced = is_currently_forced(t)
        final_forced = resolve_final_forced(t, forced_sub_id, forced_mode)
        forced_str = f" forced={status_label(final_forced, final_forced != original_forced)}"

    override_str = f" {c_override(f'[was {override_from}]')}" if override_from else ""

    label = f'"{name}"' if name else "(no name)"
    return f'    [id {t["id"]}] {lang}{override_str} {codec} {label} {default_str}{forced_str}'


def process_file(mkv_path: Path, rel_path: Path, output_dir: Path, language: str,
                  dry_run: bool = False, forced_mode=None, no_default_subs: bool = False,
                  audio_include=None, audio_exclude=None,
                  sub_include=None, sub_exclude=None,
                  audio_lang_override=None, sub_lang_override=None,
                  stop_after_video_ends: bool = False, remove_images: bool = False):
    audio_lang_override = audio_lang_override or []
    sub_lang_override = sub_lang_override or []

    info = get_track_info(mkv_path)
    tracks = info.get("tracks", [])
    all_audio = [t for t in tracks if t["type"] == "audio"]
    all_subs = [t for t in tracks if t["type"] == "subtitles"]
    all_attachments = info.get("attachments", [])
    image_attachments = [a for a in all_attachments if is_image_attachment(a)]

    for w in find_lang_mismatches(all_audio, audio_lang_override, "audio"):
        print(c_warn(f"  WARNING: {w}"))
    for w in find_lang_mismatches(all_subs, sub_lang_override, "sub"):
        print(c_warn(f"  WARNING: {w}"))

    keep_audio, keep_subs, default_ids, no_default_ids, removal_reasons = decide_tracks(
        tracks, language,
        audio_include=audio_include, audio_exclude=audio_exclude,
        sub_include=sub_include, sub_exclude=sub_exclude,
        audio_lang_override=audio_lang_override, sub_lang_override=sub_lang_override,
        no_default_subs=no_default_subs,
    )
    forced_sub_id = final_default_sub_id(keep_subs, default_ids, no_default_ids)

    if not keep_audio:
        _, secondary_desc = SECONDARY_AUDIO_LANG[language]
        print(c_warn(f"  WARNING: no English/{secondary_desc} audio tracks found in {rel_path.as_posix()}"))
    if not keep_subs:
        print(c_note(f"  NOTE: no English subtitle tracks found in {rel_path.as_posix()}"))
    if forced_mode is True and forced_sub_id is None and keep_subs:
        print(c_note(f"  NOTE: --forced was given but no default subtitle track "
                     f"was determined for {rel_path.as_posix()}; every subtitle will be forced=no"))
    if image_attachments and not remove_images:
        noun = "image attachment" if len(image_attachments) == 1 else "image attachments"
        names = ", ".join(a.get("file_name", f"id {a['id']}") for a in image_attachments)
        print(c_note(f"  NOTE: {len(image_attachments)} {noun} found in {rel_path.as_posix()} "
                     f"({names}); pass --remove-images to strip them"))

    out_path = output_dir / rel_path

    lang_corrections = {}
    for t in keep_audio:
        effective, override_from = resolve_track_lang(t, audio_lang_override)
        if override_from:
            lang_corrections[t["id"]] = effective
    for t in keep_subs:
        effective, override_from = resolve_track_lang(t, sub_lang_override)
        if override_from:
            lang_corrections[t["id"]] = effective

    attachment_args = attachment_command_args(all_attachments, remove_images)

    cmd = build_command(mkv_path, out_path, keep_audio, keep_subs, default_ids, no_default_ids,
                         forced_sub_id=forced_sub_id, forced_mode=forced_mode,
                         lang_corrections=lang_corrections,
                         stop_after_video_ends=stop_after_video_ends,
                         attachment_args=attachment_args)

    layout_sig = build_track_layout_signature(
        keep_audio, keep_subs, default_ids, forced_sub_id, forced_mode,
        audio_lang_override, sub_lang_override,
    )

    if dry_run:
        keep_audio_ids = {t["id"] for t in keep_audio}
        keep_sub_ids = {t["id"] for t in keep_subs}

        print(c_header("  [DRY RUN] Audio tracks:"))
        for t in all_audio:
            status = "KEEP" if t["id"] in keep_audio_ids else "REMOVE"
            effective_lang, override_from = resolve_track_lang(t, audio_lang_override)
            line = describe_track(t, default_ids, no_default_ids, status,
                                   remove_reason=removal_reasons.get(t["id"]),
                                   effective_lang=effective_lang, override_from=override_from)
            status_disp = c_keep(status) if status == "KEEP" else c_remove(status)
            print(f"  {status_disp}{line}")

        print(c_header("  [DRY RUN] Subtitle tracks:"))
        for t in all_subs:
            status = "KEEP" if t["id"] in keep_sub_ids else "REMOVE"
            effective_lang, override_from = resolve_track_lang(t, sub_lang_override)
            line = describe_track(t, default_ids, no_default_ids, status,
                                   forced_sub_id=forced_sub_id, forced_mode=forced_mode,
                                   remove_reason=removal_reasons.get(t["id"]),
                                   effective_lang=effective_lang, override_from=override_from)
            status_disp = c_keep(status) if status == "KEEP" else c_remove(status)
            print(f"  {status_disp}{line}")

        if all_attachments and remove_images:
            print(c_header("  [DRY RUN] Attachments:"))
            for a in all_attachments:
                is_image = is_image_attachment(a)
                status = "REMOVE" if is_image else "KEEP"
                status_disp = c_keep(status) if status == "KEEP" else c_remove(status)
                kind = "image" if is_image else "other"
                name = a.get("file_name", f"id {a['id']}")
                content_type = a.get("content_type", "")
                print(f"  {status_disp}    [id {a['id']}] {kind} {content_type} \"{name}\"")

        print(c_dim(f"  [DRY RUN] Would write: {out_path}"))
        print(c_dim(f"  [DRY RUN] Command: {' '.join(cmd)}"))
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        print(c_dim(f"  Running: {' '.join(cmd)}"))
        subprocess.run(cmd, check=True)

    return layout_sig


def handle_same_dir_backup(input_dir: Path, mkv_files: list, dry_run: bool) -> list:
    """
    When the (resolved) input and output directories are the same, mkvmerge
    would otherwise need to read and write the same file. To avoid that,
    move all matched .mkv files into a 'backup' subdirectory of input_dir
    first, then read from there while writing output back into input_dir
    under the original filenames.

    Raises FileExistsError if the backup directory already exists, rather
    than risk overwriting or mixing in a previous backup.

    Returns the list of file paths that should actually be read from
    (unchanged in dry-run mode, since nothing is actually moved).
    """
    backup_dir = input_dir / "backup"

    if backup_dir.exists():
        raise FileExistsError(
            f"backup directory already exists: {backup_dir}\n"
            "Input and output directories are the same, so existing files "
            "need to be backed up first, but a backup directory is already "
            "present. Remove/rename it, or move its contents elsewhere, "
            "before running again."
        )

    if dry_run:
        print(c_dryrun(f"  [DRY RUN] Input and output directories are the same: {input_dir}"))
        print(c_dryrun(f"  [DRY RUN] Would create backup dir and move {len(mkv_files)} file(s) into: {backup_dir}"))
        return mkv_files  # nothing actually moved; read from the original location

    print(c_note(f"Input and output directories are the same: {input_dir}"))
    print(c_note(f"Moving {len(mkv_files)} file(s) into backup directory: {backup_dir}"))
    backup_dir.mkdir(parents=True)

    moved = []
    for f in mkv_files:
        dest = backup_dir / f.relative_to(input_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        moved.append(dest)
    return moved


def restore_backup(input_dir: Path, dry_run: bool, confirm: bool = True) -> int:
    """
    The inverse of handle_same_dir_backup(): move every file out of
    input_dir/backup back into input_dir under its original name (silently
    overwriting anything already there, since the whole point of --restore
    is to undo everything unconditionally), then remove the backup
    directory if it's empty afterward.

    Unless confirm=False (e.g. --yes was passed), asks for interactive
    confirmation before actually moving anything -- dry-run mode never
    prompts, since nothing is touched either way.

    Raises FileNotFoundError if there's no backup directory to restore from.

    Returns the number of files that were (or, in dry-run/cancelled mode,
    would have been) restored.
    """
    backup_dir = input_dir / "backup"
    if not backup_dir.is_dir():
        raise FileNotFoundError(f"no backup directory found at: {backup_dir}")

    entries = sorted(backup_dir.rglob("*"))
    files = [e for e in entries if e.is_file()]
    other = [e for e in entries if not e.is_file() and not e.is_dir()]

    if not files:
        print(c_note(f"Backup directory is empty (nothing to restore): {backup_dir}"))
        return 0

    if dry_run:
        print(c_dryrun(f"  [DRY RUN] Would restore {len(files)} file(s) from {backup_dir} back into {input_dir}"))
        if other:
            noun = "entry" if len(other) == 1 else "entries"
            print(c_warn(f"  [DRY RUN] NOTE: {len(other)} non-file {noun} in the backup dir "
                         f"would be left as-is: {', '.join(e.name for e in other)}"))
        return len(files)

    if confirm:
        prompt = c_warn(f"Restore {len(files)} file(s), overwriting the converted versions? [y/N] ")
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print(c_note("Restore cancelled; no files were moved."))
            return 0

    print(c_note(f"Restoring {len(files)} file(s) from {backup_dir} back into {input_dir}"))
    for f in files:
        dest = input_dir / f.relative_to(backup_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))

    # Clean up any now-empty subdirectories left behind (recursive backups
    # can have nested folders), deepest first, then the backup dir itself.
    for dirpath, _, _ in os.walk(backup_dir, topdown=False):
        d = Path(dirpath)
        if d == backup_dir:
            continue
        try:
            d.rmdir()
        except OSError:
            pass  # not empty (e.g. contains a leftover 'other' entry) -- leave it

    if other:
        noun = "entry" if len(other) == 1 else "entries"
        print(c_warn(f"  NOTE: leaving {len(other)} non-file {noun} in place: "
                     f"{', '.join(e.name for e in other)} (backup dir not removed)"))
    else:
        try:
            backup_dir.rmdir()
            print(c_note(f"Removed now-empty backup directory: {backup_dir}"))
        except OSError:
            pass  # not actually empty for some other reason; leave it alone

    return len(files)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    global USE_COLOR

    args = parse_args()

    single_file_path = None
    if args.file:
        single_file_path = Path(args.file).resolve()
        if not single_file_path.is_file():
            print(c_err(f"File does not exist: {single_file_path}"), file=sys.stderr)
            sys.exit(1)
        input_dir = single_file_path.parent
    else:
        input_dir = Path(args.input_dir).resolve()

    output_dir = Path(args.output_dir).resolve()

    USE_COLOR = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")

    if not input_dir.is_dir():
        print(c_err(f"Input directory does not exist: {input_dir}"), file=sys.stderr)
        sys.exit(1)

    if args.restore and single_file_path is not None:
        print(c_err("ERROR: -f/--file cannot be combined with --restore"), file=sys.stderr)
        sys.exit(1)

    if args.restore:
        if args.dry_run:
            print(c_banner("=== DRY RUN: no files will be modified or written ===") + "\n")
        try:
            restore_backup(input_dir, args.dry_run, confirm=not args.yes)
        except FileNotFoundError as e:
            print(c_err(f"ERROR: {e}"), file=sys.stderr)
            sys.exit(1)
        print(c_success("Done."))
        sys.exit(0)

    if args.language == "jpn":
        args.language = "jap"

    # args.forced stays None here when neither --forced nor --no-forced was passed;
    # None means "auto" (signs tracks forced=yes, everything else forced=no,
    # regardless of -l), resolved per-track downstream. An explicit True/False
    # instead applies uniformly to just the chosen default subtitle track.

    audio_include = normalize_keywords(args.audio_include)
    audio_exclude = normalize_keywords(args.audio_exclude)
    sub_include = normalize_keywords(args.sub_include)
    sub_exclude = normalize_keywords(args.sub_exclude)

    if not args.include_commentary:
        for kw in DEFAULT_EXCLUDE_KEYWORDS:
            if kw not in audio_exclude:
                audio_exclude.append(kw)
            if kw not in sub_exclude:
                sub_exclude.append(kw)

    try:
        audio_lang_override = parse_lang_overrides(args.audio_lang_override)
        sub_lang_override = parse_lang_overrides(args.sub_lang_override)
    except ValueError as e:
        print(c_err(f"ERROR: {e}"), file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    if single_file_path is not None:
        if single_file_path.suffix.lower() != ".mkv":
            print(c_note(f"NOTE: {single_file_path.name} does not have a .mkv extension; attempting anyway"))
        mkv_files = [single_file_path]
    else:
        if args.recursive:
            candidates = sorted(input_dir.rglob("*.mkv"))
            backup_dir_path = input_dir / "backup"
            mkv_files = [
                f for f in candidates
                if not is_within(f, backup_dir_path)
                and not (output_dir != input_dir and is_within(f, output_dir))
            ]
        else:
            mkv_files = sorted(input_dir.glob("*.mkv"))

        if not mkv_files:
            where = " (recursively)" if args.recursive else ""
            print(f"No .mkv files found in {input_dir}{where}")
            return

    # Paths relative to input_dir, computed now (before any same-dir backup
    # relocation) so output files always mirror the original input layout
    # even after mkv_files gets pointed at backup/ below.
    rel_paths = [f.relative_to(input_dir) for f in mkv_files]

    if args.dry_run:
        print(c_banner("=== DRY RUN: no files will be modified or written ===") + "\n")

    if audio_include or audio_exclude or sub_include or sub_exclude or audio_lang_override or sub_lang_override:
        print(c_header("Active filters:"))
        if audio_include:
            print(f"  --audio-include: {', '.join(audio_include)}")
        if audio_exclude:
            print(f"  --audio-exclude: {', '.join(audio_exclude)}")
        if sub_include:
            print(f"  --sub-include:   {', '.join(sub_include)}")
        if sub_exclude:
            print(f"  --sub-exclude:   {', '.join(sub_exclude)}")
        if audio_lang_override:
            print(f"  --audio-lang-override: {', '.join(f'{kw}={lang}' for kw, lang in audio_lang_override)}")
        if sub_lang_override:
            print(f"  --sub-lang-override:   {', '.join(f'{kw}={lang}' for kw, lang in sub_lang_override)}")
        print()

    if input_dir == output_dir:
        try:
            mkv_files = handle_same_dir_backup(input_dir, mkv_files, args.dry_run)
        except FileExistsError as e:
            print(c_err(f"ERROR: {e}"), file=sys.stderr)
            sys.exit(1)

    had_error = False
    error_files = []
    layouts = []  # list of (display_name, signature), dry-run only
    for mkv_path, rel_path in zip(mkv_files, rel_paths):
        display_name = rel_path.as_posix()
        print(c_header(f"Processing {display_name}..."))
        try:
            layout_sig = process_file(
                mkv_path, rel_path, output_dir, args.language,
                dry_run=args.dry_run, forced_mode=args.forced, no_default_subs=args.no_default,
                audio_include=audio_include, audio_exclude=audio_exclude,
                sub_include=sub_include, sub_exclude=sub_exclude,
                audio_lang_override=audio_lang_override, sub_lang_override=sub_lang_override,
                stop_after_video_ends=args.stop_after_video_ends,
                remove_images=args.remove_images,
            )
            if args.dry_run:
                layouts.append((display_name, layout_sig))
        except subprocess.CalledProcessError as e:
            had_error = True
            error_files.append(display_name)
            print(c_err(f"  ERROR processing {display_name}: {e}"), file=sys.stderr)
        except json.JSONDecodeError as e:
            had_error = True
            error_files.append(display_name)
            print(c_err(f"  ERROR parsing mkvmerge output for {display_name}: {e}"), file=sys.stderr)

    total = len(mkv_files)
    succeeded = total - len(error_files)
    print()
    print(c_header("=== Summary ==="))
    print(f"  Files processed: {total}")
    print(f"  Succeeded:       {succeeded}")
    if error_files:
        print(c_err(f"  Failed:          {len(error_files)}"))
        for fname in error_files:
            print(c_err(f"    - {fname}"))
    if args.dry_run:
        print("  (dry run -- no files were actually written)")

    if args.dry_run and layouts:
        print()
        print(c_header("=== Track Layout Summary ==="))
        counts = collections.Counter(sig for _, sig in layouts)
        standard_sig, standard_count = counts.most_common(1)[0]
        total_layouts = len(layouts)

        if len(counts) == 1:
            print(f"All {total_layouts} file(s) share the same track layout:")
            print(f"  {format_track_layout(standard_sig)}")
        else:
            print(f"Standard layout ({standard_count}/{total_layouts} files):")
            print(f"  {format_track_layout(standard_sig)}")
            print(c_warn(f"Files that deviate from the standard layout ({total_layouts - standard_count}):"))
            for filename, sig in layouts:
                if sig != standard_sig:
                    print(f"  {c_warn(filename)}")
                    print(f"    {format_track_layout(sig)}")

    print(c_success("Done.") if not had_error else c_err("Done, with errors (see above)."))
    sys.exit(1 if had_error else 0)


if __name__ == "__main__":
    main()
