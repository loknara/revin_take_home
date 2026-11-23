import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from revin_take_home.membership import (
    MembershipUnavailable,
    ResolveMembership,
    SupabaseMembershipStore,
)
from revin_take_home.intake import UpdateIntake


@pytest.mark.parametrize(
    "extra",
    [
        {"customer_id": "demo_maya"},
        {"call_id": "other"},
        {"phone_number": "+12125550101"},
        {"service_address": "other"},
        {"expected_revision": True},
        {"expected_revision": -1},
    ],
)
def test_resolution_cannot_override_saved_scope(extra):
    with pytest.raises(ValidationError):
        ResolveMembership.model_validate({"expected_revision": 2, **extra})


@pytest.mark.parametrize(
    "status,payload",
    [
        (500, {}),
        (200, []),
        (200, {"status": "resolved"}),
        (200, {"status": "resolved", "membership_status": "gold"}),
    ],
)
def test_failed_or_malformed_database_result_is_not_nonmembership(status, payload):
    async def run():
        def handler(req):
            assert json.loads(req.content) == {
                "p_call_id": "provider_call",
                "p_expected_revision": 2,
            }
            return httpx.Response(status, json=payload)

        async with httpx.AsyncClient(
            base_url="https://test.supabase.co/rest/v1/",
            transport=httpx.MockTransport(handler),
        ) as http:
            with pytest.raises(MembershipUnavailable):
                await SupabaseMembershipStore(http).resolve(
                    "provider_call", ResolveMembership(expected_revision=2)
                )

    asyncio.run(run())


@pytest.mark.parametrize(
    "patch",
    [
        {"service_zip": "1002"},
        {"service_zip": 10023},
        {"service_unit": ""},
        {"service_unit": "4B", "address_confirmed": True},
        {"service_zip": "10023", "address_confirmed": True},
        {"membership_claim": "yes"},
    ],
)
def test_location_and_claim_validation(patch):
    with pytest.raises(ValidationError):
        UpdateIntake.model_validate(
            {
                "expected_revision": 1,
                "caller_quote": "Yes",
                "changes": patch,
                "confirmation_address": "152 West 72nd Street",
            }
        )
