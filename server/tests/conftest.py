import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("BUDDY_NO_MDNS", "1")
for k in ("GROQ_API_KEY", "ELEVENLABS_API_KEY", "ANTHROPIC_API_KEY"):
    os.environ.pop(k, None)

from buddy.config import Settings  # noqa: E402
from tests.fakes import FakeAPIs, FakeClaude  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    s = Settings(tmp_path)
    s.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    s.set_secret("GROQ_API_KEY", "gsk_test")
    s.set_secret("ELEVENLABS_API_KEY", "el_test")
    s.update({"room_listening_on": True, "reword_replies": False, "fact_check": False})  # exact replies in asserts
    return s


@pytest.fixture
def apis():
    return FakeAPIs()


@pytest.fixture
def claude():
    return FakeClaude()
