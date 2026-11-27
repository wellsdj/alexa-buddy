import asyncio

from buddy.listening import ContextJudge


def judge_for(settings):
    return ContextJudge(settings)


def ask(j, text, **kw):
    j.hear(text)
    return asyncio.run(j.is_for_buddy(text, **kw))


def test_hey_buddy_at_the_start_needs_no_model(settings, apis):
    j = judge_for(settings)
    assert ask(j, "Hey buddy, what's the weather?") and j.last_how == "hey buddy"
    assert not apis.chat_requests


def test_quick_command_right_after_buddy_spoke(settings, apis):
    j = judge_for(settings)
    assert ask(j, "stop", since_reply_s=3) and not apis.chat_requests


def test_phrase_rules_decide_and_no_model_is_asked(settings, apis):
    j = judge_for(settings)
    assert ask(j, "Let me ask you a question.") and j.last_how == "phrase rules"
    assert ask(j, "Quick question for you") and j.last_how == "phrase rules"
    assert not ask(j, "I was telling my buddy about it.")
    assert not ask(j, "Did you see the match last night?")
    assert not apis.chat_requests  # Groq is gone: nothing is sent anywhere to decide


def test_groq_is_gone(settings):
    import buddy.llm as llm
    from buddy.config import DEFAULTS, SECRET_KEYS
    assert not hasattr(llm, "GroqLLM")
    assert "GROQ_API_KEY" not in SECRET_KEYS
    assert not {"fast_model", "smart_model", "fallback_models"} & set(DEFAULTS)


def test_an_old_groq_key_is_dropped_on_load(tmp_path):
    import json
    from buddy.config import Settings
    (tmp_path / "secrets.json").write_text(json.dumps({"GROQ_API_KEY": "gsk_old", "ANTHROPIC_API_KEY": "sk-ant"}))
    (tmp_path / "settings.json").write_text(json.dumps({"fast_model": "openai/gpt-oss-20b", "volume": 4}))
    s = Settings(tmp_path)
    assert s.secret("GROQ_API_KEY") in ("", None) and s.secret("ANTHROPIC_API_KEY") == "sk-ant"
    assert s.get("fast_model") is None and s["volume"] == 4
