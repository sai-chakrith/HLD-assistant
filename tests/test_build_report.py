"""Placeholder resolution and PDF generation of scripts/build_report_pdf.py."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_report_pdf as b  # noqa: E402

DATA = {
    "per_model": {"qwen2.5:3b": {"qa": {"summary": {"answer_correctness": 0.75, "latency": {"p50_ms": 2974.8}}},
                                "heldout_v4": {"syn_hld_v4_heldout": {"micro_all": {"f1": 0.5, "recall": None}}}}},
    "deterministic": {"heldout_v3": {"syn_hld_v3_heldout": {"micro_all": {"f1": 0.25}}},
                      "methods": {"bodycomfort_r3.1.5": {"ports": {"diagram": 3}}}},
    "list": [{"x": 1}, {"x": 2}],
}


def r(t):
    return b.resolve_placeholders(t, DATA)


def test_model_key_with_dot_and_colon():
    assert r("{{per_model.qwen2.5:3b.qa.summary.answer_correctness|pct}}")[0] == "75%"
    assert r("{{per_model.qwen2.5:3b.heldout_v4.syn_hld_v4_heldout.micro_all.f1|f2}}")[0] == "0.50"


def test_doc_id_with_dots_and_bracket_form():
    assert r("{{deterministic.methods.bodycomfort_r3.1.5.ports.diagram}}")[0] == "3"
    assert r("{{['per_model']['qwen2.5:3b']['qa']['summary']['answer_correctness']|pct1}}")[0] == "75.0%"


def test_formats_and_list_index():
    assert r("{{per_model.qwen2.5:3b.qa.summary.latency.p50_ms|s}}")[0] == "3.0 s"
    assert r("{{deterministic.heldout_v3.syn_hld_v3_heldout.micro_all.f1|f1}}")[0] == "0.2"
    assert r("{{list.1.x|int}}")[0] == "2"


def test_len_format():
    assert r("{{list|len}}")[0] == "2"
    assert r("{{per_model.qwen2.5:3b.qa.summary.answer_correctness|len}}")[0] == b.MISSING_TEXT


def test_missing_null_and_bad_format_render_na_without_crash():
    out, missing = r("a {{nope.x}} b {{per_model.qwen2.5:3b.heldout_v4.syn_hld_v4_heldout.micro_all.recall|pct}} "
                     "c {{per_model.qwen2.5:3b|pct}} d {{list.9.x}} e {{deterministic.heldout_v3|zzz}}")
    assert out.count(b.MISSING_TEXT) == 5
    assert len(missing) == 5


def test_text_without_placeholders_is_unchanged():
    t = "no placeholders { here } and `code`"
    assert r(t) == (t, [])


def test_parse_markdown_blocks():
    md = "# T\n\npara one\nline two\n\n- a\n- b\n\n| h1 | h2 |\n|---|---|\n| x | y |\n\n```\ncode\n```\n"
    kinds = [x[0] for x in b.parse_markdown(md)]
    assert kinds == ["h", "p", "ul", "table", "code"]


def test_pdf_is_produced_from_tiny_markdown_and_json(tmp_path):
    md = tmp_path / "r.md"
    md.write_text("# Title\n\nF1 is {{a.b|f2}} and missing {{c}}.\n\n- item **bold** `x`\n\n| k | v |\n|---|---|\n| a | {{a.b|pct}} |\n\n"
                  "```mermaid\nflowchart LR\n A-->B\n```\n", encoding="utf-8")
    js = tmp_path / "res.json"
    js.write_text(json.dumps({"a": {"b": 0.25}}), encoding="utf-8")
    out_md, out_pdf = tmp_path / "r.rendered.md", tmp_path / "r.pdf"
    res = b.build(md, js, out_md, out_pdf)
    assert res["missing"] == ["c"]
    assert "F1 is 0.25" in out_md.read_text(encoding="utf-8")
    assert out_pdf.read_bytes().startswith(b"%PDF") and out_pdf.stat().st_size > 1000


def test_unreadable_results_does_not_crash(tmp_path):
    md = tmp_path / "r.md"
    md.write_text("x {{a.b}}", encoding="utf-8")
    res = b.build(md, tmp_path / "missing.json", tmp_path / "o.md", tmp_path / "o.pdf")
    assert res["missing"] == ["a.b"] and (tmp_path / "o.pdf").is_file()
