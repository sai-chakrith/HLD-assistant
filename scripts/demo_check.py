"""Walk the 11 demo scenarios from docs/outcomes.md against a RUNNING API, exactly as the UI would (HTTP only,
apart from setup/cleanup of the scratch project). Prints a JSON evidence log.

    PYTHONPATH=. .venv/Scripts/python scripts/demo_check.py [--keep]

Uploads go to a scratch project ``demo-check`` (created here, removed at the end) so real projects keep their
review decisions. Needs uvicorn on API_BASE_URL (default http://127.0.0.1:8000) and Ollama.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
ROOT = Path(__file__).resolve().parent.parent
SC = "demo-check"
MODEL = os.environ.get("DEMO_MODEL", "qwen2.5:3b")
results: dict[str, dict] = {}


def call(method, path, user="admin", **kw):
    return requests.request(method, BASE + path, headers={"X-User": user}, timeout=600, **kw)


def rec(n: int, title: str, ok: bool, evidence):
    results[str(n)] = {"title": title, "pass": bool(ok), "evidence": evidence}
    print(f"[{n}] {'PASS' if ok else 'FAIL'} {title}", flush=True)


def upload(path: Path, status: str, version: str, user="admin", pid=SC):
    with open(path, "rb") as f:
        return call("POST", f"/projects/{pid}/upload", user=user, files={"file": (path.name, f, "application/pdf")},
                    data={"doc_type": "hld", "version": version, "source_status": status, "model": MODEL})


def setup():
    from app import controls
    c = controls.get_conn()
    controls.create_project(SC, conn=c)
    for u, r in (("admin", "admin"), ("reviewer", "reviewer"), ("viewer", "viewer")):
        controls.add_member(SC, u, role=r, conn=c)
    c.close()


def cleanup():
    from app import controls, retrieve
    c = controls.get_conn()
    for t in ("components", "ports", "interfaces", "signals", "findings", "reports", "answers", "documents",
              "reviews", "project_members"):
        try:
            c.execute(f"DELETE FROM {t} WHERE project_id=?", (SC,))
        except Exception:
            pass
    c.execute("DELETE FROM projects WHERE project_id=?", (SC,))
    c.commit()
    c.close()
    try:
        retrieve._client(None).delete_collection(retrieve.collection_name(SC))
    except Exception:
        pass
    import shutil
    from app.config import settings
    shutil.rmtree(settings.processed_dir / SC, ignore_errors=True)


def main(keep: bool):
    sys.path.insert(0, str(ROOT))
    setup()
    raw, syn = ROOT / "data" / "raw", ROOT / "data" / "synthetic"
    try:
        # 1 unapproved upload rejected and logged
        r = upload(raw / "bodycomfort_r3.1.5.pdf", "draft", "R3.1.5")
        audit = call("GET", "/audit").json()
        logged = [a for a in audit if "reject" in str(a.get("action", "")).lower() or "draft" in str(a.get("details", "")).lower()]
        docs = call("GET", f"/projects/{SC}/documents").json()
        rec(1, "Upload without approved/synthetic status is rejected and logged",
            r.status_code == 400 and not docs and bool(logged),
            {"status": r.status_code, "detail": r.json().get("detail"), "docs_after": len(docs),
             "audit_rows": [{k: a[k] for k in ("action", "target", "user") if k in a} for a in logged[:2]]})

        # 2 approved upload then ask about a SWC's ports
        t0 = time.time()
        r = upload(raw / "bodycomfort_r3.1.5.pdf", "approved", "R3.1.5")
        up = r.json() if r.status_code == 200 else r.text
        a = call("POST", f"/projects/{SC}/ask", json={"question": "Which ports does the FrontWasher component have?",
                                                      "model": MODEL}).json()
        rec(2, "Approved upload, cited answer about a SWC's ports with confidence badge",
            r.status_code == 200 and a["mode"] != "refused" and bool(a["citations"]) and a["confidence"] in ("High", "Medium", "Low"),
            {"upload_s": round(time.time() - t0, 1), "extracted": up.get("extracted") if isinstance(up, dict) else up,
             "answer": a["answer"][:400], "confidence": a["confidence"], "mode": a["mode"], "model": a.get("model"),
             "citations": [(c["doc_id"], c["version"], c["page_start"], c["section_id"]) for c in a["citations"]]})

        # 3 unanswerable
        a3 = call("POST", f"/projects/{SC}/ask", json={"question": "What is the maximum torque of the diesel engine?",
                                                       "model": MODEL}).json()
        rec(3, "Question not in the document is refused with a limitation note",
            a3["mode"] == "refused" and bool(a3["limitation"]),
            {"mode": a3["mode"], "confidence": a3["confidence"], "answer": a3["answer"][:200], "limitation": a3["limitation"][:200]})

        # 4 synthetic v1 upload -> inventory + graph
        r = upload(syn / "hld_v1.pdf", "synthetic", "1.0")
        r2 = upload(syn / "hld_v2.pdf", "synthetic", "2.0")
        inv = call("GET", f"/projects/{SC}/inventory", params={"doc_id": "hld_v1"}).json()
        g = call("GET", f"/projects/{SC}/graph", params={"doc_id": "hld_v1"}).json()
        comp = g["components"][0]
        gs = call("GET", f"/projects/{SC}/graph", params={"doc_id": "hld_v1", "component": comp, "depth": 1}).json()
        ev = next((e for e in gs["evidence"] if e["page"]), None)
        png = call("GET", f"/projects/{SC}/documents/hld_v1/pages/{ev['page']}.png") if ev else None
        rec(4, "Synthetic v1 upload: inventory and dependency graph (+ component filter, evidence page)",
            r.status_code == 200 and r2.status_code == 200 and g["nodes"] > 0 and bool(png) and png.content[:4] == b"\x89PNG",
            {"inventory_counts": {k: len(v) for k, v in inv.items()}, "graph": [g["nodes"], g["edges"]],
             "filtered": {"component": comp, "nodes": gs["nodes"], "edges": gs["edges"]},
             "evidence_page_png_bytes": len(png.content) if png else 0, "html_has_vis": "vis-network" in g["html"] or "vis.js" in g["html"]})

        # 5 component report + flow summary, every line cited
        comps = [c for c in inv["components"] if c.get("extraction_method") == "table"]
        cname = "WinCtrlManager" if any(c["name"] == "WinCtrlManager" for c in comps) else comps[0]["name"]
        t0 = time.time()
        rep = call("GET", f"/projects/{SC}/reports/component", params={"doc_id": "hld_v1", "component": cname, "model": MODEL}).json()
        flow = call("GET", f"/projects/{SC}/reports/flow", params={"doc_id": "hld_v1"}).json()
        facts = [x for k in ("ports", "interfaces", "upstream", "downstream", "signals_sent", "signals_received") for x in rep[k]]
        steps = [s for f in flow["flows"] for s in f["steps"]]
        ok = all(x.get("citation") for x in facts) and all(s.get("citation") for s in steps) and bool(steps)
        ok = ok and (not rep["purpose"].strip() or bool(rep.get("purpose_citations")))
        rec(5, "Component report and functional-flow summary: every line cited", ok,
            {"component": cname, "report_s": round(time.time() - t0, 1), "model": rep.get("model"), "purpose": rep["purpose"][:300],
             "purpose_citations": len(rep.get("purpose_citations") or []), "fact_rows": len(facts),
             "uncited_fact_rows": sum(1 for x in facts if not x.get("citation")), "flow_steps": len(steps),
             "uncited_steps": sum(1 for s in steps if not s.get("citation")), "flow_source": flow.get("source")})

        # 6 checks find planted defects with page evidence
        r = call("POST", f"/projects/{SC}/run-checks", json={"doc_id": "hld_v1"})
        fl = call("GET", f"/projects/{SC}/findings", params={"doc_id": "hld_v1"}).json()
        gt = json.loads((syn / "ground_truth.json").read_text(encoding="utf-8"))
        planted = [d for d in gt["planted_defects"] if d["version"] == "1.0"]
        found = 0
        for d in planted:
            if any(f["rule_id"] == d["rule_id"] and set(map(str.lower, d["entities"])) <= set(map(str.lower, f["entities"])) for f in fl):
                found += 1
        with_pages = sum(1 for f in fl if f["evidence"] and all(e.get("page") for e in f["evidence"]))
        rec(6, "Mismatch and completeness checks find planted defects, each with page evidence",
            found >= 6 and with_pages == len(fl),
            {"planted": len(planted), "found": found, "total_findings": len(fl), "false_positive_est": len(fl) - found,
             "findings_with_page_evidence": with_pages, "by_rule": sorted({f["rule_id"] for f in fl})})

        # 7 roles: viewer cannot approve; reviewer approves one and rejects one; audit log shows it
        f0, f1 = fl[0], fl[1]
        body = lambda f, d: {"item_type": "finding", "item_id": f["id"], "decision": d, "comment": "demo"}
        rv = call("POST", f"/projects/{SC}/review", user="viewer", json=body(f0, "approved"))
        ra = call("POST", f"/projects/{SC}/review", user="reviewer", json=body(f0, "approved"))
        rr = call("POST", f"/projects/{SC}/review", user="reviewer", json=body(f1, "rejected"))
        audit = call("GET", "/audit", params={"limit": 50}).json()
        acts = [a["action"] for a in audit]
        after = {f["id"]: f["review_status"] for f in call("GET", f"/projects/{SC}/findings", params={"doc_id": "hld_v1"}).json()}
        rec(7, "Viewer cannot approve; reviewer approves one and rejects one; audit log shows it",
            rv.status_code == 403 and ra.status_code == 200 and rr.status_code == 200 and after[f0["id"]] == "approved"
            and after[f1["id"]] == "rejected" and any("review" in x or "approve" in x for x in acts),
            {"viewer_status": rv.status_code, "viewer_detail": rv.json().get("detail"), "reviewer": [ra.status_code, rr.status_code],
             "statuses": [after[f0["id"]], after[f1["id"]]], "recent_audit_actions": acts[:8],
             "viewer_audit_endpoint": call("GET", "/audit", user="viewer").status_code})

        # 8 compare v1 v2
        d = call("POST", f"/projects/{SC}/compare", json={"doc_a": "hld_v1", "doc_b": "hld_v2"}).json()
        exp = gt["expected_diff"]
        want = {"components": (len(exp["components"]["added"]), len(exp["components"]["removed"]), len(exp["components"]["changed"])),
                "ports": (len(exp["ports"]["added"]), len(exp["ports"]["removed"]), len(exp["ports"]["changed"]))}
        got = {k: (len(d[k]["added"]), len(d[k]["removed"]), len(d[k]["changed"])) for k in want}
        ex = call("GET", f"/projects/{SC}/export/diff", params={"doc_a": "hld_v1", "doc_b": "hld_v2", "fmt": "xlsx"})
        rec(8, "Compare v1 vs v2: added/removed/changed entities (+ change history, export)",
            got == want and ex.status_code == 200 and len(ex.content) > 1000,
            {"summary": d["summary"], "expected_vs_got": {"expected": want, "got": got},
             "revisions": [len(d.get("revisions_a") or []), len(d.get("revisions_b") or [])], "export_bytes": len(ex.content)})

        # 9 project switch isolation
        a9 = call("POST", "/projects/chassis/ask", json={"question": "Which ports does the FrontWasher component have?", "model": MODEL}).json()
        docs_c = [x["doc_id"] for x in call("GET", "/projects/chassis/documents").json()]
        cross = call("GET", f"/projects/chassis/documents/hld_v1/pages/1.png")
        nonmember = call("GET", f"/projects/{SC}/documents", user="nobody")
        cited_docs = {c["doc_id"] for c in a9["citations"]}
        rec(9, "Switching project: the other project's documents are not retrievable",
            "bodycomfort_r3.1.5" not in cited_docs and not (cited_docs & {"hld_v1"}) and cross.status_code == 404,
            {"chassis_docs": docs_c, "chassis_answer_mode": a9["mode"], "cited_docs": sorted(cited_docs),
             "chassis_ask_for_bodycomfort_doc_page_status": cross.status_code, "unknown_user_status": nonmember.status_code,
             "answer": a9["answer"][:200]})

        # 10 offline: no network except loopback
        rec(10, "Same query works with the network blocked (see offline subprocess)", *offline_check())

        # 11 export findings to Excel
        ex = call("GET", f"/projects/{SC}/export", params={"kind": "findings", "fmt": "xlsx", "doc_id": "hld_v1"})
        info = {"status": ex.status_code, "bytes": len(ex.content)}
        ok = False
        if ex.status_code == 200:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(ex.content))
            ws = wb.active
            allrows = list(ws.iter_rows(values_only=True))
            hi = next(i for i, r in enumerate(allrows) if any(str(x).lower() == "rule_id" for x in r if x))  # skip banner row
            hdr = [str(x) for x in allrows[hi]]
            rows = allrows[hi + 1:]
            info["banner"] = str(allrows[0][0])
            info.update(header=hdr, rows=len(rows))
            ok = any("review" in h.lower() for h in hdr) and any("evidence" in h.lower() or "citation" in h.lower() or "page" in h.lower() for h in hdr)
            col = next(i for i, h in enumerate(hdr) if "review" in h.lower())
            info["review_statuses"] = sorted({str(r[col]) for r in rows})
        rec(11, "Export findings to Excel: rows show review status and citations", ok, info)
    finally:
        if not keep:
            cleanup()
    out = ROOT / "data" / "demo_check.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"wrote {out}; passed {sum(v['pass'] for v in results.values())}/{len(results)}")


def offline_check():
    """Run a question in-process with every non-loopback socket connection refused and HF offline flags set."""
    import subprocess
    code = r'''
import os, socket, json, sys
os.environ["HF_HUB_OFFLINE"]="1"; os.environ["TRANSFORMERS_OFFLINE"]="1"
real = socket.socket.connect
def guard(self, addr, *a, **k):
    host = addr[0] if isinstance(addr, tuple) else str(addr)
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise OSError("network blocked: " + str(host))
    return real(self, addr, *a, **k)
socket.socket.connect = guard
from app import qa, llm
a = qa.ask("body-comfort", "Which ports does the FrontWasher component have?", "admin", model="qwen2.5:3b", provider=llm.get_provider())
print(json.dumps({"mode": a.mode, "confidence": a.confidence, "n_cit": len(a.citations), "answer": a.answer[:200]}))
'''
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT, timeout=600)
    line = next((x for x in p.stdout.splitlines() if x.startswith("{")), "{}")
    d = json.loads(line) if line != "{}" else {}
    return (d.get("mode") in ("generated", "evidence_only") and d.get("n_cit", 0) > 0), {"result": d, "rc": p.returncode, "stderr_tail": p.stderr[-300:]}


if __name__ == "__main__":
    main("--keep" in sys.argv)
