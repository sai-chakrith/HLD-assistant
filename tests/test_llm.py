"""LLM provider: mocked HTTP for retry/error/cache logic (no Ollama needed) + real Ollama under @llm."""
import json

import pytest
import requests
from pydantic import BaseModel

from app import llm


class Item(BaseModel):
    name: str
    count: int


class FakeHttp:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, url, payload, timeout):
        self.calls.append((url, json.loads(json.dumps(payload))))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"response": r}


def prov(tmp_path, *responses):
    http = FakeHttp(*responses)
    return llm.OllamaProvider("http://x:1/", "qwen2.5:3b", db_path=tmp_path / "c.db",
                              trace_path=tmp_path / "t.jsonl", http_post=http), http


def traces(tmp_path):
    return [json.loads(x) for x in (tmp_path / "t.jsonl").read_text().splitlines()]


def test_null_provider_raises():
    with pytest.raises(llm.LLMNotConfigured):
        llm.NullProvider().generate("hi")
    with pytest.raises(llm.LLMNotConfigured):
        llm.NullProvider().complete("hi")


def test_get_provider_selection():
    assert isinstance(llm.get_provider("none"), llm.NullProvider)
    assert isinstance(llm.get_provider("OLLAMA"), llm.OllamaProvider)
    assert not hasattr(llm, "GroqProvider")
    with pytest.raises(ValueError, match="cloud providers disabled"):
        llm.get_provider("groq")
    with pytest.raises(ValueError):
        llm.get_provider("bogus")


def test_default_provider_setting():
    from app.config import settings
    assert settings.allowed_models == ("qwen2.5:3b", "qwen3:4b-instruct")
    assert llm.OllamaProvider("http://x:1/", "m").base_url == "http://x:1"


def test_validate_model():
    assert llm.validate_model("qwen2.5:3b") == "qwen2.5:3b"
    with pytest.raises(llm.ModelNotAllowed):
        llm.validate_model("gpt-4")


def test_text_payload_and_cache(tmp_path):
    p, http = prov(tmp_path, "hello")
    assert p.generate("hi", "sys", purpose="t", chunk_ids=["a"]) == "hello"
    url, payload = http.calls[0]
    assert url == "http://x:1/api/generate" and payload["stream"] is False and "format" not in payload
    assert payload["options"] == {"temperature": 0.1, "num_ctx": 4096} and payload["system"] == "sys"
    assert p.generate("hi", "sys") == "hello" and len(http.calls) == 1  # cache hit, no HTTP
    assert p.complete("hi", "sys") == "hello"
    t = traces(tmp_path)
    assert [x["cached"] for x in t] == [False, True, True] and t[0]["chunk_ids"] == ["a"] and t[0]["ok"]
    assert {"ts", "model", "purpose", "prompt_hash", "latency_ms"} <= set(t[0])


def test_cache_key_depends_on_model_and_schema():
    k = llm.cache_key("a", "s", "p")
    assert k != llm.cache_key("b", "s", "p") and k != llm.cache_key("a", "s", "p", Item)
    assert k == llm.cache_key("a", "s", "p")


def test_per_call_model(tmp_path):
    p, http = prov(tmp_path, "x")
    p.generate("q", model="qwen2.5:3b")
    assert http.calls[0][1]["model"] == "qwen2.5:3b"


def test_schema_retry_with_validation_error(tmp_path):
    p, http = prov(tmp_path, '{"name": "a"}', 'not json', '{"name": "a", "count": 3}')
    out = p.generate("extract", schema=Item)
    assert out == {"name": "a", "count": 3} and len(http.calls) == 3
    assert http.calls[0][1]["format"]["properties"]["count"]
    assert "invalid" in http.calls[1][1]["prompt"] and "count" in http.calls[1][1]["prompt"]
    assert p.generate("extract", schema=Item) == out and len(http.calls) == 3  # cached parsed result


def test_schema_fails_after_retries(tmp_path):
    p, http = prov(tmp_path, "{}", "{}", "{}")
    with pytest.raises(ValueError):
        p.generate("x", schema=Item)
    assert len(http.calls) == 3
    assert traces(tmp_path)[-1]["ok"] is False
    assert llm.cache_get(llm.cache_key("qwen2.5:3b", "", "x", Item), tmp_path / "c.db") is None


@pytest.mark.parametrize("exc", [requests.ConnectionError("refused"), requests.Timeout("slow")])
def test_connection_errors_map_to_not_configured(tmp_path, exc):
    p, _ = prov(tmp_path, exc)
    with pytest.raises(llm.LLMNotConfigured):
        p.generate("hi")
    assert traces(tmp_path)[-1]["ok"] is False


def test_ollama_status_unreachable():
    s = llm.ollama_status("http://127.0.0.1:9", timeout=1)
    assert s["reachable"] is False and s["models"] == []


@pytest.mark.llm
def test_ollama_generates_text(tmp_path):
    p = llm.OllamaProvider(db_path=tmp_path / "c.db", trace_path=tmp_path / "t.jsonl")
    out = p.generate("Reply with the single word OK.")
    assert isinstance(out, str) and out.strip()


@pytest.mark.llm
def test_ollama_json_schema_and_cache_hit(tmp_path):
    p = llm.OllamaProvider(db_path=tmp_path / "c.db", trace_path=tmp_path / "t.jsonl")
    out = p.generate("Extract: 'There are 3 apples.' name=fruit, count=number.", schema=Item, purpose="t")
    assert Item.model_validate(out)
    p.generate("Extract: 'There are 3 apples.' name=fruit, count=number.", schema=Item, purpose="t")
    t = traces(tmp_path)
    assert t[0]["cached"] is False and t[1]["cached"] is True and t[1]["latency_ms"] < 200


@pytest.mark.llm
def test_ollama_status_lists_models():
    s = llm.ollama_status()
    assert s["reachable"] and any(m.startswith("qwen2.5:3b") for m in s["models"])


# ---------------------------------------------------------------- qwen3 thinking + timed (cache-read off) runs
def test_qwen3_payload_disables_thinking_and_strips_think_blocks(tmp_path):
    p, http = prov(tmp_path, '<think>reasoning...</think>\n{"name": "a", "count": 1}', "<think>x</think> The answer [C1].")
    assert p.generate("q", schema=Item, model="qwen3:4b-instruct") == {"name": "a", "count": 1}
    assert p.generate("q2", model="qwen3:4b-instruct") == "The answer [C1]."
    assert http.calls[0][1]["think"] is False


def test_every_payload_sends_think_false(tmp_path):
    p, http = prov(tmp_path, "x")
    p.generate("q", model="qwen2.5:3b")
    assert http.calls[0][1]["think"] is False


def test_cache_read_off_regenerates_but_still_stores(tmp_path, monkeypatch):
    p, http = prov(tmp_path, "first", "second")
    p.generate("q")
    monkeypatch.setenv("LLM_CACHE_READ", "0")
    assert p.generate("q") == "second" and len(http.calls) == 2
    monkeypatch.delenv("LLM_CACHE_READ")
    assert p.generate("q") == "second" and len(http.calls) == 2  # the regenerated answer was stored


def test_keep_alive_only_for_the_qa_model(tmp_path):
    p, http = prov(tmp_path, "a", "b")
    p.generate("q", model="qwen3:4b-instruct")
    p.generate("q", model="qwen2.5:3b")
    assert http.calls[0][1]["keep_alive"] == "30m" and "keep_alive" not in http.calls[1][1]


def test_prose_extraction_defaults_to_the_extract_model():
    from app import extract
    seen = []

    class P:
        def generate(self, prompt, system="", schema=None, model=None, purpose="", chunk_ids=None):
            seen.append(model)
            return {"components": [], "ports": [], "interfaces": [], "signals": []}

    sec = {"section_id": "4.1", "title": "Wiper", "page_start": 1, "text": "The WiperManager controls the wiper. " * 20}
    extract.extract_from_prose(sec, "d", "1", provider=P())
    assert seen and set(seen) == {"qwen2.5:3b"}



# ---------------------------------------------------------------- purpose routing, swaps, warm-up
@pytest.mark.parametrize("purpose,model", [("qa", "qwen3:4b-instruct"), ("qa_desc", "qwen3:4b-instruct"),
                                           ("component_report", "qwen3:4b-instruct"),
                                           ("doc_summary_map", "qwen3:4b-instruct"), ("summary", "qwen3:4b-instruct"),
                                           ("report", "qwen3:4b-instruct"), ("extract_prose", "qwen2.5:3b"),
                                           ("extract", "qwen2.5:3b"), ("upload", "qwen2.5:3b"),
                                           ("other", "qwen2.5:3b")])
def test_model_routed_by_purpose(tmp_path, purpose, model):
    p, http = prov(tmp_path, "x")
    p.generate(f"q-{purpose}", purpose=purpose)
    assert http.calls[0][1]["model"] == model


def test_override_must_be_allowed(tmp_path):
    p, http = prov(tmp_path, "x")
    assert llm.model_for("extract_prose", "qwen3:4b-instruct") == "qwen3:4b-instruct"  # allowed override wins
    with pytest.raises(llm.ModelNotAllowed):
        p.generate("q", purpose="qa", model="llama3.2:3b")
    assert http.calls == []


class LoadHttp(FakeHttp):
    def __call__(self, url, payload, timeout):
        self.calls.append((url, json.loads(json.dumps(payload))))
        return {"response": "ok", "load_duration": self.responses.pop(0)}


def test_model_swap_is_traced_with_duration(tmp_path):
    http = LoadHttp(9_000_000_000, 20_000_000, 7_500_000_000)  # load 9 s, warm 20 ms, load 7.5 s
    p = llm.OllamaProvider("http://x:1/", "qwen2.5:3b", db_path=tmp_path / "c.db", trace_path=tmp_path / "t.jsonl",
                           http_post=http)
    p.generate("a", purpose="qa")
    p.generate("b", purpose="qa")
    p.generate("c", purpose="extract_prose")
    swaps = [t for t in traces(tmp_path) if t["purpose"] == "model_swap"]
    assert [(s["from_model"], s["model"], s["latency_ms"]) for s in swaps][-1] == ("qwen3:4b-instruct", "qwen2.5:3b", 7500)
    assert len(swaps) == 2


def test_warm_is_one_token_on_the_qa_model_with_keep_alive(tmp_path):
    http = LoadHttp(8_000_000_000)
    p = llm.OllamaProvider("http://x:1/", db_path=tmp_path / "c.db", trace_path=tmp_path / "t.jsonl", http_post=http)
    out = p.warm()
    body = http.calls[0][1]
    assert body["model"] == "qwen3:4b-instruct" and body["options"]["num_predict"] == 1
    assert body["keep_alive"] == "30m" and body["think"] is False
    assert out["load_ms"] == 8000 and any(t["purpose"] == "warmup" for t in traces(tmp_path))
