# Setup and export

Read this when `bdq` finds no configuration, when a profile is missing for the design the user
asks about, or when the user wants to change export parameters. Day-to-day questions do not need it.

## First setup

If no configuration is found, the work directory is the **first thing to ask** (propose
`~/bdq/<project-name>`). Then create the setup with `--workdir <dir> --project-root <project>`
on the first `bdq export ... --save`. `--save` also registers the project in
`~/.config/bdq/projects.json`, so later calls from inside the project (or any subfolder) need no
`--config` at all.

A setup made before the registry existed, or found with `--config`/`BDQ_CONFIG`, is not
registered yet: `bdq config` says so, and `bdq --config <dir>/bdq.json config --register` fixes it
(`--unregister` removes the entry).

If `bdq config` warns that the work directory is the project root or contains it (an older setup
with `bdq.json` at the project root), bdq refuses to write into the project: tell the user and
offer to move the setup to a dedicated folder.

```bash
bdq --workdir ~/bdq/proj --project-root ~/work/proj discover
bdq --workdir ~/bdq/proj --project-root ~/work/proj export --xpr proj.xpr --bd pkt_proc \
    --src-root src/rtl --src-root src/hls --settings <settings64.sh> --dry-run
bdq --workdir ~/bdq/proj --project-root ~/work/proj export --xpr proj.xpr --bd pkt_proc \
    --src-root src/rtl --src-root src/hls --settings <settings64.sh> --save
bdq --config ~/bdq/proj/bdq.json overview
bdq --config ~/bdq/proj/bdq.json --profile dbg_bd overview     # another saved design
```

Always run `--dry-run` first when parameters are new. `--save [NAME]` stores the parameters of a
successful export (or of a `--dry-run`) as a profile, so the next session needs no questions.

## The parameters

| Parameter | Flag | Default |
|---|---|---|
| Work directory (the only writable place) | `--workdir` (first setup), then `--config` | none: ask |
| Project root (read-only) | `--project-root` (saved as `project_root`) | current directory |
| Vivado project | `--xpr` (on `export`) | none: must be chosen |
| Block design | `--bd` | none: must be chosen |
| Output graph, inside the work directory | `--out` | `graphs/bd_graph_<bd>.json` |
| Source folders for RTL/HLS, in the project | `--src-root` (repeatable) | project root |
| Vivado setup script | `--settings` | none (Vivado must be on PATH) |
| Cells whose CONFIG is skipped | `--skip-config` | none |
| Export time limit (seconds) | `--timeout` | 3600, or `vivado.timeout` |

Values resolve as: command-line flags > active profile > top-level `bdq.json`. A **profile** is a
saved set of these parameters, usually one per block design; the first one saved becomes the
default, and other commands pick one with `--profile NAME`. Each profile keeps its own graph and
its own notes file (`design_notes_<profile>.md`), both in the work directory.

Useful views: `bdq config` (work directory, project, effective parameters, profiles, graph status)
and `bdq discover` (candidate `.xpr` and `.bd` files, graphs in the work directory, profiles).

## When to ask the user

Ask only when a choice is genuinely open, and ask everything at once in a single message,
proposing a default for each item (use the client's interactive question tool if it offers one).
Collect candidates first with `bdq discover` and `bdq config`, so the user picks rather than types.

- **First setup**: ask for the work directory, the project, the block design, the source folders
  and the `settings64.sh` of the local Vivado. Skip any item with a single obvious candidate and
  state the assumption instead ("only one .xpr found: proj.xpr").
- **The user names a design, project or folder that differs from the active profile**: ask whether
  to switch to a matching existing profile, create a new one, or override just this once.
- **Several profiles exist and the question does not say which design**: ask which one.
- **`"confirm_before_export": true` in `bdq.json`**: before every export, show the
  `bdq export --dry-run` output and ask the user to confirm or change parameters. Set this flag
  (it lives in the work directory) when the user says they always want to be asked.
- **The user asks to change a parameter** ("put the graphs in a different folder"): apply it with
  the flag, then ask whether to save it in the profile. Output paths must stay inside the work
  directory; bdq refuses anything else.

Otherwise do not ask: when an active profile fits the request, use it silently.

## Vivado not available locally

`bdq export` only runs the Vivado installed on this machine. If Vivado lives elsewhere, the user
runs the exporter there and copies the resulting JSON into `<workdir>/graphs/bd_graph_<bd>.json`;
all queries then work as usual. Without a working batch setup, the user can also run the exporter
from the Vivado GUI Tcl console:
`source SKILL_DIR/scripts/bd_export.tcl; bdx_export <workdir>/graphs/bd_graph_<bd>.json`.
Tell them to give an output path in the work directory, and that a GUI session may still save
project state on its own, unlike `bdq export`. For slow exports caused by HBM/DDR/PCIe IPs, use
`--skip-config "*hbm*"`. Export internals and debugging: `references/export.md`.
