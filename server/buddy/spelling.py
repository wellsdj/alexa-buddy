"""Spelling a password out loud, one character at a time (for joining Wi-Fi by voice).

Everything here runs on this computer: the password is never sent to Claude or saved in Buddy's
conversation memory.
"""
from __future__ import annotations

import re

LETTER_NAMES = {
    "a": "a", "ay": "a", "bee": "b", "be": "b", "b": "b", "see": "c", "sea": "c", "cee": "c", "c": "c",
    "dee": "d", "d": "d", "ee": "e", "e": "e", "eff": "f", "ef": "f", "f": "f", "gee": "g", "g": "g",
    "aitch": "h", "haitch": "h", "h": "h", "eye": "i", "i": "i", "jay": "j", "j": "j", "kay": "k", "k": "k",
    "el": "l", "elle": "l", "l": "l", "em": "m", "m": "m", "en": "n", "n": "n", "oh": "o", "o": "o",
    "pee": "p", "pea": "p", "p": "p", "queue": "q", "cue": "q", "q": "q", "are": "r", "ar": "r", "r": "r",
    "ess": "s", "es": "s", "s": "s", "tee": "t", "tea": "t", "t": "t", "you": "u", "u": "u", "vee": "v",
    "v": "v", "ex": "x", "x": "x", "why": "y", "wye": "y", "y": "y", "zed": "z", "zee": "z", "z": "z", "w": "w",
}
NATO = {
    "alpha": "a", "alfa": "a", "bravo": "b", "charlie": "c", "delta": "d", "echo": "e", "foxtrot": "f",
    "golf": "g", "hotel": "h", "india": "i", "juliet": "j", "juliett": "j", "kilo": "k", "lima": "l", "mike": "m",
    "november": "n", "oscar": "o", "papa": "p", "quebec": "q", "romeo": "r", "sierra": "s", "tango": "t",
    "uniform": "u", "victor": "v", "whiskey": "w", "whisky": "w", "xray": "x", "x-ray": "x", "yankee": "y",
    "zulu": "z",
}
DIGITS = {"zero": "0", "nought": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
          "seven": "7", "eight": "8", "nine": "9"}
# Longest first, so "question mark" wins over "question".
SYMBOLS = [
    ("exclamation mark", "!"), ("exclamation point", "!"), ("question mark", "?"), ("forward slash", "/"),
    ("back slash", "\\"), ("full stop", "."), ("open bracket", "("), ("left bracket", "("),
    ("close bracket", ")"), ("right bracket", ")"), ("quotation mark", '"'), ("pound sign", "£"),
    ("dollar sign", "$"), ("at sign", "@"), ("and sign", "&"), ("hash sign", "#"), ("percent sign", "%"),
    ("plus sign", "+"), ("equals sign", "="),
    ("exclamation", "!"), ("bang", "!"), ("at", "@"), ("hash", "#"), ("hashtag", "#"), ("dollar", "$"),
    ("percent", "%"), ("ampersand", "&"), ("star", "*"), ("asterisk", "*"), ("dash", "-"), ("hyphen", "-"),
    ("minus", "-"), ("underscore", "_"), ("dot", "."), ("period", "."), ("point", "."), ("comma", ","),
    ("plus", "+"), ("equals", "="), ("slash", "/"), ("backslash", "\\"), ("space", " "), ("colon", ":"),
    ("semicolon", ";"), ("apostrophe", "'"), ("quote", '"'), ("tilde", "~"), ("caret", "^"), ("hat", "^"),
    ("pound", "£"),
]
SYMBOL_NAMES = {}
for _w, _c in SYMBOLS:
    SYMBOL_NAMES.setdefault(_c, _w if _c != " " else "space")
SYMBOL_NAMES.update({"@": "at sign", "#": "hash", ".": "dot", "!": "exclamation mark", "£": "pound sign"})
UPPER = {"capital", "uppercase", "upper", "big", "cap"}
LOWER = {"lowercase", "lower", "small", "little"}
FILLER = {"the", "letter", "character", "sign", "symbol", "then", "and", "next", "is", "it's", "its", "yes", "yeah",
          "yep", "okay", "ok", "um", "uhm", "uh", "er", "erm", "a", "an", "number", "digit", "case", "so", "now"}
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
            "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12, "thirteenth": 13, "fourteenth": 14,
            "fifteenth": 15, "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19, "twentieth": 20}


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'").replace("x ray", "xray")
    t = re.sub(r"\bupper case\b", "uppercase", t)
    t = re.sub(r"\blower case\b", "lowercase", t)
    t = re.sub(r"\bdouble (u|you)\b", " w ", t)
    t = re.sub(r"\bhey,? buddy\b", " ", t)
    return t


def parse_chars(text: str) -> list[str]:
    """Characters spoken in text, in order: "capital D, number 3, at sign" -> ["D", "3", "@"]."""
    t = _norm(text)
    out: list[str] = []
    case = None
    # Symbols said as words first (several words long), keeping their place in the sentence.
    for word, ch in SYMBOLS:
        t = re.sub(rf"(?<![a-z]){re.escape(word)}(?![a-z])", f" \x00{ord(ch)} ", t)
    for raw in re.split(r"[\s,;]+", t):
        w = raw.strip(".!?:\"'()")
        if raw.startswith("\x00"):
            out.append(chr(int(raw[1:])))
            case = None
            continue
        if not w:
            continue
        if w in UPPER:
            case = "upper"
            continue
        if w in LOWER:
            case = "lower"
            continue
        if w == "a" and case is None and out == [] and len(t.split()) > 1:
            continue  # "a capital D": the "a" is just a word
        letter = NATO.get(w) or LETTER_NAMES.get(w)
        if letter and not (w in FILLER and case is None and w != "a"):
            out.append(letter.upper() if case == "upper" else letter)
            case = None
            continue
        if w in DIGITS:
            out.append(DIGITS[w])
            case = None
            continue
        if w.isdigit():
            out.extend(w)
            case = None
            continue
        if len(w) == 1 and not w.isalnum():
            out.append(w)
            continue
        if w in FILLER:
            continue
        if len(raw) == 1 and raw.isalpha():
            out.append(raw.upper() if case == "upper" else raw)
            case = None
    return out


def describe(ch: str) -> str:
    if ch.isdigit():
        return f"number {ch}"
    if ch.isalpha():
        return f"capital {ch}" if ch.isupper() else f"small {ch}"
    return SYMBOL_NAMES.get(ch, f"the symbol {ch}")


def read_back(chars: list[str]) -> str:
    return ", ".join(describe(c) for c in chars)


def _position(word: str, n: int) -> int | None:
    """1-based place from "3", "3rd", "third", "last"."""
    w = word.strip().lower()
    if w in ("last", "final"):
        return n or None
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)?", w)
    if m:
        return int(m.group(1))
    if w in ORDINALS:
        return ORDINALS[w]
    if w in DIGITS:
        return int(DIGITS[w])
    return None


def correct(chars: list[str], text: str) -> tuple[list[str], str] | None:
    """Apply a spoken correction. Returns (new characters, what was done) or None if not understood.
    "number 3 should be capital D", "the third one is a 3", "change the small d to a 3",
    "the lowercase d needs to be a capital D", "delete the last one", "add a 7 at the end"."""
    t = _norm(text).strip().strip(".!?")
    t = re.sub(r"^(no|nope|nearly|almost|not quite|in the middle|actually)[, ]+", "", t)
    t = re.sub(r"^(no|nope)[, ]+", "", t)
    chars = list(chars)
    n = len(chars)
    m = re.search(r"(?:delete|remove|take out|get rid of) (?:the )?(?:number |character )?(\w+)(?: one| character)?$", t)
    if m:
        p = _position(m.group(1), n)
        if p and 1 <= p <= n:
            gone = chars.pop(p - 1)
            return chars, f"Took out {describe(gone)}"
    m = re.search(r"add (.+?) (?:at|on|to) the end$", t)
    if m and parse_chars(m.group(1)):
        new = parse_chars(m.group(1))
        return chars + new, f"Added {read_back(new)} at the end"
    m = re.search(r"(?:change )?(?:the )?(?:number |character )?(\w+)(?: one| character| letter)? "
                  r"(?:should be|should've been|is meant to be|needs to be|must be|is|to|becomes|was meant to be) (.+)$", t)
    if m:
        p = _position(m.group(1), n)
        new = parse_chars(m.group(2))
        if p and 1 <= p <= n and new:
            old = chars[p - 1]
            chars[p - 1:p] = new
            return chars, f"Number {p}, {describe(old)}, is now {read_back(new)}"
    # "the small d needs to be a 3": find the character they mean by what it is.
    m = re.search(r"(?:change )?(.+?) (?:should be|should've been|needs to be|is meant to be|must be|to|was meant to be) (.+)$", t)
    if m:
        which, new = parse_chars(m.group(1)), parse_chars(m.group(2))
        if len(which) == 1 and new:
            spots = [i for i, c in enumerate(chars) if c == which[0]]
            if not spots:
                spots = [i for i, c in enumerate(chars) if c.lower() == which[0].lower()]
            if len(spots) == 1:
                i = spots[0]
                old = chars[i]
                chars[i:i + 1] = new
                return chars, f"Number {i + 1}, {describe(old)}, is now {read_back(new)}"
            if len(spots) > 1:
                return None  # more than one: they need to say which number
    return None
