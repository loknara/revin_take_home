"""Round 3 finding R9: a confirmation needs a readback the caller actually heard.

Conversations below are taken from the failing voice calls (artifact.messages shape as
Vapi sends it with tool calls: roles bot / user / tool_calls)."""

import json

from fastapi.testclient import TestClient

from revin_take_home.app import Settings, create_app
from revin_take_home.readback import (
    address_was_read,
    number_was_read,
    recent_agent_text,
    unheard_confirmations,
)

SECRET = "x" * 32


def convo(*turns):
    return {"messages": [{"role": r, "message": m} for r, m in turns]}


RESCHEDULE = convo(
    ("bot", "I can't look up or change existing appointments, but I can have someone call you. Am I speaking with Maya?"),
    ("user", "Yes, this is Maya. That would be great."),
    ("tool_calls", None),
    ("user", "Please have someone from your office call me."),
)
DECLINED_OFFER = convo(
    ("bot", "Happy to set that up. Am I speaking with Maya?"),
    ("user", "Yes, this is Maya Chen."),
    ("bot", "I have 152 West 72nd Street, apartment 4B, ZIP 10023 on your account. Is that where you need the service?"),
    ("user", "Yes, that's correct. Please schedule the maintenance at 152 West 72nd Street."),
    ("user", "Apartment 4B, New York, New York 10023."),
)
CORRECTED_NUMBER = convo(
    ("bot", "Just to confirm, that's 152 West 72nd Street, no unit, New York, ZIP 10023, and the technician can reach you at 212-555-0199?"),
    ("user", "The address is correct, but my callback number is +1 212-555-0188."),
    ("tool_calls", None),
    ("bot", "Can I confirm the technician can reach you at 212-555-0188?"),
    ("user", "Yes, that's correct."),
)


def test_reschedule_without_any_readback_confirms_nothing():
    changes = {"address_confirmed": True, "callback_confirmed": True}
    assert unheard_confirmations(RESCHEDULE, changes, "152 West 72nd Street", "+12125550101") == [
        "address_confirmed",
        "callback_confirmed",
    ]


def test_address_readback_does_not_confirm_the_number():
    changes = {"callback_confirmed": True}
    assert unheard_confirmations(DECLINED_OFFER, changes, None, "+12125550101") == ["callback_confirmed"]
    assert unheard_confirmations(DECLINED_OFFER, {"address_confirmed": True}, "152 West 72nd Street", None) == []


def test_address_read_a_turn_earlier_still_counts_after_a_number_correction():
    changes = {"address_confirmed": True, "callback_confirmed": True}
    assert unheard_confirmations(CORRECTED_NUMBER, changes, "152 West 72nd Street", "+12125550188") == []


def test_recent_agent_text_skips_consecutive_caller_messages_and_tool_calls():
    text = recent_agent_text(DECLINED_OFFER, turns=1)
    assert text.startswith("I have 152 West 72nd Street")


def test_spoken_numbers_and_calling_from_count():
    assert number_was_read("reach you at two one two, five five five, zero one eight eight?", "+12125550188")
    assert number_was_read("can they reach you at the number you're calling from?", "+12125550188")
    assert not number_was_read("Is that where you need the service?", "+12125550188")
    assert address_was_read("one fifty-two West Seventy-Second Street, apartment four B", "152 West 72nd Street")
    assert address_was_read("that's 99 Example Street", "99 Example Street")
    assert not address_was_read("Am I speaking with Maya?", "99 Example Street")


def test_without_a_conversation_the_check_is_skipped():
    assert unheard_confirmations(None, {"address_confirmed": True}, "1 Main St", None) == []
    assert unheard_confirmations({"messages": []}, {"callback_confirmed": True}, None, "+12125550101") == []


class Store:
    def __init__(self):
        self.received = []

    async def update(self, call_id, operation_id, number, request):
        self.received.append(request)
        return {"status": "saved", "state": {"revision": 3, "details": {}}}


def post(store, arguments, artifact):
    app = TestClient(
        create_app(
            Settings(assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=SECRET),
            intake_store=store,
        )
    )
    r = app.post(
        "/vapi/tools",
        headers={"Authorization": "Bearer " + SECRET},
        json={
            "message": {
                "type": "tool-calls",
                "call": {"type": "inboundPhoneCall", "id": "c1", "customer": {"number": "+12125550101"}},
                "toolCallList": [{"id": "op", "function": {"name": "update_intake", "arguments": arguments}}],
                "artifact": artifact,
            }
        },
    )
    return json.loads(r.json()["results"][0]["result"])


def test_webhook_refuses_an_unheard_confirmation_without_writing():
    store = Store()
    result = post(
        store,
        {
            "expected_revision": 2,
            "caller_quote": "Please have someone from your office call me.",
            "changes": {"address_confirmed": True, "callback_confirmed": True},
            "confirmation_address": "152 West 72nd Street",
            "confirmation_callback": "+12125550101",
        },
        RESCHEDULE,
    )
    assert result["status"] == "needs_caller_yes" and result["reason"] == "not_read_back"
    assert "address and number" in result["hint"]
    assert result["expected"]["confirmation_callback"] == "+12125550101"
    assert store.received == []


def test_webhook_saves_the_heard_part_and_refuses_the_rest():
    store = Store()
    result = post(
        store,
        {
            "expected_revision": 2,
            "caller_quote": "Apartment 4B, New York, New York 10023.",
            "changes": {"address_confirmed": True, "callback_confirmed": True},
            "confirmation_address": "152 West 72nd Street",
            "confirmation_callback": "+12125550101",
        },
        DECLINED_OFFER,
    )
    assert result["status"] == "needs_caller_yes" and result["unheard"] == ["callback_confirmed"]
    assert result["saved_other_changes"] is True
    saved = store.received[0]
    assert saved.changes.address_confirmed is True and saved.changes.callback_confirmed is None
    assert saved.confirmation_callback is None
