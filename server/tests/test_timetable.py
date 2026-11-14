"""Knowing which lesson it is from your timetable, and not guessing a subject from a few words."""
from datetime import datetime

from buddy.study_text import timetable_guess, tidy, too_few_words


def at(day, hh, mm=0):
    return datetime(2026, 11, day, hh, mm).timestamp()  # 2026-11-02 is a Monday


def test_learns_the_timetable_from_past_weeks():
    past = [(at(2, 9, 5), "Maths"), (at(9, 8, 55), "Maths"), (at(2, 11), "History"), (at(3, 9), "French")]
    assert timetable_guess(past, at(16, 9, 10)) == "Maths"  # Monday about 9
    assert timetable_guess(past, at(17, 9)) == ""  # Tuesday 9: French only once, not enough yet
    assert timetable_guess(past, at(16, 14)) == ""  # nothing then


def test_a_tie_isnt_a_guess():
    past = [(at(2, 9), "Maths"), (at(9, 9), "Maths"), (at(23, 9), "Art"), (at(30, 9), "Art")]
    assert timetable_guess(past, at(16, 9)) == ""


def test_a_three_second_test_is_just_a_lesson():
    assert too_few_words("testing one two three")
    lesson = {"subject": "", "transcript": "testing one two three"}
    tidy({"subject": "Maths", "title": "x"}, lesson)
    assert lesson["subject"] == ""
