"""Change-history answers from the parsed revision tables, and the 'releases differ' note."""
import json
import sqlite3

from app import qa, retrieve

REV_OLD = [{"date": "23.06.2008", "version": "1.0.1", "changed_by": "Admin", "description": "Legal disclaimer revised"},
           {"date": "04.12.2007", "version": "1.0.0", "changed_by": "Admin", "description": "Initial Release"}]
REV_NEW = [{"date": "23.06.2008", "version": "1.0.1", "changed_by": "Admin", "description": "Legal disclaimer revised"},
           {"date": "27.04.2011", "version": "1.0.2", "changed_by": "Admin", "description": "Typo fixes"},
           {"date": "04.12.2007", "version": "1.0.0", "changed_by": "Admin", "description": "Initial Release"}]


def _db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE documents(doc_id TEXT, project_id TEXT, title TEXT, version TEXT, revisions_json TEXT)")
    for doc, ver, rev in (("dom_r3.1.5", "R3.1.5", REV_OLD), ("dom_r3.2.3", "R3.2.3", REV_NEW)):
        c.execute("INSERT INTO documents VALUES(?,?,?,?,?)", (doc, "p", "Explanation of the Domain", ver, json.dumps(rev)))
    return c


def _hist_hit(doc):
    return {"id": f"{doc}::0::0", "text": "| Date | Version | Changed by | Change Description |", "score": 0.7,
            "metadata": {"doc_id": doc, "page_start": 1}}


def _lmap(*docs):
    return qa.label_map([_hist_hit(d) for d in docs] + [{"id": "x", "text": "other text", "score": 0.6,
                                                          "metadata": {"doc_id": docs[0], "page_start": 5}}])


def _info(q, c):
    return retrieve.analyze_query(q, retrieve.project_docs("p", c))


def test_latest_entry_from_newest_release_sorted_by_date():
    c = _db()
    q = "In the latest release, what is the most recent entry in the document change history?"
    text, labels, note = qa.history_answer(q, _info(q, c), _lmap("dom_r3.2.3"), c, "p")
    assert "27.04.2011" in text and "1.0.2" in text and "Typo fixes" in text and "newest release" in text
    assert labels == ["C1"] and "change-history table" in note


def test_what_changed_between_named_releases_lists_new_rows_only():
    c = _db()
    q = "What changed between release R3.1.5 and R3.2.3 of the document?"
    text, labels, _ = qa.history_answer(q, _info(q, c), _lmap("dom_r3.2.3", "dom_r3.1.5"), c, "p")
    assert "Typo fixes" in text and "Initial Release" not in text and set(labels) == {"C1", "C2"}


def test_history_needs_a_retrieved_history_chunk():
    c = _db()
    q = "What changed between release R3.1.5 and R3.2.3 of the document?"
    lmap = qa.label_map([{"id": "x", "text": "plain text", "score": 0.6, "metadata": {"doc_id": "dom_r3.2.3"}}])
    assert qa.history_answer(q, _info(q, c), lmap, c, "p") is None


def test_non_history_question_not_answered_here():
    c = _db()
    q = "What does the WiperWasher do?"
    assert qa.history_answer(q, _info(q, c), _lmap("dom_r3.2.3"), c, "p") is None


def test_release_difference_note_only_when_texts_differ():
    same = {"id": "n::4.1::0", "text": "The wiper manager controls the wipers.", "replaced": "o::4.1::0",
            "replaced_text": "The wiper manager controls the wipers.", "metadata": {"doc_id": "n"}}
    diff = dict(same, replaced_text="The washer pump is driven by a relay output on the body controller.")
    assert qa.release_difference_note({"C1": same}, ["C1"]) == ""
    assert "differs from the same section in o" in qa.release_difference_note({"C1": diff}, ["C1"])


# ---------------------------------------------------------------- evidence-support confidence (qa.evidence_support)
def _ev(text, inventory=False):
    return {"id": text[:8], "text": text, "score": 0.8, "metadata": {"doc_id": "d", "inventory": inventory}}


def test_sentence_supported_needs_numbers_and_identifiers():
    chunk = "All window movements stop below 9 V and above 16 V supply voltage (WinCtrlManager)."
    assert qa.sentence_supported("Window movements stop below 9 V and above 16 V [C1].", chunk)
    assert not qa.sentence_supported("Window movements stop below 8 V and above 16 V [C1].", chunk)
    assert not qa.sentence_supported("The MotorDriver stops all window movements [C1].", chunk)


def test_high_needs_support_and_two_agreeing_sources():
    lmap = {"C1": _ev("The ACC composition outputs a desired acceleration to the VLC."),
            "C2": _ev("ACC sends the acceleration request to VLC for longitudinal control.")}
    text = "The ACC composition outputs a desired acceleration [C1]. The acceleration request goes to the VLC [C2]."
    sup = qa.evidence_support(text, lmap)
    assert sup == {"claims": 2, "supported": 2, "agreeing": 2, "inventory": False}
    assert qa.compute_confidence("generated", 0.8, 2, 1.0, sup) == "High"
    one = qa.evidence_support("The ACC composition outputs a desired acceleration [C1, C2].", lmap)
    assert one["agreeing"] == 1  # only C1 supports the claim: one source is not enough for High
    assert qa.compute_confidence("generated", 0.8, 2, 1.0, one) == "Medium"


def test_unsupported_claim_blocks_high_and_majority_unsupported_is_low():
    lmap = {"C1": _ev("The ACC composition outputs a desired acceleration to the VLC."),
            "C2": _ev("Chassis signals are treated as safety relevant.")}
    mixed = qa.evidence_support("The ACC composition outputs a desired acceleration [C1]. "
                                "Joysticks and microphones are standardized UI devices [C2].", lmap)
    assert qa.compute_confidence("generated", 0.8, 2, 1.0, mixed) == "Medium"
    bad = qa.evidence_support("Joysticks and microphones are standardized UI devices [C2].", lmap)
    assert qa.compute_confidence("generated", 0.8, 1, 1.0, bad) == "Low"


def test_inventory_citation_counts_as_support():
    lmap = {"C1": _ev("EXTRACTED INVENTORY for DoorCtrl: port RpFob", inventory=True)}
    sup = qa.evidence_support("The DoorCtrl has port RpFob [C1].", lmap)
    assert sup["supported"] == 1 and sup["inventory"]


def test_inventory_chunk_does_not_support_a_port_it_does_not_list():
    lmap = {"C1": _ev("EXTRACTED INVENTORY for component WinMgr:\n- port RpLockState (direction required)", True)}
    assert qa.evidence_support("The RpChildLocks port of WinMgr uses IChildLock [C1].", lmap)["supported"] == 0
    assert qa.evidence_support("WinMgr requires port RpLockState [C1].", lmap)["supported"] == 1
