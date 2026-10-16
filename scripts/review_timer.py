"""Stopwatch for the review-effort experiment (see docs/review_effort_protocol.md).

Usage: python scripts/review_timer.py start|stop <manual|tool>
       python scripts/review_timer.py note <text> [--phase P]
       python scripts/review_timer.py found <phase> <n> [--ids M1,C3,...]
       python scripts/review_timer.py status | reset --yes
State file (default eval/review_effort.json, override with REVIEW_EFFORT_FILE):
  {"phases": {"manual": {"intervals": [{"start": iso, "stop": iso|null}], "found": int|null, "found_ids": [str]},
              "tool": {...same...}},
   "notes": [{"ts": iso, "phase": str|null, "text": str}]}
A phase's minutes are the sum of its closed intervals (start/stop may be repeated for breaks).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PHASES = ("manual", "tool")
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "eval" / "review_effort.json"


def _path() -> Path:
    return Path(os.environ.get("REVIEW_EFFORT_FILE") or DEFAULT_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(path: Path | None = None) -> dict:
    p = path or _path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return fresh()


def fresh() -> dict:
    return {"phases": {n: {"intervals": [], "found": None, "found_ids": []} for n in PHASES}, "notes": []}


def save(d: dict, path: Path | None = None) -> None:
    p = path or _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def _phase(d: dict, name: str) -> dict:
    if name not in PHASES:
        raise SystemExit(f"phase must be one of {PHASES}")
    return d["phases"].setdefault(name, {"intervals": [], "found": None, "found_ids": []})


def minutes(ph: dict) -> float:
    """Total minutes over closed intervals."""
    tot = 0.0
    for iv in ph.get("intervals", []):
        if iv.get("stop"):
            tot += (datetime.fromisoformat(iv["stop"]) - datetime.fromisoformat(iv["start"])).total_seconds() / 60
    return round(tot, 2)


def start(d: dict, name: str, now: str | None = None) -> None:
    ph = _phase(d, name)
    if ph["intervals"] and not ph["intervals"][-1].get("stop"):
        raise SystemExit(f"{name} is already running")
    ph["intervals"].append({"start": now or _now(), "stop": None})


def stop(d: dict, name: str, now: str | None = None) -> None:
    ph = _phase(d, name)
    if not ph["intervals"] or ph["intervals"][-1].get("stop"):
        raise SystemExit(f"{name} is not running")
    ph["intervals"][-1]["stop"] = now or _now()


def status_lines(d: dict) -> list[str]:
    out = []
    for n, ph in d["phases"].items():
        running = bool(ph["intervals"]) and not ph["intervals"][-1].get("stop")
        out.append(f"{n}: {minutes(ph)} min closed{' (RUNNING)' if running else ''}, found={ph.get('found')}"
                   f"{' ' + ','.join(ph['found_ids']) if ph.get('found_ids') else ''}")
    out.append(f"notes: {len(d['notes'])}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("start", "stop"):
        sub.add_parser(c).add_argument("phase")
    n = sub.add_parser("note")
    n.add_argument("text", nargs="+")
    n.add_argument("--phase", default=None)
    f = sub.add_parser("found")
    f.add_argument("phase")
    f.add_argument("n", type=int)
    f.add_argument("--ids", default="")
    sub.add_parser("status")
    r = sub.add_parser("reset")
    r.add_argument("--yes", action="store_true")
    a = ap.parse_args(argv)
    d = load()
    if a.cmd == "start":
        start(d, a.phase)
    elif a.cmd == "stop":
        stop(d, a.phase)
    elif a.cmd == "note":
        d["notes"].append({"ts": _now(), "phase": a.phase, "text": " ".join(a.text)})
    elif a.cmd == "found":
        ph = _phase(d, a.phase)
        ph["found"] = a.n
        ph["found_ids"] = [x.strip() for x in a.ids.split(",") if x.strip()]
    elif a.cmd == "reset":
        if not a.yes:
            raise SystemExit("reset deletes all timings; pass --yes")
        d = fresh()
    if a.cmd != "status":
        save(d)
    print("\n".join(status_lines(d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
