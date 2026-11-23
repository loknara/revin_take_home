"""The earliest window is held as an offer in the same search call (dead-air pass)."""

import json

from fastapi.testclient import TestClient

from revin_take_home.app import Settings, create_app
from revin_take_home.workflow import WorkflowUnavailable

SECRET = "x" * 32
SLOT = "11111111-1111-4111-8111-111111111111"
SEARCH = "22222222-2222-4222-8222-222222222222"


class Workflow:
    def __init__(self, search, propose):
        self.search, self.propose, self.calls = search, propose, []

    async def execute(self, name, call_id, operation_id, request, originating_number=None):
        self.calls.append((name, operation_id, request.model_dump(mode="json")))
        if name == "search_slots":
            return json.loads(json.dumps(self.search))
        if isinstance(self.propose, Exception):
            raise self.propose
        return json.loads(json.dumps(self.propose))


def search(workflow):
    app = TestClient(
        create_app(
            Settings(assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=SECRET),
            workflow_store=workflow,
        )
    )
    r = app.post(
        "/vapi/tools",
        headers={"Authorization": "Bearer " + SECRET},
        json={
            "message": {
                "type": "tool-calls",
                "call": {"type": "inboundPhoneCall", "id": "c1"},
                "toolCallList": [{"id": "op1", "function": {"name": "search_slots", "arguments": {"expected_revision": 4}}}],
            }
        },
    )
    return json.loads(r.json()["results"][0]["result"])


WINDOW = {"slot_id": SLOT, "starts_at": "2026-11-24T14:00:00+00:00", "ends_at": "2026-11-24T17:00:00+00:00"}
AVAILABLE = {"status": "available", "search_id": SEARCH, "slots": [WINDOW]}
OFFERED = {"status": "offered", "proposal_id": "p1", "arrival": dict(WINDOW)}


def test_search_holds_the_earliest_window_as_an_offer():
    workflow = Workflow(AVAILABLE, OFFERED)
    result = search(workflow)
    assert result["status"] == "available"
    assert result["offer"]["proposal_id"] == "p1" and result["offer"]["spoken"]
    name, operation, args = workflow.calls[1]
    assert name == "propose_booking" and operation == "op1:offer"
    assert args == {"expected_revision": 4, "search_id": SEARCH, "slot_id": SLOT}


def test_no_offer_when_nothing_is_available_or_holding_fails():
    workflow = Workflow({"status": "no_availability", "slots": []}, OFFERED)
    assert "offer" not in search(workflow) and len(workflow.calls) == 1
    workflow = Workflow(AVAILABLE, WorkflowUnavailable())
    result = search(workflow)
    assert result["status"] == "available" and "offer" not in result
    workflow = Workflow(AVAILABLE, {"status": "stale_availability"})
    assert "offer" not in search(workflow)
