"""Generate the HELD-OUT synthetic HLD v4 ("Door Control / Central Locking ECU") and ground_truth_v4.json.

Usage: python data/synthetic/generate_heldout_v4.py [out_dir]
Written without looking at any parser code or extraction output. Mixed layout: a two-column page, a table that
splits across pages, ports in tables and in varied prose. Planted defects (6): M1, M3, M4 (interfaces), C2, C3, C4.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (BaseDocTemplate, Frame, FrameBreak, NextPageTemplate, PageBreak, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

DOC_NAME = "Door Control Unit - Central Locking Design Specification"
DOC_ID_LINE = "Doc-No. 4410: SYN_DoorCtrl_DS"
VERSION = "4.0"
KIND_LABEL = {"sender_receiver": "Sender/Receiver", "client_server": "Client/Server"}

COMPONENTS = [  # name, description, category
    ("DoorLockCtrl", "Decides the lock state of all doors from fob, crash and child lock inputs", "Application"),
    ("DoorPosSensor", "Reports whether the door is open or closed from a hall sensor", "Sensor"),
    ("LockActuatorDrv", "Drives the lock motor H-bridge and reads back the latch position", "Actuator"),
    ("KeyFobReceiver", "Decodes radio frames of the key fob into lock requests", "Sensor"),
    ("WindowInhibitMgr", "Blocks window movement while the doors are being locked", "Application"),
    ("CrashUnlockMon", "Watches the impact signal and demands automatic unlocking after a crash", "Service"),
    ("ChildLockHandler", "Stores and provides the rear door child lock setting", "Service"),
    ("DoorAjarDetector", "", "Sensor"),  # C4: no description
]
INTERFACES = [  # IAjarStatus deliberately NOT defined -> C3
    ("ILockRequest", "sender_receiver", ["LockCommand"]),
    ("ILockState", "sender_receiver", ["LockStatus"]),
    ("IDoorPos", "sender_receiver", ["DoorPosition"]),
    ("IFobCommand", "sender_receiver", ["FobRequest"]),
    ("IImpactEvent", "sender_receiver", ["ImpactSeverity"]),
    ("IChildLock", "client_server", ["SetChildLock", "GetChildLock"]),
    ("IChildLocks", "client_server", ["ReadChildLockMask"]),  # M4: near duplicate of IChildLock
]
# component, port, direction, interface, where described
PORTS = [
    ("DoorLockCtrl", "RpFobCmd", "required", "IFobCommand", "table"),
    ("DoorLockCtrl", "PpLockReq", "provided", "ILockRequest", "table"),
    ("DoorLockCtrl", "RpDoorPos", "required", "IDoorPos", "table"),
    ("DoorLockCtrl", "RpImpact", "required", "IImpactEvent", "prose"),
    ("DoorLockCtrl", "RpChildLock", "required", "IChildLock", "prose"),
    ("DoorPosSensor", "PpDoorPos", "provided", "IDoorPos", "table"),
    ("LockActuatorDrv", "RpLockReq", "required", "ILockRequest", "table"),
    ("LockActuatorDrv", "PpLockState", "provided", "ILockState", "table"),
    ("LockActuatorDrv", "PpDriverDiag", "provided", "", "table"),  # C2
    ("KeyFobReceiver", "PpFobCmd", "provided", "IFobCommand", "prose"),
    ("CrashUnlockMon", "PpImpact", "provided", "IImpactEvent", "prose"),
    ("ChildLockHandler", "PpChildLock", "provided", "IChildLock", "prose"),
    ("WindowInhibitMgr", "RpLockState", "required", "ILockState", "prose"),
    ("WindowInhibitMgr", "RpChildLocks", "required", "IChildLocks", "prose"),  # M1: nobody provides IChildLocks
    ("DoorAjarDetector", "PpAjar", "provided", "IAjarStatus", "table"),  # C3
    ("DoorAjarDetector", "RpDoorPos", "required", "IDoorPos", "table"),
]
SIGNALS = [
    ("DoorPosition", "DoorPosSensor", ["DoorLockCtrl", "DoorAjarDetector"], "boolean"),
    ("LockCommand", "DoorLockCtrl", ["LockActuatorDrv"], "uint8"),
    ("LockStatus", "LockActuatorDrv", ["WindowInhibitMgr"], "uint8"),
    ("FobRequest", "KeyFobReceiver", ["DoorLockCtrl"], "uint8"),
    ("ChildLockActive", "ChildLockHandler", ["DoorLockCtrl"], "boolean"),
    ("ImpactSeverity", "CrashUnlockMon", [], "uint8"),  # M3
]
PLANTED = [
    ("M1", ["WindowInhibitMgr.RpChildLocks"]),
    ("M3", ["ImpactSeverity"]),
    ("M4", ["IChildLock", "IChildLocks"]),
    ("C2", ["LockActuatorDrv.PpDriverDiag"]),
    ("C3", ["IAjarStatus"]),
    ("C4", ["DoorAjarDetector"]),
]
REVISIONS = [["07.10.2026", "4.0", "C. Author", "Initial release of the door control design specification"]]


def ground_truth() -> dict:
    kind = {n: k for n, k, _ in INTERFACES}
    return {
        "project_id": "synthetic-ecu",
        "documents": {VERSION: {"doc_id": "syn_hld_v4_heldout", "file": "hld_v4_heldout.pdf"}},
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
    return {
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14, spaceAfter=6),
        "col": ParagraphStyle("col", fontName="Helvetica", fontSize=9.5, leading=13, spaceAfter=7),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=9, leading=11),
        "hcell": ParagraphStyle("hcell", fontName="Helvetica-Bold", fontSize=9, leading=11),
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, spaceBefore=6, spaceAfter=8),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=10, spaceAfter=6),
        "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=12, leading=15, spaceBefore=8, spaceAfter=4),
        "cover": ParagraphStyle("cover", fontName="Helvetica", fontSize=20, leading=26, spaceAfter=10),
    }


def _table(st: dict, header: list[str], rows: list[list[str]], widths: list[float]) -> Table:
    data = [[Paragraph(h, st["hcell"]) for h in header]]
    data += [[Paragraph(c.replace("&", "&amp;"), st["cell"]) for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e4d8")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _port_sentence(c: str, p: str, d: str, i: str, idx: int) -> str:
    if d == "provided":
        forms = [f"{c} provides {p} via {i}.", f"The provided port {p} of {c} is typed by the interface {i}."]
    else:
        forms = [f"{c} receives data through the {p} port typed by {i}.", f"The required port {p} of {c} uses {i}."]
    return forms[idx % 2]


def _story() -> list:
    st = _styles()
    P = lambda t, s="body": Paragraph(t, st[s])  # noqa: E731
    story: list = [Spacer(1, 40), P(DOC_NAME, "cover"), P(f"Version {VERSION}"),
                   P("Status: Synthetic held-out evaluation document (fictional ECU)"), Spacer(1, 24)]
    hist = [[P("Document Change History", "hcell"), "", "", ""],
            [P(x, "hcell") for x in ("Date", "Version", "Author", "Description of Change")]]
    hist += [[P(c, "cell") for c in r] for r in REVISIONS]
    t = Table(hist, colWidths=[70, 55, 80, 290])
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.6, colors.black), ("SPAN", (0, 0), (-1, 0))]))
    story += [t, PageBreak()]

    story += [P("1 Introduction", "h1"),
              P("This specification describes the door control software of a fictional body ECU. Software components "
                "are explained in a two-column section, ports appear in tables and in running text."),
              P("1.1 Use Cases", "h2"),
              P("A key fob press locks or unlocks all doors. After a crash the doors are unlocked automatically."),
              NextPageTemplate("two"), PageBreak()]

    story.append(P("2 Software Components", "h1"))
    for k, (n, d, c) in enumerate(COMPONENTS):
        if k == 4:
            story.append(FrameBreak())
        story.append(P(f"<b>{n}</b> ({c}). {d}." if d else f"<b>{n}</b> ({c}).", "col"))
    story += [NextPageTemplate("one"), PageBreak()]

    port_rows = [[c, p, "Provider" if d == "provided" else "Requirer", i] for c, p, d, i, w in PORTS if w == "table"]
    story += [P("3 Port Descriptions", "h1"),
              P("3.1 Tabulated Ports", "h2"),
              P("The table below lists the ports of the locking chain."),
              Spacer(1, 455),  # forces the table to break across the page boundary
              _table(st, ["SW-C", "Port Prototype", "Role", "Interface Ref"], port_rows, [120, 120, 70, 150]),
              PageBreak(),
              P("3.2 Ports described in Text", "h2")]
    for idx, (c, p, d, i, w) in enumerate(x for x in PORTS if x[4] == "prose"):
        story.append(P(_port_sentence(c, p, d, i, idx)))
    story.append(PageBreak())

    story += [P("4 Port Interfaces", "h1"),
              _table(st, ["Port Interface", "Communication Pattern", "Data Elements / Operations"],
                     [[n, KIND_LABEL[k], ", ".join(e)] for n, k, e in INTERFACES], [110, 120, 245]),
              PageBreak(),
              P("5 Signal Overview", "h1"),
              _table(st, ["Data Element", "Provider", "Requirer(s)", "Data Type"],
                     [[n, s, ", ".join(r), t] for n, s, r, t in SIGNALS], [110, 120, 165, 80]),
              PageBreak(),
              P("6 Design Constraints", "h1"),
              P("Lock motor current is limited to 8 A for at most 400 ms.")]
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
        kw = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        one = Frame(50, 110, W - 100, H - 220, id="f", **kw)
        cw = (W - 100 - 20) / 2
        left = Frame(50, 110, cw, H - 220, id="l", **kw)
        right = Frame(50 + cw + 20, 110, cw, H - 220, id="r", **kw)
        doc.addPageTemplates([PageTemplate(id="one", frames=[one], onPage=chrome),
                              PageTemplate(id="two", frames=[left, right], onPage=chrome)])
        doc.build(_story())
        total[0] = state["page"]
    return path


def generate(out_dir: Path | str | None = None) -> dict[str, Path]:
    out = Path(out_dir) if out_dir else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    pdf = render_pdf(out / "hld_v4_heldout.pdf")
    gt = out / "ground_truth_v4.json"
    gt.write_text(json.dumps(ground_truth(), indent=2, ensure_ascii=False), encoding="utf-8")
    return {"pdf": pdf, "ground_truth": gt}


if __name__ == "__main__":
    for k, v in generate(sys.argv[1] if len(sys.argv) > 1 else None).items():
        print(k, v)
