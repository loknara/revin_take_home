"""Vapi webhook adapter for caller lookup and persistent intake. Audio stays in Vapi.

Run: uv run --env-file .env uvicorn revin_take_home.app:create_app --factory
Contract: https://docs.vapi.ai/api-reference/webhooks/server-message
"""

import asyncio
from contextlib import suppress
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from .audit import AuditStore, config_fingerprint
from .workflow import WorkflowStore, WorkflowUnavailable, WORKFLOW_SCHEMAS
from .caller_context import CustomerDirectory, LookupUnavailable, bootstrap_context, normalize_number
from .supabase_directory import SupabaseDirectory, supabase_client
from .intake import IntakeStore, IntakeUnavailable, SupabaseIntakeStore, UpdateIntake
from .membership import MembershipStore, MembershipUnavailable, ResolveMembership, SupabaseMembershipStore
from .evaluation_api import NewEvaluationSession, EvaluationUnavailable, SupabaseEvaluationStore


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


class CallMessage(BaseModel):
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


class AssistantRequest(CallMessage):
    type: Literal["assistant-request"]


class Envelope(BaseModel):
    message: AssistantRequest


class ToolFunction(BaseModel):
    name: str
    arguments: dict | str


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    function: ToolFunction


class ToolMessage(CallMessage):
    type: Literal["tool-calls"]
    toolCallList: list[ToolCall] = Field(min_length=1, max_length=5)


class ToolEnvelope(BaseModel):
    message: ToolMessage


class EndCallMessage(CallMessage):
    type: Literal['end-of-call-report']
    endedReason: str = Field(default='unknown',max_length=200)


class EndCallEnvelope(BaseModel):
    message: EndCallMessage


class EvaluationToolMessage(BaseModel):
    type: Literal["tool-calls"]
    toolCallList: list[ToolCall] = Field(min_length=1, max_length=5)


class EvaluationToolEnvelope(BaseModel):
    message: EvaluationToolMessage


class UnconfiguredDirectory:
    async def find_by_phone(self, number: str):
        raise LookupUnavailable("Customer directory not configured")


def create_app(
    settings: Settings | None = None,
    directory: CustomerDirectory | None = None,
    intake_store: IntakeStore | None = None,
    membership_store: MembershipStore | None = None,
    evaluation_store: SupabaseEvaluationStore | None = None,
    workflow_store: WorkflowStore | None = None,
    audit_store: AuditStore | None = None,
) -> FastAPI:
    settings = settings if settings is not None else Settings.from_env()
    db_client = None
    if settings.supabase_url and settings.supabase_secret_key:
        db_client = supabase_client(
            settings.supabase_url, settings.supabase_secret_key.get_secret_value()
        )
        if audit_store is None:
            audit_store = AuditStore(db_client)
        if workflow_store is None:
            workflow_store = WorkflowStore(db_client)
        if directory is None:
            directory = SupabaseDirectory(db_client)
        if intake_store is None:
            intake_store = SupabaseIntakeStore(db_client)
        if membership_store is None:
            membership_store = SupabaseMembershipStore(db_client)
        if evaluation_store is None:
            evaluation_store = SupabaseEvaluationStore(db_client)
    if directory is None:
        directory = UnconfiguredDirectory()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = asyncio.create_task(workflow_store.worker()) if workflow_store else None
        try:
            yield
        finally:
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            if db_client is not None:
                await db_client.aclose()

    app = FastAPI(title="Summit Air demo — caller lookup and intake", lifespan=lifespan)
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

    @app.get("/version")
    async def version():
        return config_fingerprint()

    @app.post("/vapi/events", dependencies=[Depends(authenticate)])
    async def call_ended(body: EndCallEnvelope):
        if audit_store is None or not await audit_store.call(body.message.call.id, body.message.endedReason):
            raise HTTPException(status_code=503, detail="Call outcome save unconfirmed")
        return {"status": "recorded"}

    @app.post("/vapi/tools", dependencies=[Depends(authenticate)])
    async def tool_calls(body: ToolEnvelope) -> dict:
        message = body.message
        results = []
        for tool in message.toolCallList:
            started = time.monotonic()
            if tool.function.name not in {"update_intake", "resolve_membership", *WORKFLOW_SCHEMAS}:
                result = {"status": "unsupported_tool"}
            else:
                try:
                    schema = {"update_intake": UpdateIntake, "resolve_membership": ResolveMembership, **WORKFLOW_SCHEMAS}[tool.function.name]
                    request = (
                        schema.model_validate_json(tool.function.arguments)
                        if isinstance(tool.function.arguments, str)
                        else schema.model_validate(tool.function.arguments)
                    )
                    if tool.function.name in WORKFLOW_SCHEMAS:
                        if workflow_store is None:
                            raise WorkflowUnavailable
                        result = await workflow_store.execute(tool.function.name, message.call.id, tool.id, request)
                    elif isinstance(request, ResolveMembership):
                        if membership_store is None:
                            raise MembershipUnavailable("Membership storage not configured")
                        result = await membership_store.resolve(message.call.id, request)
                    else:
                        if intake_store is None:
                            raise IntakeUnavailable("Intake storage not configured")
                        result = await intake_store.update(
                            message.call.id, tool.id, message.originating_number(), request
                        )
                except ValidationError as exc:
                    result = {
                        "status": "invalid_arguments",
                        "errors": [
                            {"field": list(e["loc"]), "message": e["msg"]}
                            for e in exc.errors(include_input=False, include_url=False)
                        ],
                    }
                except WorkflowUnavailable:
                    result = {"status": "outcome_unconfirmed", "message": "Do not claim success. Reconcile booking status before retrying writes; unknown reads are not negative results."}
                except IntakeUnavailable:
                    result = {
                        "status": "save_unconfirmed",
                        "message": "Do not claim the intake was saved or a technician was requested.",
                    }
                except MembershipUnavailable:
                    result = {
                        "status": "lookup_unavailable", "membership_status": "unknown",
                        "message": "Membership could not be verified. Do not say the caller is not a member or promise benefits.",
                    }
            if workflow_store is not None and result.get("membership_lookup"):
                workflow_store.wake.set()
            if audit_store is not None:
                await audit_store.tool(message.call.id, tool.id, tool.function.name,
                    result.get('status','unknown'), round((time.monotonic()-started)*1000))
            results.append({"toolCallId": tool.id, "result": json.dumps(result)})
        return {"results": results}

    @app.post("/vapi/assistant-request", dependencies=[Depends(authenticate)])
    async def assistant_request(body: Envelope) -> dict:
        message = body.message
        context = await bootstrap_context(
            message.call.id, message.originating_number(),
            directory,
        )
        if audit_store is not None:
            await audit_store.call(message.call.id)
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

    async def evaluation_session(session_id: UUID, *, require_open=False):
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="Evaluation storage unavailable")
        try:
            session = await evaluation_store.get(session_id)
        except EvaluationUnavailable:
            raise HTTPException(status_code=503, detail="Evaluation storage unavailable")
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown evaluation session")
        if require_open and not session.is_open():
            raise HTTPException(status_code=410, detail="Evaluation session closed or expired")
        if session.call_id != "eval_" + str(session.id):
            raise HTTPException(status_code=503, detail="Invalid evaluation scope")
        return session

    @app.post("/evaluation/sessions", dependencies=[Depends(authenticate)])
    async def create_evaluation(body: NewEvaluationSession):
        if evaluation_store is None:
            raise HTTPException(status_code=503, detail="Evaluation storage unavailable")
        try:
            session = await evaluation_store.create(body.fixture_key, body.lookup_delay_ms)
        except EvaluationUnavailable:
            raise HTTPException(status_code=503, detail="Evaluation storage unavailable")
        context = await bootstrap_context(session.call_id, session.originating_number, directory)
        return {"session_id": str(session.id), "call_id": session.call_id,
                "expires_at": session.expires_at, "caller_context": context.for_agent()}

    @app.post("/evaluation/sessions/{session_id}/tools", dependencies=[Depends(authenticate)])
    async def evaluation_tools(session_id: UUID, body: EvaluationToolEnvelope):
        session = await evaluation_session(session_id, require_open=True)
        # Simulator chat/voice metadata is NOT inbound caller identity. Bind the
        # test to a server-created fixture; never relax the production endpoint.
        return await tool_calls(ToolEnvelope(message=ToolMessage(
            type="tool-calls", call=Call(id=session.call_id, type="inboundPhoneCall"),
            customer=Customer(number=session.originating_number),
            toolCallList=body.message.toolCallList,
        )))

    @app.get("/evaluation/sessions/{session_id}", dependencies=[Depends(authenticate)])
    async def get_evaluation(session_id: UUID):
        session = await evaluation_session(session_id)
        try:
            return {"session_id": str(session.id), "call_id": session.call_id,
                    "evidence": await evaluation_store.evidence(session)}
        except EvaluationUnavailable:
            raise HTTPException(status_code=503, detail="Evaluation evidence unavailable")

    @app.post("/evaluation/sessions/{session_id}/close", dependencies=[Depends(authenticate)])
    async def close_evaluation(session_id: UUID):
        await evaluation_session(session_id)
        try:
            await evaluation_store.close(session_id)
        except EvaluationUnavailable:
            raise HTTPException(status_code=503, detail="Evaluation storage unavailable")
        return {"status": "closed"}

    return app
