# bdq CLI reference

`bdq` is the command-line tool behind the **vivado-bd-analyzer** skill. Claude runs it to query
the block design and its sources, and you can run it by hand too. It is a single Python 3.8+
script with no dependencies: `vivado-bd-analyzer/scripts/bdq.py`.

Back to the [README](README.md) for installation, configuration and the overall workflow.

## Contents

- [Running bdq](#running-bdq)
- [Global options](#global-options)
- [Exploring the design](#exploring-the-design)
- [Reading sources](#reading-sources)
- [Notes](#notes)
- [Setup and export](#setup-and-export)
- [Configuration file](#configuration-file)
- [Common workflows](#common-workflows)

## Running bdq

`bdq` is a script, not an installed command:

```bash
python3 ~/.claude/skills/vivado-bd-analyzer/scripts/bdq.py <command> [options]

# or, with an alias in ~/.bashrc / ~/.zshrc
alias bdq="python3 ~/.claude/skills/vivado-bd-analyzer/scripts/bdq.py"
bdq <command> [options]
```

Every command prints a short plain-text answer, designed to be read by an LLM as well as by you.
`bdq --help` and `bdq <command> --help` list all options. The examples below come from a small
example design.

## Global options

| Option | Meaning |
|---|---|
| `--config PATH` | Configuration file |
| `--workdir DIR` | Work directory (uses `DIR/bdq.json`; needed for the first `--save`) |
| `--project-root DIR` | Project root (saved as `project_root`) |
| `--profile NAME` | Profile to use (default: `BDQ_PROFILE`, then `default_profile`) |
| `--graph PATH` | Graph file, overriding the configuration |

## Exploring the design

**`overview [--depth N]`**: hierarchy, block kinds, cached roles and top-level ports.

```text
design: pkt_proc  exported: 2026-10-14T10:00:00  vivado: 2023.2
cells: 8 leaf (ip 4, rtl 2, hls 1, cip 1), 2 hier | flat nets: 8 intf, 2 data, 1 clk, 1 rst
hierarchy  (name  kind  type  | cached role):
  egress  ip  axis_switch:1.1
  hdr_switch  rtl  hdr_switch:1.0  | header-based switch
  ingress_parser  hls  ingress_parser:1.0
  pe_0/  hier (2 leaf cells)
    core  cip  pe_core:1.0
    fifo  ip  axis_data_fifo:2.0
  ...
ports in : S_AXIS_ETH(axis), gate_en
ports out: M_AXIS_QDMA(axis), gate_stat[31:0]
```

Kinds: `ip` (Xilinx IP), `rtl` (module reference), `hls` (Vitis HLS IP), `cip` (other packaged
IP), `hier` (hierarchy).

**`block NAME [--params user|prop|all]`**: one block's configuration, interfaces with their
peers (resolved across hierarchies), clock/reset, and note status. By default only user-set
parameters are shown.

```text
block: hdr_switch  kind: module_ref  vlnv: xilinx.com:module_ref:hdr_switch:1.0  ref: hdr_switch
config (user-set only, 2 total; --params all for everything): N_OUT=2
interfaces:
  s_axis  slave  axis  <- ingress_parser/m_axis
  m00_axis  master  axis  -> pe_0/fifo/S_AXIS
  m01_axis  master  axis  -> pe_1/fifo/S_AXIS
clk/rst: aclk <- sys_clk ; aresetn <- rst_proc/peripheral_aresetn
note: fresh | Role: header-based switch
```

**`path A B [--k 3] [--max-hops 12]`**: data-flow routes from A to B, shortest first. If B is
upstream of A, it says so.

```text
path 1: S_AXIS_ETH -> ingress_parser -> hdr_switch -> pe_1/fifo -> egress -> M_AXIS_QDMA
  S_AXIS_ETH -> ingress_parser/s_axis  [axis]
  ingress_parser/m_axis -> hdr_switch/s_axis  [axis]
  hdr_switch/m01_axis -> pe_1/fifo/S_AXIS  [axis]
  ...
```

**`neighbors NAME [--depth N] [--with-clocks]`**: data-flow edges around a block, in both directions.
Clocks and resets are hidden unless requested.

**`clocks NAME`**: clock and reset nets reaching a block or hierarchy, with driver and fanout.

```text
clk sys_clk_1  src=sys_clk  fanout=7  here: pe_0/core/aclk, pe_0/fifo/s_axis_aclk
rst axis_aresetn_1  src=rst_proc/peripheral_aresetn  fanout=6  here: pe_0/core/aresetn, ...
```

**`find PATTERN`**: cells by substring or glob over name, VLNV and kind.
**`addr [FILTER]`**: address map segments.

Names can be short (`hdr_switch`, `pe_0/fifo`) or full paths (`/pe_0/fifo`). A hierarchy expands to
its leaf cells in `path`, `neighbors` and `clocks`.

## Reading sources

**`src NAME`**: where the block's source lives, with line counts and sha1, plus the ready-made
sources line for the notes. Xilinx IP and hierarchies have no user source.

```text
src: hdr_switch  kind: module_ref  top: hdr_switch
  src/rtl/hdr_switch.sv  45 lines  sha1:4fd0cbe513  (module decl)
notes sources line: <!-- sources: src/rtl/hdr_switch.sv@4fd0cbe513 -->
```

Lookup rules: `src_map` first. For module references and packaged IP, files under `src_roots`
declaring `module <name>` or `entity <name> is` are used. For HLS IP, the C/C++ file defining the
top function and the HLS config (`syn.top=`, `set_top`) are used. Build folders (`*.runs`, `*.cache`,
`*.gen`, `*.sim`, `.Xil`, …) are skipped, and HLS-generated HDL is marked `GENERATED`. What each file
declares is cached in `<workdir>/.cache/src_index.json` and refreshed when a file's size or mtime
changes, so repeated lookups do not re-read the sources.

**`outline NAME [--max N]`**: structural outline with line numbers and no bodies.

```text
== src/rtl/hdr_switch.sv  sha1:4fd0cbe513
module hdr_switch @L2-43 (42 lines)
ports: input logic aclk | input logic aresetn | input logic [63:0] s_axis_tdata | ...
params: parameter int N_OUT = 2 | parameter int HDR_W = 64 | localparam int SEL_W = $clog2(N_OUT)
enum state_t: IDLE, HDR, FWD, DROP
blocks: @L25 always_ff @(posedge aclk) begin : hdr_cap  (hdr_cap) ; @L36 always_comb begin  (destination select)
case: @L27 case(state)
instances: @L40 axis_fifo u_fifo
```

For VHDL it lists entity ports, enumerated types, processes, `case` statements and instances. For
HLS C++ it lists the top function, helper functions, interface and other pragmas, loop labels and
stream types.

## Notes

**`notes [NAME] [--template | --set] [--source FILE]`** lists notes and their freshness, prints one
note, prints a template for a new note, or writes one.

`notes NAME --set` reads the note body (a `Role:` line plus bullets) from stdin and writes the
section for that block in the notes file: it generates the `## /path` header and the
`<!-- sources: file@sha1 -->` line from the block's current source, replaces any existing section
for the block (removing duplicates), and refuses a body without a `Role:` line. `--source FILE`
(repeatable, relative to the project root) adds other files the note depends on to its sources.

```text
$ bdq notes hdr_switch --set <<'EOF'
Role: header-based switch, routes each packet to pe_0/pe_1
- Verified: output select = s_axis_tdata[8 +: SEL_W] (src/rtl/hdr_switch.sv:36-38)
EOF
note added: hdr_switch in workdir:design_notes_pkt_proc.md  (sources: src/rtl/hdr_switch.sv@4fd0cbe513)
```

## Setup and export

| Command | Meaning |
|---|---|
| `discover [--max-depth N]` | Candidate `.xpr` and `.bd` files in the project, graphs in the work directory, profiles |
| `config [--register \| --unregister]` | Effective configuration: work directory, project, profile, Vivado parameters, graph and notes paths, registry status. `--register` maps this project to this `bdq.json` |
| `export [options]` | Run the export (see below) |

`export` options:

| Option | Meaning |
|---|---|
| `--xpr`, `--bd` | Project file (relative to the project root) and block design |
| `--out` | Output graph, relative to the work directory (must stay inside it) |
| `--src-root` | Source folder in the project, repeatable |
| `--settings` | `settings64.sh` to source |
| `--skip-config` | Glob of cells whose CONFIG is not exported |
| `--timeout SECONDS` | Abort the export after this long (default: `vivado.timeout`, else 3600) |
| `--dry-run` | Print what would run and where, without running Vivado |
| `--save [NAME]` | Save the parameters as a profile (default name: the block design name) |

`--save` stores the parameters only after a successful export (or with `--dry-run`, without running
Vivado). The first saved profile becomes the default.

## Configuration file

`bdq` finds `bdq.json` in this order: `--config PATH`, `--workdir DIR` (uses `DIR/bdq.json`), the
`BDQ_CONFIG` environment variable, the **project registry**, then a search upwards from the current
directory. The work directory is the folder that holds it:

```text
~/bdq/proj/                      # the work directory: the ONLY writable place
├── bdq.json                     # configuration and profiles
├── graphs/bd_graph_pkt_proc.json   # one exported graph per block design
├── design_notes_pkt_proc.md     # notes, one file per profile
└── .cache/src_index.json        # which file declares which module/function (rebuilt as needed)
```

The registry, `~/.config/bdq/projects.json` (`$XDG_CONFIG_HOME/bdq/` if set, or `BDQ_REGISTRY`),
maps project roots to their `bdq.json`. bdq looks up the current directory (or `--project-root`)
and picks the innermost registered project containing it. `export --save` registers the project;
`config --register` / `config --unregister` add or remove an existing setup, and `bdq config`
shows whether the project is registered. It is the only file bdq writes outside a work directory.

Values resolve as command-line flags > active profile (`--profile`, `BDQ_PROFILE`,
`default_profile`) > top-level fields. A complete example is in
[`assets/bdq.json.example`](vivado-bd-analyzer/assets/bdq.json.example).

| Field | Relative to | Meaning |
|---|---|---|
| `project_root` | work directory, or absolute | Vivado project root. Read-only |
| `src_roots` | project root | Folders scanned for RTL/HLS sources. Keep them narrow on large projects |
| `src_map` | project root | Explicit source files for a cell (full path, short path or leaf name as key) when automatic lookup fails |
| `vivado.xpr` | project root | Project file |
| `vivado.bd` | | Block design name (or a `.bd` path) |
| `vivado.settings` | | `settings64.sh` to source before Vivado (needed whenever Vivado is not on `PATH`) |
| `vivado.skip_config` | | Glob of cell paths whose `CONFIG.*` is not exported (HBM/DDR/PCIe IPs carry thousands of parameters) |
| `vivado.vivado` | | Vivado executable name or path (default `vivado`) |
| `vivado.timeout` | | Seconds before an export is aborted (default 3600; `--timeout` overrides it) |
| `graph` | work directory | Graph file (default `graphs/bd_graph_<bd>.json`) |
| `notes` | work directory | Notes file (default `design_notes_<profile>.md`, or `design_notes.md` without profiles) |
| `confirm_before_export` | | If `true`, Claude shows the dry run and asks before every export |
| `profiles` / `default_profile` | | Named parameter sets, usually one per block design |

## Common workflows

```bash
# First setup: pick a work directory, look around, export, save a profile
bdq --workdir ~/bdq/proj --project-root ~/work/proj discover
bdq --workdir ~/bdq/proj --project-root ~/work/proj \
    export --xpr proj.xpr --bd pkt_proc --src-root src/rtl --dry-run
bdq --workdir ~/bdq/proj --project-root ~/work/proj \
    export --xpr proj.xpr --bd pkt_proc --src-root src/rtl --save

# Day to day, from inside the project (registered by the first --save)
bdq overview
bdq block hdr_switch
bdq path S_AXIS_ETH M_AXIS_QDMA
bdq outline hdr_switch

# The design changed: refresh the graph with the saved profile
bdq export

# A second block design in the same project
bdq export --bd dbg_bd --save debug
bdq --profile debug overview

# Record what you learned about a block
printf 'Role: header-based switch\n' | bdq notes hdr_switch --set
bdq notes
```
