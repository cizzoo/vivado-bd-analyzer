---
name: vivado-bd-analyzer
description: Analyzes Vivado block designs (.bd) and the RTL/HLS code inside them through a queryable graph. Explains what a cell does, AXI-Stream/AXI-MM data flow between blocks, clock/reset domains and address maps, even from a screenshot or a bare cell name ("il diagramma"). Not for writing RTL or timing reports.
---

# Vivado BD Analyzer

A block design is a graph plus a set of sources. Screenshots lose the connections and
`write_bd_tcl` dumps are mostly boilerplate, so both waste context. This skill keeps the
full design in a local database (`bd_graph.json`) and lets you pull out only the slice a
question needs, then read only the source lines that answer it.

## When not to use it

The first use costs a setup (questions plus a Vivado export of several minutes), so make sure the
question needs the design's structure before starting it. Answer directly, without `bdq`, when the
question is about:

- writing, modifying or debugging RTL/HLS code on its own (no "how is it connected" part);
- synthesis, implementation or simulation errors, timing closure, utilization or power reports;
- creating or editing a block design, or general Vivado usage and Tcl commands.

If a configuration already exists (`bdq config` shows a graph), using it is cheap: prefer it for
any question about connectivity, even a borderline one.

## Write policy: read-only except the work directory

This skill is **read-only** on everything except one folder chosen by the user, the **work
directory** (e.g. `~/bdq/<project>`), which holds `bdq.json`, the exported graphs and the notes.

- Never create, edit, move or delete files anywhere else: not the sources, not the Vivado
  project (`.xpr`, `.bd`, `.srcs`, runs, IP), not `.claude/` or `CLAUDE.md` in the project, not
  shell or Claude settings, even for "small helpful fixes". Propose them in the reply instead.
- Write only through `bdq` (export, `--save`, `notes --set`, its cache), which refuses any path
  outside the work directory. The one exception is bdq's project registry
  (`~/.config/bdq/projects.json`, paths only), updated by `--save` and `config --register`.
  Do not edit the notes file or the registry by hand.
- Never open the project in Vivado yourself. `bdq export` is the only way: it runs Vivado in a
  throw-away temp directory and opens the project with `-read_only`.
- If something would need a write elsewhere (installing the `hdl-reader` agent, adding
  an alias to a shell profile, fixing the project), tell the user what to do; do it only if
  they explicitly ask you to.

## Running bdq

`SKILL_DIR` is the directory containing this file. Invoke `python3 SKILL_DIR/scripts/bdq.py ...`
(written `bdq` below) unless the user has a `bdq` alias. Run it from inside the project: once a
project is registered, bdq finds its configuration by itself, with no `--config`.
`bdq <command> --help` lists options.

**No configuration found, no profile for the design in question, or the user wants to change
export parameters:** read `references/setup.md` before doing anything else.

Re-export (`bdq export`, reusing the saved profile) when `bdq overview` or `bdq block` warns that
the `.bd` file is newer than the graph. Missing connections, wrong directions or a failing export
usually mean a Vivado property differs in the user's version: see `references/export.md`.

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
   for the path and suggest a `src_map` entry in `bdq.json`.
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

After explaining a block for the first time, or when a note was stale, save a short note. `bdq`
writes the header and the sources line (with current sha1) and replaces any previous note:

```bash
bdq notes X --set <<'EOF'
Role: <one line, shown by bdq overview>
- Verified: <fact> (file:line)
- Inferred: <deduction, not checked in code>
EOF
```

Add `--source <file>` (relative to the project root) for each other file the note relies on, such
as a submodule you read. Keep notes short: they are an index for future questions, not
documentation. Do this without asking unless the user declined it.
