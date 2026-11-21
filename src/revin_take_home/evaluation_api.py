"""Authenticated simulation bridge; production inbound metadata remains unchanged."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict


class NewEvaluationSession(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fixture_key: Literal['maya', 'shared', 'unknown', 'withheld']


class EvaluationUnavailable(Exception):
    pass


class EvaluationSession(BaseModel):
    id: UUID
    call_id: str
    fixture_key: str
    originating_number: str | None
    expires_at: datetime
    closed_at: datetime | None = None

    def is_open(self):
        return self.closed_at is None and self.expires_at > datetime.now(timezone.utc)


class SupabaseEvaluationStore:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def create(self, fixture_key):
        try:
            r = await self.client.post('rpc/create_evaluation_session', json={'p_fixture_key': fixture_key}, timeout=3)
            r.raise_for_status()
            return EvaluationSession.model_validate(r.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise EvaluationUnavailable from exc

    async def get(self, session_id):
        try:
            r = await self.client.get('evaluation_sessions', params={'id': 'eq.'+str(session_id), 'select': '*'}, timeout=3)
            r.raise_for_status()
            rows = r.json()
            return EvaluationSession.model_validate(rows[0]) if rows else None
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            raise EvaluationUnavailable from exc

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
            'intake_events': 'operation_id,applied_revision,changes,caller_quote,created_at',
            'priority_requests': 'id,highest_level,current_level,status,notification_status,latest_intake_revision,needs_review,created_at',
            'call_membership_checks': 'intake_revision,input_snapshot,result,checked_at',
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
