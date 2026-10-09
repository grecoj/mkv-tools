"""Stage: reposition -- adjust ASS/SSA coordinates/sizes for a resolution change (pad or crop).
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from .common import (
    C,
    DIALOGUE_STYLE_RE,
    EXTRACTED_DIRNAME,
    LAYOUTRESX_LINE_RE,
    LAYOUTRESY_LINE_RE,
    PLAYRESX_LINE_RE,
    PLAYRESY_LINE_RE,
    REPOSITIONED_DIRNAME,
    STYLE_FORMAT_LINE_RE,
    STYLE_LINE_RE,
    TRACKS_META_FILENAME,
    c,
    check_tools,
    find_ass_play_res,
    find_ass_wrap_style,
    fmt_num,
    get_timestamp,
    read_text_smart,
    rebuild_all_mkvs,
    track_matches_filter,
)


TAG_BLOCK_RE = re.compile(r"\{([^}]*)\}")


POS_TAG_RE = re.compile(r"\\pos\(\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*\)")


ORG_TAG_RE = re.compile(r"\\org\(\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*\)")


MOVE_ARGS_RE = re.compile(
    r"\\move\(\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*"
    r"(?:,\s*([\d.+-]+)\s*,\s*([\d.+-]+)\s*)?\)"
)


CLIP_FULL_RE = re.compile(r"\\(i?clip)\(([^)]*)\)")


CLIP_RECT_RE = re.compile(r"^[\d.+-]+(?:\s*,\s*[\d.+-]+){3}$")


CLIP_SCALE_PREFIX_RE = re.compile(r"^(\d+)\s*,\s*(.*)$", re.DOTALL)


FS_TAG_RE = re.compile(r"\\fs(?![cp])([\d.]+)")          # \fs<n>, not \fscx/\fscy/\fsp


BORD_TAG_RE = re.compile(r"\\(x?bord)([\d.]+)")


SHAD_TAG_RE = re.compile(r"\\(x?shad|y?shad)([\d.]+)")


FSP_TAG_RE = re.compile(r"\\fsp([\d.]+)")


# Style (v4+) fields that represent an absolute pixel *position* (need
# scale + offset) vs. a pixel *size* (need scale only).
STYLE_MARGIN_FIELDS_X = ("MarginL", "MarginR")


STYLE_MARGIN_FIELDS_Y = ("MarginV",)


STYLE_SIZE_FIELDS = ("Fontsize", "Outline", "Shadow", "Spacing")


def transform_clip_content(content: str, scale: float, offset_x: float, offset_y: float) -> Optional[str]:
    """
    Transform the argument list of a \\clip(...)/\\iclip(...) tag for a
    resolution change. Handles the two safe, unambiguous forms:
      - Rectangular: "x1,y1,x2,y2" -- transform both corner points.
      - Vector drawing path with NO leading scale argument, e.g.
        "m 100 100 l 200 100 200 200" -- every x/y in the path is in the
        same units as everything else, so each pair gets the same
        scale+offset transform as \\pos.
    A vector path WITH a leading drawing-scale argument (e.g.
    "\\clip(2,m 100 100 ...)") changes what unit the coordinates are in,
    which isn't safe to reinterpret generically, so that scale prefix is
    left untouched (only the coordinates after it are still transformed --
    they're still plain PlayRes-space coordinates regardless of the
    drawing scale, which only affects how finely curves are subdivided).
    Returns None if the content doesn't match either recognizable form
    (caller should treat this as "couldn't transform, leave as-is and warn").
    """
    stripped = content.strip()
    if not stripped:
        return None

    if CLIP_RECT_RE.match(stripped):
        parts = [p.strip() for p in stripped.split(",")]
        try:
            x1, y1, x2, y2 = (float(p) for p in parts)
        except ValueError:
            return None
        return (
            f"{fmt_num(x1 * scale + offset_x)},{fmt_num(y1 * scale + offset_y)},"
            f"{fmt_num(x2 * scale + offset_x)},{fmt_num(y2 * scale + offset_y)}"
        )

    scale_prefix = ""
    drawing = stripped
    m_scale = CLIP_SCALE_PREFIX_RE.match(stripped)
    if m_scale and re.search(r"[a-zA-Z]", m_scale.group(2)):
        scale_prefix = m_scale.group(1) + ","
        drawing = m_scale.group(2)

    if not re.search(r"[a-zA-Z]", drawing):
        return None  # not a drawing path and not a plain rect -- unrecognized form

    tokens = drawing.split()
    new_tokens: List[str] = []
    coord_index = 0
    for tok in tokens:
        try:
            val = float(tok)
        except ValueError:
            new_tokens.append(tok)  # drawing command letter (m/l/b/s/p/c...), unchanged
            continue
        if coord_index % 2 == 0:
            new_tokens.append(fmt_num(val * scale + offset_x))
        else:
            new_tokens.append(fmt_num(val * scale + offset_y))
        coord_index += 1

    if coord_index == 0 or coord_index % 2 != 0:
        return None  # no coordinates found, or an odd count -- malformed/unexpected

    return scale_prefix + " ".join(new_tokens)


def compute_letterbox_transform(
    old_res: Tuple[int, int], new_res: Tuple[int, int]
) -> Tuple[float, float, float]:
    """
    Given the subtitle's original resolution and the new video's
    resolution, compute (scale, offset_x, offset_y) assuming the original
    content is centered and letterboxed/pillarboxed (not cropped or
    stretched) to fit inside the new frame -- i.e. the standard "contain"
    fit. scale=1 and a pure offset (like your 1440x1080 -> 1920x1080
    pillarbox case) is the common special case where the new frame is
    simply padded without resizing the content at all.
    """
    old_w, old_h = old_res
    new_w, new_h = new_res
    scale = min(new_w / old_w, new_h / old_h)
    offset_x = (new_w - old_w * scale) / 2
    offset_y = (new_h - old_h * scale) / 2
    return scale, offset_x, offset_y


def compute_crop_transform(
    old_res: Tuple[int, int], content_res: Tuple[int, int], new_res: Tuple[int, int]
) -> Tuple[float, float, float]:
    """
    The opposite of compute_letterbox_transform: use this when the new
    video has padding REMOVED (cropped out) rather than added, e.g. going
    from a pillarboxed 1920x1080 frame back down to a 960x720 frame that
    holds just the actual content. `content_res` is the real content's
    size within the old (padded) frame -- content is assumed centered, as
    it is for any normal letterbox/pillarbox. The content rectangle is
    cropped out of old_res and rescaled to exactly fill new_res.

    Reuses the same (scale, offset_x, offset_y) representation as
    compute_letterbox_transform -- the offsets just come out negative
    here instead of positive, since coordinates are being shifted toward
    the frame origin (padding removed) rather than away from it.

    Raises ValueError if content_res's aspect ratio doesn't reasonably
    match new_res's -- that would mean new_res isn't actually "just the
    content, rescaled," and scale would have to differ in x vs y (this
    tool only supports uniform scaling, matching how \\fscx/\\fscy already
    work elsewhere in this script).
    """
    old_w, old_h = old_res
    content_w, content_h = content_res
    new_w, new_h = new_res
    scale_x = new_w / content_w
    scale_y = new_h / content_h
    if abs(scale_x - scale_y) > 0.01 * max(scale_x, scale_y):
        raise ValueError(
            f"--new-res {new_w}x{new_h} isn't the same aspect ratio as "
            f"--old-content-res {content_w}x{content_h} "
            f"(would need non-uniform x/y scaling: {scale_x:.4f} vs {scale_y:.4f})"
        )
    scale = (scale_x + scale_y) / 2
    crop_x = (old_w - content_w) / 2
    crop_y = (old_h - content_h) / 2
    offset_x = -crop_x * scale
    offset_y = -crop_y * scale
    return scale, offset_x, offset_y


def reposition_ass_lines(
    lines: List[str],
    scale: float,
    offset_x: float,
    offset_y: float,
    new_res: Tuple[int, int],
) -> Tuple[
    List[str], Dict[str, int], List[Tuple[int, str]], List[Tuple[int, str, str]],
    List[Tuple[str, str, float, float]], List[Tuple[int, str, float, float]],
    Dict[str, List[int]],
]:
    """
    Transform an ASS/SSA file's lines for a resolution change:
      - PlayResX/PlayResY and LayoutResX/LayoutResY headers -> new_res
        (kept in lockstep -- a stale LayoutRes makes libass apply its own,
        possibly non-uniform, extra auto-scale on top of this transform)
      - Style Fontsize/Outline/Shadow/Spacing -> scaled
      - Style MarginL/MarginR/MarginV -> scaled + offset, clamped to 0 (and
        flagged) if that comes out negative -- which happens when a crop
        removes more width/height than the original margin accounted for,
        meaning that style's word-wrapped text was relying on space that
        no longer exists in the new frame
      - inline \\pos/\\org -> scaled + offset
      - inline \\fs/\\bord/\\xbord/\\shad/\\xshad/\\yshad/\\fsp -> scaled
      - inline \\move(x1,y1,x2,y2[,t1,t2]) -> both points scaled + offset,
        timing (if present) left untouched
      - inline \\clip(...)/\\iclip(...) -> transformed via
        transform_clip_content (rectangular form, or a vector drawing path
        with no leading scale argument); left untouched with a warning if
        the content doesn't match either recognizable form

    Every transformed \\pos and \\move endpoint is also checked against the
    new frame bounds [0, new_res] -- points landing outside it (most often
    from a crop that removed the part of the frame a sign/typesetting
    element was anchored to) are reported rather than silently accepted,
    since there's no way to automatically "fix" a position or word-wrap
    that no longer fits -- that needs a human decision (re-wrap the text
    ourselves, re-position it, or accept it'll be partly off-frame).

    Returns (new_lines, counts, warnings, complex_changes, margin_clamps, out_of_bounds):
      - counts tallies how many of each tag type were transformed.
      - warnings is [(line_no, raw_line)] for \\move/\\clip/\\iclip content
        that couldn't be confidently parsed and was left unchanged.
      - complex_changes is [(line_no, old_line, new_line)] for every line
        where a \\move or \\clip/\\iclip WAS transformed -- these involve
        more moving parts than a plain \\pos, so callers (dry-run in
        particular) should surface the before/after for manual spot-checking
        after the real merge.
      - margin_clamps is [(style_name, field_name, old_value, negative_value)]
        for every style margin that came out negative and was clamped to 0.
      - out_of_bounds is [(line_no, tag, x, y)] for every transformed \\pos
        or \\move endpoint that landed outside the new frame.
      - style_usage is {style_name: [line_no, ...]} for every Dialogue/Comment
        line, letting callers report which specific lines use a style that
        showed up in margin_clamps.
    """
    counts = {"playres": 0, "style_lines": 0, "pos": 0, "org": 0, "sizes": 0, "move": 0, "clip": 0}
    warnings: List[Tuple[int, str]] = []
    complex_changes: List[Tuple[int, str, str]] = []
    margin_clamps: List[Tuple[str, str, float, float]] = []
    out_of_bounds: List[Tuple[int, str, float, float]] = []
    style_usage: Dict[str, List[int]] = {}
    new_lines: List[str] = []
    style_fields: Optional[List[str]] = None

    def check_bounds(line_no: int, tag: str, x: float, y: float) -> None:
        if x < 0 or x > new_res[0] or y < 0 or y > new_res[1]:
            out_of_bounds.append((line_no, tag, x, y))

    for line_no, line in enumerate(lines):
        mx = PLAYRESX_LINE_RE.match(line)
        if mx:
            new_lines.append(f"{mx.group(1)}{new_res[0]}")
            counts["playres"] += 1
            continue
        my = PLAYRESY_LINE_RE.match(line)
        if my:
            new_lines.append(f"{my.group(1)}{new_res[1]}")
            counts["playres"] += 1
            continue
        lx = LAYOUTRESX_LINE_RE.match(line)
        if lx:
            new_lines.append(f"{lx.group(1)}{new_res[0]}")
            counts["playres"] += 1
            continue
        ly = LAYOUTRESY_LINE_RE.match(line)
        if ly:
            new_lines.append(f"{ly.group(1)}{new_res[1]}")
            counts["playres"] += 1
            continue

        fmt_m = STYLE_FORMAT_LINE_RE.match(line)
        if fmt_m and style_fields is None:
            # Only meaningful right after a "[V4+ Styles]"/"[V4 Styles]" section
            # header, but tracking the most recent Format: line is a reasonable
            # proxy without needing full section-aware parsing.
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
                style_name = parts[field_index["Name"]] if "Name" in field_index else "?"

                def set_scaled(name: str) -> None:
                    idx = field_index.get(name)
                    if idx is None:
                        return
                    try:
                        parts[idx] = fmt_num(float(parts[idx]) * scale)
                    except ValueError:
                        pass

                def set_scaled_offset(name: str, offset: float) -> None:
                    idx = field_index.get(name)
                    if idx is None:
                        return
                    try:
                        old_val = float(parts[idx])
                    except ValueError:
                        return
                    new_val = old_val * scale + offset
                    if new_val < 0:
                        margin_clamps.append((style_name, name, old_val, new_val))
                        new_val = 0.0
                    parts[idx] = fmt_num(new_val)

                for name in STYLE_SIZE_FIELDS:
                    set_scaled(name)
                for name in STYLE_MARGIN_FIELDS_X:
                    set_scaled_offset(name, offset_x)
                for name in STYLE_MARGIN_FIELDS_Y:
                    set_scaled_offset(name, offset_y)

                new_lines.append(prefix + ",".join(parts))
                counts["style_lines"] += 1
                continue
            # Field count mismatch -- can't safely map, leave the style untouched.
            new_lines.append(line)
            continue

        if line.startswith("Dialogue:") or line.startswith("Comment:"):
            style_use_m = DIALOGUE_STYLE_RE.match(line)
            if style_use_m:
                style_usage.setdefault(style_use_m.group(1).strip(), []).append(line_no)

            had_move_or_clip = bool(MOVE_ARGS_RE.search(line) or CLIP_FULL_RE.search(line))
            line_had_failure = [False]  # mutable flag closures can set

            def repl_move(mm: "re.Match[str]") -> str:
                counts["move"] += 1
                x1 = float(mm.group(1)) * scale + offset_x
                y1 = float(mm.group(2)) * scale + offset_y
                x2 = float(mm.group(3)) * scale + offset_x
                y2 = float(mm.group(4)) * scale + offset_y
                check_bounds(line_no, "move (start)", x1, y1)
                check_bounds(line_no, "move (end)", x2, y2)
                if mm.group(5) is not None and mm.group(6) is not None:
                    return f"\\move({fmt_num(x1)},{fmt_num(y1)},{fmt_num(x2)},{fmt_num(y2)},{mm.group(5)},{mm.group(6)})"
                return f"\\move({fmt_num(x1)},{fmt_num(y1)},{fmt_num(x2)},{fmt_num(y2)})"

            def repl_clip(mm: "re.Match[str]") -> str:
                tag_name, content = mm.group(1), mm.group(2)
                new_content = transform_clip_content(content, scale, offset_x, offset_y)
                if new_content is None:
                    line_had_failure[0] = True
                    return mm.group(0)  # leave this specific tag unchanged
                counts["clip"] += 1
                return f"\\{tag_name}({new_content})"

            def repl_block(m: "re.Match[str]") -> str:
                content = m.group(1)

                def repl_pos(mm: "re.Match[str]") -> str:
                    counts["pos"] += 1
                    x = float(mm.group(1)) * scale + offset_x
                    y = float(mm.group(2)) * scale + offset_y
                    check_bounds(line_no, "pos", x, y)
                    return f"\\pos({fmt_num(x)},{fmt_num(y)})"

                def repl_org(mm: "re.Match[str]") -> str:
                    counts["org"] += 1
                    x = float(mm.group(1)) * scale + offset_x
                    y = float(mm.group(2)) * scale + offset_y
                    return f"\\org({fmt_num(x)},{fmt_num(y)})"

                def repl_size(mm: "re.Match[str]", tag: str, has_prefix: bool = False) -> str:
                    counts["sizes"] += 1
                    if has_prefix:
                        val = float(mm.group(2)) * scale
                        return f"\\{mm.group(1)}{fmt_num(val)}"
                    val = float(mm.group(1)) * scale
                    return f"\\{tag}{fmt_num(val)}"

                content = POS_TAG_RE.sub(repl_pos, content)
                content = ORG_TAG_RE.sub(repl_org, content)
                content = MOVE_ARGS_RE.sub(repl_move, content)
                content = CLIP_FULL_RE.sub(repl_clip, content)
                content = FS_TAG_RE.sub(lambda mm: repl_size(mm, "fs"), content)
                content = BORD_TAG_RE.sub(lambda mm: repl_size(mm, "", has_prefix=True), content)
                content = SHAD_TAG_RE.sub(lambda mm: repl_size(mm, "", has_prefix=True), content)
                content = FSP_TAG_RE.sub(lambda mm: repl_size(mm, "fsp"), content)
                return "{" + content + "}"

            new_line = TAG_BLOCK_RE.sub(repl_block, line)

            if line_had_failure[0]:
                warnings.append((line_no, line))
            elif had_move_or_clip and new_line != line:
                complex_changes.append((line_no, line, new_line))

            new_lines.append(new_line)
            continue

        new_lines.append(line)

    return new_lines, counts, warnings, complex_changes, margin_clamps, out_of_bounds, style_usage


def cmd_reposition(args: argparse.Namespace) -> None:
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

    new_res = args.new_res
    forced_old_res = args.old_res

    repositioned_root = work_dir / REPOSITIONED_DIRNAME
    edited_paths: Dict[str, Path] = {}
    any_repositioned = False
    total_warnings: List[Tuple[str, int, str]] = []
    total_complex_changes: List[Tuple[str, int, str, str]] = []
    total_margin_clamps: List[Tuple[str, str, str, float, float, int, str, str, str, Optional[int]]] = []
    total_out_of_bounds: List[Tuple[str, int, str, float, float]] = []

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
                print(f"  track {t['id']} ({label}): {ext} has no absolute positioning to adjust, skipped.")
                continue

            text, encoding = read_text_smart(src_path)
            newline = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines(keepends=False)

            old_res = forced_old_res or find_ass_play_res(lines)
            if old_res is None:
                print(c(f"  track {t['id']} ({label}): no PlayResX/PlayResY found and no --old-res "
                      "given; skipped.", C.YELLOW))
                continue
            wrap_style = find_ass_wrap_style(lines)

            if args.old_content_res:
                try:
                    scale, offset_x, offset_y = compute_crop_transform(old_res, args.old_content_res, new_res)
                except ValueError as e:
                    print(c(f"  track {t['id']} ({label}): {e}; skipped.", C.YELLOW))
                    continue
            else:
                scale, offset_x, offset_y = compute_letterbox_transform(old_res, new_res)
            new_lines, counts, warnings, complex_changes, margin_clamps, out_of_bounds, style_usage = reposition_ass_lines(
                lines, scale, offset_x, offset_y, new_res
            )

            for line_no, raw_line in warnings:
                total_warnings.append((rel, line_no + 1, raw_line))
            for line_no, old_line, new_line in complex_changes:
                total_complex_changes.append((rel, line_no + 1, old_line, new_line))

            # Merge MarginL/MarginR clamps for the same style into one entry
            # when their values match (always true for a symmetric crop),
            # and drop clamps for styles nothing actually uses -- both are
            # pure noise, not information loss.
            merged_clamps: Dict[Tuple[str, float, float], List[str]] = {}
            for style_name, field_name, old_val, neg_val in margin_clamps:
                if not style_usage.get(style_name):
                    continue
                merged_clamps.setdefault((style_name, old_val, neg_val), []).append(field_name)

            for (style_name, old_val, neg_val), field_names in merged_clamps.items():
                affected = sorted(style_usage[style_name])
                line_range = f"{affected[0] + 1}-{affected[-1] + 1}" if len(affected) > 1 else str(affected[0] + 1)
                ts_first = get_timestamp(lines, affected[0], ext)
                ts_last = get_timestamp(lines, affected[-1], ext)
                total_margin_clamps.append((
                    rel, style_name, "/".join(field_names), old_val, neg_val,
                    len(affected), line_range, ts_first, ts_last, wrap_style,
                ))
            for line_no, tag, x, y in out_of_bounds:
                total_out_of_bounds.append((rel, line_no + 1, tag, x, y))

            if warnings:
                print(c(f"  track {t['id']} ({label}): {len(warnings)} line(s) with \\move/\\clip/\\iclip "
                      "-- NOT adjusted, needs manual review (see warnings below).", C.YELLOW))
            if complex_changes:
                print(c(f"  track {t['id']} ({label}): {len(complex_changes)} \\move/\\clip/\\iclip line(s) "
                      "adjusted -- worth spot-checking (see list below).", C.MAGENTA))
            if margin_clamps:
                any_no_wrap = wrap_style == 2
                overflow_note = (
                    "text can genuinely extend past the frame edges (WrapStyle 2, no auto-wrap)"
                    if any_no_wrap else
                    "text will still auto-wrap but onto more lines than before"
                )
                print(c(f"  track {t['id']} ({label}): {len(margin_clamps)} style margin(s) went negative "
                      f"and were clamped to 0 (see list below) -- {overflow_note}.", C.YELLOW))
            if out_of_bounds:
                print(c(f"  track {t['id']} ({label}): {len(out_of_bounds)} \\pos/\\move point(s) landed "
                      "outside the new frame (see list below).", C.YELLOW))

            transformed_anything = counts["style_lines"] or counts["pos"] or counts["org"] or counts["sizes"] or counts["move"] or counts["clip"]
            if not transformed_anything and not warnings:
                print(f"  track {t['id']} ({label}): nothing to adjust.")
                continue

            print(c(
                f"  track {t['id']} ({label}): {old_res[0]}x{old_res[1]} -> {new_res[0]}x{new_res[1]}"
                f"  (scale {scale:.3f}, offset {offset_x:+.1f},{offset_y:+.1f})"
                f"  -- {counts['style_lines']} style(s), {counts['pos']} \\pos, {counts['org']} \\org, "
                f"{counts['move']} \\move, {counts['clip']} \\clip/\\iclip, {counts['sizes']} size tag(s) adjusted.",
                C.GREEN,
            ))

            new_text = newline.join(new_lines) + (newline if text.endswith(("\n", "\r\n")) else "")
            dest_path = repositioned_root / rel
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(new_text, encoding="utf-8")
            edited_paths[rel] = dest_path
            any_repositioned = True
        print()

    if args.condensed:
        files_with_items: Dict[str, Dict[str, Any]] = {}

        def file_entry(rel: str) -> Dict[str, Any]:
            return files_with_items.setdefault(rel, {"complex": 0, "clamped_styles": set(), "oob": 0, "warn": 0})

        for rel, *_ in total_complex_changes:
            file_entry(rel)["complex"] += 1
        for rel, style_name, *_ in total_margin_clamps:
            file_entry(rel)["clamped_styles"].add(style_name)
        for rel, *_ in total_out_of_bounds:
            file_entry(rel)["oob"] += 1
        for rel, *_ in total_warnings:
            file_entry(rel)["warn"] += 1

        if files_with_items:
            print(c(f"\n{len(files_with_items)} file(s) have items worth reviewing:", C.BOLD, C.YELLOW))
            for rel in sorted(files_with_items):
                s = files_with_items[rel]
                parts = []
                if s["complex"]:
                    parts.append(f"{s['complex']} \\move/\\clip adjusted")
                if s["clamped_styles"]:
                    parts.append(f"{len(s['clamped_styles'])} style(s) clamped ({', '.join(sorted(s['clamped_styles']))})")
                if s["oob"]:
                    parts.append(f"{s['oob']} pos/move out of bounds")
                if s["warn"]:
                    parts.append(f"{s['warn']} unparseable move/clip (needs manual review)")
                print(c(f"  {rel}", C.CYAN) + ": " + "; ".join(parts))
            print()
    else:
        if total_complex_changes:
            print(c(
                f"\n{len(total_complex_changes)} \\move/\\clip/\\iclip line(s) were adjusted -- "
                "spot-check these after merging (timed motion and drawn masks have more moving parts "
                "than a plain \\pos):",
                C.BOLD, C.MAGENTA,
            ))
            for rel, line_no, old_line, new_line in total_complex_changes:
                print(c(f"  {rel}:{line_no}", C.CYAN))
                print(f"    {c('old:', C.RED)} {old_line.strip()}")
                print(f"    {c('new:', C.GREEN)} {new_line.strip()}")
            print()

        if total_margin_clamps:
            no_wrap_present = any(ws == 2 for *_, ws in total_margin_clamps)
            if no_wrap_present:
                header = (
                    f"\n{len(total_margin_clamps)} style margin(s) went negative after the transform and were "
                    "clamped to 0. At least one affected file has WrapStyle 2 (word-wrap disabled), where this "
                    "genuinely means long lines in that style can extend past the frame edges with no automatic "
                    "wrapping to catch them -- worth checking closely for those files (noted below):"
                )
            else:
                header = (
                    f"\n{len(total_margin_clamps)} style margin(s) went negative after the transform and were "
                    "clamped to 0. All affected files auto-wrap (WrapStyle 0/1/3), so text will still break onto "
                    "more lines automatically rather than run off the frame edges -- but since the effective wrap "
                    "width shrank a lot, captions may now wrap onto noticeably more lines than originally "
                    "authored (taller, more cramped blocks), which is worth a visual spot-check:"
                )
            print(c(header, C.BOLD, C.YELLOW))
            for rel, style_name, field_name, old_val, neg_val, count, line_range, ts_first, ts_last, wrap_style in total_margin_clamps:
                ts_note = ""
                if ts_first and ts_last:
                    ts_note = f"  [{ts_first}]" if ts_first == ts_last else f"  [{ts_first} .. {ts_last}]"
                wrap_note = "  [WrapStyle 2: NO auto-wrap]" if wrap_style == 2 else ""
                print(
                    c(f"  {rel}", C.CYAN)
                    + f"  style '{style_name}' {field_name}: {fmt_num(old_val)} -> would be {fmt_num(neg_val)}, clamped to 0"
                    + c(wrap_note, C.RED)
                )
                print(f"      affects {count} line(s): {line_range}{ts_note}")
            print()

        if total_out_of_bounds:
            print(c(
                f"\n{len(total_out_of_bounds)} \\pos/\\move point(s) landed outside the new "
                f"{new_res[0]}x{new_res[1]} frame after the transform -- most often this means a sign/"
                "typesetting element was anchored to a part of the picture that got cropped away. These "
                "still need a manual decision (reposition, or accept it'll render partly/fully off-frame):",
                C.BOLD, C.YELLOW,
            ))
            for rel, line_no, tag, x, y in total_out_of_bounds:
                print(c(f"  {rel}:{line_no}", C.CYAN) + f"  \\{tag}: ({fmt_num(x)}, {fmt_num(y)})")
            print()

        if total_warnings:
            print(c(f"\n{len(total_warnings)} line(s) need manual review (contain \\move/\\clip/\\iclip "
                  "that couldn't be parsed and were left unchanged):", C.BOLD, C.YELLOW))
            for rel, line_no, raw_line in total_warnings:
                print(c(f"  {rel}:{line_no}", C.CYAN))
                print(f"    {raw_line.strip()}")
            print()

    if not any_repositioned:
        print(c("No subtitle tracks needed repositioning.", C.BOLD, C.GREEN))
        return

    rebuild_all_mkvs(work_dir, input_dir, output_dir, edited_paths, args.in_place, args.dry_run)
