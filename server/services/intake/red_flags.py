"""Deterministic emergency screen that runs on every caller turn before the LLM.

The screen is deliberately keyword based. It is explainable, it costs no
latency, and it cannot be talked out of a decision the way a prompt can. The
model gets a second look afterwards (the flag_emergency tool), so this layer
only has to be conservative, not complete.

Each red flag has two pattern sets:

* definite - unambiguous phrasing ("I can't breathe"). Unless it is negated or
  clearly about the past, this goes straight to the 911 script.
* possible - phrasing that may or may not be an emergency ("chest tightness").
  The caller gets exactly one yes/no question before anything else happens.

A definite phrase that is negated ("no chest pain") or historical ("my dad had
a stroke last year") is downgraded to possible, so it also gets the one
confirm question rather than being silently ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class RedFlag:
    key: str
    label: str
    category: str  # "medical" -> 911 script, "crisis" -> 988/911 script plus handoff
    definite: Tuple[str, ...]
    possible: Tuple[str, ...]
    question: str


RED_FLAGS: Tuple[RedFlag, ...] = (
    RedFlag(
        key="self_harm",
        label="thoughts of self-harm",
        category="crisis",
        definite=(
            r"kill(ing)? (myself|himself|herself|themselves)",
            r"suicid\w*",
            r"end (my|his|her|their) life",
            r"want(s)? to die",
            r"don'?t want to (live|be alive)",
            r"better off dead",
        ),
        possible=(
            r"hurt(ing)? (myself|himself|herself|themselves)",
            r"self[- ]?harm\w*",
            r"no reason to live",
            r"can'?t go on",
        ),
        question="I want to make sure you're safe. Are you having thoughts of hurting yourself right now?",
    ),
    RedFlag(
        key="chest_pain",
        label="chest pain",
        category="medical",
        definite=(
            r"chest (pain|pains|hurts?|is hurting)",
            r"pain in (my|his|her|their|the) chest",
            r"heart attack",
            r"crushing (pain|pressure)",
        ),
        possible=(
            r"chest (tight\w*|pressure|discomfort|heaviness|feels? (tight|heavy|weird|funny))",
            r"tight(ness)? in (my|his|her|their|the) chest",
            r"heart (is )?(racing|pounding)",
            r"palpitations?",
        ),
        question="Are you having chest pain or pressure right now?",
    ),
    RedFlag(
        key="breathing",
        label="trouble breathing",
        category="medical",
        definite=(
            r"(can'?t|cannot|can not|unable to) (breathe|catch (my|his|her|their) breath)",
            r"not breathing",
            r"struggling to breathe",
            r"gasping",
            r"choking",
            r"(lips?|face) (are |is )?(turning )?blue",
        ),
        possible=(
            r"short(ness)? of breath",
            r"hard to breathe",
            r"(trouble|difficulty|problems?) breathing",
            r"breathing (is )?(hard|difficult|trouble)",
            r"wheez\w*",
        ),
        question="Are you having trouble breathing right now?",
    ),
    RedFlag(
        key="stroke",
        label="stroke symptoms",
        category="medical",
        definite=(
            r"\bstroke\b",
            r"face (is )?droop\w*",
            r"droop\w* (face|smile|on one side)",
            r"slurr\w*",
            r"can'?t (move|feel) (my|his|her|their) (arm|leg|face)",
            r"one side of (my|his|her|their) (body|face)",
        ),
        possible=(
            r"sudden(ly)? (numb\w*|tingl\w*)",
            r"numb\w* (in|on) (my|his|her|their) (face|arm|leg|one side)",
            r"(face|arm|leg) (is |went |feels? )?numb",
            r"sudden(ly)? confus\w*",
            r"\b(is|are|seems?|got|getting) (really |very |suddenly )?confused",
            r"trouble (speaking|talking)",
            r"(sudden(ly)?|lost) (my )?(vision|sight)",
            r"\bweak(ness)? on one side",
        ),
        question="Is there any sudden weakness, numbness, face drooping, or trouble speaking happening right now?",
    ),
    RedFlag(
        key="unresponsive",
        label="loss of consciousness or seizure",
        category="medical",
        definite=(
            r"unconscious",
            r"unresponsive",
            r"won'?t wake up",
            r"not waking up",
            r"seizure",
            r"seizing",
            r"convuls\w*",
        ),
        possible=(
            r"passed out",
            r"fainted",
            r"fainting",
            r"blacked out",
        ),
        question="Has anyone passed out or had a seizure in the last little while?",
    ),
    RedFlag(
        key="bleeding",
        label="heavy bleeding",
        category="medical",
        definite=(
            r"(won'?t|will not|doesn'?t|does not|can'?t get it to) stop bleeding",
            r"bleeding (won'?t|will not|doesn'?t|does not) stop",
            r"(severe|heavy|heavily|a lot of|lots of) bleed\w*",
            r"bleeding (heavily|a lot|everywhere)",
            r"(coughing|throwing|vomiting|spitting) up blood",
            r"vomiting blood",
        ),
        possible=(r"\bbleed\w*",),
        question="Is there heavy bleeding that won't stop right now?",
    ),
    RedFlag(
        key="anaphylaxis",
        label="a severe allergic reaction",
        category="medical",
        definite=(
            r"anaphyla\w*",
            r"throat (is )?(closing|swelling|swollen|tight)",
            r"(tongue|lips?) (is |are )?(swelling|swollen)",
            r"used (my|his|her|their|an) epi-?pen",
        ),
        possible=(
            r"allergic reaction",
            r"\bhives\b",
            r"face (is )?(swelling|swollen)",
        ),
        question="Is there any swelling of the face, lips, or throat, or trouble breathing, right now?",
    ),
    RedFlag(
        key="overdose",
        label="a possible overdose or poisoning",
        category="medical",
        definite=(
            r"overdos\w*",
            r"took too many pills",
            r"poison(ed|ing)",
        ),
        possible=(
            r"took too (much|many)",
            r"swallowed (something|a|some)",
        ),
        question="Has someone taken too much of a medication or swallowed something harmful?",
    ),
    RedFlag(
        key="headache",
        label="a sudden severe headache",
        category="medical",
        definite=(
            r"worst headache (of|in) (my|his|her|their) life",
            r"thunderclap",
        ),
        possible=(
            r"sudden(ly)? (severe|really bad|terrible) headache",
            r"worst headache",
        ),
        question="Did a severe headache come on suddenly, within the last few hours?",
    ),
)

_FLAGS_BY_KEY = {flag.key: flag for flag in RED_FLAGS}

# Words that negate a symptom when they appear shortly before it in the same clause.
_NEGATION_RE = re.compile(
    r"\b(no|not|never|without|denies|deny|don'?t|doesn'?t|didn'?t|isn'?t|aren'?t|"
    r"wasn'?t|haven'?t|hasn'?t|none|nor|neither|free of)\b"
)

# Markers that a mention is historical, hypothetical, or a worry rather than a
# current symptom. These downgrade a definite match to a confirm question.
_NOT_CURRENT_RE = re.compile(
    r"\b(last (year|month|week|summer|winter|spring|fall)|years? ago|months? ago|"
    r"used to|history of|in the past|had a|what if|in case|worried about|"
    r"scared of|afraid of|risk of|my (dad|mom|father|mother|grand\w+) had)\b"
)

# Clause boundaries. "no fever but chest pain" must not negate the chest pain.
_CLAUSE_SPLIT_RE = re.compile(r"[.;!?,]|\bbut\b|\bthough\b|\bhowever\b|\bexcept\b")

NEGATION_WINDOW_WORDS = 5


@dataclass(frozen=True)
class ScreenHit:
    flag: RedFlag
    level: str  # "definite" | "possible"
    matched: str
    downgraded_because: Optional[str] = None  # "negated" | "not current"


def get_flag(key: str) -> Optional[RedFlag]:
    return _FLAGS_BY_KEY.get(key)


def _normalize(text: str) -> str:
    lowered = text.lower().replace("’", "'")
    return re.sub(r"\s+", " ", lowered)


def _clauses(text: str) -> List[str]:
    return [part.strip() for part in _CLAUSE_SPLIT_RE.split(text) if part.strip()]


def _is_negated(clause: str, match_start: int) -> bool:
    preceding = clause[:match_start].split()[-NEGATION_WINDOW_WORDS:]
    return bool(_NEGATION_RE.search(" ".join(preceding)))


def screen(text: str) -> List[ScreenHit]:
    """Return every red-flag hit in the caller's words, most serious first.

    Ordering: definite hits before possible ones, crisis flags first within a
    level, then the order of RED_FLAGS.
    """

    hits: dict[str, ScreenHit] = {}
    for clause in _clauses(_normalize(text)):
        not_current = bool(_NOT_CURRENT_RE.search(clause))
        for flag in RED_FLAGS:
            for level, patterns in (("definite", flag.definite), ("possible", flag.possible)):
                for pattern in patterns:
                    match = re.search(pattern, clause)
                    if not match:
                        continue
                    hit_level, reason = level, None
                    if _is_negated(clause, match.start()):
                        hit_level, reason = "possible", "negated"
                    elif level == "definite" and not_current:
                        hit_level, reason = "possible", "not current"
                    previous = hits.get(flag.key)
                    if previous is None or (previous.level == "possible" and hit_level == "definite"):
                        hits[flag.key] = ScreenHit(flag, hit_level, match.group(0), reason)
                    break

    order = {flag.key: index for index, flag in enumerate(RED_FLAGS)}
    return sorted(
        hits.values(),
        key=lambda hit: (hit.level != "definite", order[hit.flag.key]),
    )


_YES_RE = re.compile(
    r"^\s*(yes|yeah|yea|yep|yup|ya|uh[- ]?huh|mm[- ]?hmm|i am|i do|i think so|it is|"
    r"they are|he is|she is|correct|right|definitely|sort of|kind of|a little|a bit|"
    r"maybe|possibly|i guess|i'?m not sure|not sure|i don'?t know)\b"
)
_NO_RE = re.compile(
    r"^\s*(no|nope|nah|not really|not at all|not right now|not now|i'?m not|i am not|"
    r"i don'?t think so|i do not|i don'?t|it'?s not|it is not|nothing like that|negative|none)\b"
)


def classify_confirmation(answer: str) -> str:
    """Classify a reply to a red-flag confirm question: "yes", "no" or "unclear".

    Hedged answers ("maybe", "a little", "not sure") count as yes. When the
    question is whether something is an emergency, uncertainty is a yes.
    """

    text = _normalize(answer)
    if _YES_RE.search(text):
        return "yes"
    if _NO_RE.search(text):
        return "no"
    return "unclear"
