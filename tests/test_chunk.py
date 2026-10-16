from app.chunk import chunk_document, est_tokens, split_text, table_to_markdown


def _doc():
    para = " ".join(f"Sentence number {i} talks about wipers and washers." for i in range(120))
    return {
        "doc_id": "d", "project_id": "p", "doc_type": "hld", "version": "v1",
        "sections": [
            {"section_id": "1", "title": "Alpha", "page_start": 1, "page_end": 2, "text": para, "tables": []},
            {"section_id": "2", "title": "Beta", "page_start": 3, "page_end": 3, "text": "Short beta text.",
             "tables": [{"kind": "table", "page_start": 3, "page_end": 3, "header": ["Port", "Interface"],
                         "rows": [["A", "I1"], ["B", "I2"]]}]},
        ],
    }


def test_no_chunk_crosses_section():
    chunks = chunk_document(_doc())
    assert chunks
    for c in chunks:
        sid = c["metadata"]["section_id"]
        title = c["metadata"]["section_title"]
        assert c["text"].startswith(f"{sid} {title}")
        other = "Beta" if sid == "1" else "Alpha"
        assert other not in c["text"].replace(f"{sid} {title}", "")
    assert len({c["id"] for c in chunks}) == len(chunks)


def test_chunk_size_bounds_and_overlap():
    chunks = [c for c in chunk_document(_doc()) if c["metadata"]["section_id"] == "1"]
    assert len(chunks) > 1
    assert all(est_tokens(c["text"]) <= 620 for c in chunks)
    last_sentence = chunks[0]["text"].splitlines()[-1]
    assert last_sentence in chunks[1]["text"]  # overlap carries last sentence forward


def test_table_chunk_markdown_with_heading():
    t = [c for c in chunk_document(_doc()) if c["metadata"]["chunk_type"] == "table"]
    assert len(t) == 1
    assert t[0]["text"].startswith("2 Beta")
    assert "| Port | Interface |" in t[0]["text"] and "| A | I1 |" in t[0]["text"]
    md = table_to_markdown([], [["k", "v"]])
    assert md.splitlines()[1].startswith("|---")


def test_metadata_fields():
    m = chunk_document(_doc())[0]["metadata"]
    for k in ("project_id", "doc_id", "doc_type", "version", "page_start", "page_end",
              "section_id", "section_title", "chunk_type"):
        assert k in m
    assert split_text("", 500, 600, 50) == []
