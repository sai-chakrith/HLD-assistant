"""Synthetic HLD generator: PDFs parse, extraction equals ground truth, planted defects are found."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from app import checks, controls, extract
from app.ingest import ingest_document

GEN = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "generate_hld.py"


@pytest.fixture(scope="module")
def gen_dir(tmp_path_factory):
    spec = importlib.util.spec_from_file_location("generate_hld", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = tmp_path_factory.mktemp("syn")
    mod.generate(d)
    return d


def test_files_and_ground_truth_consistent(gen_dir):
    assert (gen_dir / "hld_v1.pdf").stat().st_size > 1000 and (gen_dir / "hld_v2.pdf").stat().st_size > 1000
    gt = json.loads((gen_dir / "ground_truth.json").read_text())
    assert len(gt["planted_defects"]) == 8 and {d["rule_id"] for d in gt["planted_defects"]} == \
        {"M1", "M2", "M3", "M4", "C1", "C2", "C3", "C4"}
    e1, e2, diff = gt["entities"]["1.0"], gt["entities"]["2.0"], gt["expected_diff"]
    assert 6 <= len(e1["components"]) <= 8 and diff["components"]["added"] == ["WinLockHandler"]
    assert diff["ports"]["removed"] == ["WinSwitchHandler.RpLockState"]
    assert diff["interfaces"]["changed"]["IDiagData"]["kind"] == {"old": "sender_receiver", "new": "client_server"}
    assert len(e2["components"]) == len(e1["components"]) + 1
    assert len(gt["remaining_defects_v2"]) == 5


def test_pipeline_extract_and_checks(gen_dir, conn, dirs):
    controls.add_member("p1", "admin", conn=conn)
    gt = json.loads((gen_dir / "ground_truth.json").read_text())
    for ver, d in gt["documents"].items():
        ingest_document(gen_dir / d["file"], "p1", "hld", ver, "synthetic", "admin", doc_id=d["doc_id"],
                        conn=conn, index=False, **{"processed_dir": dirs["processed_dir"]})
        extract.extract_document("p1", d["doc_id"], conn=conn, processed_dir=dirs["processed_dir"])
        inv = extract.get_inventory("p1", d["doc_id"], conn=conn)
        for t in ("components", "ports", "interfaces", "signals"):
            assert len(inv[t]) == len(gt["entities"][ver][t]), (ver, t)
        found = checks.run_checks("p1", d["doc_id"], ver, conn=conn, processed_dir=dirs["processed_dir"])
        expected = gt["planted_defects"] if ver == "1.0" else gt["remaining_defects_v2"]
        assert sorted(f.rule_id for f in found) == sorted(x["rule_id"] for x in expected)
