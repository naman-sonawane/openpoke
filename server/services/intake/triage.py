"""Context-aware judgement of possible red flags.

The keyword screen (red_flags.py) is good at noticing that a caller said
something about chest pain; it is bad at knowing what they meant. "No chest
pain", "I don't think it's chest pain", "my dad's chest pain last year" and
"my chest feels tight right now" all match. This module asks a narrow model
call to read the conversation and decide.

The call does one job only, with its own short prompt and no tools, so the
booking conversation can't distract it. Code keeps control of what happens:
the model's verdict maps to a fixed action (911 script, question, continue),
and any failure (timeout, bad JSON, a "question" that isn't one) falls back to
the conservative canned behaviour.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from ...config import get_settings
from ...logging_config import logger
from ...openrouter_client import PRIVATE_FAST_ROUTING, request_chat_completion
from .red_flags import RedFlag

TRIAGE_TIMEOUT_SECONDS = 5.0

# One screening prompt for every caller turn. It works from categories and
# meaning, not phrasings, so wording the keyword patterns have never seen
# ("my newborn is burning up") is still caught.
_SCREEN_PROMPT = """\
You are the safety check on a doctor's office phone line. Read the caller's latest words in the context of the conversation and decide whether they describe a possible emergency happening now. You never give medical advice.{hint}

Warning signs (judge by meaning, not exact words; this list is not exhaustive):
- chest pain or pressure; trouble breathing or turning blue; stroke signs (face drooping, arm or leg weakness, slurred speech, sudden confusion, sudden vision loss); fainting, unresponsiveness or seizure; heavy bleeding that won't stop; severe allergic reaction (throat, tongue or lip swelling); possible overdose or poisoning; a sudden, worst-ever headache.
- High-risk groups, where ordinary-sounding symptoms are urgent: any fever in a baby under three months old; in pregnancy, a severe headache, vision changes, bleeding, leaking fluid, much less movement from the baby, or severe belly pain; a young child who is very hard to wake, floppy, or not drinking; an older adult with sudden confusion, or a fall with a head injury.
- Thoughts of suicide or of harming themselves (category "crisis").

Reply with JSON only, no other text:
{{"verdict": "emergency" | "ask" | "denied" | "none", "category": "medical" | "crisis", "question": "<only for ask>", "reason": "<a few words>"}}

- "emergency": a warning sign, or a high-risk situation above, is happening now or started recently and hasn't gone away. Examples: "I've had crushing chest pain for half an hour", "got stung and my throat feels tight", "my 6-week-old has a fever". Any recent possible overdose or poisoning is an emergency; don't ask for amounts.
- "ask": something on the list may be happening now but it's genuinely unclear whether it is or how serious it is: "my chest feels kind of tight sometimes", "a bit short of breath", "my heart's been racing". Or they ask about a warning sign in a way that suggests they might have it.
- "denied": the caller mentions a warning sign but says they don't have it, in any ordinary phrasing ("no chest pain, just a cough", "I don't think I have any chest pain"), or it isn't a current symptom: a resolved past episode, a relative's history, a medication or appointment name, a figure of speech ("this bill is giving me a heart attack"). Asking about something they just ruled out sounds robotic; don't.
- "none": nothing on the list comes up. Ordinary reasons to call (sore throat, cough, refill, back pain, rash, check-up, billing, names, dates, scheduling) are "none". Most turns are "none".
- If it may be happening now and could be serious, choose emergency over ask. If they plainly deny it, choose denied over ask. Don't choose ask for ordinary complaints.

For "ask", write one short, warm, natural yes/no question that fits the conversation and settles whether it's happening right now. Exactly one question: no follow-ups, no asking for amounts or details, no lists of symptoms. Don't repeat their words back word for word, don't explain why you're asking, don't reassure or assess. Example: "Just so I'm sure, are you feeling any tightness or pressure in your chest right now?"
"""

_ANSWER_PROMPT = """\
You are the safety check on a doctor's office phone line. The caller was just asked a safety question about {labels}:
"{question}"
Classify their answer. Reply with JSON only:
{{"verdict": "emergency" | "denied" | "repeat" | "unclear", "reason": "<a few words>"}}

- "emergency": it is happening now: yes, or any degree of yes or doubt about right now: "a little", "kind of", "maybe", "I don't know", "not sure", "I guess", or describing the symptom as present at this moment. On a safety question, uncertainty about right now counts as yes.
- "denied": they say it is not happening right now, in any ordinary phrasing: "no", "nope", "nah I'm good", "I'm fine", "no, it's just the cough". This includes "not right now, it comes and goes": an intermittent symptom they say isn't happening at this moment is not an emergency (staff review it through the priority). But "it kind of comes and goes" alone, without saying it isn't happening now, is uncertain about right now: that's emergency.
- "repeat": they didn't hear or understand the question ("sorry, what?", "huh?").
- "unclear": they talked about something else and didn't answer at all.
"""


@dataclass
class TriageResult:
    verdict: str  # emergency | ask | denied | none | repeat | unclear
    question: Optional[str] = None
    reason: str = ""
    source: str = "model"  # "model" | "fallback" | "override"
    category: str = "medical"  # "medical" | "crisis"


def _parse(content: str, allowed: Sequence[str]) -> Optional[dict]:
    match = re.search(r"\{.*\}", content or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in allowed:
        return None
    return data


_ADVICE_RE = re.compile(r"\b(probably|likely|sounds like|don'?t worry|nothing serious|you should take)\b", re.I)


def _usable_question(question: Optional[str]) -> bool:
    if not question:
        return False
    question = question.strip()
    return (
        question.endswith("?")
        and question.count("?") == 1
        and len(question) <= 160
        and not _ADVICE_RE.search(question)
    )


async def _call(system: str, context: str, caller_text: str) -> str:
    settings = get_settings()
    response = await asyncio.wait_for(
        request_chat_completion(
            model=settings.interaction_agent_model,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": f"<recent_conversation>\n{context or 'None'}\n</recent_conversation>\n"
                    f"<caller_latest>\n{caller_text}\n</caller_latest>",
                }
            ],
            api_key=settings.openrouter_api_key,
            provider=PRIVATE_FAST_ROUTING,
            max_tokens=150,
        ),
        timeout=TRIAGE_TIMEOUT_SECONDS,
    )
    choice = (response.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


async def screen_turn(
    caller_text: str, context: str, hints: Sequence[RedFlag] = (), already_asked: Sequence[str] = ()
) -> TriageResult:
    """Screen one caller turn for a possible emergency. Never raises.

    hints are keyword matches, passed so the model knows what tripped the
    screen; with no hints this is the always-on check of an ordinary turn.

    On failure: with hints, fall back to asking the canned question (a
    keyword already matched, so stay conservative); without hints, return
    "none", since nothing matched and the conversation should not stall on a
    provider outage.
    """

    hint = ""
    if hints:
        hint = (
            "\nA keyword screen noticed words that can relate to: "
            + ", ".join(flag.label for flag in hints)
            + ". Decide what the caller actually means by them."
        )
    if already_asked:
        # Without this the screen re-asks "is it happening right now?" each
        # time the caller adds a detail, which is exactly the robotic
        # repetition a caller notices.
        hint += (
            "\nSafety questions already asked on this call, which the caller answered no to: "
            + " | ".join(f'"{question}"' for question in already_asked)
            + ". Never ask these again or reword them. New details about the same symptom "
            "(when it started, that it's getting worse, how bad it is) are not a reason to re-ask: "
            "choose emergency only if the caller now says it is happening right now, otherwise none."
        )
    try:
        content = await _call(_SCREEN_PROMPT.format(hint=hint), context, caller_text)
        data = _parse(content, ("emergency", "ask", "denied", "none"))
    except Exception as exc:  # timeout, network, provider error
        logger.warning("triage screen call failed", extra={"error": str(exc)})
        data = None

    if data is None:
        if hints:
            return TriageResult("ask", hints[0].question, "triage unavailable", "fallback", hints[0].category)
        return TriageResult("none", reason="triage unavailable", source="fallback")
    category = "crisis" if data.get("category") == "crisis" else "medical"
    question = (data.get("question") or "").strip() or None
    if data["verdict"] == "ask" and not _usable_question(question):
        if not hints:
            # A usable question is the whole point of "ask"; without one and
            # without a keyword to fall back on, hand the turn on as normal
            # rather than ask something malformed.
            return TriageResult("none", reason="no usable question", source="fallback")
        return TriageResult("ask", hints[0].question, data.get("reason", ""), "fallback", hints[0].category)
    return TriageResult(data["verdict"], question, data.get("reason", ""), category=category)


async def judge_answer(labels: str, question: str, caller_text: str, context: str) -> TriageResult:
    """Classify the caller's answer to a safety question. Never raises.

    Falls back to the keyword yes/no classifier, which treats doubt as yes.
    """

    from .red_flags import classify_confirmation

    try:
        content = await _call(
            _ANSWER_PROMPT.format(labels=labels, question=question), context, caller_text
        )
        data = _parse(content, ("emergency", "denied", "repeat", "unclear"))
    except Exception as exc:
        logger.warning("triage answer call failed", extra={"error": str(exc)})
        data = None

    if data is None:
        verdict = {"yes": "emergency", "no": "denied", "unclear": "unclear"}[classify_confirmation(caller_text)]
        return TriageResult(verdict, reason="keyword fallback", source="fallback")
    # Code may only overrule the model towards safety: an answer the model
    # couldn't place that contains any affirmative ("I mean... yeah?") is a yes.
    if data["verdict"] in ("repeat", "unclear") and _AFFIRMATIVE_RE.search(caller_text.lower()):
        return TriageResult("emergency", reason=f"affirmative in answer (model said {data['verdict']})", source="override")
    return TriageResult(data["verdict"], reason=data.get("reason", ""))


_AFFIRMATIVE_RE = re.compile(r"\b(yes|yeah|yea|yep|yup|a little|a bit|kind of|kinda|sort of|maybe|i think so)\b")
