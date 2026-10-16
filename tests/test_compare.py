import json

import pytest

from app import extract
from app.compare import compare


def _doc(conn, project, doc_id, version, revisions=()):
    conn.execute("INSERT INTO documents(doc_id, project_id, title, doc_type, version, source_status, sha256, "
                 "revisions_json) VALUES(?,?,?,?,?,?,?,?)",
                 (doc_id, project, doc_id, "hld", version, "synthetic", "x", json.dumps(list(revisions))))
    conn.commit()


def _ent(conn, project, doc_id, version, table, name, page=1, **fields):
    extract.ensure_schema(conn)
    conn.execute(f"INSERT INTO {table}(project_id, doc_id, version, name, norm_name, fields, page, section, "
                 "sources_json, extraction_method, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (project, doc_id, version, name, extract.normalize_name(name), json.dumps(fields), page, "1",
                  json.dumps([{"doc_id": doc_id, "version": version, "page": page, "section": "1"}]),
                  "table", "unreviewed"))
    conn.commit()


@pytest.fixture
def two(conn):
    _doc(conn, "p1", "a", "1.0", [{"date": "d1", "version": "1.0", "changed_by": "x", "description": "init"}])
    _doc(conn, "p1", "b", "2.0", [{"date": "d1", "version": "1.0", "changed_by": "x", "description": "init"},
                                  {"date": "d2", "version": "2.0", "changed_by": "y", "description": "more"}])
    return conn


def test_added_removed_changed(two):
    c = two
    _ent(c, "p1", "a", "1.0", "components", "Alpha", description="old text", category="x")
    _ent(c, "p1", "a", "1.0", "components", "Gone", description="g")
    _ent(c, "p1", "b", "2.0", "components", "Alpha", page=9, description="new text", category="x")
    _ent(c, "p1", "b", "2.0", "components", "Fresh", description="f")
    _ent(c, "p1", "a", "1.0", "ports", "P1", component="Alpha", direction="provided", interface="I")
    _ent(c, "p1", "b", "2.0", "ports", "P1", component="Alpha", direction="provided", interface="J")
    _ent(c, "p1", "a", "1.0", "signals", "S", receivers=["B", "C"])
    _ent(c, "p1", "b", "2.0", "signals", "S", receivers=["C", "B"])  # order only -> no change
    d = compare("p1", "a", "1.0", "b", "2.0", conn=c)
    assert [e.name for e in d.components.added] == ["Fresh"]
    assert [e.name for e in d.components.removed] == ["Gone"]
    assert d.components.changed[0].changes == {"description": {"old": "old text", "new": "new text"}}
    assert d.ports.changed[0].key == "alpha.p1" and set(d.ports.changed[0].changes) == {"interface"}
    assert d.signals.changed == []
    assert d.review_status == "unreviewed"
    assert d.summary["components"] == {"added": 1, "removed": 1, "changed": 1, "possible_renames": 0}
    assert d.summary["total"]["changed"] == 2
    assert len(d.revisions_a) == 1 and len(d.revisions_b) == 2
    assert d.components.added[0].citations[0].doc_id == "b"
    assert d.components.removed[0].citations[0].doc_id == "a"


def test_rename_detected(two):
    _ent(two, "p1", "a", "1.0", "components", "WindowLiftController")
    _ent(two, "p1", "b", "2.0", "components", "WindowLiftControler2")
    d = compare("p1", "a", "1.0", "b", "2.0", conn=two)
    assert len(d.components.possible_renames) == 1
    r = d.components.possible_renames[0]
    assert r.old == "WindowLiftController" and r.score >= 88
    assert len(d.components.added) == 1 and len(d.components.removed) == 1  # documented: kept in both


def test_page_and_whitespace_only_not_changes(two):
    _ent(two, "p1", "a", "1.0", "components", "Alpha", page=3, description="Does  a THING", category="X")
    _ent(two, "p1", "b", "2.0", "components", "alpha", page=40, description="does a thing\n", category="x")
    d = compare("p1", "a", "1.0", "b", "2.0", conn=two)
    assert d.summary["total"] == {"added": 0, "removed": 0, "changed": 0, "possible_renames": 0}


def test_cross_project_refused(two):
    _doc(two, "p2", "c", "1.0")
    with pytest.raises(ValueError):
        compare("p1", "a", "1.0", "c", "1.0", conn=two)
    with pytest.raises(ValueError):
        compare("p2", "a", "1.0", "c", "1.0", conn=two)


def test_llm_only_entities_on_one_side_are_not_counted_as_release_changes(two):
    c = two
    _ent(c, "p1", "a", "1.0", "components", "Stable")
    _ent(c, "p1", "b", "2.0", "components", "Stable")
    _ent(c, "p1", "a", "1.0", "components", "OnlyLlmA")
    _ent(c, "p1", "b", "2.0", "components", "OnlyLlmB")
    c.execute("UPDATE components SET extraction_method='llm' WHERE name LIKE 'OnlyLlm%'")
    c.commit()
    d = compare("p1", "a", "1.0", "b", "2.0", conn=c)
    assert d.components.added == [] and d.components.removed == [] and d.components.possible_renames == []
    assert sorted(e.name for e in d.components.llm_only_unmatched) == ["OnlyLlmA", "OnlyLlmB"]
    assert d.llm_only_unmatched_total == 2 and d.summary["total"]["added"] == 0
