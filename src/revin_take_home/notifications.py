"""Transactional appointment confirmations. Provider acceptance is not delivery.

Twilio: https://www.twilio.com/docs/messaging/api/message-resource
Resend: https://resend.com/docs/api-reference/emails/send-email
"""
import asyncio
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)


def delivery_mode():
    # Sending requires explicit opt-in as well as credentials. Unknown values fail closed.
    return 'live' if os.getenv('CONFIRMATION_DELIVERY_MODE', 'stub') == 'live' else 'stub'


def sender_ready(channel):
    if delivery_mode() != 'live': return False
    required = {'sms': ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM_NUMBER'),
                'email': ('RESEND_API_KEY', 'CONFIRMATION_FROM_EMAIL')}
    return channel in required and all(os.getenv(k) for k in required[channel])


def message_text(content):
    unit = content.get('unit')
    address = ', '.join(str(x) for x in (content.get('street'),
        ('Unit '+unit) if unit and unit != 'none' else None,
        content.get('city'), content.get('state'), content.get('zip')) if x)
    # The public phone experience is role-play; an actual inbox must identify
    # synthetic reservations so recipients cannot mistake them for a real visit.
    prefix = 'Summit Air test appointment' if content.get('synthetic_roster', True) else 'Summit Air appointment'
    text = (f"{prefix} confirmed. {content['job_type'].capitalize()}: {content['arrival_window']} "
            f"(New York time). Address: {address}. Technician: {content.get('technician_name') or 'assignment pending'}. "
            f"Reference: {content['reference']}.")
    if content.get('synthetic_roster', True):
        text += ' This is a simulated service visit; no real technician will arrive.'
    return text


class NotificationSender:
    def __init__(self, client):
        self.client = client

    async def send(self, job):
        channel = job['channel']
        if delivery_mode() == 'stub':
            return {'status': 'blocked_unconfigured', 'error': 'stub_mode_no_external_delivery'}
        if not sender_ready(channel):
            return {'status': 'blocked_unconfigured', 'error': 'sender_not_configured'}
        # Defense in depth: evaluation fixtures must never reach a provider.
        if job['call_id'].startswith(('eval_', 'test_')):
            return {'status': 'failed', 'error': 'test_recipient_suppressed'}
        try:
            text = message_text(job['content'])
            if channel == 'sms':
                sid = os.environ['TWILIO_ACCOUNT_SID']
                r = await self.client.post(f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
                    auth=(sid, os.environ['TWILIO_AUTH_TOKEN']), data={
                        'To': job['recipient'], 'From': os.environ['TWILIO_FROM_NUMBER'], 'Body': text}, timeout=8)
                provider = 'twilio'
                r.raise_for_status()
                value = r.json(); receipt = value['sid']
                if not re.fullmatch(r'SM[a-fA-F0-9]{32}', receipt): raise ValueError('Invalid receipt')
                status = self.twilio_status(value.get('status'))
            else:
                r = await self.client.post('https://api.resend.com/emails',
                    headers={'Authorization': 'Bearer '+os.environ['RESEND_API_KEY'],
                             'Idempotency-Key': 'appointment-confirmation/'+job['id']},
                    json={'from': os.environ['CONFIRMATION_FROM_EMAIL'], 'to': [job['recipient']],
                          'subject': 'Summit Air appointment confirmation', 'text': text}, timeout=8)
                provider = 'resend'
                r.raise_for_status()
                from uuid import UUID
                receipt = str(UUID(r.json()['id']))
                status = 'submitted'
            return {'status': status, 'provider_id': receipt, 'provider_name': provider}
        except httpx.HTTPStatusError as exc:
            # A 5xx can follow an accepted send: do not retry blindly.
            return {'status': 'failed' if 400 <= exc.response.status_code < 500 else 'delivery_unknown',
                    'error': 'provider_http_'+str(exc.response.status_code)}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {'status': 'delivery_unknown', 'error': 'provider_response_unconfirmed'}

    @staticmethod
    def twilio_status(status):
        if status == 'delivered': return 'delivered'
        if status in ('failed', 'undelivered', 'canceled'): return 'failed'
        return 'submitted'

    async def poll(self, job):
        if delivery_mode() == 'stub':
            return {'status': 'delivery_unknown', 'error': 'stub_mode_no_external_delivery'}
        if not sender_ready(job['channel']):
            return {'status': 'delivery_unknown', 'error': 'sender_unavailable_for_status'}
        try:
            receipt = job['provider_id']
            if job['provider_name'] == 'twilio':
                if not re.fullmatch(r'SM[a-fA-F0-9]{32}', receipt): raise ValueError('Invalid receipt')
                sid = os.environ['TWILIO_ACCOUNT_SID']
                r = await self.client.get(f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages/{receipt}.json',
                    auth=(sid, os.environ['TWILIO_AUTH_TOKEN']), timeout=8)
                r.raise_for_status()
                value = r.json()
                if value['sid'] != receipt: raise ValueError('Receipt mismatch')
                status = self.twilio_status(value.get('status'))
            else:
                from uuid import UUID
                receipt = str(UUID(receipt))
                r = await self.client.get('https://api.resend.com/emails/'+receipt,
                    headers={'Authorization': 'Bearer '+os.environ['RESEND_API_KEY']}, timeout=8)
                r.raise_for_status()
                value = r.json()
                if value['id'] != receipt: raise ValueError('Receipt mismatch')
                event = value.get('last_event')
                status = ('delivered' if event in ('delivered', 'opened', 'clicked') else
                          'failed' if event in ('bounced', 'complained', 'failed', 'suppressed', 'canceled') else 'submitted')
            return {'status': status}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return {'status': 'submitted', 'error': 'delivery_status_unavailable'}


class NotificationWorker:
    def __init__(self, database, sender):
        self.database = database
        self.sender = sender
        self.wake = asyncio.Event()

    async def rpc(self, name, payload):
        r = await self.database.post('rpc/'+name, json=payload, timeout=3)
        r.raise_for_status()
        return r.json()

    async def work_once(self):
        job = await self.rpc('claim_appointment_notification', {})
        if not job: return False
        result = await (self.sender.send(job) if job['action'] == 'send' else self.sender.poll(job))
        await self.rpc('finish_appointment_notification', {
            'p_id': job['id'], 'p_lease_token': job['lease_token'], 'p_status': result['status'],
            'p_provider_id': result.get('provider_id'), 'p_provider_name': result.get('provider_name'),
            'p_error': result.get('error'),
        })
        return True

    async def run(self):
        while True:
            try:
                if await self.work_once(): continue
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                log.warning('Confirmation worker unavailable; durable queue/lease retained')
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=15)
            except TimeoutError:
                pass
            self.wake.clear()
