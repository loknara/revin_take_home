"""The first backend boundary: caller number -> candidate context.

Storage-independent lookup contract; no identity confirmation or membership yet.
The caller number comes from authenticated provider metadata, not the LLM.
"""

import asyncio
from enum import StrEnum
from typing import Annotated, Protocol, Self

import phonenumbers
from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Candidate(Record):
    customer_id: Identifier
    first_name: Identifier


class LookupStatus(StrEnum):
    SINGLE = "matched_single"
    MULTIPLE = "matched_multiple"
    NO_MATCH = "no_match"
    NUMBER_UNAVAILABLE = "number_unavailable"
    LOOKUP_UNAVAILABLE = "lookup_unavailable"


class LookupResult(Record):
    status: LookupStatus
    candidates: tuple[Candidate, ...] = ()

    @model_validator(mode="after")
    def check_candidates(self) -> Self:
        count = len(self.candidates)
        if len({c.customer_id for c in self.candidates}) != count:
            raise ValueError("Candidates must have distinct customer IDs")
        if self.status == LookupStatus.SINGLE and count != 1:
            raise ValueError("Single match requires exactly one candidate")
        if self.status == LookupStatus.MULTIPLE and count < 2:
            raise ValueError("Multiple matches require at least two candidates")
        if self.status not in (LookupStatus.SINGLE, LookupStatus.MULTIPLE) and count:
            raise ValueError("Unresolved results cannot contain candidates")
        return self


class CallContext(Record):
    """Backend state. Originating and callback numbers have distinct meanings."""

    call_id: Identifier
    originating_number: str | None
    callback_number: str | None = None
    lookup: LookupResult

    def for_agent(self) -> dict:
        """Minimal model context; candidate data is not identity verification."""
        candidate = (
            self.lookup.candidates[0].model_dump()
            if self.lookup.status == LookupStatus.SINGLE
            else None
        )
        return {
            "call_id": self.call_id,
            "lookup_status": self.lookup.status.value,
            "candidate": candidate,
            "identity_status": "unconfirmed",
            "membership_status": "unknown",
        }


class LookupUnavailable(Exception):
    """Expected directory failure, distinct from no matching customer."""


class CustomerDirectory(Protocol):
    async def find_by_phone(self, number: str) -> tuple[Candidate, ...]: ...


def normalize_number(raw: str | None) -> str | None:
    """Normalize possible US/default or international numbers; not ownership proof."""
    if not raw:
        return None
    # Provider metadata must contain a number, not prose that embeds a number.
    if any(ch not in "+0123456789(). -\t\r\n" for ch in raw):
        return None
    try:
        parsed = phonenumbers.parse(raw, "US")
    except phonenumbers.NumberParseException:
        return None
    if parsed.extension or not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


async def bootstrap_context(
    call_id: str,
    raw_number: str | None,
    directory: CustomerDirectory,
    *,
    lookup_timeout_seconds: float = 1.0,
) -> CallContext:
    """Bound enrichment time. Unexpected programming errors remain visible."""
    if lookup_timeout_seconds <= 0:
        raise ValueError("Lookup timeout must be positive")
    number = normalize_number(raw_number)
    if number is None:
        result = LookupResult(status=LookupStatus.NUMBER_UNAVAILABLE)
    else:
        try:
            rows = await asyncio.wait_for(
                directory.find_by_phone(number), timeout=lookup_timeout_seconds
            )
        except (TimeoutError, LookupUnavailable):
            result = LookupResult(status=LookupStatus.LOOKUP_UNAVAILABLE)
        else:
            # Deduplicate joined rows without mistaking one customer for many.
            candidates = tuple({row.customer_id: row for row in rows}.values())
            status = (
                LookupStatus.NO_MATCH if not candidates
                else LookupStatus.SINGLE if len(candidates) == 1
                else LookupStatus.MULTIPLE
            )
            result = LookupResult(status=status, candidates=candidates)
    return CallContext(call_id=call_id, originating_number=number, lookup=result)
