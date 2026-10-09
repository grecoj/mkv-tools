"""
mkv_subtitle_lib -- implementation of mkv_subtitle_tool.py (the launcher next to this folder).

Module index (open only the one you need to edit):

  cli.py         argparse wiring + main(); also holds the user-facing usage docs
  common.py      shared infrastructure: constants/dir names, color output, mkvtoolnix helpers,
                 ASS/SRT parsing primitives, arg-type helpers, build_mkvmerge_command and
                 rebuild_all_mkvs (the mkvmerge step every writing stage goes through)
  extract.py     stage `extract`     -- subtitle tracks + metadata out of each mkv
  replace.py     stages `plan-replace` + `merge-replace` -- REPLACE_RULES (per-show text replacements):
                 scan/prompt/plan, then apply the plan and rebuild mkvs
  trim.py        stage `trim`        -- cut subtitle entries past the end of the video
  reposition.py  stage `reposition`  -- coordinate/size transform for pad/crop resolution changes
  style_sync.py  stage `style-sync`  -- STYLE_SYNC_RULES (per-show look), signs track, font attach

Dependency rule: stage modules import only from common.py, never from each other
(cli.py is the only module that imports from the stages).
"""
