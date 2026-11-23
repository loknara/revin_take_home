import asyncio
import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from revin_take_home import app as app_module
from revin_take_home.app import Settings, create_app
from revin_take_home.guidance import (
    RetryTracker,
    normalize_update_intake,
    ordinal_words,
    shape,
    spoken_window,
    today_label,
)
from revin_take_home.office import NEW_YORK, office_status
from test_intake import SECRET, envelope

MONDAY_9AM = datetime(2026, 11, 23, 9, 5, tzinfo=NEW_YORK)


@pytest.mark.parametrize(
    "start,end,spoken",
    [
        ("2026-11-23T14:00:00+00:00", "2026-11-23T17:00:00+00:00", "today between nine and noon"),
        (
            "2026-11-24T18:00:00+00:00",
            "2026-11-24T21:00:00+00:00",
            "tomorrow, Tuesday, November twenty-fourth, between one and four",
        ),
        (
            "2026-11-27T14:30:00+00:00",
            "2026-11-27T17:00:00+00:00",
            "Friday, November twenty-seventh, between nine thirty and noon",
        ),
        (
            "2026-12-01T23:00:00+00:00",
            "2026-12-02T02:00:00+00:00",
            "Tuesday, December first, between six and nine in the evening",
        ),
    ],
)
def test_spoken_windows_are_relative_to_today_in_new_york(start, end, spoken):
    assert spoken_window(start, end, MONDAY_9AM) == spoken


def test_late_night_utc_still_uses_the_new_york_day():
    # 11:30 PM Monday in New York is already Tuesday in UTC.
    late = datetime(2026, 11, 23, 23, 30, tzinfo=NEW_YORK)
    assert spoken_window("2026-11-24T14:00:00Z", "2026-11-24T17:00:00Z", late).startswith("tomorrow, Tuesday")


@pytest.mark.parametrize(
    "n,word", [(1, "first"), (2, "second"), (12, "twelfth"), (20, "twentieth"), (23, "twenty-third"), (30, "thirtieth"), (31, "thirty-first")]
)
def test_ordinals(n, word):
    assert ordinal_words(n) == word


def test_today_is_in_call_context_and_refresh():
    assert today_label(MONDAY_9AM) == "Monday, November 23, 2026"
    assert office_status(MONDAY_9AM)["today"] == "Monday, November 23, 2026"
    assert shape("refresh_context", {"status": "current"}, now=MONDAY_9AM)["today"] == "Monday, November 23, 2026"


def test_normalize_moves_confirmations_out_and_facts_in():
    args, moved = normalize_update_intake(
        {
            "expected_revision": 3,
            "caller_quote": "Yes",
            "service_zip": "10023",
            "changes": {"address_confirmed": True, "confirmation_address": "152 West 72nd Street"},
        }
    )
    assert args == {
        "expected_revision": 3,
        "caller_quote": "Yes",
        "confirmation_address": "152 West 72nd Street",
        "changes": {"address_confirmed": True, "service_zip": "10023"},
    }
    assert moved == ["changes.confirmation_address", "service_zip"]


def test_normalize_builds_changes_when_missing_and_keeps_unknown_fields():
    args, moved = normalize_update_intake(
        {"expected_revision": 0, "caller_quote": "AC broken", "issue_summary": "AC broken", "call_id": "x"}
    )
    assert args["changes"] == {"issue_summary": "AC broken"} and args["call_id"] == "x"
    assert moved == ["issue_summary"]
    assert normalize_update_intake("not json") == ("not json", [])


def test_retry_tracker_stops_the_third_failure_and_resets_on_success():
    t = RetryTracker()
    fail = {"status": "confirmation_mismatch"}
    assert t.observe("c", "update_intake", dict(fail))["status"] == "confirmation_mismatch"
    assert t.observe("c", "update_intake", dict(fail))["status"] == "confirmation_mismatch"
    stopped = t.observe("c", "update_intake", {"status": "invalid_arguments"})
    assert stopped["status"] == "stop_retrying" and stopped["failed_status"] == "invalid_arguments"
    assert "technical_issue" in stopped["hint"]
    # Other tools and other calls are independent.
    assert t.observe("c", "search_slots", dict(fail))["status"] == "confirmation_mismatch"
    assert t.observe("other", "update_intake", dict(fail))["status"] == "confirmation_mismatch"
    t.observe("c", "update_intake", {"status": "saved"})
    assert t.observe("c", "update_intake", dict(fail))["status"] == "confirmation_mismatch"
    # Outcomes such as out_of_scope are answers, not a retry loop.
    for _ in range(4):
        assert t.observe("c", "request_followup", {"status": "out_of_scope"})["status"] == "out_of_scope"


def test_followup_stop_does_not_point_back_at_itself():
    t = RetryTracker()
    for _ in range(2):
        t.observe("c", "request_followup", {"status": "needs_details"})
    stopped = t.observe("c", "request_followup", {"status": "needs_details"})
    assert stopped["status"] == "stop_retrying" and "technical_issue" not in stopped["hint"]


def test_tracker_memory_is_bounded():
    t = RetryTracker(max_calls=3)
    for n in range(10):
        t.observe(f"call{n}", "update_intake", {"status": "invalid_arguments"})
    assert list(t.calls) == ["call7", "call8", "call9"]


@pytest.mark.parametrize(
    "status",
    ["invalid_arguments", "revision_conflict", "not_eligible", "stale_proposal", "outcome_unconfirmed", "out_of_scope", "no_availability"],
)
def test_every_non_success_result_carries_the_contract(status):
    result = shape("search_slots", {"status": status, "current_revision": 4, "readiness": {"missing_fields": ["service_zip"], "next_required": "service_zip"}})
    assert {"missing", "expected", "hint"} <= set(result)
    assert isinstance(result["missing"], list) and isinstance(result["expected"], dict)
    assert result["hint"].endswith(".")


def test_success_results_are_left_alone():
    assert shape("update_intake", {"status": "saved"}) == {"status": "saved"}


def test_not_eligible_names_what_to_collect():
    r = shape("propose_booking", {"status": "not_eligible", "readiness": {"missing_fields": ["callback_confirmed"], "next_required": "callback_confirmed"}})
    assert r["missing"] == ["callback_confirmed"] and "callback_confirmed" in r["hint"]


def test_slow_store_answers_inside_vapi_timeout(monkeypatch):
    monkeypatch.setattr(app_module, "TOOL_TIMEOUT_SECONDS", 0.05)

    class Slow:
        async def update(self, *args):
            await asyncio.sleep(1)

    api = TestClient(
        create_app(
            Settings(assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=SECRET),
            intake_store=Slow(),
        )
    )
    body = envelope({"expected_revision": 0, "caller_quote": "AC broken", "changes": {"issue_summary": "AC broken"}})
    r = api.post("/vapi/tools", json=body, headers={"Authorization": "Bearer " + SECRET})
    result = json.loads(r.json()["results"][0]["result"])
    assert result["status"] == "save_unconfirmed" and "refresh_context" in result["hint"]


def test_invalid_arguments_name_the_bad_field_over_http():
    api = TestClient(
        create_app(Settings(assistant_id="c1d4d1d8-b2e4-48b9-9ffe-ca4bbea2dd43", webhook_secret=SECRET))
    )
    body = envelope(json.dumps({"expected_revision": 0, "caller_quote": "x", "changes": {"service_zip": "1002"}}))
    r = api.post("/vapi/tools", json=body, headers={"Authorization": "Bearer " + SECRET})
    result = json.loads(r.json()["results"][0]["result"])
    assert result["status"] == "invalid_arguments"
    assert result["bad_fields"] == ["changes.service_zip"] and "changes.service_zip" in result["hint"]


def test_evaluation_evidence_includes_times_tiers_and_alerts():
    import inspect

    from revin_take_home.evaluation_api import SupabaseEvaluationStore

    source = inspect.getsource(SupabaseEvaluationStore.evidence)
    assert "slot:appointment_slots(starts_at,ends_at)" in source
    assert "priority_tier,observed_reasons" in source and '"dispatcher_alerts"' in source
