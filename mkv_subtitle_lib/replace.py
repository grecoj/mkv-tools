"""Stages: plan-replace and merge-replace -- per-show text replacement across subtitle files.

plan-replace scans extracted subtitles for the show's REPLACE_RULES (plus any --rules-file
entries), prompts for lines containing the show's watch word that no rule covers, and records
everything to plan.json / plan.log without modifying anything. merge-replace then applies that
plan to the subtitle files and rebuilds the mkvs.
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .common import (
    ASS_COMMENT_RE,
    ASS_DIALOGUE_RE,
    C,
    EDITED_DIRNAME,
    EXTRACTED_DIRNAME,
    PLAN_FILENAME,
    PLAN_LOG_FILENAME,
    SRT_INDEX_RE,
    TEXT_EDITABLE_EXTS,
    TIMESTAMP_RE,
    c,
    check_tools,
    get_timestamp,
    parse_timestamp_bounds,
    read_text_smart,
    rebuild_all_mkvs,
)


# --------------------------------------------------------------------------
# plan-replace
# --------------------------------------------------------------------------


# Per-show replacement rule sets, selected via the 'plan-replace' stage's required
# --show flag. Each show has:
#   replacements             - exact substring match, case-insensitive.
#                               Key = text to search for, Value = replacement.
#   width_compensated_rules  - subset of the replacement keys that should
#                               get automatic \fscx width compensation in
#                               ASS/SSA files when the replacement text is
#                               longer (see compute_width_compensation).
#                               Rules not listed here are left at their
#                               natural width even if longer.
#   watch_word                - word that triggers an interactive prompt for
#                               any line containing it that isn't already
#                               covered by a replacement rule above.
REPLACE_RULES: Dict[str, Dict[str, Any]] = {
    "frieren": {
        "replacements": {
            "an ageless elegy": "BEYOND JOURNEY'S END",
            "Frieren the Elegy": "Frieren the Slayer",
            "Solltrag": "Zoltraak",
            "end your life": "Kill yourself",
        },
        "width_compensated_rules": {
            "an ageless elegy",
        },
        "watch_word": "elegy",
    },
}


# Cap how much a replacement's on-screen text is allowed to shrink to
# compensate for extra width (percent of original horizontal scale).
MIN_FSCX_SCALE = 80


MAX_FSCX_SCALE = 100


# How much of the "full" correction to actually apply (0 = never scale,
# 1 = fully match the old text's width). 0.5 splits the difference so
# long replacements shrink noticeably less than a full 1:1 correction.
WIDTH_COMPENSATION_STRENGTH = 1.0


# When grouping consecutive matches for the log, two records are treated as
# part of the same animation/effect sequence if the next one starts within
# this many seconds of the previous one ending (handles frame-stepped
# reveal/wipe animations with lots of tiny timing increments). Set higher
# to merge more generously, lower to split on any real timing gap.
TIME_GROUPING_TOLERANCE_SECONDS = 0.5


# Sanity cap on how many raw file lines a group is allowed to span, so a
# rule matching the same text again far later in the file (a genuinely
# separate occurrence) doesn't get merged just because timing happens to
# line up.
MAX_LINE_GAP_FOR_GROUPING = 50


ASS_INLINE_TAG_RE = re.compile(r"\{[^}]*\}")


ASS_LEADING_TAGS_RE = re.compile(r"^(?:\{[^}]*\})+")


def split_leading_ass_tags(text: str) -> Tuple[str, str]:
    """
    Split off any {...} override-tag block(s) sitting at the very start of
    an ASS/SSA text field (e.g. positioning/color/font setup) from the
    rest of the visible text.
    """
    m = ASS_LEADING_TAGS_RE.match(text)
    leading = m.group(0) if m else ""
    return leading, text[len(leading):]


def strip_inline_ass_tags(text: str) -> str:
    """
    Remove any remaining {...} override-tag blocks embedded *within* the
    text (e.g. per-letter effects like {\\fsp} used for stylized
    typesetting/karaoke). These often split words across tag boundaries
    (e.g. "AN AGELESS ELEG{\\fsp}Y"), which would otherwise prevent plain
    substring matching from ever succeeding.
    """
    return ASS_INLINE_TAG_RE.sub("", text)


def compute_width_compensation(old_visible: str, new_visible: str) -> Optional[int]:
    """
    If new_visible is longer than old_visible, return a horizontal-scale
    percentage (for an ASS \\fscx tag) that shrinks the new text to
    partially compensate for the extra width. Only WIDTH_COMPENSATION_STRENGTH
    of the full 1:1 correction is applied, so the result sits between "no
    scaling" and "exactly matching the old width" rather than fully
    matching it. Returns None if no compensation is needed (new text
    isn't longer). Clamped to [MIN_FSCX_SCALE, MAX_FSCX_SCALE] so large
    size differences don't squash the text illegibly.
    """
    old_len = len(old_visible.strip())
    new_len = len(new_visible.strip())
    if old_len == 0 or new_len <= old_len:
        return None
    full_scale = 100 * old_len / new_len
    scale = round(100 - (100 - full_scale) * WIDTH_COMPENSATION_STRENGTH)
    scale = max(MIN_FSCX_SCALE, min(MAX_FSCX_SCALE, scale))
    return scale if scale < 100 else None


def iter_text_segments(lines: List[str], ext: str):
    """
    Yield (line_index, prefix, text_segment, suffix) for every line that
    contains searchable dialogue text. Reconstructed line = prefix + new_text + suffix.
    For simple formats (srt/vtt) prefix/suffix are "" and text_segment is the whole line.
    For ass/ssa, only the Text field of Dialogue/Comment lines is searchable.
    """
    for i, line in enumerate(lines):
        stripped = line.strip()

        if ext in (".ass", ".ssa"):
            m = ASS_DIALOGUE_RE.match(line) or ASS_COMMENT_RE.match(line)
            if m:
                yield i, m.group(1), m.group(2), ""
            continue

        # .srt / .vtt
        if not stripped:
            continue
        if SRT_INDEX_RE.match(stripped):
            continue
        if TIMESTAMP_RE.search(stripped):
            continue
        if ext == ".vtt" and stripped.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        yield i, "", line, ""


def apply_known_replacements(text: str, replacements: Dict[str, str]) -> Tuple[str, List[str]]:
    """Case-insensitive substring replace. Returns (new_text, matched_keys)."""
    matched = []
    new_text = text
    for old, new in replacements.items():
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        if pattern.search(new_text):
            matched.append(old)
            new_text = pattern.sub(lambda _m, new=new: new, new_text)
    return new_text, matched


def flush_match_records(rel: Path, records: List[Dict[str, Any]], log_lines: List[str]) -> None:
    """
    Print and log the buffered match/asked/skip records for one subtitle
    file, collapsing runs of matches that are identical in every way
    except line number/timestamp into a single grouped entry with a line
    range and occurrence count. This covers both:
      - stacked-layer typesetting (several adjacent Dialogue lines at the
        *same* timestamp for a glow/color effect), and
      - frame-stepped reveal/wipe animations (many Dialogue lines with
        slightly *increasing* timestamps, often with unrelated lines for
        other simultaneous signs interleaved between them, breaking raw
        line-number adjacency).
    Grouping is based on timing continuity (see TIME_GROUPING_TOLERANCE_SECONDS)
    rather than strict "next line number" adjacency, so interleaved
    unrelated lines and tiny timing increments don't fragment the log.
    """
    i, n = 0, len(records)
    while i < n:
        j = i
        cur = records[i]
        group_end_seconds = None
        bounds = parse_timestamp_bounds(cur["timestamp"])
        if bounds:
            group_end_seconds = bounds[1]

        while j + 1 < n:
            nxt = records[j + 1]
            if not (
                nxt["kind"] == cur["kind"]
                and nxt["old"] == cur["old"]
                and nxt["new"] == cur["new"]
                and nxt["note"] == cur["note"]
                and nxt["scale"] == cur["scale"]
                and nxt["line_no"] - records[j]["line_no"] <= MAX_LINE_GAP_FOR_GROUPING
            ):
                break

            nxt_bounds = parse_timestamp_bounds(nxt["timestamp"])
            if group_end_seconds is not None and nxt_bounds is not None:
                if nxt_bounds[0] > group_end_seconds + TIME_GROUPING_TOLERANCE_SECONDS:
                    break
                group_end_seconds = max(group_end_seconds, nxt_bounds[1])
            elif nxt["line_no"] != records[j]["line_no"] + 1:
                # No usable timestamp on one side -> fall back to requiring
                # strict adjacency so we don't over-merge blindly.
                break

            j += 1
        group = records[i:j + 1]
        i = j + 1

        if len(group) == 1:
            line_range = str(group[0]["line_no"] + 1)
        else:
            line_range = f"{group[0]['line_no'] + 1}-{group[-1]['line_no'] + 1}"
        count_note = f"  {c(f'(x{len(group)})', C.DIM)}" if len(group) > 1 else ""
        count_note_log = f"  (x{len(group)})" if len(group) > 1 else ""

        timestamps = [r["timestamp"] for r in group if r["timestamp"]]
        if not timestamps:
            ts_display = ""
        elif len(set(timestamps)) == 1:
            ts_display = timestamps[0]
        else:
            ts_display = f"{timestamps[0]} .. {timestamps[-1]}"
        ts_console = f"  {c(f'[{ts_display}]', C.GRAY)}" if ts_display else ""
        ts_log = f"  [{ts_display}]" if ts_display else ""

        scale = cur["scale"]
        width_note_console = f"  {c(f'[fscx {scale}%]', C.MAGENTA)}" if scale is not None else ""
        width_note_log = f"  [fscx {scale}%]" if scale is not None else ""

        if cur["kind"] == "MATCH":
            msg_console = (
                f"{c('[MATCH]', C.BOLD, C.GREEN)} {c(str(rel), C.CYAN)}:{line_range}{ts_console}"
                f"  {c(cur['note'], C.DIM)}{width_note_console}{count_note}\n"
                f"        {c('old:', C.RED)} {cur['old']}\n"
                f"        {c('new:', C.GREEN)} {cur['new']}"
            )
            msg_log = (
                f"[MATCH] {rel}:{line_range}{ts_log}  ({cur['note']}){width_note_log}{count_note_log}\n"
                f"        old: {cur['old']}\n"
                f"        new: {cur['new']}"
            )
        elif cur["kind"] == "ASKED":
            msg_console = (
                f"{c('[ASKED]', C.BOLD, C.YELLOW)} {c(str(rel), C.CYAN)}:{line_range}"
                f"{ts_console}{width_note_console}{count_note}\n"
                f"        {c('old:', C.RED)} {cur['old']}\n"
                f"        {c('new:', C.GREEN)} {cur['new']}"
            )
            msg_log = (
                f"[ASKED] {rel}:{line_range}{ts_log}{width_note_log}{count_note_log}\n"
                f"        old: {cur['old']}\n"
                f"        new: {cur['new']}"
            )
        else:  # SKIP
            msg_console = (
                f"{c('[SKIP]', C.BOLD, C.GRAY)}  {c(str(rel), C.CYAN)}:{line_range}{ts_console}"
                f"  {c(cur['note'], C.DIM)}{count_note}\n"
                f"        {c('line:', C.DIM)} {cur['old']}"
            )
            msg_log = (
                f"[SKIP]  {rel}:{line_range}{ts_log}  ({cur['note']}){count_note_log}\n"
                f"        line: {cur['old']}"
            )

        print(msg_console)
        log_lines.append(msg_log)


def cmd_plan_replace(args: argparse.Namespace) -> None:
    work_dir = Path(args.work_dir).resolve()
    extracted_root = work_dir / EXTRACTED_DIRNAME
    if not extracted_root.exists():
        sys.exit(f"No extracted subtitles found at {extracted_root}. Run the 'extract' stage first.")

    show_key = args.show.strip().lower()
    if show_key not in REPLACE_RULES:
        available = ", ".join(sorted(REPLACE_RULES))
        sys.exit(f"Unknown --show '{args.show}'. Available shows: {available}")
    show_config = REPLACE_RULES[show_key]

    replacements = dict(show_config["replacements"])
    width_compensated_rules = show_config["width_compensated_rules"]
    watch_word = show_config["watch_word"]
    if args.rules_file:
        extra = json.loads(Path(args.rules_file).read_text(encoding="utf-8"))
        replacements.update(extra)

    sub_files = sorted(
        p for p in extracted_root.rglob("*")
        if p.is_file() and p.suffix.lower() in TEXT_EDITABLE_EXTS
    )
    if not sub_files:
        sys.exit(f"No text-editable subtitle files found under {extracted_root}.")

    plan_entries: List[Dict[str, Any]] = []
    log_lines: List[str] = []
    session_watchword_answers: Dict[str, str] = {}  # exact text -> chosen replacement

    print(c(f"Scanning {len(sub_files)} subtitle file(s) for replacements...\n", C.BOLD))

    for sub_path in sub_files:
        rel = sub_path.relative_to(work_dir)
        text, encoding = read_text_smart(sub_path)
        lines = text.splitlines(keepends=False)
        ext = sub_path.suffix.lower()

        header = f"== {rel} =="
        print(c(header, C.BOLD, C.BLUE))
        log_lines.append(("\n" if log_lines else "") + header)

        records: List[Dict[str, Any]] = []

        for line_no, prefix, segment, suffix in iter_text_segments(lines, ext):
            is_ass = ext in (".ass", ".ssa")
            if is_ass:
                leading_tags, visible = split_leading_ass_tags(segment)
                search_text = strip_inline_ass_tags(visible)
            else:
                leading_tags, search_text = "", segment

            new_search_text, matched_keys = apply_known_replacements(search_text, replacements)
            watchword_hit = re.search(re.escape(watch_word), new_search_text, re.IGNORECASE)
            timestamp = get_timestamp(lines, line_no, ext)

            if matched_keys:
                width_tag = ""
                scale = None
                if is_ass and any(k in width_compensated_rules for k in matched_keys):
                    scale = compute_width_compensation(search_text, new_search_text)
                    if scale is not None:
                        width_tag = f"{{\\fscx{scale}}}"

                new_line = prefix + leading_tags + width_tag + new_search_text + suffix
                rules_str = ", ".join(f"'{k}' -> '{replacements[k]}'" for k in matched_keys)
                plan_entries.append(
                    {
                        "file": str(rel),
                        "line_no": line_no,
                        "old_line": lines[line_no],
                        "new_line": new_line,
                        "reason": "rule:" + ",".join(matched_keys),
                    }
                )
                records.append(
                    {
                        "kind": "MATCH",
                        "line_no": line_no,
                        "timestamp": timestamp,
                        "old": search_text.strip(),
                        "new": new_search_text.strip(),
                        "note": rules_str,
                        "scale": scale,
                    }
                )
                continue

            if watchword_hit:
                # Contains the show's watch word but not covered by a known rule -> ask the user.
                original_search_text = search_text
                if original_search_text in session_watchword_answers:
                    chosen = session_watchword_answers[original_search_text]
                else:
                    print(c(f"\n--- Unmapped line containing '{watch_word}' ---", C.BOLD, C.YELLOW))
                    print(f"{c('File:', C.DIM)} {rel}")
                    print(f"{c('Line:', C.DIM)} {line_no + 1}")
                    if timestamp:
                        print(f"{c('Time:', C.DIM)} {timestamp}")
                    print(f"{c('Text:', C.DIM)} {original_search_text.strip()}")
                    if is_ass and leading_tags:
                        print(c("(ASS override tags around this text are preserved automatically)", C.DIM))
                    chosen = input(
                        c("Replacement text (leave empty to skip this line): ", C.YELLOW)
                    ).strip()
                    session_watchword_answers[original_search_text] = chosen

                if not chosen:
                    records.append(
                        {
                            "kind": "SKIP",
                            "line_no": line_no,
                            "timestamp": timestamp,
                            "old": original_search_text.strip(),
                            "new": "",
                            "note": "(no replacement given)",
                            "scale": None,
                        }
                    )
                    continue

                width_tag = ""
                scale = None
                if is_ass:
                    scale = compute_width_compensation(original_search_text, chosen)
                    if scale is not None:
                        width_tag = f"{{\\fscx{scale}}}"

                new_line = prefix + leading_tags + width_tag + chosen + suffix
                plan_entries.append(
                    {
                        "file": str(rel),
                        "line_no": line_no,
                        "old_line": lines[line_no],
                        "new_line": new_line,
                        "reason": f"watchword:{watch_word}",
                    }
                )
                records.append(
                    {
                        "kind": "ASKED",
                        "line_no": line_no,
                        "timestamp": timestamp,
                        "old": original_search_text.strip(),
                        "new": chosen.strip(),
                        "note": "",
                        "scale": scale,
                    }
                )

        flush_match_records(rel, records, log_lines)
        print()

    plan_path = work_dir / PLAN_FILENAME
    plan_path.write_text(json.dumps(plan_entries, indent=2), encoding="utf-8")
    (work_dir / PLAN_LOG_FILENAME).write_text("\n".join(log_lines), encoding="utf-8")

    print(c(f"\nPlan complete: {len(plan_entries)} replacement(s) queued.", C.BOLD, C.GREEN))
    print(f"  Plan file: {plan_path}")
    print(f"  Log file:  {work_dir / PLAN_LOG_FILENAME}")


# --------------------------------------------------------------------------
# merge-replace
# --------------------------------------------------------------------------


def apply_replace_plan_to_files(work_dir: Path, plan_entries: List[Dict[str, Any]]) -> Dict[str, Path]:
    """
    Writes edited copies of every subtitle file touched by the plan into
    work_dir/EDITED_DIRNAME, preserving the extracted/ directory layout.
    Returns a map of relative-path-string -> edited file Path.
    """
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for entry in plan_entries:
        by_file.setdefault(entry["file"], []).append(entry)

    edited_root = work_dir / EDITED_DIRNAME
    edited_paths: Dict[str, Path] = {}

    for rel_str, entries in by_file.items():
        src_path = work_dir / rel_str
        text, encoding = read_text_smart(src_path)
        lines = text.splitlines(keepends=False)
        newline = "\r\n" if "\r\n" in text else "\n"

        applied, mismatched = 0, 0
        for e in entries:
            ln = e["line_no"]
            if 0 <= ln < len(lines) and lines[ln] == e["old_line"]:
                lines[ln] = e["new_line"]
                applied += 1
            else:
                mismatched += 1
                print(c(
                    f"  WARNING: {rel_str}:{ln + 1} no longer matches the planned "
                    "original text; skipping this replacement (re-run 'plan-replace' if the "
                    "subtitle files changed).", C.YELLOW
                ))

        dest_path = edited_root / rel_str
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(newline.join(lines) + (newline if text.endswith(("\n", "\r\n")) else ""),
                              encoding="utf-8")
        edited_paths[rel_str] = dest_path
        print(f"  {rel_str}: applied {applied}/{len(entries)} replacement(s)"
              + (f", {mismatched} skipped" if mismatched else ""))

    return edited_paths


def cmd_merge_replace(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    plan_path = work_dir / PLAN_FILENAME
    if not plan_path.exists():
        sys.exit(f"No plan file found at {plan_path}. Run the 'plan-replace' stage first.")
    plan_entries = json.loads(plan_path.read_text(encoding="utf-8"))

    if not plan_entries:
        print("Plan is empty - nothing to merge. Copying source files through unchanged is skipped; "
              "run 'extract'/'plan-replace' again if you expected replacements.")
        return

    print("Applying plan to subtitle files...")
    edited_paths = apply_replace_plan_to_files(work_dir, plan_entries)
    print()

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run)
