"""'What changed between releases' is answered from the inventory diff without the similarity threshold."""
import sqlite3

import pytest

from app import compare, qa, retrieve

DOCS = [("dom_r3.1.5", "R3.1.5"), ("dom_r3.2.3", "R3.2.3")]


def _db(docs=DOCS):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE documents(doc_id TEXT, project_id TEXT, title TEXT, version TEXT, revisions_json TEXT)")
    for doc, ver in docs:
        c.execute("INSERT INTO documents VALUES(?,?,?,?,?)", (doc, "p", "Explanation of the Domain", ver, "[]"))
    return c


def _info(q, c):
    return retrieve.analyze_query(q, retrieve.project_docs("p", c))


def _entry(name, doc="dom_r3.2.3", ver="R3.2.3", page=11, **kw):
    return compare.Entry(key=name.lower(), name=name, citations=[compare.Citation(
        doc_id=doc, version=ver, page=page, section="4.2.1 Mirror Adjustment")], **kw)


def _diff(**kw):
    d = compare.Diff(project_id="p", doc_a="dom_r3.1.5", version_a="R3.1.5", doc_b="dom_r3.2.3", version_b="R3.2.3")
    for t, td in kw.items():
        setattr(d, t, td)
    return d


@pytest.mark.parametrize("q,qtype", [
    ("What changed between release R3.1.5 and R3.2.3?", "what_changed"),
    ("What is new in R3.2.3?", "what_changed"),
    ("What is the purpose of R3.2.3?", "definition"),
])
def test_question_type_for_release_questions(q, qtype):
    assert retrieve.question_type(q) == qtype


def test_pair_from_two_named_releases_oldest_first():
    c = _db()
    q = "What changed between release R3.2.3 and R3.1.5?"
    old, new = qa._pick_release_pair(_info(q, c), retrieve.project_docs("p", c), q)
    assert (old["doc_id"], new["doc_id"]) == ("dom_r3.1.5", "dom_r3.2.3")


def test_pair_from_one_release_is_compared_with_the_one_before():
    c = _db()
    q = "What is new in R3.2.3?"
    old, new = qa._pick_release_pair(_info(q, c), retrieve.project_docs("p", c), q)
    assert (old["version"], new["version"]) == ("R3.1.5", "R3.2.3")
    q = "What is new in R3.1.5?"  # the first release has nothing before it
    assert qa._pick_release_pair(_info(q, c), retrieve.project_docs("p", c), q) is None


def test_pair_from_between_releases_only_when_exactly_two():
    q = "What changed between the releases?"
    c = _db()
    assert qa._pick_release_pair(_info(q, c), retrieve.project_docs("p", c), q) is not None
    c3 = _db(DOCS + [("dom_r3.3.0", "R3.3.0")])
    assert qa._pick_release_pair(_info(q, c3), retrieve.project_docs("p", c3), q) is None


def test_unknown_release_is_refused_naming_it_and_listing_existing_releases():
    c = _db()
    q = "What changed between release R3.1.5 and R9.9.9?"
    ans = qa.release_diff_answer(q, _info(q, c), {}, c, "p", {"project_id": "p", "question": q})
    assert ans.mode == "refused" and ans.refusal_reason == "not_found"
    assert "R9.9.9" in ans.answer and "R3.1.5 (dom_r3.1.5)" in ans.answer and "R3.2.3 (dom_r3.2.3)" in ans.answer
    assert ans.answer.startswith("Release R9.9.9 does not exist")


def test_diff_answer_lists_changes_with_release_and_page_citations(monkeypatch):
    c = _db()
    diff = _diff(ports=compare.TypeDiff(added=[_entry("Interdomain.EnergyMgnt")]),
                 signals=compare.TypeDiff(changed=[_entry("StaOperationMode", changes={
                     "receivers": {"old": ["A"], "new": ["A", "B"]}})]),
                 components=compare.TypeDiff(removed=[_entry("OldComp", "dom_r3.1.5", "R3.1.5", 9)]))
    monkeypatch.setattr(compare, "compare", lambda *a, **k: diff)
    q = "What changed between release R3.1.5 and R3.2.3?"
    ans = qa.release_diff_answer(q, _info(q, c), {}, c, "p", {"project_id": "p", "question": q})
    assert ans.mode == "generated" and ans.built_from == "inventory" and ans.answer_type == "text"
    assert "Interdomain.EnergyMgnt" in ans.answer and "OldComp" in ans.answer and "StaOperationMode" in ans.answer
    assert "receivers: A -> A, B" in ans.answer
    cited = {(x.doc_id, x.version, x.page_start) for x in ans.citations}
    assert cited == {("dom_r3.2.3", "R3.2.3", 11), ("dom_r3.1.5", "R3.1.5", 9)}
    labels = {x.label for x in ans.citations}
    assert labels == {e["label"] for e in ans.evidence} and all(f"[{lab}" in ans.answer or f", {lab}" in ans.answer
                                                                  for lab in labels)


def test_identical_releases_say_so(monkeypatch):
    c = _db()
    monkeypatch.setattr(compare, "compare", lambda *a, **k: _diff())
    q = "What changed between release R3.1.5 and R3.2.3?"
    ans = qa.release_diff_answer(q, _info(q, c), {}, c, "p", {"project_id": "p", "question": q})
    assert "No difference was found" in ans.answer and not ans.citations


def test_other_questions_are_left_to_the_normal_path():
    c = _db()
    q = "Which ports does the Doorlocks component have?"
    assert qa.release_diff_answer(q, _info(q, c), {}, c, "p", {}) is None
