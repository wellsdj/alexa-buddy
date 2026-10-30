import asyncio
import json

from fastapi.testclient import TestClient

from buddy.alarms import AlarmStore
from buddy.brain import Brain, Conversation
from buddy.claude import ClaudeLLM, convert_messages
from buddy.llm import GroqLLM
from buddy.main import create_app
from buddy.memory import MemoryManager, MemoryStore
from buddy.router import LLMRouter
from buddy.weather import Weather
from tests.fakes import chat_response, claude_error, claude_message, claude_text, claude_tool


def run(coro):
    return asyncio.run(coro)


def make_router(settings, apis, claude):
    return LLMRouter(settings, GroqLLM(settings, apis.client()), ClaudeLLM(settings, claude.client()))


def make_brain(settings, apis, claude):
    http = apis.client()
    llm = make_router(settings, apis, claude)
    mem = MemoryStore(settings.data_dir / "memory.json")
    alarms = AlarmStore(settings.data_dir / "alarms.json")
    return Brain(settings, llm, alarms, mem, MemoryManager(mem, llm, settings), Weather(settings, http))


def test_haiku_request_shape(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    llm = make_router(settings, apis, claude)
    res = run(llm.chat([{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}],
                       tools=[{"type": "function", "function": {"name": "set_timer", "description": "Timer.",
                                                                "parameters": {"type": "object", "properties": {}}}}],
                       temperature=0.6))
    body = claude.requests[0]
    assert body["model"] == "claude-haiku-5-5" and body["output_config"] == {"effort": "low"}
    assert body["system"] == "Be brief." and body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"] == [{"name": "set_timer", "description": "Timer.",
                              "input_schema": {"type": "object", "properties": {}}}]
    assert "temperature" not in body and body["max_tokens"] >= 4096
    assert res.content == "Okay." and res.model == "claude-haiku-5-5" and not apis.chat_requests


def test_big_tasks_think_harder(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    settings.update({"claude_smart_model": "claude-sonnet-5-5"})
    run(make_router(settings, apis, claude).chat([{"role": "user", "content": "plan my week"}], tier="smart"))
    assert claude.requests[0]["model"] == "claude-sonnet-5-5"
    assert claude.requests[0]["output_config"] == {"effort": "high"}


def test_tool_loop_sends_thinking_and_results_back(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    claude.script = [claude_message(claude_text("Let me check."), claude_tool("get_weather", {"day": "tomorrow"})),
                     claude_message(claude_text("Tomorrow looks dry."))]
    reply = run(make_brain(settings, apis, claude).handle("is the match on tomorrow worth going to", Conversation()))
    assert reply.text == "Tomorrow looks dry." and reply.model == "claude-haiku-5-5"
    second = claude.requests[1]["messages"]
    assert second[-2]["role"] == "assistant"
    assert second[-2]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-1"}
    assert second[-2]["content"][2]["type"] == "tool_use"
    result = second[-1]["content"][0]
    assert second[-1]["role"] == "user" and result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"


def test_action_tool_confirms_without_second_call(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    claude.script = [claude_message(claude_tool("set_timer", {"seconds": 300, "label": "pasta"}))]
    reply = run(make_brain(settings, apis, claude).handle("pasta timer please, the usual", Conversation()))
    assert "5 minutes" in reply.text and len(claude.requests) == 1


def test_no_credit_falls_back_to_groq_and_pauses_claude(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    claude.script = [claude_error(400, "invalid_request_error", "Your credit balance is too low to access the API.")]
    llm = make_router(settings, apis, claude)
    res = run(llm.chat([{"role": "user", "content": "hi"}]))
    assert res.model == "openai/gpt-oss-20b" and len(apis.chat_requests) == 1
    run(llm.chat([{"role": "user", "content": "again"}]))
    assert len(claude.requests) == 1  # paused, not hammered
    st = llm.status()
    assert st["brain"] == "groq" and "no credit" in st["last_error"]


def test_groq_never_sees_claude_blocks(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    claude.script = [claude_message(claude_tool("get_weather", {"day": "today"})),
                     claude_error(529, "overloaded_error", "Overloaded"),
                     claude_error(529, "overloaded_error", "Overloaded")]  # the SDK retries once
    apis.chat_script = [chat_response("openai/gpt-oss-20b", "Mild and dry.")]
    reply = run(make_brain(settings, apis, claude).handle("what's it like outside, in a word or two", Conversation()))
    assert reply.text == "Mild and dry."
    sent = apis.chat_requests[0]["messages"]
    assert all("_claude" not in m for m in sent)
    assert any(m.get("tool_calls") for m in sent) and any(m["role"] == "tool" for m in sent)


def test_without_claude_key_groq_answers(settings, apis, claude):
    res = run(make_router(settings, apis, claude).chat([{"role": "user", "content": "hi"}]))
    assert res.model == "openai/gpt-oss-20b" and not claude.requests


def test_groq_only_setting(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    settings.update({"brain": "groq"})
    res = run(make_router(settings, apis, claude).chat([{"role": "user", "content": "hi"}]))
    assert res.model == "openai/gpt-oss-20b" and not claude.requests


def test_web_search_uses_claude_then_groq(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    claude.script = [claude_message(claude_text("Arsenal won 2-1 "), claude_text("on Saturday."))]
    llm = make_router(settings, apis, claude)
    assert run(llm.web_search("arsenal score")) == "Arsenal won 2-1 on Saturday."
    assert claude.requests[0]["tools"][0]["type"] == "web_search_20250305"
    claude.script = [claude_error(500, "api_error", "boom"), claude_error(500, "api_error", "boom")]
    apis.chat_script = [chat_response("openai/gpt-oss-120b", "From Groq's search.")]
    assert run(llm.web_search("arsenal score")) == "From Groq's search."


def test_memory_json_mode_asks_for_json(settings, apis, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    claude.script = [claude_message(claude_text('{"ops": []}'))]
    run(make_router(settings, apis, claude).chat(
        [{"role": "system", "content": "Extract facts."}, {"role": "user", "content": "chat"}],
        tier="smart", json_mode=True))
    assert claude.requests[0]["system"].endswith("Reply with a single JSON object and nothing else.")


def test_convert_messages_groups_results_and_starts_with_user():
    system, msgs = convert_messages([
        {"role": "system", "content": "S"},
        {"role": "assistant", "content": "left over from last time"},
        {"role": "user", "content": "set two timers"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "set_timer", "arguments": json.dumps({"seconds": 60})}},
            {"id": "b", "type": "function", "function": {"name": "set_timer", "arguments": "{bad json"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "ok"},
        {"role": "tool", "tool_call_id": "b", "content": "ok"},
    ])
    assert system == "S" and msgs[0] == {"role": "user", "content": "set two timers"}
    assert [b["input"] for b in msgs[1]["content"]] == [{"seconds": 60}, {}]
    assert msgs[2]["role"] == "user" and [r["tool_use_id"] for r in msgs[2]["content"]] == ["a", "b"]
    assert len(msgs) == 3


def test_saving_claude_key_checks_it(settings, apis, claude):
    app = create_app(settings, apis.client(), claude.client())
    with TestClient(app) as client:
        claude.script = [claude_error(401, "authentication_error", "invalid x-api-key")]
        bad = client.post("/api/secrets", json={"name": "ANTHROPIC_API_KEY", "value": "sk-ant-wrong"}).json()
        assert not bad["check"]["ok"] and "didn't accept" in bad["check"]["message"]
        claude.script = [claude_error(400, "invalid_request_error", "Your credit balance is too low.")]
        broke = client.post("/api/secrets", json={"name": "ANTHROPIC_API_KEY", "value": "sk-ant-x"}).json()
        assert "no credit" in broke["check"]["message"]
        good = client.post("/api/secrets", json={"name": "ANTHROPIC_API_KEY", "value": "sk-ant-right"}).json()
        assert good["check"] == {"ok": True, "message": "Claude key works. Buddy now thinks with Claude."}
        assert good["keys"]["ANTHROPIC_API_KEY"]["set"]
        st = client.get("/api/status").json()["llm"]
        assert st["brain"] == "claude" and st["fast"] == ["claude-haiku-5-5"]


def test_old_install_without_the_library_keeps_using_groq(settings, apis, claude, monkeypatch):
    import buddy.claude
    monkeypatch.setattr(buddy.claude, "anthropic", None)
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    llm = make_router(settings, apis, claude)
    assert run(llm.chat([{"role": "user", "content": "hi"}])).model == "openai/gpt-oss-20b"
    assert "Update Buddy" in llm.status()["last_error"]
    assert "Update Buddy" in run(llm.claude.check())[1]
