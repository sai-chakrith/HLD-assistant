"""Fixes found while testing the web UI: junk names, inventory-backed Q&A evidence."""
import json
import sqlite3

import pytest

from app import extract, qa


@pytest.mark.parametrize("name", [": : :1 00 1234", ": Process :", ": :", "12", "A1", "", " - "])
def test_junk_names_rejected(name):
    assert extract.is_junk_name(name)


@pytest.mark.parametrize("name", ["WiperWasher", "Dcm", "FrontWasher", "SeatMemoryAdapter", "Cmd_WiperSpeed"])
def test_real_names_kept(name):
    assert not extract.is_junk_name(name)


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    extract.ensure_schema(c)
    return c


def _add(c, table, name, page, **fields):
    c.execute(f"INSERT INTO {table}(project_id, doc_id, version, name, norm_name, fields, page, section, "
              "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("p", "doc1", "R1", name, extract.normalize_name(name), json.dumps(fields), page, "4.1", "[]",
               "diagram", "Medium", "unreviewed"))


def test_inventory_hits_lists_ports_for_named_component():
    c = _db()
    _add(c, "components", "WiperWasher", 9)
    _add(c, "ports", "WiperSpeed", 10, component="WiperWasher", direction="provided", interface="WiperIf")
    hits = qa.inventory_hits("p", "Which ports does the WiperWasher component have?", c)
    assert len(hits) == 1
    assert "WiperSpeed" in hits[0]["text"] and hits[0]["metadata"]["page_start"] == 9
    assert hits[0]["metadata"]["inventory"] is True


def test_inventory_hits_reports_extraction_gap_not_absence():
    c = _db()
    _add(c, "components", "WiperWasher", 9)
    hits = qa.inventory_hits("p", "Which ports does WiperWasher have?", c)
    assert "extraction gap" in hits[0]["text"]


def test_inventory_hits_ignores_non_architecture_questions():
    c = _db()
    _add(c, "components", "WiperWasher", 9)
    assert qa.inventory_hits("p", "Who wrote the WiperWasher chapter?", c) == []
