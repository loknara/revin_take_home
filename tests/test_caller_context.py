import asyncio

import pytest
from pydantic import ValidationError

from revin_take_home.caller_context import (
    Candidate, LookupResult, LookupStatus, bootstrap_context, normalize_number,
)
from revin_take_home.demo import DemoDirectory, MAYA, UnavailableDirectory


def run(number, directory=None, **kwargs):
    return asyncio.run(bootstrap_context(
        "call_test", number, directory or DemoDirectory(), **kwargs
    ))


def test_unique_match_prefills_candidate_without_verifying_identity_or_plan():
    state = run("(212) 555-0101")
    assert state.originating_number == "+12125550101"
    assert state.callback_number is None
    assert state.for_agent() == {
        "call_id": "call_test",
        "lookup_status": "matched_single",
        "candidate": {"customer_id": "demo_maya", "first_name": "Maya"},
        "identity_status": "unconfirmed",
        "membership_status": "unknown",
    }


def test_shared_number_does_not_expose_candidate_names_to_model():
    state = run("+12125550102")
    assert len(state.lookup.candidates) == 2
    assert state.for_agent()["candidate"] is None
    assert "Omar" not in str(state.for_agent())
    assert "Leah" not in str(state.for_agent())


def test_no_match_and_directory_failure_are_different():
    assert run("+12125550103").lookup.status == LookupStatus.NO_MATCH
    failed = run("+12125550101", UnavailableDirectory())
    assert failed.lookup.status == LookupStatus.LOOKUP_UNAVAILABLE
    assert failed.for_agent()["membership_status"] == "unknown"


@pytest.mark.parametrize("number", [None, "", "anonymous", "blocked", "123", "name 2125550101"])
def test_unusable_number_skips_directory(number):
    class MustNotBeCalled:
        async def find_by_phone(self, value):
            raise AssertionError("Lookup should not run")

    assert run(number, MustNotBeCalled()).lookup.status == LookupStatus.NUMBER_UNAVAILABLE


def test_timeout_returns_generic_context():
    class SlowDirectory:
        async def find_by_phone(self, number):
            await asyncio.sleep(10)
            return (MAYA,)

    result = run("+12125550101", SlowDirectory(), lookup_timeout_seconds=0.01)
    assert result.lookup.status == LookupStatus.LOOKUP_UNAVAILABLE
    assert result.for_agent()["candidate"] is None


def test_duplicate_join_rows_do_not_create_false_ambiguity():
    class DuplicateRows:
        async def find_by_phone(self, number):
            return (MAYA, MAYA)

    assert run("+12125550101", DuplicateRows()).lookup.status == LookupStatus.SINGLE


def test_invalid_state_and_unexpected_fields_are_rejected():
    with pytest.raises(ValidationError):
        LookupResult(status=LookupStatus.SINGLE)
    with pytest.raises(ValidationError):
        LookupResult(status=LookupStatus.NO_MATCH, candidates=(MAYA,))
    with pytest.raises(ValidationError):
        Candidate(customer_id="x", first_name="X", membership="active")


def test_unexpected_errors_are_not_disguised_as_customer_lookup_failures():
    class BrokenDirectory:
        async def find_by_phone(self, number):
            raise RuntimeError("Programming bug")
    with pytest.raises(RuntimeError):
        run("+12125550101", BrokenDirectory())


def test_independent_calls_do_not_share_context():
    known = run("+12125550101")
    unknown = asyncio.run(bootstrap_context("other_call", None, DemoDirectory()))
    assert known.call_id != unknown.call_id
    assert unknown.for_agent()["candidate"] is None


def test_international_number_is_normalized_without_claiming_coverage():
    assert normalize_number("+44 20 7946 0018") == "+442079460018"
