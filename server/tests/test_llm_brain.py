"""The brain (Claude Haiku via a fake Anthropic API) and the Groq client that now only
judges whether room talk is meant for Buddy."""
import asyncio

from buddy.alarms import AlarmStore
from buddy.brain import THINK_OFFER, Brain, Conversation
from buddy.claude import ClaudeLLM
from buddy.llm import GroqLLM, LLMError
from buddy.memory import MemoryManager, MemoryStore
from buddy.weather import Weather
from tests.fakes import chat_response, claude_error, claude_message, claude_text, claude_tool


def make_brain(settings, apis, claude):
    http = apis.client()
    llm = ClaudeLLM(settings, claude.client())
    mem = MemoryStore(settings.data_dir / "memory.json")
    alarms = AlarmStore(settings.data_dir / "alarms.json")
    return Brain(settings, llm, alarms, mem, MemoryManager(mem, llm, settings), Weather(settings, http))


def run(coro):
    return asyncio.run(coro)


# ---------- Groq: the room listener's model client ----------
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


# ---------- the brain ----------
def test_fast_path_alarm_needs_no_model(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    r = run(brain.handle("hey buddy set an alarm for half seven tomorrow", Conversation()))
    assert r.intent == "alarm" and r.text.startswith("Alarm set for 7:30 AM tomorrow")
    assert claude.requests == [] and len(brain.alarms.list()) == 1


def test_model_action_tool_confirms_without_second_call(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    claude.script = [claude_message(claude_tool("set_alarm", {"time": "06:45", "repeat": "weekdays", "label": "gym"}))]
    r = run(brain.handle("I need to be up for the gym at quarter to seven on work days", Conversation()))
    assert len(claude.requests) == 1
    assert r.text.startswith("Alarm set for 6:45 AM every weekday.")
    assert brain.alarms.list()[0].label == "gym"
    tools = {t["name"] for t in claude.requests[0]["tools"]}
    assert {"set_alarm", "remember", "forget", "web_search", "think_harder"} <= tools
    assert claude.requests[0]["model"] == "claude-haiku-5-5" and claude.requests[0]["output_config"]["effort"] == "low"


def test_web_search_uses_claude_search(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    claude.script = [
        claude_message(claude_tool("web_search", {"query": "Arsenal score"})),
        claude_message(claude_text("Arsenal won 2-1 on Saturday.")),  # the search itself
        claude_message(claude_text("Arsenal beat Spurs two one on Saturday.")),
    ]
    r = run(brain.handle("how did Arsenal get on", Conversation()))
    assert r.text == "Arsenal beat Spurs two one on Saturday."
    assert claude.requests[1]["tools"][0]["type"].startswith("web_search_")
    result = claude.requests[2]["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["content"] == "Arsenal won 2-1 on Saturday."
    assert not apis.chat_requests  # Groq is never asked to answer


def test_deep_thinking_asks_permission_first(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    claude.script = [claude_message(claude_tool("think_harder", {"reason": "planning"}))]
    r = run(brain.handle("plan my whole week of meals and workouts", conv))
    assert r.text == THINK_OFFER and r.follow_up and len(claude.requests) == 1
    claude.script = [claude_message(claude_text("Here's a proper plan."))]
    r = run(brain.handle("yes please", conv))
    assert r.text == "Here's a proper plan." and r.intent == "llm_deep"
    deep = claude.requests[1]
    assert deep["output_config"]["effort"] == "high"
    assert deep["messages"][-1] == {"role": "user", "content": "plan my whole week of meals and workouts"}


def test_saying_no_to_thinking_gives_a_quick_answer(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    claude.script = [claude_message(claude_tool("think_harder", {}))]
    run(brain.handle("plan my week", conv))
    claude.script = [claude_message(claude_text("Quick version: rest on Sunday."))]
    r = run(brain.handle("no, just tell me", conv))
    assert r.text == "Quick version: rest on Sunday."
    quick = claude.requests[1]
    assert quick["output_config"]["effort"] == "low"
    assert "think_harder" not in {t["name"] for t in quick["tools"]}


def test_asking_to_think_hard_skips_the_question(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    run(brain.handle("think it through properly: should I get a heat pump", Conversation()))
    assert claude.requests[0]["output_config"]["effort"] == "high"


def test_off_puts_buddy_to_sleep(settings, apis, claude):
    settings.update({"follow_up": "always"})
    brain = make_brain(settings, apis, claude)
    r = run(brain.handle("hey buddy, off", Conversation()))
    assert {"type": "sleep"} in r.actions and r.text == "" and not r.follow_up  # off quietly
    for said in ("turn yourself off", "go to sleep", "go away", "shut up", "be quiet please",
                 "okay you can turn off now thanks", "that's all, thanks buddy", "bye", "stop listening"):
        assert run(brain.handle(said, Conversation())).intent == "off", said
    assert claude.requests == []


def test_off_words_dont_catch_other_requests(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    for said in ("turn off my alarm", "turn off the lights", "off the top of your head, what's 12 times 12"):
        assert run(brain.handle(said, Conversation())).intent != "off", said


def test_memory_tools_and_clear_needs_confirmation(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    claude.script = [claude_message(claude_tool("remember", {"text": "Sister is called Emma", "category": "people"}))]
    r = run(brain.handle("remember that my sister is called Emma", conv))
    assert r.text == "Got it, I'll remember that." and brain.memory.list()[0].pinned
    # clear_memory without a prior request is refused and the model is told to ask.
    claude.script = [
        claude_message(claude_tool("clear_memory", {"confirmed": True})),
        claude_message(claude_text("Are you sure you want me to forget everything?")),
    ]
    r = run(brain.handle("forget everything", conv))
    assert len(brain.memory) == 1 and r.follow_up
    claude.script = [claude_message(claude_tool("clear_memory", {"confirmed": True}))]
    r = run(brain.handle("yes", conv))
    assert len(brain.memory) == 0 and "wiped" in r.text


def test_system_prompt_has_weather_and_memory(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    brain.memory.add("Allergic to peanuts", pinned=True)
    prompt = run(brain.system_prompt())
    assert "Weather in Richmond, London: now 14°C" in prompt
    assert "#1 Allergic to peanuts." in prompt


def test_memory_readout_and_paging(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    for name in ["Emma", "Oliver", "Priya", "Tom", "Sam", "Jess", "Ravi", "Lucy"]:
        brain.memory.add(f"Has a friend called {name}")
    conv = Conversation()
    r = run(brain.handle("what's in your memory", conv))
    assert r.text.startswith("I remember 8 things") and r.follow_up
    r = run(brain.handle("keep going", conv))
    assert "Number 7" in r.text and claude.requests == []


def test_markdown_is_stripped_for_speech(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    claude.script = [claude_message(claude_text("**Sure!** Here are some ideas:\n- Pasta\n- Curry 🍛 https://x.y"))]
    r = run(brain.handle("dinner ideas?", Conversation()))
    assert r.text == "Sure! Here are some ideas: Pasta Curry"


def test_no_key_gives_helpful_message(tmp_path, apis, claude):
    from buddy.config import Settings
    brain = make_brain(Settings(tmp_path), apis, claude)
    r = run(brain.handle("tell me a joke", Conversation()))
    assert "Claude API key" in r.text and not claude.requests


def test_out_of_credit_says_so(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    claude.script = [claude_error(400, "invalid_request_error", "Your credit balance is too low.")]
    r = run(brain.handle("tell me a joke", Conversation()))
    assert "out of credit" in r.text
    run(brain.handle("another joke", Conversation()))
    assert len(claude.requests) == 1  # it doesn't keep hammering the API
