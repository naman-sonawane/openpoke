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
from typing import List, Optional, Sequence

from ...config import get_settings
from ...logging_config import logger
from ...openrouter_client import request_chat_completion
from .red_flags import RedFlag

TRIAGE_TIMEOUT_SECONDS = 5.0

_MENTION_PROMPT = """\
You are the safety check on a doctor's office phone line. A keyword screen noticed words in the caller's latest message that can relate to an emergency warning sign: {labels}. Decide what the caller is actually saying about that warning sign, using the conversation for context. You never give medical advice.

Reply with JSON only, no other text:
{{"verdict": "emergency" | "denied" | "ask", "question": "<only for ask>", "reason": "<a few words>"}}

- "emergency": the caller (or someone with them) has this warning sign now or it started recently and hasn't gone away, described in a way that could be serious. Examples: "I've had crushing chest pain for half an hour", "got stung and my throat feels tight". Any recent possible overdose or poisoning is an emergency; don't ask for amounts.
- "denied": the caller says they don't have it, in any ordinary phrasing ("no chest pain, just a cough", "I don't think I have any chest pain", "my chest doesn't hurt, it's my back"), or the words aren't about a current symptom at all: a past episode that has resolved, a relative's history, medication or appointment names, a figure of speech ("this bill is giving me a heart attack"). Asking someone about a symptom they just said they don't have sounds robotic; don't.
- "ask": they describe something that may be present now but it's unclear how serious it is: "my chest feels kind of tight sometimes", "a bit short of breath", "my heart's been racing". Or they ask about the symptom in a way that suggests they might have it.
- If it may be happening now and could be serious, choose emergency over ask. If they plainly deny it, choose denied over ask.

For "ask", write one short, warm, natural yes/no question that fits the conversation and settles whether they have it right now. Exactly one question: no follow-ups, no asking for amounts or details, no lists of symptoms. Don't repeat their words back word for word, don't explain why you're asking, don't reassure or assess. Example: "Just so I'm sure, are you feeling any tightness or pressure in your chest right now?"
"""

_ANSWER_PROMPT = """\
You are the safety check on a doctor's office phone line. The caller was just asked a safety question about {labels}:
"{question}"
Classify their answer. Reply with JSON only:
{{"verdict": "emergency" | "denied" | "repeat" | "unclear", "reason": "<a few words>"}}

- "emergency": yes, or any degree of yes or doubt: "a little", "kind of", "maybe", "I don't know", "not sure", "I guess", "it comes and goes", or describing the symptom as present now. On a safety question, uncertainty counts as yes.
- "denied": any ordinary no: "no", "nope", "nah I'm good", "not right now", "I'm fine", "no, it's just the cough".
- "repeat": they didn't hear or understand the question ("sorry, what?", "huh?").
- "unclear": they talked about something else and didn't answer at all.
"""


@dataclass
class TriageResult:
    verdict: str  # emergency | denied | ask | repeat | unclear
    question: Optional[str] = None
    reason: str = ""
    source: str = "model"  # "model" | "fallback"


def _labels(flags: Sequence[RedFlag]) -> str:
    return ", ".join(flag.label for flag in flags)


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
            provider={"sort": "latency"},
            max_tokens=150,
        ),
        timeout=TRIAGE_TIMEOUT_SECONDS,
    )
    choice = (response.get("choices") or [{}])[0]
    return (choice.get("message") or {}).get("content") or ""


async def judge_mention(flags: List[RedFlag], caller_text: str, context: str) -> TriageResult:
    """Decide what a possible red-flag mention means. Never raises."""

    try:
        content = await _call(_MENTION_PROMPT.format(labels=_labels(flags)), context, caller_text)
        data = _parse(content, ("emergency", "denied", "ask"))
    except Exception as exc:  # timeout, network, provider error
        logger.warning("triage mention call failed", extra={"error": str(exc)})
        data = None

    if data is None:
        return TriageResult("ask", flags[0].question, "triage unavailable", source="fallback")
    if data["verdict"] == "ask" and not _usable_question(data.get("question")):
        return TriageResult("ask", flags[0].question, data.get("reason", ""), source="fallback")
    return TriageResult(data["verdict"], (data.get("question") or "").strip() or None, data.get("reason", ""))


async def judge_answer(flags: List[RedFlag], question: str, caller_text: str, context: str) -> TriageResult:
    """Classify the caller's answer to a safety question. Never raises.

    Falls back to the keyword yes/no classifier, which treats doubt as yes.
    """

    from .red_flags import classify_confirmation

    try:
        content = await _call(
            _ANSWER_PROMPT.format(labels=_labels(flags), question=question), context, caller_text
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
