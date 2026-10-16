"""Dependency graph built from the stored inventory.

Node ids: "component:<Name>", "port:<Component>.<Port>" (component "?" when unknown),
"interface:<Name>", "signal:<Name>". Node attrs: type, name, norm, citation, label.
Edges (attr `type`): has_port (component->port), uses_interface (port->interface),
sends (component->signal), receives (signal->component),
provides_to (provided port -> required port with the same normalised interface, other component).
Component-level data flow is provider/sender -> consumer/receiver.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import networkx as nx

from app.config import settings
from app.extract import get_inventory, normalize_name


def _cite(row: dict) -> str:
    src = (row.get("sources") or [{}])[0]
    p = src.get("page")
    return f"{row.get('doc_id')} v{row.get('version')} p.{p} s.{src.get('section', '')}".strip()


def _ensure_component(g: nx.MultiDiGraph, name: str) -> str | None:
    if not name:
        return None
    nid = f"component:{name}"
    norm = normalize_name(name)
    for n, d in g.nodes(data=True):
        if d["type"] == "component" and d["norm"] == norm:
            return n
    g.add_node(nid, type="component", name=name, norm=norm, citation=None, label=name, inferred=True)
    return nid


def build_graph(project_id: str, doc_id: str, version: str | None, conn=None) -> nx.MultiDiGraph:
    inv = get_inventory(project_id, doc_id, version, conn, include_rejected=False)
    g = nx.MultiDiGraph(project_id=project_id, doc_id=doc_id, version=version)
    for r in inv["components"]:
        g.add_node(f"component:{r['name']}", type="component", name=r["name"], norm=r["norm_name"],
                   citation=_cite(r), label=r["name"], description=r.get("description", ""))
    for r in inv["interfaces"]:
        g.add_node(f"interface:{r['name']}", type="interface", name=r["name"], norm=r["norm_name"],
                   citation=_cite(r), label=r["name"], kind=r.get("kind", "unknown"))
    ports = []
    for r in inv["ports"]:
        comp = r.get("component") or ""
        pid = f"port:{comp or '?'}.{r['name']}"
        g.add_node(pid, type="port", name=r["name"], norm=r["norm_name"], citation=_cite(r), label=r["name"],
                   direction=r.get("direction", "unknown"), component=comp, interface=r.get("interface", ""))
        cid = _ensure_component(g, comp)
        if cid:
            g.add_edge(cid, pid, type="has_port")
        if r.get("interface"):
            iname = r["interface"]
            iid = next((n for n, d in g.nodes(data=True)
                        if d["type"] == "interface" and d["norm"] == normalize_name(iname)), None)
            if iid is None:
                iid = f"interface:{iname}"
                g.add_node(iid, type="interface", name=iname, norm=normalize_name(iname), citation=None,
                           label=iname, kind="unknown", inferred=True)
            g.add_edge(pid, iid, type="uses_interface")
        ports.append((pid, g.nodes[pid]))
    for r in inv["signals"]:
        sid = f"signal:{r['name']}"
        g.add_node(sid, type="signal", name=r["name"], norm=r["norm_name"], citation=_cite(r), label=r["name"],
                   data_type=r.get("data_type", ""))
        for snd in [r.get("sender", "")] + list(r.get("extra_senders") or []):
            s = _ensure_component(g, snd)
            if s and not g.has_edge(s, sid):
                g.add_edge(s, sid, type="sends")
        for rc in r.get("receivers") or []:
            c = _ensure_component(g, rc)
            if c:
                g.add_edge(sid, c, type="receives")
    # provides_to: provided port -> required port, same normalised interface, different component
    for pid, pd in ports:
        if pd["direction"] != "provided" or not pd["interface"]:
            continue
        for qid, qd in ports:
            if (qd["direction"] == "required" and normalize_name(qd["interface"]) == normalize_name(pd["interface"])
                    and normalize_name(qd["component"]) != normalize_name(pd["component"])):
                g.add_edge(pid, qid, type="provides_to")
    return g


# ---------------------------------------------------------------- queries

def _comp_id(g: nx.MultiDiGraph, component: str) -> str:
    if component in g and g.nodes[component]["type"] == "component":
        return component
    norm = normalize_name(component.split(":", 1)[-1])
    for n, d in g.nodes(data=True):
        if d["type"] == "component" and d["norm"] == norm:
            return n
    raise KeyError(f"unknown component: {component}")


def component_flow_graph(g: nx.MultiDiGraph) -> nx.DiGraph:
    """Component-level data-flow graph: provider/sender -> consumer/receiver."""
    f = nx.DiGraph()
    f.add_nodes_from(n for n, d in g.nodes(data=True) if d["type"] == "component")

    def owner(pid):
        return next((u for u, _, k in g.in_edges(pid, data="type") if k == "has_port"), None)

    for u, v, k in g.edges(data="type"):
        if k == "provides_to":
            a, b = owner(u), owner(v)
            if a and b:
                f.add_edge(a, b)
        elif k == "sends":
            for _, r, k2 in g.out_edges(v, data="type"):
                if k2 == "receives":
                    f.add_edge(u, r)
    return f


def dependencies_of(g: nx.MultiDiGraph, component: str) -> list[str]:
    """Components that `component` depends on (its providers/senders)."""
    f, c = component_flow_graph(g), _comp_id(g, component)
    return sorted(g.nodes[n]["name"] for n in f.predecessors(c))


def dependents_of(g: nx.MultiDiGraph, component: str) -> list[str]:
    """Components that depend on `component` (its consumers/receivers)."""
    f, c = component_flow_graph(g), _comp_id(g, component)
    return sorted(g.nodes[n]["name"] for n in f.successors(c))


def paths(g: nx.MultiDiGraph, a: str, b: str, cutoff: int = 8) -> list[list[str]]:
    """All simple data-flow paths a -> b as lists of component names."""
    f = component_flow_graph(g)
    return [[g.nodes[n]["name"] for n in p]
            for p in nx.all_simple_paths(f, _comp_id(g, a), _comp_id(g, b), cutoff=cutoff)]


# ---------------------------------------------------------------- export

_COLORS = {"component": "#4c78a8", "port": "#f58518", "interface": "#54a24b", "signal": "#b279a2"}


def graph_dir(project_id: str, processed_dir=None) -> Path:
    base = Path(processed_dir) if processed_dir else settings.processed_dir
    d = base / project_id / "graphs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def export_json(g: nx.MultiDiGraph, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"graph": dict(g.graph),
            "nodes": [{"id": n, **d} for n, d in g.nodes(data=True)],
            "edges": [{"source": u, "target": v, "type": k} for u, v, k in g.edges(data="type")]}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


_REMOTE_TAG = re.compile(r'<link[^>]*https?://[^>]*>|<script[^>]*src="https?://[^"]*"[^>]*>\s*</script>', re.S)
_EMPTY_HEADING = re.compile(r"<center>\s*<h1>\s*</h1>\s*</center>")
_FRAME_CSS = "<style>html,body{margin:0;height:100%;background:#fff}#mynetwork{border:0!important}</style>"


def _offline(html: str) -> str:
    """pyvis inlines vis-network with cdn_resources="in_line" but its template still links Bootstrap from a CDN
    (only used for a card class); drop those tags so the page needs no network at all."""
    html = _EMPTY_HEADING.sub("", _REMOTE_TAG.sub("", html))
    return html.replace("</head>", _FRAME_CSS + "</head>", 1)


def export_pyvis(g: nx.MultiDiGraph, path) -> Path:
    """Self-contained pyvis HTML (vis-network inlined, no external requests), sized to fill its frame."""
    from pyvis.network import Network
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    net = Network(height="100vh", width="100%", directed=True, notebook=False, cdn_resources="in_line")
    for n, d in g.nodes(data=True):
        net.add_node(n, label=d["label"], title=f"{d['type']}: {d['name']}\n{d.get('citation') or 'inferred'}",
                     color=_COLORS.get(d["type"], "#999999"))
    for u, v, k in g.edges(data="type"):
        net.add_edge(u, v, label=k, title=k)
    html = _offline(net.generate_html(notebook=False))
    path.write_text(html, encoding="utf-8")
    return path
