"""Vapi webhook adapter for caller lookup and persistent intake. Audio stays in Vapi.

Run: uv run --env-file .env uvicorn revin_take_home.app:create_app --factory
Contract: https://docs.vapi.ai/api-reference/webhooks/server-message
"""

import asyncio
import json
import httpx
import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr, ValidationError, model_validator

from .alerts import DispatcherAlertSender, DispatcherAlertWorker
from .office import office_status
from .safety_backstop import record_missed_emergency
from .weather import WeatherWorker, stop_worker
from .notifications import NotificationSender, NotificationWorker
from .audit import AuditStore, config_fingerprint
from .workflow import WorkflowStore, WorkflowUnavailable, WORKFLOW_SCHEMAS
from .guidance import RetryTracker, normalize_update_intake, shape
from .caller_context import (
    CustomerDirectory,
    LookupUnavailable,
    bootstrap_context,
    normalize_number,
)
from .supabase_directory import SupabaseDirectory, supabase_client
from .intake import IntakeStore, IntakeUnavailable, SupabaseIntakeStore, UpdateIntake
from .readback import unheard_confirmations
from .evaluation_api import (
    NewEvaluationSession,
    EvaluationUnavailable,
    SupabaseEvaluationStore,
)


# Vapi waits about 10 s for a tool webhook. Stay well inside it (finding O11).
TOOL_TIMEOUT_SECONDS = 4.5
BATCH_BUDGET_SECONDS = 8.0


class Settings(BaseModel):
    assistant_id: UUID
    webhook_secret: SecretStr = Field(min_length=32)
    supabase_url: str | None = None
    supabase_secret_key: SecretStr | None = None
    # Simulation endpoints share the webhook secret; keep them off unless running evals.
    enable_evaluation_api: bool = False

    @model_validator(mode="after")
    def check_supabase_config(self):
        """Require both Supabase settings together, and HTTPS unless it is a local stack."""
        if bool(self.supabase_url) != bool(self.supabase_secret_key):
            raise ValueError("Set both SUPABASE_URL and SUPABASE_SECRET_KEY")
        if self.supabase_url and not self.supabase_url.startswith(
            ("https://", "http://127.0.0.1:", "http://localhost:")
        ):
            raise ValueError("SUPABASE_URL must use HTTPS (plain HTTP only for a local stack)")
        return self

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the process environment."""
        return cls(
            assistant_id=os.environ["VAPI_ASSISTANT_ID"],
            webhook_secret=os.environ["VAPI_WEBHOOK_SECRET"],
            supabase_url=os.environ.get("SUPABASE_URL") or None,
            supabase_secret_key=os.environ.get("SUPABASE_SECRET_KEY") or None,
            enable_evaluation_api=os.environ.get("ENABLE_EVALUATION_API") == "1",
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
            customer.number
            for customer in (self.customer, self.call.customer)
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
    # The conversation so far; used only to check a readback happened (finding R9).
    artifact: dict | None = None


class ToolEnvelope(BaseModel):
    message: ToolMessage


class EndCallMessage(CallMessage):
    type: Literal["end-of-call-report"]
    endedReason: str = Field(default="unknown", max_length=200)
    artifact: dict | None = None


class EndCallEnvelope(BaseModel):
    message: EndCallMessage


class EvaluationToolMessage(BaseModel):
    type: Literal["tool-calls"]
    toolCallList: list[ToolCall] = Field(min_length=1, max_length=5)
    artifact: dict | None = None


class EvaluationToolEnvelope(BaseModel):
    message: EvaluationToolMessage


class UnconfiguredDirectory:
    async def find_by_phone(self, number: str):
        """Fail closed when the customer directory is not configured."""
        raise LookupUnavailable("Customer directory not configured")


def create_app(
    settings: Settings | None = None,
    directory: CustomerDirectory | None = None,
    intake_store: IntakeStore | None = None,
    evaluation_store: SupabaseEvaluationStore | None = None,
    workflow_store: WorkflowStore | None = None,
    audit_store: AuditStore | None = None,
) -> FastAPI:
    """Wire stores and routes into a FastAPI app."""
    settings = settings if settings is not None else Settings.from_env()
    db_client = None
    alert_worker = None
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
        if evaluation_store is None:
            evaluation_store = SupabaseEvaluationStore(db_client)
    if directory is None:
        directory = UnconfiguredDirectory()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Background workers: weather refresh, dispatcher alerts, confirmation outbox.

        None of them run on the voice request path.
        """
        nonlocal alert_worker
        weather_client = None
        weather_task = None
        notification_client = None
        notification_task = None
        alert_client = None
        alert_task = None
        if db_client is not None:
            weather_client = httpx.AsyncClient(
                timeout=8,
                headers={
                    "User-Agent": "SummitAirServiceCoordinator (https://github.com/loknara/revin_take_home)",
                    "Accept": "application/geo+json",
                },
            )
            weather_task = asyncio.create_task(
                WeatherWorker(db_client, weather_client).run()
            )
            notification_client = httpx.AsyncClient(timeout=8)
            notification_task = asyncio.create_task(
                NotificationWorker(
                    db_client, NotificationSender(notification_client)
                ).run()
            )
            alert_client = httpx.AsyncClient(timeout=5)
            alert_worker = DispatcherAlertWorker(
                db_client, DispatcherAlertSender(alert_client)
            )
            alert_task = asyncio.create_task(alert_worker.run())
        try:
            yield
        finally:
            if alert_task is not None:
                await stop_worker(alert_task)
            if alert_client is not None:
                await alert_client.aclose()
            if notification_task is not None:
                await stop_worker(notification_task)
            if notification_client is not None:
                await notification_client.aclose()
            if weather_task is not None:
                await stop_worker(weather_task)
            if weather_client is not None:
                await weather_client.aclose()
            if db_client is not None:
                await db_client.aclose()

    app = FastAPI(title="Summit Air — caller lookup and intake", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        """Reject requests whose bearer token does not match the webhook secret."""
        expected = settings.webhook_secret.get_secret_value().encode()
        if credentials is None or not secrets.compare_digest(
            credentials.credentials.encode(), expected
        ):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/health")
    async def health() -> dict:
        """Liveness only; says nothing about database or provider availability."""
        return {"status": "ok"}

    @app.get("/version")
    async def version():
        """Deployed prompt, tool, and pipeline fingerprint."""
        return config_fingerprint()

    @app.post("/vapi/events", dependencies=[Depends(authenticate)])
    async def call_ended(body: EndCallEnvelope):
        """Persist the end-of-call reason or fail if the write is unconfirmed."""
        # A caller who hung up to evacuate may never have reached report_safety_emergency.
        backstop_failed = False
        try:
            if await record_missed_emergency(
                workflow_store, body.message.call.id, body.message.artifact
            ):
                if alert_worker is not None:
                    alert_worker.wake.set()
        except WorkflowUnavailable:
            backstop_failed = True
        if audit_store is None or not await audit_store.call(
            body.message.call.id, body.message.endedReason
        ):
            raise HTTPException(status_code=503, detail="Call outcome save unconfirmed")
        if backstop_failed:
            raise HTTPException(status_code=503, detail="Safety backstop unconfirmed")
        return {"status": "recorded"}

    retries = RetryTracker()

    async def refuse_unheard(message, tool, request_args, unheard):
        """Refuse confirmations the caller never heard read back; still save other facts."""
        changes = {k: v for k, v in request_args["changes"].items() if k not in unheard}
        refusal = {"status": "needs_caller_yes", "reason": "not_read_back", "unheard": unheard}
        if not changes:
            return refusal
        args = {k: v for k, v in request_args.items() if k != "changes"} | {"changes": changes}
        if "address_confirmed" in unheard:
            args.pop("confirmation_address", None)
        if "callback_confirmed" in unheard:
            args.pop("confirmation_callback", None)
        result = await intake_store.update(
            message.call.id, tool.id, message.originating_number(), UpdateIntake.model_validate(args)
        )
        if result.get("status") == "saved":
            return result | refusal | {"saved_other_changes": True}
        return result

    async def run_tool(message: ToolMessage, tool: ToolCall) -> tuple[dict, dict | None]:
        """Validate and run one tool call. Returns (result, parsed arguments)."""
        name = tool.function.name
        if name not in {"update_intake", *WORKFLOW_SCHEMAS}:
            return {"status": "unsupported_tool"}, None
        schema = {"update_intake": UpdateIntake, **WORKFLOW_SCHEMAS}[name]
        arguments, moved = tool.function.arguments, []
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except ValueError:
                parsed = None
        else:
            parsed = arguments
        if name == "update_intake" and isinstance(parsed, dict):
            parsed, moved = normalize_update_intake(parsed)
        try:
            request = (
                schema.model_validate_json(arguments)
                if isinstance(arguments, str) and not moved
                else schema.model_validate(parsed)
            )
        except ValidationError as exc:
            return {
                "status": "invalid_arguments",
                "errors": [
                    {"field": list(e["loc"]), "message": e["msg"], "type": e["type"]}
                    for e in exc.errors(include_input=False, include_url=False)
                ],
            }, parsed if isinstance(parsed, dict) else None
        request_args = request.model_dump(mode="json", exclude_unset=True)
        try:
            if name in WORKFLOW_SCHEMAS:
                if workflow_store is None:
                    raise WorkflowUnavailable
                result = await workflow_store.execute(
                    name,
                    message.call.id,
                    tool.id,
                    request,
                    originating_number=message.originating_number(),
                )
            else:
                if intake_store is None:
                    raise IntakeUnavailable("Intake storage not configured")
                unheard = unheard_confirmations(
                    message.artifact,
                    request_args.get("changes") or {},
                    request.confirmation_address,
                    request.confirmation_callback,
                )
                if unheard:
                    return await refuse_unheard(message, tool, request_args, unheard), request_args
                result = await intake_store.update(
                    message.call.id, tool.id, message.originating_number(), request
                )
                if moved:
                    result["moved_into_place"] = moved
        except WorkflowUnavailable:
            result = {
                "status": "outcome_unconfirmed",
                "message": "Do not claim success. Reconcile booking status before retrying writes; unknown reads are not negative results.",
            }
        except IntakeUnavailable:
            result = {
                "status": "save_unconfirmed",
                "message": "Do not claim the intake was saved or a technician was requested.",
            }
        return result, request_args

    @app.post("/vapi/tools", dependencies=[Depends(authenticate)])
    async def tool_calls(body: ToolEnvelope) -> dict:
        """Validate and dispatch each tool call, then audit the outcome.

        Vapi gives a tool request about 10 s. Each call gets at most TOOL_TIMEOUT and the
        whole batch at most BATCH_BUDGET, so the webhook always answers; an unfinished
        call reports an unconfirmed outcome instead of returning nothing.
        """
        message = body.message
        results = []
        deadline = time.monotonic() + BATCH_BUDGET_SECONDS
        for tool in message.toolCallList:
            started = time.monotonic()
            request_args = None
            try:
                async with asyncio.timeout(
                    max(0.1, min(TOOL_TIMEOUT_SECONDS, deadline - started))
                ):
                    result, request_args = await run_tool(message, tool)
            except TimeoutError:
                result = {
                    "status": "save_unconfirmed"
                    if tool.function.name == "update_intake"
                    else "outcome_unconfirmed",
                    "message": "Timed out. Do not claim success; check state before retrying.",
                }
            result = shape(tool.function.name, result, request_args)
            result = retries.observe(message.call.id, tool.function.name, result)
            if audit_store is not None:
                await audit_store.tool(
                    message.call.id,
                    tool.id,
                    tool.function.name,
                    result.get("status", "unknown"),
                    round((time.monotonic() - started) * 1000),
                )
            results.append({"toolCallId": tool.id, "result": json.dumps(result)})
        # A save may have queued a dispatcher alert; deliver it now, not on the next poll.
        if alert_worker is not None:
            alert_worker.wake.set()
        return {"results": results}

    @app.post("/vapi/assistant-request", dependencies=[Depends(authenticate)])
    async def assistant_request(body: Envelope) -> dict:
        """Look up the caller and return assistant overrides for this call."""
        message = body.message
        context = await bootstrap_context(
            message.call.id,
            message.originating_number(),
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
                    "caller_context": json.dumps(context.for_agent() | office_status()),
                },
            },
        }

    if not settings.enable_evaluation_api:
        return app

    async def evaluation_session(session_id: UUID, *, require_open=False):
        if evaluation_store is None:
            raise HTTPException(
                status_code=503, detail="Evaluation storage unavailable"
            )
        try:
            session = await evaluation_store.get(session_id)
        except EvaluationUnavailable:
            raise HTTPException(
                status_code=503, detail="Evaluation storage unavailable"
            )
        if session is None:
            raise HTTPException(status_code=404, detail="Unknown evaluation session")
        if require_open and not session.is_open():
            raise HTTPException(
                status_code=410, detail="Evaluation session closed or expired"
            )
        if session.call_id != "eval_" + str(session.id):
            raise HTTPException(status_code=503, detail="Invalid evaluation scope")
        return session

    @app.post("/evaluation/sessions", dependencies=[Depends(authenticate)])
    async def create_evaluation(body: NewEvaluationSession):
        if evaluation_store is None:
            raise HTTPException(
                status_code=503, detail="Evaluation storage unavailable"
            )
        try:
            session = await evaluation_store.create(
                body.fixture_key, body.lookup_delay_ms
            )
        except EvaluationUnavailable:
            raise HTTPException(
                status_code=503, detail="Evaluation storage unavailable"
            )
        context = await bootstrap_context(
            session.call_id, session.originating_number, directory
        )
        return {
            "session_id": str(session.id),
            "call_id": session.call_id,
            "expires_at": session.expires_at,
            # Simulations never attach the transfer tool, so never advertise one.
            "caller_context": context.for_agent()
            | office_status()
            | {"live_transfer_configured": False},
        }

    @app.post(
        "/evaluation/sessions/{session_id}/tools", dependencies=[Depends(authenticate)]
    )
    async def evaluation_tools(session_id: UUID, body: EvaluationToolEnvelope):
        session = await evaluation_session(session_id, require_open=True)
        # Simulator chat/voice metadata is NOT inbound caller identity. Bind the
        # test to a server-created fixture; never relax the production endpoint.
        return await tool_calls(
            ToolEnvelope(
                message=ToolMessage(
                    type="tool-calls",
                    call=Call(id=session.call_id, type="inboundPhoneCall"),
                    customer=Customer(number=session.originating_number),
                    toolCallList=body.message.toolCallList,
                    artifact=body.message.artifact,
                )
            )
        )

    @app.get("/evaluation/sessions/{session_id}", dependencies=[Depends(authenticate)])
    async def get_evaluation(session_id: UUID):
        session = await evaluation_session(session_id)
        try:
            return {
                "session_id": str(session.id),
                "call_id": session.call_id,
                "evidence": await evaluation_store.evidence(session),
            }
        except EvaluationUnavailable:
            raise HTTPException(
                status_code=503, detail="Evaluation evidence unavailable"
            )

    @app.post(
        "/evaluation/sessions/{session_id}/close", dependencies=[Depends(authenticate)]
    )
    async def close_evaluation(session_id: UUID):
        await evaluation_session(session_id)
        try:
            await evaluation_store.close(session_id)
        except EvaluationUnavailable:
            raise HTTPException(
                status_code=503, detail="Evaluation storage unavailable"
            )
        return {"status": "closed"}

    return app
