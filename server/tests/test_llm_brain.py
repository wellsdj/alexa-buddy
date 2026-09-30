import asyncio

from buddy.alarms import AlarmStore
from buddy.brain import Brain, Conversation
from buddy.llm import GroqLLM, LLMError
from buddy.memory import MemoryManager, MemoryStore
from buddy.weather import Weather
from tests.fakes import chat_response


def make_brain(settings, apis):
    http = apis.client()
    llm = GroqLLM(settings, http)
    mem = MemoryStore(settings.data_dir / "memory.json")
    alarms = AlarmStore(settings.data_dir / "alarms.json")
    return Brain(settings, llm, alarms, mem, MemoryManager(mem, llm, settings), Weather(settings, http))


def run(coro):
    return asyncio.run(coro)


def test_fast_model_request_shape(settings, apis):
    llm = GroqLLM(settings, apis.client())
    res = run(llm.chat([{"role": "user", "content": "hi"}]))
    body = apis.chat_requests[0]
    assert body["model"] == "openai/gpt-oss-20b"
    assert body["reasoning_effort"] == "low" and body["include_reasoning"] is False
    assert res.content == "Okay." and res.model == "openai/gpt-oss-20b"


def test_rate_limit_falls_back_and_cools_down(settings, apis):
    def limited(body):
        return 429, {"error": {"message": "Rate limit reached ... tokens per minute (TPM)"}}, {"retry-after": "7"}
    apis.chat_script = [limited, chat_response("qwen/qwen3.6-27b", "From the fallback.")]
    llm = GroqLLM(settings, apis.client())
    res = run(llm.chat([{"role": "user", "content": "hi"}]))
    assert res.model == "qwen/qwen3.6-27b" and res.content == "From the fallback."
    assert apis.chat_requests[1]["reasoning_effort"] == "none"  # qwen: thinking off on the fast path
    assert "openai/gpt-oss-20b" in llm.status()["cooldowns"]
    # Next call skips the cooling model entirely.
    run(llm.chat([{"role": "user", "content": "again"}]))
    assert apis.chat_requests[2]["model"] == "qwen/qwen3.6-27b"


def test_retired_model_is_skipped(settings, apis):
    apis.models = ["openai/gpt-oss-120b", "whisper-large-v3-turbo"]  # 20b and qwen gone
    llm = GroqLLM(settings, apis.client())
    run(llm.chat([{"role": "user", "content": "hi"}]))
    assert apis.chat_requests[0]["model"] == "openai/gpt-oss-120b"


def test_decommissioned_error_marks_dead(settings, apis):
    def gone(body):
        return 400, {"error": {"message": "The model has been decommissioned", "code": "model_decommissioned"}}, {}
    apis.chat_script = [gone]
    llm = GroqLLM(settings, apis.client())
    run(llm.chat([{"role": "user", "content": "hi"}]))
    assert "openai/gpt-oss-20b" in llm.dead


def test_all_models_failing_raises(settings, apis):
    fail = lambda body: (500, {"error": "boom"}, {})  # noqa: E731
    apis.chat_script = [fail] * 12
    llm = GroqLLM(settings, apis.client())
    try:
        run(llm.chat([{"role": "user", "content": "hi"}]))
        assert False, "expected LLMError"
    except LLMError:
        pass


def test_fast_path_alarm_needs_no_model(settings, apis):
    brain = make_brain(settings, apis)
    r = run(brain.handle("hey buddy set an alarm for half seven tomorrow", Conversation()))
    assert r.intent == "alarm" and r.text.startswith("Alarm set for 7:30 AM tomorrow")
    assert apis.chat_requests == [] and len(brain.alarms.list()) == 1


def test_model_action_tool_confirms_without_second_call(settings, apis):
    brain = make_brain(settings, apis)
    apis.chat_script = [chat_response("openai/gpt-oss-20b", tool_calls=[
        ("set_alarm", {"time": "06:45", "repeat": "weekdays", "label": "gym"})])]
    r = run(brain.handle("I need to be up for the gym at quarter to seven on work days", Conversation()))
    assert len(apis.chat_requests) == 1
    assert r.text.startswith("Alarm set for 6:45 AM every weekday.")
    assert brain.alarms.list()[0].label == "gym"
    tools = {t["function"]["name"] for t in apis.chat_requests[0]["tools"]}
    assert {"set_alarm", "remember", "forget", "web_search", "think_harder"} <= tools


def test_info_tool_gets_second_call(settings, apis):
    brain = make_brain(settings, apis)
    apis.chat_script = [
        chat_response("openai/gpt-oss-20b", tool_calls=[("web_search", {"query": "Arsenal score"})]),
        chat_response("openai/gpt-oss-120b", "Arsenal won 2-1 on Saturday."),  # the search itself
        chat_response("openai/gpt-oss-20b", "Arsenal beat Spurs two one on Saturday."),
    ]
    r = run(brain.handle("how did Arsenal get on", Conversation()))
    assert r.text == "Arsenal beat Spurs two one on Saturday."
    search = apis.chat_requests[1]
    assert search["tools"] == [{"type": "browser_search"}] and search["model"] == "openai/gpt-oss-120b"


def test_think_harder_escalates(settings, apis):
    brain = make_brain(settings, apis)
    apis.chat_script = [
        chat_response("openai/gpt-oss-20b", tool_calls=[("think_harder", {"reason": "long"})]),
        chat_response("openai/gpt-oss-120b", "Here's a plan."),
    ]
    r = run(brain.handle("hmm what about my week", Conversation()))
    assert apis.chat_requests[1]["model"] == "openai/gpt-oss-120b" and r.model == "openai/gpt-oss-120b"


def test_long_requests_start_on_smart_model(settings, apis):
    brain = make_brain(settings, apis)
    run(brain.handle("explain how a heat pump works", Conversation()))
    assert apis.chat_requests[0]["model"] == "openai/gpt-oss-120b"
    assert apis.chat_requests[0]["reasoning_effort"] == "medium"


def test_memory_tools_and_clear_needs_confirmation(settings, apis):
    brain = make_brain(settings, apis)
    conv = Conversation()
    apis.chat_script = [chat_response("openai/gpt-oss-20b", tool_calls=[
        ("remember", {"text": "Sister is called Emma", "category": "people"})])]
    r = run(brain.handle("remember that my sister is called Emma", conv))
    assert r.text == "Got it, I'll remember that." and brain.memory.list()[0].pinned
    # clear_memory without a prior request is refused and the model is told to ask.
    apis.chat_script = [
        chat_response("openai/gpt-oss-20b", tool_calls=[("clear_memory", {"confirmed": True})]),
        chat_response("openai/gpt-oss-20b", "Are you sure you want me to forget everything?"),
    ]
    r = run(brain.handle("forget everything", conv))
    assert len(brain.memory) == 1 and r.follow_up
    apis.chat_script = [chat_response("openai/gpt-oss-20b", tool_calls=[("clear_memory", {"confirmed": True})])]
    r = run(brain.handle("yes", conv))
    assert len(brain.memory) == 0 and "wiped" in r.text


def test_system_prompt_has_weather_and_memory(settings, apis):
    brain = make_brain(settings, apis)
    brain.memory.add("Allergic to peanuts", pinned=True)
    prompt = run(brain.system_prompt())
    assert "Weather in Richmond, London: now 14°C" in prompt
    assert "#1 Allergic to peanuts." in prompt


def test_memory_readout_and_paging(settings, apis):
    brain = make_brain(settings, apis)
    for name in ["Emma", "Oliver", "Priya", "Tom", "Sam", "Jess", "Ravi", "Lucy"]:
        brain.memory.add(f"Has a friend called {name}")
    conv = Conversation()
    r = run(brain.handle("what's in your memory", conv))
    assert r.text.startswith("I remember 8 things") and r.follow_up
    r = run(brain.handle("keep going", conv))
    assert "Number 7" in r.text and apis.chat_requests == []


def test_markdown_is_stripped_for_speech(settings, apis):
    brain = make_brain(settings, apis)
    apis.chat_script = [chat_response("openai/gpt-oss-20b", "**Sure!** Here are some ideas:\n- Pasta\n- Curry 🍛 https://x.y")]
    r = run(brain.handle("dinner ideas?", Conversation()))
    assert r.text == "Sure! Here are some ideas: Pasta Curry"


def test_no_key_gives_helpful_message(tmp_path, apis):
    from buddy.config import Settings
    brain = make_brain(Settings(tmp_path), apis)
    r = run(brain.handle("tell me a joke", Conversation()))
    assert "Groq API key" in r.text
