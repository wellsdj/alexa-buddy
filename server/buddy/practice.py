"""Maths practice that fits the student: question frameworks, answers worked out by code.

Every kind of question ("skill") is a small generator: it picks numbers for a difficulty level from
1 (easy) to 5 (hard), writes the question and works out the answer itself, exactly (with fractions,
not rounded floats) and then checks it a second way. Claude never writes these questions or their
answers, so it can't get them wrong; it's only used to say which skills a lesson covered and to
explain the steps when someone presses Help (it is told the right answer).

How well someone knows each skill is a level from 1 to 5. Questions are set a little above it;
getting them right raises it, getting them wrong lowers it.
"""
from __future__ import annotations

import json
import math
import random
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction as F
from pathlib import Path
from typing import Callable


# ---------- answers ----------
def num(v, dp: int | None = None) -> dict:
    """A number answer. Exact (a Fraction or int) unless dp is given."""
    if dp is None:
        v = F(v)
        return {"kind": "number", "exact": f"{v.numerator}/{v.denominator}"}
    return {"kind": "number", "value": round_dp(v, dp), "dp": dp}


def frac(v, mixed: bool = False) -> dict:
    v = F(v)
    return {"kind": "mixed" if mixed else "fraction", "n": v.numerator, "d": v.denominator}


def expr(coef: dict[int, F], var: str = "x") -> dict:
    return {"kind": "expr", "var": var, "coef": {str(k): f"{F(c).numerator}/{F(c).denominator}" for k, c in coef.items() if c}}


def round_dp(v, dp: int) -> str:
    q = Decimal(1).scaleb(-dp)
    return str(Decimal(str(v) if not isinstance(v, F) else v.numerator / Decimal(v.denominator)).quantize(q, ROUND_HALF_UP))


def show_number(v) -> str:
    v = F(v)
    if v.denominator == 1:
        return str(v.numerator)
    den = v.denominator
    for p in (2, 5):
        while den % p == 0:
            den //= p
    if den != 1:  # doesn't end as a decimal (like 1/3): show it as a fraction
        return f"{v.numerator}/{v.denominator}"
    return format((v.numerator / Decimal(v.denominator)).normalize(), "f")


def show_answer(a: dict) -> str:
    if a["kind"] == "number":
        return a["value"] if "dp" in a else show_number(F(a["exact"]))
    if a["kind"] in ("fraction", "mixed"):
        n, d = a["n"], a["d"]
        if d == 1:
            return str(n)
        if a["kind"] == "mixed" and abs(n) > d:
            w, r = divmod(abs(n), d)
            return f"{'-' if n < 0 else ''}{w} {r}/{d}"
        return f"{n}/{d}"
    if a["kind"] == "expr":
        return show_poly({int(k): F(v) for k, v in a["coef"].items()}, a["var"])
    return "?"


def show_poly(coef: dict[int, F], var: str = "x") -> str:
    out = ""
    for p in sorted(coef, reverse=True):
        c = F(coef[p])
        if c == 0:
            continue
        mag = abs(c)
        body = (show_number(mag) if (mag != 1 or p == 0) else "") + (var if p >= 1 else "") + (f"²" if p == 2 else f"³" if p == 3 else "")
        out += (" − " if c < 0 else " + ") + body if out else ("−" if c < 0 else "") + body
    return out or "0"


# ---------- checking what was typed ----------
def parse_number(s: str) -> F | None:
    s = (s or "").strip().replace(",", "").replace("£", "").replace("°", "").replace("−", "-").replace(" ", "")
    s = re.sub(r"(cm|mm|m|km|kg|g|%|units?)[²³23]?$", "", s)
    if not s:
        return None
    try:
        if re.fullmatch(r"-?\d+/\d+", s):
            a, b = s.split("/")
            return F(int(a), int(b)) if int(b) else None
        if re.fullmatch(r"-?(\d+\.?\d*|\.\d+)", s):
            return F(Decimal(s))
    except (ValueError, ArithmeticError):
        return None
    return None


def parse_poly(s: str, var: str) -> dict[int, F] | None:
    """A polynomial like 3x² - 5x + 2 (also x^2, 2n+3). Returns {power: coefficient}."""
    s = (s or "").replace("−", "-").replace("²", "^2").replace("³", "^3").replace(" ", "").replace("*", "").lower()
    if not s or not re.fullmatch(rf"[0-9.{var}^+\-/]+", s):
        return None
    if s[0] not in "+-":
        s = "+" + s
    coef: dict[int, F] = {}
    for sign, c, v, p in re.findall(rf"([+-])(\d+(?:\.\d+)?(?:/\d+)?)?({var}(?:\^(\d+))?)?", s):
        if not c and not v:
            continue
        val = F(c) if c and "/" in c else F(Decimal(c)) if c else F(1)
        power = (int(p) if p else 1) if v else 0
        coef[power] = coef.get(power, F(0)) + (-val if sign == "-" else val)
    rebuilt = "".join(re.findall(rf"[+-](?:\d+(?:\.\d+)?(?:/\d+)?)?(?:{var}(?:\^\d+)?)?", s))
    if rebuilt != s:
        return None  # something we couldn't read
    return {k: v for k, v in coef.items() if v}


def check_field(a: dict, typed) -> bool:
    k = a["kind"]
    if k == "number":
        v = parse_number(typed if isinstance(typed, str) else str(typed))
        if v is None:
            return False
        if "dp" in a:
            return round_dp(v, a["dp"]) == a["value"]
        return v == F(a["exact"])
    if k in ("fraction", "mixed"):
        parts = typed if isinstance(typed, list) else [typed]
        try:
            if k == "mixed":
                w = int(parts[0] or 0) if str(parts[0]).strip() else 0
                n, d = int(parts[1] or 0), int(parts[2] or 1)
            else:
                w, n, d = 0, int(parts[0]), int(parts[1] if len(parts) > 1 and str(parts[1]).strip() else 1)
        except (ValueError, IndexError):
            return False
        if d <= 0 or math.gcd(n, d) != 1:
            return False  # must be in its simplest form
        if k == "mixed" and (n >= d and d != 1):
            return False
        value = F(abs(w) * d + n, d) * (-1 if w < 0 else 1) if w else F(n, d)
        return value == F(a["n"], a["d"])
    if k == "expr":
        got = parse_poly(typed if isinstance(typed, str) else "", a["var"])
        want = {int(p): F(c) for p, c in a["coef"].items()}
        return got is not None and got == {p: c for p, c in want.items() if c}
    return False


# ---------- the question frameworks ----------
@dataclass
class Q:
    text: str
    fields: list[dict]  # what to type, in order: {label, kind, prefix, suffix}
    answers: list[dict]
    diagram: dict | None = None
    method: str = ""  # a short note for Help on how to do it


def field_(label: str = "", kind: str = "number", prefix: str = "", suffix: str = "") -> dict:
    return {"label": label, "kind": kind, "prefix": prefix, "suffix": suffix}


@dataclass
class Skill:
    id: str
    name: str
    topic: str
    keywords: tuple[str, ...]
    make: Callable[[int, random.Random], Q] = field(repr=False)


SKILLS: dict[str, Skill] = {}


def skill(id: str, name: str, topic: str, *keywords: str):
    def wrap(fn):
        SKILLS[id] = Skill(id, name, topic, keywords, fn)
        return fn
    return wrap


def pick(r: random.Random, *xs):
    return r.choice(xs)


def nz(r: random.Random, lo: int, hi: int) -> int:
    while True:
        v = r.randint(lo, hi)
        if v:
            return v


def signed(v) -> str:
    v = F(v)
    return f"+ {show_number(v)}" if v >= 0 else f"− {show_number(-v)}"


def neg(v) -> str:
    return show_number(v).replace("-", "−")


# --- number ---
@skill("arithmetic", "Arithmetic", "Number", "add", "subtract", "multiply", "divide", "arithmetic", "order of operations", "bidmas", "bodmas", "negative", "decimal")
def _arith(level, r):
    if level == 1:
        a, b = r.randint(12, 99), r.randint(11, 99)
        op = pick(r, "+", "−")
        a, b = (a, b) if op == "+" or a >= b else (b, a)
        return Q(f"Work out {a} {op} {b}", [field_()], [num(a + b if op == "+" else a - b)])
    if level == 2:
        a, b = r.randint(3, 12), r.randint(3, 12)
        if r.random() < .5:
            return Q(f"Work out {a} × {b}", [field_()], [num(a * b)])
        return Q(f"Work out {a * b} ÷ {a}", [field_()], [num(b)])
    if level == 3:
        a, b = nz(r, -20, 20), nz(r, -20, 20)
        op = pick(r, "+", "−", "×")
        v = a + b if op == "+" else a - b if op == "−" else a * b
        return Q(f"Work out {neg(a)} {op} ({neg(b)})", [field_()], [num(v)], method="Signs: two negatives multiplied make a positive; subtracting a negative is adding.")
    if level == 4:
        a, b = F(r.randint(11, 199), 10), F(r.randint(2, 9))
        if r.random() < .5:
            c = F(r.randint(11, 99), 100)
            return Q(f"Work out {show_number(a)} + {show_number(c)}", [field_()], [num(a + c)])
        return Q(f"Work out {show_number(a)} × {show_number(b)}", [field_()], [num(a * b)])
    a, b, c, d = r.randint(2, 9), r.randint(2, 9), r.randint(5, 12), r.randint(1, 4)
    return Q(f"Work out {a} + {b} × ({c} − {d})²", [field_()], [num(a + b * (c - d) ** 2)],
             method="Order of operations: brackets, then powers, then × and ÷, then + and −.")


@skill("fraction_add", "Adding and subtracting fractions", "Fractions", "fraction", "add fractions", "subtract fractions", "common denominator", "mixed number")
def _frac_add(level, r):
    def f():
        d = r.randint(2, 9)
        return F(r.randint(1, d - 1), d)
    if level == 1:
        d = r.randint(5, 12)
        a, b = F(r.randint(1, d // 2), d), F(r.randint(1, d // 2), d)
    elif level == 2:
        d1 = r.randint(2, 6)
        a, b = F(r.randint(1, d1 - 1), d1), F(r.randint(1, 2 * d1 - 1), 2 * d1)
    else:
        a, b = f(), f()
        while a.denominator == b.denominator:
            b = f()
    if level >= 4:
        a, b = a + r.randint(1, 3), b + r.randint(1, 2)
    op = pick(r, "+", "−") if level >= 2 else "+"
    if op == "−" and a < b:
        a, b = b, a
    v = a + b if op == "+" else a - b
    mixed = level >= 4
    return Q(f"Work out {showfrac(a, mixed)} {op} {showfrac(b, mixed)}. Give your answer in its simplest form"
             + (" as a mixed number." if mixed and v > 1 and v.denominator != 1 else "."),
             [field_(kind="mixed" if mixed else "fraction")], [frac(v, mixed)],
             method="Make the denominators the same (find a common multiple), then add or subtract the numerators. Simplify at the end.")


def showfrac(v: F, mixed: bool = False) -> str:
    if mixed and v > 1 and v.denominator != 1:
        w = v.numerator // v.denominator
        return f"{w} {v.numerator - w * v.denominator}/{v.denominator}"
    return f"{v.numerator}/{v.denominator}" if v.denominator != 1 else str(v.numerator)


@skill("fraction_multiply", "Multiplying and dividing fractions", "Fractions", "multiply fractions", "divide fractions", "reciprocal", "fraction")
def _frac_mul(level, r):
    def f():
        d = r.randint(2, 9)
        return F(r.randint(1, d - 1), d)
    a, b = f(), f()
    if level == 1:
        b = F(r.randint(2, 6))
    if level >= 4:
        a = a + r.randint(1, 3)
    op = "×" if level <= 2 or (level == 4 and r.random() < .5) else "÷"
    v = a * b if op == "×" else a / b
    mixed = level >= 4
    return Q(f"Work out {showfrac(a, mixed)} {op} {showfrac(b)}. Give your answer in its simplest form.",
             [field_(kind="fraction")], [frac(v)],
             method="To multiply, multiply the tops and the bottoms. To divide, flip the second fraction and multiply. Turn mixed numbers into top-heavy fractions first.")


@skill("fraction_of_amount", "Fractions of amounts", "Fractions", "fraction of", "of an amount")
def _frac_of(level, r):
    d = r.randint(2, 9 if level < 3 else 12)
    n = 1 if level == 1 else r.randint(1, d - 1)
    if level >= 4:
        whole = d * r.randint(3, 20)
        part = whole * F(n, d)
        return Q(f"{n}/{d} of a number is {show_number(part)}. What is the number?", [field_()], [num(whole)],
                 method="Divide by the top number to find one part, then multiply by the bottom number.")
    amount = d * r.randint(2, 15) if level <= 2 else r.randint(10, 99)
    return Q(f"Work out {n}/{d} of {amount}.", [field_()], [num(F(n, d) * amount)],
             method="Divide by the bottom number, then multiply by the top number.")


@skill("percentages", "Percentages of amounts", "Percentages", "percent", "percentage", "increase", "decrease", "discount", "vat")
def _pct(level, r):
    if level <= 3:
        p = pick(r, 10, 50, 25, 75) if level == 1 else r.choice(range(5, 100, 5)) if level == 2 else r.randint(1, 99)
        amt = r.randint(2, 30) * 20 if level < 3 else r.randint(20, 900)
        return Q(f"Work out {p}% of £{amt}.", [field_(prefix="£")], [num(F(p, 100) * amt)],
                 method="Find 1% (divide by 100) or 10% (divide by 10), then build up the percentage you need.")
    if level == 4:
        p, amt = r.choice(range(5, 60, 5)), r.randint(4, 60) * 10
        up = r.random() < .5
        v = amt * (1 + F(p, 100) * (1 if up else -1))
        return Q(f"A price of £{amt} is {'increased' if up else 'decreased'} by {p}%. What is the new price?",
                 [field_(prefix="£")], [num(v)], method="Multiply by the multiplier: 1 + p/100 to increase, 1 − p/100 to decrease.")
    p, orig = r.choice(range(5, 50, 5)), r.randint(4, 60) * 10
    after = orig * (1 + F(p, 100))
    return Q(f"After a {p}% increase, a price is £{show_number(after)}. What was the price before the increase?",
             [field_(prefix="£")], [num(orig)], method="Reverse percentage: the new price is (100 + p)%, so divide by 1 + p/100.")


@skill("percentage_change", "Percentage change", "Percentages", "percentage change", "profit", "loss", "percentage increase", "percentage decrease")
def _pct_change(level, r):
    a = r.randint(4, 40) * (10 if level <= 2 else 1)
    p = r.choice(range(5, 80, 5)) if level <= 3 else r.randint(1, 90)
    up = r.random() < .5
    b = a * (1 + F(p, 100) * (1 if up else -1))
    if level >= 4:
        b = F(round(b * 100), 100)
    change = (b - a) / a * 100
    dp = None if change.denominator == 1 else 1
    return Q(f"A value changes from {show_number(a)} to {show_number(b)}. Work out the percentage {'increase' if b > a else 'decrease'}"
             + (" to 1 decimal place." if dp else "."), [field_(suffix="%")], [num(abs(change), dp) if dp else num(abs(change))],
             method="Percentage change = difference ÷ original × 100.")


@skill("ratio", "Sharing in a ratio", "Ratio", "ratio", "share", "proportion")
def _ratio(level, r):
    names = r.sample(["Amy", "Ben", "Cara", "Dev", "Ella", "Finn", "Grace", "Hari"], 3)
    if level <= 3:
        a, b = r.randint(1, 5), r.randint(1, 7)
        while math.gcd(a, b) != 1 or a == b:
            a, b = r.randint(1, 5), r.randint(1, 7)
        total = (a + b) * r.randint(2, 20) * (1 if level < 3 else 5)
        part = F(total, a + b)
        return Q(f"{names[0]} and {names[1]} share £{total} in the ratio {a}:{b}. How much does each get?",
                 [field_(f"{names[0]}", prefix="£"), field_(f"{names[1]}", prefix="£")], [num(part * a), num(part * b)],
                 method="Add the parts of the ratio, divide the total by that to get one part, then multiply.")
    if level == 4:
        a, b, c = r.randint(1, 4), r.randint(1, 5), r.randint(1, 6)
        total = (a + b + c) * r.randint(3, 25)
        part = F(total, a + b + c)
        return Q(f"{names[0]}, {names[1]} and {names[2]} share £{total} in the ratio {a}:{b}:{c}. How much does each get?",
                 [field_(n, prefix="£") for n in names], [num(part * a), num(part * b), num(part * c)],
                 method="Add the parts, divide the total by that to get one part, then multiply.")
    a, b = r.randint(2, 7), r.randint(1, 6)
    while a == b:
        b = r.randint(1, 6)
    part = r.randint(2, 30)
    return Q(f"{names[0]} and {names[1]} share some money in the ratio {a}:{b}. {names[0] if a > b else names[1]} gets £{abs(a - b) * part} more. "
             f"How much money was shared altogether?", [field_(prefix="£")], [num((a + b) * part)],
             method="The difference in the ratio is worth the extra money. Find one part, then multiply by the total parts.")


@skill("powers", "Powers and roots", "Number", "power", "index", "indices", "square root", "cube", "squared", "exponent")
def _powers(level, r):
    if level == 1:
        a = r.randint(2, 15)
        if r.random() < .6:
            return Q(f"Work out {a}²", [field_()], [num(a * a)])
        a = r.randint(2, 6)
        return Q(f"Work out {a}³", [field_()], [num(a ** 3)])
    if level == 2:
        a = r.randint(2, 15)
        return Q(f"Work out √{a * a}", [field_()], [num(a)])
    base = r.randint(2, 9)
    m, n = r.randint(2, 9), r.randint(2, 9)
    if level == 3:
        op = pick(r, "×", "÷")
        if op == "÷" and m < n:
            m, n = n, m
        return Q(f"Write {base}^{m} {op} {base}^{n} as a single power of {base}.", [field_(prefix=f"{base}^")],
                 [num(m + n if op == "×" else m - n)], method="Multiplying: add the powers. Dividing: subtract the powers.")
    if level == 4:
        b, p = r.randint(2, 5), r.randint(1, 3)
        if r.random() < .5:
            return Q(f"Work out {b}^−{p}. Give your answer as a fraction.", [field_(kind="fraction")], [frac(F(1, b ** p))],
                     method="A negative power means 1 over: a^−n = 1/aⁿ.")
        return Q(f"Write ({base}^{m})^{n} as a single power of {base}.", [field_(prefix=f"{base}^")], [num(m * n)],
                 method="A power of a power: multiply the powers.")
    root, b = pick(r, 2, 3), r.randint(2, 5)
    p = pick(r, 1, 3) if root == 2 else pick(r, 1, 2, 4)
    return Q(f"Work out {b ** root}^({p}/{root})", [field_()], [num(b ** p)],
             method="The bottom of the fraction is the root, the top is the power: find the root first, then raise it to the power.")


@skill("hcf_lcm", "HCF and LCM", "Number", "hcf", "lcm", "highest common factor", "lowest common multiple", "prime factor", "factor", "multiple")
def _hcf(level, r):
    g = r.randint(2, 6 + 3 * level)
    a, b = g * r.randint(2, 9), g * r.randint(2, 11)
    while a == b:
        b = g * r.randint(2, 11)
    if level >= 5:
        c = g * r.randint(2, 7)
        while c in (a, b):
            c = g * r.randint(2, 13)
        want_h = r.random() < .5
        v = math.gcd(math.gcd(a, b), c) if want_h else math.lcm(a, b, c)
        return Q(f"Find the {'highest common factor (HCF)' if want_h else 'lowest common multiple (LCM)'} of {a}, {b} and {c}.",
                 [field_()], [num(v)], method="Write each number as a product of primes, then compare.")
    want_h = r.random() < .5
    return Q(f"Find the {'highest common factor (HCF)' if want_h else 'lowest common multiple (LCM)'} of {a} and {b}.",
             [field_()], [num(math.gcd(a, b) if want_h else math.lcm(a, b))],
             method="HCF: the biggest number that goes into both. LCM: the smallest number both go into. Prime factors help.")


@skill("rounding", "Rounding", "Number", "round", "rounding", "decimal places", "significant figures", "estimate")
def _round(level, r):
    if level == 1:
        n = r.randint(101, 9999)
        to = pick(r, 10, 100)
        v = int(Decimal(n / to).quantize(Decimal(1), ROUND_HALF_UP)) * to
        return Q(f"Round {n} to the nearest {to}.", [field_()], [num(v)])
    x = Decimal(r.randint(10000, 999999)) / Decimal(10 ** r.randint(2, 4))
    if level <= 3:
        dp = 1 if level == 2 else 2
        return Q(f"Round {x} to {dp} decimal place{'s' if dp > 1 else ''}.", [field_()], [num(F(x.quantize(Decimal(1).scaleb(-dp), ROUND_HALF_UP)))],
                 method="Look at the next digit: 5 or more rounds up.")
    sf = r.randint(1, 3)
    x = Decimal(r.randint(1001, 99999)) / Decimal(10 ** r.randint(0, 6))
    e = x.adjusted() - sf + 1
    v = x.scaleb(-e).quantize(Decimal(1), ROUND_HALF_UP).scaleb(e)
    return Q(f"Round {format(x.normalize(), 'f')} to {sf} significant figure{'s' if sf > 1 else ''}.", [field_()], [num(F(v))],
             method="Count significant figures from the first non-zero digit, then round; keep the place value with zeros.")


@skill("standard_form", "Standard form", "Number", "standard form", "scientific notation", "powers of ten")
def _std(level, r):
    a = F(r.randint(11, 99), 10)
    n = r.randint(2, 8) * (1 if level in (1, 3) or r.random() < .5 else -1)
    value = a * F(10) ** n
    if level <= 2:
        return Q(f"Write {show_number(value)} in standard form.", [field_(), field_("× 10^")], [num(a), num(n)],
                 method="Move the decimal point so the first number is between 1 and 10; the power is how many places it moved.")
    if level == 3:
        return Q(f"Write {show_number(a)} × 10^{n} as an ordinary number.", [field_()], [num(value)])
    b = F(r.randint(11, 49), 10)
    m = r.randint(-5, 9)
    prod = a * b if level == 4 else a / b
    p = n + m if level == 4 else n - m
    while prod >= 10:
        prod, p = prod / 10, p + 1
    while prod < 1:
        prod, p = prod * 10, p - 1
    prod_r = F(Decimal(prod.numerator / Decimal(prod.denominator)).quantize(Decimal("0.01"), ROUND_HALF_UP))
    if prod_r >= 10:
        prod_r, p = prod_r / 10, p + 1
    return Q(f"Work out ({show_number(a)} × 10^{n}) {'×' if level == 4 else '÷'} ({show_number(b)} × 10^{m}). "
             f"Give your answer in standard form, with the first number to 2 decimal places.",
             [field_(), field_("× 10^")], [num(prod_r), num(p)],
             method="Multiply (or divide) the front numbers, add (or subtract) the powers, then adjust so the front number is between 1 and 10.")


# --- algebra ---
@skill("substitution", "Substitution", "Algebra", "substitute", "substitution", "formula", "expression", "evaluate")
def _subst(level, r):
    x = r.randint(2, 9) if level < 4 else nz(r, -6, 6)
    y = r.randint(2, 9) if level < 4 else nz(r, -5, 5)
    a, b = r.randint(2, 9), r.randint(1, 12)
    if level == 1:
        return Q(f"Work out the value of {a}x + {b} when x = {neg(x)}.", [field_()], [num(a * x + b)])
    if level == 2 or level == 4:
        c = r.randint(2, 7)
        return Q(f"Work out the value of {a}x − {c}y when x = {neg(x)} and y = {neg(y)}.", [field_()], [num(a * x - c * y)],
                 method="Replace each letter with its number (use brackets for negatives), then work it out.")
    if level == 3:
        return Q(f"Work out the value of x² + {a}x when x = {neg(x)}.", [field_()], [num(x * x + a * x)])
    u, acc, t = r.randint(0, 20), r.randint(2, 10), r.randint(2, 12)
    return Q(f"v = u + at. Work out v when u = {u}, a = {acc} and t = {t}.", [field_("v =")], [num(u + acc * t)])


@skill("linear_equations", "Solving equations", "Algebra", "solve", "equation", "linear equation", "unknown", "both sides")
def _linear(level, r):
    x = F(r.randint(-9 if level >= 3 else 1, 12))
    a, b = r.randint(2, 9), nz(r, -15, 15) if level >= 3 else r.randint(1, 15)
    if level == 1:
        if r.random() < .5:
            return Q(f"Solve x + {b} = {neg(x + b)}", [field_("x =")], [num(x)])
        return Q(f"Solve {a}x = {neg(a * x)}", [field_("x =")], [num(x)])
    if level == 2:
        return Q(f"Solve {a}x {signed(b)} = {neg(a * x + b)}", [field_("x =")], [num(x)],
                 method="Undo the + or − first, then divide by the number in front of x.")
    if level == 3:
        c = r.randint(1, a - 1) if a > 2 else 1
        d = a * x + b - c * x
        return Q(f"Solve {a}x {signed(b)} = {c if c != 1 else ''}x {signed(d)}", [field_("x =")], [num(x)],
                 method="Get the x terms on one side and the numbers on the other, then divide.")
    if level == 4:
        c = nz(r, -8, 8)
        return Q(f"Solve {a}(x {signed(c)}) = {neg(a * (x + c))}", [field_("x =")], [num(x)],
                 method="Expand the bracket (or divide both sides by the number outside), then solve.")
    x = F(r.randint(-20, 20), r.randint(2, 5))
    c = r.randint(2, 6)
    return Q(f"Solve (x {signed(b)}) / {c} = {neg((x + b) / c)}. Give your answer as a number or fraction.",
             [field_("x =")], [num(x)], method="Multiply both sides by the number underneath, then undo the + or −.")


@skill("simultaneous_equations", "Simultaneous equations", "Algebra", "simultaneous", "elimination", "two equations")
def _simul(level, r):
    x, y = nz(r, -6, 9) if level >= 4 else r.randint(1, 9), nz(r, -6, 9) if level >= 4 else r.randint(1, 9)
    while True:
        a, b, c, d = r.randint(1, 5), r.randint(1, 5), r.randint(1, 5), r.randint(1, 5)
        if level <= 2:
            c, d = a, -b
        elif level == 3:
            c, d = r.randint(1, 4), b * pick(r, 1, -1)
        if a * d - b * c != 0:
            break
    e, f = a * x + b * y, c * x + d * y
    def eq(p, q, s):
        return f"{p if p != 1 else ''}x {'+' if q > 0 else '−'} {abs(q) if abs(q) != 1 else ''}y = {neg(s)}"
    return Q(f"Solve the simultaneous equations:\n{eq(a, b, e)}\n{eq(c, d, f)}", [field_("x ="), field_("y =")], [num(x), num(y)],
             method="Make the x or y numbers match (multiply an equation if needed), add or subtract to get rid of one letter, solve, then substitute back.")


@skill("expanding_brackets", "Expanding brackets", "Algebra", "expand", "brackets", "multiply out", "foil", "quadratic")
def _expand(level, r):
    a, b = nz(r, -9, 9), nz(r, -9, 9)
    if level == 1:
        k = r.randint(2, 9)
        return Q(f"Expand {k}(x {signed(a)})", [field_(kind="expr")], [expr({1: F(k), 0: F(k * a)})],
                 method="Multiply everything inside the bracket by the number outside.")
    if level == 2:
        return Q(f"Expand x(x {signed(a)})", [field_(kind="expr")], [expr({2: F(1), 1: F(a)})])
    if level == 3:
        return Q(f"Expand and simplify (x {signed(a)})(x {signed(b)})", [field_(kind="expr")], [expr({2: F(1), 1: F(a + b), 0: F(a * b)})],
                 method="Multiply each term in the first bracket by each term in the second (a grid helps), then collect like terms.")
    if level == 4:
        p, q = r.randint(2, 5), r.randint(1, 4)
        return Q(f"Expand and simplify ({p}x {signed(a)})({q if q != 1 else ''}x {signed(b)})", [field_(kind="expr")],
                 [expr({2: F(p * q), 1: F(p * b + a * q), 0: F(a * b)})], method="Multiply each term by each term, then collect like terms.")
    return Q(f"Expand and simplify (x {signed(a)})²", [field_(kind="expr")], [expr({2: F(1), 1: F(2 * a), 0: F(a * a)})],
             method="Write it as two brackets: (x + a)(x + a), then multiply out.")


@skill("nth_term", "Sequences and the nth term", "Algebra", "sequence", "nth term", "term to term", "arithmetic sequence")
def _nth(level, r):
    if level <= 4:
        d = r.randint(2, 9) * (1 if level < 4 else pick(r, 1, -1))
        c = r.randint(-5, 15)
        terms = [d * n + c for n in range(1, 6)]
        if level == 1:
            return Q(f"The sequence {', '.join(neg(t) for t in terms)}, … What is the next term?", [field_()], [num(d * 6 + c)])
        if level == 2:
            n = r.randint(10, 50)
            return Q(f"The nth term of a sequence is {d}n {signed(c)}. Work out the {n}th term.", [field_()], [num(d * n + c)])
        return Q(f"Find the nth term of {', '.join(neg(t) for t in terms)}, …", [field_(kind="expr")], [expr({1: F(d), 0: F(c)}, "n")],
                 method="The difference between terms goes in front of n; then adjust with a number so the first term is right.")
    a, b, c = r.randint(1, 3), r.randint(-4, 4), r.randint(-5, 5)
    terms = [a * n * n + b * n + c for n in range(1, 6)]
    return Q(f"Find the nth term of the quadratic sequence {', '.join(neg(t) for t in terms)}, …", [field_(kind="expr")],
             [expr({2: F(a), 1: F(b), 0: F(c)}, "n")],
             method="Half the second difference goes in front of n². Take an² away from each term and find the nth term of what's left.")


@skill("straight_lines", "Straight line graphs", "Algebra", "gradient", "straight line", "y = mx + c", "intercept", "midpoint", "coordinates")
def _lines(level, r):
    x1, y1 = r.randint(-6, 6), r.randint(-6, 6)
    dx = nz(r, -6, 6) if level >= 2 else r.randint(1, 6)
    m = F(nz(r, -4, 4)) if level <= 3 else F(nz(r, -6, 6), pick(r, 1, 2))
    x2, y2 = x1 + dx * m.denominator, y1 + dx * m.numerator
    if level <= 3:
        return Q(f"Find the gradient of the line through ({neg(x1)}, {neg(y1)}) and ({neg(x2)}, {neg(y2)}).", [field_()], [num(m)],
                 method="Gradient = change in y ÷ change in x.")
    if level == 4:
        c = y1 - m * x1
        return Q(f"A straight line goes through ({neg(x1)}, {neg(y1)}) and ({neg(x2)}, {neg(y2)}). Its equation is y = mx + c. Find m and c.",
                 [field_("m ="), field_("c =")], [num(m), num(c)],
                 method="Find the gradient m first, then put one point into y = mx + c to find c.")
    return Q(f"Find the midpoint of ({neg(x1)}, {neg(y1)}) and ({neg(x2)}, {neg(y2)}).", [field_("x ="), field_("y =")],
             [num(F(x1 + x2, 2)), num(F(y1 + y2, 2))], method="Add the x's and halve; add the y's and halve.")


# --- geometry ---
def triangle(angles_deg: tuple[float, float, float], sides=(None, None, None), angle_labels=(None, None, None), right: int | None = None,
             vertices=("A", "B", "C")) -> dict:
    """A triangle drawn to the right shape. angles at A, B, C; side i is opposite vertex i."""
    A, B, C = (math.radians(a) for a in angles_deg)
    c = 1.0
    b = c * math.sin(B) / math.sin(C)
    pts = [(0.0, 0.0), (c, 0.0), (b * math.cos(A), b * math.sin(A))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    s = 220 / max(w, h)
    pts = [(round(20 + (x - min(xs)) * s, 1), round(20 + (max(ys) - y) * s, 1)) for x, y in pts]
    return {"kind": "triangle", "pts": pts, "sides": list(sides), "angles": list(angle_labels), "right": right, "vertices": list(vertices)}


@skill("angles_triangles", "Angles in triangles", "Angles", "angle", "triangle", "isosceles", "equilateral", "exterior angle", "interior angle")
def _ang_tri(level, r):
    if level == 1:
        a, b = r.randint(25, 100), r.randint(25, 70)
        c = 180 - a - b
        return Q("Find the angle marked x.", [field_("x =", suffix="°")], [num(c)], triangle((a, b, c), angle_labels=(f"{a}°", f"{b}°", "x")),
                 method="Angles in a triangle add up to 180°.")
    if level == 2:
        base = r.randint(30, 80)
        apex = 180 - 2 * base
        if r.random() < .5:
            return Q("The triangle is isosceles (AC = BC). Find the angle marked x.", [field_("x =", suffix="°")], [num(base)],
                     triangle((base, base, apex), angle_labels=("x", None, f"{apex}°")),
                     method="An isosceles triangle has two equal base angles. Take the top angle from 180° and halve.")
        return Q("The triangle is isosceles (AC = BC). Find the angle marked x.", [field_("x =", suffix="°")], [num(apex)],
                 triangle((base, base, apex), angle_labels=(f"{base}°", None, "x")), method="The two base angles are equal; angles add up to 180°.")
    if level == 3:
        a = r.randint(20, 70)
        return Q("Find the angle marked x.", [field_("x =", suffix="°")], [num(90 - a)],
                 triangle((a, 90 - a, 90), angle_labels=(f"{a}°", "x", None), right=2), method="The right angle is 90°; angles add up to 180°.")
    if level == 4:
        p, q, s = r.randint(1, 4), r.randint(1, 4), r.randint(1, 4)
        while 180 % (p + q + s):
            p, q, s = r.randint(1, 4), r.randint(1, 4), r.randint(1, 4)
        x = 180 // (p + q + s)
        lab = lambda k: "x" if k == 1 else f"{k}x"
        return Q(f"The angles of a triangle are {lab(p)}, {lab(q)} and {lab(s)}. Find x.", [field_("x =", suffix="°")], [num(x)],
                 triangle((p * x, q * x, s * x), angle_labels=(lab(p), lab(q), lab(s))),
                 method="Add the expressions and set them equal to 180°, then solve for x.")
    a, b = r.randint(30, 80), r.randint(30, 80)
    return Q("Find the exterior angle at C (the angle outside the triangle, between side BC and AC extended).",
             [field_("x =", suffix="°")], [num(a + b)], triangle((a, b, 180 - a - b), angle_labels=(f"{a}°", f"{b}°", None)),
             method="An exterior angle equals the two opposite interior angles added together.")


@skill("angles_lines", "Angles on lines and around a point", "Angles", "straight line", "around a point", "vertically opposite", "parallel", "alternate", "corresponding")
def _ang_lines(level, r):
    if level == 1:
        a = r.randint(20, 160)
        return Q(f"Two angles lie on a straight line. One is {a}°. Find the other.", [field_(suffix="°")], [num(180 - a)],
                 method="Angles on a straight line add up to 180°.")
    if level == 2:
        a, b, c = r.randint(40, 120), r.randint(40, 100), r.randint(30, 80)
        return Q(f"Four angles meet at a point: {a}°, {b}°, {c}° and x. Find x.", [field_("x =", suffix="°")], [num(360 - a - b - c)],
                 method="Angles around a point add up to 360°.")
    if level == 3:
        a = r.randint(20, 160)
        return Q(f"Two straight lines cross. One of the angles is {a}°. Find the angle vertically opposite it, and the angle next to it.",
                 [field_("opposite", suffix="°"), field_("next to it", suffix="°")], [num(a), num(180 - a)],
                 method="Vertically opposite angles are equal; angles next to each other on a straight line add up to 180°.")
    a = r.randint(25, 155)
    kind = pick(r, "alternate", "corresponding", "co-interior")
    v = 180 - a if kind == "co-interior" else a
    return Q(f"A line crosses two parallel lines. One angle is {a}°. Find the {kind} angle to it.", [field_(suffix="°")], [num(v)],
             method="Alternate (Z) and corresponding (F) angles are equal. Co-interior (C) angles add up to 180°.")


@skill("polygons", "Angles in polygons", "Angles", "polygon", "interior angles", "exterior angles", "pentagon", "hexagon", "octagon")
def _poly(level, r):
    n = r.choice([5, 6, 8, 9, 10, 12, 15, 18, 20, 24, 30, 36])
    names = {5: "pentagon", 6: "hexagon", 8: "octagon", 9: "nonagon", 10: "decagon", 12: "dodecagon"}
    name = names.get(n, f"{n}-sided polygon")
    if level <= 2:
        return Q(f"Work out the sum of the interior angles of a {name}.", [field_(suffix="°")], [num((n - 2) * 180)],
                 method="Sum of interior angles = (n − 2) × 180°.")
    if level == 3:
        return Q(f"Work out the size of each exterior angle of a regular {name}.", [field_(suffix="°")], [num(F(360, n))],
                 method="Exterior angles add up to 360°, so each one is 360° ÷ n.")
    if level == 4:
        return Q(f"Work out the size of each interior angle of a regular {name}.", [field_(suffix="°")], [num(180 - F(360, n))],
                 method="Find the exterior angle (360 ÷ n), then interior = 180° − exterior.")
    return Q(f"Each interior angle of a regular polygon is {show_number(180 - F(360, n))}°. How many sides does it have?",
             [field_(suffix="sides")], [num(n)], method="Exterior angle = 180° − interior. Number of sides = 360 ÷ exterior angle.")


@skill("area", "Area and perimeter", "Shapes", "area", "perimeter", "rectangle", "trapezium", "parallelogram", "circle", "circumference")
def _area(level, r):
    if level == 1:
        a, b = r.randint(2, 15), r.randint(2, 15)
        if r.random() < .5:
            return Q(f"A rectangle is {a} cm by {b} cm. Work out its area.", [field_(suffix="cm²")], [num(a * b)])
        return Q(f"A rectangle is {a} cm by {b} cm. Work out its perimeter.", [field_(suffix="cm")], [num(2 * (a + b))])
    if level == 2:
        b, h = r.randint(3, 20), r.randint(2, 15)
        return Q(f"A triangle has base {b} cm and perpendicular height {h} cm. Work out its area.", [field_(suffix="cm²")],
                 [num(F(b * h, 2))], method="Area of a triangle = ½ × base × height.")
    if level == 3:
        a, b, h = r.randint(3, 12), r.randint(5, 18), r.randint(2, 10)
        return Q(f"A trapezium has parallel sides {a} cm and {b} cm, and height {h} cm. Work out its area.", [field_(suffix="cm²")],
                 [num(F((a + b) * h, 2))], method="Area of a trapezium = ½ × (a + b) × h.")
    rad = r.randint(2, 15)
    if level == 4:
        if r.random() < .5:
            return Q(f"A circle has radius {rad} cm. Work out its area to 1 decimal place.", [field_(suffix="cm²")],
                     [num(math.pi * rad * rad, 1)], method="Area of a circle = π × r².")
        return Q(f"A circle has diameter {2 * rad} cm. Work out its circumference to 1 decimal place.", [field_(suffix="cm")],
                 [num(math.pi * 2 * rad, 1)], method="Circumference = π × diameter.")
    return Q(f"A semicircle has diameter {2 * rad} cm. Work out its perimeter (the curved part and the straight edge) to 1 decimal place.",
             [field_(suffix="cm")], [num(math.pi * rad + 2 * rad, 1)],
             method="Half the circumference (π × d ÷ 2), then add the diameter.")


@skill("pythagoras", "Pythagoras' theorem", "Triangles", "pythagoras", "hypotenuse", "right-angled triangle", "right angled")
def _pyth(level, r):
    triples = [(3, 4, 5), (5, 12, 13), (8, 15, 17), (7, 24, 25), (20, 21, 29), (9, 40, 41)]
    if level in (1, 3):
        a, b, c = r.choice(triples)
        k = r.randint(1, 3)
        a, b, c = a * k, b * k, c * k
    else:
        a, b = r.randint(3, 20), r.randint(3, 20)
        c = math.hypot(a, b)
    ang = math.degrees(math.atan2(a, b))
    hyp = level <= 2 or level == 5 and r.random() < .5
    unit = "cm"
    if hyp:
        d = triangle((ang, 90 - ang, 90), sides=(f"{a} {unit}", f"{b} {unit}", "x"), right=2)
        return Q("Find the length x." + (" Give your answer to 1 decimal place." if level != 1 else ""), [field_("x =", suffix=unit)],
                 [num(c) if level == 1 else num(math.hypot(a, b), 1)], d,
                 method="Hypotenuse² = a² + b². Square the two shorter sides, add, then square root.")
    d = triangle((ang, 90 - ang, 90), sides=("x", f"{b} {unit}", f"{show_number(c) if isinstance(c, int) else round(c, 1)} {unit}"), right=2)
    if level == 3:
        return Q("Find the length x.", [field_("x =", suffix=unit)], [num(a)], d,
                 method="Shorter side² = hypotenuse² − other side². Subtract, then square root.")
    hyp_len = round(c, 1) if not isinstance(c, int) else c
    side = math.sqrt(hyp_len ** 2 - b ** 2)
    d["sides"][2] = f"{hyp_len} {unit}"
    return Q("Find the length x. Give your answer to 1 decimal place.", [field_("x =", suffix=unit)], [num(side, 1)], d,
             method="Shorter side² = hypotenuse² − other side². Subtract, then square root.")


@skill("trigonometry", "Trigonometry (SOH CAH TOA)", "Triangles", "trigonometry", "sin", "cos", "tan", "soh cah toa", "sohcahtoa")
def _trig(level, r):
    ang = r.randint(20, 70)
    unit = "cm"
    if level <= 3:
        ratio = ["tan", "sin", "cos"][level - 1] if level < 3 else pick(r, "sin", "cos", "tan")
        known = r.randint(4, 20)
        # A at the left (angle ang), right angle at C. Opposite = BC (side a), adjacent = AC (side b), hypotenuse = AB (side c).
        if ratio == "tan":
            x = known * math.tan(math.radians(ang))
            sides = ("x", f"{known} {unit}", None)
        elif ratio == "sin":
            x = known * math.sin(math.radians(ang))
            sides = ("x", None, f"{known} {unit}")
        else:
            x = known * math.cos(math.radians(ang))
            sides = (None, "x", f"{known} {unit}")
        d = triangle((ang, 90 - ang, 90), sides=sides, angle_labels=(f"{ang}°", None, None), right=2)
        return Q("Find the length x. Give your answer to 1 decimal place.", [field_("x =", suffix=unit)], [num(x, 1)], d,
                 method="Label the sides opposite, adjacent and hypotenuse from the angle, pick SOH, CAH or TOA, then rearrange.")
    o, a = r.randint(3, 15), r.randint(3, 15)
    if level == 4:
        d = triangle((math.degrees(math.atan2(o, a)), 90 - math.degrees(math.atan2(o, a)), 90),
                     sides=(f"{o} {unit}", f"{a} {unit}", None), angle_labels=("x", None, None), right=2)
        return Q("Find the angle x. Give your answer to 1 decimal place.", [field_("x =", suffix="°")], [num(math.degrees(math.atan2(o, a)), 1)], d,
                 method="Opposite and adjacent: use tan. x = tan⁻¹(opposite ÷ adjacent).")
    h = o + r.randint(2, 10)
    angx = math.degrees(math.asin(o / h))
    d = triangle((angx, 90 - angx, 90), sides=(f"{o} {unit}", None, f"{h} {unit}"), angle_labels=("x", None, None), right=2)
    return Q("Find the angle x. Give your answer to 1 decimal place.", [field_("x =", suffix="°")], [num(angx, 1)], d,
             method="Opposite and hypotenuse: use sin. x = sin⁻¹(opposite ÷ hypotenuse).")


@skill("volume", "Volume", "Shapes", "volume", "cuboid", "prism", "cylinder", "cone", "sphere", "capacity")
def _vol(level, r):
    a, b, c = r.randint(2, 12), r.randint(2, 12), r.randint(2, 12)
    if level == 1:
        return Q(f"A cuboid is {a} cm by {b} cm by {c} cm. Work out its volume.", [field_(suffix="cm³")], [num(a * b * c)],
                 method="Volume of a cuboid = length × width × height.")
    if level == 2:
        return Q(f"A prism has a cross-section of area {a * b} cm² and length {c} cm. Work out its volume.", [field_(suffix="cm³")],
                 [num(a * b * c)], method="Volume of a prism = area of cross-section × length.")
    if level == 3:
        return Q(f"A triangular prism has a triangle cross-section with base {a} cm and height {b} cm. The prism is {c} cm long. "
                 "Work out its volume.", [field_(suffix="cm³")], [num(F(a * b * c, 2))],
                 method="Find the triangle's area (½ × base × height), then multiply by the length.")
    if level == 4:
        return Q(f"A cylinder has radius {a} cm and height {c} cm. Work out its volume to 1 decimal place.", [field_(suffix="cm³")],
                 [num(math.pi * a * a * c, 1)], method="Volume of a cylinder = π × r² × h.")
    if r.random() < .5:
        return Q(f"A sphere has radius {a} cm. Work out its volume to 1 decimal place.", [field_(suffix="cm³")],
                 [num(4 / 3 * math.pi * a ** 3, 1)], method="Volume of a sphere = 4/3 × π × r³.")
    return Q(f"A cone has radius {a} cm and height {c} cm. Work out its volume to 1 decimal place.", [field_(suffix="cm³")],
             [num(math.pi * a * a * c / 3, 1)], method="Volume of a cone = ⅓ × π × r² × h.")


# --- statistics & probability ---
@skill("averages", "Averages and range", "Statistics", "mean", "median", "mode", "range", "average")
def _avg(level, r):
    n = r.randint(5, 9)
    data = [r.randint(1, 30) for _ in range(n)]
    shown = ", ".join(map(str, data))
    if level == 1:
        return Q(f"Find the range of: {shown}", [field_()], [num(max(data) - min(data))], method="Range = biggest − smallest.")
    if level == 2:
        s = sorted(data)
        med = F(s[n // 2]) if n % 2 else F(s[n // 2 - 1] + s[n // 2], 2)
        return Q(f"Find the median of: {shown}", [field_()], [num(med)], method="Put them in order; the median is the middle one (or halfway between the middle two).")
    if level == 3:
        return Q(f"Find the mean of: {shown}" + ("" if sum(data) % n == 0 else ". Give your answer to 1 decimal place."),
                 [field_()], [num(F(sum(data), n)) if sum(data) % n == 0 else num(sum(data) / n, 1)],
                 method="Mean = add them all up, then divide by how many there are.")
    if level == 4:
        m = r.randint(5, 15)
        known = [r.randint(1, 25) for _ in range(4)]
        missing = 5 * m - sum(known)
        while missing < 0:
            m += 3
            missing = 5 * m - sum(known)
        return Q(f"Five numbers have a mean of {m}. Four of them are {', '.join(map(str, known))}. What is the fifth number?",
                 [field_()], [num(missing)], method="Total = mean × how many. Take away the numbers you know.")
    vals = sorted(r.sample(range(0, 6), 3))
    freq = [r.randint(1, 9) for _ in vals]
    total = sum(v * f for v, f in zip(vals, freq))
    rows = "; ".join(f"{v} goals: {f} matches" for v, f in zip(vals, freq))
    mean = F(total, sum(freq))
    return Q(f"A team's results: {rows}. Work out the mean number of goals per match"
             + ("." if mean.denominator in (1, 2, 4, 5) else " to 1 decimal place."),
             [field_()], [num(mean) if mean.denominator in (1, 2, 4, 5) else num(total / sum(freq), 1)],
             method="Multiply each value by its frequency, add those up, then divide by the total frequency.")


@skill("probability", "Probability", "Probability", "probability", "chance", "likely", "dice", "coin", "spinner", "expected")
def _prob(level, r):
    cols = r.sample(["red", "blue", "green", "yellow", "white"], 3)
    counts = [r.randint(1, 9) for _ in cols]
    total = sum(counts)
    bag = ", ".join(f"{c} {col}" for c, col in zip(counts, cols))
    if level == 1:
        return Q(f"A bag has {bag} counters. One is taken at random. What is the probability it is {cols[0]}? Give it as a fraction in its simplest form.",
                 [field_(kind="fraction")], [frac(F(counts[0], total))], method="Probability = number of ways it can happen ÷ total.")
    if level == 2:
        return Q(f"A bag has {bag} counters. One is taken at random. What is the probability it is NOT {cols[1]}? Give it as a fraction in its simplest form.",
                 [field_(kind="fraction")], [frac(1 - F(counts[1], total))], method="P(not A) = 1 − P(A).")
    if level == 3:
        p = F(r.randint(1, 9), 10)
        q = F(r.randint(1, 9), 10)
        return Q(f"P(rain on Monday) = {show_number(p)} and P(rain on Tuesday) = {show_number(q)}, independently. "
                 "What is the probability it rains on both days?", [field_()], [num(p * q)],
                 method="For independent events, P(A and B) = P(A) × P(B).")
    if level == 4:
        sides = pick(r, 4, 6, 8, 10)
        n = sides * r.randint(5, 50)
        return Q(f"A fair {sides}-sided spinner is spun {n} times. How many times would you expect it to land on 1?", [field_()],
                 [num(F(n, sides))], method="Expected number = probability × number of tries.")
    ps = [F(r.randint(5, 30), 100) for _ in range(3)]
    while sum(ps) >= 1:
        ps = [F(r.randint(5, 30), 100) for _ in range(3)]
    shown = ", ".join(f"{c} = {show_number(p)}" for c, p in zip(cols, ps))
    return Q(f"A spinner can land on {', '.join(cols)} or black. P({shown}). Find P(black).", [field_()], [num(1 - sum(ps))],
             method="All the probabilities add up to 1, so take the others away from 1.")


# ---------- second check: every answer is checked against its own working ----------
def self_check(q: Q) -> bool:
    """A question is only used if its stored answer passes our own marking (catches any slip in
    how an answer was written down, e.g. a rounding or a fraction not in its simplest form)."""
    for a in q.answers:
        if a["kind"] == "number":
            typed = a["value"] if "dp" in a else show_number(F(a["exact"]))
        elif a["kind"] == "fraction":
            typed = [str(a["n"]), str(a["d"])]
        elif a["kind"] == "mixed":
            n, d = a["n"], a["d"]
            w = n // d if n > d and d != 1 else 0
            typed = [str(w) if w else "", str(n - w * d), str(d)]
        else:
            typed = show_answer(a).replace("−", "-")
        if not check_field(a, typed):
            return False
    return len(q.fields) == len(q.answers)


def make_question(skill_id: str, level: int, rng: random.Random | None = None) -> Q:
    rng = rng or random.Random()
    for _ in range(20):
        q = SKILLS[skill_id].make(max(1, min(5, level)), rng)
        if self_check(q):
            return q
    raise RuntimeError(f"couldn't make a good {skill_id} question")


def catalogue_text() -> str:
    return "\n".join(f"- {s.id}: {s.name} ({s.topic})" for s in SKILLS.values())


def match_skills(text: str) -> list[str]:
    """Skills whose keywords appear in a lesson's topics and notes (no AI needed)."""
    t = " " + text.lower() + " "
    scored = []
    for s in SKILLS.values():
        hits = sum(1 for k in s.keywords if re.search(rf"\b{re.escape(k)}", t))
        if hits:
            scored.append((hits, s.id))
    return [sid for _, sid in sorted(scored, reverse=True)[:6]]


# ---------- what each person knows ----------
class Progress:
    """Per person: a level for each skill, and a log of answers."""

    def __init__(self, study_dir: Path):
        self.dir = study_dir
        self._lock = threading.Lock()

    def _path(self, pid: str) -> Path:
        return self.dir / pid / "progress.json"

    def load(self, pid: str) -> dict:
        p = self._path(pid)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (OSError, ValueError):
                pass
        return {"skills": {}, "log": []}

    def save(self, pid: str, data: dict) -> None:
        p = self._path(pid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)

    def level(self, pid: str, skill_id: str) -> float:
        return float(self.load(pid)["skills"].get(skill_id, {}).get("level", 1.5))

    def record(self, pid: str, skill_id: str, level: int, right: bool, lesson: str = "", helped: bool = False) -> float:
        with self._lock:
            data = self.load(pid)
            s = data["skills"].setdefault(skill_id, {"level": 1.5, "seen": 0, "right": 0, "streak": 0, "last": 0})
            s["seen"] += 1
            s["last"] = time.time()
            if right and not helped:
                s["right"] += 1
                s["streak"] = max(1, s["streak"] + 1)
                s["level"] = min(5.0, s["level"] + (0.5 if level >= s["level"] else 0.2))
            elif right:  # right, but after Help: a small step
                s["right"] += 1
                s["streak"] = 0
                s["level"] = min(5.0, s["level"] + 0.1)
            else:
                s["streak"] = min(-1, s["streak"] - 1) if s["streak"] <= 0 else -1
                s["level"] = max(1.0, s["level"] - (0.4 if level <= s["level"] + 0.5 else 0.2))
            data["log"] = (data["log"] + [{"t": time.time(), "skill": skill_id, "level": level, "right": right,
                                           "helped": helped, "lesson": lesson}])[-1000:]
            self.save(pid, data)
            return s["level"]

    def summary(self, pid: str) -> list[dict]:
        data = self.load(pid)
        out = []
        for sid, s in data["skills"].items():
            if sid not in SKILLS:
                continue
            out.append({"skill": sid, "name": SKILLS[sid].name, "topic": SKILLS[sid].topic, "level": round(s["level"], 1),
                        "seen": s["seen"], "right": s["right"], "last": s["last"]})
        return sorted(out, key=lambda x: x["level"])

    def recent(self, pid: str, days: float = 7) -> dict:
        cut = time.time() - days * 86400
        log = [x for x in self.load(pid)["log"] if x["t"] >= cut]
        return {"answered": len(log), "right": sum(1 for x in log if x["right"]),
                "days": len({time.strftime("%Y-%m-%d", time.localtime(x["t"])) for x in log})}


def target_level(level: float, streak: int) -> int:
    """A little above where they are (that's where learning happens); more after a run of right answers."""
    stretch = 0.5 + (0.5 if streak >= 2 else 0) - (0.5 if streak <= -2 else 0)
    return max(1, min(5, int(level + stretch)))


# ---------- a revising session ----------
@dataclass
class Live:
    pid: str
    skill: str
    level: int
    q: Q
    lesson: str
    helped: bool = False
    help_steps: list[str] | None = None
    answered: bool = False
    tries: int = 0


class Practice:
    ROUND = 10  # questions per round

    def __init__(self, study_dir: Path):
        self.progress = Progress(study_dir)
        self.live: dict[str, Live] = {}
        self.rounds: dict[str, dict] = {}  # round id -> {pid, lesson, skills, done, right, last_skill}

    def start(self, pid: str, lesson_id: str, skills: list[str]) -> dict:
        skills = [s for s in skills if s in SKILLS]
        if not skills:
            raise ValueError("no skills")
        rid = secrets.token_hex(6)
        self.rounds[rid] = {"pid": pid, "lesson": lesson_id, "skills": skills, "done": 0, "right": 0, "last": "", "results": []}
        return self.next(rid)

    def next(self, rid: str) -> dict:
        rd = self.rounds.get(rid)
        if not rd:
            raise KeyError(rid)
        if rd["done"] >= self.ROUND:
            return {"round": rid, "finished": True, "right": rd["right"], "total": rd["done"], "results": rd["results"],
                    "skills": [{"name": SKILLS[s].name, "level": round(self.progress.level(rd["pid"], s), 1)} for s in rd["skills"]]}
        data = self.progress.load(rd["pid"])["skills"]
        choices = [s for s in rd["skills"] if s != rd["last"]] or rd["skills"]
        # Weakest first, but mixed up a bit so it doesn't feel like a list.
        weights = [1.0 + (5 - data.get(s, {}).get("level", 1.5)) ** 1.5 for s in choices]
        sid = random.choices(choices, weights)[0]
        st = data.get(sid, {"level": 1.5, "streak": 0})
        level = target_level(st["level"], st.get("streak", 0))
        q = make_question(sid, level)
        qid = secrets.token_hex(6)
        self.live[qid] = Live(rd["pid"], sid, level, q, rd["lesson"])
        rd["last"] = sid
        if len(self.live) > 500:
            for k in list(self.live)[:200]:
                self.live.pop(k, None)
        return {"round": rid, "finished": False, "number": rd["done"] + 1, "of": self.ROUND,
                "question": {"id": qid, "skill": SKILLS[sid].name, "level": level, "text": q.text, "fields": q.fields,
                             "diagram": q.diagram}}

    def answer(self, rid: str, qid: str, typed: list) -> dict:
        rd, lv = self.rounds.get(rid), self.live.get(qid)
        if not rd or not lv or lv.pid != rd["pid"]:
            raise KeyError(qid)
        if lv.answered:
            raise ValueError("already answered")
        typed = (typed or []) + [""] * len(lv.q.answers)
        right = all(check_field(a, t) for a, t in zip(lv.q.answers, typed))
        lv.tries += 1
        if not right and lv.tries == 1:
            return {"right": False, "try_again": True}  # one more go before the answer is shown
        lv.answered = True
        new_level = self.progress.record(lv.pid, lv.skill, lv.level, right, lv.lesson, lv.helped)
        rd["done"] += 1
        rd["right"] += int(right)
        rd["results"].append(right)
        return {"right": right, "answer": [show_answer(a) for a in lv.q.answers], "level": round(new_level, 1)}

    def question(self, qid: str, pid: str) -> Live:
        lv = self.live.get(qid)
        if not lv or lv.pid != pid:
            raise KeyError(qid)
        return lv


HELP_PROMPT = """You are a friendly maths tutor helping a secondary-school student who is stuck.
Question: {question}
The correct answer (worked out by a computer, definitely right): {answer}
Method: {method}

Explain how to do THIS question in 3 to 6 short steps, in simple, encouraging language. Each step does one
thing and shows the working with the real numbers. The last step reaches exactly the correct answer above.
Don't use LaTeX; write maths plainly (×, ÷, ², √, fractions like 3/4).
Reply with JSON only: {{"steps": ["step 1", "step 2", ...]}}"""
