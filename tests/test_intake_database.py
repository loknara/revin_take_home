"""Opt-in transaction checks against the linked demo Supabase project.

RUN_DATABASE_TESTS=1 uv run --env-file .env --with 'psycopg[binary]' pytest tests/test_intake_database.py
Only random test call IDs are created, then removed by the fixture.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DATABASE_TESTS") != "1", reason="Live database checks are opt-in"
)


@pytest.fixture
def database():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.types.json import Jsonb

    url = (os.environ.get('TEST_DATABASE_URL') or Path("supabase/.temp/pooler-url").read_text().strip())
    call_id = "test_intake_" + uuid4().hex

    def connect():
        return psycopg.connect(url, password=os.environ.get('SUPABASE_DB_PASSWORD', ''),
                               sslmode='disable' if os.environ.get('TEST_DATABASE_URL') else 'require', connect_timeout=15, autocommit=True)

    def update(op, revision, patch, quote="Caller said this", confirmation=None):
        with connect() as db:
            return db.execute(
                "select public.update_call_intake(%s,%s,%s,%s,%s,%s,%s)",
                (call_id, op, "+12125550101", revision, Jsonb(patch), quote, confirmation),
            ).fetchone()[0]

    yield update
    with connect() as db:
        db.execute("delete from public.call_intakes where call_id=%s", (call_id,))


def test_correction_clears_confirmation_and_preserves_other_facts(database):
    assert database("a", 0, {"service_address": "125 West 72nd", "issue_summary": "AC broken"})["status"] == "saved"
    assert database("b", 1, {"address_confirmed": True}, "Yes", "125 West 72nd")["state"]["details"]["address_confirmed"]
    result = database("c", 2, {"service_address": "152 West 72nd"})
    assert result["state"]["revision"] == 3
    assert result["state"]["details"]["address_confirmed"] is False
    assert "confirmation_quote" not in result["state"]["details"]
    assert result["state"]["details"]["issue_summary"] == "AC broken"


def test_retry_after_lost_response_does_not_write_twice(database):
    database("same", 0, {"issue_summary": "AC broken"})
    replay = database("same", 0, {"issue_summary": "AC broken"})
    assert replay["replayed"] is True
    assert replay["state"]["revision"] == 1
    assert database("same", 0, {"issue_summary": "Different"})["status"] == "operation_conflict"


def test_parallel_updates_allow_only_one_winner(database):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda op: database(op, 0, {"issue_summary": op}), ["first", "second"]))
    assert sorted(r["status"] for r in results) == ["revision_conflict", "saved"]
    assert all(r["state"]["revision"] == 1 for r in results)


def test_confirmation_is_bound_to_saved_address(database):
    database("a", 0, {"service_address": "152 West 72nd"})
    wrong = database("b", 1, {"address_confirmed": True}, "Yes", "125 West 72nd")
    assert wrong["status"] == "confirmation_mismatch"
    assert wrong["state"]["revision"] == 1
    assert database("c", 1, {"service_address": "154 West 72nd", "address_confirmed": True},
                    "Yes", "154 West 72nd")["status"] == "confirmation_mismatch"


def test_late_retry_returns_current_state_without_reverting_it(database):
    database("old", 0, {"service_address": "125 West 72nd"})
    database("new", 1, {"service_address": "152 West 72nd"})
    replay = database("old", 0, {"service_address": "125 West 72nd"})
    assert replay["replayed"] is True
    assert replay["state"]["revision"] == 2
    assert replay["state"]["details"]["service_address"] == "152 West 72nd"


@pytest.mark.parametrize("facts,level,reason", [
    ({"service_type": "maintenance"}, "routine", None),
    ({"gas_smell_reported": True}, "emergency", "gas_smell"),
    ({"service_type": "maintenance", "gas_smell_reported": True}, "emergency", "gas_smell"),
    ({"heating_unavailable": True, "cold_conditions_reported": True}, "priority", "no_heat_cold"),
    ({"heating_unavailable": True}, "needs_assessment", None),
    ({"heating_unavailable": True, "cold_conditions_reported": False, "unsafe_to_wait": False}, "routine", None),
    ({"cooling_unavailable": True, "vulnerable_resident": True}, "priority", "no_cooling_vulnerable"),
    ({"cooling_unavailable": True}, "needs_assessment", None),
    ({"cooling_unavailable": True, "vulnerable_resident": "unknown"}, "needs_assessment", None),
    ({"cooling_unavailable": True, "vulnerable_resident": False, "unsafe_to_wait": False}, "routine", None),
    ({"service_type": "maintenance", "vulnerable_resident": True}, "routine", None),
])
def test_triage_uses_reported_facts_and_unknown_is_not_false(database, facts, level, reason):
    result = database("risk", 0, facts)
    assert result["status"] == "saved"
    assert result["state"]["triage"]["level"] == level
    if reason:
        assert reason in result["state"]["triage"]["reasons"]
        assert result["priority_request"]["status"] == "recorded"
        assert result["priority_request"]["notification_status"] == "not_connected"
        # No name, address, membership or eligibility was needed to save the case.
        assert "service_address" not in result["state"]["details"]
    else:
        assert result["priority_request"] is None


def test_priority_is_idempotent_and_escalates_without_new_case(database):
    facts = {"cooling_unavailable": True, "vulnerable_resident": True}
    first = database("risk", 0, facts)
    replay = database("risk", 0, facts)
    assert replay["priority_request"]["id"] == first["priority_request"]["id"]
    emergency = database("gas", 1, {"gas_smell_reported": True})
    assert emergency["priority_request"]["id"] == first["priority_request"]["id"]
    assert emergency["priority_request"]["highest_level"] == "emergency"


def test_risk_correction_retains_case_for_human_review(database):
    first = database("risk", 0, {"cooling_unavailable": True, "vulnerable_resident": True})
    corrected = database("correction", 1, {"vulnerable_resident": False, "unsafe_to_wait": False})
    assert corrected["state"]["triage"]["level"] == "routine"
    case = corrected["priority_request"]
    assert case["id"] == first["priority_request"]["id"]
    assert case["highest_level"] == "priority"
    assert case["current_level"] == "routine"
    assert case["needs_review"] is True
    assert case["status"] == "recorded"


def test_reverting_a_risk_to_unknown_does_not_silently_remove_priority(database):
    database("risk", 0, {"heating_unavailable": True, "cold_conditions_reported": True})
    result = database("uncertain", 1, {"cold_conditions_reported": "unknown"})
    assert result["state"]["triage"]["level"] == "needs_assessment"
    assert result["priority_request"]["needs_review"] is True
    assert result["priority_request"]["highest_level"] == "priority"
