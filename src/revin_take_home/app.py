"""Vapi call-start adapter. Synthetic lookup only; no audio or persistence.

Run: uv run --env-file .env uvicorn revin_take_home.app:create_app --factory
Contract: https://docs.vapi.ai/api-reference/webhooks/server-message
"""

import json
import os
import secrets
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr

from .caller_context import CustomerDirectory, bootstrap_context, normalize_number
from .demo import DemoDirectory


class Settings(BaseModel):
    assistant_id: UUID
    webhook_secret: SecretStr = Field(min_length=32)
    demo_caller_number: str | None = Field(default=None, repr=False)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            assistant_id=os.environ["VAPI_ASSISTANT_ID"],
            webhook_secret=os.environ["VAPI_WEBHOOK_SECRET"],
            demo_caller_number=os.environ.get("DEMO_CALLER_NUMBER"),
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


def create_app(
    settings: Settings | None = None,
    directory: CustomerDirectory | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    directory = directory if directory is not None else DemoDirectory(settings.demo_caller_number)
    app = FastAPI(title="Summit Air demo — caller lookup")
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
            message.call.id, message.originating_number(), directory
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
