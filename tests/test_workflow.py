import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from revin_take_home.app import Settings, create_app
from revin_take_home.workflow import WorkflowStore
from test_intake import SECRET, envelope


@pytest.mark.parametrize(
    "hazard",
    [
        "gas_smell",
        "carbon_monoxide_alarm",
        "smoke_or_burning_smell",
        "electrical_sparking",
    ],
)
def test_safety_emergency_is_bound_to_provider_call_and_operation(hazard):
    from revin_take_home.workflow import SafetyEmergency

    async def run():
        seen = []

        def handler(request):
            seen.append((request.url.path, json.loads(request.content)))
            return httpx.Response(
                200,
                json={
                    "status": "saved",
                    "state": {"originating_number": "+12125550101"},
                },
            )

        async with httpx.AsyncClient(
            base_url="https://test/rest/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            result = await WorkflowStore(client).execute(
                "report_safety_emergency",
                "provider_call",
                "op",
                SafetyEmergency(hazard=hazard, caller_quote="My alarm is going off"),
            )
            assert "originating_number" not in result["state"]
            assert seen == [
                (
                    "/rest/v1/rpc/report_safety_emergency",
                    {
                        "p_call_id": "provider_call",
                        "p_operation_id": "op",
                        "p_hazard": hazard,
                        "p_caller_quote": "My alarm is going off",
                        "p_originating_number": None,
                    },
                )
            ]

    asyncio.run(run())


def test_unknown_hazard_is_rejected():
    from pydantic import ValidationError
    from revin_take_home.workflow import SafetyEmergency

    with pytest.raises(ValidationError):
        SafetyEmergency(hazard="spilled_coffee", caller_quote="Oops")


@pytest.mark.parametrize(
    "name,args",
    [
        ("refresh_context", {"call_id": "other"}),
        ("search_slots", {"expected_revision": True}),
    ],
)
def test_workflow_tools_reject_scope_override(name, args):
    app = TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
            )
        )
    )
    body = envelope(args)
    body["message"]["toolCallList"][0]["function"]["name"] = name
    r = app.post(
        "/vapi/tools", json=body, headers={"Authorization": "Bearer " + SECRET}
    )
    assert json.loads(r.json()["results"][0]["result"])["status"] == "invalid_arguments"


def test_storage_outage_never_fabricates_booking_or_empty_availability():
    def fail(request):
        raise httpx.ReadTimeout("test outage")

    store = WorkflowStore(
        httpx.AsyncClient(
            base_url="https://test/rest/v1/", transport=httpx.MockTransport(fail)
        )
    )
    app = TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
            ),
            workflow_store=store,
        )
    )
    from uuid import uuid4

    for name, args in [
        ("search_slots", {"expected_revision": 2}),
        ("get_booking_status", {}),
        (
            "confirm_booking",
            {
                "expected_revision": 2,
                "proposal_id": str(uuid4()),
                "decision": "accepted",
                "caller_quote": "Yes",
            },
        ),
    ]:
        body = envelope(args)
        body["message"]["toolCallList"][0]["function"]["name"] = name
        result = json.loads(
            app.post(
                "/vapi/tools", json=body, headers={"Authorization": "Bearer " + SECRET}
            ).json()["results"][0]["result"]
        )
        assert result["status"] == "outcome_unconfirmed"
        assert "slots" not in result and "reference" not in result


def test_call_end_requires_auth_and_inbound_scope():
    app = TestClient(
        create_app(
            Settings(
                assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43",
                webhook_secret=SECRET,
            )
        )
    )
    body = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "call", "type": "inboundPhoneCall"},
            "endedReason": "customer-ended-call",
        }
    }
    assert app.post("/vapi/events", json=body).status_code == 401
    assert (
        app.post(
            "/vapi/events", json=body, headers={"Authorization": "Bearer " + SECRET}
        ).status_code
        == 503
    )
    body["message"]["call"]["type"] = "webCall"
    assert (
        app.post(
            "/vapi/events", json=body, headers={"Authorization": "Bearer " + SECRET}
        ).status_code
        == 422
    )
