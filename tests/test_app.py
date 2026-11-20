import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from revin_take_home.app import Settings, create_app
from revin_take_home.demo import DemoDirectory, UnavailableDirectory

ASSISTANT_ID = "c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43"
SECRET = "local-test-only-secret-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {SECRET}"}


def client(directory=None):
    return TestClient(create_app(
        Settings(assistant_id=ASSISTANT_ID, webhook_secret=SECRET),
        directory if directory is not None else DemoDirectory()
    ))


def event(number="+12125550101"):
    return {"message": {
        "type": "assistant-request",
        "call": {"id": "call_test", "type": "inboundPhoneCall"},
        "customer": {"number": number},
        "phoneNumber": {"number": "+12125550102"},
    }}


def post(payload, directory=None):
    return client(directory).post(
        "/vapi/assistant-request", json=payload, headers=AUTH
    )


def context(response):
    assert response.status_code == 200
    return json.loads(response.json()["assistantOverrides"]["variableValues"]["caller_context"])


def test_returns_existing_assistant_and_minimal_unverified_context():
    response = post(event())
    assert response.json()["assistantId"] == ASSISTANT_ID
    assert set(response.json()["assistantOverrides"]) == {"variableValues"}
    assert context(response) == {
        "call_id": "call_test", "lookup_status": "matched_single",
        "candidate": {"customer_id": "demo_maya", "first_name": "Maya"},
        "identity_status": "unconfirmed", "membership_status": "unknown",
    }
    assert "+1212" not in response.text


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                       {"Authorization": f"Basic {SECRET}"}])
def test_unauthorized_requests_never_lookup(headers):
    class MustNotBeCalled:
        async def find_by_phone(self, number):
            raise AssertionError("Unauthorized lookup")

    response = client(MustNotBeCalled()).post(
        "/vapi/assistant-request", json=event(), headers=headers
    )
    assert response.status_code == 401


@pytest.mark.parametrize("number,status", [
    ("+12125550102", "matched_multiple"),
    ("+12125550103", "no_match"),
    (None, "number_unavailable"),
    ("anonymous", "number_unavailable"),
])
def test_generic_context_for_shared_unknown_or_missing_number(number, status):
    result = context(post(event(number)))
    assert result["lookup_status"] == status
    assert result["candidate"] is None
    assert result["membership_status"] == "unknown"


def test_supports_call_customer_and_requires_agreement_if_both_present():
    payload = event()
    payload["message"]["call"]["customer"] = {"number": "(212) 555-0101"}
    assert context(post(payload))["lookup_status"] == "matched_single"
    del payload["message"]["customer"]
    assert context(post(payload))["lookup_status"] == "matched_single"
    payload["message"]["customer"] = {"number": "+12125550102"}
    assert context(post(payload))["lookup_status"] == "number_unavailable"


def test_never_uses_service_number_or_conversation_as_caller_id():
    payload = event()
    del payload["message"]["customer"]
    payload["message"]["call"]["messages"] = [
        {"role": "user", "message": "My number is +12125550101"}
    ]
    assert context(post(payload))["lookup_status"] == "number_unavailable"


def test_directory_outage_still_returns_assistant_with_generic_context():
    assert context(post(event(), UnavailableDirectory()))["lookup_status"] == "lookup_unavailable"


@pytest.mark.parametrize("change", ["no_call", "web_call", "wrong_event"])
def test_rejects_invalid_call_start_envelopes(change):
    payload = event()
    if change == "no_call":
        del payload["message"]["call"]
    elif change == "web_call":
        payload["message"]["call"]["type"] = "webCall"
    else:
        payload["message"]["type"] = "tool-calls"
    assert post(payload).status_code == 422


def test_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("VAPI_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("VAPI_ASSISTANT_ID", ASSISTANT_ID)
    with pytest.raises(KeyError):
        create_app()
    with pytest.raises(ValidationError):
        Settings(assistant_id=ASSISTANT_ID, webhook_secret="")


def test_health_check_has_no_customer_data():
    assert client().get("/health").json() == {"status": "ok"}


def test_unconfigured_database_returns_generic_intake_not_hardcoded_customers():
    with TestClient(create_app(Settings(
        assistant_id=ASSISTANT_ID, webhook_secret=SECRET,
    ))) as unconfigured:
        result = context(unconfigured.post(
            "/vapi/assistant-request", json=event(), headers=AUTH
        ))
    assert result["lookup_status"] == "lookup_unavailable"
    assert result["candidate"] is None


@pytest.mark.parametrize("config", [
    {"supabase_url": "https://test.supabase.co"},
    {"supabase_secret_key": "sb_secret_test"},
    {"supabase_url": "http://test.supabase.co", "supabase_secret_key": "sb_secret_test"},
])
def test_incomplete_database_config_fails_at_startup(config):
    with pytest.raises(ValidationError):
        Settings(assistant_id=ASSISTANT_ID, webhook_secret=SECRET, **config)
