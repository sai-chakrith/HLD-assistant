"""Shared table-header vocabulary: fuzzy header -> canonical field mapping and value-pattern column-role inference.

Used by app.ingest (is this row a plausible header?) and app.extract (which column holds what?).
Order of attempts in ``match_header``: exact alias -> head-noun rules (the last significant word of a compound
header decides, so 'Owner SWC' / 'Port Interface' / 'Communication Pattern' resolve) -> rapidfuzz on single words.

Public API: hn(s), HEADER_ALIASES, match_header(cell), is_header_row(row), infer_roles(rows)
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz, process

FUZZY_WORD = 86  # rapidfuzz ratio for a header word vs a head-noun vocabulary word (len >= 5 only)


def hn(s: str) -> str:
    """Normalise a header cell: lowercase, collapse whitespace, keep letters/digits and '/'."""
    s = re.sub(r"\s+", " ", (s or "").replace("\n", " ")).strip().lower()
    return re.sub(r"[^a-z0-9/]+", " ", s).strip()


HEADER_ALIASES: dict[str, list[str]] = {
    "component": ["component", "components", "software component", "swc", "sw-c", "composition",
                  "component name", "swc name", "owner swc", "owner", "sw component", "component prototype",
                  "owning component", "software component name"],
    "description": ["description", "purpose", "comments", "comment", "responsibility", "function", "remarks",
                    "notes", "details"],
    "category": ["category", "layer", "group", "component type", "class", "swc type"],
    "port": ["port", "ports", "port name", "port prototype", "port prototype name", "port id", "pport/rport"],
    "direction": ["direction", "provided/required", "port type", "p-port/r-port", "port direction", "p/r", "r/p",
                  "role", "port role", "provided required", "p r", "pr", "port kind", "p/r port"],
    "interface": ["interface", "interface name", "port interface", "interface ref", "interface reference",
                  "port interface ref", "provided interface", "required interface", "interface prototype"],
    "kind": ["kind", "interface kind", "interface type", "sender-receiver/client-server", "pattern",
             "communication pattern", "interface pattern", "communication kind", "s/r c/s", "sr/cs", "paradigm"],
    "data_elements": ["data element", "data elements", "operation", "operations", "data element / operation",
                      "elements", "element", "data element prototype", "signals/operations"],
    "signal": ["signal", "signal name", "signals"],
    "sender": ["sender", "source", "from", "producer", "provider", "transmitter", "writer", "sending component",
               "source component", "sender swc"],
    "receivers": ["receiver", "receivers", "destination", "to", "consumer", "consumers", "requirer", "requirers",
                  "reader", "readers", "receiving component", "destination component", "receiver swc"],
    "data_type": ["data type", "type", "datatype", "base type", "implementation type"],
}
_EXACT = {hn(a): canon for canon, al in HEADER_ALIASES.items() for a in al}
_GENERIC = {"name", "ref", "reference", "id", "list", "of", "the", "s", "prototype", "prototypes", "no", "nr"}
# head noun (last significant word) -> canonical field
_HEAD = {
    "swc": "component", "swcs": "component", "component": "component", "components": "component",
    "composition": "component", "owner": "component",
    "port": "port", "ports": "port",
    "interface": "interface", "interfaces": "interface",
    "direction": "direction", "role": "direction", "provided": "direction", "required": "direction",
    "pattern": "kind", "kind": "kind", "paradigm": "kind",
    "element": "data_elements", "elements": "data_elements", "operation": "data_elements",
    "operations": "data_elements",
    "signal": "signal", "signals": "signal",
    "producer": "sender", "sender": "sender", "source": "sender", "provider": "sender", "transmitter": "sender",
    "writer": "sender", "from": "sender",
    "consumer": "receivers", "consumers": "receivers", "receiver": "receivers", "receivers": "receivers",
    "destination": "receivers", "requirer": "receivers", "requirers": "receivers", "reader": "receivers",
    "readers": "receivers", "to": "receivers",
    "description": "description", "purpose": "description", "comment": "description", "comments": "description",
    "responsibility": "description", "remarks": "description", "notes": "description",
    "category": "category", "layer": "category", "group": "category", "class": "category",
    "type": "data_type", "datatype": "data_type",
}
_HEAD_FUZZ = [w for w in _HEAD if len(w) >= 5]
_PR = re.compile(r"^(p ?/ ?r|r ?/ ?p|p r|pr)$")


def _head_word(tokens: list[str]) -> str:
    sig = [t for t in tokens if t not in _GENERIC]
    return sig[-1] if sig else ""


def match_header(cell: str) -> str | None:
    """Canonical field for a header cell (fuzzy), or None when the cell is not a recognisable header word."""
    h = hn(cell)
    if not h or len(h) > 40:
        return None
    if h in _EXACT:
        return _EXACT[h]
    if _PR.match(h):
        return "direction"
    toks = re.findall(r"[a-z]+", h)
    if not toks or len(toks) > 5:
        return None
    # specials before the head-noun rule: '<interface|port|signal> kind/type/pattern' is a kind or type column
    if "interface" in toks and set(toks) & {"kind", "type", "pattern", "paradigm"}:
        return "kind"
    if "port" in toks and set(toks) & {"direction", "role", "type", "kind"}:
        return "direction"
    hd = _head_word(toks)
    if hd in _HEAD:
        return _HEAD[hd]
    if len(hd) >= 5 and not hd.isdigit():
        best = process.extractOne(hd, _HEAD_FUZZ, scorer=fuzz.ratio, score_cutoff=FUZZY_WORD)
        if best:
            return _HEAD[best[0]]
    return None


def is_header_row(row: list[str]) -> bool:
    """True when every cell of the row is a recognisable header word (data rows never are)."""
    return len(row) >= 2 and all(match_header(c) for c in row)


# ---------------------------------------------------------------- column-role inference from values

_DIR = {"p", "r", "pport", "rport", "provided", "required", "provider", "requirer", "provide", "require"}
_KIND = {"s/r", "c/s", "sr", "cs", "senderreceiver", "clientserver", "sender/receiver", "client/server"}
_IFACE = re.compile(r"^(I[A-Z][A-Za-z0-9_]*|[A-Za-z][A-Za-z0-9_]*(If|Interface|_I))$")
_PORT = re.compile(r"^(Pp|Rp|PP|RP|Pport|Rport)_?[A-Z][A-Za-z0-9_]*$")
_CAMEL = re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+$")
_DTYPE = re.compile(r"^(u?int(8|16|32|64)(_t)?|sint(8|16|32|64)|float(32|64)?|double|boolean|bool|char)$", re.I)
_MIN_ROWS = 2
_FRAC = 0.8


def _col_fraction(vals: list[str], pred) -> float:
    vals = [v for v in vals if v]
    return sum(1 for v in vals if pred(v)) / len(vals) if len(vals) >= _MIN_ROWS else 0.0


def infer_roles(rows: list[list[str]]) -> dict[str, int]:
    """Infer {canonical field: column index} from cell VALUE patterns alone (conservative: >= 80% of >= 2
    non-empty cells must match). direction: P/R/provided/required; kind: S/R, C/S, sender-receiver; interface:
    I<Camel> or *If/*Interface; port: Pp/Rp<Camel>; data_type: uint8/float32...; component: a leftover CamelCase
    column, only when a port, interface or direction column was found."""
    if not rows:
        return {}
    width = max(len(r) for r in rows)
    cols = [[(r[i] if i < len(r) else "").strip() for r in rows] for i in range(width)]
    out: dict[str, int] = {}
    tests = [
        ("direction", lambda v: re.sub(r"[^a-z/]", "", v.lower()) in _DIR),
        ("kind", lambda v: re.sub(r"[^a-z/]", "", v.lower()) in _KIND),
        ("port", lambda v: bool(_PORT.match(v))),
        ("interface", lambda v: bool(_IFACE.match(v))),
        ("data_type", lambda v: bool(_DTYPE.match(v))),
    ]
    taken: set[int] = set()
    for role, pred in tests:
        for i, vals in enumerate(cols):
            if i not in taken and role not in out and _col_fraction(vals, pred) >= _FRAC:
                out[role] = i
                taken.add(i)
    if set(out) & {"port", "interface", "direction"}:
        for i, vals in enumerate(cols):
            if i not in taken and _col_fraction(vals, lambda v: bool(_CAMEL.match(v))) >= _FRAC:
                out["component"] = i  # leftmost leftover CamelCase column
                break
    return out
