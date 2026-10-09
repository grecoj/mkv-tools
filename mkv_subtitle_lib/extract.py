"""Stage: extract -- pull every subtitle track (plus its metadata) out of each mkv.
"""

from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List
from .common import (
    C,
    EXTRACTED_DIRNAME,
    TEXT_EDITABLE_EXTS,
    TRACKS_META_FILENAME,
    c,
    check_tools,
    find_mkvs,
    probe,
    run,
)


# codec_id (from `mkvmerge -J`) -> file extension for extraction.
CODEC_EXTENSIONS: Dict[str, str] = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASCII": ".srt",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/WEBVTT": ".vtt",
    "S_TEXT/USF": ".usf",
    "S_VOBSUB": ".sub",       # image-based, paired with .idx
    "S_HDMV/PGS": ".sup",     # image-based
    "S_HDMV/TEXTST": ".sup",
    "S_KATE": ".ogg",
}


def cmd_extract(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    work_dir = Path(args.work_dir).resolve()
    out_root = work_dir / EXTRACTED_DIRNAME
    out_root.mkdir(parents=True, exist_ok=True)

    mkvs = find_mkvs(input_dir)
    print(f"Found {len(mkvs)} mkv file(s) in {input_dir}\n")

    for mkv in mkvs:
        print(f"Probing {mkv.name} ...")
        info = probe(mkv)
        sub_tracks = [t for t in info.get("tracks", []) if t.get("type") == "subtitles"]

        if not sub_tracks:
            print("  no subtitle tracks, skipping.\n")
            continue

        dest_dir = out_root / mkv.stem
        dest_dir.mkdir(parents=True, exist_ok=True)

        extract_args: List[str] = []
        tracks_meta: List[Dict[str, Any]] = []

        for t in sub_tracks:
            tid = t["id"]
            props = t.get("properties", {})
            codec_id = props.get("codec_id", "")
            ext = CODEC_EXTENSIONS.get(codec_id, f".track{tid}.bin")
            lang = props.get("language_ietf") or props.get("language") or "und"
            name = props.get("track_name", "")
            safe_name = re.sub(r"[^\w.-]+", "_", name)[:40] if name else ""
            fname = f"track{tid}_{lang}" + (f"_{safe_name}" if safe_name else "") + ext
            out_path = dest_dir / fname

            extract_args.append(f"{tid}:{out_path}")
            tracks_meta.append(
                {
                    "id": tid,
                    "codec_id": codec_id,
                    "codec": t.get("codec"),
                    "language": props.get("language", "und"),
                    "language_ietf": props.get("language_ietf"),
                    "track_name": props.get("track_name", ""),
                    "default_track": bool(props.get("default_track", False)),
                    "forced_track": bool(props.get("forced_track", False)),
                    "enabled_track": bool(props.get("enabled_track", True)),
                    "file": str(out_path.relative_to(work_dir)),
                    "editable": ext in TEXT_EDITABLE_EXTS,
                }
            )

            flags = []
            if props.get("default_track"):
                flags.append("default")
            if props.get("forced_track"):
                flags.append("forced")
            flags_note = f"  [{', '.join(flags)}]" if flags else ""
            editable_note = "" if ext in TEXT_EDITABLE_EXTS else c("  (not text-editable)", C.YELLOW)
            print(
                f"  {c(f'id {tid}', C.BOLD, C.CYAN)}  "
                f"{name or c('(unnamed)', C.DIM)}  "
                f"{c(f'[{lang}]', C.DIM)}  "
                f"{t.get('codec', codec_id)}{flags_note}{editable_note}"
            )

        run(["mkvextract", "tracks", str(mkv), *extract_args])

        meta_payload = {
            "source_mkv": str(mkv.resolve()),
            "source_name": mkv.name,
            "title": info.get("container", {}).get("properties", {}).get("title", ""),
            "tracks": tracks_meta,
        }
        (dest_dir / TRACKS_META_FILENAME).write_text(json.dumps(meta_payload, indent=2), encoding="utf-8")

        editable = sum(1 for m in tracks_meta if m["editable"])
        skipped = len(tracks_meta) - editable
        print(f"  extracted {len(tracks_meta)} subtitle track(s) "
              f"({editable} text-editable, {skipped} image/binary - not editable).\n")

    print(f"Done. Extracted subtitles are under: {out_root}")
