"""Section-level (text) revision comparison: noise filtering, alignment, API, export, synthetic v1 -> v2."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import api, extract
from app.compare import compare, section_diff
from app.ingest import ingest_document

H = lambda u: {"X-User": u}  # noqa: E731


def sec(sid, title, text, p=1, tables=()):
    return {"section_id": sid, "title": title, "level": 1, "parent_id": None, "page_start": p, "page_end": p,
            "text": text, "tables": list(tables)}


def doc(*secs):
    return {"sections": [sec("0", "Front matter", "cover")] + list(secs)}


def test_noise_is_ignored():
    a = doc(sec("1", "Intro", "The window\nmoves up.\n5 of 40\nDocument ID 12: AUTOSAR_Test\n- AUTOSAR CONFIDENTIAL-\n"
                "R3.1 Rev 0001\nIt is com-\nmand driven."))
    b = doc(sec("1", "Intro", "The  window moves up.\n6 of 41\nDocument ID 12: AUTOSAR_Test\n- AUTOSAR CONFIDENTIAL-\n"
                "R3.2 Rev 1\nIt is command   driven.", p=2))
    d = section_diff(a, b)
    assert d.summary == {"added": 0, "removed": 0, "modified": 0, "front_matter": 0}


def test_real_change_reported_with_pages_and_sentences():
    a = doc(sec("1", "Intro", "The window moves up. It stops at 5 N.", p=3))
    b = doc(sec("1", "Intro", "The window moves up. It stops at 8 N force.", p=4))
    d = section_diff(a, b)
    assert d.summary["modified"] == 1
    m = d.modified[0]
    assert m.pages_a[0] == 3 and m.pages_b[0] == 4 and m.words_added >= 1 and "words" in m.summary
    assert "5 N" in m.sentences[0]["old"] and "8 N" in m.sentences[0]["new"]


def test_reorder_only_is_ignored_but_added_removed_found():
    a = doc(sec("1", "Fig", "alpha beta gamma delta"), sec("2", "Gone", "old stuff here"))
    b = doc(sec("1", "Fig", "delta gamma alpha beta"), sec("3", "New", "fresh content"))
    d = section_diff(a, b)
    assert d.summary == {"added": 1, "removed": 1, "modified": 0, "front_matter": 0}


def test_alignment_renumbered_and_retitled():
    a = doc(sec("2", "Port Table", "ports x y z"), sec("3", "Signal Table Overview", "signals one two"))
    b = doc(sec("4", "Port Table", "ports x y z more"), sec("3.1", "Signal Table Overviews", "signals one two"))
    d = section_diff(a, b)
    assert d.summary["added"] == d.summary["removed"] == 0
    assert d.summary["modified"] == 1 and d.modified[0].renumbered and d.modified[0].section_b == "4"


def test_front_matter_and_history_separate():
    hist = {"table_id": "t", "kind": "change_history", "header": [], "rows": [["d", "1.0", "x", "init"]]}
    a = {"sections": [sec("0", "Front matter", "release 3.1", tables=[hist]), sec("1", "Body", "same")]}
    hist2 = dict(hist, rows=[["d", "1.0", "x", "init"], ["d2", "2.0", "y", "more"]])
    b = {"sections": [sec("0", "Front matter", "release 3.2", tables=[hist2]), sec("1", "Body", "same")]}
    d = section_diff(a, b)
    assert d.summary["modified"] == 0 and d.summary["front_matter"] == 2
    assert any(c.title == "Change history" and c.rows_added for c in d.front_matter)


@pytest.fixture
def syn2(conn, dirs, tmp_path):
    gen = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "generate_hld.py"
    spec = importlib.util.spec_from_file_location("generate_hld_sd", gen)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = tmp_path / "syn"
    mod.generate(out)
    ids = []
    for f, v in (("hld_v1.pdf", "1.0"), ("hld_v2.pdf", "2.0")):
        i = ingest_document(out / f, "p1", "hld", v, "synthetic", "admin", conn=conn, **dirs)
        extract.extract_document("p1", i, conn=conn, processed_dir=dirs["processed_dir"])
        ids.append(i)
    return ids


def test_synthetic_v1_v2(conn, dirs, syn2):
    d = compare("p1", syn2[0], "1.0", syn2[1], "2.0", conn=conn, processed_dir=dirs["processed_dir"])
    assert d.sections is not None
    titles = {c.title: c for c in d.sections.modified}
    for t in ("Component Overview", "Interface Table", "Port Table", "Signal Table"):
        assert titles[t].rows_added, t
    assert any("Flow" in t for t in titles)
    assert any(c.title == "Change history" for c in d.sections.front_matter)


def test_missing_processed_json_skips(conn, dirs, syn2, tmp_path):
    d = compare("p1", syn2[0], "1.0", syn2[1], "2.0", conn=conn, processed_dir=tmp_path / "nowhere")
    assert d.sections is None


def test_api_and_export(conn, dirs, syn2, tmp_path):
    conn.close()
    paths = api.Paths(tmp_path / "test.db", dirs["chroma_dir"], dirs["processed_dir"])
    api.app.dependency_overrides[api.get_paths] = lambda: paths
    try:
        client = TestClient(api.app)
        r = client.post("/projects/p1/compare", headers=H("view"), json={"doc_a": syn2[0], "doc_b": syn2[1]})
        assert r.status_code == 200
        s = r.json()["sections"]
        assert s["summary"]["modified"] >= 4 and s["modified"][0]["pages_b"]
        x = client.get("/projects/p1/export/diff", headers=H("view"),
                       params={"doc_a": syn2[0], "doc_b": syn2[1], "fmt": "xlsx"})
        assert x.status_code == 200
        f = tmp_path / "d.xlsx"
        f.write_bytes(x.content)
        sheets = pd.read_excel(f, sheet_name=None)
        assert "section_changes" in sheets and len(sheets["section_changes"]) >= 4
        c = client.get("/projects/p1/export/diff", headers=H("view"),
                       params={"doc_a": syn2[0], "doc_b": syn2[1], "fmt": "json"})
        assert b"section_changes" in c.content
    finally:
        api.app.dependency_overrides.clear()
