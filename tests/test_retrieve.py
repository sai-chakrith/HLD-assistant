import pytest

from app.chunk import chunk_document
from app.retrieve import HashingEmbedder, collection_name, get_embedder, index_chunks, search


def _doc(project, doc_id, text):
    return {"doc_id": doc_id, "project_id": project, "doc_type": "hld", "version": "v1",
            "sections": [{"section_id": "1", "title": "Wipers", "page_start": 1, "page_end": 1,
                          "text": text, "tables": []}]}


def test_hash_embedder_forced_and_normalised():
    e = get_embedder()
    assert isinstance(e, HashingEmbedder)
    v = e.encode(["front washer nozzle"])[0]
    assert abs(sum(x * x for x in v) - 1.0) < 1e-4


def test_search_returns_scored_hits(dirs):
    index_chunks("p1", chunk_document(_doc("p1", "d1", "The front washer sprays fluid.")), dirs["chroma_dir"])
    index_chunks("p1", chunk_document(_doc("p1", "d2", "Mirror tinting uses electrochromic glass.")),
                 dirs["chroma_dir"])
    hits = search("p1", "front washer", k=2, chroma_dir=dirs["chroma_dir"])
    assert hits[0]["metadata"]["doc_id"] == "d1"
    assert hits[0]["score"] > hits[1]["score"]
    only = search("p1", "front washer", k=5, filters={"doc_id": "d2"}, chroma_dir=dirs["chroma_dir"])
    assert {h["metadata"]["doc_id"] for h in only} == {"d2"}


def test_project_isolation(dirs):
    index_chunks("p1", chunk_document(_doc("p1", "d1", "Secret wiper torque value is 5 Nm.")), dirs["chroma_dir"])
    index_chunks("p2", chunk_document(_doc("p2", "d2", "Chassis brake pressure is high.")), dirs["chroma_dir"])
    assert collection_name("p1") != collection_name("p2")
    hits = search("p2", "secret wiper torque", k=10, chroma_dir=dirs["chroma_dir"])
    assert hits and all(h["metadata"]["project_id"] == "p2" for h in hits)
    assert "Secret" not in " ".join(h["text"] for h in hits)
    assert search("nonexistent", "wiper", chroma_dir=dirs["chroma_dir"]) == []


def test_index_rejects_foreign_chunks(dirs):
    with pytest.raises(ValueError):
        index_chunks("p2", chunk_document(_doc("p1", "d1", "x y z.")), dirs["chroma_dir"])
