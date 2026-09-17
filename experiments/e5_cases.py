"""Fixed case set for E5 routing accuracy.

Twenty follow-up messages, each with exactly one correct target agent already in
the roster. Distractors are near misses only: they share surface vocabulary with
the target but refer to a different person, topic, or both.

Two cases are deliberately adversarial for a naive selector:
  did-she-reply   - the follow-up has zero lexical overlap with the target name,
                    so the selector has to read the recent transcript
  archived-email  - the target is archived and the follow-up names only her
                    email address, so a strong identifier match has to resurrect it
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

VERBS = ["Email to", "Reply to", "Follow up with", "Draft for", "Call notes for"]

TOPICS = [
    "lease",
    "invoice",
    "parking",
    "renewal",
    "deposit",
    "sublet",
    "insurance",
    "onboarding",
    "contract",
    "referral",
    "expenses",
    "scheduling",
]


@dataclass
class RoutingCase:
    case_id: str
    target: str
    follow_up: str
    transcript: List[Tuple[str, str]]  # (tag, text) written as conversation history
    person: str                        # for near-miss name variants
    topic: str                         # for near-miss topic variants
    description: str = ""
    emails: List[str] = field(default_factory=list)
    thread_ids: List[str] = field(default_factory=list)
    archived: bool = False
    note: str = ""


def _name_variants(person: str) -> List[str]:
    """Surface-similar but distinct spellings of a person's name."""
    base = person.split()[0]
    return [
        base[: max(4, len(base) - 3)],
        base + "jit" if not base.endswith("jit") else base + "a",
        f"{base} K",
        f"{base} (legal)",
        base + "a",
        f"{base}s team",
    ]


CASES: List[RoutingCase] = [
    RoutingCase(
        case_id="lease-followup",
        target="Email to Sharanjeet re: lease",
        person="Sharanjeet",
        topic="lease",
        follow_up="any update on the lease thing with sharanjeet?",
        transcript=[
            ("user_message", "can you ask sharanjeet when the lease renewal paperwork lands"),
            ("poke_reply", "on it, asking her now"),
        ],
        description="Chasing Sharanjeet Gill for the lease renewal paperwork date.",
        emails=["sharanjeet.gill@meridianprop.com"],
        thread_ids=["thread-lease-8841"],
    ),
    RoutingCase(
        case_id="did-she-reply",
        target="Email to Priya re: contract redlines",
        person="Priya",
        topic="contract",
        follow_up="did she reply?",
        transcript=[
            ("user_message", "ping priya about the contract redlines she owes me"),
            ("poke_reply", "sent, i'll flag the moment she comes back"),
        ],
        description="Waiting on Priya Raghavan's contract redlines.",
        emails=["priya.raghavan@northwind.io"],
        thread_ids=["thread-contract-2210"],
        note="zero lexical overlap between follow-up and target name",
    ),
    RoutingCase(
        case_id="archived-email",
        target="Email to Devika re: Q2 audit",
        person="Devika",
        topic="expenses",
        follow_up="anything back from devika.menon@aurelia-partners.com yet?",
        transcript=[
            ("user_message", "ask devika for the q2 audit schedule"),
            ("poke_reply", "asked, will let you know"),
        ],
        description="Requested the Q2 audit schedule from Devika Menon.",
        emails=["devika.menon@aurelia-partners.com"],
        thread_ids=["thread-audit-5573"],
        archived=True,
        note="archived target; follow-up carries only the email address",
    ),
    RoutingCase(
        case_id="invoice-chase",
        target="Follow up with Marcus re: invoice",
        person="Marcus",
        topic="invoice",
        follow_up="has marcus sorted the invoice out",
        transcript=[
            ("user_message", "marcus still hasn't paid invoice 4417, chase him"),
            ("poke_reply", "chasing"),
        ],
        description="Chasing Marcus Iyer on unpaid invoice 4417.",
        emails=["marcus.iyer@bellwether.co"],
        thread_ids=["thread-inv-4417"],
    ),
    RoutingCase(
        case_id="parking-permit",
        target="Email to Tomas re: parking",
        person="Tomas",
        topic="parking",
        follow_up="what happened with the parking permit",
        transcript=[
            ("user_message", "get tomas to sort my parking permit for the new garage"),
            ("poke_reply", "asking tomas now"),
        ],
        description="Parking permit request with Tomas Vrabel at building ops.",
        emails=["tomas.vrabel@harborgate.com"],
        thread_ids=["thread-parking-771"],
    ),
    RoutingCase(
        case_id="deposit-refund",
        target="Reply to Ingrid re: deposit",
        person="Ingrid",
        topic="deposit",
        follow_up="did ingrid confirm the deposit refund amount",
        transcript=[
            ("user_message", "ingrid emailed about the deposit refund, ask her for the exact figure"),
            ("poke_reply", "asked her for the number"),
        ],
        description="Deposit refund figure requested from Ingrid Halvorsen.",
        emails=["ingrid.halvorsen@stonebridge.se"],
        thread_ids=["thread-deposit-3092"],
    ),
    RoutingCase(
        case_id="insurance-cert",
        target="Email to Rafael re: insurance",
        person="Rafael",
        topic="insurance",
        follow_up="rafael send over the insurance certificate?",
        transcript=[
            ("user_message", "need the insurance certificate from rafael before friday"),
            ("poke_reply", "requested it"),
        ],
        description="Insurance certificate request to Rafael Duarte, needed by Friday.",
        emails=["rafael.duarte@cascadia-ins.com"],
        thread_ids=["thread-ins-6614"],
    ),
    RoutingCase(
        case_id="onboarding-docs",
        target="Draft for Hannah re: onboarding",
        person="Hannah",
        topic="onboarding",
        follow_up="send the onboarding draft to hannah",
        transcript=[
            ("user_message", "write hannah an onboarding welcome note for the new hire"),
            ("poke_reply", "drafting it now"),
        ],
        description="Onboarding welcome note drafted for Hannah Oyelaran.",
        emails=["hannah.oyelaran@brightpath.org"],
        thread_ids=["thread-onboard-1180"],
    ),
    RoutingCase(
        case_id="sublet-terms",
        target="Email to Yusuf re: sublet",
        person="Yusuf",
        topic="sublet",
        follow_up="yusuf get back about the sublet terms?",
        transcript=[
            ("user_message", "ask yusuf whether the landlord allows a sublet"),
            ("poke_reply", "asking"),
        ],
        description="Sublet permission question to Yusuf Baraka.",
        emails=["yusuf.baraka@keystone-re.com"],
        thread_ids=["thread-sublet-4402"],
    ),
    RoutingCase(
        case_id="referral-intro",
        target="Follow up with Noor re: referral",
        person="Noor",
        topic="referral",
        follow_up="did noor make the referral intro yet",
        transcript=[
            ("user_message", "noor offered to refer me to their head of design, follow up"),
            ("poke_reply", "following up with noor"),
        ],
        description="Referral introduction to a head of design, offered by Noor Fassi.",
        emails=["noor.fassi@lumenworks.dev"],
        thread_ids=["thread-ref-9925"],
    ),
    RoutingCase(
        case_id="expenses-q3",
        target="Email to Gabriel re: expenses",
        person="Gabriel",
        topic="expenses",
        follow_up="any word from gabriel on the expense report",
        transcript=[
            ("user_message", "gabriel needs to approve my q3 expense report, nudge him"),
            ("poke_reply", "nudged"),
        ],
        description="Q3 expense report approval pending with Gabriel Sostre.",
        emails=["gabriel.sostre@vantage-fin.com"],
        thread_ids=["thread-exp-7318"],
    ),
    RoutingCase(
        case_id="renewal-quote",
        target="Reply to Anika re: renewal",
        person="Anika",
        topic="renewal",
        follow_up="anika come back with the renewal quote?",
        transcript=[
            ("user_message", "anika was getting us a renewal quote, check in"),
            ("poke_reply", "checking in with her"),
        ],
        description="Renewal quote requested from Anika Bhatt.",
        emails=["anika.bhatt@solstice-group.com"],
        thread_ids=["thread-renew-2047"],
    ),
    RoutingCase(
        case_id="contract-signature",
        target="Follow up with Lars re: contract",
        person="Lars",
        topic="contract",
        follow_up="has lars signed it",
        transcript=[
            ("user_message", "lars still needs to sign the contract, chase it up"),
            ("poke_reply", "chasing lars for the signature"),
        ],
        description="Awaiting Lars Nyberg's signature on the contract.",
        emails=["lars.nyberg@fjordline.no"],
        thread_ids=["thread-sign-8830"],
    ),
    RoutingCase(
        case_id="scheduling-review",
        target="Email to Mei re: scheduling",
        person="Mei",
        topic="scheduling",
        follow_up="did we ever get a time from mei",
        transcript=[
            ("user_message", "ask mei for a slot for the design review next week"),
            ("poke_reply", "asked her for times"),
        ],
        description="Design review slot requested from Mei Tanaka for next week.",
        emails=["mei.tanaka@orbitlabs.jp"],
        thread_ids=["thread-sched-5501"],
    ),
    RoutingCase(
        case_id="invoice-dispute",
        target="Reply to Cormac re: invoice dispute",
        person="Cormac",
        topic="invoice",
        follow_up="where did the invoice dispute with cormac land",
        transcript=[
            ("user_message", "cormac is disputing the march invoice, push back with the timesheets"),
            ("poke_reply", "replied with the timesheet breakdown"),
        ],
        description="Disputed March invoice with Cormac Brennan; timesheets sent.",
        emails=["cormac.brennan@ardara.ie"],
        thread_ids=["thread-dispute-6672"],
    ),
    RoutingCase(
        case_id="lease-inspection",
        target="Call notes for Elena re: lease inspection",
        person="Elena",
        topic="lease",
        follow_up="what did elena say on the inspection call",
        transcript=[
            ("user_message", "log what elena said on the lease inspection call"),
            ("poke_reply", "logged the call notes"),
        ],
        description="Call notes from the lease inspection discussion with Elena Marchetti.",
        emails=["elena.marchetti@civitas-re.it"],
        thread_ids=["thread-inspect-4419"],
    ),
    RoutingCase(
        case_id="deposit-escrow",
        target="Email to Kwame re: escrow deposit",
        person="Kwame",
        topic="deposit",
        follow_up="kwame confirm the escrow went through?",
        transcript=[
            ("user_message", "check with kwame that the escrow deposit cleared"),
            ("poke_reply", "asking kwame to confirm"),
        ],
        description="Confirming the escrow deposit cleared, with Kwame Asante.",
        emails=["kwame.asante@meridiantrust.gh"],
        thread_ids=["thread-escrow-3357"],
    ),
    RoutingCase(
        case_id="insurance-claim",
        target="Follow up with Saoirse re: insurance claim",
        person="Saoirse",
        topic="insurance",
        follow_up="any movement on the claim",
        transcript=[
            ("user_message", "saoirse is handling the water damage insurance claim, follow up"),
            ("poke_reply", "following up on the claim"),
        ],
        description="Water damage insurance claim handled by Saoirse Nolan.",
        emails=["saoirse.nolan@glenveagh-ins.ie"],
        thread_ids=["thread-claim-7740"],
        note="follow-up omits the person's name entirely",
    ),
    RoutingCase(
        case_id="onboarding-laptop",
        target="Email to Dmitri re: onboarding hardware",
        person="Dmitri",
        topic="onboarding",
        follow_up="did dmitri order the laptop for the new starter",
        transcript=[
            ("user_message", "get dmitri to order onboarding hardware for the new starter"),
            ("poke_reply", "asked dmitri to place the order"),
        ],
        description="Onboarding hardware order for a new starter, with Dmitri Volkov.",
        emails=["dmitri.volkov@stackforge.io"],
        thread_ids=["thread-hw-2284"],
    ),
    RoutingCase(
        case_id="referral-bonus",
        target="Reply to Amara re: referral bonus",
        person="Amara",
        topic="referral",
        follow_up="amara ever hear back about the referral bonus",
        transcript=[
            ("user_message", "amara asked whether her referral bonus was approved, find out"),
            ("poke_reply", "checking on the bonus approval"),
        ],
        description="Referral bonus approval question raised by Amara Okonkwo.",
        emails=["amara.okonkwo@northgate-hr.com"],
        thread_ids=["thread-bonus-9013"],
    ),
]


def build_distractors(case: RoutingCase, count: int, seed: int) -> List[str]:
    """Near-miss names for one case, nested so smaller sets are prefixes.

    A distractor is excluded when it shares BOTH the target's person and topic,
    since such a name would be a defensible answer and scoring it wrong would be
    unfair to the model.
    """
    rng = random.Random(seed)
    target_first = case.person.split()[0].lower()
    pool: List[str] = []

    for verb in VERBS:
        for name in _name_variants(case.person):
            for topic in TOPICS:
                candidate = f"{verb} {name} re: {topic}"
                if candidate == case.target:
                    continue
                same_person = name.split()[0].lower() == target_first
                if same_person and topic == case.topic:
                    continue
                pool.append(candidate)

    # Other cases' people crossed with this case's topic: same-topic confusers.
    for other in CASES:
        if other.case_id == case.case_id:
            continue
        for verb in VERBS[:3]:
            candidate = f"{verb} {other.person} re: {case.topic}"
            if candidate != case.target:
                pool.append(candidate)

    pool = list(dict.fromkeys(pool))
    rng.shuffle(pool)
    if len(pool) < count:
        raise RuntimeError(f"{case.case_id}: only {len(pool)} distractors for {count}")
    return pool[:count]


__all__ = ["CASES", "RoutingCase", "build_distractors"]
