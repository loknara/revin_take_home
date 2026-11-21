"""Incremental intake: model-reported facts, scoped to provider call metadata."""

from typing import Annotated, Literal, Protocol, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

ShortText = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
RiskFact = bool | Literal["unknown"]


class IntakeChanges(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    caller_name: ShortText | None = None
    property_type: Literal["residential", "commercial", "unknown"] | None = None
    issue_summary: ShortText | None = None
    service_address: ShortText | None = None
    service_unit: Annotated[str, Field(min_length=1, max_length=50, pattern=r"\S")] | None = None
    service_zip: Annotated[str, Field(pattern=r"^[0-9]{5}$")] | None = None
    membership_claim: RiskFact | None = None
    address_confirmed: bool | None = None
    service_type: Literal["repair", "maintenance", "other", "unknown"] | None = None
    heating_unavailable: RiskFact | None = None
    cooling_unavailable: RiskFact | None = None
    cold_conditions_reported: RiskFact | None = None
    vulnerable_resident: RiskFact | None = None
    gas_smell_reported: RiskFact | None = None

    @model_validator(mode="after")
    def check_patch(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Provide at least one changed field")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Omit unchanged fields; null cannot clear a field")
        if self.address_confirmed and self.model_fields_set & {
            "service_address", "service_unit", "service_zip"
        }:
            raise ValueError("Save an address first, then confirm it in a later update")
        return self


class UpdateIntake(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    changes: IntakeChanges
    confirmation_address: ShortText | None = None

    @model_validator(mode="after")
    def check_confirmation(self) -> Self:
        if self.changes.address_confirmed is True and not self.confirmation_address:
            raise ValueError("Confirmation must identify the exact saved address")
        return self


class IntakeUnavailable(Exception):
    """A write may have committed; do not claim success without a result."""


class IntakeStore(Protocol):
    async def update(
        self, call_id: str, operation_id: str, originating_number: str | None,
        request: UpdateIntake,
    ) -> dict: ...


class SupabaseIntakeStore:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def update(self, call_id, operation_id, originating_number, request):
        try:
            response = await self.client.post(
                "rpc/update_call_intake",
                json={
                    "p_call_id": call_id,
                    "p_operation_id": operation_id,
                    "p_originating_number": originating_number,
                    "p_expected_revision": request.expected_revision,
                    "p_changes": request.changes.model_dump(exclude_unset=True),
                    "p_caller_quote": request.caller_quote,
                    "p_confirmation_address": request.confirmation_address,
                },
                # Call-start enrichment stays at 1s; writes have a separate budget.
                timeout=3.0,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict) or result.get("status") not in {
                "saved", "revision_conflict", "operation_conflict", "confirmation_mismatch"
            }:
                raise ValueError("Unexpected intake result")
            state = result["state"]
            details = state["details"]
            result["state"] = {key: state[key] for key in ("call_id", "revision", "details", "triage")}
            result["missing_fields"] = [
                key for key in ("caller_name", "issue_summary", "service_address", "service_unit", "service_zip")
                if not details.get(key)
            ]
            if details.get("property_type", "unknown") == "unknown":
                result["missing_fields"].append("property_type")
            if not details.get("address_confirmed"):
                result["missing_fields"].append("address_confirmation")
            result["eligibility"] = (
                "out_of_scope" if details.get("property_type") == "commercial"
                else "not_evaluated"
            )
            result["booking_status"] = "not_booked"
            return result
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise IntakeUnavailable("Intake save could not be confirmed") from exc
