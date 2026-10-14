# Design notes

Short, versioned explanations of BD blocks, kept in the work directory. Written by Claude via
`bdq notes NAME --set`, which generates each header and sources line. Source paths are relative to the project root.
A note is "fresh" while the sha1 in its sources line matches the file on disk.

## /hdr_switch
<!-- sources: src/rtl/hdr_switch.sv@f3fcbddfc3 -->
Role: routes each packet to pe_0/pe_1 using header byte 1; drops packets tagged 0xFF
- Verified: output select = s_axis_tdata[8 +: SEL_W] (src/rtl/hdr_switch.sv:36-38)
- Verified: FSM IDLE->HDR->FWD|DROP, back to IDLE on tlast (src/rtl/hdr_switch.sv:25-34)
- Inferred: tready is not gated in DROP state, so dropped packets are consumed at line rate (not checked)

## /pe_0/fifo
Role: 1024-deep AXIS FIFO decoupling hdr_switch from pe_0/core (vendor axis_data_fifo, from config)
