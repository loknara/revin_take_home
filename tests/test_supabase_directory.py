import asyncio

import httpx
import pytest

from revin_take_home.caller_context import bootstrap_context
from revin_take_home.supabase_directory import SupabaseDirectory, supabase_client


def lookup(handler):
    async def run():
        async with httpx.AsyncClient(
            base_url="https://test.supabase.co/rest/v1/",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await bootstrap_context(
                "call_database_test", "+12125550101", SupabaseDirectory(client)
            )

    return asyncio.run(run()).for_agent()


def test_queries_exact_normalized_phone_and_returns_minimal_candidate():
    def handler(request):
        assert request.url.path == "/rest/v1/customer_phone_numbers"
        assert request.url.params["phone_number"] == "eq.+12125550101"
        assert request.url.params["select"] == "customer:customers!inner(id,first_name)"
        assert request.url.params["limit"] == "2"
        return httpx.Response(
            200,
            json=[
                {
                    "customer": {"id": "demo_maya", "first_name": "Maya"},
                }
            ],
        )

    result = lookup(handler)
    assert result["candidate"] == {"customer_id": "demo_maya", "first_name": "Maya"}
    assert result["identity_status"] == "unconfirmed"
    assert result["membership_status"] == "unknown"


def test_shared_database_phone_never_exposes_names():
    result = lookup(
        lambda _: httpx.Response(
            200,
            json=[
                {"customer": {"id": "demo_omar", "first_name": "Omar"}},
                {"customer": {"id": "demo_leah", "first_name": "Leah"}},
            ],
        )
    )
    assert result["lookup_status"] == "matched_multiple"
    assert result["candidate"] is None


def test_empty_database_result_is_no_match():
    assert lookup(lambda _: httpx.Response(200, json=[]))["lookup_status"] == "no_match"


@pytest.mark.parametrize(
    "status,body",
    [
        (401, {"message": "invalid key"}),
        (403, {"message": "permission denied"}),
        (503, {"message": "unavailable"}),
        (200, {"unexpected": "shape"}),
        (200, [{"customer": None}]),
        (200, [{"customer": {"id": "demo_maya", "first_name": ""}}]),
    ],
)
def test_failed_or_malformed_response_does_not_claim_no_customer(status, body):
    result = lookup(lambda _: httpx.Response(status, json=body))
    assert result["lookup_status"] == "lookup_unavailable"
    assert result["candidate"] is None


def test_network_timeout_allows_generic_intake():
    def timeout(request):
        raise httpx.ReadTimeout("Simulated network timeout", request=request)

    assert lookup(timeout)["lookup_status"] == "lookup_unavailable"


@pytest.mark.parametrize("key", ["sb_secret_test", "legacy-service-role-jwt"])
def test_key_headers_support_new_and_legacy_server_keys(key):
    async def run():
        async with supabase_client("https://test.supabase.co", key) as client:
            assert client.headers["apikey"] == key
            assert ("Authorization" in client.headers) == (
                not key.startswith("sb_secret_")
            )

    asyncio.run(run())
