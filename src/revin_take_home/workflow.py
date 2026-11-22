"""Call-scoped business tools and a supervised, leased database worker."""
import asyncio
from contextlib import suppress
import logging
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)


class EmptyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class RevisionRequest(EmptyRequest):
    expected_revision: int = Field(ge=0)


Identifier = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]


class ProposeBooking(RevisionRequest):
    search_id: Identifier
    slot_id: Identifier


class ConfirmBooking(RevisionRequest):
    proposal_id: Identifier
    decision: Literal['accepted', 'declined']
    caller_quote: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class FollowupRequest(RevisionRequest):
    reason: Literal['no_availability','booking_problem','priority','membership_verification','incomplete_intake','booking_change','caller_requested']
    caller_quote: str = Field(min_length=1,max_length=1000,pattern=r"\S")


WORKFLOW_SCHEMAS = {
    'start_membership_check': RevisionRequest, 'refresh_context': EmptyRequest,
    'search_slots': RevisionRequest, 'propose_booking': ProposeBooking,
    'confirm_booking': ConfirmBooking, 'get_booking_status': EmptyRequest,
    'request_followup': FollowupRequest,
}
RPC_NAMES = {'refresh_context': 'refresh_call_context'}


class WorkflowUnavailable(Exception):
    pass


class WorkflowStore:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.wake = asyncio.Event()

    async def rpc(self, name, payload):
        try:
            r = await self.client.post('rpc/'+name, json=payload, timeout=3)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WorkflowUnavailable from exc

    async def execute(self, name, call_id, operation_id, request):
        payload = {'p_call_id': call_id, **{'p_'+k: v for k,v in request.model_dump(mode='json').items()}}
        result = await self.rpc(RPC_NAMES.get(name, name), payload)
        if not isinstance(result, dict) or 'status' not in result:
            raise WorkflowUnavailable
        if name == 'start_membership_check':
            self.wake.set()
        return result

    async def work_once(self):
        job = await self.rpc('claim_enrichment_job', {})
        if not job:
            return False
        await self.rpc('finish_enrichment_job', {'p_job_id': job['id'], 'p_lease_token': job['lease_token']})
        return True

    async def worker(self):
        """Supervised by app lifespan. DB deadlines/leases survive restarts.

        No caller data in logs. A failed completion leaves a lease for recovery;
        two attempts / 15s deadline prevent an endless retry queue.
        """
        while True:
            try:
                if await self.work_once():
                    continue
            except WorkflowUnavailable:
                log.warning('Enrichment worker unavailable; durable lease/deadline retained')
            with suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            self.wake.clear()
