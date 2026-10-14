---
name: vivado-bd-analyzer
description: Explain and navigate Vivado IP Integrator block designs (.bd) and the RTL/HLS blocks inside them with minimal tokens, through a queryable graph export (bd_graph.json) and the bdq CLI. Use this skill whenever the user asks what a BD block/cell does, how data or packets flow between blocks, which path a stream takes to an output, clock/reset domains, address maps, or anything about "the block design", "the Vivado diagram", "il diagramma", AXI-Stream/AXI-MM connections, module_ref or Vitis HLS IP inside a Vivado project - even if they paste a schematic screenshot, only name a cell (e.g. "hdr_switch"), or don't mention the skill. Prefer it over reading write_bd_tcl dumps, raw JSON, screenshots or whole source trees.
---

# Vivado BD Analyzer

A block design is a graph plus a set of sources. Screenshots lose the connections and
`write_bd_tcl` dumps are mostly boilerplate, so both waste context. This skill keeps the
full design in a local database (`bd_graph.json`) and lets you pull out only the slice a
question needs, then read only the source lines that answer it.

## Write policy: read-only except the work directory

This skill is **read-only** on everything except one folder chosen by the user, the **work
directory**. It holds `bdq.json`, the exported graphs and the notes files, and may sit inside or
outside the project (outside is safer, e.g. `~/bdq/<project>`).

- Never create, edit, move or delete files anywhere else: not the sources, not the Vivado
  project (`.xpr`, `.bd`, `.srcs`, runs, IP), not `.claude/` or `CLAUDE.md` in the project, not
  shell or Claude settings. This holds even for "small helpful fixes"; propose them in the reply
  instead and let the user apply them.
- Write only by: running `bdq` (export, `--save`, and its source index in `<workdir>/.cache/`, all
  refused outside the work directory) and editing the notes file shown by `bdq config`.
- Never open the project in Vivado yourself. `bdq export` is the only way: it runs Vivado in a
  throw-away temp directory, opens the project with `-read_only`, and receives the graph on stdout.
- If something would need a write elsewhere (installing the `hdl-reader` agent, adding
  `BDQ_CONFIG` to a settings file, fixing an export problem in the project), tell the user what to do;
  do it only if they explicitly ask you to.

## Setup and export parameters

`SKILL_DIR` is the directory containing this file. There is no `bdq` command on PATH: invoke
`python3 SKILL_DIR/scripts/bdq.py ...` (written `bdq` below), unless the user has a `bdq` alias.
Point every call at the configuration with `--config <workdir>/bdq.json`, or with the
`BDQ_CONFIG` environment variable if the user has set it. Without either, bdq searches for
`bdq.json` upwards from the current directory.

If no configuration is found, the work directory is the **first thing to ask** (propose
`~/bdq/<project-name>`). Then create the setup with `--workdir <dir> --project-root <project>`
on the first `bdq export ... --save`; later calls only need `--config <dir>/bdq.json`.
Suggest that the user set `BDQ_CONFIG` (shell profile, or the `env` section of their Claude Code
user settings) so future sessions find it; don't edit those files yourself.

If `bdq config` warns that the work directory is the project root or contains it (an older setup
with `bdq.json` at the project root), bdq refuses to write into the project: tell the user and
offer to move the setup to a dedicated folder.

### The parameters

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

Values resolve as: command-line flags > active profile > top-level `bdq.json`. A **profile** is a
saved set of these parameters, usually one per block design. `--save [NAME]` stores the
parameters of a successful export (or of a `--dry-run`) as a profile; the first one saved becomes
the default. Other commands pick a profile with `--profile NAME`. Each profile keeps its own graph
and its own notes file (`design_notes_<profile>.md`), both in the work directory.

Useful views: `bdq config` (work directory, project, effective parameters, profiles, graph status)
and `bdq discover` (candidate `.xpr` and `.bd` files, graphs in the work directory, profiles).

### When to ask the user

Ask only when a choice is genuinely open, and ask everything at once in a single message,
proposing a default for each item (use the client's interactive question tool if it offers one).
Collect candidates first with `bdq discover` and `bdq config`, so the user picks rather than types.

- **First setup** (no configuration, or no profile for the design in question): ask for the work
  directory, the project, the block design, the source folders, and the `settings64.sh` of the local Vivado. Skip any item with a
  single obvious candidate and state the assumption instead ("only one .xpr found: proj.xpr").
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

After the answers, run the export with the chosen flags plus `--save <name>`, so the next session
needs no questions. Always run `--dry-run` first when parameters are new.

```bash
bdq --workdir ~/bdq/proj --project-root ~/mnt/proj discover
bdq --workdir ~/bdq/proj --project-root ~/mnt/proj export --xpr proj.xpr --bd pkt_proc \
    --src-root src/rtl --src-root src/hls --dry-run
bdq --workdir ~/bdq/proj --project-root ~/mnt/proj export --xpr proj.xpr --bd pkt_proc \
    --src-root src/rtl --src-root src/hls --save
bdq --config ~/bdq/proj/bdq.json overview
bdq --config ~/bdq/proj/bdq.json --profile dbg_bd overview     # another saved design
```

### Vivado not available locally: the manual fallback

`bdq export` only runs the Vivado installed on this machine. Exports are aborted after `--timeout`
seconds (default 3600, or `vivado.timeout`). Details: `references/export.md`.

If Vivado is not installed here (e.g. it lives on another machine), bdq cannot export. The user
runs the exporter wherever Vivado is, and copies the resulting JSON into
`<workdir>/graphs/bd_graph_<bd>.json`; all queries then work as usual. Without a working batch setup,
the user can also run the exporter from the Vivado GUI Tcl console:
`source SKILL_DIR/scripts/bd_export.tcl; bdx_export <workdir>/graphs/bd_graph_<bd>.json`.
Tell them to give an output path in the work directory, and that a GUI session may still save
project state on its own, unlike `bdq export`. For slow exports caused by HBM/DDR/PCIe IPs,
use `--skip-config "*hbm*"`.

Re-export whenever the design changed; `bdq overview` and `bdq block` print a warning when the
`.bd` file is newer than the graph.

## Core rules

- Never `cat`/Read `bd_graph.json` or a `write_bd_tcl` script. Query with `bdq`; it is the only
  view of the design you need.
- Ignore clocks and resets unless the question is about them; `bdq` hides them by default
  because they connect to everything and drown the data path.
- Do not search for or read the source of Xilinx IP (`kind: xilinx_ip`). Explain it from its
  product guide plus the config `bdq block` shows.
- Never read HLS-generated Verilog/VHDL (marked `GENERATED`). The C++ is the real source.
- Stop climbing the reading ladder as soon as you can answer. Each step costs more context.
- Answer in the user's language. Keep code blocks and file content in English.

## Workflow for each question

### 1. Check the cache

`bdq notes` (or the `note:` line of `bdq block`) shows saved explanations. A `fresh` note means
the source has not changed since it was written: answer from it and only go deeper if the
question needs detail the note lacks. A `stale` note is a hint, not a fact: re-verify the parts
you use.

### 2. Route the question to a query

| Question shape | Start with | Then |
|---|---|---|
| "What does X do?" | `bdq block X` | `bdq src X`, then the reading ladder |
| "How does data get from A to B?" | `bdq path A B` | `block` on hops whose role is unclear |
| "What is around X / what feeds X?" | `bdq neighbors X [--depth 2]` | `block` on the interesting neighbours |
| Clock/reset, CDC, "X stuck in reset" | `bdq clocks X` | `block` on the reset/clock source |
| Address map, "who talks to HBM/DDR?" | `bdq addr [filter]`, `bdq find hbm` | `path` from master to slave |
| Vague: "explain the design" | `bdq overview` | pick 2-4 central blocks, ask the user if unsure |
| Cell name unknown or ambiguous | `bdq find <pattern>` | |

Names can be short (`hdr_switch`, `pe_0/fifo`) or full paths. A hierarchy (`kind: hier`)
expands to its leaf cells in `path`, `neighbors` and `clocks`.
More worked examples: `references/routing.md`.

### 3. Read code only as far as needed (reading ladder)

Use this only for `module_ref`, `hls` and `custom_ip` blocks.

1. **Graph only.** Kind, config, peers and interface types from `bdq block`, plus the block name,
   often answer "what is it for". If you answer here, say it is inferred from connectivity.
2. **Locate.** `bdq src X` gives file(s), line counts and sha1. If nothing is found, ask the user
   for the path and add it to `src_map` in `bdq.json`.
3. **Outline.** `bdq outline X` shows ports, parameters, FSM enums, `always`/`process` blocks,
   `case` selectors and instances with line numbers (HLS: interface pragmas, top function,
   helper functions, loop labels, stream types). No bodies.
4. **Targeted read.** From the outline, choose the line ranges that answer the question and Read
   only those (offset/limit). Example: "how does it choose the output?" means the select logic and
   the FSM transitions, not the port list.
5. **Whole file.** Only when the module is small (< ~250 lines, where the outline costs about as
   much as the file) or when targeted reads leave a real gap.

If an instance inside the module matters (e.g. a FIFO or a sub-FSM), apply the same ladder to it
by searching for its module with `rg -n "module <name>"`.

### 4. Deep reading in a subagent

When the answer needs more than ~300 lines of source, or spans several blocks, delegate the
reading to a subagent so the main context only receives conclusions. Use the `hdl-reader` agent
if the user installed it, otherwise a general-purpose subagent with the prompt from
`assets/hdl-reader.md`.
Give it: block path, the question, the `bdq block` and `bdq outline` output, and the line ranges
you suspect matter. It returns at most ~15 lines with `file:line` references.

### 5. Answer format

Lead with the direct answer, then the supporting detail. Separate what you **verified** in the
source (cite `file:line`) from what you **inferred** from names, connectivity or conventions, so
the user knows how far to trust each statement and what to ask you to check next. Keep the
answer at the level of the question; offer to go deeper rather than dumping everything.

### 6. Save what you learned

After explaining a block for the first time, or when a note was stale, update the notes file:

```bash
bdq notes X --template   # prints the header + sources line with current sha1
```

Fill in `Role:` (one line, shown by `bdq overview`), then a few `Verified:` / `Inferred:` bullets.
Append or replace that block's section in the notes file (path from `bdq config`, inside the
work directory; the only file you edit by hand). Keep notes short: they are an index
for future questions, not documentation. Do this without asking unless the user declined it.

## Command reference

```text
bdq overview [--depth N]             hierarchy, kinds, cached roles, top ports
bdq block NAME [--params user|prop|all]   config, interfaces + peers, clk/rst, note status
bdq neighbors NAME [--depth N] [--with-clocks]
bdq path A B [--k 3] [--max-hops 12] data-flow routes (reports if B is upstream of A)
bdq clocks NAME                      clock/reset nets, driver and fanout
bdq find PATTERN                     substring or glob over name / VLNV / kind
bdq addr [FILTER]                    address segments
bdq src NAME                         source files, sha1, HLS config, notes sources line
bdq outline NAME [--max N]           structural outline with line numbers
bdq notes [NAME] [--template]        cached notes and freshness
bdq export [--dry-run] [--xpr --bd --out --src-root --settings --skip-config --timeout] [--save [NAME]]
bdq config                           effective parameters and profiles
bdq discover                         candidate .xpr/.bd files, graphs, profiles
```

Global options: `--config PATH` / `--workdir DIR` (otherwise `BDQ_CONFIG` > search upwards),
`--project-root DIR`, `--profile NAME` (otherwise `BDQ_PROFILE` > `default_profile`), `--graph PATH`.

## When something looks wrong

Missing connections, wrong directions, empty config or a failing export usually mean a Vivado
property differs in the user's version. See `references/export.md` for the JSON schema, the
properties the exporter relies on, and how to debug them in the Tcl console.
