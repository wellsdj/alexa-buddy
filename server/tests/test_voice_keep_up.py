"""Mid-conversation, a so-so match to the person it was just sure about still counts, and is learnt."""
import asyncio

import numpy as np

from buddy.hub import Hub


def test_a_weaker_match_mid_chat_is_still_you_and_gets_learnt(settings):
    hub = Hub(settings)
    added = []
    hub.voiceid.add_print = lambda name, v, s: added.append(name)
    hub.voiceid.trusted = lambda name, t: True
    v = np.zeros(4)
    asyncio.run(hub._learn_voice({"name": "Wells", "score": 0.6, "best": "Wells", "print": v, "seconds": 3}))
    weak = {"name": "", "score": 0.25, "best": "Wells", "print": v, "seconds": 3}
    asyncio.run(hub._learn_voice(weak))
    assert weak["name"] == "Wells" and added == ["Wells", "Wells"]
    stranger = {"name": "", "score": 0.1, "best": "Wells", "print": v, "seconds": 3}
    asyncio.run(hub._learn_voice(stranger))
    assert stranger["name"] == "" and len(added) == 2  # too far off: not them
