import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from revin_take_home.app import Settings, create_app
from revin_take_home.intake import UpdateIntake, SupabaseIntakeStore

SECRET = "intake-test-secret-0123456789abcdef"


def request(**overrides):
    return {
        "expected_revision": 0,
        "caller_quote": "My AC won't turn on",
        "changes": {"issue_summary": "AC not turning on"},
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"call_id": "another_call"},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"changes": {}},
        {"changes": {"issue_summary": None}},
        {"changes": {"customer_id": "demo_maya"}},
        {"changes": {"service_address": " "}},
        {"changes": {"address_confirmed": "true"}},
        {"changes": {"address_confirmed": True}},
        {
            "changes": {"service_address": "152 West 72nd", "address_confirmed": True},
            "confirmation_address": "152 West 72nd",
        },
    ],
)
def test_rejects_unsafe_or_ambiguous_changes(overrides):
    with pytest.raises(ValidationError):
        UpdateIntake.model_validate(request(**overrides))


def envelope(arguments):
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": "provider_call", "type": "inboundPhoneCall"},
            "customer": {"number": "+12125550101"},
            "toolCallList": [
                {
                    "id": "provider_operation",
                    "function": {
                        "name": "update_intake",
                        "arguments": arguments,
                    },
                }
            ],
        }
    }


class Store:
    def __init__(self):
        self.received = []

    async def update(self, *args):
        self.received.append(args)
        return {"status": "saved"}


def client(store):
    return TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
            ),
            intake_store=store,
        )
    )


@pytest.mark.parametrize("string_args", [False, True])
def test_binds_call_and_operation_to_provider_metadata(string_args):
    store = Store()
    args = json.dumps(request()) if string_args else request()
    response = client(store).post(
        "/vapi/tools",
        json=envelope(args),
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["toolCallId"] == "provider_operation"
    assert store.received[0][:3] == (
        "provider_call",
        "provider_operation",
        "+12125550101",
    )


def test_invalid_tool_args_and_missing_auth_never_write():
    store = Store()
    app = client(store)
    assert app.post("/vapi/tools", json=envelope(request())).status_code == 401
    response = app.post(
        "/vapi/tools",
        json=envelope(request(call_id="other_call")),
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert (
        json.loads(response.json()["results"][0]["result"])["status"]
        == "invalid_arguments"
    )
    assert store.received == []


def test_missing_storage_does_not_claim_saved():
    response = client(None).post(
        "/vapi/tools",
        json=envelope(request()),
        headers={"Authorization": "Bearer " + SECRET},
    )
    assert (
        json.loads(response.json()["results"][0]["result"])["status"]
        == "save_unconfirmed"
    )


def test_saved_commercial_intake_is_not_booking_eligibility():
    async def run():
        def handler(req):
            assert req.url.path == "/rest/v1/rpc/update_call_intake"
            assert json.loads(req.content)["p_call_id"] == "call"
            return httpx.Response(
                200,
                json={
                    "status": "saved",
                    "state": {
                        "call_id": "call",
                        "revision": 1,
                        "originating_number": "+12125550101",
                        "details": {
                            "property_type": "commercial",
                            "address_confirmed": False,
                        },
                        "triage": {"level": "needs_assessment"},
                    },
                },
            )

        async with httpx.AsyncClient(
            base_url="https://test.supabase.co/rest/v1/",
            transport=httpx.MockTransport(handler),
        ) as http:
            result = await SupabaseIntakeStore(http).update(
                "call", "operation", None, UpdateIntake.model_validate(request())
            )
            assert result["eligibility"] == "out_of_scope"
            assert result["booking_status"] == "not_checked"
            assert "address_confirmation" in result["missing_fields"]
            assert "originating_number" not in result["state"]

    asyncio.run(run())


@pytest.mark.parametrize("value", [True, False, "unknown"])
def test_risk_fields_preserve_true_false_and_unknown(value):
    result = UpdateIntake.model_validate(
        request(changes={"vulnerable_resident": value})
    )
    assert (
        result.changes.vulnerable_resident is value
        or result.changes.vulnerable_resident == value
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"gas_smell_reported": "false"},
        {"gas_smell_reported": 1},
        {"gas_smell_reported": None},
        {"triage": "routine"},
        {"service_type": "emergency"},
        {"notification_status": "notified"},
    ],
)
def test_model_cannot_supply_decisions_or_ambiguous_risk_flags(changes):
    with pytest.raises(ValidationError):
        UpdateIntake.model_validate(request(changes=changes))
