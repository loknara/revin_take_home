"""Call-scoped business tools. The server binds call identity; the model only supplies facts."""

from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .notifications import sender_ready, delivery_mode


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RevisionRequest(EmptyRequest):
    expected_revision: int = Field(ge=0)


Identifier = Annotated[
    str,
    Field(
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    ),
]


class ProposeBooking(RevisionRequest):
    search_id: Identifier
    slot_id: Identifier


class ConfirmBooking(RevisionRequest):
    proposal_id: Identifier
    decision: Literal["accepted", "declined"]
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class SafetyEmergency(EmptyRequest):
    hazard: Literal[
        "gas_smell",
        "carbon_monoxide_alarm",
        "smoke_or_burning_smell",
        "electrical_sparking",
    ]
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class FollowupRequest(RevisionRequest):
    reason: Literal[
        "no_availability",
        "booking_problem",
        "priority",
        "membership_verification",
        "incomplete_intake",
        "booking_change",
        "caller_requested",
        "technical_issue",
    ]
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class PrepareConfirmation(EmptyRequest):
    channel: Literal["sms", "email", "none"]
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")
    email: str | None = Field(
        default=None,
        min_length=3,
        max_length=254,
        pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$",
    )

    @model_validator(mode="after")
    def email_matches_channel(self):
        if (self.channel == "email") != (self.email is not None):
            raise ValueError("Supply an email only for the email channel")
        return self


class ConfirmNotification(EmptyRequest):
    confirmation_token: Identifier
    decision: Literal["accepted", "declined"]
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")


WORKFLOW_SCHEMAS = {
    "prepare_confirmation": PrepareConfirmation,
    "confirm_notification": ConfirmNotification,
    "get_confirmation_status": EmptyRequest,
    "refresh_context": EmptyRequest,
    "search_slots": RevisionRequest,
    "propose_booking": ProposeBooking,
    "confirm_booking": ConfirmBooking,
    "get_booking_status": EmptyRequest,
    "request_followup": FollowupRequest,
    "report_safety_emergency": SafetyEmergency,
}
# Safety reports are monotonic and keyed by the provider operation for idempotent replays.
OPERATION_SCOPED = {"report_safety_emergency"}
# These may create the call record, so they carry the provider caller ID for a callback.
ORIGIN_SCOPED = {"report_safety_emergency", "request_followup"}
RPC_NAMES = {"refresh_context": "refresh_call_context"}


class WorkflowUnavailable(Exception):
    pass


class WorkflowStore:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def rpc(self, name, payload):
        """Call a workflow RPC and raise if the response is unusable."""
        try:
            r = await self.client.post("rpc/" + name, json=payload, timeout=3)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkflowUnavailable from exc

    async def execute(self, name, call_id, operation_id, request, originating_number=None):
        """Run one tool RPC and hide the originating number from the result."""
        payload = {"p_call_id": call_id}
        if name in OPERATION_SCOPED:
            payload["p_operation_id"] = operation_id
        payload |= {"p_" + k: v for k, v in request.model_dump(mode="json").items()}
        if name in ORIGIN_SCOPED:
            payload["p_originating_number"] = originating_number
        if name == "confirm_notification":
            current = await self.rpc("get_confirmation_status", {"p_call_id": call_id})
            if not isinstance(current, dict) or not isinstance(
                current.get("notifications"), list
            ):
                raise WorkflowUnavailable
            match = next(
                (
                    n
                    for n in current["notifications"]
                    if isinstance(n, dict)
                    and n.get("confirmation_token") == request.confirmation_token
                ),
                {},
            )
            payload["p_sender_ready"] = sender_ready(match.get("channel"))
            payload["p_stub"] = delivery_mode() == "stub"
        result = await self.rpc(RPC_NAMES.get(name, name), payload)
        if not isinstance(result, dict) or "status" not in result:
            raise WorkflowUnavailable
        if isinstance(result.get("state"), dict):
            result["state"].pop("originating_number", None)
        return result
