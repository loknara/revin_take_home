"""Membership reaches the model in the save response, with bounded failure."""

import asyncio
import copy
import json

import httpx
import pytest

from revin_take_home.intake import SupabaseIntakeStore, UpdateIntake


def saved_result():
    return {
        "status": "saved",
        "state": {
            "call_id": "call",
            "revision": 2,
            "details": {
                "caller_name": "Maya Chen",
                "address_confirmed": True,
                "property_type": "residential",
            },
            "triage": {"level": "routine"},
        },
        "readiness": {
            "scope": "eligible",
            "coverage": "covered",
            "missing_fields": ["callback_number"],
        },
        "membership": {
            "status": "not_checked_for_current_details",
            "membership_status": "unknown",
        },
    }


def save_request():
    return UpdateIntake.model_validate(
        {
            "expected_revision": 1,
            "caller_quote": "Yes, that is my address",
            "changes": {"address_confirmed": True},
            "confirmation_address": "152 West 72nd Street",
        }
    )


def exercise(save, lookup):
    async def run():
        paths = []

        async def handler(request):
            paths.append(request.url.path.rsplit("/", 1)[-1])
            if paths[-1] == "update_call_intake":
                return httpx.Response(200, json=copy.deepcopy(save))
            assert paths[-1] == "resolve_call_membership"
            assert json.loads(request.content) == {
                "p_call_id": "call",
                "p_expected_revision": 2,
            }
            return await lookup(request)

        async with httpx.AsyncClient(
            base_url="https://test/rest/v1/", transport=httpx.MockTransport(handler)
        ) as client:
            result = await SupabaseIntakeStore(client).update(
                "call", "operation", None, save_request()
            )
        return result, paths

    return asyncio.run(run())


async def active(_):
    return httpx.Response(
        200,
        json={
            "status": "resolved",
            "membership_status": "active",
            "intake_revision": 2,
        },
    )


def test_address_confirmation_returns_membership_without_refresh_or_job():
    result, paths = exercise(saved_result(), active)
    assert paths == ["update_call_intake", "resolve_call_membership"]
    assert result["status"] == "saved"
    assert result["membership"]["membership_status"] == "active"
    assert "membership_lookup" not in result


@pytest.mark.parametrize("failure", ["timeout", "http_error", "malformed"])
def test_lookup_failure_does_not_discard_confirmed_intake(failure):
    async def fail(_):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(503 if failure == "http_error" else 200, json={})

    result, _ = exercise(saved_result(), fail)
    assert result["status"] == "saved"
    assert result["state"]["revision"] == 2
    assert result["state"]["details"]["address_confirmed"] is True
    assert result["membership"]["status"] == "lookup_unavailable"
    assert result["membership"]["membership_status"] == "unknown"


def test_total_lookup_budget_cancels_slow_query_and_keeps_save(monkeypatch):
    monkeypatch.setattr("revin_take_home.intake.MEMBERSHIP_TIMEOUT_SECONDS", 0.01)
    cancelled = []

    async def slow(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    result, _ = exercise(saved_result(), slow)
    assert cancelled == [True]
    assert result["status"] == "saved"
    assert result["membership"]["membership_status"] == "unknown"


def test_priority_callers_get_the_bounded_membership_lookup():
    save = saved_result()
    save["state"]["triage"]["level"] = "priority"
    result, paths = exercise(save, active)
    assert paths == ["update_call_intake", "resolve_call_membership"]
    assert result["membership"]["membership_status"] == "active"


@pytest.mark.parametrize("level", ["emergency", "needs_assessment"])
def test_risk_handling_never_waits_for_membership(level):
    save = saved_result()
    save["state"]["triage"]["level"] = level
    _, paths = exercise(save, active)
    assert paths == ["update_call_intake"]


@pytest.mark.parametrize(
    "change", ["unconfirmed", "no_name", "out_of_scope", "out_of_area", "conflict"]
)
def test_unready_or_conflicted_intake_does_not_lookup(change):
    save = saved_result()
    if change == "unconfirmed":
        save["state"]["details"]["address_confirmed"] = False
    if change == "no_name":
        save["state"]["details"].pop("caller_name")
    if change == "out_of_scope":
        save["readiness"]["scope"] = "out_of_scope"
    if change == "out_of_area":
        save["readiness"]["coverage"] = "out_of_area"
    if change == "conflict":
        save["status"] = "revision_conflict"
    _, paths = exercise(save, active)
    assert paths == ["update_call_intake"]


@pytest.mark.parametrize(
    "status,member", [("resolved", "active"), ("no_match", "unknown")]
)
def test_fresh_database_validated_cache_is_returned_without_repeat_lookup(
    status, member
):
    save = saved_result()
    save["membership"] = {"status": status, "membership_status": member}
    result, paths = exercise(save, active)
    assert paths == ["update_call_intake"]
    assert result["membership"] == save["membership"]


def test_correction_between_save_and_lookup_cannot_report_old_membership():
    async def conflict(_):
        return httpx.Response(
            200,
            json={
                "status": "revision_conflict",
                "membership_status": "unknown",
                "current_revision": 3,
            },
        )

    result, _ = exercise(saved_result(), conflict)
    assert result["status"] == "saved"
    assert result["membership"]["status"] == "revision_conflict"
    assert result["membership"]["membership_status"] == "unknown"


def test_live_toolset_retires_background_start_and_keeps_recovery():
    from revin_take_home.evaluation import ALLOWED_TOOLS
    from revin_take_home.workflow import WORKFLOW_SCHEMAS

    assert "start_membership_check" not in ALLOWED_TOOLS | WORKFLOW_SCHEMAS.keys()
    assert "refresh_context" in ALLOWED_TOOLS


def test_inline_evaluation_requires_result_in_save_and_no_polling():
    from revin_take_home.evaluation import verify_conversation

    case = {"expected": {"inline_membership": True, "membership": "active"}}
    conversation = {
        "messages": [
            {
                "role": "tool_call_result",
                "result": json.dumps(
                    {
                        "status": "saved",
                        "membership": {"membership_status": "active"},
                    }
                ),
            }
        ]
    }
    assert all(c["passed"] for c in verify_conversation(case, conversation))
    conversation["messages"][0]["result"] = json.dumps(
        {"status": "current", "membership": {"membership_status": "active"}}
    )
    assert not next(
        c["passed"]
        for c in verify_conversation(case, conversation)
        if c["name"] == "membership_result_reached_model"
    )
    conversation["messages"].append(
        {"role": "tool_calls", "toolCalls": [{"function": {"name": "refresh_context"}}]}
    )
    assert not next(
        c["passed"]
        for c in verify_conversation(case, conversation)
        if c["name"] == "no_routine_membership_polling"
    )
