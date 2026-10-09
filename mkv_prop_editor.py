#!/usr/bin/env python3
"""
mkv_prop_editor.py

Batch-edits audio/subtitle track properties (name, language, default flag,
forced flag) on a folder of MKV files IN PLACE, using mkvpropedit (from
MKVToolNix). Unlike a remux-based tool, mkvpropedit rewrites only the
container metadata -- it does not touch or re-encode the actual track data,
so it's fast and lossless.

REQUIREMENTS:
    MKVToolNix must be installed and `mkvpropedit` (and `mkvmerge`, used
    only to inspect files) must be on your PATH.
    https://mkvtoolnix.download/

    NOTE ON MKVPROPEDIT VERSION (language property):
    By default this script sets the classic ISO 639-2 `language` property.
    Recent MKVToolNix releases (v52+) also support the BCP 47 / IETF
    `language-ietf` property, which newer players increasingly prefer.
    Pass --lang-prop language-ietf to set that property instead (values
    like "en", "en-US", "ja" are valid there; plain 3-letter codes like
    "eng"/"jpn" are still accepted and will be used as-is). You can also
    pass --lang-prop both to set both properties in the same edit.

WHAT THIS SCRIPT DOES:
    You describe one or more edits with repeated --set flags. Each --set
    targets a group of tracks (audio or subtitles) within EVERY matched
    file, selects one specific track within that group per file, and
    assigns it new property values. The same set of --set flags is applied
    across the whole batch, which is the normal case for a folder of
    episodes that all share the same track layout.

    --set TYPE SELECTOR PROP=VALUE [PROP=VALUE ...]

    TYPE is "audio" or "subtitles" (aliases: a / audio, s / sub / subs /
    subtitle / subtitles).

    SELECTOR picks exactly one track of that type within a file:
        N            The Nth track of that type, 1-based, in the order
                      mkvmerge reports them (e.g. "2" = second audio track).
        lang:CODE     The track currently tagged with language CODE (eng,
                      jpn, fre, de, spanish, ... -- same aliasing as the
                      PROP value below). Errors if zero or more than one
                      track of that type currently has that language.
        name:KEYWORD  The track whose current name contains KEYWORD
                      (case-insensitive substring). Errors if zero or more
                      than one track matches.
        id:N          The track with mkvmerge's raw container track id N
                      (as shown by `mkvmerge -J`). Most exact, but ids are
                      generally file-specific, so this selector is mainly
                      useful when running against a single file.

    PROP=VALUE (repeatable, at least one required):
        name=TEXT      Set the track name. name= (empty) DELETES the name
                        property entirely, rather than setting it blank.
        lang=CODE      Set the track's language. Accepts eng/en/english,
                        jpn/jp/ja/japanese, fre/fr/french, ger/de/german,
                        spa/es/spanish, ita/it/italian, por/pt/portuguese,
                        chi/zh/chinese, kor/ko/korean, und/undetermined,
                        or any other 2-3 letter / IETF-style code, which is
                        passed through as-is (lowercased).
        default=BOOL   Set the default-track flag.
        forced=BOOL    Set the forced-track flag.
        BOOL values: yes/no, true/false, on/off, 1/0 (case-insensitive).

    Examples:
        --set audio 1 name="English" lang=eng default=yes
        --set audio 2 lang=jpn default=no
        --set subtitles lang:eng name="Full Subtitles" default=yes forced=no
        --set subtitles name:sign name="Signs & Songs" default=no forced=yes

    Quoting: each PROP=VALUE is a single shell token, so a value containing
    spaces needs to be quoted as part of that token, e.g.:
        --set audio 1 "name=English 5.1"

AUTO-NAMING AUDIO TRACKS (--auto-name-audio):
    Instead of (or alongside) --set, --auto-name-audio renames EVERY audio
    track in every matched file to:

        <Language> <Channel layout> <Codec>

    e.g. "English 5.1 FLAC" or "Japanese 2.0 AAC". Each track's name is
    computed independently from its OWN current language, channel count,
    and codec, so no SELECTOR is needed and it works even if you pass no
    --set at all. Channel counts are shown as a conventional layout label
    (2.0, 5.1, 7.1, ...) -- see the note under BATCH CONSISTENCY CHECK.
    Language display names cover common languages (English, Japanese,
    French, German, Spanish, ...); anything not recognized falls back to
    the raw language code.

    You can combine --auto-name-audio with --set to also change other
    properties (lang=, default=, forced=) on specific tracks in the same
    run. If a --set for a given track includes its own name=, that
    explicit name wins over the auto-generated one for that track only;
    every other audio track is still auto-named.

BATCH CONSISTENCY CHECK (the main safety mechanism):
    Before touching any file, the script probes every matched .mkv file
    with `mkvmerge -J` and, for each file:
        1. Resolves every --set selector against that file's actual
           tracks. A selector that matches zero or more than one track is
           an error for that file.
        2. Records a signature of that file's input track layout (the
           language, name, default flag, forced flag, and -- for audio --
           channel count of every audio and subtitle track, in order).
           Channel counts are shown as a conventional layout label (2.0,
           5.1, 7.1, ...) rather than the raw channel number; mkvmerge only
           reports a channel count, not the actual speaker layout, so this
           is the common-case convention, not a guaranteed-accurate label.

    If every file resolves every selector cleanly AND every file's layout
    signature matches the most common ("standard") one, the batch is
    considered uniform. If not -- a file is missing a track a selector
    expects, a name:/lang: selector matches ambiguously on one file, or a
    file simply has a different number/arrangement of tracks -- the batch
    is flagged as DEVIATING, and the specific problem is printed per file.

    This check always runs and is always shown, in both --dry-run and
    normal mode. In --dry-run mode, nothing is ever written regardless of
    the result. In normal mode, if the batch deviates, the script refuses
    to edit ANY file and exits with an error -- unless --force is passed.

    --force does not change what happens to files whose selectors failed
    to resolve; edits that couldn't be resolved for a given file are still
    skipped for that file specifically (with a warning), since there's
    nothing to act on. --force only lifts the batch-level refusal to
    proceed at all when the overall layout isn't uniform.

BACKUPS:
    Because mkvpropedit edits files in place with no separate output file,
    pass --backup-dir DIR to have the script copy each file into DIR
    (created if needed) before editing it, as a safety net. Skipped in
    --dry-run mode (the copy that would happen is printed instead). If a
    same-named file already exists in the backup dir, the copy is skipped
    (with a note) rather than overwriting a possibly-earlier backup.

USAGE:
    python mkv_prop_editor.py -i INPUT_DIR --list

    python mkv_prop_editor.py -i INPUT_DIR --dry-run \
        --set audio 1 name=English lang=eng default=yes \
        --set audio 2 lang=jpn default=no \
        --set subtitles lang:eng name="Full Subtitles" default=yes forced=no

    python mkv_prop_editor.py -i INPUT_DIR --backup-dir INPUT_DIR/backup \
        --set audio 1 name=English lang=eng default=yes

    python mkv_prop_editor.py -f "path/to/single_episode.mkv" --dry-run \
        --set subtitles 1 name="Full Subtitles" default=yes

    python mkv_prop_editor.py -i INPUT_DIR --dry-run --auto-name-audio

    -i / --input-dir     Directory containing .mkv files to edit (default: .)
    -f / --file           Operate on a single .mkv file instead of scanning a
                          directory. Mutually exclusive with -i/--input-dir.
                          Skips the batch consistency check (nothing to
                          compare against with only one file).
    -r / --recursive     Also search subfolders of --input-dir. Applies to
                          --list as well as editing. A --backup-dir located
                          inside --input-dir is automatically excluded from
                          the scan. No effect with -f/--file.
    --list                 Print the aggregate (most common) track layout
                          across all matched files, then call out any files
                          whose layout differs (outliers), shown individually.
                          Use this first to see what SELECTOR values are
                          available and whether the batch is uniform. Ignores
                          --set/--dry-run/--force/--backup-dir; nothing is
                          ever modified.
    --set                 One track edit; repeatable. See above.
    --auto-name-audio       Rename every audio track to '<Language>
                          <Channel layout> <Codec>'. Works with zero --set
                          flags, or combine with --set for other props.
                          See AUTO-NAMING AUDIO TRACKS above.
    --dry-run             Print what would change, in color, without running
                          mkvpropedit or modifying anything.
    --force               Allow the actual edit to run even if the batch
                          consistency check found the input files aren't
                          uniform. Has no effect in --dry-run mode (which
                          never writes anything anyway).
    --backup-dir DIR      Copy each file here before editing it. Not
                          created or written to in --dry-run mode.
    --lang-prop {language,language-ietf,both}
                          Which mkvpropedit property lang=CODE writes to.
                          Default: language.
    --no-color             Disable colored output (also auto-disabled when
                          not attached to a terminal, or when the NO_COLOR
                          environment variable is set).

END-OF-RUN SUMMARY:
    After processing, the script prints how many files were found, how
    many were actually edited, and how many failed -- in both --dry-run
    and normal mode.
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
    ORANGE = "\033[38;5;208m"


def _colorize(text, *codes):
    if not USE_COLOR or not text:
        return text
    return "".join(codes) + str(text) + _Ansi.RESET


def c_yes(text):       return _colorize(text, _Ansi.GREEN)
def c_no(text):        return _colorize(text, _Ansi.GRAY)
def c_warn(text):      return _colorize(text, _Ansi.ORANGE)
def c_note(text):      return _colorize(text, _Ansi.YELLOW)
def c_err(text):       return _colorize(text, _Ansi.RED, _Ansi.BOLD)
def c_header(text):    return _colorize(text, _Ansi.CYAN, _Ansi.BOLD)
def c_banner(text):    return _colorize(text, _Ansi.MAGENTA, _Ansi.BOLD)
def c_dim(text):       return _colorize(text, _Ansi.DIM)
def c_change(text):    return _colorize(text, _Ansi.YELLOW, _Ansi.BOLD)
def c_success(text):   return _colorize(text, _Ansi.GREEN, _Ansi.BOLD)


def status_label(value: bool, changed: bool) -> str:
    """Color a yes/no status: green for a value that changed to yes, red
    for a value that changed to no, dim/neutral if it didn't change."""
    text = "yes" if value else "no"
    if not changed:
        return c_dim(text)
    return c_yes(text) if value else _colorize(text, _Ansi.RED)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

_HELP_EPILOG = """\
------------------------------------------------------------------------------
QUICK REFERENCE
------------------------------------------------------------------------------
Each --set defines ONE track edit, applied to every matched file:

    --set TYPE SELECTOR PROP=VALUE [PROP=VALUE ...]

  TYPE      audio | subtitles

  SELECTOR  N              Nth track of that type (1-based), e.g. "2"
            lang:CODE      the track currently tagged language CODE, e.g. "lang:eng"
            name:KEYWORD   the track whose current name contains KEYWORD, e.g. "name:signs"
            id:N           the track with mkvmerge container id N, e.g. "id:3"

  PROP      name=TEXT      set the track name  (name=  with nothing after it deletes it)
            lang=CODE      set the language, e.g. lang=eng, lang=jpn, lang=en-US
            default=BOOL   set the default-track flag
            forced=BOOL    set the forced-track flag
            BOOL is yes/no, true/false, on/off, or 1/0

--auto-name-audio renames EVERY audio track in every file to
'<Language> <Channel layout> <Codec>' (e.g. "English 5.1 FLAC"), computed
per track from its own metadata -- no SELECTOR needed, and it works even
with zero --set flags. Combine it with --set to also change other props
(lang=, default=, forced=) on specific tracks in the same run; an explicit
--set name= for a track overrides the auto-generated name for that track.

------------------------------------------------------------------------------
EXAMPLES
------------------------------------------------------------------------------
  # See the batch's aggregate track layout and any outlier files, to figure
  # out your SELECTOR values before writing --set:
  mkv_prop_editor.py -i "Show S01" --list

  # Same, but including every season subfolder under one parent directory:
  mkv_prop_editor.py -i "Show" --recursive --list

  # Rename every audio track to "<Language> <Channels> <Codec>":
  mkv_prop_editor.py -i "Show S01" --dry-run --auto-name-audio

  # Auto-name audio AND set the default flag on a specific track in one run:
  mkv_prop_editor.py -i "Show S01" --dry-run --auto-name-audio \\
      --set audio lang:eng default=yes

  # Edit just one file (no directory scanning, no batch consistency check):
  mkv_prop_editor.py -f "Show S01E05.mkv" --dry-run \\
      --set subtitles name:sign name="Signs & Songs" forced=yes

  # Preview changes for a folder of episodes, no files touched:
  mkv_prop_editor.py -i "Show S01" --dry-run \\
      --set audio 1 name="Japanese" lang=jpn default=no \\
      --set audio 2 name="English"  lang=eng default=yes \\
      --set subtitles lang:eng name="Full Subtitles" default=yes forced=no

  # Same edits for real, backing up every file first:
  mkv_prop_editor.py -i "Show S01" --backup-dir "Show S01/backup" \\
      --set audio 1 name="Japanese" lang=jpn default=no \\
      --set audio 2 name="English"  lang=eng default=yes

  # Target a track by its current name instead of position, and force the
  # run through even though the batch consistency check found deviations:
  mkv_prop_editor.py -i "Show S01" --force \\
      --set subtitles name:sign name="Signs & Songs" default=no forced=yes

------------------------------------------------------------------------------
SAFETY CHECK
------------------------------------------------------------------------------
Before writing anything, every file is inspected and every --set selector is
resolved against it. If any file's track layout differs from the others, or
a selector can't be resolved on some file, the run is refused with a report
of what deviates -- unless --force is given. --dry-run always just reports
and never writes, regardless of the outcome.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        prog="mkv_prop_editor.py",
        usage="%(prog)s (-i INPUT_DIR [--recursive] | -f FILE) --list\n"
              "       %(prog)s (-i INPUT_DIR [--recursive] | -f FILE)\n"
              "                          [--set TYPE SELECTOR PROP=VALUE [PROP=VALUE ...]]\n"
              "                          [--set ...] [--auto-name-audio] [--dry-run] [--force]\n"
              "                          [--backup-dir DIR] [--lang-prop {language,language-ietf,both}]\n"
              "                          [--no-color]",
        description="Batch-edit MKV audio/subtitle track name, language, default, and "
                     "forced flags in place with mkvpropedit (no remux). Run with --dry-run "
                     "first to preview changes in color before writing anything.",
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    location_group = parser.add_mutually_exclusive_group()
    location_group.add_argument(
        "-i", "--input-dir", default=".", metavar="DIR",
        help="Directory containing .mkv files to edit (default: current directory)"
    )
    location_group.add_argument(
        "-f", "--file", default=None, metavar="FILE",
        help="Operate on a single .mkv file instead of scanning a directory. "
             "Mutually exclusive with -i/--input-dir. --recursive and "
             "--backup-dir's directory-exclusion logic don't apply, but "
             "--backup-dir itself still works normally."
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true",
        help="Also search subfolders of --input-dir for .mkv files (default: only "
             "the top-level folder). Applies to --list as well as editing. If "
             "--backup-dir points inside --input-dir, it's automatically excluded "
             "from the scan so backup copies are never treated as input files. "
             "No effect with -f/--file."
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Print the aggregate (most common) audio/subtitle track layout "
             "across all matched files -- position, language, name, default/"
             "forced flags, codec -- then call out any files whose layout "
             "differs (OUTLIERS), shown individually. No editing happens. "
             "Use this first to see what SELECTOR values are available and "
             "whether the batch is uniform before writing --set flags. "
             "Ignores --set/--dry-run/--force/--backup-dir/--lang-prop."
    )
    parser.add_argument(
        "--set", dest="set_specs", action="append", nargs="+", default=None,
        metavar="ARG",
        help="Define one track edit: TYPE (audio/subtitles), a SELECTOR "
             "(N, lang:CODE, name:KEYWORD, or id:N), and one or more "
             "PROP=VALUE pairs (name=, lang=, default=, forced=). Repeatable "
             "-- use one --set per track you want to change. See QUICK "
             "REFERENCE and EXAMPLES below."
    )
    parser.add_argument(
        "--auto-name-audio", action="store_true",
        help="Rename every audio track in every matched file to "
             "'<Language> <Channel layout> <Codec>', e.g. 'English 5.1 FLAC' "
             "or 'Japanese 2.0 AAC', computed independently per track from "
             "its own current language/channel-count/codec -- no SELECTOR "
             "needed, and it applies even with zero --set flags. If a --set "
             "already targets a given track and gives it an explicit name=, "
             "that explicit name wins over the auto-generated one for that "
             "track; any other props on that --set (lang=, default=, "
             "forced=) still apply as normal."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview only: print what would change for each file, in color, "
             "without running mkvpropedit or modifying anything. Recommended "
             "before every real run."
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed with the real edit even if the batch consistency check "
             "found files that deviate from each other's track layout, or a "
             "selector that fails to resolve on some files. Any file/edit "
             "that still can't be resolved is skipped individually with a "
             "warning rather than applied incorrectly. No effect with --dry-run."
    )
    parser.add_argument(
        "--backup-dir", default=None, metavar="DIR",
        help="Copy each file here (created if needed) before editing it in "
             "place. Recommended for real runs, since mkvpropedit edits have "
             "no separate output file. Skipped, but reported, in --dry-run mode."
    )
    parser.add_argument(
        "--lang-prop", choices=["language", "language-ietf", "both"], default="language",
        metavar="{language,language-ietf,both}",
        help="Which mkvpropedit property lang=CODE writes to: the classic "
             "ISO 639-2 'language' field, the newer BCP47 'language-ietf' "
             "field, or both at once. Default: language."
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable colored output (also auto-disabled when not attached to "
             "a terminal, or when the NO_COLOR environment variable is set)"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# --set parsing
# ---------------------------------------------------------------------------

_TYPE_ALIASES = {
    "audio": "audio", "a": "audio",
    "subtitles": "subtitles", "subtitle": "subtitles",
    "subs": "subtitles", "sub": "subtitles", "s": "subtitles",
}

_LANG_ALIASES = {
    "eng": "eng", "en": "eng", "english": "eng",
    "jpn": "jpn", "jp": "jpn", "ja": "jpn", "japanese": "jpn",
    "fre": "fre", "fra": "fre", "fr": "fre", "french": "fre",
    "ger": "ger", "deu": "ger", "de": "ger", "german": "ger",
    "spa": "spa", "es": "spa", "spanish": "spa",
    "ita": "ita", "it": "ita", "italian": "ita",
    "por": "por", "pt": "por", "portuguese": "por",
    "chi": "chi", "zho": "chi", "zh": "chi", "chinese": "chi",
    "kor": "kor", "ko": "kor", "korean": "kor",
    "und": "und", "undetermined": "und",
}


def normalize_lang(value: str) -> str:
    """Normalize a language code/name for --set lang=... or a lang:
    selector. Known aliases map to a canonical code; anything else that
    looks like a plausible code/IETF tag is passed through lowercased."""
    v = value.strip().lower()
    if v in _LANG_ALIASES:
        return _LANG_ALIASES[v]
    if v and all(part.isalnum() for part in v.split("-")) and 2 <= len(v.split("-")[0]) <= 3:
        return v
    raise ValueError(f"unrecognized language code/name: '{value}'")


# ISO 639-1 (2-letter) and ISO 639-2 (3-letter) codes -> a human-readable
# English display name, used to build auto-generated audio track names
# (see --auto-name-audio). Not exhaustive; anything not listed here falls
# back to the raw code, title-cased, in lang_display_name() below.
_LANG_DISPLAY_NAMES = {
    "eng": "English", "en": "English",
    "jpn": "Japanese", "ja": "Japanese",
    "fre": "French", "fra": "French", "fr": "French",
    "ger": "German", "deu": "German", "de": "German",
    "spa": "Spanish", "es": "Spanish",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese",
    "kor": "Korean", "ko": "Korean",
    "rus": "Russian", "ru": "Russian",
    "ara": "Arabic", "ar": "Arabic",
    "hin": "Hindi", "hi": "Hindi",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch",
    "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "no": "Norwegian",
    "dan": "Danish", "da": "Danish",
    "fin": "Finnish", "fi": "Finnish",
    "pol": "Polish", "pl": "Polish",
    "tur": "Turkish", "tr": "Turkish",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "ind": "Indonesian", "id": "Indonesian",
    "cze": "Czech", "ces": "Czech", "cs": "Czech",
    "gre": "Greek", "ell": "Greek", "el": "Greek",
    "heb": "Hebrew", "he": "Hebrew",
    "hun": "Hungarian", "hu": "Hungarian",
    "rum": "Romanian", "ron": "Romanian", "ro": "Romanian",
    "ukr": "Ukrainian", "uk": "Ukrainian",
    "und": "Undetermined",
}


def lang_display_name(raw_lang: str) -> str:
    """A human-readable display name for a track's raw language code, used
    to build auto-generated audio track names (e.g. 'eng' -> 'English').
    Handles IETF-style tags with a region (e.g. 'en-US' -> 'English') by
    matching on the base subtag. Falls back to the raw code, title-cased,
    for anything not in the lookup table."""
    base = raw_lang.strip().lower().split("-")[0]
    if base in _LANG_DISPLAY_NAMES:
        return _LANG_DISPLAY_NAMES[base]
    return raw_lang.upper() if len(raw_lang) <= 3 else raw_lang.title()


def compute_auto_audio_name(track: dict) -> str:
    """The --auto-name-audio name for an audio track: '<Language> <Channel
    layout> <Codec>', e.g. 'English 5.1 FLAC'."""
    lang_name = lang_display_name(get_lang(track))
    ch_label = channel_layout_label(get_audio_channels(track))
    codec = track.get("codec") or "?"
    return f"{lang_name} {ch_label} {codec}"


def parse_bool(value: str, prop_name: str) -> bool:
    v = value.strip().lower()
    if v in ("yes", "true", "on", "1"):
        return True
    if v in ("no", "false", "off", "0"):
        return False
    raise ValueError(f"invalid value '{value}' for {prop_name}= (expected yes/no, true/false, on/off, or 1/0)")


class TrackEditSpec:
    """One parsed --set entry: a (type, selector) target plus the property
    changes to apply to whichever single track that selector resolves to,
    per file."""

    def __init__(self, track_type, selector_kind, selector_value, props, raw: str):
        self.track_type = track_type          # "audio" or "subtitles"
        self.selector_kind = selector_kind     # "index" | "lang" | "name" | "id"
        self.selector_value = selector_value
        self.props = props                     # dict: name/language/default/forced -> value
        self.raw = raw                         # original text, for messages

    def describe_selector(self) -> str:
        if self.selector_kind == "index":
            return f"{self.track_type} #{self.selector_value}"
        return f"{self.track_type} {self.selector_kind}:{self.selector_value}"


def parse_selector(token: str):
    """Parse a SELECTOR token into (kind, value). Raises ValueError."""
    if token.isdigit():
        n = int(token)
        if n < 1:
            raise ValueError(f"invalid selector '{token}': index must be 1 or greater")
        return "index", n
    for prefix, kind in (("lang:", "lang"), ("name:", "name"), ("id:", "id")):
        if token.lower().startswith(prefix):
            value = token[len(prefix):]
            if not value:
                raise ValueError(f"invalid selector '{token}': nothing after '{prefix}'")
            if kind == "id":
                if not value.isdigit():
                    raise ValueError(f"invalid selector '{token}': id: must be followed by a number")
                return "id", int(value)
            if kind == "lang":
                return "lang", normalize_lang(value)
            return "name", value
    raise ValueError(
        f"invalid selector '{token}': expected an integer (Nth track of that type), "
        f"lang:CODE, name:KEYWORD, or id:N"
    )


def parse_set_group(tokens: list, group_num: int) -> TrackEditSpec:
    """Parse the tokens following one --set flag into a TrackEditSpec.
    Raises ValueError with a message identifying which --set (1-based,
    in order given on the command line) was malformed."""
    raw = " ".join(tokens)
    if len(tokens) < 3:
        raise ValueError(
            f"--set #{group_num} ('{raw}'): expected TYPE SELECTOR PROP=VALUE "
            f"[PROP=VALUE ...] (at least 3 tokens)"
        )

    type_token, selector_token, *prop_tokens = tokens
    track_type = _TYPE_ALIASES.get(type_token.lower())
    if track_type is None:
        raise ValueError(
            f"--set #{group_num} ('{raw}'): invalid TYPE '{type_token}' "
            f"(expected audio or subtitles)"
        )

    try:
        selector_kind, selector_value = parse_selector(selector_token)
    except ValueError as e:
        raise ValueError(f"--set #{group_num} ('{raw}'): {e}")

    props = {}
    for tok in prop_tokens:
        if "=" not in tok:
            raise ValueError(
                f"--set #{group_num} ('{raw}'): invalid property token '{tok}' "
                f"(expected PROP=VALUE)"
            )
        key, _, value = tok.partition("=")
        key = key.strip().lower()
        if key in ("name", "n"):
            props["name"] = value
        elif key in ("lang", "language", "l"):
            try:
                props["language"] = normalize_lang(value)
            except ValueError as e:
                raise ValueError(f"--set #{group_num} ('{raw}'): {e}")
        elif key in ("default", "def", "d"):
            try:
                props["default"] = parse_bool(value, "default")
            except ValueError as e:
                raise ValueError(f"--set #{group_num} ('{raw}'): {e}")
        elif key in ("forced", "force", "f"):
            try:
                props["forced"] = parse_bool(value, "forced")
            except ValueError as e:
                raise ValueError(f"--set #{group_num} ('{raw}'): {e}")
        else:
            raise ValueError(
                f"--set #{group_num} ('{raw}'): unknown property '{key}' "
                f"(expected name, lang, default, or forced)"
            )

    if not props:
        raise ValueError(f"--set #{group_num} ('{raw}'): at least one PROP=VALUE is required")

    return TrackEditSpec(track_type, selector_kind, selector_value, props, raw)


def parse_all_set_specs(raw_groups) -> list:
    if not raw_groups:
        raise ValueError("at least one --set is required")
    specs = []
    for i, tokens in enumerate(raw_groups, start=1):
        specs.append(parse_set_group(tokens, i))
    return specs


# ---------------------------------------------------------------------------
# mkvmerge inspection helpers
# ---------------------------------------------------------------------------

def get_track_info(mkv_path: Path) -> dict:
    """Run `mkvmerge -J` on a file and return the parsed JSON."""
    result = subprocess.run(
        ["mkvmerge", "-J", str(mkv_path)],
        capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)


def get_lang(track: dict) -> str:
    props = track.get("properties", {}) or {}
    lang = props.get("language_ietf") or props.get("language") or "und"
    return lang.lower()


def track_name(track: dict) -> str:
    return (track.get("properties", {}) or {}).get("track_name", "") or ""


def is_currently_default(track: dict) -> bool:
    return bool((track.get("properties", {}) or {}).get("default_track"))


def is_currently_forced(track: dict) -> bool:
    return bool((track.get("properties", {}) or {}).get("forced_track"))


_CHANNEL_LAYOUT_LABELS = {
    1: "1.0", 2: "2.0", 3: "2.1", 4: "4.0", 5: "5.0",
    6: "5.1", 7: "6.1", 8: "7.1", 10: "7.1.2", 12: "7.1.4",
}


def get_audio_channels(track: dict):
    """Raw channel count for an audio track (from mkvmerge's audio_channels
    property), or None for non-audio tracks / when unknown."""
    if track.get("type") != "audio":
        return None
    return (track.get("properties", {}) or {}).get("audio_channels")


def channel_layout_label(channels) -> str:
    """A conventional 'N.1'-style layout label for a channel count, e.g.
    2 -> '2.0', 6 -> '5.1', 8 -> '7.1'. mkvmerge only reports a channel
    COUNT, not the actual speaker layout, so this is the common-case
    convention rather than a guaranteed-accurate layout (e.g. 6 channels
    is almost always 5.1, but could technically be something else)."""
    if channels is None:
        return "?"
    return _CHANNEL_LAYOUT_LABELS.get(channels, f"{channels}ch")


def lang_bucket(raw_lang: str) -> str:
    """Best-effort alias-normalized bucket for a track's raw language code,
    used to match against lang: selectors (e.g. so a track tagged 'en'
    matches a 'lang:eng' selector)."""
    v = raw_lang.strip().lower()
    return _LANG_ALIASES.get(v, v)


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------

def resolve_selector(spec: TrackEditSpec, type_tracks: list):
    """
    Resolve a TrackEditSpec's selector against one file's tracks of the
    matching type (already filtered to spec.track_type, in file order).

    Returns (track_or_None, position_or_None, error_or_None):
      - track: the matched track dict
      - position: its 1-based index within type_tracks (what mkvpropedit's
        track:a{N}/track:s{N} selector needs)
      - error: a human-readable reason, if resolution failed
    """
    kind, value = spec.selector_kind, spec.selector_value

    if kind == "index":
        if 1 <= value <= len(type_tracks):
            return type_tracks[value - 1], value, None
        return None, None, (
            f"index {value} out of range (file has {len(type_tracks)} {spec.track_type} track(s))"
        )

    if kind == "id":
        for pos, t in enumerate(type_tracks, start=1):
            if t.get("id") == value:
                return t, pos, None
        return None, None, f"no {spec.track_type} track with id {value}"

    if kind == "lang":
        matches = [(pos, t) for pos, t in enumerate(type_tracks, start=1)
                   if lang_bucket(get_lang(t)) == value]
        if not matches:
            return None, None, f"no {spec.track_type} track currently tagged language '{value}'"
        if len(matches) > 1:
            return None, None, (
                f"ambiguous: {len(matches)} {spec.track_type} tracks are tagged language "
                f"'{value}' (use an index or name: selector instead)"
            )
        pos, t = matches[0]
        return t, pos, None

    if kind == "name":
        needle = value.lower()
        matches = [(pos, t) for pos, t in enumerate(type_tracks, start=1)
                   if needle in track_name(t).lower()]
        if not matches:
            return None, None, f"no {spec.track_type} track name contains '{value}'"
        if len(matches) > 1:
            return None, None, (
                f"ambiguous: {len(matches)} {spec.track_type} track names contain '{value}' "
                f"(use an index or id: selector instead)"
            )
        pos, t = matches[0]
        return t, pos, None

    raise AssertionError(f"unknown selector kind: {kind}")  # pragma: no cover


class ResolvedEdit:
    """One TrackEditSpec successfully resolved against one file."""

    def __init__(self, spec: TrackEditSpec, track: dict, position: int):
        self.spec = spec
        self.track = track
        self.position = position

    @property
    def mkvpropedit_selector(self) -> str:
        prefix = "a" if self.spec.track_type == "audio" else "s"
        return f"track:{prefix}{self.position}"


# ---------------------------------------------------------------------------
# Layout signature (for cross-file consistency checking)
# ---------------------------------------------------------------------------

def build_layout_signature(tracks: list):
    """A hashable snapshot of a file's current (pre-edit) audio/subtitle
    track layout: language, name, default flag, forced flag, and (for
    audio) channel count for every audio and every subtitle track, in
    order. Two files with the same signature have identical input track
    layouts."""
    audio = tuple(
        (get_lang(t), track_name(t), is_currently_default(t), is_currently_forced(t), get_audio_channels(t))
        for t in tracks if t["type"] == "audio"
    )
    subs = tuple(
        (get_lang(t), track_name(t), is_currently_default(t), is_currently_forced(t), None)
        for t in tracks if t["type"] == "subtitles"
    )
    return (audio, subs)


def _entry_label(entry) -> str:
    lang, name, default, forced, channels = entry
    lang_part = f"{lang} {channel_layout_label(channels)}" if channels is not None else lang
    label = f'{lang_part}:"{name}"' if name else f"{lang_part}:(no name)"
    tags = []
    if default:
        tags.append("default")
    if forced:
        tags.append("forced")
    if tags:
        label += " [" + "+".join(tags) + "]"
    return label


def format_layout(sig) -> str:
    audio_sig, subs_sig = sig

    def fmt_group(group):
        parts = [_entry_label(entry) for entry in group]
        return ", ".join(parts) if parts else "(none)"

    return f"audio=[{fmt_group(audio_sig)}]  subs=[{fmt_group(subs_sig)}]"


def format_layout_diff(sig, standard_sig) -> str:
    """Same one-line-per-group format as format_layout(), but for a file
    that deviates from standard_sig: entries that differ from the standard
    track at the same position are highlighted, an entry this file has
    beyond what standard has is tagged [EXTRA], and a standard entry this
    file is missing is shown as [MISSING]."""
    audio_sig, subs_sig = sig
    std_audio, std_subs = standard_sig

    def fmt_group(group, std_group):
        max_len = max(len(group), len(std_group))
        if max_len == 0:
            return "(none)"
        parts = []
        for i in range(max_len):
            cur = group[i] if i < len(group) else None
            std = std_group[i] if i < len(std_group) else None
            if cur is None:
                parts.append(c_err(f"{_entry_label(std)} [MISSING]"))
            elif std is None:
                parts.append(c_warn(f"{_entry_label(cur)} [EXTRA]"))
            elif cur != std:
                parts.append(c_warn(_entry_label(cur)))
            else:
                parts.append(_entry_label(cur))
        return ", ".join(parts)

    return f"audio=[{fmt_group(audio_sig, std_audio)}]  subs=[{fmt_group(subs_sig, std_subs)}]"


# ---------------------------------------------------------------------------
# Per-file probing (phase 1: never writes anything)
# ---------------------------------------------------------------------------

class FileProbe:
    def __init__(self, path: Path):
        self.path = path
        self.display = path.name   # overwritten with a path relative to --input-dir in main()
        self.tracks = []
        self.layout_sig = None
        self.resolved = []          # list[ResolvedEdit]
        self.errors = []            # list[str], one per failed --set for this file
        self.probe_error = None     # set if mkvmerge -J itself failed


def probe_file(mkv_path: Path, specs: list, auto_name_audio: bool = False) -> FileProbe:
    probe = FileProbe(mkv_path)
    try:
        info = get_track_info(mkv_path)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        probe.probe_error = str(e)
        return probe

    tracks = info.get("tracks", [])
    probe.tracks = tracks
    probe.layout_sig = build_layout_signature(tracks)

    audio_tracks = [t for t in tracks if t["type"] == "audio"]
    sub_tracks = [t for t in tracks if t["type"] == "subtitles"]

    for spec in specs:
        type_tracks = audio_tracks if spec.track_type == "audio" else sub_tracks
        track, position, error = resolve_selector(spec, type_tracks)
        if error:
            probe.errors.append(f"--set {spec.describe_selector()} ({spec.raw}): {error}")
        else:
            probe.resolved.append(ResolvedEdit(spec, track, position))

    if auto_name_audio:
        for pos, t in enumerate(audio_tracks, start=1):
            computed_name = compute_auto_audio_name(t)
            existing = next(
                (r for r in probe.resolved if r.spec.track_type == "audio" and r.position == pos), None
            )
            if existing is not None:
                # an explicit --set already targets this track. If it didn't
                # already specify a name itself, fold the computed name in --
                # via a NEW spec object, since existing.spec is shared across
                # every file's probe and must not be mutated in place.
                if "name" not in existing.spec.props:
                    merged_props = dict(existing.spec.props)
                    merged_props["name"] = computed_name
                    existing.spec = TrackEditSpec(
                        existing.spec.track_type, existing.spec.selector_kind,
                        existing.spec.selector_value, merged_props, existing.spec.raw
                    )
            else:
                auto_spec = TrackEditSpec("audio", "index", pos, {"name": computed_name}, raw="(auto-name-audio)")
                probe.resolved.append(ResolvedEdit(auto_spec, t, pos))

    return probe


def check_batch_consistency(probes: list):
    """
    Compare every file's probe results. Returns (uniform, standard_sig,
    deviation_report) where deviation_report is a list of
    (probe, [reason, ...]) for every file that isn't clean, empty if
    uniform is True.
    """
    ok_probes = [p for p in probes if p.probe_error is None]
    if not ok_probes:
        return False, None, [(p, [p.probe_error or "could not be inspected"]) for p in probes]

    counts = collections.Counter(p.layout_sig for p in ok_probes)
    standard_sig, _ = counts.most_common(1)[0]

    deviation_report = []
    for p in probes:
        reasons = []
        if p.probe_error is not None:
            reasons.append(f"could not be inspected: {p.probe_error}")
        else:
            if p.layout_sig != standard_sig:
                reasons.append("track layout differs from the standard layout")
            reasons.extend(p.errors)
        if reasons:
            deviation_report.append((p, reasons))

    uniform = not deviation_report
    return uniform, standard_sig, deviation_report


# ---------------------------------------------------------------------------
# Dry-run diff display
# ---------------------------------------------------------------------------

def print_file_diff(probe: FileProbe):
    print(c_header(f"  {probe.display}"))
    if probe.probe_error:
        print(c_err(f"    ERROR: could not be inspected: {probe.probe_error}"))
        return

    for resolved in probe.resolved:
        t = resolved.track
        props = resolved.spec.props
        print(f"    [{resolved.mkvpropedit_selector}] id={t.get('id')} "
              f"{get_lang(t)} \"{track_name(t)}\"" +
              (c_dim("  (currently default)") if is_currently_default(t) else "") +
              (c_dim("  (currently forced)") if is_currently_forced(t) else ""))

        if "name" in props:
            old = track_name(t)
            new = props["name"]
            if new == "":
                print(f"        name:    \"{old}\" -> {c_change('(deleted)')}" if old
                      else f"        name:    (no name) -> {c_dim('(deleted, already unset)')}")
            elif new == old:
                print(f"        name:    \"{old}\" {c_dim('(unchanged)')}")
            else:
                print(f"        name:    \"{old}\" -> {c_change(chr(34) + new + chr(34))}")

        if "language" in props:
            old = get_lang(t)
            new = props["language"]
            if lang_bucket(old) == new:
                print(f"        lang:    {old} {c_dim('(unchanged)')}")
            else:
                print(f"        lang:    {old} -> {c_change(new)}")

        if "default" in props:
            old = is_currently_default(t)
            new = props["default"]
            changed = old != new
            suffix = "" if changed else f"  {c_dim('(unchanged)')}"
            print(f"        default: {status_label(old, False)} -> {status_label(new, changed)}{suffix}")

        if "forced" in props:
            old = is_currently_forced(t)
            new = props["forced"]
            changed = old != new
            suffix = "" if changed else f"  {c_dim('(unchanged)')}"
            print(f"        forced:  {status_label(old, False)} -> {status_label(new, changed)}{suffix}")

    if probe.errors:
        for err in probe.errors:
            print(c_warn(f"    SKIPPED: {err}"))


# ---------------------------------------------------------------------------
# --list mode: aggregate track properties across the batch, no editing
# ---------------------------------------------------------------------------

def _flag_cell(value: bool) -> str:
    text = "yes" if value else "no "
    return c_yes(text) if value else c_dim(text)


def _diff_field(label: str, text: str, width: int, differs: bool) -> str:
    padded = f"{label}={text:<{width}}"
    return c_warn(padded) if differs else padded


def build_track_lines(tracks: list, standard_sig=None) -> list:
    """Render the per-track detail lines (position, language, name, flags,
    codec, plus usable --set selector hints) for one file's tracks.

    If standard_sig is given (the aggregate/standard layout signature),
    fields that differ from the standard track at the same position are
    highlighted in color, and positions that don't exist on one side
    (an extra track this file has that the standard doesn't, or a track
    the standard has that this file is missing) are called out."""
    std_audio, std_subs = standard_sig if standard_sig else ((), ())
    lines = []
    for track_type, std_group in (("audio", std_audio), ("subtitles", std_subs)):
        type_tracks = [t for t in tracks if t["type"] == track_type]
        max_len = max(len(type_tracks), len(std_group))
        if max_len == 0:
            lines.append(c_dim(f"    {track_type}: (none)"))
            continue
        for pos in range(1, max_len + 1):
            t = type_tracks[pos - 1] if pos <= len(type_tracks) else None
            std = std_group[pos - 1] if pos <= len(std_group) else None

            if t is None:
                # standard has a track at this position that this file lacks
                s_lang, s_name, s_default, s_forced, s_channels = std
                s_name_display = f'"{s_name}"' if s_name else "(no name)"
                s_ch_display = f" ch={channel_layout_label(s_channels)}" if s_channels is not None else ""
                lines.append(c_err(
                    f"    {track_type:<10} #{pos:<3} MISSING "
                    f"(standard has lang={s_lang}{s_ch_display} name={s_name_display})"
                ))
                continue

            codec = t.get("codec", "?")
            lang = get_lang(t)
            name = track_name(t)
            default = is_currently_default(t)
            forced = is_currently_forced(t)
            channels = get_audio_channels(t)
            ch_label = channel_layout_label(channels) if channels is not None else "-"
            name_display = f'"{name}"' if name else "(no name)"

            if std is None:
                # this file has an extra track the standard doesn't have
                line = (
                    f"    {track_type:<10} #{pos:<3} "
                    f"{c_warn(f'lang={lang:<8}')} "
                    f"{c_warn(f'ch={ch_label:<6}')} "
                    f"default={_flag_cell(default)} "
                    f"forced={_flag_cell(forced)} "
                    f"codec={codec:<12} "
                    f"{c_warn(f'name={name_display}')}  {c_warn('[EXTRA]')}"
                )
            else:
                s_lang, s_name, s_default, s_forced, s_channels = std
                s_ch_label = channel_layout_label(s_channels) if s_channels is not None else "-"
                s_name_display = f'"{s_name}"' if s_name else "(no name)"
                line = (
                    f"    {track_type:<10} #{pos:<3} "
                    f"{_diff_field('lang', lang, 8, lang != s_lang)} "
                    f"{_diff_field('ch', ch_label, 6, channels != s_channels)} "
                    f"default={_flag_cell(default) if default == s_default else c_warn('yes' if default else 'no ')} "
                    f"forced={_flag_cell(forced) if forced == s_forced else c_warn('yes' if forced else 'no ')} "
                    f"codec={codec:<12} "
                    f"{_diff_field('name', name_display, 0, name != s_name)}"
                )
            lines.append(line)

            selector_hint = f"{track_type} {pos}"
            extras = [f"id:{t.get('id')}"]
            if lang != "und":
                extras.append(f"lang:{lang}")
            if name:
                extras.append(f"name:{name.lower()}")
            lines.append(c_dim(f"        selectors: '{selector_hint}'  ({', '.join(extras)})"))
    return lines


def build_group_lines_from_signature(sig) -> list:
    """Same rendering as build_track_lines, but from a layout signature
    (lang, name, default, forced, channels tuples) rather than live track
    dicts -- used for the aggregate standard-layout view, which has no
    single file or track ids of its own."""
    audio_sig, subs_sig = sig
    lines = []
    for track_type, group in (("audio", audio_sig), ("subtitles", subs_sig)):
        if not group:
            lines.append(c_dim(f"    {track_type}: (none)"))
            continue
        for pos, (lang, name, default, forced, channels) in enumerate(group, start=1):
            name_display = f'"{name}"' if name else c_dim("(no name)")
            ch_label = channel_layout_label(channels) if channels is not None else "-"
            lines.append(
                f"    {track_type:<10} #{pos:<3} "
                f"lang={lang:<8} "
                f"ch={ch_label:<6} "
                f"default={_flag_cell(default)} "
                f"forced={_flag_cell(forced)} "
                f"name={name_display}"
            )
            selector_hint = f"{track_type} {pos}"
            extras = []
            if lang != "und":
                extras.append(f"lang:{lang}")
            if name:
                extras.append(f"name:{name.lower()}")
            extra_str = f"  ({', '.join(extras)})" if extras else ""
            lines.append(c_dim(f"        selectors: '{selector_hint}'{extra_str}"))
    return lines


class ListProbe:
    def __init__(self, path: Path, tracks: list, error: str = None):
        self.path = path
        self.display = path.name   # overwritten with a path relative to --input-dir below
        self.tracks = tracks
        self.error = error
        self.signature = build_layout_signature(tracks) if error is None else None


def probe_file_for_list(mkv_path: Path) -> ListProbe:
    try:
        info = get_track_info(mkv_path)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        return ListProbe(mkv_path, [], error=str(e))
    return ListProbe(mkv_path, info.get("tracks", []))


def run_list_command(mkv_files: list, input_dir: Path):
    probes = [probe_file_for_list(p) for p in mkv_files]
    for p in probes:
        p.display = str(p.path.relative_to(input_dir))
    ok_probes = [p for p in probes if p.error is None]
    error_probes = [p for p in probes if p.error is not None]

    if not ok_probes:
        print(c_err("None of the files could be inspected:"))
        for p in error_probes:
            print(c_err(f"  {p.display}: {p.error}"))
        return

    counts = collections.Counter(p.signature for p in ok_probes)
    standard_sig, standard_count = counts.most_common(1)[0]
    outliers = [p for p in ok_probes if p.signature != standard_sig] + error_probes

    print(c_header(f"Aggregate track layout across {len(probes)} file(s):"))
    print()
    for line in build_group_lines_from_signature(standard_sig):
        print(line)
    print()

    if not outliers:
        print(c_success(f"All {len(probes)} file(s) match this layout exactly."))
    else:
        print(c_note(f"{standard_count} of {len(probes)} file(s) match this layout."))
        print()
        print(c_warn(f"OUTLIERS ({len(outliers)}) -- these files differ from the layout above "
                     f"(highlighted fields are what differs):"))
        for p in outliers:
            print(c_header(f"  {p.display}"))
            if p.error is not None:
                print(c_err(f"    ERROR: could not be inspected: {p.error}"))
                continue
            for line in build_track_lines(p.tracks, standard_sig=standard_sig):
                print(line)

    print()
    print(c_dim(
        "Use these positions/languages/names as SELECTOR values in --set "
        "(e.g. --set audio 1 ..., --set subtitles lang:eng ...). For an outlier "
        "file, use the selectors shown under that file specifically."
    ))


# ---------------------------------------------------------------------------
# mkvpropedit command building / execution
# ---------------------------------------------------------------------------

def build_command(mkv_path: Path, resolved_edits: list, lang_prop: str) -> list:
    cmd = ["mkvpropedit", str(mkv_path)]
    for r in resolved_edits:
        cmd += ["--edit", r.mkvpropedit_selector]
        props = r.spec.props
        if "name" in props:
            if props["name"] == "":
                cmd += ["--delete", "name"]
            else:
                cmd += ["--set", f"name={props['name']}"]
        if "language" in props:
            if lang_prop in ("language", "both"):
                cmd += ["--set", f"language={props['language']}"]
            if lang_prop in ("language-ietf", "both"):
                cmd += ["--set", f"language-ietf={props['language']}"]
        if "default" in props:
            cmd += ["--set", f"flag-default={1 if props['default'] else 0}"]
        if "forced" in props:
            cmd += ["--set", f"flag-forced={1 if props['forced'] else 0}"]
    return cmd


def maybe_backup(mkv_path: Path, backup_dir: Path, dry_run: bool, relative_to: Path = None):
    rel = mkv_path.relative_to(relative_to) if relative_to else mkv_path.name
    dest = backup_dir / rel
    if dry_run:
        note = "already exists, would be skipped" if dest.exists() else "would be copied"
        print(c_dim(f"    [DRY RUN] Backup: {dest} ({note})"))
        return
    if dest.exists():
        print(c_note(f"    Backup already exists, skipping: {dest}"))
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mkv_path, dest)
    print(c_dim(f"    Backed up to: {dest}"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def find_mkv_files(input_dir: Path, recursive: bool, exclude_dir: Path = None) -> list:
    pattern = "**/*.mkv" if recursive else "*.mkv"
    files = input_dir.glob(pattern)
    if exclude_dir is not None:
        files = (f for f in files if exclude_dir not in f.resolve().parents and f.resolve() != exclude_dir)
    return sorted(files)


def main():
    global USE_COLOR

    args = parse_args()
    USE_COLOR = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")

    single_file = None
    if args.file:
        single_file = Path(args.file).resolve()
        if not single_file.is_file():
            print(c_err(f"File does not exist: {single_file}"), file=sys.stderr)
            sys.exit(1)
        scan_root = single_file.parent
    else:
        input_dir = Path(args.input_dir).resolve()
        if not input_dir.is_dir():
            print(c_err(f"Input directory does not exist: {input_dir}"), file=sys.stderr)
            sys.exit(1)
        scan_root = input_dir

    backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else None

    def discover():
        if single_file:
            return [single_file]
        files = find_mkv_files(scan_root, args.recursive, exclude_dir=backup_dir)
        if not files:
            scope = "recursively" if args.recursive else "(non-recursively; pass --recursive to include subfolders)"
            print(f"No .mkv files found in {scan_root} {scope}")
        return files

    if args.list:
        mkv_files = discover()
        if not mkv_files:
            return
        run_list_command(mkv_files, scan_root)
        return

    if not args.set_specs and not args.auto_name_audio:
        print(c_err("ERROR: at least one --set or --auto-name-audio is required"), file=sys.stderr)
        sys.exit(1)

    try:
        specs = parse_all_set_specs(args.set_specs) if args.set_specs else []
    except ValueError as e:
        print(c_err(f"ERROR: {e}"), file=sys.stderr)
        sys.exit(1)

    mkv_files = discover()
    if not mkv_files:
        return

    if args.dry_run:
        print(c_banner("=== DRY RUN: no files will be modified ===") + "\n")

    print(c_header("Edits to apply to every file:"))
    for i, spec in enumerate(specs, start=1):
        prop_str = ", ".join(f"{k}={v}" for k, v in spec.props.items())
        print(f"  {i}. {spec.describe_selector()}: {prop_str}")
    if args.auto_name_audio:
        print(f"  {len(specs) + 1}. audio (every track): name=<Language> <Channel layout> <Codec>  "
              f"(e.g. \"English 5.1 FLAC\")")
    print()

    # ---- Phase 1: probe every file, resolve selectors, never write anything ----
    print(c_header(f"Inspecting {len(mkv_files)} file(s)..."))
    probes = [probe_file(p, specs, auto_name_audio=args.auto_name_audio) for p in mkv_files]
    for p in probes:
        p.display = str(p.path.relative_to(scan_root))
    uniform, standard_sig, deviation_report = check_batch_consistency(probes)
    print()

    for probe in probes:
        print_file_diff(probe)

    print()
    print(c_header("=== Batch Consistency Check ==="))
    if standard_sig is not None:
        print("Standard input track layout:")
        print(f"  {format_layout(standard_sig)}")
    if uniform:
        print(c_success(f"All {len(probes)} file(s) match the standard layout and every --set resolved cleanly."))
    else:
        print(c_note(f"{len(deviation_report)} of {len(probes)} file(s) DEVIATE from the standard layout or "
                     f"could not resolve every --set:"))
        for probe, reasons in deviation_report:
            print(c_header(f"  {probe.display}"))
            for reason in reasons:
                is_hard_error = reason.startswith("--set ") or reason.startswith("could not be inspected")
                print(c_err(f"    - {reason}") if is_hard_error else f"    - {reason}")
            if probe.probe_error is None and standard_sig is not None and probe.layout_sig != standard_sig:
                print(f"    {format_layout_diff(probe.layout_sig, standard_sig)}")

    if args.dry_run:
        print()
        print(c_dim("(dry run -- no files were inspected for writing and nothing was modified)"))
        return

    if not uniform and not args.force:
        print()
        print(c_err("Refusing to edit: input files are not uniform (see above). Re-run with --dry-run "
                     "to review, fix the deviating file(s) or your --set selectors, or pass --force to "
                     "proceed anyway (deviating files/edits that can't be resolved will be skipped)."))
        sys.exit(1)

    # ---- Phase 2: actually run mkvpropedit ----
    print()
    print(c_header("=== Applying edits ==="))
    succeeded = 0
    skipped = 0
    failed = []
    for probe in probes:
        if probe.probe_error:
            print(c_err(f"  SKIPPING {probe.display}: could not be inspected: {probe.probe_error}"))
            skipped += 1
            continue
        if not probe.resolved:
            print(c_warn(f"  SKIPPING {probe.display}: no --set resolved to a track"))
            skipped += 1
            continue

        print(c_header(f"  {probe.display}"))
        if probe.errors:
            for err in probe.errors:
                print(c_warn(f"    (skipping unresolved edit: {err})"))

        if backup_dir:
            maybe_backup(probe.path, backup_dir, dry_run=False, relative_to=scan_root)

        cmd = build_command(probe.path, probe.resolved, args.lang_prop)
        print(c_dim(f"    Running: {' '.join(cmd)}"))
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            succeeded += 1
        except subprocess.CalledProcessError as e:
            failed.append(probe.display)
            print(c_err(f"    ERROR: {e.stderr.strip() if e.stderr else e}"))

    print()
    print(c_header("=== Summary ==="))
    print(f"  Files found:     {len(probes)}")
    print(f"  Edited:          {succeeded}")
    if skipped:
        print(c_note(f"  Skipped:         {skipped}"))
    if failed:
        print(c_err(f"  Failed:          {len(failed)}"))
        for fname in failed:
            print(c_err(f"    - {fname}"))

    print(c_success("Done.") if not failed else c_err("Done, with errors (see above)."))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
