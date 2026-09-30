import asyncio
import json

from buddy.llm import GroqLLM
from buddy.memory import MemoryManager, MemoryStore
from tests.fakes import chat_response


def test_add_dedupes_and_pins(tmp_path):
    m = MemoryStore(tmp_path / "m.json")
    a, created = m.add("Sister is called Emma")
    assert created
    b, created = m.add("Sister is called Emma.", pinned=True)
    assert not created and b.id == a.id and b.pinned
    assert len(m) == 1


def test_spoken_list_pages(tmp_path):
    m = MemoryStore(tmp_path / "m.json")
    for i in range(8):
        m.add(f"Fact number {i} about the user that is distinct {i * 17}")
    text, nxt = m.spoken_list(0)
    assert text.startswith("I remember 8 things about you.") and nxt == 6
    assert text.endswith("Want me to keep going?")
    text, nxt = m.spoken_list(nxt)
    assert nxt is None and "Number 8" in text


def test_clear_makes_backup(tmp_path):
    m = MemoryStore(tmp_path / "m.json")
    m.add("Likes jazz")
    assert m.clear() == 1 and len(m) == 0
    assert list((tmp_path / "memory_backups").glob("*before-clear.json"))


def test_learning_applies_ops(settings, apis):
    store = MemoryStore(settings.data_dir / "memory.json")
    store.add("Likes jazz", "preferences")
    apis.chat_script.append(chat_response("openai/gpt-oss-120b", json.dumps({
        "add": [{"text": "Has a dog called Biscuit", "category": "people"}],
        "update": [{"id": 1, "text": "Loves jazz, especially Miles Davis"}],
        "delete": []})))
    mgr = MemoryManager(store, GroqLLM(settings, apis.client()), settings)
    r = asyncio.run(mgr.learn_from([{"role": "user", "content": "My dog Biscuit loves it when I play Miles Davis"},
                                    {"role": "assistant", "content": "Lovely."}]))
    assert r["added"] == ["Has a dog called Biscuit."]
    assert store.get(1).text == "Loves jazz, especially Miles Davis."
    assert apis.chat_requests[0]["model"] == "openai/gpt-oss-120b"  # learning uses the smart model
    assert apis.chat_requests[0]["response_format"] == {"type": "json_object"}


def test_compaction_keeps_pinned_and_rejects_destruction(settings, apis):
    store = MemoryStore(settings.data_dir / "memory.json")
    for i in range(10):
        store.add(f"Trivial detail {i} about something unrelated {i * 31}")
    pinned, _ = store.add("Allergic to peanuts", "about_you", pinned=True)
    mgr = MemoryManager(store, GroqLLM(settings, apis.client()), settings)
    # 1) A result that drops almost everything is refused.
    apis.chat_script.append(chat_response("openai/gpt-oss-120b", json.dumps(
        {"memories": [{"text": "Trivial detail 0", "category": "other", "from": [1]}], "dropped": list(range(2, 11))})))
    r = asyncio.run(mgr.compact())
    assert r["error"] == "too destructive" and len(store) == 11
    # 2) A sensible merge is applied, and the pinned memory survives even though the model forgot it.
    apis.chat_script.append(chat_response("openai/gpt-oss-120b", json.dumps(
        {"memories": [{"text": f"Merged detail {i}", "category": "other", "from": [i * 2 + 1, i * 2 + 2]}
                      for i in range(5)], "dropped": []})))
    r = asyncio.run(mgr.compact())
    assert r == {"before": 11, "after": 6}
    texts = [m.text for m in store.list()]
    assert "Allergic to peanuts." in texts
    assert store.get(pinned.id).pinned
    assert list((settings.data_dir / "memory_backups").glob("*before-compaction.json"))
