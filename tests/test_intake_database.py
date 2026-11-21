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

    url = Path("supabase/.temp/pooler-url").read_text().strip()
    call_id = "test_intake_" + uuid4().hex

    def connect():
        return psycopg.connect(url, password=os.environ["SUPABASE_DB_PASSWORD"],
                               sslmode="require", connect_timeout=15, autocommit=True)

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
