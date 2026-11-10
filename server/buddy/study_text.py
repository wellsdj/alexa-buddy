"""Study Buddy's words and rules, shared by Buddy's Mac server and the Study Buddy website.
Only the standard library, so the website can use it without Buddy's audio tools."""
from __future__ import annotations

import hashlib
import json
import re

CHECK_PROMPT = """Here is the start of an automatic transcript of a recording made at school. Give it a name.
Reply with JSON only: {{"subject": "the school subject, one or two words, e.g. Maths (your best guess)",
"about": "what it was about in under 8 words, starting with a small letter"}}

Transcript:
\"\"\"
{text}
\"\"\""""

LESSON_PROMPT = """You help a secondary-school student revise. Below is {what} from a {subject} lesson.
It may be messy: several people talking, gaps and mistakes from automatic transcription. Work out what
was actually taught, and ignore chat that isn't about the lesson.

Reply with JSON only, in exactly this shape:
{{
  "title": "short title for the lesson, 3-7 words",
  "subject": "the school subject, one or two words, e.g. Maths, English, Chemistry",
  "about": "what the lesson was about in under 8 words, starting with a small letter, e.g. calculating angles in triangles",
  "summary": "2-3 plain sentences: what the lesson was about",
  "notes": [{{"heading": "...", "points": ["clear revision note", "..."]}}],
  "topics": [{{"name": "short topic name", "summary": "one sentence"}}],
  "skills": ["skill ids from the list below that this lesson taught, if it's a maths lesson"],
  "questions": [{{"topic": "one of the topic names", "level": 1, "question": "...",
                  "answer": "the correct answer, short", "why": "one or two sentences explaining it",
                  "choices": []}}]
}}

Maths practice skills (for "skills"; leave it empty if none fit or it isn't maths):
{skills}

Rules:
- Notes: the key facts, methods, definitions and the teacher's worked examples, in simple language.
  Keep any numbers, formulas and steps exactly. 3-8 headings.
- Topics: 2-8 topics, named the way a teacher or exam board would (e.g. "Simultaneous equations").
- Questions: 8-15 practice questions covering every topic, from easy (level 1) through medium (2) to
  exam-style (3). Make up fresh numbers rather than repeating the lesson's examples word for word.
  For about a third of them give 4 "choices" (multiple choice, the answer must be one of them);
  leave "choices" empty for the rest.
- Don't invent content that wasn't in the lesson. If there's too little to work with, still reply in
  this shape, with fewer notes and questions.

{what_cap}:
\"\"\"
{text}
\"\"\""""


class StudyError(Exception):
    pass


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:30]


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise StudyError("Claude didn't send back notes.")
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise StudyError("Claude's notes came back garbled.") from e


def tidy(result: dict, lesson: dict) -> dict:
    """Keep only well-formed notes, topics and questions."""
    notes = [{"heading": str(n.get("heading", "")).strip(), "points": [str(p).strip() for p in n.get("points", []) if str(p).strip()]}
             for n in result.get("notes", []) if isinstance(n, dict)]
    topics = [{"name": str(t.get("name", "")).strip(), "summary": str(t.get("summary", "")).strip()}
              for t in result.get("topics", []) if isinstance(t, dict) and str(t.get("name", "")).strip()]
    questions = []
    for i, q in enumerate(result.get("questions", [])):
        if not isinstance(q, dict) or not str(q.get("question", "")).strip() or not str(q.get("answer", "")).strip():
            continue
        choices = [str(c).strip() for c in q.get("choices") or [] if str(c).strip()]
        answer = str(q["answer"]).strip()
        if choices and answer not in choices:
            choices = []  # a multiple-choice question whose answer isn't a choice: ask it as a normal one
        try:
            level = min(3, max(1, int(q.get("level", 2))))
        except (TypeError, ValueError):
            level = 2
        questions.append({"id": f"q{i + 1}", "topic": str(q.get("topic", "")).strip(), "level": level,
                          "question": str(q["question"]).strip(), "answer": answer,
                          "why": str(q.get("why", "")).strip(), "choices": choices})
    # Your own title stays; Buddy's suggestion is kept apart, shown only if you didn't give one.
    lesson.update({"auto_title": str(result.get("title", "")).strip()[:80],
                   "subject": lesson.get("subject") or str(result.get("subject", "")).strip()[:30],
                   "summary": str(result.get("summary", "")).strip(),
                   "about": str(result.get("about", "")).strip().rstrip(".")[:80],
                   "notes": [n for n in notes if n["heading"] or n["points"]], "topics": topics,
                   "questions": questions})
    from .practice import SKILLS, match_skills
    skills = [x for x in result.get("skills") or [] if isinstance(x, str) and x in SKILLS]
    if not skills and re.search(r"math|algebra|geometr|statistic|number", f"{lesson['subject']} {result.get('subject', '')}", re.I):
        # Claude didn't say: match the topics and notes against the skills' keywords instead.
        skills = match_skills(" ".join([t["name"] for t in topics] + [p for n in notes for p in n["points"]]))
    lesson["skills"] = skills[:8]
    return lesson


