import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from revin_take_home.app import Settings, create_app
from revin_take_home.safety_backstop import (
    caller_turns,
    detect_hazard,
    record_missed_emergency,
)
from revin_take_home.workflow import WorkflowUnavailable

SECRET = "local-test-only-secret-0123456789abcdef"
EVACUATE = (
    "Please get everyone out of the home now, and call nine one one once you're "
    "outside. Don't use light switches or appliances on your way out."
)


def artifact(*caller, tool_result=None):
    """Vapi end-of-call artifact: caller turns, the agent's guidance, an optional tool result."""
    messages = [{"role": "system", "message": "prompt"}]
    for text in caller:
        messages.append({"role": "user", "message": text})
    messages.append({"role": "bot", "message": EVACUATE})
    messages.append(
        {
            "role": "tool_calls",
            "toolCalls": [{"function": {"name": "report_safety_emergency"}}],
        }
    )
    if tool_result is not None:
        messages.append(
            {
                "role": "tool_call_result",
                "name": "report_safety_emergency",
                "result": json.dumps(tool_result),
            }
        )
    return {"messages": messages, "transcript": "AI: ..."}


@pytest.mark.parametrize(
    "text, hazard",
    [
        # Opening lines from the 2026-11-23 voice runs that left no record.
        ("My carbon monoxide detector keeps going off.", "carbon_monoxide_alarm"),
        ("I can smell gas near my furnace.", "gas_smell"),
        ("It smells like rotten eggs in the basement.", "gas_smell"),
        ("There's a gas smell by the boiler.", "gas_smell"),
        ("I'm not sure, but I smell gas.", "gas_smell"),
        ("The CO alarm went off earlier but it stopped.", "carbon_monoxide_alarm"),
        ("My C O detector is beeping.", "carbon_monoxide_alarm"),
        ("There's smoke coming out of the vents.", "smoke_or_burning_smell"),
        ("The furnace smells like something burning.", "smoke_or_burning_smell"),
        ("There's a burning smell from the AC.", "smoke_or_burning_smell"),
        ("The outlet behind the unit is sparking.", "electrical_sparking"),
        ("I saw sparks when it turned on.", "electrical_sparking"),
    ],
)
def test_detects_current_hazard_reports(text, hazard):
    found = detect_hazard([text])
    assert found is not None and found[0] == hazard


@pytest.mark.parametrize(
    "text",
    [
        "What if I smell gas?",
        "What should I do if I smell gas?",
        "If my CO alarm goes off, should I call you?",
        "In case there's a burning smell, who do I call?",
        "How do I know if my furnace is leaking gas?",
        "There's no gas smell, the heat just doesn't work.",
        "I don't smell gas or anything.",
        "It's not sparking, it just clicks.",
        "My heat is out and it's freezing in here.",
        "Do you handle gas furnaces?",
        "I have a carbon monoxide detector in the hallway.",
    ],
)
def test_ignores_hypothetical_negated_and_unrelated_mentions(text):
    assert detect_hazard([text]) is None


def test_reads_only_caller_turns():
    # The agent's own guidance mentions hazards; it must never trigger a record.
    data = {
        "messages": [
            {"role": "bot", "message": "If you smell gas, leave and call 911."},
            {"role": "user", "message": "No, my AC just won't cool."},
        ]
    }
    assert caller_turns(data) == ["No, my AC just won't cool."]
    assert detect_hazard(caller_turns(data)) is None
    assert caller_turns({"transcript": "AI: Hi\nUser: I smell gas."}) == [" I smell gas."]


class FakeStore:
    def __init__(self, status="saved"):
        self.status = status
        self.calls = []

    async def execute(self, name, call_id, operation_id, request):
        self.calls.append((name, call_id, operation_id, request.model_dump()))
        return {"status": self.status}


def test_records_hazard_when_caller_hung_up_before_the_tool_request():
    store = FakeStore()
    data = artifact("My carbon monoxide detector keeps going off.")
    hazard = asyncio.run(record_missed_emergency(store, "call-1", data))
    assert hazard == "carbon_monoxide_alarm"
    assert store.calls == [
        (
            "report_safety_emergency",
            "call-1",
            "end-of-call-backstop:carbon_monoxide_alarm",
            {
                "hazard": "carbon_monoxide_alarm",
                "caller_quote": "My carbon monoxide detector keeps going off.",
            },
        )
    ]


def test_skips_when_the_call_already_recorded_the_emergency():
    store = FakeStore()
    data = artifact("I can smell gas near my furnace.", tool_result={"status": "saved"})
    assert asyncio.run(record_missed_emergency(store, "call-1", data)) is None
    assert store.calls == []


def test_records_when_the_in_call_attempt_failed():
    # voice gas_emergency: the model sent an extra argument, so the save was rejected.
    store = FakeStore()
    data = artifact(
        "I can smell gas near my furnace.", tool_result={"status": "invalid_arguments"}
    )
    assert asyncio.run(record_missed_emergency(store, "call-1", data)) == "gas_smell"


def test_hypothetical_question_records_nothing():
    store = FakeStore()
    data = artifact("My heat isn't working.", "What if I smell gas later, what do I do?")
    assert asyncio.run(record_missed_emergency(store, "call-1", data)) is None
    assert store.calls == []


def test_unconfirmed_save_raises():
    with pytest.raises(WorkflowUnavailable):
        asyncio.run(
            record_missed_emergency(
                FakeStore("revision_conflict"), "call-1", artifact("I smell gas.")
            )
        )


class FakeAudit:
    def __init__(self):
        self.calls = []

    async def call(self, call_id, ended_reason=None):
        self.calls.append((call_id, ended_reason))
        return True


def end_of_call(data):
    return {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "call-9", "type": "inboundPhoneCall"},
            "endedReason": "customer-ended-call",
            "artifact": data,
        }
    }


def app_with(store, audit):
    return TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
            ),
            workflow_store=store,
            audit_store=audit,
        )
    )


def test_end_of_call_report_runs_the_backstop_and_still_audits():
    store, audit = FakeStore(), FakeAudit()
    response = app_with(store, audit).post(
        "/vapi/events",
        json=end_of_call(artifact("I can smell gas near my furnace.")),
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert response.status_code == 200
    assert [c[2] for c in store.calls] == ["end-of-call-backstop:gas_smell"]
    assert audit.calls == [("call-9", "customer-ended-call")]


def test_end_of_call_report_fails_loudly_when_backstop_save_is_unconfirmed():
    store, audit = FakeStore("revision_conflict"), FakeAudit()
    response = app_with(store, audit).post(
        "/vapi/events",
        json=end_of_call(artifact("My carbon monoxide detector keeps going off.")),
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert response.status_code == 503
    assert audit.calls == [("call-9", "customer-ended-call")]


def test_end_of_call_report_without_artifact_is_unchanged():
    store, audit = FakeStore(), FakeAudit()
    body = end_of_call(None)
    del body["message"]["artifact"]
    response = app_with(store, audit).post(
        "/vapi/events", json=body, headers={"Authorization": "Bearer " + SECRET}
    )
    assert response.status_code == 200 and store.calls == []


def test_emergency_request_accepts_and_drops_a_stray_revision():
    """A habitual expected_revision must never block an emergency save."""
    from revin_take_home.workflow import SafetyEmergency

    request = SafetyEmergency(hazard="gas_smell", caller_quote="I smell gas", expected_revision=0)
    assert request.model_dump(mode="json") == {"hazard": "gas_smell", "caller_quote": "I smell gas"}
