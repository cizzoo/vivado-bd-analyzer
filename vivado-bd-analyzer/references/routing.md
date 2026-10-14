# Routing examples

Worked sequences showing how a question turns into the minimum set of queries and reads.
Command output is abbreviated.

## 1. "What does hdr_switch do?"

```text
$ bdq block hdr_switch
block: hdr_switch  kind: module_ref  vlnv: xilinx.com:module_ref:hdr_switch:1.0  ref: hdr_switch
config (user-set only, 2 total): N_OUT=2
interfaces:
  s_axis    slave   axis  <- ingress_parser/m_axis
  m00_axis  master  axis  -> pe_0/fifo/S_AXIS
  m01_axis  master  axis  -> pe_1/fifo/S_AXIS
note: none
```

Already known: one parsed stream in, one stream per processing engine out. The name and the
parser upstream suggest routing by header field, but that is still inference.

```text
$ bdq outline hdr_switch
module hdr_switch @L2-43 (42 lines)
enum state_t: IDLE, HDR, FWD, DROP
blocks: @L25 always_ff ... (hdr_cap) ; @L36 always_comb (destination select)
case: @L27 case(state)
```

The question is about behaviour, so read the FSM (L25-34) and the select logic (L36-38) only.
The module is 42 lines, so reading it whole would also be fine here. For a 600-line module the
outline is what keeps the read small.

Answer: role, routing rule (verified, with lines), drop condition (verified), backpressure
behaviour (state it as unverified if those lines were not read). Then write the note.

## 2. "How does a packet get from the Ethernet input to QDMA?"

```text
$ bdq path S_AXIS_ETH M_AXIS_QDMA
path 1: S_AXIS_ETH -> ingress_parser -> hdr_switch -> pe_1/fifo -> egress -> M_AXIS_QDMA
path 2: S_AXIS_ETH -> ingress_parser -> hdr_switch -> pe_0/fifo -> pe_0/core -> egress -> M_AXIS_QDMA
```

Explain the hops using fresh notes and `bdq block` only. Read source only for the hop that decides
between path 1 and path 2 (here `hdr_switch`). Vendor IP hops (`axis_data_fifo`, `axis_switch`)
are explained from their config, never from source.

## 3. "Why does pe_1 never leave reset?"

```text
$ bdq clocks pe_1
rst axis_aresetn_1  src=rst_proc/peripheral_aresetn  fanout=6  here: pe_1/fifo/s_axis_aresetn
```

Then `bdq block rst_proc` (vendor `proc_sys_reset`: check its `ext_reset_in`/`dcm_locked`
drivers with `bdq block`) and the clock feeding it. Only open RTL if a custom block generates
the reset.

## 4. "Explain the design" (vague)

`bdq overview` (a few hundred tokens). From the hierarchy, identify the ingress, the egress, and
the blocks with the most data connections, then explain the main path with `bdq path` between the
top input and output ports. Offer to go into specific blocks instead of reading everything.
If several plausible "main" paths exist, ask the user which one matters.

## 5. "Which blocks access HBM?"

`bdq find hbm` to get the memory cell, `bdq addr hbm` for the segments, then
`bdq neighbors <hbm_cell>` to list the AXI-MM masters connected to it (through SmartConnects,
`neighbors --depth 2`).

## 6. HLS block

```text
$ bdq src ingress_parser
  hls config: src/hls/ingress_parser/hls_config.cfg
  src/hls/ingress_parser/ingress_parser.cpp  21 lines  (top function)
$ bdq outline ingress_parser
top function ingress_parser @L11
interface pragmas: @L14 INTERFACE axis port=s_axis ; ...
other pragmas: @L17 DATAFLOW
functions: @L4 parse_eth
```

DATAFLOW plus helper functions means the top function is a pipeline of stages: read the top
function body to get the stage order, then only the stage the question is about.

## 7. Two blocks or more than ~300 lines

Hand the reading to the `hdl-reader` subagent with the outlines and the question; keep only its
summary in the main context.
