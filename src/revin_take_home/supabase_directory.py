"""Read caller candidates through Supabase's Postgres-backed Data API."""

import httpx
from pydantic import BaseModel

from .caller_context import Candidate, LookupUnavailable


class CustomerRow(BaseModel):
    id: str
    first_name: str


class PhoneRow(BaseModel):
    customer: CustomerRow


class SupabaseDirectory:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def find_by_phone(self, number: str) -> tuple[Candidate, ...]:
        try:
            response = await self.client.get(
                "customer_phone_numbers",
                params={
                    "phone_number": f"eq.{number}",
                    "select": "customer:customers!inner(id,first_name)",
                    # Two distinct matches suffice to require generic intake.
                    "limit": "2",
                    "order": "customer_id.asc",
                },
            )
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list):
                raise ValueError("Expected a list of customer matches")
            return tuple(
                Candidate(customer_id=row.customer.id, first_name=row.customer.first_name)
                for row in (PhoneRow.model_validate(value) for value in rows)
            )
        except (httpx.HTTPError, ValueError) as exc:
            # No raw HTTP body, key, or phone number is exposed to the model.
            raise LookupUnavailable("Customer directory unavailable") from exc


def supabase_client(url: str, key: str) -> httpx.AsyncClient:
    headers = {"apikey": key}
    # Legacy service_role keys are JWTs; new sb_secret keys use apikey only.
    if not key.startswith("sb_secret_"):
        headers["Authorization"] = f"Bearer {key}"
    return httpx.AsyncClient(
        base_url=url.rstrip("/") + "/rest/v1/",
        headers=headers,
        timeout=0.8,
        follow_redirects=False,
    )
