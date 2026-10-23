"""Rule-based visit priority for non-emergency callers.

The model gathers the structured facts (visit type, severity, onset, trend);
these rules turn them into a level. Every rule that fires is reported, the
highest level wins, and missing information rounds up rather than down. The
model never picks the level itself, so the decision is reproducible and can be
read back to staff line by line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

LEVELS = ("routine", "soon", "same_day")

WINDOWS = {
    "same_day": "today",
    "soon": "within the next 3 business days",
    "routine": "the next available routine slot",
}

VISIT_TYPES = ("new_problem", "follow_up", "medication_refill", "wellness", "other")

# Reason-for-visit phrases that warrant a same-day look even when the caller
# rates the severity low.
_SAME_DAY_TERMS = {
    r"fever": "fever",
    r"infect\w*": "possible infection",
    r"spreading|red streak": "spreading redness",
    r"pregnan\w*": "pregnancy-related concern",
    r"vomit\w*|throwing up|can'?t keep (anything|food|water) down": "vomiting",
    r"dehydrat\w*": "possible dehydration",
    r"burn\w* (when|while) (i )?(pee|urinat)\w*|\buti\b|urinary": "urinary symptoms",
    r"(ran|run|running) out of (my )?(medication|meds|insulin|inhaler)": "out of essential medication",
    r"\bcut\b|wound|stitches|burned": "wound care",
}


@dataclass
class PriorityDecision:
    level: str
    window: str
    reasons: List[str] = field(default_factory=list)
    needs_nurse_review: bool = False

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "window": self.window,
            "reasons": self.reasons,
            "needs_nurse_review": self.needs_nurse_review,
        }


def _bump(current: str, candidate: str) -> str:
    return candidate if LEVELS.index(candidate) > LEVELS.index(current) else current


def assess_priority(
    *,
    reason_for_visit: str,
    visit_type: str,
    severity: Optional[int] = None,
    onset_days: Optional[float] = None,
    worsening: Optional[bool] = None,
    high_risk: Optional[bool] = None,
    cleared_red_flags: Iterable[str] = (),
) -> PriorityDecision:
    """Apply the rule table and return the most urgent level any rule supports."""

    level = "routine"
    reasons: List[str] = []
    needs_review = False

    def fire(candidate: str, why: str) -> None:
        nonlocal level
        level = _bump(level, candidate)
        reasons.append(f"{candidate}: {why}")

    text = (reason_for_visit or "").lower()

    if severity is not None and severity >= 7:
        fire("same_day", f"caller rates it {severity}/10")
    if worsening:
        fire("same_day", "symptoms are getting worse")
    if high_risk:
        fire("same_day", "caller is in a higher-risk group (e.g. pregnant, infant, immunocompromised)")
    for pattern, label in _SAME_DAY_TERMS.items():
        if re.search(pattern, text):
            fire("same_day", label)
    if severity is not None and 4 <= severity <= 6:
        fire("soon", f"moderate severity ({severity}/10)")
    if visit_type == "new_problem":
        if onset_days is None:
            fire("soon", "new problem, onset unknown")
            needs_review = True
        elif onset_days <= 14:
            fire("soon", f"new problem that started {onset_days:g} day(s) ago")
        else:
            reasons.append(f"routine: long-standing problem ({onset_days:g} days)")
        if severity is None:
            fire("soon", "new problem, severity not given")
            needs_review = True

    # A warning sign the caller mentioned, then ruled out on the confirm
    # question. Not urgent on its own, but never routine, and a nurse looks.
    cleared = list(cleared_red_flags)
    if cleared:
        fire("soon", f"warning sign mentioned then ruled out ({', '.join(cleared)})")
        needs_review = True

    if visit_type in ("follow_up", "medication_refill", "wellness") and level == "routine":
        reasons.append(f"routine: {visit_type.replace('_', ' ')} with no warning signs")

    if not reasons:
        fire("soon", "not enough information to call it routine")
        needs_review = True

    return PriorityDecision(level=level, window=WINDOWS[level], reasons=reasons, needs_nurse_review=needs_review)
