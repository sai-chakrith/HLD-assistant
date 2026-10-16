"""QA pipeline: refusal, prompt labels, citation parsing, evidence-only mode, confidence rule."""
import pytest

from app import controls, qa


class FakeProvider:
    name = "fake"

    def __init__(self, text):
        self.text, self.prompt = text, None

    def generate(self, prompt, system="", schema=None):
        self.prompt = prompt
        return self.text


def _hit(i, score=0.8):
    return {"id": f"c{i}", "text": f"text {i}", "score": score,
            "metadata": {"doc_id": "d", "version": "1", "page_start": i, "page_end": i, "section_id": f"{i}",
                         "section_title": f"T{i}"}}


def test_build_prompt_labels():
    prompt, system = qa.build_prompt("Q?", [_hit(1), _hit(2)])
    assert "[C1]" in prompt and "[C2]" in prompt and "Q?" in prompt and "ONLY" in system


def test_parse_citations_drops_invented():
    lmap = qa.label_map([_hit(1), _hit(2)])
    assert qa.parse_citations("A [C1] and [C2, C9] but [C7] and [C1].", lmap) == ["C1", "C2"]
    assert qa.parse_citations("no cites", lmap) == []


def test_confidence_rule():
    assert qa.compute_confidence("generated", 0.8, 2) == "High"
    assert qa.compute_confidence("generated", 0.5, 2) == "Medium"
    assert qa.compute_confidence("generated", 0.8, 1) == "Medium"
    assert qa.compute_confidence("generated", 0.8, 0) == "Low"
    assert qa.compute_confidence("evidence_only", 0.9, 3) == "Low"
    assert qa.compute_confidence("refused", 0.9, 3) == "Low"


@pytest.fixture
def indexed(conn, dirs, make_pdf, tmp_path):
    from app.ingest import ingest_document
    ingest_document(make_pdf(tmp_path / "doc.pdf"), "p1", "hld", "1.0", "approved", "admin", conn=conn, **dirs)
    return dirs


def test_evidence_only_with_null_provider(conn, indexed):
    a = qa.ask("p1", "What does the WiperManager component do?", "view", conn=conn,
               chroma_dir=indexed["chroma_dir"], threshold=-1.0)
    assert a.mode == "evidence_only" and a.confidence == "Low"
    assert a.answer == qa.EVIDENCE_ONLY_TEXT and a.citations and a.evidence
    assert a.citations[0].doc_id == "doc" and a.citations[0].label == "C1"
    assert conn.execute("SELECT review_status FROM answers WHERE id=?", (a.id,)).fetchone()[0] == "unreviewed"
    assert conn.execute("SELECT 1 FROM audit_log WHERE action='ask'").fetchone()


def test_refused_below_threshold(conn, indexed):
    a = qa.ask("p1", "WiperManager", "view", conn=conn, chroma_dir=indexed["chroma_dir"], threshold=2.0)
    assert a.mode == "refused" and a.confidence == "Low" and a.limitation and not a.citations


def test_generated_with_valid_and_invented_labels(conn, indexed):
    prov = FakeProvider("It moves the wiper [C1] and more [C99].")
    a = qa.ask("p1", "wiper", "view", conn=conn, chroma_dir=indexed["chroma_dir"], provider=prov, threshold=-1.0)
    assert a.mode == "generated" and [c.label for c in a.citations] == ["C1"] and a.confidence == "Medium"
    assert "[C1]" in prov.prompt


def test_nonmember_denied(conn, indexed):
    controls.add_user("outsider", "viewer", conn=conn)
    with pytest.raises(controls.PermissionDenied):
        qa.ask("p1", "x", "outsider", conn=conn, chroma_dir=indexed["chroma_dir"])


def test_postprocess_drops_uncited_and_invented():
    lmap = qa.label_map([_hit(1), _hit(2)])
    txt = "The wiper moves [C1]. It is blue. Washer sprays [C2, C9]. Also fake [C9]. Not stated in the provided documents."
    clean, labels, share = qa.postprocess(txt, lmap)
    assert labels == ["C1", "C2"]
    assert "blue" not in clean and "fake" not in clean and "C9" not in clean
    assert clean.count("[C") == 2 and "Not stated" in clean
    assert abs(share - 0.5) < 1e-9


def test_postprocess_label_after_period():
    lmap = qa.label_map([_hit(1)])
    clean, labels, share = qa.postprocess("It works. [C1] Another fact. [C1]", lmap)
    assert labels == ["C1"] and share == 1.0 and clean.count("[C1]") == 2


def test_confidence_cited_share():
    assert qa.compute_confidence("generated", 0.8, 2, 0.9) == "High"
    assert qa.compute_confidence("generated", 0.8, 2, 0.6) == "Medium"
    assert qa.compute_confidence("generated", 0.8, 2, 0.3) == "Low"


def test_all_uncited_becomes_refusal(conn, indexed):
    a = qa.ask("p1", "wiper", "view", conn=conn, chroma_dir=indexed["chroma_dir"],
               provider=FakeProvider("The wiper is great. It is blue."), threshold=-1.0)
    assert a.mode == "refused" and a.answer == qa.REFUSAL_TEXT and a.confidence == "Low" and not a.citations


def test_model_passed_to_provider(conn, indexed):
    class P:
        name = "p"
        def generate(self, prompt, system="", schema=None, model=None, purpose="", chunk_ids=None):
            self.kw = (model, purpose, chunk_ids)
            return "Moves the wiper [C1]."
    p = P()
    qa.ask("p1", "wiper", "view", conn=conn, chroma_dir=indexed["chroma_dir"], provider=p, threshold=-1.0,
           model="qwen3:4b-instruct")
    assert p.kw[0] == "qwen3:4b-instruct" and p.kw[1] == "qa" and p.kw[2]


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    """Real bge embedder + real chroma; the real SQLite DB is copied so answers are not written to it."""
    import shutil

    from app import retrieve
    from app.config import settings
    monkeypatch.delenv("EMBEDDER", raising=False)
    retrieve.reset_embedder()
    db = tmp_path / "real.db"
    shutil.copy(settings.db_path, db)
    c = controls.get_conn(db)
    yield c, settings.chroma_dir, tmp_path
    c.close()


@pytest.mark.llm
def test_real_grounded_answer(real_env):
    from app import llm
    c, chroma, tmp = real_env
    prov = llm.OllamaProvider(db_path=tmp / "cache.db", trace_path=tmp / "t.jsonl")
    a = qa.ask("body-comfort", "What does the FrontWasher component do?", "viewer", conn=c, chroma_dir=chroma,
               provider=prov)
    assert a.mode == "generated" and a.citations and "[C" in a.answer
    assert all(x.doc_id and x.page_start for x in a.citations)


@pytest.mark.llm
def test_real_unanswerable_refused(real_env):
    c, chroma, tmp = real_env
    a = qa.ask("chassis", "How do I bake sourdough bread?", "viewer", conn=c, chroma_dir=chroma)
    assert a.mode == "refused" and a.confidence == "Low"


def test_hash_embedder_fallback_refuses(conn, indexed, monkeypatch):
    monkeypatch.delenv("ALLOW_HASH_QA", raising=False)
    prov = FakeProvider("It moves the wiper [C1].")
    a = qa.ask("p1", "What does the WiperManager do?", "view", conn=conn, chroma_dir=indexed["chroma_dir"],
               provider=prov, threshold=-1.0)
    assert a.mode == "refused" and a.confidence == "Low" and "fallback active" in a.limitation.lower()
    assert prov.prompt is None  # the LLM was never called


# ---------------------------------------------------------------- off-topic vs not-found refusals
def test_term_coverage_ignores_generic_words_and_versions():
    from app import retrieve
    corpus = retrieve.Corpus([], [], [], retrieve.BM25([retrieve.tokenize("wiper washer manager controls the pump")]),
                             {})
    cov, missing = retrieve.term_coverage("What is the recommended oven temperature for the wiper in R3.2.3?", corpus)
    assert missing == ["oven", "temperature"] and abs(cov - 1 / 3) < 1e-9
    assert retrieve.term_coverage("What does the wiper washer manager control?", corpus)[0] == 1.0


def test_off_topic_question_refused_before_the_llm(conn, indexed):
    prov = FakeProvider("Bake it at 250 degrees [C1].")
    a = qa.ask("p1", "What oven temperature bakes sourdough bread?", "view", conn=conn,
               chroma_dir=indexed["chroma_dir"], provider=prov, threshold=-1.0)
    assert a.mode == "refused" and a.refusal_reason == "off_topic" and "Off-topic" in a.limitation
    assert prov.prompt is None  # never reached the LLM


def test_on_topic_not_stated_says_what_was_searched(conn, indexed):
    a = qa.ask("p1", "wiper", "view", conn=conn, chroma_dir=indexed["chroma_dir"],
               provider=FakeProvider("Not stated in the provided documents."), threshold=-1.0)
    assert a.mode == "refused" and a.refusal_reason == "not_found"
    assert "Not found in doc (1.0)" in a.limitation
