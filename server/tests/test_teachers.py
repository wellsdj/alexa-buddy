"""Study Buddy learns teachers' voices from lessons (with a stand-in for the speaker model)."""
import asyncio

import numpy as np
import pytest

from buddy.study import StudyError, StudyStore, StudyWorker
from buddy.voiceid import VoiceID
from tests.test_voiceid import fake_embed, voice


def lesson(teacher_pitch, other_pitch=900, minutes=1.0, seed=0):
    """A pretend lesson: the teacher talks most (8 s turns), a pupil now and then (3 s)."""
    parts, n = [], 0
    while n < minutes * 60:
        parts += [voice(teacher_pitch, 8, seed=seed + n), voice(other_pitch, 3, seed=seed + n + 1)]
        n += 11
    return np.concatenate(parts)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceID, "installed", lambda self: True)
    monkeypatch.setattr(VoiceID, "embed", fake_embed)
    w = StudyWorker(StudyStore(tmp_path), None, None)
    w.teachers = VoiceID(tmp_path / "teachers")
    return w


def test_the_voice_that_talks_most_is_the_teacher(worker):
    main = worker.teachers.main_voice(lesson(200))
    assert main["share"] > 0.6
    assert worker.teachers.match_print(main["print"]) == ("", 0.0)  # nobody saved yet


def test_a_teacher_who_says_their_name_is_learnt_and_known_next_time(worker):
    first = asyncio.run(worker.find_teacher(lesson(200), said_name="Mr Smith"))
    assert first["teacher"] == "Mr Smith" and first["teacher_guessed"]
    second = asyncio.run(worker.find_teacher(lesson(200, seed=50)))
    assert second["teacher"] == "Mr Smith" and "teacher_guessed" not in second
    other = asyncio.run(worker.find_teacher(lesson(450, seed=90)))
    assert other["teacher"] == "" and other["teacher_print"]  # a new teacher: waits to be named


def test_naming_the_teacher_on_the_lesson_page(worker):
    pid = worker.store.add_profile("Wells", "1234")["id"]
    x = worker.store.new_lesson(pid, "Maths", "2026-11-14", "", "audio")
    x.update(asyncio.run(worker.find_teacher(lesson(450))))
    worker.store.save_lesson(pid, x)
    named = worker.name_teacher(pid, x["id"], "Miss Patel")
    assert named["teacher"] == "Miss Patel"
    assert asyncio.run(worker.find_teacher(lesson(450, seed=7)))["teacher"] == "Miss Patel"


def test_a_wrong_guess_is_replaced_when_you_correct_it(worker):
    pid = worker.store.add_profile("Wells", "1234")["id"]
    x = worker.store.new_lesson(pid, "Maths", "2026-11-14", "", "audio")
    x.update(asyncio.run(worker.find_teacher(lesson(200), said_name="Sir")))
    worker.store.save_lesson(pid, x)
    worker.name_teacher(pid, x["id"], "Mr Jones")
    assert list(worker.teachers.people) == ["Mr Jones"]


def test_no_clear_teacher_no_question(worker):
    pid = worker.store.add_profile("Wells", "1234")["id"]
    x = worker.store.new_lesson(pid, "Maths", "2026-11-14", "", "audio")
    with pytest.raises(StudyError):
        worker.name_teacher(pid, x["id"], "Mr Smith")
