"""Stage: fonts -- font tooling for mkv subtitles.

Commands:
  check-fonts  find ASS/SSA fonts that are neither attached nor installed
  dump-fonts   extract every unique attached font from a folder of mkvs
  add-fonts    attach fonts from the fonts folder that the subs use but the mkv lacks

Requires fontTools (pip install fonttools).
"""

from __future__ import annotations
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from fontTools.ttLib import TTCollection, TTFont
from .common import DEFAULT_FONTS_DIRNAME, C, c, check_tools, find_mkvs, probe

SSA_CODECS = {"S_TEXT/ASS": ".ass", "S_TEXT/SSA": ".ssa"}
FONT_EXTS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2"}
FONT_MIMES = ("font/", "application/x-truetype-font", "application/vnd.ms-opentype",
              "application/x-font", "application/font", "application/x-opentype")
FN_RE = re.compile(r"\\fn([^\\}]*)")
OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def norm(name: str) -> str:
    return name.strip().lstrip("@").strip().lower()


def font_attachments(info: dict) -> List[dict]:
    """Return the attachments in a `mkvmerge -J` result that are font files."""
    return [a for a in info.get("attachments", [])
            if Path(a.get("file_name", "")).suffix.lower() in FONT_EXTS
            or a.get("content_type", "").lower().startswith(FONT_MIMES)]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def font_names_from_file(path: Path) -> Set[str]:
    """Return every family/full/postscript name a font file answers to."""
    if path.suffix.lower() in (".ttc", ".otc"):
        fonts = TTCollection(str(path), lazy=True).fonts
    else:
        fonts = [TTFont(str(path), lazy=True)]
    names: Set[str] = set()
    for f in fonts:
        for rec in f["name"].names:
            if rec.nameID in (1, 4, 6, 16):
                names.add(norm(rec.toUnicode(errors="replace")))
    names.discard("")
    return names


def default_fonts_dir() -> Path:
    """The fonts library next to the launcher (sibling of the mkv_subtitle_lib folder)."""
    return Path(__file__).resolve().parent.parent / DEFAULT_FONTS_DIRNAME


INDEX_NAME = ".font_index.json"
INDEX_VERSION = 2   # bump when the cached contents change; older caches are discarded


def scan_library(folder: Path, names: bool = True, hashes: bool = False) -> Dict[str, dict]:
    """Index the font files in a folder, caching results in <folder>/.font_index.json.

    A file is only re-read when its size or modification time changed, so after the
    first run this is just a directory walk. Returns {relative path: {size, mtime,
    names?, sha256?}} with `names` / `sha256` filled in when requested.
    """
    try:
        cached = json.loads((folder / INDEX_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = {}
    old = cached.get("files", {}) if cached.get("version") == INDEX_VERSION else {}

    files = [p for p in sorted(folder.rglob("*")) if p.is_file() and p.suffix.lower() in FONT_EXTS]
    index: Dict[str, dict] = {}
    for p in files:
        rel = p.relative_to(folder).as_posix()
        st = p.stat()
        e = old.get(rel)
        if e and e.get("size") == st.st_size and e.get("mtime") == st.st_mtime_ns:
            index[rel] = e
        else:
            index[rel] = {"size": st.st_size, "mtime": st.st_mtime_ns}

    todo = [(rel, e) for rel, e in index.items()
            if (names and "names" not in e) or (hashes and "sha256" not in e)]
    if todo:
        print(f"  indexing {len(todo)} new/changed font file(s) in {folder.name} (cached after this run) ...")
        for i, (rel, e) in enumerate(todo, 1):
            path = folder / rel
            if names and "names" not in e:
                try:
                    e["names"] = sorted(font_names_from_file(path))
                except Exception as exc:
                    raise RuntimeError(f"could not read font file {path}: {exc}") from exc
            if hashes and "sha256" not in e:
                e["sha256"] = sha256_of(path)
            if i % 100 == 0 and i != len(todo):
                print(f"    {i}/{len(todo)}")

    if todo or set(old) != set(index):
        try:
            (folder / INDEX_NAME).write_text(json.dumps({"version": INDEX_VERSION, "files": index}), encoding="utf-8")
        except OSError:
            pass  # read-only folder: just skip caching
    return index


def library_fonts(folder: Path) -> Set[str]:
    """Every font name answered by the font files in a folder."""
    found: Set[str] = set()
    for e in scan_library(folder).values():
        found |= set(e["names"])
    return found


def print_available(from_lib: Dict[str, Set[str]], from_system: Dict[str, Set[str]]) -> None:
    if from_lib:
        print(f"      {c('in fonts folder, not attached', C.DIM)}: {', '.join(sorted(from_lib))}")
    if from_system:
        print(f"      {c('installed, not attached', C.DIM)}: {', '.join(sorted(from_system))}")


def system_fonts() -> Set[str]:
    out = subprocess.run(["fc-list", ":", "family", "fullname"], capture_output=True, text=True).stdout
    names: Set[str] = set()
    for line in out.splitlines():
        for part in re.split(r"[,:]", line):
            part = part.replace("family=", "").replace("fullname=", "").strip()
            if part:
                names.add(norm(part))
    return names


TAG_RE = re.compile(r"\\(?:fn(?P<fn>[^\\}]*)|r(?P<r>[^\\}]*)|p(?P<p>\d+)(?![A-Za-z]))")
ESC_RE = re.compile(r"\\[Nnh]")


def parse_ass(path: Path) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Parse an ASS/SSA script into ({style name: font name}, [(style, text) per Dialogue line])."""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    style_fonts: Dict[str, str] = {}
    events: List[Tuple[str, str]] = []
    section = ""
    style_fmt: List[str] = []
    event_fmt: List[str] = []

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.lower()
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if "styles" in section:
            if key == "format":
                style_fmt = [x.strip().lower() for x in val.split(",")]
            elif key == "style" and style_fmt:
                parts = val.split(",", len(style_fmt) - 1)
                row = dict(zip(style_fmt, parts))
                style_fonts[row.get("name", "").strip()] = row.get("fontname", "").strip()
        elif section == "[events]":
            if key == "format":
                event_fmt = [x.strip().lower() for x in val.split(",")]
            elif key == "dialogue" and event_fmt:
                parts = val.split(",", len(event_fmt) - 1)
                row = dict(zip(event_fmt, parts))
                events.append((row.get("style", "").strip().lstrip("*"), row.get("text", "")))
    return style_fonts, events


def fonts_used_in_script(path: Path) -> Dict[str, Set[str]]:
    """Return {font name: {where it's used}} for styles used by events plus inline \\fn overrides."""
    style_fonts, events = parse_ass(path)
    used_styles = {style for style, _ in events}
    inline: Set[str] = set()
    for _, text in events:
        for block in OVERRIDE_RE.findall(text):
            for m in FN_RE.findall(block):
                if m.strip():
                    inline.add(m.strip())

    result: Dict[str, Set[str]] = {}
    for style in used_styles:
        font = style_fonts.get(style) or style_fonts.get(style.lstrip("*"))
        if font:
            result.setdefault(font, set()).add(f"style '{style}'")
    for font in inline:
        result.setdefault(font, set()).add("inline \\fn")
    return result


def chars_by_font(path: Path) -> Dict[str, Set[str]]:
    """Return {font name: characters actually rendered with it}.

    Follows each line's style font through inline \\fn / \\r overrides, and skips
    vector drawing commands (\\p) and whitespace.
    """
    style_fonts, events = parse_ass(path)
    out: Dict[str, Set[str]] = {}

    def add(font: str, segment: str) -> None:
        seg = ESC_RE.sub(" ", segment)
        found = {ch for ch in seg if ch.isprintable() and not ch.isspace()}
        if font and found:
            out.setdefault(font, set()).update(found)

    for style, text in events:
        base = style_fonts.get(style, "")
        font, drawing, pos = base, False, 0
        for m in OVERRIDE_RE.finditer(text):
            if not drawing:
                add(font, text[pos:m.start()])
            for t in TAG_RE.finditer(m.group(0)):
                if t.group("fn") is not None:
                    font = t.group("fn").strip() or base
                elif t.group("r") is not None:
                    font = style_fonts.get(t.group("r").strip(), base) if t.group("r").strip() else base
                    drawing = False
                elif t.group("p") is not None:
                    drawing = int(t.group("p")) > 0
            pos = m.end()
        if not drawing:
            add(font, text[pos:])
    return out


def cmd_check_fonts(args: argparse.Namespace) -> None:
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    mkvs = find_mkvs(input_dir)
    print(f"Found {len(mkvs)} mkv file(s) in {input_dir}\n")

    check_system = getattr(args, "check_system", False)
    sys_fonts = system_fonts() if check_system else set()
    if check_system:
        if sys_fonts:
            print(f"--check-system: {len(sys_fonts)} installed font name(s) detected via fc-list.\n")
        else:
            print(c("--check-system: no installed fonts detected (fc-list missing or returned nothing), "
                    "so it has no effect.\n", C.YELLOW))

    explicit_dir = getattr(args, "fonts_dir", None)
    fonts_dir = Path(explicit_dir).resolve() if explicit_dir else default_fonts_dir()
    lib_fonts: Set[str] = set()
    if fonts_dir.is_dir():
        lib_fonts = library_fonts(fonts_dir)
        print(f"Fonts folder {fonts_dir}: {len(lib_fonts)} font name(s) available.\n")
    elif explicit_dir:
        print(c(f"Fonts folder {fonts_dir} not found; ignoring.\n", C.YELLOW))
    else:
        print(c(f"No fonts folder at {fonts_dir} yet (dump-fonts creates it).\n", C.DIM))

    problems: List[Tuple[str, str]] = []
    system_only: Set[str] = set()
    library_only: Set[str] = set()

    for mkv in mkvs:
        print(f"Checking {mkv.name} ...")
        info = probe(mkv)
        sub_tracks = [t for t in info.get("tracks", [])
                      if t.get("type") == "subtitles"
                      and t.get("properties", {}).get("codec_id") in SSA_CODECS]
        if not sub_tracks:
            print("  no ASS/SSA tracks, skipping.\n")
            continue

        attachments = font_attachments(info)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)

            # attached fonts -> set of names they answer to
            attached: Set[str] = set()
            if attachments:
                att_args = []
                for a in attachments:
                    att_args.append(f"{a['id']}:{tmp_dir / ('att%d_%s' % (a['id'], Path(a['file_name']).name))}")
                subprocess.run(["mkvextract", "attachments", str(mkv), *att_args],
                               capture_output=True, text=True)
                for p in tmp_dir.glob("att*"):
                    attached |= font_names_from_file(p)
            print(f"  {len(attachments)} attached font file(s)")

            # subtitle tracks -> fonts used
            for t in sub_tracks:
                tid = t["id"]
                props = t.get("properties", {})
                ext = SSA_CODECS[props["codec_id"]]
                script = tmp_dir / f"track{tid}{ext}"
                subprocess.run(["mkvextract", "tracks", str(mkv), f"{tid}:{script}"],
                               capture_output=True, text=True)
                label = f"track {tid} [{props.get('language', 'und')}] {props.get('track_name', '')}".rstrip()
                used = fonts_used_in_script(script) if script.exists() else {}

                missing = {f: w for f, w in used.items() if norm(f) not in attached}
                from_lib = {f: w for f, w in missing.items() if norm(f) in lib_fonts}
                missing = {f: w for f, w in missing.items() if f not in from_lib}
                from_system = {f: w for f, w in missing.items() if norm(f) in sys_fonts}
                missing = {f: w for f, w in missing.items() if f not in from_system}
                library_only |= set(from_lib)
                system_only |= set(from_system)

                if not missing and not from_lib:
                    print(f"  {c('OK', C.BOLD, C.CYAN)}  {label}  ({len(used)} font(s) used)")
                    print_available(from_lib, from_system)
                    continue
                if not missing:
                    print(f"  {c('UNATTACHED', C.BOLD, C.YELLOW)}  {label}")
                    print_available(from_lib, from_system)
                    continue
                print(f"  {c('MISSING', C.BOLD, C.YELLOW)}  {label}")
                for font, where in sorted(missing.items()):
                    print(f"      {font}  <- {', '.join(sorted(where))}")
                    problems.append((mkv.name, font))
                print_available(from_lib, from_system)
        print()

    unique = sorted({f for _, f in problems}, key=str.lower)
    affected = len({m for m, _ in problems})
    if problems:
        print(f"Done. {len(unique)} unique missing font(s) across {affected} file(s):")
        for f in unique:
            print(f"  - {f}")
    else:
        print("Done. No missing fonts found.")
    if library_only:
        print(f"\n{len(library_only)} font(s) are not attached to the mkv but are available "
              f"in the fonts folder:")
        for f in sorted(library_only, key=str.lower):
            print(f"  - {f}")
    if system_only:
        print(f"\n{len(system_only)} font(s) are not attached but are installed on this machine "
              f"(they'll break on other players):")
        for f in sorted(system_only, key=str.lower):
            print(f"  - {f}")


def cmd_dump_fonts(args: argparse.Namespace) -> None:
    """Extract every unique font attachment from each mkv into one folder."""
    check_tools()
    input_dir = Path(args.input_dir).resolve()
    if args.output_dir:
        out_dir = Path(args.output_dir).resolve()
    else:
        out_dir = default_fonts_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    mkvs = find_mkvs(input_dir)
    print(f"Found {len(mkvs)} mkv file(s) in {input_dir}")
    print(f"Writing unique fonts to {out_dir}\n")

    # Hash anything already in the output folder so repeat runs don't re-add fonts.
    seen: Dict[str, Path] = {}
    for rel, e in scan_library(out_dir, names=False, hashes=True).items():
        seen[e["sha256"]] = out_dir / rel
    already = len(seen)
    new_total = dup_total = 0

    for mkv in mkvs:
        print(f"Scanning {mkv.name} ...")
        atts = font_attachments(probe(mkv))
        if not atts:
            print("  no attached fonts, skipping.\n")
            continue

        new_here: List[str] = []
        dup_here = 0
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            att_args = [f"{a['id']}:{tmp_dir / ('att%d' % a['id'])}" for a in atts]
            subprocess.run(["mkvextract", "attachments", str(mkv), *att_args],
                           capture_output=True, text=True)

            for a in atts:
                src = tmp_dir / f"att{a['id']}"
                if not src.exists():
                    print(c(f"  could not extract attachment {a['id']} ({a.get('file_name', '?')})", C.YELLOW))
                    continue
                digest = sha256_of(src)
                if digest in seen:
                    dup_here += 1
                    continue
                name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(a.get("file_name", "")).name) or f"font{a['id']}.ttf"
                dest = out_dir / name
                if dest.exists():  # same filename, different content (e.g. another version)
                    dest = out_dir / f"{dest.stem}_{digest[:8]}{dest.suffix}"
                shutil.copy2(src, dest)
                seen[digest] = dest
                new_here.append(dest.name)

        new_total += len(new_here)
        dup_total += dup_here
        print(f"  {len(atts)} attached font file(s): {len(new_here)} new, {dup_here} already have")
        for n in new_here:
            print(f"      {c('+', C.BOLD, C.CYAN)} {n}")
        print()

    print(f"Done. {new_total} new font file(s) written to {out_dir} "
          f"({already} already there, {dup_total} duplicate(s) skipped).")


EXT_RANK = {".ttf": 0, ".otf": 1, ".ttc": 2, ".otc": 3, ".woff": 4, ".woff2": 5}
HASH_SUFFIX_RE = re.compile(r"_[0-9a-f]{8}$")
WIDTH_TOLERANCE = 0.01   # em; smaller advance-width differences are treated as identical


def font_faces(path: Path) -> List[dict]:
    """Describe each face in a font file: its name sets (by nameID) and bold/italic flags."""
    if path.suffix.lower() in (".ttc", ".otc"):
        fonts = TTCollection(str(path), lazy=True).fonts
    else:
        fonts = [TTFont(str(path), lazy=True)]
    faces: List[dict] = []
    for idx, f in enumerate(fonts):
        ids: Dict[int, Set[str]] = {i: set() for i in (1, 2, 4, 6, 16, 17)}
        for rec in f["name"].names:
            if rec.nameID in ids:
                val = norm(rec.toUnicode(errors="replace"))
                if val:
                    ids[rec.nameID].add(val)
        sub = " ".join(ids[2] | ids[17])
        mac = f["head"].macStyle if "head" in f else 0
        sel = f["OS/2"].fsSelection if "OS/2" in f else 0
        faces.append({
            "index": idx,
            "fam": ids[1], "full": ids[4], "ps": ids[6], "typo": ids[16], "typosub": ids[17],
            "bold": bool(mac & 1 or sel & 0x20 or "bold" in sub),
            "italic": bool(mac & 2 or sel & 1 or "italic" in sub or "oblique" in sub),
        })
    return faces


def face_support(path: Path, idx: int, chars: Set[str], objs: dict) -> Tuple[Set[str], Dict[str, float]]:
    """Check a font face against the characters a sub renders with it.

    Returns (characters the font lacks, {character: advance width in em}).
    `objs` caches opened fonts across calls.
    """
    key = (str(path), idx)
    if key not in objs:
        if path.suffix.lower() in (".ttc", ".otc"):
            objs[key] = TTCollection(str(path), lazy=True).fonts[idx]
        else:
            objs[key] = TTFont(str(path), lazy=True)
    f = objs[key]
    cmap = f.getBestCmap() or {}
    symmap = {}
    if "cmap" in f:
        sym = f["cmap"].getcmap(3, 0)   # symbol-encoded fonts map ASCII to U+F0xx
        if sym:
            symmap = sym.cmap
    hmtx, upem = f["hmtx"], f["head"].unitsPerEm
    missing: Set[str] = set()
    widths: Dict[str, float] = {}
    for ch in chars:
        cp = ord(ch)
        glyph = cmap.get(cp) or symmap.get(0xF000 + cp) or symmap.get(cp)
        if glyph is None:
            missing.add(ch)
        else:
            widths[ch] = hmtx[glyph][0] / upem
    return missing, widths


def pick_font_files(font: str, rels: List[str], index: Dict[str, dict], fonts_dir: Path,
                    faces_cache: Dict[str, List[dict]], chars: Set[str], objs: dict) -> List[dict]:
    """Choose the fewest files that cover a font name: one per bold/italic variant.

    Files that answer to the name as a family/full/PostScript name win over ones that
    only match via the typographic family (e.g. 'Foo Light' for 'Foo'). Within a
    variant, candidates are checked against the characters the subs render with the
    font: any that lack some are excluded, and if the rest still differ in character
    widths the pick is flagged. Remaining ties go to .ttf, then an unsuffixed filename,
    then the largest file, then a file named like the font itself.

    Returns one dict per variant: rel, n (candidates), status (ok / equivalent / differ /
    uncovered / unchecked), n_chars, excluded [(rel, chars lacking)], differ [(rel, chars
    whose width differs)], missing_n.
    """
    n = norm(font)
    primary: Dict[tuple, Dict[str, int]] = {}
    secondary: Dict[tuple, Dict[str, int]] = {}
    for rel in rels:
        if rel not in faces_cache:
            faces_cache[rel] = font_faces(fonts_dir / rel)
        for face in faces_cache[rel]:
            if n in face["fam"] | face["full"] | face["ps"]:
                primary.setdefault((face["bold"], face["italic"]), {}).setdefault(rel, face["index"])
            elif n in face["typo"]:
                key = (frozenset(face["typosub"]), face["bold"], face["italic"])
                secondary.setdefault(key, {}).setdefault(rel, face["index"])
    groups = primary or secondary

    def squash(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    def rank(rel: str) -> tuple:
        path = Path(rel)
        own = {squash(x) for face in faces_cache[rel] for x in face["ps"] | face["full"]}
        return (EXT_RANK.get(path.suffix.lower(), 9), bool(HASH_SUFFIX_RE.search(path.stem)),
                -index[rel]["size"],
                squash(path.stem) not in own,                       # filename matches the font's own name
                path.stem.isupper() or path.stem.islower(),         # prefer a properly cased name
                rel.lower())

    picks: List[dict] = []
    for cands in groups.values():
        ordered = sorted(cands, key=rank)
        support = {rel: face_support(fonts_dir / rel, cands[rel], chars, objs) for rel in ordered} if chars else {}
        excluded = [(rel, len(support[rel][0])) for rel in ordered if chars and support[rel][0]]
        good = [rel for rel in ordered if not (chars and support[rel][0])]
        pick = {"n": len(ordered), "n_chars": len(chars), "excluded": excluded, "differ": [], "missing_n": 0}

        if not good:   # every candidate lacks some of the characters used: take the closest
            best = min(ordered, key=lambda r: (len(support[r][0]), ordered.index(r)))
            pick.update(rel=best, status="uncovered", excluded=[], missing_n=len(support[best][0]))
            picks.append(pick)
            continue

        chosen = good[0]
        if chars:
            base = support[chosen][1]
            for rel in good[1:]:
                other = support[rel][1]
                k = sum(1 for ch in chars
                        if ch in base and ch in other and abs(base[ch] - other[ch]) > WIDTH_TOLERANCE)
                if k:
                    pick["differ"].append((rel, k))
        if not chars:
            status = "unchecked"
        elif pick["differ"]:
            status = "differ"
        else:
            status = "equivalent" if len(good) > 1 else "ok"
        pick.update(rel=chosen, status=status)
        picks.append(pick)
    return picks


def describe_pick(font: str, p: dict) -> Tuple[str, Optional[str], List[str]]:
    """Return (short note, reason to double-check or None, extra detail lines) for one pick."""
    parts = [f"for {font}"]
    if p["n"] > 1:
        parts.append(f"picked from {p['n']} files")
    reason: Optional[str] = None
    extra: List[str] = []
    status = p["status"]
    if status == "equivalent":
        parts.append(f"candidates agree on the {p['n_chars']} characters used")
    elif status == "differ":
        parts.append("CHECK: other candidates differ in character widths")
        reason = (f"{p['rel']} vs " + ", ".join(rel for rel, _ in p["differ"]) + ": character widths differ")
        extra += [f"differs: {rel} ({k} of {p['n_chars']} characters have a different width)"
                  for rel, k in p["differ"]]
    elif status == "uncovered":
        parts.append(f"CHECK: lacks {p['missing_n']} of {p['n_chars']} characters used")
        reason = f"no candidate has all {p['n_chars']} characters used (best, {p['rel']}, lacks {p['missing_n']})"
    elif status == "unchecked" and p["n"] > 1:
        parts.append("no subtitle text uses this font, so it wasn't verified")
    if p["excluded"]:
        parts.append(f"{len(p['excluded'])} excluded for missing glyphs")
        extra += [f"excluded: {rel} (lacks {k} of {p['n_chars']} characters used)" for rel, k in p["excluded"]]
    return "; ".join(parts), reason, extra


def collect_mkv_fonts(mkv: Path, info: dict):
    """Inspect one mkv for font usage.

    Returns None if it has no ASS/SSA subtitle tracks, otherwise
    (names answered by attached fonts, lowercase attachment file names,
    {font used by any ASS/SSA track: where it's used},
    {font: characters rendered with it}).
    """
    sub_tracks = [t for t in info.get("tracks", [])
                  if t.get("type") == "subtitles"
                  and t.get("properties", {}).get("codec_id") in SSA_CODECS]
    if not sub_tracks:
        return None

    attachments = font_attachments(info)
    att_file_names = {Path(a.get("file_name", "")).name.lower() for a in info.get("attachments", [])}
    attached: Set[str] = set()
    used: Dict[str, Set[str]] = {}
    chars: Dict[str, Set[str]] = {}

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        if attachments:
            att_args = [f"{a['id']}:{tmp_dir / ('att%d_%s' % (a['id'], Path(a['file_name']).name))}"
                        for a in attachments]
            subprocess.run(["mkvextract", "attachments", str(mkv), *att_args],
                           capture_output=True, text=True)
            for p in tmp_dir.glob("att*"):
                attached |= font_names_from_file(p)

        for t in sub_tracks:
            tid = t["id"]
            ext = SSA_CODECS[t["properties"]["codec_id"]]
            script = tmp_dir / f"track{tid}{ext}"
            subprocess.run(["mkvextract", "tracks", str(mkv), f"{tid}:{script}"],
                           capture_output=True, text=True)
            if script.exists():
                for font, where in fonts_used_in_script(script).items():
                    used.setdefault(font, set()).update(where)
                for font, found in chars_by_font(script).items():
                    chars.setdefault(font, set()).update(found)
    return attached, att_file_names, used, chars


def cmd_add_fonts(args: argparse.Namespace) -> None:
    """Attach fonts from the fonts folder that the subs use but the mkv doesn't carry."""
    check_tools()
    if not shutil.which("mkvpropedit"):
        print(c("mkvpropedit not found (it ships with MKVToolNix).", C.YELLOW))
        return

    input_dir = Path(args.input_dir).resolve()
    out_dir = Path(args.output_dir).resolve() if getattr(args, "output_dir", None) else None
    if out_dir and out_dir == input_dir:
        print(c("--output-dir must differ from the input folder (omit it to edit the mkvs in place).", C.YELLOW))
        return
    explicit_dir = getattr(args, "fonts_dir", None)
    fonts_dir = Path(explicit_dir).resolve() if explicit_dir else default_fonts_dir()
    if not fonts_dir.is_dir():
        print(c(f"Fonts folder {fonts_dir} not found. Run dump-fonts first or pass --fonts-dir.", C.YELLOW))
        return
    dry = getattr(args, "dry_run", False)

    mkvs = find_mkvs(input_dir)
    mode = ("dry run, nothing will be changed" if dry
            else f"edited copies go to {out_dir}" if out_dir
            else "editing the mkv files in place")
    print(f"Found {len(mkvs)} mkv file(s) in {input_dir} ({mode})")

    index = scan_library(fonts_dir)
    by_name: Dict[str, List[str]] = {}
    for rel, e in index.items():
        for n in e["names"]:
            by_name.setdefault(n, []).append(rel)
    print(f"Fonts folder {fonts_dir}: {len(index)} font file(s).\n")

    mkvs_changed = files_total = failures = 0
    still_missing: Set[str] = set()
    review: Dict[str, Set[str]] = {}
    faces_cache: Dict[str, List[dict]] = {}
    objs: dict = {}

    for mkv in mkvs:
        print(f"Checking {mkv.name} ...")
        info = probe(mkv)
        result = collect_mkv_fonts(mkv, info)
        if result is None:
            print("  no ASS/SSA tracks, skipping.\n")
            continue
        attached, att_file_names, used, chars = result
        chars_by_norm: Dict[str, Set[str]] = {}
        for font, found in chars.items():
            chars_by_norm.setdefault(norm(font), set()).update(found)

        to_add: Dict[str, Tuple[str, dict]] = {}   # relative path -> (first font it's for, pick details)
        unresolved: List[str] = []
        needed = sorted((f for f in used if norm(f) not in attached), key=str.lower)
        for font in needed:
            rels = by_name.get(norm(font))
            if not rels:
                unresolved.append(font)
                continue
            for pick in pick_font_files(font, rels, index, fonts_dir, faces_cache,
                                        chars_by_norm.get(norm(font), set()), objs):
                to_add.setdefault(pick["rel"], (font, pick))
        still_missing |= set(unresolved)
        if unresolved:
            print(f"  {c('still missing', C.BOLD, C.YELLOW)} (not in fonts folder): {', '.join(unresolved)}")
        if not to_add:
            print("  nothing to add.\n")
            continue

        print(f"  {len(to_add)} font file(s) to attach (covering {len(needed) - len(unresolved)} font name(s)):")
        for rel, (font, pick) in to_add.items():
            note, reason, extra = describe_pick(font, pick)
            mark = c("!", C.BOLD, C.YELLOW) if reason else c("+", C.BOLD, C.CYAN)
            print(f"      {mark} {rel}  {c(f'({note})', C.DIM)}")
            for line in extra:
                print(f"          {c(line, C.DIM)}")
            if reason:
                review.setdefault(font, set()).add(reason)
        files_total += len(to_add)
        mkvs_changed += 1
        if dry:
            print()
            continue

        target = mkv
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / mkv.name
            shutil.copy2(mkv, target)

        taken = set(att_file_names)
        items: List[Tuple[str, Path]] = []
        for rel in to_add:
            path = fonts_dir / rel
            name = path.name
            if name.lower() in taken:  # same filename already in the mkv: keep both distinct
                name = f"{path.stem}_{sha256_of(path)[:8]}{path.suffix}"
            taken.add(name.lower())
            items.append((name, path))

        before = len(font_attachments(info))
        ok = True
        for i in range(0, len(items), 25):
            cmd = ["mkvpropedit", str(target)]
            for name, path in items[i:i + 25]:
                cmd += ["--attachment-name", name, "--add-attachment", str(path)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode >= 2:
                ok = False
                print(c(f"  mkvpropedit failed: {(r.stdout + r.stderr).strip()}", C.YELLOW))
                break

        after = len(font_attachments(probe(target))) if ok else before
        if ok and after >= before + len(items):
            print(f"  attached {len(items)} font file(s) ({after} fonts now attached).\n")
        else:
            failures += 1
            mkvs_changed -= 1
            files_total -= len(items)
            if out_dir and target != mkv:
                target.unlink(missing_ok=True)
            print(c("  could not confirm the fonts were attached; "
                    + ("the copy was removed." if out_dir else "check the mkv.") + "\n", C.YELLOW))

    for f in objs.values():
        f.close()

    verb = "Dry run: would attach" if dry else "Done. Attached"
    print(f"{verb} {files_total} font file(s) to {mkvs_changed} mkv file(s)."
          + (f" {failures} mkv file(s) failed." if failures else ""))
    if review:
        print(f"\n{len(review)} font(s) worth double-checking:")
        for f in sorted(review, key=str.lower):
            for reason in sorted(review[f]):
                print(f"  - {f}: {reason}")
    if still_missing:
        print(f"\n{len(still_missing)} font(s) still missing (not in the fonts folder):")
        for f in sorted(still_missing, key=str.lower):
            print(f"  - {f}")