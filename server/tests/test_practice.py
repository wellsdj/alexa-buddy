"""Every question framework, at every level, many times: the stored answer must be right."""
import math
import random
from fractions import Fraction as F

import pytest

from buddy.practice import SKILLS, Practice, check_field, make_question, match_skills, parse_number, parse_poly, show_answer

N = 150


def value(a):
    return F(a["exact"]) if "exact" in a else F(a["value"])


@pytest.mark.parametrize("sid", sorted(SKILLS))
def test_every_skill_makes_good_questions_at_every_level(sid):
    rng = random.Random(sid)
    for level in range(1, 6):
        for _ in range(N):
            q = make_question(sid, level, rng)
            assert q.text and len(q.fields) == len(q.answers)
            for a in q.answers:
                shown = show_answer(a)
                assert shown and "?" not in shown


def test_answers_check_out_a_second_way():
    """Independent checks by substituting answers back into the question's own numbers."""
    import re
    rng = random.Random(1)
    for _ in range(300):
        level = rng.randint(1, 5)
        q = make_question("angles_triangles", 1, rng)
        known = [int(x) for x in re.findall(r"(\d+)°", " ".join(a or "" for a in q.diagram["angles"]))]
        assert sum(known) + value(q.answers[0]) == 180
        q = make_question("simultaneous_equations", level, rng)
        x, y = value(q.answers[0]), value(q.answers[1])
        for line in q.text.split("\n")[1:]:
            m = re.fullmatch(r"(\d*)x ([+−]) (\d*)y = (−?\d+)", line)
            a = int(m.group(1) or 1)
            b = int(m.group(3) or 1) * (1 if m.group(2) == "+" else -1)
            assert a * x + b * y == int(m.group(4).replace("−", "-"))
        q = make_question("linear_equations", 2, rng)
        m = re.fullmatch(r"Solve (\d+)x ([+−]) (\d+) = (−?[\d.]+)", q.text)
        a, b, c = int(m.group(1)), int(m.group(3)) * (1 if m.group(2) == "+" else -1), F(m.group(4).replace("−", "-"))
        assert a * value(q.answers[0]) + b == c
        q = make_question("pythagoras", 1, rng)
        a_, b_ = (int(s.split()[0]) for s in q.diagram["sides"][:2])
        assert value(q.answers[0]) ** 2 == a_ ** 2 + b_ ** 2


def test_typing_answers_in_different_ways():
    assert parse_number("£12.50") == F(25, 2) and parse_number("3/4") == F(3, 4) and parse_number("−7") == -7
    assert parse_number("45°") == 45 and parse_number("abc") is None
    assert parse_poly("x² + 5x + 6", "x") == {2: 1, 1: 5, 0: 6}
    assert parse_poly("6+5x+x^2", "x") == {2: 1, 1: 5, 0: 6}
    assert parse_poly("3n-2", "n") == {1: 3, 0: -2}
    assert parse_poly("2x^^2", "x") is None and parse_poly("hello", "x") is None
    frac = {"kind": "fraction", "n": 3, "d": 4}
    assert check_field(frac, ["3", "4"]) and not check_field(frac, ["6", "8"])  # simplest form only
    rounded = {"kind": "number", "value": "12.6", "dp": 1}
    assert check_field(rounded, "12.6") and check_field(rounded, "12.57") and not check_field(rounded, "12.5")
    mixed = {"kind": "mixed", "n": 7, "d": 3}
    assert check_field(mixed, ["2", "1", "3"]) and not check_field(mixed, ["", "7", "3"])


def test_level_goes_up_with_right_answers_and_down_with_wrong(tmp_path):
    p = Practice(tmp_path)
    r = p.start("me", "lesson", ["arithmetic"])
    rid, start = r["round"], p.progress.level("me", "arithmetic")
    for _ in range(4):
        q = r["question"]
        live = p.question(q["id"], "me")
        res = p.answer(rid, q["id"], [show_answer(a) for a in live.q.answers])
        assert res["right"]
        r = p.next(rid)
    up = p.progress.level("me", "arithmetic")
    assert up > start
    q = r["question"]
    assert p.answer(rid, q["id"], ["nonsense"])["try_again"]
    assert not p.answer(rid, q["id"], ["nonsense"])["right"]
    assert p.progress.level("me", "arithmetic") < up


def test_lessons_are_matched_to_skills_without_ai():
    assert "angles_triangles" in match_skills("Angles in triangles: isosceles and exterior angles")
    assert "pythagoras" in match_skills("Using Pythagoras to find the hypotenuse")
