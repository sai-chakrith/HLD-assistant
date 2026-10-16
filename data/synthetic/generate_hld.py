"""Generate the synthetic Window Lift ECU HLD (v1, v2) and data/synthetic/ground_truth.json.

Usage: python data/synthetic/generate_hld.py [out_dir]

The PDFs mimic the real AUTOSAR layout so app/ingest.py parses them unchanged: a header block
(top 11% of page), footer ("N of M" + "Document ID 999: SYN_WindowLift_HLD", bottom 11%), a cover page
with a "Document Change History" table, a dotted table of contents page, then bold numbered headings
(16/14/12pt) with gridded tables whose headers match app/extract.py.

Port-side kind (used by rule M2): the port table has an optional "Kind" column. A port whose Kind differs
from the Kind of the interface it references (in the interface table) is a kind mismatch.
"-" in a cell means "empty" (missing description / interface / receivers).

V1 plants exactly one defect per rule (see PLANTED_V1). v2 fixes M3, C2, C4, adds WinLockHandler (+ port),
removes WinSwitchHandler.RpLockState and changes IDiagData from Sender-Receiver to Client-Server.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Frame, KeepTogether, PageBreak, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

DOC_NAME = "Window Lift ECU - High Level Design"
DOC_ID_LINE = "Document ID 999: SYN_WindowLift_HLD"
KIND_LABEL = {"sender_receiver": "Sender-Receiver", "client_server": "Client-Server", "": "-"}

# ------------------------------------------------------------------ model

COMPONENTS_V1 = [
    ("WinSwitchHandler", "Reads the driver and passenger window switches and debounces them", "Sensor"),
    ("WinCtrlManager", "Coordinates window movement requests and the window state machine", "Application"),
    ("MotorDriver", "Drives the window lift motor through an H-bridge", "Actuator"),
    ("PositionSensor", "Provides the filtered window position in percent", "Sensor"),
    ("AntiPinchMonitor", "Detects obstructions while the window is closing", "Application"),
    ("DiagHandler", "Stores and reports window lift diagnostic trouble codes", "Service"),
    ("VehicleSpeedAdapter", "", "Adapter"),  # C4: description missing
]
INTERFACES_V1 = [  # name, kind, data elements
    ("ISwitchCmd", "sender_receiver", ["SwitchState", "SwitchSide"]),
    ("IWinPosition", "sender_receiver", ["PositionPercent"]),
    ("IMotorCmd", "client_server", ["MoveUp", "MoveDown", "Stop"]),
    ("IMotorCmnd", "client_server", ["Brake"]),  # M4: near duplicate of IMotorCmd
    ("IAntiPinch", "sender_receiver", ["PinchDetected"]),
    ("IVehSpeed", "sender_receiver", ["VehicleSpeedKph"]),
    ("IDiagData", "sender_receiver", ["WinFaultCode"]),
    ("IBattVoltage", "sender_receiver", ["BattVoltageMv"]),
]
# component, port, direction, interface ("" = none), port kind override (None = derive from interface)
PORTS_V1 = [
    ("WinSwitchHandler", "PpSwitchCmd", "provided", "ISwitchCmd", None),
    ("WinSwitchHandler", "RpLockState", "required", "IWinLockState", None),
    ("WinCtrlManager", "RpSwitchCmd", "required", "ISwitchCmd", None),
    ("WinCtrlManager", "RpPosition", "required", "IWinPosition", "client_server"),  # M2: kind mismatch
    ("WinCtrlManager", "RpPinch", "required", "IAntiPinch", None),
    ("WinCtrlManager", "RpMotorCmd", "required", "IMotorCmd", None),
    ("WinCtrlManager", "PpLockState", "provided", "IWinLockState", None),  # C3: interface never defined
    ("MotorDriver", "PpMotorCmd", "provided", "IMotorCmd", None),
    ("MotorDriver", "PpDiagData", "provided", "IDiagData", None),
    ("PositionSensor", "PpPosition", "provided", "IWinPosition", None),
    ("AntiPinchMonitor", "PpPinch", "provided", "IAntiPinch", None),
    ("AntiPinchMonitor", "RpPosition", "required", "IWinPosition", None),
    ("AntiPinchMonitor", "RpVehSpeed", "required", "IVehSpeed", None),
    ("AntiPinchMonitor", "RpBattVoltage", "required", "IBattVoltage", None),  # M1: no provider anywhere
    ("VehicleSpeedAdapter", "PpVehSpeed", "provided", "IVehSpeed", None),
    ("DiagHandler", "RpDiagData", "required", "IDiagData", None),
    ("DiagHandler", "PpDiagState", "provided", "", None),  # C2: port without interface
]
SIGNALS_V1 = [  # name, sender, receivers, data type
    ("SwitchRequest", "WinSwitchHandler", ["WinCtrlManager"], "uint8"),
    ("WindowPosition", "PositionSensor", ["WinCtrlManager", "AntiPinchMonitor"], "uint8"),
    ("MotorCommand", "WinCtrlManager", ["MotorDriver"], "uint8"),
    ("PinchDetected", "AntiPinchMonitor", ["WinCtrlManager"], "boolean"),
    ("VehicleSpeed", "VehicleSpeedAdapter", ["AntiPinchMonitor"], "uint16"),
    ("MotorCurrent", "MotorDriver", [], "uint16"),  # M3: sender without receivers
]

FLOWS_V1 = [
    ("Functional Flow Manual Window Operation", [
        "WinSwitchHandler reads the switch state and sends SwitchRequest to WinCtrlManager.",
        "WinCtrlManager validates the request against the current window state.",
        "WinCtrlManager sends MotorCommand to MotorDriver.",
        "MotorDriver energises the motor and the window moves.",
        "PositionSensor reports WindowPosition to WinCtrlManager until the end position is reached.",
    ]),
    ("Functional Flow Automatic Close with Anti-Pinch", [
        "WinSwitchHandler sends a SwitchRequest with the auto-close flag to WinCtrlManager.",
        "VehicleSpeedAdapter provides VehicleSpeed to AntiPinchMonitor to select the pinch threshold.",
        "AntiPinchMonitor evaluates WindowPosition and the motor load while the window closes.",
        "AntiPinchMonitor sends PinchDetected to WinCtrlManager when an obstruction is found.",
        "WinCtrlManager commands MotorDriver to reverse the window.",
    ]),
    ("Functional Flow Window Position Calibration", [
        "WinCtrlManager commands MotorDriver to drive the window to the upper end stop.",
        "PositionSensor reports WindowPosition and WinCtrlManager stores the reference position.",
        "WinCtrlManager commands MotorDriver to drive the window to the lower end stop.",
    ]),
]  # DiagHandler appears in no flow section: C1

PLANTED_V1 = [
    ("M1", ["AntiPinchMonitor.RpBattVoltage"]),
    ("M2", ["IWinPosition", "WinCtrlManager.RpPosition"]),
    ("M3", ["MotorCurrent"]),
    ("M4", ["IMotorCmd", "IMotorCmnd"]),
    ("C1", ["DiagHandler"]),
    ("C2", ["DiagHandler.PpDiagState"]),
    ("C3", ["IWinLockState"]),
    ("C4", ["VehicleSpeedAdapter"]),
]
FIXED_IN_V2 = {"M3", "C2", "C4"}


def _build(version: str) -> dict:
    comps = [{"name": n, "description": d, "category": c} for n, d, c in COMPONENTS_V1]
    ifaces = [{"name": n, "kind": k, "data_elements": list(de)} for n, k, de in INTERFACES_V1]
    ports = [{"component": c, "name": p, "direction": d, "interface": i, "_kind": k}
             for c, p, d, i, k in PORTS_V1]
    sigs = [{"name": n, "sender": s, "receivers": list(r), "data_type": t} for n, s, r, t in SIGNALS_V1]
    flows = copy.deepcopy(FLOWS_V1)
    if version == "2.0":
        for c in comps:  # C4 fix
            if c["name"] == "VehicleSpeedAdapter":
                c["description"] = "Adapts the vehicle speed from the bus to the window lift software"
        for p in ports:  # C2 fix
            if p["name"] == "PpDiagState":
                p["interface"] = "IDiagData"
        for s in sigs:  # M3 fix
            if s["name"] == "MotorCurrent":
                s["receivers"] = ["DiagHandler"]
        comps.append({"name": "WinLockHandler", "category": "Application",
                      "description": "Blocks rear window requests while the child lock is active"})
        ports.append({"component": "WinLockHandler", "name": "RpPosition", "direction": "required",
                      "interface": "IWinPosition", "_kind": None})
        ports = [p for p in ports if not (p["component"] == "WinSwitchHandler" and p["name"] == "RpLockState")]
        for i in ifaces:  # interface kind change
            if i["name"] == "IDiagData":
                i["kind"] = "client_server"
        flows[1][1].insert(2, "WinLockHandler blocks the request for rear windows while the child lock is active.")
    kind_of = {i["name"]: i["kind"] for i in ifaces}
    for p in ports:
        k = p.pop("_kind")
        p["kind"] = k or kind_of.get(p["interface"], "")
    return {"components": comps, "ports": ports, "interfaces": ifaces, "signals": sigs, "flows": flows}


def port_key(p: dict) -> str:
    return f"{p['component']}.{p['name']}"


ENTITY_KEYS = {"components": lambda e: e["name"], "ports": port_key,
               "interfaces": lambda e: e["name"], "signals": lambda e: e["name"]}


def _diff(a: dict, b: dict) -> dict:
    out: dict = {}
    for t, key in ENTITY_KEYS.items():
        ma, mb = {key(e): e for e in a[t]}, {key(e): e for e in b[t]}
        changed = {}
        for k in ma.keys() & mb.keys():
            f = {x: {"old": ma[k][x], "new": mb[k][x]} for x in ma[k] if ma[k][x] != mb[k].get(x)}
            if f:
                changed[k] = f
        out[t] = {"added": sorted(mb.keys() - ma.keys()), "removed": sorted(ma.keys() - mb.keys()),
                  "changed": dict(sorted(changed.items()))}
    return out


def ground_truth() -> dict:
    v1, v2 = _build("1.0"), _build("2.0")
    ents = lambda m: {t: m[t] for t in ENTITY_KEYS}  # noqa: E731
    planted = [{"rule_id": r, "entities": e, "version": "1.0"} for r, e in PLANTED_V1]
    remaining = [{"rule_id": r, "entities": e, "version": "2.0"} for r, e in PLANTED_V1 if r not in FIXED_IN_V2]
    return {
        "project_id": "synthetic-ecu",
        "documents": {"1.0": {"doc_id": "syn_hld_v1", "file": "hld_v1.pdf"},
                      "2.0": {"doc_id": "syn_hld_v2", "file": "hld_v2.pdf"}},
        "entities": {"1.0": ents(v1), "2.0": ents(v2)},
        "flows": {"1.0": v1["flows"], "2.0": v2["flows"]},
        "planted_defects": planted,
        "remaining_defects_v2": remaining,
        "expected_diff": {"from": "1.0", "to": "2.0", "entity_key": {
            "components": "name", "ports": "Component.Port", "interfaces": "name", "signals": "name"},
            **_diff(v1, v2)},
    }


# ------------------------------------------------------------------ rendering

def _styles() -> dict:
    base = dict(fontName="Helvetica", fontSize=10, leading=14)
    return {
        "body": ParagraphStyle("body", **base, spaceAfter=6),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5),
        "hcell": ParagraphStyle("hcell", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5),
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, spaceBefore=6, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=10, spaceAfter=6),
        "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=12, leading=16, spaceBefore=8, spaceAfter=4),
        "cover": ParagraphStyle("cover", fontName="Helvetica", fontSize=20, leading=26, spaceAfter=10),
        "toc": ParagraphStyle("toc", **base),
    }


def _table(st: dict, header: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(h, st["hcell"]) for h in header]]
    data += [[Paragraph((c or "-").replace("&", "&amp;"), st["cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.7, colors.black),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _history(st: dict, version: str) -> Table:
    rows = [["Document Change History", "", "", ""], ["Date", "Version", "Changed by", "Change Description"],
            ["12.03.2025", "1.0", "A. Author", "Initial version of the Window Lift ECU HLD"]]
    if version == "2.0":
        rows.insert(2, ["02.06.2025", "2.0", "A. Author",
                        "Added WinLockHandler, fixed missing descriptions, signal receivers and port interface"])
    data = [[Paragraph(c, st["hcell"] if i < 2 else st["cell"]) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=[70, 55, 80, 290])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.7, colors.black), ("SPAN", (0, 0), (-1, 0)),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _story(version: str, m: dict) -> list:
    st = _styles()
    P = lambda t, s="body": Paragraph(t, st[s])  # noqa: E731
    story: list = [Spacer(1, 40), P(DOC_NAME, "cover"), P(f"Version {version}", "body"),
                   P("Status: Synthetic evaluation document (fictional ECU)", "body"), Spacer(1, 30),
                   _history(st, version), PageBreak()]
    toc = ["1 Introduction", "2 Architecture Overview", "3 Interface Definitions", "4 Signal Overview",
           "5 Functional Flows", "6 Constraints"]
    story.append(P("Table of Contents", "body"))
    for i, t in enumerate(toc):
        story.append(P(f"{t} ........................................ {i + 3}", "toc"))
    story.append(PageBreak())

    story += [P("1 Introduction", "h1"),
              P("This document describes the high level design of the Window Lift ECU, a fictional "
                "body controller function used to evaluate the HLD assistant."),
              P("1.1 Purpose", "h2"),
              P("The document lists the software components, their ports, the interfaces they use and the "
                "signals exchanged between them."),
              P("1.2 Scope", "h2"),
              P("The Window Lift ECU moves the four door windows on driver or passenger request and protects "
                "occupants against pinching."),
              PageBreak()]

    story += [P("2 Architecture Overview", "h1"),
              P("The software is split into the units listed below. Each unit owns a set of ports."),
              P("2.1 Component Overview", "h2"),
              _table(st, ["Component", "Description", "Category"],
                     [[c["name"], c["description"], c["category"]] for c in m["components"]], [120, 275, 80]),
              PageBreak()]

    story += [P("3 Interface Definitions", "h1"), P("3.1 Interface Table", "h2"),
              _table(st, ["Interface", "Kind", "Data Elements"],
                     [[i["name"], KIND_LABEL[i["kind"]], ", ".join(i["data_elements"])] for i in m["interfaces"]],
                     [110, 100, 265]),
              PageBreak(), P("3.2 Port Table", "h2"),
              _table(st, ["Component", "Port", "Direction", "Interface", "Kind"],
                     [[p["component"], p["name"], p["direction"], p["interface"], KIND_LABEL[p["kind"]]]
                      for p in m["ports"]], [110, 100, 65, 100, 90]),
              PageBreak()]

    story += [P("4 Signal Overview", "h1"), P("4.1 Signal Table", "h2"),
              _table(st, ["Signal", "Sender", "Receivers", "Data Type"],
                     [[s["name"], s["sender"], ", ".join(s["receivers"]), s["data_type"]] for s in m["signals"]],
                     [105, 115, 175, 80]),
              PageBreak()]

    story.append(P("5 Functional Flows", "h1"))
    for n, (title, steps) in enumerate(m["flows"], 1):
        block = [P(f"5.{n} {title}", "h2")]
        block += [P(f"{i}. {s}") for i, s in enumerate(steps, 1)]
        story.append(KeepTogether(block))
    story += [PageBreak(), P("6 Constraints", "h1"),
              P("All window movements stop when the supply voltage leaves the 9 V to 16 V range."),
              P("6.1 Timing", "h2"), P("A window request is answered within 50 ms."),
              P("6.2 Safety", "h2"), P("Anti-pinch protection is active for every closing movement.")]
    return story


def render_pdf(path: Path, version: str) -> Path:
    m = _build(version)
    W, H = A4
    state = {"page": 0}

    def chrome(c, doc):
        state["page"] += 1
        c.saveState()
        c.setFont("Helvetica", 9)
        c.drawString(50, H - 36, DOC_NAME)
        c.drawString(50, H - 48, f"Version {version}")
        c.drawString(50, 44, f"{state['page']} of {TOTAL[0]}")
        c.drawString(50, 32, DOC_ID_LINE)
        c.restoreState()

    TOTAL = [0]
    for _ in range(2):  # pass 1 counts pages, pass 2 prints "N of M"
        state["page"] = 0
        doc = BaseDocTemplate(str(path), pagesize=A4, title=DOC_NAME, author="Synthetic")
        frame = Frame(50, 110, W - 100, H - 220, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=chrome)])
        doc.build(_story(version, m))
        TOTAL[0] = state["page"]
    return path


def generate(out_dir: Path | str | None = None) -> dict[str, Path]:
    out = Path(out_dir) if out_dir else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    paths = {"v1": render_pdf(out / "hld_v1.pdf", "1.0"), "v2": render_pdf(out / "hld_v2.pdf", "2.0")}
    gt = out / "ground_truth.json"
    gt.write_text(json.dumps(ground_truth(), indent=2, ensure_ascii=False), encoding="utf-8")
    paths["ground_truth"] = gt
    return paths


if __name__ == "__main__":
    for k, v in generate(sys.argv[1] if len(sys.argv) > 1 else None).items():
        print(k, v)
