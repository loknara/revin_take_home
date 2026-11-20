"""Vapi call-start adapter. Customer lookup only; no audio or saved call intake.

Run: uv run --env-file .env uvicorn revin_take_home.app:create_app --factory
Contract: https://docs.vapi.ai/api-reference/webhooks/server-message
"""

import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr, model_validator

from .caller_context import CustomerDirectory, LookupUnavailable, bootstrap_context, normalize_number
from .supabase_directory import SupabaseDirectory, supabase_client


class Settings(BaseModel):
    assistant_id: UUID
    webhook_secret: SecretStr = Field(min_length=32)
    supabase_url: str | None = None
    supabase_secret_key: SecretStr | None = None

    @model_validator(mode="after")
    def check_supabase_config(self):
        if bool(self.supabase_url) != bool(self.supabase_secret_key):
            raise ValueError("Set both SUPABASE_URL and SUPABASE_SECRET_KEY")
        if self.supabase_url and not self.supabase_url.startswith("https://"):
            raise ValueError("SUPABASE_URL must use HTTPS")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            assistant_id=os.environ["VAPI_ASSISTANT_ID"],
            webhook_secret=os.environ["VAPI_WEBHOOK_SECRET"],
            supabase_url=os.environ.get("SUPABASE_URL") or None,
            supabase_secret_key=os.environ.get("SUPABASE_SECRET_KEY") or None,
        )


# Only model the fields we consume; ignore Vapi's other event metadata.
class Customer(BaseModel):
    number: str | None = None


class Call(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["inboundPhoneCall"]
    customer: Customer | None = None


class AssistantRequest(BaseModel):
    type: Literal["assistant-request"]
    call: Call
    customer: Customer | None = None

    def originating_number(self) -> str | None:
        """Use authenticated customer metadata, never phoneNumber or transcript.

        Vapi exposes customer on the event and call. If both supply numbers,
        require agreement; conflicting metadata gets generic intake.
        """
        numbers = [
            customer.number for customer in (self.customer, self.call.customer)
            if customer is not None and customer.number is not None
        ]
        normalized = {normalize_number(number) for number in numbers}
        return normalized.pop() if len(normalized) == 1 else None


class Envelope(BaseModel):
    message: AssistantRequest


class UnconfiguredDirectory:
    async def find_by_phone(self, number: str):
        raise LookupUnavailable("Customer directory not configured")


def create_app(
    settings: Settings | None = None,
    directory: CustomerDirectory | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    db_client = None
    if directory is None and settings.supabase_url and settings.supabase_secret_key:
        db_client = supabase_client(
            settings.supabase_url, settings.supabase_secret_key.get_secret_value()
        )
        directory = SupabaseDirectory(db_client)
    if directory is None:
        directory = UnconfiguredDirectory()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            if db_client is not None:
                await db_client.aclose()

    app = FastAPI(title="Summit Air demo — caller lookup", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None, Depends(bearer)
        ],
    ) -> None:
        expected = settings.webhook_secret.get_secret_value().encode()
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), expected
        ):
            raise HTTPException(
                status_code=401, detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/vapi/assistant-request", dependencies=[Depends(authenticate)])
    async def assistant_request(body: Envelope) -> dict:
        message = body.message
        context = await bootstrap_context(
            message.call.id, message.originating_number(),
            directory,
        )
        # Vapi substitutes this string into {{caller_context}} in the prompt.
        # Supplying variables alone does not put them in the model's context.
        return {
            "assistantId": str(settings.assistant_id),
            "assistantOverrides": {
                "variableValues": {
                    "caller_context": json.dumps(context.for_agent()),
                },
            },
        }

    return app
