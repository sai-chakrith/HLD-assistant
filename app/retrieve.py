"""Vector store (one persistent Chroma collection per project; bge-small embeddings, hash fallback) and the
hybrid retrieval pipeline used by Q&A (``retrieve``)."""
from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol

import numpy as np

from app.config import settings

log = logging.getLogger(__name__)
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
HASH_DIM = 384


class Embedder(Protocol):
    name: str

    def encode(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    """Deterministic bag-of-words hashing embedder (offline fallback; NOT semantic like bge)."""

    name = "hash"

    def __init__(self, dim: int = HASH_DIM) -> None:
        self.dim = dim

    def _vec(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
        for t in toks + [a + "_" + b for a, b in zip(toks, toks[1:])]:
            h = int.from_bytes(hashlib.md5(t.encode()).digest()[:8], "little")
            v[h % self.dim] += 1.0 if (h >> 32) & 1 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t).tolist() for t in texts]


class BgeEmbedder:
    name = "bge"

    def __init__(self, model_name: str) -> None:
        from sentence_transformers import SentenceTransformer
        self.model = SentenceTransformer(model_name, device="cpu")

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True, batch_size=32,
                                 show_progress_bar=False).tolist()


_EMBEDDER: Embedder | None = None
_EMBEDDER_LOCK = threading.Lock()


def get_embedder() -> Embedder:
    """Lazily-loaded singleton. EMBEDDER=hash forces the fallback; load failure falls back with a warning."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    with _EMBEDDER_LOCK:
        if _EMBEDDER is None:
            if os.environ.get("EMBEDDER", "").lower() == "hash":
                log.warning("EMBEDDER=hash: using hashing embedder")
                _EMBEDDER = HashingEmbedder()
            else:
                try:
                    _EMBEDDER = BgeEmbedder(settings.embed_model)
                    log.info("Loaded embedding model %s", settings.embed_model)
                except Exception as e:
                    log.warning("FALLBACK: could not load %s (%s); using hashing embedder. "
                                "Re-ingest once the model is available.", settings.embed_model, e)
                    _EMBEDDER = HashingEmbedder()
    return _EMBEDDER


def embedder_status() -> dict:
    """{"kind": "bge"|"hash", "fallback": bool, "model": ...}. fallback=True means the real model is NOT in use
    (forced EMBEDDER=hash or load failure): scores are lexical, so similarity thresholds are meaningless."""
    e = get_embedder()
    return {"kind": e.name, "fallback": e.name != "bge", "model": settings.embed_model if e.name == "bge" else "hashing"}


def reset_embedder() -> None:
    global _EMBEDDER
    _EMBEDDER = None


# ---------------------------------------------------------------- chroma
_CLIENTS: dict[str, Any] = {}


def _client(chroma_dir: Path | str | None = None):
    import chromadb
    from chromadb.config import Settings as CS
    path = str(Path(chroma_dir) if chroma_dir else settings.chroma_dir)
    if path not in _CLIENTS:
        Path(path).mkdir(parents=True, exist_ok=True)
        _CLIENTS[path] = chromadb.PersistentClient(path=path, settings=CS(anonymized_telemetry=False))
    return _CLIENTS[path]


def collection_name(project_id: str, embedder_name: str | None = None) -> str:
    """Chroma-safe, collision-free collection name for a project (and embedder kind)."""
    emb = embedder_name or get_embedder().name
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", project_id).strip("_")[:40] or "p"
    digest = hashlib.sha1(project_id.encode()).hexdigest()[:6]
    return f"hld_{safe}_{digest}_{emb}"


def index_chunks(project_id: str, chunks: list[dict[str, Any]], chroma_dir: Path | str | None = None) -> int:
    """Upsert chunks into the project's collection. Chunk metadata must carry this project_id."""
    if not chunks:
        return 0
    for ch in chunks:
        if ch["metadata"].get("project_id") != project_id:
            raise ValueError("chunk project_id does not match target project")
    emb = get_embedder()
    col = _client(chroma_dir).get_or_create_collection(
        collection_name(project_id, emb.name), metadata={"hnsw:space": "cosine"})
    for i in range(0, len(chunks), 64):
        batch = chunks[i:i + 64]
        col.upsert(ids=[c["id"] for c in batch], documents=[c["text"] for c in batch],
                   embeddings=emb.encode([c["text"] for c in batch]),
                   metadatas=[c["metadata"] for c in batch])
    _invalidate(project_id)
    return len(chunks)


def delete_document(project_id: str, doc_id: str, chroma_dir: Path | str | None = None) -> None:
    try:
        col = _client(chroma_dir).get_collection(collection_name(project_id))
        col.delete(where={"doc_id": doc_id})
    except Exception:
        pass
    _invalidate(project_id)


def _where(project_id: str, filters: dict[str, Any] | None) -> dict[str, Any]:
    conds = [{"project_id": project_id}] + [{k: v} for k, v in (filters or {}).items() if k != "project_id"]
    return conds[0] if len(conds) == 1 else {"$and": conds}


def search(project_id: str, query: str, k: int = 5, filters: dict[str, Any] | None = None,
           chroma_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Top-k hits [{id, text, metadata, score}] from ONLY this project's collection (score = cosine similarity)."""
    emb = get_embedder()
    try:
        col = _client(chroma_dir).get_collection(collection_name(project_id, emb.name))
    except Exception:
        log.info("no collection for project %s (embedder %s)", project_id, emb.name)
        return []
    n = col.count()
    if n == 0:
        return []
    res = col.query(query_embeddings=emb.encode([QUERY_PREFIX + query]), n_results=min(k, n),
                    where=_where(project_id, filters), include=["documents", "metadatas", "distances"])
    return [{"id": i, "text": d, "metadata": m, "score": float(1.0 - dist)}
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])]


# ====================================================================== hybrid retrieval pipeline
# dense (bge) + BM25 + exact identifiers, fused with reciprocal rank fusion, then version policy, optional
# cross-encoder rerank, inventory evidence and section expansion. Every stage is switchable (RetrievalConfig) so
# it can be ablated; RetrievalConfig.dense_only() reproduces the original dense top-k behaviour exactly.
# hit["score"] stays the dense cosine similarity of that chunk, so SIMILARITY_THRESHOLD keeps its meaning.


@dataclass(frozen=True)
class RetrievalConfig:
    hybrid: bool = True                # BM25 + exact identifiers fused with dense via RRF
    query_understanding: bool = True   # version filter / newest-release preference, identifier + component detection
    inventory: bool = True             # labelled evidence chunks from the extracted inventory
    expand: bool = True                # parent-section intro + neighbouring chunk, within the token budget
    rerank: bool = False               # cross-encoder over the fused top candidates
    candidates: int = 20
    context_tokens: int = 2800         # evidence budget inside num_ctx 4096 (system prompt + answer need the rest)
    rrf_k: int = 60

    @classmethod
    def from_settings(cls) -> "RetrievalConfig":
        return cls(hybrid=settings.retrieval_hybrid, query_understanding=settings.retrieval_query_understanding,
                   inventory=settings.retrieval_inventory, expand=settings.retrieval_expand,
                   rerank=settings.retrieval_rerank, candidates=settings.retrieval_candidates,
                   context_tokens=settings.retrieval_context_tokens)

    @classmethod
    def dense_only(cls) -> "RetrievalConfig":
        return cls(hybrid=False, query_understanding=False, inventory=False, expand=False, rerank=False)

    def is_dense_only(self) -> bool:
        return not (self.hybrid or self.query_understanding or self.inventory or self.expand or self.rerank)


# ---------------------------------------------------------------- tokenisation (identifier aware)
_STOP = frozenset("""a an and are as at be by can do does did for from has have how i if in into is it its of on or
that the their there these this those to was were what when where which who whom whose why will with within
""".split())
_WORD = re.compile(r"[A-Za-z0-9_]+(?:[.\-][A-Za-z0-9_]+)*")
_PART = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def split_identifier(word: str) -> list[str]:
    """CamelCase / snake_case / hyphen / digit boundaries -> lower-case parts ('CmdWiperSpeed' -> cmd wiper speed)."""
    return [p.lower() for piece in re.split(r"[_\-.]+", word) for p in _PART.findall(piece)]


def is_identifier(word: str) -> bool:
    """CamelCase with an inner capital, snake_case, or letters mixed with digits (not a bare version like R3)."""
    if len(word) < 4 or not re.search(r"[A-Za-z]", word):
        return False
    if re.fullmatch(r"[RrVv]\d+(?:[.\-]\d+)*", word):
        return False
    return bool(re.search(r"[a-z][A-Z]|[A-Z]{2,}[a-z]", word) or "_" in word.strip("_")
                or (re.search(r"\d", word) and re.search(r"[A-Za-z]{2,}", word)))


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t


def tokenize(text: str) -> list[str]:
    """BM25 tokens: compounds are kept whole (exact match, lower-cased) AND split into parts."""
    out: list[str] = []
    for w in _WORD.findall(text or ""):
        parts = split_identifier(w)
        if len(parts) > 1:
            out.append(w.lower())
            out += [_stem(p) for p in parts if len(p) > 1 and p not in _STOP]
        else:
            lw = w.lower()
            if lw not in _STOP and (len(lw) > 1 or lw.isdigit()):
                out.append(_stem(lw))
    return out


def identifiers_in(text: str) -> list[str]:
    seen: list[str] = []
    for w in _WORD.findall(text or ""):
        w = w.strip("._-")
        if is_identifier(w) and w not in seen:
            seen.append(w)
    return seen


def contains_identifier(text: str, ident: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])", text or "", re.I) is not None


# ---------------------------------------------------------------- BM25 over the project's chunks
class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.dl = [len(d) for d in docs]
        self.avgdl = (sum(self.dl) / len(docs)) if docs else 1.0
        df: Counter = Counter()
        self.post: dict[str, list[int]] = {}
        for i, tf in enumerate(self.tf):
            for t in tf:
                df[t] += 1
                self.post.setdefault(t, []).append(i)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str], allowed: set[int] | None = None) -> dict[int, float]:
        out: dict[int, float] = {}
        for t in set(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i in self.post[t]:
                if allowed is not None and i not in allowed:
                    continue
                f = self.tf[i][t]
                out[i] = out.get(i, 0.0) + idf * f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * self.dl[i] / self.avgdl))
        return out


@dataclass
class Corpus:
    ids: list[str]
    texts: list[str]
    metas: list[dict]
    bm25: BM25
    pos: dict[str, int]


_CORPORA: dict[tuple, Corpus] = {}


def _invalidate(project_id: str) -> None:
    for key in [k for k in _CORPORA if k[1] == project_id]:
        _CORPORA.pop(key, None)


def load_corpus(project_id: str, chroma_dir: Path | str | None = None) -> Corpus | None:
    """All chunks of ONE project's collection (cached until the chunk count changes or the project is re-indexed)."""
    emb = get_embedder()
    try:
        col = _client(chroma_dir).get_collection(collection_name(project_id, emb.name))
    except Exception:
        return None
    n = col.count()
    key = (str(chroma_dir or settings.chroma_dir), project_id, emb.name, n)
    if key not in _CORPORA:
        _invalidate(project_id)
        got = col.get(where={"project_id": project_id}, include=["documents", "metadatas"])
        ids, texts, metas = got["ids"], got["documents"], got["metadatas"]
        _CORPORA[key] = Corpus(ids, texts, metas, BM25([tokenize(t) for t in texts]),
                               {i: n for n, i in enumerate(ids)})
    return _CORPORA[key]


def rrf(rankings: list[list[str]], k: int = 60, weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Reciprocal rank fusion: sum_l w_l / (k + rank_l(d)), rank starting at 1. Ties keep first-seen order."""
    score: dict[str, float] = {}
    for li, ranking in enumerate(rankings):
        w = weights[li] if weights else 1.0
        for r, doc in enumerate(ranking, 1):
            score[doc] = score.get(doc, 0.0) + w / (k + r)
    return sorted(score.items(), key=lambda x: -x[1])


# ---------------------------------------------------------------- query understanding (deterministic)
_VER_R = re.compile(r"\bR\s?(\d+(?:[.\-]\d+)+)\b", re.I)
_VER_WORD = re.compile(r"\b(?:release|version|rev(?:ision)?|v)\s?(\d+(?:\.\d+)*)\b", re.I)
_LATEST = re.compile(r"\b(latest|newest|most recent|current release|newer release|new release|recent release)\b",
                     re.I)
_OLDER = re.compile(r"\b(older|oldest|previous|earlier|old release|first release|original release)\b", re.I)
_CHANGED = re.compile(r"\b(chang\w*|differ\w*|compar\w*|added|removed|new in)\b", re.I)
_LIST = re.compile(r"\b(ports?|interfaces?|signals?|data elements?)\b", re.I)
_WHO = re.compile(r"\b(who|which (?:component|swc|module|software component)s?)\b.*\b(provid\w*|send\w*|receiv\w*|"
                  r"requir\w*|uses?|consum\w*|produc\w*|drives?)\b", re.I)
_DEF = re.compile(r"\b(what is|what are|what does|meaning|mean|define|definition|purpose|responsib\w*)\b", re.I)
_CHANGE_HISTORY = re.compile(r"change\s+history|change\s+description", re.I)
_HISTORY_Q = re.compile(r"\b(change|revision|document) history\b", re.I)
INVENTORY_INTENT = re.compile(r"\b(ports?|interfaces?|signals?|data elements?|provid\w*|requir\w*|send\w*|receiv\w*|"
                              r"connect\w*|depend\w*)\b", re.I)


def version_tuple(v: str | None) -> tuple[int, ...]:
    t = [int(x) for x in re.findall(r"\d+", v or "")]
    while len(t) > 1 and t[-1] == 0:
        t.pop()
    return tuple(t)


def version_matches(query_v: str, doc_v: str) -> bool:
    q, d = version_tuple(query_v), version_tuple(doc_v)
    return bool(q) and d[:len(q)] == q


def doc_family(title: str | None, doc_id: str) -> str:
    """Releases of the same document share a family: title (or doc_id) with version tokens/parentheses removed."""
    base = title or doc_id
    base = re.sub(r"\([^)]*\)", " ", base)
    base = re.sub(r"(?i)(?<![a-z])(?:r|v|version|release)?\s?\d+(?:[.\-]\d+)*", " ", base)
    return re.sub(r"[^a-z]+", "", base.lower()) or doc_id


@dataclass
class QueryInfo:
    question: str
    identifiers: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)  # normalised names (longest first)
    versions: list[str] = field(default_factory=list)    # explicit versions named in the question
    version_pref: str | None = None                      # "latest" | "older" | None
    qtype: str = "other"                                 # list_ports | who_provides | what_changed | definition | other
    doc_ids: list[str] = field(default_factory=list)     # documents matching the named versions


def detect_versions(question: str) -> tuple[list[str], str | None]:
    """(explicit versions, preference). 'R3.1.5', 'R25-11', 'version 1.0', 'v2'; 'latest'/'older' keywords."""
    vs = [m.group(0).replace(" ", "") for m in _VER_R.finditer(question)]
    vs += [m.group(1) for m in _VER_WORD.finditer(question) if not any(m.group(1) in v for v in vs)]
    pref = "latest" if _LATEST.search(question) else "older" if _OLDER.search(question) else None
    return vs, pref


def question_type(question: str) -> str:
    vs, _ = detect_versions(question)
    if _CHANGED.search(question) and (len(vs) >= 2 or re.search(r"\b(between|releases|versions)\b", question, re.I)
                                      or (len(vs) == 1 and re.search(r"\b(new|changed?|changes) in\b", question, re.I))):
        return "what_changed"
    if _WHO.search(question):
        return "who_provides"
    if _LIST.search(question):
        return "list_ports"
    if _DEF.search(question):
        return "definition"
    return "other"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


_SHORT_NAME = re.compile(r"[A-Z][A-Z0-9]{2}")  # ACC, EPB, HMI: 3-character all-caps component names


def match_components(question: str, names: Iterable[str] | dict[str, str]) -> list[str]:
    """Normalised component names found in the question, greedy longest-first, non-overlapping (min 4 chars).
    With a {normalised: display} dict, 3-character all-caps names (ACC) also match, but only as a whole word written
    in capitals in the question ("ACC", not "acc" or "access")."""
    qn = _norm(question)
    found: list[str] = []
    for n in sorted({x for x in names if len(x) >= 4}, key=len, reverse=True):
        i = qn.find(n)
        if i >= 0:
            found.append(n)
            qn = qn[:i] + "|" + qn[i + len(n):]
    if isinstance(names, dict):
        for n, display in names.items():
            if (len(n) < 4 and _SHORT_NAME.fullmatch(display or "") and not any(n in f for f in found)
                    and re.search(rf"(?<![A-Za-z0-9]){re.escape(display)}(?![A-Za-z0-9])", question)):
                found.append(n)
    return found


def analyze_query(question: str, docs: list[dict] | None = None, component_names: Iterable[str] = ()) -> QueryInfo:
    vs, pref = detect_versions(question)
    info = QueryInfo(question=question, identifiers=identifiers_in(question), versions=vs, version_pref=pref,
                     qtype=question_type(question), components=match_components(question, component_names))
    if vs and docs:
        info.doc_ids = [d["doc_id"] for d in docs if any(version_matches(v, d.get("version") or "") for v in vs)]
    return info


# ---------------------------------------------------------------- documents / releases
def project_docs(project_id: str, conn=None, corpus: Corpus | None = None) -> list[dict]:
    """[{doc_id, version, title, family}] from the documents table, falling back to chunk metadata."""
    docs: dict[str, dict] = {}
    if conn is not None:
        try:
            for r in conn.execute("SELECT doc_id, version, title FROM documents WHERE project_id=?", (project_id,)):
                docs[r[0]] = {"doc_id": r[0], "version": r[1], "title": r[2]}
        except Exception:
            pass
    if corpus is not None:
        for m in corpus.metas:
            docs.setdefault(m["doc_id"], {"doc_id": m["doc_id"], "version": m.get("version"), "title": None})
    for d in docs.values():
        d["family"] = doc_family(d["title"], d["doc_id"])
    return list(docs.values())


def _families(docs: list[dict]) -> dict[str, list[dict]]:
    fam: dict[str, list[dict]] = {}
    for d in docs:
        fam.setdefault(d["family"], []).append(d)
    for v in fam.values():
        v.sort(key=lambda d: version_tuple(d.get("version")), reverse=True)  # newest first
    return fam


# ---------------------------------------------------------------- optional cross-encoder
_RERANKER: Any = None


def get_reranker():
    global _RERANKER
    if _RERANKER is None:
        from sentence_transformers import CrossEncoder
        _RERANKER = CrossEncoder(settings.rerank_model, device="cpu")
    return _RERANKER


# ---------------------------------------------------------------- result
@dataclass
class Retrieval:
    hits: list[dict]
    top_score: float               # best dense cosine of the retrieved text chunks (threshold input)
    on_topic: bool                 # an inventory component named in the question matched
    query: QueryInfo
    release_note: str = ""
    timings: dict = field(default_factory=dict)
    exact_identifier: bool = False  # (hybrid) an identifier from the question occurs verbatim in a text hit
    coverage: float = 1.0           # share of the question's content terms that occur in the project's chunks
    missing_terms: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)  # "doc_id (version)" of the documents in scope


# question words that say nothing about the topic (kept out of the coverage test)
_GENERIC_Q = frozenset("""according also any assigned between describe describes described document documents
does exist explain given kind give list main mean means name named please provide recommended shown specified
state stated tell type use used using value way""".split())


# question verbs that are never reported as "terms not found" (the coverage score itself is unchanged)
_QUESTION_VERBS = frozenset("""send sends sent sending receive receives received receiving have having provide
provides provided providing require requires required requiring""".split())


def reportable_missing(terms: list[str]) -> list[str]:
    """Missing terms worth showing to the user: question verbs (send, receive, have, provide, require) removed."""
    return [t for t in terms if t not in _QUESTION_VERBS and _stem(t) not in {_stem(v) for v in _QUESTION_VERBS}]


def term_coverage(question: str, corpus: Corpus) -> tuple[float, list[str]]:
    """(share of the question's content terms found in the project's chunks, terms not found). Terms are BM25
    tokens minus generic question words; version tokens (R3.1.5) are ignored."""
    terms = [t for t in dict.fromkeys(tokenize(question)) if t not in _GENERIC_Q and not _VER_R.fullmatch(t)
             and not t.isdigit()]
    if not terms:
        return 1.0, []
    missing = [t for t in terms if t not in corpus.bm25.idf]
    return 1 - len(missing) / len(terms), missing


def _hit(corpus: Corpus, cid: str, score: float, **extra) -> dict:
    i = corpus.pos[cid]
    return {"id": cid, "text": corpus.texts[i], "metadata": corpus.metas[i], "score": score, **extra}


def _dense_scores(project_id: str, ids: list[str], qvec: list[float], chroma_dir=None) -> dict[str, float]:
    """Cosine similarity of the query to chunks found lexically (embeddings are unit-normalised)."""
    if not ids:
        return {}
    col = _client(chroma_dir).get_collection(collection_name(project_id))
    got = col.get(ids=ids, include=["embeddings"])
    q = np.asarray(qvec, dtype=np.float32)
    return {i: float(np.dot(np.asarray(e, dtype=np.float32), q)) for i, e in zip(got["ids"], got["embeddings"])}


def _dense(project_id: str, qvec: list[float], k: int, doc_ids: list[str] | None, chroma_dir) -> list[dict]:
    try:
        col = _client(chroma_dir).get_collection(collection_name(project_id))
    except Exception:
        return []
    n = col.count()
    if n == 0:
        return []
    filters = {"doc_id": {"$in": doc_ids}} if doc_ids else None
    res = col.query(query_embeddings=[qvec], n_results=min(k, n), where=_where(project_id, filters),
                    include=["documents", "metadatas", "distances"])
    return [{"id": i, "text": d, "metadata": m, "score": float(1.0 - dist)}
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0])]


def _jaccard(a: str, b: str) -> float:
    x, y = set(tokenize(a)), set(tokenize(b))
    return len(x & y) / len(x | y) if x and y else 0.0


def _equivalent(corpus: Corpus, hit: dict, target_doc: str) -> str | None:
    """Chunk id in target_doc with the same section title and the most similar text (Jaccard >= 0.5)."""
    m = hit["metadata"]
    best, best_j = None, 0.5
    for cid, meta, text in zip(corpus.ids, corpus.metas, corpus.texts):
        if meta["doc_id"] != target_doc or meta.get("section_title") != m.get("section_title") \
                or meta.get("chunk_type") != m.get("chunk_type"):
            continue
        j = _jaccard(text, hit["text"])
        if j >= best_j:
            best, best_j = cid, j
    return best


def apply_version_policy(order: list[dict], corpus: Corpus, docs: list[dict], info: QueryInfo) -> list[dict]:
    """No version named -> each older-release chunk is replaced by the equivalent chunk of the newest release of its
    document (kept when the newest release has no equivalent). 'older' -> the oldest release instead.
    'what changed' questions keep both releases and interleave them."""
    fams = _families(docs)
    fam_of = {d["doc_id"]: d["family"] for d in docs}
    if info.qtype == "what_changed":
        by_doc: dict[str, list[dict]] = {}
        for h in order:
            by_doc.setdefault(h["metadata"]["doc_id"], []).append(h)
        out: list[dict] = []
        while any(by_doc.values()):
            for lst in by_doc.values():
                if lst:
                    out.append(lst.pop(0))
        return out
    if info.doc_ids:
        return order  # a named version already filtered the candidates
    out, seen = [], set()
    for h in order:
        fam = fams.get(fam_of.get(h["metadata"]["doc_id"], ""), [])
        if len(fam) > 1:
            target = (fam[-1] if info.version_pref == "older" else fam[0])["doc_id"]
            if h["metadata"]["doc_id"] != target:
                eq = _equivalent(corpus, h, target)
                if eq:
                    h = _hit(corpus, eq, h["score"], replaced=h["id"], replaced_text=h["text"])
        if h["id"] not in seen:
            seen.add(h["id"])
            out.append(h)
    return out


def release_note(hits: list[dict], docs: list[dict], info: QueryInfo) -> str:
    """'Answered from release R3.2.3 (bodycomfort_r3.2.3): the newest release of the available releases ...' when
    the evidence comes from a document that exists in several releases in this project ('' otherwise)."""
    fams = _families(docs)
    fam_of = {d["doc_id"]: d["family"] for d in docs}
    used, why, vers = [], "", ""
    for doc_id in dict.fromkeys(h["metadata"].get("doc_id") for h in hits):
        fam = fams.get(fam_of.get(doc_id, ""), [])
        if len(fam) < 2:
            continue
        d = next(x for x in fam if x["doc_id"] == doc_id)
        used.append(f"{d['version']} ({doc_id})")
        vers = ", ".join(x["version"] for x in reversed(fam))
        why = ("the release named in the question" if info.doc_ids else
               "releases compared" if info.qtype == "what_changed" else
               "the oldest release" if info.version_pref == "older" else "the newest release")
    if not used:
        return ""
    return f"Answered from release {', '.join(used)}: {why} (available releases: {vers})."


# ---------------------------------------------------------------- inventory evidence
def inventory_names(inv: dict) -> dict[str, str]:
    """normalised -> display name, for components and port/signal owners ('Inst::Type' gives both parts)."""
    names: dict[str, str] = {}

    def add(n: str) -> None:
        for part in [n] + (n.split("::") if "::" in n else []):
            part = part.strip()
            if part and not part.endswith("..."):
                names.setdefault(_norm(part), part)
    for c in inv.get("components", []):
        add(c["name"])
    for p in inv.get("ports", []):
        add(p.get("component") or "")
    for s in inv.get("signals", []):
        add(s.get("sender") or "")
        for r in s.get("receivers") or []:
            add(r)
    names.pop("", None)
    return names


def _owner_is(owner: str, comp_norm: str) -> bool:
    return any(_norm(p) == comp_norm for p in [owner] + owner.split("::"))


def _port_line(p: dict, owner: bool = False) -> str:
    return (f"port {p['name']}" + (f" of {p.get('component')}" if owner else "")
            + f" (direction {p.get('direction') or 'unknown'}"
            + (f", interface {p['interface']}" if p.get("interface") else "")
            + f", page {p.get('page')}, extraction method {p.get('extraction_method')})")


def _signal_line(s: dict) -> str:
    return (f"signal {s['name']} (sender {s.get('sender') or 'unknown'}, receivers "
            f"{', '.join(s.get('receivers') or []) or 'unknown'}, page {s.get('page')}, "
            f"extraction method {s.get('extraction_method')})")


def _sec_id(section: str | None) -> str:
    return (section or "").split(" ", 1)[0]


def inventory_evidence(project_id: str, info: QueryInfo, conn, docs: list[dict], max_docs: int = 2,
                       inv: dict | None = None) -> list[dict]:
    """Labelled evidence chunks built from the extracted inventory (cited as "from extracted inventory").

    * component named + ports/interfaces/signals/connection wording -> its ports and signals, per document release;
      if nothing was extracted, an explicit "possible extraction gap" chunk plus the ports of components described
      in its subsections (never a claim that the component has no ports);
    * an identifier naming a port or signal -> the inventory rows for that port/signal.
    Release choice follows the version policy (named version, else the newest release of each document)."""
    if inv is None:
        from app import extract
        inv = extract.get_inventory(project_id, conn=conn, include_rejected=False)
    known = {d["doc_id"] for d in docs}
    for row in (x for t in ("components", "ports", "interfaces", "signals") for x in inv.get(t, [])):
        if row["doc_id"] not in known:  # releases also known from the inventory rows themselves
            known.add(row["doc_id"])
            docs = docs + [{"doc_id": row["doc_id"], "version": row.get("version"), "title": None,
                            "family": doc_family(None, row["doc_id"])}]
    fams = _families(docs)
    fam_of = {d["doc_id"]: d["family"] for d in docs}

    def pick(doc_ids: list[str]) -> list[str]:
        if info.doc_ids:
            return [d for d in doc_ids if d in info.doc_ids]
        if info.qtype == "what_changed":
            return doc_ids
        keep: list[str] = []
        for d in doc_ids:
            fam = [x["doc_id"] for x in fams.get(fam_of.get(d, ""), []) if x["doc_id"] in doc_ids]
            best = (fam[-1] if info.version_pref == "older" else fam[0]) if fam else d
            if best not in keep:
                keep.append(best)
        return keep

    version = {d["doc_id"]: d.get("version") for d in docs}
    out: list[dict] = []

    def chunk(cid: str, doc_id: str, title: str, lines: list[str], pages: list, section: str | None,
              rows: dict | None = None) -> None:
        pages = [p for p in pages if isinstance(p, int)]
        out.append({"id": cid, "score": 1.0, "rows": rows or {}, "text": f"EXTRACTED INVENTORY {title} in {doc_id} "
                    f"(release {version.get(doc_id)}):\n" + "\n".join(f"- {x}" for x in lines),
                    "metadata": {"project_id": project_id, "doc_id": doc_id, "version": version.get(doc_id),
                                 "page_start": min(pages) if pages else None,
                                 "page_end": max(pages) if pages else None, "section_id": _sec_id(section),
                                 "section_title": "extracted inventory", "chunk_type": "inventory",
                                 "inventory": True}})

    if info.components and INVENTORY_INTENT.search(info.question):
        for comp_norm in info.components[:2]:
            owned = [p for p in inv.get("ports", []) if _owner_is(p.get("component") or "", comp_norm)]
            sigs = [s for s in inv.get("signals", []) if _norm(s.get("sender") or "") == comp_norm
                    or comp_norm in [_norm(r) for r in s.get("receivers") or []]]
            comps = [c for c in inv.get("components", []) if _norm(c["name"]) == comp_norm]
            display = (comps[0]["name"] if comps else
                       next((part.strip() for p in owned for part in p["component"].split("::")
                             if _norm(part) == comp_norm), None) or
                       next((s["sender"] for s in sigs if _norm(s.get("sender") or "") == comp_norm), comp_norm))
            doc_ids = list(dict.fromkeys([x["doc_id"] for x in comps + owned + sigs]))
            for doc_id in pick(doc_ids)[:max_docs]:
                p_d = [p for p in owned if p["doc_id"] == doc_id]
                s_d = [s for s in sigs if s["doc_id"] == doc_id]
                c_d = [c for c in comps if c["doc_id"] == doc_id]
                lines = [_port_line(p) for p in p_d][:15] + [_signal_line(s) for s in s_d][:10]
                section = (c_d or p_d or s_d)[0].get("section")
                if lines:
                    chunk(f"inv:{doc_id}:{comp_norm}", doc_id, f"for component {display}", lines,
                          [x.get("page") for x in c_d + p_d + s_d], section,
                          {"kind": "component", "component": display, "norm": comp_norm, "ports": p_d,
                           "signals": s_d})  # all rows for list answers; only the prompt text lines are capped
                    continue
                # extraction gap: say so, and show the ports of components described in its subsections
                sid = _sec_id(section)
                rel = [p for p in inv.get("ports", []) if p["doc_id"] == doc_id and sid
                       and _sec_id(p.get("section")).startswith(sid + ".")]
                lines = [f"No ports or signals were extracted for {display} (possible extraction gap; this does not "
                         f"mean that {display} has no ports)."]
                children = [x for c in c_d for x in c.get("children") or []]
                if children:
                    lines.append(f"{display} is a software composition containing: {', '.join(children)}.")
                if rel:
                    lines.append(f"Ports of components described in subsections of section {sid}:")
                    lines += [_port_line(p, owner=True) for p in rel][:12]
                chunk(f"inv:{doc_id}:{comp_norm}", doc_id, f"for component {display}", lines,
                      [c.get("page") for c in c_d] + [p.get("page") for p in rel], section,
                      {"kind": "gap", "component": display, "norm": comp_norm, "children": children,
                       "section": section or ""})
                out[-1]["metadata"]["extraction_gap"] = display
    for ident in info.identifiers:
        n = _norm(ident)
        if n in info.components[:2] and out:
            continue  # already covered as a component
        rows = [("p", p) for p in inv.get("ports", []) if p["norm_name"] == n] + \
               [("s", s) for s in inv.get("signals", []) if s["norm_name"] == n]
        for doc_id in pick(list(dict.fromkeys(r["doc_id"] for _, r in rows)))[:max_docs]:
            r_d = [(kind, r) for kind, r in rows if r["doc_id"] == doc_id]
            lines = [_port_line(r, owner=True) if kind == "p" else _signal_line(r) for kind, r in r_d][:12]
            chunk(f"inv:{doc_id}:{n}", doc_id, f"for {ident}", lines, [r.get("page") for _, r in r_d],
                  r_d[0][1].get("section"), {"kind": "identifier", "identifier": ident, "norm": n,
                                             "ports": [r for k, r in r_d if k == "p"],
                                             "signals": [r for k, r in r_d if k == "s"]})
    uniq: dict[str, dict] = {}
    for h in out:
        uniq.setdefault(h["id"], h)
    return list(uniq.values())[:max_docs]


# ---------------------------------------------------------------- section-aware expansion
def _chunk_key(cid: str) -> tuple[str, str, int] | None:
    parts = cid.rsplit("::", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        return None
    return parts[0], parts[1], int(parts[2])


def expand_sections(hits: list[dict], corpus: Corpus, budget: int, dense: dict[str, float] | None = None,
                    max_parent_tokens: int = 160, n_expand: int = 3) -> list[dict]:
    """For the top text hits add the parent section's opening text (truncated; subsections only) and the next chunk
    of the same section, deduplicated, as separate evidence chunks with their own pages (citations stay real),
    while the estimated total stays within budget tokens."""
    from app.chunk import est_tokens
    used = sum(est_tokens(h["text"]) for h in hits)
    have = {h["id"] for h in hits}
    extra: list[dict] = []
    for h in [x for x in hits if not x["metadata"].get("inventory")][:n_expand]:
        key = _chunk_key(h["id"])
        if not key:
            continue
        doc, sid, n = key
        cands = ([(f"{doc}::{sid.rsplit('.', 1)[0]}::0", "parent")] if "." in sid else []) + \
                [(f"{doc}::{sid}::{n + 1}", "neighbour")]
        for cid, kind in cands:
            if cid in have or cid not in corpus.pos:
                continue
            text = corpus.texts[corpus.pos[cid]]
            if kind == "parent":
                words = text.split()
                cap = int(max_parent_tokens / 1.3)
                text = " ".join(words[:cap]) + (" ..." if len(words) > cap else "")
            t = est_tokens(text)
            if used + t > budget:
                continue
            used += t
            have.add(cid)
            extra.append({"id": cid, "text": text, "metadata": {**corpus.metas[corpus.pos[cid]], "expansion": kind},
                          "score": (dense or {}).get(cid, 0.0)})
    return hits + extra


# ---------------------------------------------------------------- pipeline
def retrieve(project_id: str, question: str, k: int = 5, cfg: RetrievalConfig | None = None, conn=None,
             chroma_dir: Path | str | None = None) -> Retrieval:
    """Evidence for a question from ONLY this project's collection and inventory (stages: see notes above)."""
    import time
    cfg = cfg or RetrievalConfig.from_settings()
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    if cfg.is_dense_only():
        hits = search(project_id, question, k=k, chroma_dir=chroma_dir)
        return Retrieval(hits, hits[0]["score"] if hits else 0.0, False, QueryInfo(question),
                         timings={"total_ms": (time.perf_counter() - t0) * 1000})
    corpus = load_corpus(project_id, chroma_dir)
    if corpus is None or not corpus.ids:
        return Retrieval([], 0.0, False, QueryInfo(question))
    docs = project_docs(project_id, conn, corpus)
    inv: dict = {}
    if cfg.inventory and conn is not None:
        try:
            from app import extract
            inv = extract.get_inventory(project_id, conn=conn, include_rejected=False)
        except Exception as e:  # inventory is optional evidence
            log.info("inventory unavailable for %s: %s", project_id, e)
    names = inventory_names(inv) if inv else ()
    if cfg.query_understanding:
        info = analyze_query(question, docs, names)
    else:  # identifiers/components are still needed by BM25-exact and inventory stages
        info = QueryInfo(question, identifiers=identifiers_in(question), qtype=question_type(question),
                         components=match_components(question, names))
    scope = info.doc_ids or None
    qvec = get_embedder().encode([QUERY_PREFIX + question])[0]
    n_cand = max(cfg.candidates, k)
    dense = _dense(project_id, qvec, n_cand, scope, chroma_dir)
    timings["dense_ms"] = (time.perf_counter() - t0) * 1000
    dense_score = {h["id"]: h["score"] for h in dense}
    if cfg.hybrid:
        allowed = {i for i, m in enumerate(corpus.metas) if not scope or m["doc_id"] in scope}
        bm = sorted(corpus.bm25.scores(tokenize(question), allowed).items(), key=lambda x: -x[1])
        lists, weights = [[h["id"] for h in dense], [corpus.ids[i] for i, _ in bm[:n_cand]]], [1.0, 1.0]
        for ident in info.identifiers:
            exact = [corpus.ids[i] for i, _ in bm if contains_identifier(corpus.texts[i], ident)][:n_cand]
            if exact:
                lists.append(exact)
                weights.append(2.0)  # an exact identifier match outranks a paraphrase
        fused = [cid for cid, _ in rrf(lists, cfg.rrf_k, weights)][:n_cand]
        dense_score.update(_dense_scores(project_id, [c for c in fused if c not in dense_score], qvec, chroma_dir))
        order = [_hit(corpus, cid, dense_score.get(cid, 0.0)) for cid in fused]
        timings["lexical_ms"] = (time.perf_counter() - t0) * 1000 - timings["dense_ms"]
    else:
        order = [_hit(corpus, h["id"], h["score"]) for h in dense if h["id"] in corpus.pos]
    if cfg.query_understanding:
        if info.qtype == "what_changed" or _HISTORY_Q.search(question):
            # the change history table of each release in scope comes first
            cand_docs = {h["metadata"]["doc_id"] for h in order}  # only documents the question already hits
            hist = [cid for cid, m, t in zip(corpus.ids, corpus.metas, corpus.texts)
                    if m["doc_id"] in cand_docs and (not scope or m["doc_id"] in scope) and _CHANGE_HISTORY.search(t)]
            seen_docs: set[str] = set()
            first = []
            for cid in hist:
                d = corpus.metas[corpus.pos[cid]]["doc_id"]
                if d not in seen_docs:
                    seen_docs.add(d)
                    first.append(_hit(corpus, cid, dense_score.get(cid, 0.0)))
            order = first + [h for h in order if h["id"] not in {x["id"] for x in first}]
        order = apply_version_policy(order, corpus, docs, info)
        dense_score.update(_dense_scores(project_id, [h["id"] for h in order if h["id"] not in dense_score],
                                         qvec, chroma_dir))
        for h in order:
            h["score"] = dense_score.get(h["id"], h["score"])
    if cfg.rerank and order:
        t1 = time.perf_counter()
        head = order[:cfg.candidates]
        ce = get_reranker().predict([(question, h["text"]) for h in head], show_progress_bar=False)
        order = [h for _, h in sorted(zip(ce, head), key=lambda x: -x[0])] + order[cfg.candidates:]
        timings["rerank_ms"] = (time.perf_counter() - t1) * 1000
    inv_hits = inventory_evidence(project_id, info, conn, docs, inv=inv) if (cfg.inventory and inv) else []
    text_hits = order[:max(k - len(inv_hits), 3) if inv_hits else k]
    top = max((h["score"] for h in text_hits), default=0.0)
    hits = inv_hits + text_hits
    if cfg.expand:
        hits = expand_sections(hits, corpus, cfg.context_tokens, dense_score)
    note = release_note(hits, docs, info) if cfg.query_understanding else ""
    timings["total_ms"] = (time.perf_counter() - t0) * 1000
    exact = cfg.hybrid and any(contains_identifier(h["text"], i) for i in info.identifiers for h in text_hits)
    cov, missing = term_coverage(question, corpus)
    searched = [f"{d['doc_id']} ({d.get('version')})" for d in docs if not scope or d["doc_id"] in scope]
    return Retrieval(hits, top, bool(inv_hits and info.components), info, note, timings, exact, cov,
                     reportable_missing(missing), searched)
