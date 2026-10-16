"""Generate the HELD-OUT synthetic HLD v3 ("Seat Comfort ECU") and data/synthetic/ground_truth_v3.json.

Usage: python data/synthetic/generate_heldout.py [out_dir]

This generator is deliberately independent from generate_hld.py (v1/v2) and was written WITHOUT looking at the
parser's header aliases or at any extraction output. Style differences versus v1/v2:
  * header wording: "SW Component | Purpose", "Owner SWC | Port Name | P/R | Port Interface",
    "Interface Name | Pattern | Elements", "Data Element | Producer | Consumer(s) | Type"
  * kind wording "S/R" and "C/S" instead of Sender-Receiver / Client-Server
  * 5 of the 13 ports... (see PORTS) are described in prose paragraphs, not in a table
  * the port table is long and breaks across two pages
  * different component naming, section titles ("Software Building Blocks", ...), no "Functional Flow" title

Planted defects (6): M1, M3, M4, C2, C3, C4. (M2 and C1 are not planted; the v3 document has no flow section.)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph, Spacer, Table,
                                TableStyle)

DOC_NAME = "Seat Comfort ECU - Software Architecture Description"
DOC_ID_LINE = "Document ID 777: SYN_SeatComfort_SAD"
VERSION = "3.0"
KIND_LABEL = {"sender_receiver": "S/R", "client_server": "C/S"}

COMPONENTS = [  # name, purpose, category
    ("SeatHeatCtrl", "Regulates the seat heating level from the measured seat temperature", "Application"),
    ("SeatTempSensor", "Delivers the filtered seat surface temperature", "Sensor"),
    ("HeaterDriver", "Switches the heater mat with a PWM output stage", "Actuator"),
    ("OccupancyDetector", "", "Sensor"),  # C4: no purpose
    ("SeatVentCtrl", "Controls the ventilation fan speed of the seat", "Application"),
    ("SeatVentCtl", "Legacy ventilation helper kept for backward compatibility", "Application"),  # M4 near duplicate
    ("SeatMemoryMgr", "Stores and recalls the driver seat position presets", "Service"),
]
INTERFACES = [  # name, kind, elements   (ISeatBelt is deliberately NOT defined -> C3)
    ("ISeatTemp", "sender_receiver", ["SeatTemperature"]),
    ("IHeaterCmd", "sender_receiver", ["HeaterPwm"]),
    ("IOccupancy", "sender_receiver", ["OccupancyState"]),
    ("IVentLevel", "sender_receiver", ["VentLevel"]),
    ("IMemPos", "client_server", ["StorePosition", "RecallPosition"]),
    ("IHeatLevel", "sender_receiver", ["HeatLevel"]),
]
# component, port, direction, interface, where described: "table" or "prose"
PORTS = [
    ("SeatHeatCtrl", "RpSeatTemp", "required", "ISeatTemp", "table"),
    ("SeatHeatCtrl", "PpHeaterCmd", "provided", "IHeaterCmd", "table"),
    ("SeatHeatCtrl", "RpOccupancy", "required", "IOccupancy", "table"),
    ("SeatTempSensor", "PpSeatTemp", "provided", "ISeatTemp", "table"),
    ("HeaterDriver", "RpHeaterCmd", "required", "IHeaterCmd", "table"),
    ("HeaterDriver", "PpDriverStatus", "provided", "", "table"),  # C2: no interface
    ("OccupancyDetector", "PpOccupancy", "provided", "IOccupancy", "table"),
    ("OccupancyDetector", "RpSeatBelt", "required", "ISeatBelt", "table"),  # C3: interface never defined
    ("SeatVentCtrl", "RpOccupancy", "required", "IOccupancy", "prose"),
    ("SeatVentCtrl", "PpVentLevel", "provided", "IVentLevel", "prose"),
    ("SeatVentCtl", "RpVentLevel", "required", "IVentLevel", "prose"),
    ("SeatMemoryMgr", "PpMemPos", "provided", "IMemPos", "prose"),
    ("SeatMemoryMgr", "RpHeatLevel", "required", "IHeatLevel", "prose"),  # M1: nobody provides IHeatLevel
    ("SeatMemoryMgr", "PpSeatBelt", "provided", "ISeatBelt", "prose"),  # provider of ISeatBelt (keeps M1 clean)
]
SIGNALS = [  # name, producer, consumers, type
    ("SeatTemperature", "SeatTempSensor", ["SeatHeatCtrl"], "float32"),
    ("HeaterPwm", "SeatHeatCtrl", ["HeaterDriver"], "uint8"),
    ("OccupancyState", "OccupancyDetector", ["SeatHeatCtrl", "SeatVentCtrl"], "boolean"),
    ("VentLevel", "SeatVentCtrl", ["SeatVentCtl"], "uint8"),
    ("StoredPosition", "SeatMemoryMgr", [], "uint16"),  # M3: producer without consumers
]
PLANTED = [
    ("M1", ["SeatMemoryMgr.RpHeatLevel"]),
    ("M3", ["StoredPosition"]),
    ("M4", ["SeatVentCtrl", "SeatVentCtl"]),
    ("C2", ["HeaterDriver.PpDriverStatus"]),
    ("C3", ["ISeatBelt"]),
    ("C4", ["OccupancyDetector"]),
]
REVISIONS = [["20.05.2025", "3.0", "B. Writer", "First issue of the Seat Comfort ECU architecture description"]]


def ground_truth() -> dict:
    kind = {n: k for n, k, _ in INTERFACES}
    return {
        "project_id": "synthetic-ecu",
        "documents": {VERSION: {"doc_id": "syn_hld_v3_heldout", "file": "hld_v3_heldout.pdf"}},
        "entities": {VERSION: {
            "components": [{"name": n, "description": d, "category": c} for n, d, c in COMPONENTS],
            "ports": [{"component": c, "name": p, "direction": d, "interface": i, "kind": kind.get(i, "")}
                      for c, p, d, i, _ in PORTS],
            "interfaces": [{"name": n, "kind": k, "data_elements": list(e)} for n, k, e in INTERFACES],
            "signals": [{"name": n, "sender": s, "receivers": list(r), "data_type": t} for n, s, r, t in SIGNALS],
        }},
        "port_described_in": {f"{c}.{p}": w for c, p, _, _, w in PORTS},
        "planted_defects": [{"rule_id": r, "entities": e, "version": VERSION} for r, e in PLANTED],
        "remaining_defects": [{"rule_id": r, "entities": e, "version": VERSION} for r, e in PLANTED],
    }


def _styles() -> dict:
    b = dict(fontName="Helvetica", fontSize=10, leading=14)
    return {
        "body": ParagraphStyle("body", **b, spaceAfter=6),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=9, leading=11),
        "hcell": ParagraphStyle("hcell", fontName="Helvetica-Bold", fontSize=9, leading=11),
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, spaceBefore=6, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=10, spaceAfter=6),
        "cover": ParagraphStyle("cover", fontName="Helvetica", fontSize=20, leading=26, spaceAfter=10),
    }


def _table(st: dict, header: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(h, st["hcell"]) for h in header]]
    data += [[Paragraph(c.replace("&", "&amp;"), st["cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dde6f0")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _prose_for(comp: str) -> str:
    kind = {n: k for n, k, _ in INTERFACES}
    parts = []
    for c, p, d, i, w in PORTS:
        if c != comp or w != "prose":
            continue
        verb = "offers" if d == "provided" else "consumes data through"
        parts.append(f"it {verb} the port {p} via the interface {i}")
    return f"{comp}: " + "; and ".join(parts) + "."


def _story() -> list:
    st = _styles()
    P = lambda t, s="body": Paragraph(t, st[s])  # noqa: E731
    story: list = [Spacer(1, 40), P(DOC_NAME, "cover"), P(f"Version {VERSION}"),
                   P("Status: Synthetic held-out evaluation document (fictional ECU)"), Spacer(1, 24)]
    hist = [[P("Document Change History", "hcell"), "", "", ""],
            [P(x, "hcell") for x in ("Date", "Version", "Changed by", "Change Description")]]
    hist += [[P(c, "cell") for c in r] for r in REVISIONS]
    t = Table(hist, colWidths=[70, 55, 80, 290])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("SPAN", (0, 0), (-1, 0))]))
    story += [t, PageBreak()]

    story += [P("1 About this Document", "h1"),
              P("This description covers the seat heating and ventilation software of a fictional body "
                "controller. It is written in a free style: building blocks are listed in a table, ports are "
                "partly listed in a table and partly explained in running text."),
              P("1.1 Operating Scenarios", "h2"),
              P("When the occupant switches the heating on, the heating regulator compares the measured seat "
                "temperature with the requested level and adjusts the heater output."),
              PageBreak()]

    story += [P("2 Software Building Blocks", "h1"),
              P("The following blocks make up the seat comfort software."),
              _table(st, ["SW Component", "Purpose", "Category"],
                     [[n, d, c] for n, d, c in COMPONENTS], [120, 290, 65]),
              PageBreak()]

    story += [P("3 Port Catalogue", "h1"),
              P("3.1 Ports of the Heating Chain", "h2"),
              P("The table lists the ports of the heating and occupancy blocks. Direction is P for provided and "
                "R for required."),
              Spacer(1, 470),  # forces the table to break across the page boundary
              _table(st, ["Owner SWC", "Port Name", "P/R", "Port Interface"],
                     [[c, p, "P" if d == "provided" else "R", i] for c, p, d, i, w in PORTS if w == "table"],
                     [120, 130, 40, 150]),
              PageBreak()]

    story += [P("3.2 Ports of the Ventilation and Memory Chain", "h2"),
              P("These ports are described in text.")]
    for comp in ("SeatVentCtrl", "SeatVentCtl", "SeatMemoryMgr"):
        story.append(P(_prose_for(comp)))
    story.append(PageBreak())

    story += [P("4 Interface Catalogue", "h1"),
              _table(st, ["Interface Name", "Pattern", "Elements"],
                     [[n, KIND_LABEL[k], ", ".join(e)] for n, k, e in INTERFACES], [120, 70, 285]),
              PageBreak(),
              P("5 Data Exchange", "h1"),
              _table(st, ["Data Element", "Producer", "Consumer(s)", "Type"],
                     [[n, s, ", ".join(r), t] for n, s, r, t in SIGNALS], [110, 120, 165, 80]),
              PageBreak(),
              P("6 Constraints", "h1"),
              P("The heater output is switched off when the supply voltage leaves the 9 V to 16 V range.")]
    return story


def render_pdf(path: Path) -> Path:
    W, H = A4
    state = {"page": 0}
    total = [0]

    def chrome(c, doc):
        state["page"] += 1
        c.saveState()
        c.setFont("Helvetica", 9)
        c.drawString(50, H - 36, DOC_NAME)
        c.drawString(50, H - 48, f"Version {VERSION}")
        c.drawString(50, 44, f"{state['page']} of {total[0]}")
        c.drawString(50, 32, DOC_ID_LINE)
        c.restoreState()

    for _ in range(2):
        state["page"] = 0
        doc = BaseDocTemplate(str(path), pagesize=A4, title=DOC_NAME, author="Synthetic")
        frame = Frame(50, 110, W - 100, H - 220, id="f", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([PageTemplate(id="p", frames=[frame], onPage=chrome)])
        doc.build(_story())
        total[0] = state["page"]
    return path


def generate(out_dir: Path | str | None = None) -> dict[str, Path]:
    out = Path(out_dir) if out_dir else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    pdf = render_pdf(out / "hld_v3_heldout.pdf")
    gt = out / "ground_truth_v3.json"
    gt.write_text(json.dumps(ground_truth(), indent=2, ensure_ascii=False), encoding="utf-8")
    return {"pdf": pdf, "ground_truth": gt}


if __name__ == "__main__":
    for k, v in generate(sys.argv[1] if len(sys.argv) > 1 else None).items():
        print(k, v)
