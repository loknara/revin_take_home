"""bot_say is Vapi's spoken filler: it must never reach validation or the database."""

import json

import pytest
from fastapi.testclient import TestClient

from revin_take_home.app import Settings, create_app

SECRET = "x" * 32


class Store:
    def __init__(self):
        self.received = []

    async def update(self, call_id, operation_id, number, request):
        self.received.append(request)
        return {"status": "saved", "state": {"revision": 1, "details": {}}}


class Workflow:
    def __init__(self):
        self.calls = []

    async def execute(self, name, call_id, operation_id, request, originating_number=None):
        self.calls.append((name, request.model_dump(mode="json")))
        return {"status": "found", "appointments": []}


def post(name, arguments, **stores):
    app = TestClient(
        create_app(Settings(assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=SECRET), **stores)
    )
    r = app.post(
        "/vapi/tools",
        headers={"Authorization": "Bearer " + SECRET},
        json={"message": {"type": "tool-calls", "call": {"type": "inboundPhoneCall", "id": "c1"},
                          "toolCallList": [{"id": "op", "function": {"name": name, "arguments": arguments}}]}},
    )
    return json.loads(r.json()["results"][0]["result"])


@pytest.mark.parametrize("as_text", [False, True])
@pytest.mark.parametrize("filler", ["Okay, adding that in", ""])
def test_update_intake_ignores_the_spoken_filler(as_text, filler):
    store = Store()
    args = {"expected_revision": 0, "caller_quote": "It's Alex", "changes": {"caller_name": "Alex"}, "bot_say": filler}
    result = post("update_intake", json.dumps(args) if as_text else args, intake_store=store)
    assert result["status"] == "saved" and "moved_into_place" not in result
    assert store.received[0].changes.caller_name == "Alex"


def test_workflow_tools_ignore_the_spoken_filler():
    workflow = Workflow()
    result = post("find_my_appointment", json.dumps({"bot_say": "Let me pull that up"}), workflow_store=workflow)
    assert result["status"] == "found"
    assert workflow.calls == [("find_my_appointment", {})]
