# bd_export.tcl - export the open Vivado Block Design to a lossless JSON graph.
#
# The output (bd_graph.json) is NOT meant to be read by an LLM directly.
# It is the database queried by bdq.py, which returns small, focused slices.
#
# Interactive (Vivado Tcl console, BD open):
#   source <skill_dir>/scripts/bd_export.tcl
#   bdx_export ./reports/bd_graph.json
#
# Batch (no GUI) - prefer `bdq export`, which runs this in a temp dir and keeps the project untouched:
#   vivado -mode batch -nojournal -nolog \
#     -source <skill_dir>/scripts/bd_export.tcl \
#     -tclargs <project.xpr> <bd_name_or_path> <out.json> [skip_config_glob]
# The project is opened with -read_only; write <out.json> outside the project.
#
# skip_config_glob: optional glob of cell paths whose CONFIG.* is not exported
#   (e.g. "*hbm*" - HBM/DDR IPs carry thousands of parameters and slow the export).

# Bump when the JSON layout changes incompatibly; bdq warns about graphs newer than it knows.
namespace eval ::bdx {
    variable schema 1
    # JSON string escapes: backslash, quote, and every control character U+0000-U+001F.
    variable escmap [list "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "\\r" "\t" "\\t"]
    for {set i 0} {$i < 32} {incr i} {
        if {$i in {9 10 13}} { continue }
        lappend escmap [format %c $i] [format "\\u%04x" $i]
    }
}

proc ::bdx::esc {s} {
    return "\"[string map $::bdx::escmap $s]\""
}

proc ::bdx::arr {items} { return "\[[join $items ,]\]" }

proc ::bdx::strs {lst} {
    set o {}
    foreach x $lst { lappend o [::bdx::esc $x] }
    return [::bdx::arr $o]
}

proc ::bdx::prop {obj name} {
    if {[catch {get_property $name $obj} v]} { return "" }
    return $v
}

proc ::bdx::path {obj} {
    set p [::bdx::prop $obj PATH]
    if {$p eq ""} { set p "$obj" }
    return $p
}

proc ::bdx::parent {p} {
    set i [string last "/" $p]
    if {$i <= 0} { return "/" }
    return [string range $p 0 [expr {$i - 1}]]
}

# 1 if obj drives the net at netpath.
# A hierarchy boundary pin seen from inside its hierarchy has the opposite role,
# and top-level ports are always seen from inside the top.
proc ::bdx::is_src {obj netpath} {
    set cls [::bdx::prop $obj CLASS]
    set p [::bdx::path $obj]
    set boundary [expr {[::bdx::parent $p] eq [::bdx::parent $netpath]}]
    switch -- $cls {
        bd_intf_pin  { set s [expr {[::bdx::prop $obj MODE] eq "Master"}] }
        bd_pin       { set s [expr {[::bdx::prop $obj DIR] eq "O"}] }
        bd_intf_port { set s [expr {[::bdx::prop $obj MODE] eq "Slave"}]; set boundary 0 }
        bd_port      { set s [expr {[::bdx::prop $obj DIR] eq "I"}];      set boundary 0 }
        default      { set s 0 }
    }
    if {$boundary} { set s [expr {!$s}] }
    return $s
}

proc ::bdx::kind {vlnv type} {
    if {$type eq "hier"} { return hier }
    if {$type eq "module_ref"} { return module_ref }
    set f [split $vlnv ":"]
    set vendor [lindex $f 0]
    set lib [lindex $f 1]
    if {$lib eq "module_ref"} { return module_ref }
    if {$lib eq "hls"} { return hls }
    if {$vendor eq "xilinx.com" && $lib eq "ip"} { return xilinx_ip }
    return custom_ip
}

proc ::bdx::cell_json {c skip_cfg} {
    set path [::bdx::path $c]
    set vlnv [::bdx::prop $c VLNV]
    set type [::bdx::prop $c TYPE]
    set kind [::bdx::kind $vlnv $type]
    set ref  [::bdx::prop $c REFERENCE_NAME]

    set cfg {}
    if {$kind ne "hier" && ($skip_cfg eq "" || ![string match $skip_cfg $path])} {
        foreach k [lsort [list_property $c CONFIG.*]] {
            if {[string match "*.VALUE_SRC" $k]} { continue }
            set v   [::bdx::prop $c $k]
            set src [::bdx::prop $c "$k.VALUE_SRC"]
            lappend cfg "[::bdx::esc [string range $k 7 end]]:[::bdx::strs [list $v $src]]"
        }
    }

    set ipins {}
    foreach p [get_bd_intf_pins -quiet -of_objects $c] {
        lappend ipins "{\"name\":[::bdx::esc [::bdx::prop $p NAME]],\"mode\":[::bdx::esc [::bdx::prop $p MODE]],\"vlnv\":[::bdx::esc [::bdx::prop $p VLNV]]}"
    }
    set pins {}
    foreach p [get_bd_pins -quiet -of_objects $c] {
        lappend pins "{\"name\":[::bdx::esc [::bdx::prop $p NAME]],\"dir\":[::bdx::esc [::bdx::prop $p DIR]],\"type\":[::bdx::esc [::bdx::prop $p TYPE]],\"left\":[::bdx::esc [::bdx::prop $p LEFT]],\"right\":[::bdx::esc [::bdx::prop $p RIGHT]]}"
    }

    return "{\"path\":[::bdx::esc $path],\"kind\":[::bdx::esc $kind],\"vlnv\":[::bdx::esc $vlnv],\"ref\":[::bdx::esc $ref],\"config\":{[join $cfg ,]},\"intf_pins\":[::bdx::arr $ipins],\"pins\":[::bdx::arr $pins]}"
}

proc ::bdx::net_json {n eps extra} {
    set np [::bdx::path $n]
    set src {}
    set dst {}
    foreach e $eps {
        if {[::bdx::is_src $e $np]} { lappend src [::bdx::path $e] } else { lappend dst [::bdx::path $e] }
    }
    return "{\"path\":[::bdx::esc $np],$extra\"src\":[::bdx::strs $src],\"dst\":[::bdx::strs $dst]}"
}

proc ::bdx::net_type {eps} {
    set types {}
    foreach e $eps { lappend types [string tolower [::bdx::prop $e TYPE]] }
    foreach t {clk rst intr} { if {$t in $types} { return $t } }
    return data
}

proc bdx_export {outfile {skip_cfg ""}} {
    set t0 [clock seconds]
    set design [::bdx::prop [current_bd_design] NAME]
    set bdfile [::bdx::prop [current_bd_design] FILE_NAME]
    set ver ""
    catch { set ver [version -short] }

    set cells {}
    foreach c [get_bd_cells -quiet -hier] { lappend cells [::bdx::cell_json $c $skip_cfg] }

    set ports {}
    foreach p [get_bd_intf_ports -quiet] {
        lappend ports "{\"path\":[::bdx::esc [::bdx::path $p]],\"intf\":true,\"mode\":[::bdx::esc [::bdx::prop $p MODE]],\"vlnv\":[::bdx::esc [::bdx::prop $p VLNV]]}"
    }
    foreach p [get_bd_ports -quiet] {
        lappend ports "{\"path\":[::bdx::esc [::bdx::path $p]],\"intf\":false,\"dir\":[::bdx::esc [::bdx::prop $p DIR]],\"type\":[::bdx::esc [::bdx::prop $p TYPE]],\"left\":[::bdx::esc [::bdx::prop $p LEFT]],\"right\":[::bdx::esc [::bdx::prop $p RIGHT]]}"
    }

    set inets {}
    foreach n [get_bd_intf_nets -quiet -hier] {
        set eps [concat [get_bd_intf_pins -quiet -of_objects $n] [get_bd_intf_ports -quiet -of_objects $n]]
        set v ""
        foreach e $eps { set v [::bdx::prop $e VLNV]; if {$v ne ""} { break } }
        lappend inets [::bdx::net_json $n $eps "\"vlnv\":[::bdx::esc $v],"]
    }

    set nets {}
    foreach n [get_bd_nets -quiet -hier] {
        set eps [concat [get_bd_pins -quiet -of_objects $n] [get_bd_ports -quiet -of_objects $n]]
        if {[llength $eps] == 0} { continue }
        lappend nets [::bdx::net_json $n $eps "\"type\":[::bdx::esc [::bdx::net_type $eps]],"]
    }

    set addr {}
    foreach s [get_bd_addr_segs -quiet] {
        set off [::bdx::prop $s OFFSET]
        set rng [::bdx::prop $s RANGE]
        if {$off eq "" && $rng eq ""} { continue }
        lappend addr "{\"seg\":[::bdx::esc [::bdx::path $s]],\"offset\":[::bdx::esc $off],\"range\":[::bdx::esc $rng]}"
    }

    set ts [clock format [clock seconds] -format "%Y-%m-%dT%H:%M:%S"]
    set meta "{\"schema\":$::bdx::schema,\"design\":[::bdx::esc $design],\"bd_file\":[::bdx::esc $bdfile],\"vivado\":[::bdx::esc $ver],\"exported\":[::bdx::esc $ts]}"

    file mkdir [file dirname $outfile]
    set fh [open $outfile w]
    puts $fh "{\"meta\":$meta,"
    puts $fh "\"cells\":[::bdx::arr $cells],"
    puts $fh "\"ports\":[::bdx::arr $ports],"
    puts $fh "\"intf_nets\":[::bdx::arr $inets],"
    puts $fh "\"nets\":[::bdx::arr $nets],"
    puts $fh "\"addr\":[::bdx::arr $addr]}"
    close $fh
    puts "bdx_export: [llength $cells] cells, [llength $inets] intf nets, [llength $nets] nets -> $outfile ([expr {[clock seconds] - $t0}] s)"
}

# ---- batch entry point ------------------------------------------------------
# The project is opened READ-ONLY. Run Vivado from a scratch directory (bdq export does this)
# so that its own temporary files (.Xil, logs) do not land in the project either.
if {[info exists ::argv] && [llength $::argv] >= 3} {
    lassign $::argv xpr bd out skip
    if {[catch {open_project -read_only $xpr} err]} {
        error "bd_export: cannot open '$xpr' read-only ($err). Refusing to open it writable."
    }
    if {[file exists $bd]} {
        set f $bd
    } else {
        set f [get_files -quiet "$bd.bd"]
        if {$f eq ""} { set f [get_files -quiet $bd] }
    }
    if {$f eq ""} { error "bd_export: block design '$bd' not found in $xpr" }
    open_bd_design [lindex $f 0]
    bdx_export $out $skip
    close_project
}
