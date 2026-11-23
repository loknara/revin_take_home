"""Location-scoped demo membership, resolved from saved call facts only."""

from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field


class ResolveMembership(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class MembershipUnavailable(Exception):
    """An unavailable lookup must remain unknown, never become nonmembership."""


class MembershipStore(Protocol):
    async def resolve(self, call_id: str, request: ResolveMembership) -> dict:
        """Resolve membership for a call from saved facts."""
        ...


class SupabaseMembershipStore:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def resolve(self, call_id, request):
        """Resolve membership and reject unexpected statuses."""
        try:
            response = await self.client.post(
                "rpc/resolve_call_membership",
                json={
                    "p_call_id": call_id,
                    "p_expected_revision": request.expected_revision,
                },
                timeout=2.0,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("status") not in {
                "resolved",
                "no_match",
                "ambiguous",
                "needs_details",
                "needs_full_name",
                "needs_address_confirmation",
                "needs_intake",
                "revision_conflict",
                "out_of_scope",
                "deferred_for_emergency",
            }:
                raise ValueError("Unexpected membership result")
            if result.get("membership_status") not in {
                "active",
                "inactive",
                "expired",
                "not_started",
                "none",
                "unknown",
            }:
                raise ValueError("Unexpected membership status")
            return result
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise MembershipUnavailable("Membership lookup unavailable") from exc
