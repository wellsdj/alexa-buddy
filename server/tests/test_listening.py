import asyncio
import json

from buddy.listening import ContextJudge
from buddy.llm import GroqLLM
from tests.fakes import chat_response


def judge_for(settings, apis):
    return ContextJudge(settings, GroqLLM(settings, apis.client()))


def ask(j, text, **kw):
    j.hear(text)
    return asyncio.run(j.is_for_buddy(text, **kw))


def test_hey_buddy_at_the_start_needs_no_model(settings, apis):
    j = judge_for(settings, apis)
    assert ask(j, "Hey buddy, what's the weather?") and j.last_how == "hey buddy"
    assert not apis.chat_requests


def test_groq_decides_with_recent_context(settings, apis):
    j = judge_for(settings, apis)
    apis.chat_script = [chat_response("openai/gpt-oss-20b", json.dumps({"for_assistant": False})),
                        chat_response("openai/gpt-oss-20b", json.dumps({"for_assistant": True}))]
    assert not ask(j, "Did you see the match last night?")
    assert ask(j, "And what time does it start tomorrow?", last_reply="Arsenal play at three.", since_reply_s=20)
    sent = apis.chat_requests[1]["messages"][1]["content"]
    assert "- Did you see the match last night?" in sent and "Arsenal play at three." in sent
    assert apis.chat_requests[1]["response_format"] == {"type": "json_object"}


def test_quick_command_right_after_buddy_spoke(settings, apis):
    j = judge_for(settings, apis)
    assert ask(j, "stop", since_reply_s=3) and not apis.chat_requests


def test_without_groq_phrase_rules_decide(settings, apis):
    settings.set_secret("GROQ_API_KEY", "")
    j = judge_for(settings, apis)
    assert ask(j, "Let me ask you a question.") and j.last_how == "phrase rules"
    assert not ask(j, "I was telling my buddy about it.")
    assert not apis.chat_requests


def test_groq_failure_falls_back_to_rules(settings, apis):
    j = judge_for(settings, apis)
    apis.chat_script = [lambda body: (500, {"error": "boom"}, {})] * 12
    assert ask(j, "Quick question for you") and j.last_how == "phrase rules"
