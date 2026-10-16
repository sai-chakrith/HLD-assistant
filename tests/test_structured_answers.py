"""Structured (inventory-built) answers for list/connection questions; the LLM only adds a description sentence."""
import json
import sqlite3

from app import extract, qa, retrieve


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    extract.ensure_schema(c)
    return c


def _add(c, table, name, page, method="table", **fields):
    c.execute(f"INSERT INTO {table}(project_id, doc_id, version, name, norm_name, fields, page, section, "
              "sources_json, extraction_method, confidence, review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ("p", "doc1", "1.0", name, extract.normalize_name(name), json.dumps(fields), page, "3.1", "[]",
               method, "High", "unreviewed"))


def _seed():
    c = _db()
    _add(c, "components", "DoorCtrl", 4)
    _add(c, "components", "LockDrv", 5)
    _add(c, "ports", "RpFob", 4, component="DoorCtrl", direction="required", interface="IFob")
    _add(c, "ports", "RpPos", 4, component="DoorCtrl", direction="required", interface="IPos")
    _add(c, "ports", "PpLockReq", 4, component="DoorCtrl", direction="provided", interface="ILockReq")
    _add(c, "ports", "RpLockReq", 5, component="LockDrv", direction="required", interface="ILockReq")
    _add(c, "signals", "LockCmd", 8, sender="DoorCtrl", receivers=["LockDrv"])
    _add(c, "signals", "StaLock", 8, sender="LockDrv", receivers=["DoorCtrl"])
    return c


def _ask(c, question):
    hits = qa.inventory_hits("p", question, c)
    inv = extract.get_inventory("p", conn=c)
    info = retrieve.analyze_query(question, [], retrieve.inventory_names(inv))
    return qa.structured_answer(question, info, qa.label_map(hits))


def test_required_interfaces_only():
    text, labels = _ask(_seed(), "Which interfaces does the DoorCtrl require?")
    assert "IFob" in text and "IPos" in text and "ILockReq" not in text
    assert labels == ["C1"] and text.endswith("[C1].")


def test_signals_sent_by_component():
    text, _ = _ask(_seed(), "Which signals does DoorCtrl send?")
    assert "LockCmd" in text and "StaLock" not in text


def test_all_ports_without_direction_word():
    text, _ = _ask(_seed(), "Which ports does the DoorCtrl have?")
    assert all(x in text for x in ("RpFob", "RpPos", "PpLockReq"))


def test_who_sends_identifier():
    text, _ = _ask(_seed(), "Who sends the LockCmd signal and who receives it?")
    assert "sent by DoorCtrl" in text and "received by LockDrv" in text


def test_unknown_identifier_falls_back_to_llm():
    assert _ask(_seed(), "Which interface types the RpMissing port of the DoorCtrl?") is None


def test_attribute_question_falls_back_to_llm():
    assert _ask(_seed(), "What is the data type of the LockCmd signal?") is None


def test_non_list_question_not_structured():
    assert _ask(_seed(), "What does the DoorCtrl do?") is None


def test_shared_port_between_two_components():
    c = _seed()
    _add(c, "ports", "ReqLock", 5, component="LockDrv", direction="required")
    _add(c, "ports", "ReqLock", 4, component="DoorCtrl", direction="provided")
    text, labels = _ask(c, "Through which port is the lock request exchanged between DoorCtrl and LockDrv?")
    assert "ReqLock" in text and len(labels) == 2


class _Fake:
    model = "fake"

    def __init__(self, reply):
        self.reply = reply

    def generate(self, prompt, system="", model=None, purpose="", chunk_ids=None):
        return self.reply


def _lmap():
    inv = {"id": "inv:doc1:doorctrl", "text": "EXTRACTED INVENTORY ...", "score": 1.0,
           "rows": {"kind": "component", "component": "DoorCtrl"},
           "metadata": {"doc_id": "doc1", "inventory": True, "page_start": 4}}
    txt = {"id": "doc1::3.1::0", "text": "DoorCtrl decides when the doors lock.", "score": 0.8,
           "metadata": {"doc_id": "doc1", "page_start": 4}}
    return {"C1": inv, "C2": txt}


def test_description_sentence_kept_when_cited():
    desc, note = qa._description(_Fake("DoorCtrl decides when the doors lock [C2]."), "q", "DoorCtrl", _lmap(), None)
    assert desc.endswith("[C2].") and note == ""


def test_description_contradicting_inventory_dropped():
    desc, note = qa._description(_Fake("DoorCtrl has no ports listed in the documents [C2]."), "q", "DoorCtrl",
                                 _lmap(), None)
    assert desc == "" and "inventory was used" in note


def test_description_cannot_cite_inventory_chunk():
    desc, _ = qa._description(_Fake("DoorCtrl locks doors [C1]."), "q", "DoorCtrl", _lmap(), None)
    assert desc == ""


def test_display_name_prefers_matching_part_of_instance_type_name():
    c = _db()
    _add(c, "ports", "StaRain", 10, method="diagram", component="RainSensor::Rain...")
    hits = qa.inventory_hits("p", "Which ports does the RainSensor have?", c)
    assert "for component RainSensor " in hits[0]["text"]


def _lmap_rows():
    m = _lmap()
    m["C1"]["rows"] = {"kind": "component", "component": "DoorCtrl", "norm": "doorctrl",
                       "ports": [{"name": "RpFob", "interface": "IFob", "direction": "required"},
                                 {"name": "PpLockReq", "interface": "ILockReq", "direction": "provided"}],
                       "signals": []}
    return m


def test_description_naming_unlisted_interface_is_a_conflict():
    listed = "the required interfaces of DoorCtrl are: IFob (port RpFob) [C1]."
    desc, note = qa._description(_Fake("DoorCtrl requires the ILockReq and IFob interfaces [C2]."), "q", "DoorCtrl",
                                 _lmap_rows(), None, listed=listed)
    assert desc == "" and "inventory was used" in note


def test_description_restating_listed_items_dropped_silently():
    listed = "the required interfaces of DoorCtrl are: IFob (port RpFob) [C1]."
    desc, note = qa._description(_Fake("DoorCtrl requires the IFob interface [C2]."), "q", "DoorCtrl",
                                 _lmap_rows(), None, listed=listed)
    assert desc == "" and note == ""


def test_description_with_other_sender_for_identifier_is_a_conflict():
    m = _lmap()
    m["C1"]["rows"] = {"kind": "identifier", "identifier": "ReqFind", "norm": "reqfind", "ports": [],
                       "signals": [{"name": "ReqFind", "sender": "RemoteKey", "receivers": ["LightMgr"]}]}
    desc, note = qa._description(_Fake("The HmiPanel sends ReqFind to locate the car [C2]."), "q", "ReqFind", m, None,
                                 listed="signal ReqFind is sent by RemoteKey [C1].")
    assert desc == "" and "inventory was used" in note


def test_description_agreeing_receiver_is_not_a_conflict():
    m = _lmap()
    m["C1"]["rows"] = {"kind": "identifier", "identifier": "MotCur", "norm": "motcur", "ports": [],
                       "signals": [{"name": "MotCur", "sender": "MotorDrv", "receivers": ["DiagHandler"]}]}
    desc, note = qa._description(_Fake("The DiagHandler receives the MotCur signal [C2]."), "q", "MotCur", m, None,
                                 listed="signal MotCur is sent by MotorDrv and received by DiagHandler [C1].")
    assert desc == "" and note == ""
