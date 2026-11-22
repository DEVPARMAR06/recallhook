from types import SimpleNamespace

from recallhook.config import Config
from recallhook.memory import tune_ollama


def fake_mem0(chat_impl):
    calls = {"chat": [], "embed": []}

    def chat(**kw):
        calls["chat"].append(kw)
        return chat_impl(kw)

    def embed(**kw):
        calls["embed"].append(kw)
        return "vec"

    m = SimpleNamespace(llm=SimpleNamespace(client=SimpleNamespace(chat=chat)),
                        embedding_model=SimpleNamespace(client=SimpleNamespace(embed=embed)))
    return m, calls


def test_injects_ctx_keepalive_think_and_cpu_embeddings():
    m, calls = fake_mem0(lambda kw: "ok")
    tune_ollama(m, Config(num_ctx=4096, keep_alive="30m"))
    m.llm.client.chat(model="x", messages=[], options={"temperature": 0})
    m.embedding_model.client.embed(model="e", input="q")
    c = calls["chat"][0]
    assert c["options"] == {"temperature": 0, "num_ctx": 4096}
    assert c["keep_alive"] == "30m" and c["think"] is False
    e = calls["embed"][0]
    assert e["options"] == {"num_gpu": 0} and e["keep_alive"] == "30m"


def test_model_without_thinking_support_falls_back_once():
    def impl(kw):
        if "think" in kw:
            raise RuntimeError('"x" does not support thinking')
        return "ok"

    m, calls = fake_mem0(impl)
    tune_ollama(m, Config())
    assert m.llm.client.chat(model="x", messages=[]) == "ok"
    m.llm.client.chat(model="x", messages=[])
    assert ["think" in c for c in calls["chat"]] == [True, False, False]  # remembered


def test_unrelated_errors_propagate():
    def impl(kw):
        raise RuntimeError("connection refused")

    m, _ = fake_mem0(impl)
    tune_ollama(m, Config())
    try:
        m.llm.client.chat(model="x", messages=[])
    except RuntimeError as e:
        assert "refused" in str(e)
    else:
        raise AssertionError("expected error")
