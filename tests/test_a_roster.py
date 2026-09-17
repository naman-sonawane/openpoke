"""Fix A - roster records, relevance selection, archiving, and resurrection.

Before the fix the roster was a flat list of every name ever created and all of
it went into every interaction prompt.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture
def roster_factory(sandbox, tmp_path):
    """A fresh AgentRoster backed by a throwaway file."""
    roster_mod = sandbox.modules["roster"]
    counter = {"n": 0}

    def _make(initial=None):
        counter["n"] += 1
        path = tmp_path / f"roster-{counter['n']}.json"
        if initial is not None:
            path.write_text(json.dumps(initial), encoding="utf-8")
        return roster_mod.AgentRoster(path)

    return _make


def _iso(days_ago):
    return (
        (datetime.now(timezone.utc) - timedelta(days=days_ago))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _record(name, **kwargs):
    base = {
        "name": name,
        "description": "",
        "emails": [],
        "thread_ids": [],
        "status": "active",
        "created_at": _iso(1),
        "last_active_at": _iso(1),
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------- migration


def test_v1_list_of_names_migrates_without_data_loss(roster_factory):
    """Existing installs have a bare JSON list; those agents must survive."""
    roster = roster_factory(["Email to Sharanjeet", "Vercel Job Offer"])
    names = {r.name for r in roster.get_records()}
    assert names == {"Email to Sharanjeet", "Vercel Job Offer"}
    assert all(r.status == "active" for r in roster.get_records())


def test_migration_is_persisted_in_v2_shape(roster_factory, tmp_path):
    roster = roster_factory(["Only Agent"])
    roster.save()
    written = json.loads(roster._roster_path.read_text(encoding="utf-8"))
    assert written["version"] == 2
    assert written["agents"][0]["name"] == "Only Agent"


def test_unrecognised_roster_shape_does_not_raise(roster_factory):
    roster = roster_factory({"totally": "unexpected"})
    assert roster.get_records() == []


# ---------------------------------------------------------------- selection


def test_prompt_selection_is_capped_well_below_roster_size(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [_record(f"Email to Person{i} re: topic{i}") for i in range(200)],
    })
    selected = roster.get_agents_for_prompt("anything at all", limit=15)
    assert len(selected) == 15
    assert roster.counts()["total"] == 200


def test_selection_prefers_lexically_relevant_agents(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [
            _record("Email to Sharanjeet re: lease", description="Lease renewal paperwork"),
            *[_record(f"Email to Person{i} re: invoice") for i in range(50)],
        ],
    })
    selected = roster.get_agents_for_prompt("any update on the lease with sharanjeet?", limit=5)
    assert selected[0].name == "Email to Sharanjeet re: lease"


def test_recent_conversation_context_resolves_a_pronoun_followup(roster_factory):
    """'did she reply?' shares no words with the target; prior turns supply them."""
    roster = roster_factory({
        "version": 2,
        "agents": [
            # Older than the distractors, so recency alone will not surface it.
            _record(
                "Email to Priya re: contract redlines",
                description="Waiting on redlines",
                last_active_at=_iso(5),
            ),
            *[
                _record(f"Email to Person{i} re: parking", last_active_at=_iso(1))
                for i in range(60)
            ],
        ],
    })

    # "did she reply?" carries no usable tokens, so the target cannot rank first.
    bare = roster.get_agents_for_prompt("did she reply?", limit=5)
    assert bare[0].name != "Email to Priya re: contract redlines"

    with_context = roster.get_agents_for_prompt(
        "ping priya about the contract redlines she owes me\ndid she reply?", limit=5
    )
    assert with_context[0].name == "Email to Priya re: contract redlines"


# ---------------------------------------------------------------- archiving


def test_stale_agents_are_archived_not_deleted(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [
            _record("Old Agent", last_active_at=_iso(120)),
            _record("Fresh Agent", last_active_at=_iso(1)),
        ],
    })
    assert roster.archive_stale(older_than_days=30) == 1
    counts = roster.counts()
    assert counts["total"] == 2 and counts["archived"] == 1
    assert any(r.name == "Old Agent" for r in roster.get_records())


def test_archived_agents_are_hidden_from_the_prompt_by_default(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [_record("Email to Devika re: Q2 audit", status="archived",
                           description="Q2 audit schedule")],
    })
    selected = roster.get_agents_for_prompt("what about the q2 audit schedule", limit=10)
    assert selected == []


# ------------------------------------------------------------ resurrection


def test_matching_email_resurrects_an_archived_agent(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [
            _record(
                "Email to Devika re: Q2 audit",
                status="archived",
                emails=["devika.menon@aurelia-partners.com"],
                last_active_at=_iso(200),
            ),
            *[_record(f"Email to Person{i} re: audit") for i in range(80)],
        ],
    })
    selected = roster.get_agents_for_prompt(
        "anything back from devika.menon@aurelia-partners.com yet?", limit=10
    )
    assert selected[0].name == "Email to Devika re: Q2 audit"
    assert selected[0].archived is True


def test_matching_thread_id_resurrects_an_archived_agent(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [
            _record("Old Thread Agent", status="archived", thread_ids=["thread-lease-8841"]),
            *[_record(f"Other {i}") for i in range(30)],
        ],
    })
    selected = roster.get_agents_for_prompt("follow up on thread-lease-8841", limit=5)
    assert selected[0].name == "Old Thread Agent"


def test_strong_match_outranks_a_lexical_near_miss(roster_factory):
    """A same-email hit must beat a name that merely looks similar."""
    roster = roster_factory({
        "version": 2,
        "agents": [
            _record("Email to Devika K re: audit", description="audit devika schedule"),
            _record(
                "Email to Devika re: Q2 audit",
                status="archived",
                emails=["devika.menon@aurelia-partners.com"],
            ),
        ],
    })
    selected = roster.get_agents_for_prompt(
        "audit update from devika.menon@aurelia-partners.com", limit=5
    )
    assert selected[0].name == "Email to Devika re: Q2 audit"


# ------------------------------------------------------------------- touch


def test_touch_merges_identifiers_and_reactivates(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [_record("Lease Agent", status="archived", emails=["a@x.com"])],
    })
    roster.touch("Lease Agent", emails=["b@y.com"], thread_ids=["thread-1"])
    record = roster.get_records()[0]
    assert record.status == "active"
    assert set(record.emails) == {"a@x.com", "b@y.com"}
    assert record.thread_ids == ["thread-1"]


def test_touch_does_not_duplicate_identifiers(roster_factory):
    roster = roster_factory({
        "version": 2,
        "agents": [_record("Agent", emails=["a@x.com"])],
    })
    for _ in range(5):
        roster.touch("Agent", emails=["A@X.com", "a@x.com"])
    assert roster.get_records()[0].emails == ["a@x.com"]


def test_identifier_lists_stay_bounded(roster_factory):
    roster = roster_factory({"version": 2, "agents": [_record("Agent")]})
    roster.touch("Agent", emails=[f"user{i}@x.com" for i in range(100)])
    assert len(roster.get_records()[0].emails) <= 12


# ------------------------------------------------------------- integration


def test_render_shows_selected_agents_and_notes_the_hidden_ones(sandbox, roster_factory):
    """The prompt block must be capped and must say the list was filtered."""
    inter_agent = sandbox.modules["inter_agent"]
    roster_mod = sandbox.modules["roster"]

    big = roster_factory({
        "version": 2,
        "agents": [_record(f"Email to Person{i} re: topic{i}") for i in range(200)],
    })
    original = roster_mod.get_agent_roster
    roster_mod.get_agent_roster = lambda: big
    # interaction_agent.agent imported the symbol directly.
    inter_agent.get_agent_roster = lambda: big
    try:
        messages = inter_agent.prepare_message_with_history("hello there", "", "user")
        content = messages[0]["content"]
    finally:
        roster_mod.get_agent_roster = original
        inter_agent.get_agent_roster = original

    assert content.count("<agent ") <= 15
    assert "further agents are not shown" in content


@pytest.mark.asyncio
async def test_send_message_to_agent_records_a_description(sandbox, fake_llm, tmp_path):
    """The description the model supplies is what makes an agent findable later."""
    from harness import text_response

    fake_llm(lambda agent_name, system, messages: (text_response("done."), 0.01))

    inter_tools = sandbox.modules["inter_tools"]
    roster_mod = sandbox.modules["roster"]
    fresh = roster_mod.AgentRoster(tmp_path / "roster-tool.json")

    original = roster_mod.get_agent_roster
    roster_mod.get_agent_roster = lambda: fresh
    inter_tools.get_agent_roster = lambda: fresh
    try:
        result = inter_tools.send_message_to_agent(
            "Email to Sharanjeet re: lease",
            "Ask about the paperwork.",
            description="Chasing the lease renewal paperwork.",
        )
    finally:
        roster_mod.get_agent_roster = original
        inter_tools.get_agent_roster = original

    assert result.success
    assert result.payload["new_agent_created"] is True
    record = fresh.get_records()[0]
    assert record.name == "Email to Sharanjeet re: lease"
    assert record.description == "Chasing the lease renewal paperwork."


@pytest.mark.asyncio
async def test_execution_agent_harvests_identifiers_into_the_roster(
    sandbox, fake_llm, fake_gmail, tmp_path
):
    """Thread ids and addresses seen by a tool become roster search keys."""
    from harness import text_response, tool_call_response

    fake_gmail(0.0, mode="blocking")

    def responder(agent_name, system, messages):
        if len(messages) <= 1:
            return (
                tool_call_response(
                    "gmail_reply_to_thread",
                    json.dumps(
                        {
                            "thread_id": "thread-lease-8841",
                            "recipient_email": "sharanjeet.gill@meridianprop.com",
                            "message_body": "Any update?",
                        }
                    ),
                ),
                0.01,
            )
        return text_response("replied"), 0.01

    fake_llm(responder)

    roster_mod = sandbox.modules["roster"]
    fresh = roster_mod.AgentRoster(tmp_path / "roster-harvest.json")
    fresh.add_agent("Email to Sharanjeet re: lease")

    original = roster_mod.get_agent_roster
    roster_mod.get_agent_roster = lambda: fresh
    try:
        runtime = sandbox.modules["exec_runtime"].ExecutionAgentRuntime(
            agent_name="Email to Sharanjeet re: lease"
        )
        assert (await runtime.execute("Chase the lease.")).success
    finally:
        roster_mod.get_agent_roster = original

    record = fresh.get_records()[0]
    assert "sharanjeet.gill@meridianprop.com" in record.emails
    assert "thread-lease-8841" in record.thread_ids

    # And that harvested identifier is now enough to find the agent.
    found = fresh.get_agents_for_prompt("any word on thread-lease-8841?", limit=5)
    assert found and found[0].name == "Email to Sharanjeet re: lease"
