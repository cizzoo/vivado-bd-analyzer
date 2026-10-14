#!/usr/bin/env python3
"""bdq - token-efficient queries over a Vivado Block Design graph.

The graph (bd_graph.json) is produced by bd_export.tcl. Every command prints a
small plain-text slice meant to be read by an LLM; the raw JSON never needs to
enter the context.

Commands:
  overview [--depth N]            hierarchy, kinds, cached roles, top ports
  block NAME [--params MODE]      one block: config, interfaces + peers, clk/rst
  neighbors NAME [--depth N]      data-flow neighbourhood (both directions)
  path A B [--k N] [--max-hops N] data-flow routes from A to B
  clocks NAME                     clock/reset nets reaching a block (or hier)
  find PATTERN                    cells by name / VLNV / kind
  addr [FILTER]                   address map segments
  src NAME                        where the block's source lives (+ sha1)
  outline NAME [--max N]          structural outline of the source, no bodies
  notes [NAME] [--template|--set] cached explanations and their freshness; --set writes one
  export [--dry-run] [--bd ..]    (re)generate bd_graph.json with the local Vivado
  config [--register]             show the effective configuration and profiles
  discover                        list .xpr / .bd candidates and existing graphs
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from collections import Counter, defaultdict, deque
from pathlib import Path

CONFIG_NAME = "bdq.json"
GRAPH_CANDIDATES = ("bd_graph.json", "reports/bd_graph.json", "build/bd_graph.json")
SKIP_DIRS = {".git", ".Xil", "node_modules", "__pycache__", ".venv", ".claude"}
SKIP_DIR_SUFFIXES = (".runs", ".cache", ".sim", ".hw", ".gen", ".ip_user_files")
HDL_EXT = {".v", ".sv", ".vh", ".svh", ".vhd", ".vhdl"}
CPP_EXT = {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh"}
HLS_CFG_EXT = {".cfg", ".tcl", ".app"}
GENERATED_MARKERS = ("/syn/verilog/", "/syn/vhdl/", "/impl/verilog/", "/impl/vhdl/",
                     "/impl/ip/", "/hdl/verilog/", "/hdl/vhdl/", "/sim/verilog/", "/sim/vhdl/")
KIND_SHORT = {"xilinx_ip": "ip", "module_ref": "rtl", "hls": "hls", "custom_ip": "cip", "hier": "hier"}
DATAFLOW = {"intf", "data", "intr"}
MAX_FILE_BYTES = 5_000_000
GRAPH_SCHEMA = 1                      # meta.schema written by bd_export.tcl; absent means 0
SRC_INDEX = ".cache/src_index.json"   # source index, in the work directory
DEFAULT_EXPORT_TIMEOUT = 3600         # seconds
REGISTRY_VERSION = 1


# ---------------------------------------------------------------- helpers ----
def die(msg: str) -> None:
    print(f"bdq: {msg}", file=sys.stderr)
    sys.exit(1)


def short(p: str) -> str:
    return p[1:] if p.startswith("/") else p


def parent(p: str) -> str:
    i = p.rfind("/")
    return "/" if i <= 0 else p[:i]


def leafname(p: str) -> str:
    return p.rsplit("/", 1)[-1]


def vlnv_short(v: str) -> str:
    f = (v or "").split(":")
    return f"{f[2]}:{f[3]}" if len(f) == 4 else (v or "-")


def iface_name(vlnv: str) -> str:
    f = (vlnv or "").split(":")
    name = f[2] if len(f) == 4 else (vlnv or "")
    return name[:-4] if name.endswith("_rtl") else name


def sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:10]


def load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except OSError as e:
        die(f"cannot read {path}: {e.strerror}")
    except json.JSONDecodeError as e:
        die(f"{path} is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})")


def read_text(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(errors="ignore")
    except OSError:
        return ""


def width(pin: dict) -> str:
    left, right = pin.get("left", ""), pin.get("right", "")
    if left not in ("", None) and right not in ("", None) and str(left) != str(right):
        return f"[{left}:{right}]"
    return ""


# --------------------------------------------------------------- registry ----
def registry_path() -> Path:
    """Project root -> bdq.json map, so that bdq finds a project's configuration by itself.
    The only file bdq writes outside a work directory; it holds paths and nothing else."""
    if os.environ.get("BDQ_REGISTRY"):
        return Path(os.environ["BDQ_REGISTRY"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "bdq" / "projects.json"


def load_registry() -> dict:
    """{project_root: config_file}; an unreadable registry is treated as empty."""
    try:
        data = json.loads(registry_path().read_text())
        projects = data.get("projects") if isinstance(data, dict) else None
        return {k: v for k, v in (projects or {}).items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError):
        return {}


def registry_lookup(start: Path):
    """(project_root, config_file) of the registered project containing start, innermost first."""
    start = start.resolve()
    best = None
    for root, cfg in load_registry().items():
        r = Path(root)
        if (r == start or r in start.parents) and Path(cfg).is_file():
            if best is None or len(r.parts) > len(Path(best[0]).parts):
                best = (root, cfg)
    return best


def registry_set(root: Path, cfg_file) -> None:
    """Map root to cfg_file (None removes the entry). Atomic; never writes anywhere else."""
    path = registry_path()
    projects = load_registry()
    key = str(root.resolve())
    old = projects.get(key)
    if cfg_file is None:
        if key not in projects:
            print(f"registry: {key} was not registered")
            return
        del projects[key]
    else:
        projects[key] = str(Path(cfg_file).resolve())
        if old == projects[key]:
            return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"version": REGISTRY_VERSION, "projects": dict(sorted(projects.items()))},
                                  indent=2) + "\n")
        tmp.replace(path)
    except OSError as e:
        print(f"bdq: could not update the registry {path}: {e.strerror}", file=sys.stderr)
        return
    if cfg_file is None:
        print(f"registry: removed {key}")
    else:
        print(f"registry: {key} -> {projects[key]}" + (f" (was {old})" if old else ""))


# ---------------------------------------------------------------- context ----
def _merge(base: dict, over: dict) -> dict:
    """Shallow-merge two config dicts, merging nested dicts one level deep."""
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Ctx:
    """Effective configuration: bdq.json defaults < selected profile < command-line overrides.

    Two locations matter:
      root  the Vivado project (sources, .xpr, .bd). Treated as READ-ONLY: bdq never writes there.
      home  the work directory chosen by the user (holds bdq.json, graphs, notes). The only place
            bdq writes to (every write goes through assert_writable()), apart from the registry.
    """

    def __init__(self, args, need_graph: bool = True):
        self.cfg_file, self.cfg_source = self._locate_config(args)
        exists = bool(self.cfg_file and self.cfg_file.exists())
        self.raw = load_json(self.cfg_file) if exists else {}
        if not isinstance(self.raw, dict):
            die(f"{self.cfg_file}: expected a JSON object")
        self.home = self.cfg_file.parent.resolve() if self.cfg_file else None

        self.cli_root = getattr(args, "project_root", None)
        pr = self.cli_root or self.raw.get("project_root")
        if pr:
            prp = Path(pr).expanduser()
            self.root = (prp if prp.is_absolute() else (self.home or Path.cwd()) / prp).resolve()
        elif exists:
            self.root = self.home          # legacy layout: bdq.json sitting in the project root
        else:
            self.root = Path.cwd().resolve()
        # The project is unprotected when the work directory is the project root or contains it
        # (e.g. a legacy bdq.json sitting in the project root). Writes into the project are then
        # refused by assert_writable().
        self.legacy = self.home is not None and (self.home == self.root or self.home in self.root.parents)

        profiles = self.raw.get("profiles") or {}
        self.profile = getattr(args, "profile", None) or os.environ.get("BDQ_PROFILE") or self.raw.get("default_profile")
        if self.profile and self.profile not in profiles:
            die(f"unknown profile '{self.profile}'. Available: {', '.join(profiles) or '(none)'}")
        base = {k: v for k, v in self.raw.items() if k not in ("profiles", "default_profile")}
        self.cfg = _merge(base, profiles.get(self.profile, {}) if self.profile else {})
        self.cfg = _merge(self.cfg, self._cli_overrides(args))

        self.base = self.home or self.root        # where graphs and notes are resolved
        bd = (self.cfg.get("vivado") or {}).get("bd")
        g = args.graph or os.environ.get("BDQ_GRAPH") or self.cfg.get("graph")
        if not g and bd:
            g = f"graphs/bd_graph_{Path(bd).stem}.json"
        if not g:
            g = next((c for c in GRAPH_CANDIDATES if (self.base / c).exists()), "graphs/bd_graph.json")
        self.graph_rel = g
        self.graph_path = (self.base / g).resolve()
        if need_graph and not exists and not getattr(args, "graph", None) and not os.environ.get("BDQ_GRAPH"):
            die(f"no configuration found for {self.root} (not registered, no bdq.json): first setup needed "
                "(`bdq --workdir DIR --project-root PROJECT export ... --save`), or pass --config")
        if need_graph and not self.graph_path.exists():
            die(f"graph not found: {self.graph_path}. Run `bdq export` (or bd_export.tcl in Vivado)")
        self.src_roots = [(self.root / r).resolve() for r in self.cfg.get("src_roots", ["."])]
        self.src_map = self.cfg.get("src_map", {})
        default_notes = f"design_notes_{self.profile}.md" if self.profile else "design_notes.md"
        self.notes_path = (self.base / self.cfg.get("notes", default_notes)).resolve()

    @staticmethod
    def _locate_config(args):
        """(config file or None, how it was found)."""
        if getattr(args, "config", None):
            return Path(args.config).expanduser().resolve(), "--config"
        if getattr(args, "workdir", None):
            return (Path(args.workdir).expanduser() / CONFIG_NAME).resolve(), "--workdir"
        if os.environ.get("BDQ_CONFIG"):
            return Path(os.environ["BDQ_CONFIG"]).expanduser().resolve(), "BDQ_CONFIG"
        start = Path(args.project_root).expanduser() if getattr(args, "project_root", None) else Path.cwd()
        hit = registry_lookup(start)
        if hit:
            return Path(hit[1]).resolve(), f"registry ({registry_path()})"
        cwd = Path.cwd()
        for d in (cwd, *cwd.parents):
            if (d / CONFIG_NAME).exists():
                return (d / CONFIG_NAME).resolve(), "found upwards from the current directory"
        return None, None

    @staticmethod
    def _cli_overrides(args) -> dict:
        o, viv = {}, {}
        for key in ("xpr", "bd", "settings", "skip_config"):
            v = getattr(args, key, None)
            if v:
                viv[key] = v
        if getattr(args, "out", None):
            o["graph"] = args.out
        if getattr(args, "src_root", None):
            o["src_roots"] = args.src_root
        if viv:
            o["vivado"] = viv
        return o

    def write_error(self, path):
        """Why path may not be written, or None if it may. Only the work directory is writable,
        and never the project inside it."""
        if self.home is None:
            return "no work directory: pass --workdir DIR (the folder that will hold bdq.json, graphs and notes)"
        p = Path(path).expanduser().resolve()
        if p != self.home and self.home not in p.parents:
            return f"refusing to write outside the work directory {self.home}: {p}"
        if self.legacy and (p == self.root or self.root in p.parents):
            return (f"refusing to write inside the project {self.root}: the work directory {self.home} "
                    "is the project root or contains it. Move bdq.json to a dedicated folder "
                    "(e.g. ~/bdq/<project>) and set project_root")
        return None

    def assert_writable(self, path) -> Path:
        """Refuse any write outside the work directory."""
        err = self.write_error(path)
        if err:
            die(err)
        return Path(path).expanduser().resolve()

    def rel(self, p) -> str:
        """Display path: relative to the project, else to the work directory, else absolute."""
        rp = Path(p).resolve()
        for anchor, prefix in ((self.root, ""), (self.home, "workdir:")):
            if anchor is None:
                continue
            try:
                return prefix + str(rp.relative_to(anchor))
            except ValueError:
                pass
        return str(p)


# ----------------------------------------------------------------- design ----
class Design:
    def __init__(self, data: dict):
        self.meta = data.get("meta", {})
        self.cells = {c["path"]: c for c in data.get("cells", [])}
        self.ports = {p["path"]: p for p in data.get("ports", [])}
        self.addr = data.get("addr", [])
        self.nets = [{**n, "cls": "intf"} for n in data.get("intf_nets", [])]
        self.nets += [{**n, "cls": n.get("type") or "data"} for n in data.get("nets", [])]
        self._flatten()
        self._edges()

    def is_hier_pin(self, ep: str) -> bool:
        c = self.cells.get(parent(ep))
        return ep not in self.ports and c is not None and c.get("kind") == "hier"

    def owner(self, ep: str) -> str:
        return ep if ep in self.ports else parent(ep)

    def _flatten(self) -> None:
        """Merge nets joined through hierarchy boundary pins into flat nets."""
        uf = list(range(len(self.nets)))

        def find(i):
            while uf[i] != i:
                uf[i] = uf[uf[i]]
                i = uf[i]
            return i

        by_hier_ep = defaultdict(list)
        for i, n in enumerate(self.nets):
            for ep in n.get("src", []) + n.get("dst", []):
                if self.is_hier_pin(ep):
                    by_hier_ep[ep].append(i)
        for idxs in by_hier_ep.values():
            for j in idxs[1:]:
                uf[find(j)] = find(idxs[0])

        groups = defaultdict(lambda: {"src": [], "dst": [], "nets": [], "classes": set(), "vlnv": ""})
        self.hier_ep_group = {}
        for ep, idxs in by_hier_ep.items():
            self.hier_ep_group[ep] = find(idxs[0])
        for i, n in enumerate(self.nets):
            g = groups[find(i)]
            g["nets"].append(n["path"])
            g["classes"].add(n["cls"])
            g["vlnv"] = g["vlnv"] or n.get("vlnv", "")
            g["src"] += [e for e in n.get("src", []) if not self.is_hier_pin(e)]
            g["dst"] += [e for e in n.get("dst", []) if not self.is_hier_pin(e)]
        self.groups = []
        self.ep_group = {}
        for g in groups.values():
            g["cls"] = next(c for c in ("clk", "rst", "intr", "intf", "data") if c in g["classes"] or c == "data")
            self.groups.append(g)
            for ep in g["src"] + g["dst"]:
                self.ep_group[ep] = g
        self.hier_ep_group = {ep: groups[root] for ep, root in self.hier_ep_group.items()}

    def _edges(self) -> None:
        self.out = defaultdict(list)
        self.inn = defaultdict(list)
        for g in self.groups:
            for s in g["src"]:
                for d in g["dst"]:
                    a, b = self.owner(s), self.owner(d)
                    if a != b:
                        e = (a, b, s, d, g)
                        self.out[a].append(e)
                        self.inn[b].append(e)

    def resolve(self, name: str) -> str:
        names = list(self.cells) + list(self.ports)
        q = name if name.startswith("/") else "/" + name
        if q in self.cells or q in self.ports:
            return q
        cands = [p for p in names if p.endswith("/" + name.strip("/"))]
        if not cands:
            cands = [p for p in names if name.lower() in short(p).lower()]
        if len(cands) == 1:
            return cands[0]
        if not cands:
            die(f"no cell or port matches '{name}' (try: bdq find {name})")
        die(f"'{name}' is ambiguous: " + ", ".join(short(c) for c in sorted(cands)[:15]))

    def members(self, path: str) -> set:
        """Leaf nodes represented by path (a hier cell expands to its leaves)."""
        c = self.cells.get(path)
        if c is None or c.get("kind") != "hier":
            return {path}
        return {p for p, x in self.cells.items() if p.startswith(path + "/") and x.get("kind") != "hier"}

    def label(self, g: dict) -> str:
        return (iface_name(g["vlnv"]) or "intf") if g["cls"] == "intf" else g["cls"]


# ------------------------------------------------------------------ notes ----
NOTE_HDR = re.compile(r"^##\s+(/\S+)\s*$", re.M)


def load_notes(ctx: Ctx) -> dict:
    if not ctx.notes_path.exists():
        return {}
    parts = NOTE_HDR.split(ctx.notes_path.read_text(errors="ignore"))
    out = {}
    for path, body in zip(parts[1::2], parts[2::2]):
        srcs = " ".join(re.findall(r"<!--\s*sources:\s*(.*?)\s*-->", body)).split()
        sources = [tuple(s.rsplit("@", 1)) for s in srcs if "@" in s]
        role = re.search(r"^\s*(?:\*\*)?Role(?:\*\*)?:\s*(.+)$", body, re.M)
        out[path] = {"role": role.group(1).strip() if role else None, "sources": sources, "body": body.strip()}
    return out


def note_status(ctx: Ctx, note: dict) -> str:
    if not note["sources"]:
        return "unversioned"
    for f, h in note["sources"]:
        fp = ctx.root / f
        if not fp.exists():
            return f"stale ({f} missing)"
        if sha(fp) != h:
            return f"stale ({f} changed)"
    return "fresh"


def graph_warning(d: Design, ctx: Ctx) -> None:
    schema = d.meta.get("schema", 0)
    if isinstance(schema, int) and schema > GRAPH_SCHEMA:
        print(f"WARNING: graph schema {schema} is newer than this bdq ({GRAPH_SCHEMA}) -> update the skill")
    bd = d.meta.get("bd_file")
    bdp = Path(bd) if bd else None
    if bdp and bdp.exists() and bdp.stat().st_mtime > ctx.graph_path.stat().st_mtime:
        print("WARNING: the .bd file is newer than bd_graph.json -> re-export before trusting connections")


# --------------------------------------------------------------- commands ----
def cmd_overview(d: Design, ctx: Ctx, a) -> None:
    m = d.meta
    notes = load_notes(ctx)
    print(f"design: {m.get('design', '?')}  exported: {m.get('exported', '?')}  vivado: {m.get('vivado', '?')}")
    graph_warning(d, ctx)
    leaf = [c for c in d.cells.values() if c.get("kind") != "hier"]
    cc = Counter(g["cls"] for g in d.groups)
    kinds = Counter(KIND_SHORT.get(c.get("kind"), "?") for c in leaf)
    print(f"cells: {len(leaf)} leaf ({', '.join(f'{k} {v}' for k, v in kinds.most_common())}), "
          f"{len(d.cells) - len(leaf)} hier | flat nets: {cc['intf']} intf, {cc['data'] + cc['intr']} data, "
          f"{cc['clk']} clk, {cc['rst']} rst")
    print("hierarchy  (name  kind  type  | cached role):")
    for path in sorted(d.cells, key=lambda p: p.split("/")):
        lvl = path.count("/")
        if lvl > a.depth:
            continue
        c = d.cells[path]
        ind = "  " * lvl
        if c.get("kind") == "hier":
            n = len(d.members(path))
            more = ", collapsed" if lvl == a.depth else ""
            print(f"{ind}{leafname(path)}/  hier ({n} leaf cells{more})")
        else:
            role = (notes.get(path) or {}).get("role")
            print(f"{ind}{leafname(path)}  {KIND_SHORT.get(c.get('kind'), '?')}  {vlnv_short(c.get('vlnv'))}"
                  + (f"  | {role}" if role else ""))
    ins, outs = [], []
    for p in sorted(d.ports.values(), key=lambda x: x["path"]):
        if p.get("intf"):
            tag = f"{short(p['path'])}({iface_name(p.get('vlnv'))})"
            (ins if p.get("mode") == "Slave" else outs).append(tag)
        elif (p.get("type") or "").lower() not in ("clk", "rst"):
            tag = f"{short(p['path'])}{width(p)}"
            (ins if p.get("dir") == "I" else outs).append(tag)
    print("ports in : " + (", ".join(ins) or "-"))
    print("ports out: " + (", ".join(outs) or "-"))
    print("next: bdq block <name> | bdq path <a> <b> | bdq neighbors <name>")


def _peers(d: Design, ep: str, is_input: bool = True) -> list:
    hg = d.hier_ep_group.get(ep)
    if hg is not None:
        inside = parent(ep) + "/"
        if is_input:   # driver outside, sinks inside
            out_ = [x for x in hg["src"] if not x.startswith(inside)]
            in_ = [x for x in hg["dst"] if x.startswith(inside)]
        else:          # driver inside, sinks outside
            out_ = [x for x in hg["dst"] if not x.startswith(inside)]
            in_ = [x for x in hg["src"] if x.startswith(inside)]
        return [f"outside: {_fmt_peers([short(x) for x in out_], 3)} | inside: {_fmt_peers([short(x) for x in in_], 3)}"]
    g = d.ep_group.get(ep)
    if not g:
        return []
    side = g["dst"] if ep in g["src"] else g["src"]
    return [short(x) for x in side if x != ep]


def _fmt_peers(peers: list, limit: int = 4) -> str:
    if not peers:
        return "(unconnected)"
    extra = f" +{len(peers) - limit} more" if len(peers) > limit else ""
    return ", ".join(peers[:limit]) + extra


def cmd_block(d: Design, ctx: Ctx, a) -> None:
    p = d.resolve(a.name)
    graph_warning(d, ctx)
    if p in d.ports:
        port = d.ports[p]
        kind = iface_name(port.get("vlnv")) if port.get("intf") else (port.get("type") or "data") + width(port)
        print(f"port: {short(p)}  {kind}  -> {_fmt_peers(_peers(d, p), 8)}")
        return
    c = d.cells[p]
    print(f"block: {short(p)}  kind: {c.get('kind')}  vlnv: {c.get('vlnv') or '-'}"
          + (f"  ref: {c['ref']}" if c.get("ref") else ""))

    if c.get("kind") == "hier":
        kids = sorted(x for x in d.cells if parent(x) == p)
        print("children: " + ", ".join(f"{leafname(k)}({KIND_SHORT.get(d.cells[k].get('kind'), '?')})" for k in kids))

    cfg = c.get("config") or {}
    if cfg:
        has_src = any(len(v) > 1 and v[1] for v in cfg.values())
        if a.params == "all" or not has_src:
            items = list(cfg.items())
            note = "" if a.params == "all" else " (no VALUE_SRC info)"
        else:
            keep = {"user": ("USER",), "prop": ("USER", "PROPAGATED")}[a.params]
            items = [(k, v) for k, v in cfg.items() if len(v) > 1 and str(v[1]).upper() in keep]
            note = f" ({a.params}-set only, {len(cfg)} total; --params all for everything)"
        limit = 25
        shown = ", ".join(f"{k}={v[0]}" for k, v in items[:limit])
        more = f" ... +{len(items) - limit}" if len(items) > limit else ""
        print(f"config{note}: {shown or '-'}{more}")

    ipins = c.get("intf_pins") or []
    if ipins:
        print("interfaces:")
        for ip in ipins:
            ep = f"{p}/{ip['name']}"
            arrow = "->" if ip.get("mode") == "Master" else "<-"
            print(f"  {ip['name']}  {ip.get('mode', '?').lower()}  {iface_name(ip.get('vlnv'))}  {arrow} {_fmt_peers(_peers(d, ep, ip.get('mode') != 'Master'))}")
    pins = c.get("pins") or []
    data = [x for x in pins if (x.get("type") or "").lower() not in ("clk", "rst")]
    cr = [x for x in pins if (x.get("type") or "").lower() in ("clk", "rst")]
    if data:
        print("data pins:")
        for x in data[:20]:
            ep = f"{p}/{x['name']}"
            arrow = "->" if x.get("dir") == "O" else "<-"
            print(f"  {x['name']}{width(x)}  {arrow} {_fmt_peers(_peers(d, ep, x.get('dir') != 'O'))}")
        if len(data) > 20:
            print(f"  ... +{len(data) - 20} more data pins")
    if cr:
        parts = []
        for x in cr:
            ep = f"{p}/{x['name']}"
            parts.append(f"{x['name']} <- {_fmt_peers(_peers(d, ep, x.get('dir') != 'O'), 1)}")
        print("clk/rst: " + " ; ".join(parts))

    note = load_notes(ctx).get(p)
    if note:
        print(f"note: {note_status(ctx, note)}" + (f" | Role: {note['role']}" if note["role"] else ""))
    else:
        print("note: none")
    kind = c.get("kind")
    if kind == "xilinx_ip":
        print("next: vendor IP, explain from its product guide + config above; no source to read")
    elif kind == "hier":
        print(f"next: bdq neighbors {short(p)} | bdq block <child>")
    else:
        print(f"next: bdq src {short(p)} | bdq outline {short(p)}")


def cmd_neighbors(d: Design, ctx: Ctx, a) -> None:
    p = d.resolve(a.name)
    allowed = DATAFLOW | ({"clk", "rst"} if a.with_clocks else set())
    start = d.members(p) if p in d.cells else {p}
    seen = set(start)
    frontier = set(start)
    printed = set()
    for lvl in range(1, a.depth + 1):
        nxt = set()
        lines = []
        for n in sorted(frontier):
            for e in d.out.get(n, []) + d.inn.get(n, []):
                if e[4]["cls"] not in allowed:
                    continue
                key = (e[2], e[3])
                if key in printed or (e[0] in start and e[1] in start):
                    continue
                printed.add(key)
                lines.append(f"  {short(e[2])} -> {short(e[3])}  [{d.label(e[4])}]")
                for x in (e[0], e[1]):
                    if x not in seen:
                        seen.add(x)
                        nxt.add(x)
        if lines:
            print(f"depth {lvl}:")
            print("\n".join(sorted(lines)))
        frontier = nxt
        if not frontier:
            break
    if not printed:
        print(f"{short(p)}: no data-flow connections" + ("" if a.with_clocks else " (clk/rst hidden)"))


def _find_paths(d: Design, A: set, B: set, k: int, max_hops: int) -> list:
    found, sigs = [], set()
    q = deque((n, []) for n in sorted(A))
    budget = 200_000
    while q and len(found) < k and budget > 0:
        node, pe = q.popleft()
        if len(pe) >= max_hops:
            continue
        visited = {e[0] for e in pe} | {node}
        for e in d.out.get(node, []):
            budget -= 1
            if e[4]["cls"] not in DATAFLOW or e[1] in visited:
                continue
            np_ = pe + [e]
            if e[1] in B:
                sig = tuple(x[1] for x in np_)
                if sig not in sigs:
                    sigs.add(sig)
                    found.append(np_)
                    if len(found) >= k:
                        break
                continue
            q.append((e[1], np_))
    return found


def cmd_path(d: Design, ctx: Ctx, a) -> None:
    pa, pb = d.resolve(a.src), d.resolve(a.dst)
    A, B = d.members(pa), d.members(pb)
    paths = _find_paths(d, A, B, a.k, a.max_hops)
    if not paths:
        back = _find_paths(d, B, A, 1, a.max_hops)
        if back:
            print(f"no forward data path {short(pa)} -> {short(pb)}; {short(pb)} is UPSTREAM of {short(pa)}:")
            paths = back
        else:
            print(f"no data path between {short(pa)} and {short(pb)} within {a.max_hops} hops")
            return
    for i, pth in enumerate(paths, 1):
        cells = [short(pth[0][0])] + [short(e[1]) for e in pth]
        print(f"path {i}: " + " -> ".join(cells))
        for e in pth:
            print(f"  {short(e[2])} -> {short(e[3])}  [{d.label(e[4])}]")


def cmd_clocks(d: Design, ctx: Ctx, a) -> None:
    p = d.resolve(a.name)
    users = defaultdict(list)
    groups = {}
    for m in sorted(d.members(p)):
        for x in (d.cells.get(m) or {}).get("pins") or []:
            if (x.get("type") or "").lower() in ("clk", "rst"):
                ep = f"{m}/{x['name']}"
                g = d.ep_group.get(ep)
                key = id(g) if g else ep
                groups[key] = g
                users[key].append(short(ep))
    if not users:
        print(f"{short(p)}: no clk/rst pins found")
        return
    for key, eps in users.items():
        g = groups[key]
        if g is None:
            print(f"(unconnected)  pins: {', '.join(eps)}")
            continue
        src = ", ".join(short(s) for s in g["src"]) or "(no driver found)"
        extra = f" +{len(eps) - 6}" if len(eps) > 6 else ""
        print(f"{g['cls']} {short(g['nets'][0])}  src={src}  fanout={len(g['dst'])}  here: {', '.join(eps[:6])}{extra}")


def cmd_find(d: Design, ctx: Ctx, a) -> None:
    pat = a.pattern.lower()
    glob = any(ch in pat for ch in "*?[")
    hits = []
    for path in sorted(d.cells, key=lambda x: x.split("/")):
        c = d.cells[path]
        fields = [short(path).lower(), leafname(path).lower(), (c.get("vlnv") or "").lower(), c.get("kind", "")]
        ok = any(fnmatch.fnmatch(f, pat) for f in fields) if glob else any(pat in f for f in fields)
        if ok:
            hits.append(f"{short(path)}  {KIND_SHORT.get(c.get('kind'), '?')}  {vlnv_short(c.get('vlnv'))}")
    print("\n".join(hits[:60]) if hits else f"no cell matches '{a.pattern}'")
    if len(hits) > 60:
        print(f"... +{len(hits) - 60} more")


def cmd_addr(d: Design, ctx: Ctx, a) -> None:
    rows = [s for s in d.addr if not a.filter or a.filter.lower() in s["seg"].lower()]
    for s in rows[:80]:
        print(f"{s.get('offset', '-'):>20}  {s.get('range', '-'):>8}  {short(s['seg'])}")
    if not rows:
        print("no address segments" + (f" matching '{a.filter}'" if a.filter else ""))


# ---------------------------------------------------------------- sources ----
def _iter_files(ctx: Ctx, exts: set):
    for root in ctx.src_roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = [x for x in dns if x not in SKIP_DIRS and not x.endswith(SKIP_DIR_SUFFIXES)]
            for f in fns:
                if os.path.splitext(f)[1].lower() in exts:
                    yield Path(dp) / f


def _is_generated(path: Path, text: str) -> bool:
    p = str(path).replace("\\", "/")
    head = text[:600]
    return any(m in p for m in GENERATED_MARKERS) or "Generated by Vitis HLS" in head or "Generated by Vivado(TM) HLS" in head


CPP_NOT_FUNCS = {"if", "for", "while", "switch", "return", "catch", "sizeof", "alignof", "decltype"}


def _cpp_defs(text: str, name: str = None):
    """Yield (name, line) for each function definition `name(...) {` (any name if None)."""
    ident = re.escape(name) if name else r"[A-Za-z_]\w*"
    for m in re.finditer(rf"(?<![\w.>:])({ident})\s*\(", text):
        if m.group(1) in CPP_NOT_FUNCS:
            continue
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start:m.start()]
        if re.search(r"(=|return|\bif\b|\bwhile\b)", prefix) or prefix.strip().startswith(("//", "#", "*")):
            continue
        depth, i = 0, m.end() - 1
        stop = min(len(text), i + 4000)          # no real parameter list is longer
        while i < stop:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        rest = text[i + 1:i + 200].lstrip()
        if depth == 0 and rest.startswith("{"):
            yield m.group(1), text.count("\n", 0, m.start()) + 1


RE_MODULE = re.compile(r"^\s*module\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)", re.M)
RE_ENTITY = re.compile(r"^\s*entity\s+(\w+)\s+is\b", re.M | re.I)
RE_HLS_TOP = re.compile(r"syn\.top\s*=\s*(\w+)|set_top\s+(\w+)|topfunction\s+name=\"(\w+)\"")


def _scan_source(path: Path, text: str) -> dict:
    """Names a source file declares: HDL modules/entities, C++ function definitions, HLS tops."""
    ext = path.suffix.lower()
    e = {"gen": _is_generated(path, text), "mods": [], "ents": [], "fns": [], "tops": []}
    if ext in HDL_EXT:
        e["mods"] = sorted(set(RE_MODULE.findall(text)))
        e["ents"] = sorted({x.lower() for x in RE_ENTITY.findall(text)})
    elif ext in CPP_EXT:
        e["fns"] = sorted({n for n, _ in _cpp_defs(text)})
    elif ext in HLS_CFG_EXT:
        e["tops"] = sorted({x for m in RE_HLS_TOP.findall(text) for x in m if x})
    return e


class SrcIndex:
    """What each file under src_roots declares, cached in the work directory by mtime and size,
    so that `src`/`outline` do not re-read every source (slow on network mounts)."""

    VERSION = 1

    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.path = ctx.base / SRC_INDEX
        self.files = {}
        self.dirty = False
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if data.get("version") == self.VERSION:
                    self.files = data.get("files") or {}
            except (OSError, ValueError, AttributeError):
                pass

    def scan(self, exts: set):
        """Yield (path, entry) for every file under src_roots with one of exts."""
        for fp in _iter_files(self.ctx, exts):
            try:
                st = fp.stat()
            except OSError:
                continue
            key = str(fp)
            e = self.files.get(key)
            if not e or e.get("mtime") != st.st_mtime_ns or e.get("size") != st.st_size:
                e = {**_scan_source(fp, read_text(fp)), "mtime": st.st_mtime_ns, "size": st.st_size}
                self.files[key] = e
                self.dirty = True
            yield fp, e

    def save(self) -> None:
        """Best effort: skipped silently when there is no writable work directory."""
        if not self.dirty or self.ctx.write_error(self.path):
            return
        self.files = {k: v for k, v in self.files.items() if os.path.exists(k)}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"version": self.VERSION, "files": self.files}))
            tmp.replace(self.path)
        except OSError:
            pass


def find_sources(d: Design, ctx: Ctx, p: str):
    """Return (top_name, [(path, generated, why)], hls_cfgs)."""
    c = d.cells[p]
    for key in (p, short(p), leafname(p)):
        if key in ctx.src_map:
            files = [(ctx.root / f).resolve() for f in ctx.src_map[key]]
            return key, [(f, False, "src_map") for f in files if f.exists()], []
    kind = c.get("kind")
    if kind in ("xilinx_ip", "hier"):
        return None, [], []
    f = (c.get("vlnv") or "").split(":")
    top = c.get("ref") or (f[2] if len(f) == 4 else leafname(p))
    hits, cfgs = [], []
    idx = SrcIndex(ctx)
    if kind in ("module_ref", "custom_ip"):
        for fp, e in idx.scan(HDL_EXT):
            if top in e["mods"] or top.lower() in e["ents"]:
                hits.append((fp, e["gen"], "module decl"))
    if kind == "hls" or (kind == "custom_ip" and not hits):
        for fp, e in idx.scan(HLS_CFG_EXT):
            if top in e["tops"]:
                cfgs.append(fp)
        for fp, e in idx.scan(CPP_EXT):
            if top in e["fns"]:
                hits.append((fp, e["gen"], "top function"))
    idx.save()
    hits.sort(key=lambda h: (h[1], str(h[0])))
    return top, hits, cfgs


def cmd_src(d: Design, ctx: Ctx, a) -> None:
    p = d.resolve(a.name)
    if p in d.ports:
        die(f"{short(p)} is a top-level port, it has no source")
    c = d.cells[p]
    kind = c.get("kind")
    top, hits, cfgs = find_sources(d, ctx, p)
    print(f"src: {short(p)}  kind: {kind}  top: {top or '-'}")
    if kind == "xilinx_ip":
        print(f"vendor IP {c.get('vlnv')}: no user source. Explain from the product guide + `bdq block` config.")
        return
    if kind == "hier":
        print("hierarchy container: no source of its own. Use `bdq block` to list children.")
        return
    for cf in cfgs:
        print(f"  hls config: {ctx.rel(cf)}")
    if not hits:
        print("  no source found. Add it to bdq.json -> \"src_map\": {\"" + short(p) + "\": [\"path/to/file\"]}")
        return
    handwritten = [h for h in hits if not h[1]]
    for fp, gen, why in hits:
        n = read_text(fp).count("\n") + 1
        print(f"  {ctx.rel(fp)}  {n} lines  sha1:{sha(fp)}  ({why}{', GENERATED - do not read' if gen else ''})")
    use = handwritten or hits
    print("notes sources line: <!-- sources: " + " ".join(f"{ctx.rel(f)}@{sha(f)}" for f, _, _ in use) + " -->")


# ---------------------------------------------------------------- outline ----
SV_KEYWORDS = {"if", "else", "for", "foreach", "while", "case", "casez", "casex", "assign", "always", "always_ff",
               "always_comb", "always_latch", "module", "function", "task", "begin", "end", "return", "input",
               "output", "inout", "logic", "wire", "reg", "int", "integer", "bit", "parameter", "localparam",
               "typedef", "generate", "endgenerate", "initial", "assert", "property", "sequence", "import",
               "export", "class", "package", "interface", "modport", "genvar", "unique", "priority", "default"}


def _clean(s: str, n: int = 90) -> str:
    s = re.sub(r"//.*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 3] + "..."


def _inst_name_after_params(chunk: str) -> str:
    """For `type #( ... ) name (`, return name by skipping the balanced #(...)."""
    i = chunk.find("#")
    j = chunk.find("(", i)
    if i < 0 or j < 0:
        return "?"
    depth = 0
    for k in range(j, len(chunk)):
        if chunk[k] == "(":
            depth += 1
        elif chunk[k] == ")":
            depth -= 1
            if depth == 0:
                m = re.match(r"\s*([A-Za-z_]\w*)\s*\(", chunk[k + 1:])
                return m.group(1) if m else "?"
    return "?"


def outline_verilog(text: str, top: str) -> list:
    lines = text.splitlines()
    m = re.search(rf"^\s*module\s+(?:automatic\s+|static\s+)?{re.escape(top)}\b", text, re.M)
    start = text.count("\n", 0, m.start()) if m else 0
    end = len(lines)
    for i in range(start, len(lines)):
        if re.match(r"^\s*endmodule\b", lines[i]):
            end = i + 1
            break
    body = lines[start:end]
    out = [f"module {top} @L{start + 1}-{end} ({end - start} lines)"]
    ports = [_clean(l) for l in body if re.match(r"^\s*(input|output|inout)\b", l)]
    if ports:
        out.append("ports: " + " | ".join(p.rstrip(",;") for p in ports[:40]) + (" | ..." if len(ports) > 40 else ""))
    params = [_clean(l, 60).rstrip(",;") for l in body if re.match(r"^\s*(parameter|localparam)\b", l)]
    if params:
        out.append("params: " + " | ".join(params[:15]) + (" | ..." if len(params) > 15 else ""))
    for em in re.finditer(r"typedef\s+enum\b[^{]*\{([^}]*)\}\s*(\w+)", "\n".join(body), re.S):
        names = [re.sub(r"=.*", "", x).strip() for x in em.group(1).split(",") if x.strip()]
        out.append(f"enum {em.group(2)}: {', '.join(names)}")
    blocks, cases, insts = [], [], []
    for i, l in enumerate(body):
        ln = start + i + 1
        if re.match(r"^\s*always(_ff|_comb|_latch)?\b", l):
            lab = re.search(r"begin\s*:\s*(\w+)", l)
            prev = body[i - 1].strip() if i else ""
            hint = lab.group(1) if lab else (prev[2:].strip()[:40] if prev.startswith("//") else "")
            blocks.append(f"@L{ln} {_clean(l, 50)}" + (f"  ({hint})" if hint else ""))
        cm = re.search(r"(?<!\w)(?:unique\s+|priority\s+)?case[zx]?\s*\(([^)]*)\)", l)
        if cm:
            cases.append(f"@L{ln} case({cm.group(1).strip()})")
        im = re.match(r"^\s*([A-Za-z_]\w*)(?:\s*#\s*\(|\s+([A-Za-z_]\w*)\s*\()", l)
        if im and im.group(1) not in SV_KEYWORDS and "=" not in l.split("(")[0]:
            name = im.group(2) or _inst_name_after_params("\n".join(body[i:i + 15]))
            insts.append(f"@L{ln} {im.group(1)} {name}")
    if blocks:
        out.append("blocks: " + " ; ".join(blocks))
    if cases:
        out.append("case: " + " ; ".join(cases[:15]))
    if insts:
        out.append("instances: " + " ; ".join(insts[:20]))
    n_assign = sum(1 for l in body if re.match(r"^\s*assign\b", l))
    if n_assign:
        out.append(f"assign statements: {n_assign}")
    return out


def outline_vhdl(text: str, top: str) -> list:
    lines = text.splitlines()
    out = []
    m = re.search(rf"^\s*entity\s+{re.escape(top)}\s+is\b", text, re.M | re.I)
    ent = lines
    if m:
        first = text.count("\n", 0, m.start())
        out.append(f"entity {top} @L{first + 1} ({len(lines)} lines in file)")
        # ports of this entity only, not of component declarations elsewhere in the file
        last = next((i for i in range(first + 1, len(lines))
                     if re.match(rf"^\s*end\b(\s+entity)?(\s+{re.escape(top)})?\s*;", lines[i], re.I)), len(lines) - 1)
        ent = lines[first:last + 1]
    ports = [_clean(l, 70) for l in ent if re.match(r"^\s*(?:port\s*\(\s*)?\w+(\s*,\s*\w+)*\s*:\s*(in|out|inout|buffer)\b", l, re.I)]
    if ports:
        out.append("ports: " + " | ".join(p.rstrip(";") for p in ports[:40]))
    for tm in re.finditer(r"type\s+(\w+)\s+is\s*\(([^)]*)\)", text, re.I | re.S):
        out.append(f"enum {tm.group(1)}: {_clean(tm.group(2), 120)}")
    procs, cases = [], []
    for i, l in enumerate(lines, 1):
        pm = re.match(r"^\s*(?:(\w+)\s*:\s*)?process\b", l, re.I)
        if pm:
            procs.append(f"@L{i} {pm.group(1) or 'process'}")
        cm = re.match(r"^\s*case\s+(.+?)\s+is\b", l, re.I)
        if cm:
            cases.append(f"@L{i} case {cm.group(1)}")
    if procs:
        out.append("processes: " + " ; ".join(procs))
    if cases:
        out.append("case: " + " ; ".join(cases[:15]))
    insts = [f"@L{text.count(chr(10), 0, im.start()) + 1} {im.group(2)} {im.group(1)}"
             for im in re.finditer(r"(?im)^\s*(\w+)\s*:\s*(?:entity\s+)?([\w.]+)\s+(?:generic|port)\s+map", text)]
    if insts:
        out.append("instances: " + " ; ".join(insts[:20]))
    return out


def outline_cpp(text: str, top: str) -> list:
    lines = text.splitlines()
    out = [f"file: {len(lines)} lines"]
    defs = [ln for _, ln in _cpp_defs(text, top)]
    if defs:
        out.append(f"top function {top} @L{', @L'.join(map(str, defs))}")
    funcs = []
    for i, l in enumerate(lines, 1):
        fm = re.match(r"^(?:static\s+|inline\s+|template\s*<[^>]*>\s*)*[A-Za-z_][\w:<>,\s\*&]*?[\s\*&]+(\w+)\s*\([^;]*$", l)
        if fm and fm.group(1) not in ("if", "for", "while", "switch", "return") and fm.group(1) != top:
            funcs.append(f"@L{i} {fm.group(1)}")
    if funcs:
        out.append("functions: " + " ; ".join(funcs[:25]))
    prag = [(i, _clean(l, 80)) for i, l in enumerate(lines, 1) if re.match(r"^\s*#pragma\s+HLS\b", l, re.I)]
    iface = [f"@L{i} {p[len('#pragma HLS '):]}" for i, p in prag if re.search(r"interface", p, re.I)]
    other = [f"@L{i} {p[len('#pragma HLS '):]}" for i, p in prag if not re.search(r"interface", p, re.I)]
    if iface:
        out.append("interface pragmas: " + " ; ".join(iface[:25]))
    if other:
        out.append("other pragmas: " + " ; ".join(other[:25]))
    loops = [f"@L{i} {m.group(1)}" for i, l in enumerate(lines, 1) if (m := re.match(r"^\s*(\w+)\s*:\s*(?:for|while)\b", l))]
    if loops:
        out.append("loop labels: " + " ; ".join(loops[:25]))
    streams = sorted(set(re.findall(r"hls::stream<\s*([^>]+?)\s*>", text)))
    if streams:
        out.append("stream types: " + ", ".join(streams[:10]))
    return out


def cmd_outline(d: Design, ctx: Ctx, a) -> None:
    p = d.resolve(a.name)
    c = d.cells.get(p)
    if c is None or c.get("kind") in ("xilinx_ip", "hier"):
        die(f"{short(p)} has no user source to outline (kind: {(c or {}).get('kind', 'port')})")
    top, hits, _ = find_sources(d, ctx, p)
    if not hits:
        die(f"no source found for {short(p)} (see `bdq src {short(p)}`)")
    files = [h for h in hits if not h[1]] or hits
    out = []
    for fp, gen, _ in files:
        text = read_text(fp)
        out.append(f"== {ctx.rel(fp)}  sha1:{sha(fp)}" + ("  (GENERATED)" if gen else ""))
        ext = fp.suffix.lower()
        if ext in (".vhd", ".vhdl"):
            out += outline_vhdl(text, top)
        elif ext in CPP_EXT:
            out += outline_cpp(text, top)
        else:
            out += outline_verilog(text, top)
    for line in out[:a.max]:
        print(line)
    if len(out) > a.max:
        print(f"... +{len(out) - a.max} lines (raise --max)")
    print("next: read only the line ranges that answer the question (Read with offset/limit)")


def _note_sources(d: Design, ctx: Ctx, p: str, extra=()) -> str:
    """`file@sha1 ...` for the block's own source (handwritten preferred) plus extra project files."""
    _, hits, _ = find_sources(d, ctx, p) if p in d.cells else (None, [], [])
    files = [f for f, _, _ in ([h for h in hits if not h[1]] or hits)]
    for x in extra:
        f = (ctx.root / x).resolve()
        if not f.is_file():
            die(f"--source {x}: no such file under the project root {ctx.root}")
        if f not in files:
            files.append(f)
    return " ".join(f"{ctx.rel(f)}@{sha(f)}" for f in files)


def _set_note(d: Design, ctx: Ctx, p: str, body: str, extra=()) -> None:
    """Write the section for block p: replace an existing one (and drop duplicates) or append it.
    The header and the sources line are generated here; body is the Role line plus bullets."""
    lines = [l for l in body.strip().splitlines()
             if not re.match(r"^\s*<!--\s*sources:", l) and not re.match(r"^##\s+" + re.escape(p) + r"\s*$", l)]
    body = "\n".join(lines).strip()
    if NOTE_HDR.search(body):
        die("the note body must not contain other '## /...' headers")
    if not re.search(r"^\s*(?:\*\*)?Role(?:\*\*)?:\s*\S", body, re.M):
        die("the note body needs a 'Role: <one line>' line (shown by bdq overview)")
    srcs = _note_sources(d, ctx, p, extra)
    section = f"## {p}\n" + (f"<!-- sources: {srcs} -->\n" if srcs else "") + body + "\n\n"

    path = ctx.assert_writable(ctx.notes_path)
    text = path.read_text(errors="ignore") if path.exists() else "# Design notes\n\n"
    heads = list(NOTE_HDR.finditer(text))
    spans = [(m.start(), heads[i + 1].start() if i + 1 < len(heads) else len(text))
             for i, m in enumerate(heads) if m.group(1) == p]
    if spans:
        out, pos = [], 0
        for k, (a0, b0) in enumerate(spans):
            out.append(text[pos:a0])
            if k == 0:
                out.append(section)
            pos = b0
        text = "".join(out) + text[pos:]
    else:
        text = text.rstrip("\n") + "\n\n" + section
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text.rstrip("\n") + "\n")
    tmp.replace(path)
    print(f"note {'updated' if spans else 'added'}: {short(p)} in {ctx.rel(path)}"
          + (f"  (sources: {srcs})" if srcs else "  (no sources: unversioned)"))


def cmd_notes(d: Design, ctx: Ctx, a) -> None:
    notes = load_notes(ctx)
    if a.set and not a.name:
        die("--set needs a block name: bdq notes NAME --set < note.md")
    if a.name:
        p = d.resolve(a.name)
        if a.set:
            _set_note(d, ctx, p, sys.stdin.read(), a.source or ())
            return
        if a.template:
            srcs = _note_sources(d, ctx, p, a.source or ())
            if p in notes:
                print(f"bdq: {ctx.rel(ctx.notes_path)} already has a section for {p}; "
                      "`bdq notes NAME --set` replaces it", file=sys.stderr)
            print(f"## {p}")
            if srcs:
                print(f"<!-- sources: {srcs} -->")
            print("Role: <one line>\n- Verified: <facts with file:line>\n- Inferred: <deductions, not checked in code>")
            return
        note = notes.get(p)
        if not note:
            print(f"no note for {short(p)} in {ctx.rel(ctx.notes_path)}")
            return
        print(f"[{note_status(ctx, note)}]\n{note['body']}")
        return
    if not notes:
        print(f"no notes yet ({ctx.rel(ctx.notes_path)})")
        return
    for path, note in sorted(notes.items()):
        print(f"{short(path)}  [{note_status(ctx, note)}]  {note['role'] or ''}")


# ----------------------------------------------------------------- export ----
def cmd_export(ctx: Ctx, a) -> None:
    """Run Vivado read-only in a throw-away directory and write the graph into the work directory.

    Nothing is written in the project: the Tcl script is sent on stdin, Vivado runs in a temporary
    directory (removed afterwards) and opens the project with -read_only, and the JSON comes back
    on stdout.
    """
    viv = ctx.cfg.get("vivado") or {}
    missing = [k for k in ("xpr", "bd") if not viv.get(k)]
    if missing:
        die(f"missing {', '.join('--' + m for m in missing)}: pass them, set them in bdq.json \"vivado\", "
            "or run `bdq discover` to list candidates")
    if ctx.cfg.get("remote"):
        die('bdq.json has a "remote" section, but bdq only runs Vivado locally: remove it')
    proj = str(ctx.root)
    out_path = ctx.assert_writable(ctx.graph_path)
    save_name = (a.save if isinstance(a.save, str) else Path(viv["bd"]).stem) if a.save else None
    if save_name:
        ctx.assert_writable(ctx.cfg_file)

    def absol(x: str) -> str:
        return x if x.startswith("/") else f"{proj}/{x}"

    bd = absol(viv["bd"]) if viv["bd"].endswith(".bd") else viv["bd"]
    targs = [absol(viv["xpr"]), bd, "$d/bd_graph.json"] + ([viv["skip_config"]] if viv.get("skip_config") else [])
    settings = viv.get("settings")
    vivado_bin = viv.get("vivado", "vivado")
    timeout = a.timeout or viv.get("timeout") or DEFAULT_EXPORT_TIMEOUT
    script = ('d=$(mktemp -d) && trap \'rm -rf "$d"\' EXIT && cat > "$d/bd_export.tcl" && cd "$d" && '
              + (f"source {shlex.quote(settings)} >/dev/null && " if settings else "")
              + f'{shlex.quote(vivado_bin)} -mode batch -nojournal -nolog -source "$d/bd_export.tcl" -tclargs '
              + " ".join('"$d/bd_graph.json"' if x == "$d/bd_graph.json" else shlex.quote(x) for x in targs)
              + ' 1>&2 && cat "$d/bd_graph.json"')
    cmd = ["bash", "-c", script]

    if a.dry_run:
        print(f"profile: {ctx.profile or '-'}")
        print("Vivado runs locally in a temp dir and opens the project -read_only")
        print(f"project: {proj}   xpr: {absol(viv['xpr'])}   bd: {bd}")
        print(f"output: {out_path}   timeout: {timeout} s")
        print(f"script (bd_export.tcl on stdin): {script}")
        if save_name:
            _save_profile(ctx, save_name)
        return

    print("bdq export: running Vivado (this takes a while)...")
    tcl = Path(__file__).with_name("bd_export.tcl").read_text()
    try:
        # own process group, so that a timeout also stops Vivado and not only the bash wrapper
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except OSError as e:
        die(f"cannot run {cmd[0]}: {e.strerror}")
    try:
        stdout, stderr = proc.communicate(tcl, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            proc.kill()
        proc.communicate()
        die(f"export timed out after {timeout} s (raise it with --timeout or \"vivado\": {{\"timeout\": N}})")
    res = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    log = res.stderr.strip().splitlines()
    tail = [l for l in log if l.startswith(("bdx_export", "bd_export", "ERROR", "CRITICAL WARNING"))] or log[-15:]
    print("\n".join(tail[-20:]))
    if res.returncode != 0:
        die(f"Vivado exited with code {res.returncode}")
    m = re.search(r'\{\s*"meta"', res.stdout)   # skip anything settings64.sh printed first
    if not m:
        die("export produced no graph on stdout")
    try:
        data = json.loads(res.stdout[m.start():])
    except json.JSONDecodeError as e:
        die(f"exported graph is not valid JSON: {e}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(out_path)
    print(f"graph updated: {ctx.rel(out_path)}  cells: {len(data.get('cells', []))}  "
          f"intf nets: {len(data.get('intf_nets', []))}  nets: {len(data.get('nets', []))}")
    if save_name:
        _save_profile(ctx, save_name)


def _save_profile(ctx: Ctx, name: str) -> None:
    """Persist the effective export parameters as a named profile in the work directory's bdq.json."""
    cfg_file = ctx.assert_writable(ctx.cfg_file)
    raw = dict(ctx.raw)
    if ctx.cli_root or "project_root" not in raw:
        raw["project_root"] = str(ctx.root)
    prof = {"vivado": ctx.cfg.get("vivado", {}), "graph": ctx.graph_rel}
    if "src_roots" in ctx.cfg:
        prof["src_roots"] = ctx.cfg["src_roots"]
    raw.setdefault("profiles", {})[name] = prof
    raw.setdefault("default_profile", name)
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_file.with_name(cfg_file.name + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2) + "\n")
    tmp.replace(cfg_file)
    print(f"saved profile '{name}' in {cfg_file}" + (" (default)" if raw["default_profile"] == name else ""))
    if not ctx.legacy:
        registry_set(ctx.root, cfg_file)   # later runs inside the project find this config by themselves


def cmd_config(ctx: Ctx, a) -> None:
    exists = bool(ctx.cfg_file and ctx.cfg_file.exists())
    if a.register or a.unregister:
        if not exists:
            die("no bdq.json to register: run the first export with --save, or pass --config")
        if ctx.legacy:
            die("the work directory is the project root or contains it: move bdq.json to a dedicated "
                "folder before registering")
        registry_set(ctx.root, None if a.unregister else ctx.cfg_file)
        return
    profiles = ctx.raw.get("profiles") or {}
    print(f"config: {ctx.cfg_file if exists else '(no bdq.json)'}"
          + (f"  [{ctx.cfg_source}]" if exists and ctx.cfg_source else ""))
    print(f"workdir (only writable place): {ctx.home or '(none: pass --workdir)'}")
    print(f"project (read-only): {ctx.root}")
    if ctx.legacy:
        print("WARNING: the work directory is the project root or contains it: bdq refuses to write there. "
              "Move bdq.json to a dedicated folder and set project_root.")
    print(f"profiles: {', '.join(profiles) or '-'}  | default: {ctx.raw.get('default_profile') or '-'}"
          f"  | active: {ctx.profile or '-'}")
    viv = ctx.cfg.get("vivado") or {}
    print(f"vivado: xpr={viv.get('xpr', '?')} bd={viv.get('bd', '?')} settings={viv.get('settings', '-')}"
          f" skip_config={viv.get('skip_config', '-')}")
    print(f"graph: {ctx.rel(ctx.graph_path)} ({'exists' if ctx.graph_path.exists() else 'missing'})")
    print(f"src_roots: {', '.join(ctx.rel(r) for r in ctx.src_roots)}")
    print(f"notes: {ctx.rel(ctx.notes_path)}")
    print(f"confirm_before_export: {bool(ctx.raw.get('confirm_before_export'))}")
    reg = load_registry().get(str(ctx.root))
    if exists and reg == str(ctx.cfg_file):
        print(f"registry: project registered ({registry_path()})")
    elif exists:
        print("registry: project not registered" + (f" (registered to {reg})" if reg else "")
              + " -> `bdq config --register` lets bdq find this config from inside the project")


def cmd_discover(ctx: Ctx, a) -> None:
    """List candidate projects, block designs and existing graphs, to propose choices to the user."""
    xprs, bds, graphs = [], [], []
    for dp, dns, fns in os.walk(ctx.root):
        dns[:] = [x for x in dns if x not in SKIP_DIRS and not x.endswith(SKIP_DIR_SUFFIXES)
                  and (ctx.home is None or (Path(dp) / x).resolve() != ctx.home)]
        if len(Path(dp).relative_to(ctx.root).parts) > a.max_depth:
            dns[:] = []
            continue
        for f in fns:
            fp = Path(dp) / f
            if f.endswith(".xpr"):
                xprs.append(ctx.rel(fp))
            elif f.endswith(".bd"):
                bds.append(f"{fp.stem}  ({ctx.rel(fp)})")
    if ctx.home and ctx.home.exists():
        graphs = [ctx.rel(g) for g in ctx.home.rglob("bd_graph*.json")]
    print(f"project root: {ctx.root}")
    print("projects (.xpr): " + (", ".join(sorted(xprs)) or "-"))
    print("block designs (.bd):" + ("" if bds else " -"))
    for b in sorted(bds):
        print(f"  {b}")
    print("existing graphs in workdir: " + (", ".join(sorted(graphs)) or "-"))
    profiles = ctx.raw.get("profiles") or {}
    if profiles:
        print("profiles: " + ", ".join(f"{k} (bd={(v.get('vivado') or {}).get('bd', '?')})" for k, v in profiles.items()))


# ------------------------------------------------------------------- main ----
def main(argv=None) -> None:
    if hasattr(signal, "SIGPIPE"):   # quiet exit when piped into head/less
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ap = argparse.ArgumentParser(prog="bdq", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", help="path to bd_graph.json (default: profile / bdq.json / BDQ_GRAPH / auto)")
    ap.add_argument("--profile", help="named profile from bdq.json (default: BDQ_PROFILE or default_profile)")
    ap.add_argument("--config", help="path to bdq.json (default: --workdir, BDQ_CONFIG, the project registry, or search upwards)")
    ap.add_argument("--workdir", help="work directory holding bdq.json, graphs and notes (the only writable place)")
    ap.add_argument("--project-root", dest="project_root", help="Vivado project root (read-only); saved in bdq.json")
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("overview"); s.add_argument("--depth", type=int, default=2)
    s = sp.add_parser("block"); s.add_argument("name"); s.add_argument("--params", choices=("user", "prop", "all"), default="user")
    s = sp.add_parser("neighbors"); s.add_argument("name"); s.add_argument("--depth", type=int, default=1); s.add_argument("--with-clocks", action="store_true")
    s = sp.add_parser("path"); s.add_argument("src"); s.add_argument("dst"); s.add_argument("--k", type=int, default=3); s.add_argument("--max-hops", type=int, default=12)
    s = sp.add_parser("clocks"); s.add_argument("name")
    s = sp.add_parser("find"); s.add_argument("pattern")
    s = sp.add_parser("addr"); s.add_argument("filter", nargs="?")
    s = sp.add_parser("src"); s.add_argument("name")
    s = sp.add_parser("outline"); s.add_argument("name"); s.add_argument("--max", type=int, default=60)
    s = sp.add_parser("notes"); s.add_argument("name", nargs="?"); s.add_argument("--template", action="store_true")
    s.add_argument("--set", action="store_true", help="write the note for NAME from stdin (Role line + bullets), "
                   "replacing any existing one; header and sources line are generated")
    s.add_argument("--source", action="append", metavar="FILE",
                   help="extra source file (relative to the project root) the note depends on; repeatable")
    s = sp.add_parser("export")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--xpr", help="Vivado project file, relative to the project root")
    s.add_argument("--bd", help="block design name (or .bd path)")
    s.add_argument("--out", help="output graph path, relative to the work directory (must stay inside it)")
    s.add_argument("--settings", help="Vivado settings64.sh to source before running")
    s.add_argument("--skip-config", dest="skip_config", help="glob of cells whose CONFIG is not exported")
    s.add_argument("--timeout", type=int, help=f"seconds before the export is aborted (default {DEFAULT_EXPORT_TIMEOUT})")
    s.add_argument("--src-root", dest="src_root", action="append", help="source folder (repeatable; saved with --save)")
    s.add_argument("--save", nargs="?", const=True, metavar="PROFILE",
                   help="save these parameters as a profile in bdq.json (default name: the bd name)")
    s = sp.add_parser("config")
    s.add_argument("--register", action="store_true", help="map this project root to this bdq.json in the registry")
    s.add_argument("--unregister", action="store_true", help="remove this project root from the registry")
    s = sp.add_parser("discover"); s.add_argument("--max-depth", type=int, default=6)
    a = ap.parse_args(argv)
    if a.cmd in ("export", "config", "discover"):
        globals()[f"cmd_{a.cmd}"](Ctx(a, need_graph=False), a)
        return
    ctx = Ctx(a)
    data = load_json(ctx.graph_path)
    if not isinstance(data, dict):
        die(f"{ctx.graph_path}: not a bd_graph.json (expected a JSON object)")
    d = Design(data)
    globals()[f"cmd_{a.cmd}"](d, ctx, a)


if __name__ == "__main__":
    main()
