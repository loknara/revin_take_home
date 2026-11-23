"""Authenticated simulation bridge; production inbound metadata remains unchanged."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field


class NewEvaluationSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fixture_key: Literal['maya', 'shared', 'unknown', 'withheld']
    lookup_delay_ms: int = Field(default=0,ge=0,le=8000,strict=True)


class EvaluationUnavailable(Exception):
    pass


class EvaluationSession(BaseModel):
    id: UUID
    call_id: str
    fixture_key: str
    originating_number: str | None
    expires_at: datetime
    closed_at: datetime | None = None

    # True while the session is unclosed and not past its expiry.
    def is_open(self):
        return self.closed_at is None and self.expires_at > datetime.now(timezone.utc)


class SupabaseEvaluationStore:
    # Keep the Supabase client used for evaluation sessions.
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # Create a fixture session and optionally set its lookup delay.
    async def create(self, fixture_key, lookup_delay_ms=0):
        try:
            r = await self.client.post('rpc/create_evaluation_session', json={'p_fixture_key': fixture_key}, timeout=3)
            r.raise_for_status()
            session = EvaluationSession.model_validate(r.json())
            if lookup_delay_ms:
                configured = await self.client.post('rpc/configure_evaluation_delay', json={'p_session_id':str(session.id),'p_delay_ms':lookup_delay_ms},timeout=3)
                configured.raise_for_status()
            return session
        except (httpx.HTTPError, ValueError) as exc:
            raise EvaluationUnavailable from exc

    # Load one evaluation session by id.
    async def get(self, session_id):
        try:
            r = await self.client.get('evaluation_sessions', params={'id': 'eq.'+str(session_id), 'select': '*'}, timeout=3)
            r.raise_for_status()
            rows = r.json()
            return EvaluationSession.model_validate(rows[0]) if rows else None
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise EvaluationUnavailable from exc

    # Mark an evaluation session closed.
    async def close(self, session_id):
        try:
            r = await self.client.post('rpc/close_evaluation_session', json={'p_session_id': str(session_id)}, timeout=3)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise EvaluationUnavailable from exc

    async def evidence(self, session):
        """Exact test-call scope; no unfiltered database exports."""
        if session.call_id != 'eval_'+str(session.id):
            raise EvaluationUnavailable
        queries = {
            'call_intakes': 'call_id,revision,details,triage',
            'contact_prefill_events': 'intake_revision,source,location_id,fields,created_at',
            'call_weather_evidence': 'intake_revision,weather,triage,created_at',
            'intake_events': 'operation_id,applied_revision,changes,caller_quote,created_at',
            'priority_requests': 'id,highest_level,current_level,status,notification_status,latest_intake_revision,needs_review,created_at',
            'call_membership_checks': 'intake_revision,input_snapshot,result,checked_at',
            'enrichment_jobs': 'id,status,input_revision,dependency_snapshot,created_at,completed_at,result',
            'availability_reads': 'id,dependency_snapshot,result,created_at,expires_at',
            'booking_proposals': 'id,slot_id,material_revision,intake_snapshot,status,created_at,expires_at',
            'booking_agreements': 'proposal_id,intake_revision,caller_quote,decision,source,created_at',
            'appointments': 'id,proposal_id,slot_id,intake_snapshot,status,dispatch_status,created_at',
            'dispatch_jobs': 'appointment_id,technician_id,starts_at,ends_at,priority,status,assigned_at',
            'appointment_notifications': 'appointment_id,channel,status,provider_name,created_at,updated_at',
            'followup_requests': 'id,reasons,status,notification_status,created_at',
            'tool_audit': 'operation_id,tool_name,result_status,elapsed_ms,created_at,config_version',
            'call_audit': 'config,ended_reason,final_disposition',
        }
        data = {}
        try:
            for table, select in queries.items():
                params = {'call_id': 'eq.'+session.call_id, 'select': select}
                if table == 'intake_events': params['order'] = 'applied_revision.asc'
                if table == 'call_membership_checks': params['order'] = 'intake_revision.asc'
                r = await self.client.get(table, params=params, timeout=3)
                r.raise_for_status()
                rows = r.json()
                if not isinstance(rows, list): raise ValueError('Expected rows')
                data[table] = rows
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise EvaluationUnavailable from exc
