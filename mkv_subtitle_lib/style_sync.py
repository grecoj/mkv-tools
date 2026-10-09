"""Stage: style-sync -- make named styles match a per-show look (STYLE_SYNC_RULES), optionally
generate a signs-only track, and attach required fonts.
"""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .common import (
    C,
    DIALOGUE_STYLE_RE,
    EXTRACTED_DIRNAME,
    STYLE_FORMAT_LINE_RE,
    STYLE_LINE_RE,
    STYLE_SYNCED_DIRNAME,
    TRACKS_META_FILENAME,
    c,
    check_tools,
    default_fonts_dir,
    find_ass_play_res,
    fmt_num,
    read_text_smart,
    rebuild_all_mkvs,
    track_matches_filter,
)


# Per-show style-sync rule sets, selected via the 'style-sync' stage's
# required --show flag. Each show has:
#   reference_res    - the PlayResX/PlayResY the values below were designed
#                       at. Pixel-sized fields are rescaled by
#                       (file's own PlayResY / reference_res[1]) for each
#                       individual file, since different releases of the
#                       same show can use different reference resolutions
#                       (e.g. a 640x360 vs. a 1920x1080 convention) even
#                       though the on-screen result should look the same.
#   style_names       - which named [V4+ Styles] entries get synced. A
#                       style name not present in a given file is skipped
#                       (not an error -- not every episode necessarily
#                       defines every variant).
#   fixed_fields      - field -> value applied as-is, unscaled (colors,
#                       Bold/Underline/StrikeOut, ScaleX/ScaleY, Angle,
#                       BorderStyle, Encoding -- anything that isn't a
#                       pixel size).
#   scaled_fields     - field -> reference value, multiplied by the
#                       computed scale before being applied (Fontsize,
#                       Outline, Shadow, Spacing, MarginL/MarginR/MarginV).
# Name, Italic, and Alignment are deliberately never touched here -- they
# define what makes each named style distinct (regular vs. italic,
# bottom- vs. top-positioned), so the sync preserves whatever each style
# already has for those three fields.
STYLE_SYNC_RULES: Dict[str, Dict[str, Any]] = {
    "dnt": {
        "reference_res": (1920, 1080),
        "style_names": [
            "Main", "Italics", "Flashback", "Flashback_Italics",
            "Main - Top", "Italics - Top", "Narration",
        ],
        "fixed_fields": {
            "Fontname": "Gandhi Sans",
            "PrimaryColour": "&H00FFFFFF",
            "SecondaryColour": "&H000000FF",
            "OutlineColour": "&H00000000",
            "BackColour": "&H96000000",
            "Bold": "-1",
            "Underline": "0",
            "StrikeOut": "0",
            "ScaleX": "100",
            "ScaleY": "100",
            "Angle": "0",
            "BorderStyle": "1",
            "Encoding": "1",
        },
        "scaled_fields": {
            "Fontsize": 75,
            "Outline": 3,
            "Shadow": 1,
            "Spacing": 0,
            "MarginL": 150,
            "MarginR": 150,
            "MarginV": 50,
        },
        # Font files this show's "look" needs embedded as mkv attachments --
        # DNT's original releases use Roboto Medium (which IS embedded), but
        # after fixed_fields above switches Fontname to Gandhi Sans, that
        # font also needs to be attached or it won't render correctly for
        # viewers who don't have it installed system-wide. Filenames are
        # looked up in --fonts-dir (default: a "mkv_subtitle_fonts" folder
        # next to this script).
        "fonts": [
            "GandhiSans-Bold_A.otf",
            "GandhiSans-BoldItalic_A.otf",
        ],
    },
}


def extract_non_dialogue_lines(lines: List[str], dialogue_style_names: List[str]) -> Tuple[List[str], int]:
    """
    Build a "signs-only" variant of an ASS/SSA file: every non-Events
    line (Script Info, Styles, Format headers, etc.) is kept as-is so the
    result is still a complete, valid, self-contained file, but Events
    section Dialogue/Comment lines are kept only if their Style field is
    NOT in `dialogue_style_names` -- i.e. everything that isn't one of
    the show's known spoken-dialogue styles (signs, titles, karaoke, and
    any other named style) survives. Returns (new_lines, kept_count).
    """
    new_lines: List[str] = []
    kept = 0
    for line in lines:
        if line.startswith("Dialogue:") or line.startswith("Comment:"):
            m = DIALOGUE_STYLE_RE.match(line)
            style_name = m.group(1).strip() if m else None
            if style_name is not None and style_name in dialogue_style_names:
                continue  # this is a dialogue line -- drop it from the signs-only copy
            kept += 1
        new_lines.append(line)
    return new_lines, kept


def sync_styles(
    lines: List[str],
    style_names: List[str],
    fixed_fields: Dict[str, str],
    scaled_fields: Dict[str, float],
    scale: float,
) -> Tuple[List[str], List[Tuple[str, Dict[str, Tuple[str, str]]]]]:
    """
    Rewrite the named [V4+ Styles] entries in `style_names` so their
    fixed_fields match exactly and their scaled_fields match (scaled by
    `scale`, which the caller computes from this file's own PlayResY vs.
    the rule set's reference_res). Name, Italic, and Alignment are always
    preserved from the file's existing definition -- they're what make
    each named style distinct, not part of the "look."

    Returns (new_lines, changes) where changes is
    [(style_name, {field: (old_value, new_value)})] for every style that
    was actually found and touched, listing only fields whose value
    changed (so a style already matching the target shows an empty dict).
    """
    new_lines: List[str] = []
    changes: List[Tuple[str, Dict[str, Tuple[str, str]]]] = []
    style_fields: Optional[List[str]] = None

    for line in lines:
        fmt_m = STYLE_FORMAT_LINE_RE.match(line)
        if fmt_m and style_fields is None:
            candidate_fields = [f.strip() for f in fmt_m.group(1).split(",")]
            if "MarginL" in candidate_fields or "Fontsize" in candidate_fields:
                style_fields = candidate_fields
            new_lines.append(line)
            continue

        style_m = STYLE_LINE_RE.match(line)
        if style_m and style_fields:
            prefix, data = style_m.groups()
            parts = [p.strip() for p in data.split(",")]
            if len(parts) == len(style_fields):
                field_index = {name: i for i, name in enumerate(style_fields)}
                name_idx = field_index.get("Name")
                this_name = parts[name_idx] if name_idx is not None else None

                if this_name in style_names:
                    field_changes: Dict[str, Tuple[str, str]] = {}

                    for field, value in fixed_fields.items():
                        idx = field_index.get(field)
                        if idx is None:
                            continue
                        if parts[idx] != value:
                            field_changes[field] = (parts[idx], value)
                        parts[idx] = value

                    for field, ref_value in scaled_fields.items():
                        idx = field_index.get(field)
                        if idx is None:
                            continue
                        new_value = fmt_num(ref_value * scale)
                        if parts[idx] != new_value:
                            field_changes[field] = (parts[idx], new_value)
                        parts[idx] = new_value

                    changes.append((this_name, field_changes))
                    new_lines.append(prefix + ",".join(parts))
                    continue

        new_lines.append(line)

    return new_lines, changes


def cmd_style_sync(args: argparse.Namespace) -> None:
    check_tools()
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

    show_key = args.show.strip().lower()
    if show_key not in STYLE_SYNC_RULES:
        available = ", ".join(sorted(STYLE_SYNC_RULES))
        sys.exit(f"Unknown --show '{args.show}'. Available shows: {available}")
    rule = STYLE_SYNC_RULES[show_key]
    reference_res = rule["reference_res"]
    style_names = rule["style_names"]
    fixed_fields = rule["fixed_fields"]
    scaled_fields = rule["scaled_fields"]
    required_fonts = rule.get("fonts", [])

    attach_files: List[Path] = []
    if required_fonts:
        fonts_dir = Path(args.fonts_dir).resolve() if args.fonts_dir else default_fonts_dir()
        if not fonts_dir.exists():
            print(c(f"WARNING: fonts directory not found ({fonts_dir}); "
                  f"required font(s) for '{show_key}' will NOT be attached: {', '.join(required_fonts)}",
                  C.YELLOW))
        else:
            missing_fonts = []
            for font_name in required_fonts:
                font_path = fonts_dir / font_name
                if font_path.exists():
                    attach_files.append(font_path)
                else:
                    missing_fonts.append(font_name)
            if attach_files:
                print(f"Will attach {len(attach_files)} font file(s) from {fonts_dir}: "
                      f"{', '.join(f.name for f in attach_files)}")
            if missing_fonts:
                print(c(f"WARNING: font file(s) not found in {fonts_dir}, will NOT be attached: "
                      f"{', '.join(missing_fonts)}", C.YELLOW))
        print()

    synced_root = work_dir / STYLE_SYNCED_DIRNAME
    signs_root = work_dir / STYLE_SYNCED_DIRNAME / "signs"
    edited_paths: Dict[str, Path] = {}
    added_subs_by_mkv: Dict[str, List[Dict[str, Any]]] = {}
    any_synced = False
    any_signs_generated = False

    for mkv_dir in mkv_dirs:
        meta_path = mkv_dir / TRACKS_META_FILENAME
        if not meta_path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        print(c(f"== {meta.get('source_name', mkv_dir.name)} ==", C.BOLD, C.BLUE))

        editable_tracks = [t for t in meta["tracks"] if t["editable"]]
        matching_tracks = [
            t for t in editable_tracks
            if track_matches_filter(t["id"], t.get("track_name", ""), args.track_id, args.track_name)
        ]
        if editable_tracks and not matching_tracks and (args.track_id or args.track_name):
            print(c("  no tracks match --track-id/--track-name; skipped.", C.YELLOW))
            print()
            continue

        for t in matching_tracks:
            rel = t["file"]
            src_path = work_dir / rel
            ext = src_path.suffix.lower()
            label = t.get("track_name") or f"track {t['id']}"

            if ext not in (".ass", ".ssa"):
                print(f"  track {t['id']} ({label}): {ext} has no [V4+ Styles] section to sync, skipped.")
                continue

            text, encoding = read_text_smart(src_path)
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines(keepends=False)

            play_res = find_ass_play_res(lines)
            if play_res is None:
                print(c(f"  track {t['id']} ({label}): no PlayResX/PlayResY found; skipped.", C.YELLOW))
                continue
            scale = play_res[1] / reference_res[1]

            new_lines, changes = sync_styles(lines, style_names, fixed_fields, scaled_fields, scale)

            found_names = {name for name, _ in changes}
            missing = [n for n in style_names if n not in found_names]
            touched = [(name, fc) for name, fc in changes if fc]

            if missing:
                print(c(f"  track {t['id']} ({label}): style(s) not found in this file, skipped: "
                      f"{', '.join(missing)}", C.DIM))

            if touched:
                print(c(
                    f"  track {t['id']} ({label}): {play_res[0]}x{play_res[1]} vs. reference "
                    f"{reference_res[0]}x{reference_res[1]}  (scale {scale:.3f}) -- "
                    f"{len(touched)}/{len(found_names)} style(s) updated:",
                    C.GREEN,
                ))
                for name, field_changes in touched:
                    field_strs = [f"{f}: {old} -> {new}" for f, (old, new) in field_changes.items()]
                    print(f"      {c(name, C.CYAN)}  " + ", ".join(field_strs))

                new_text = newline.join(new_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
                dest_path = synced_root / rel
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_text(new_text, encoding="utf-8")
                edited_paths[rel] = dest_path
                any_synced = True
            else:
                print(f"  track {t['id']} ({label}): {len(found_names)} style(s) already match, nothing to sync.")

            if args.generate_signs_track:
                signs_lines, kept = extract_non_dialogue_lines(new_lines, style_names)
                if kept == 0:
                    print(f"  track {t['id']} ({label}): no non-dialogue-style lines found, no signs track generated.")
                else:
                    signs_text = newline.join(signs_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
                    signs_dest = signs_root / rel
                    signs_dest.parent.mkdir(parents=True, exist_ok=True)
                    signs_dest.write_text(signs_text, encoding="utf-8")

                    signs_label = f"{label} - Signs" if label and label != f"track {t['id']}" else "Signs"
                    added_subs_by_mkv.setdefault(mkv_dir.name, []).append({
                        "path": signs_dest,
                        "language": t.get("language_ietf") or t.get("language") or "und",
                        "track_name": signs_label,
                        "default": False,
                        "forced": False,
                    })
                    any_signs_generated = True
                    print(c(f"  track {t['id']} ({label}): generated signs track "
                          f"'{signs_label}' with {kept} line(s).", C.MAGENTA))
        print()

    if not any_synced and not any_signs_generated and not attach_files:
        print(c("No subtitle tracks needed style syncing, and no signs track was generated.", C.BOLD, C.GREEN))
        return

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run,
                      added_subs_by_mkv, attach_files)
