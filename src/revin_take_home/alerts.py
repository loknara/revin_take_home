"""Push dispatcher alerts (priority/emergency cases, callback requests) via ntfy.

ntfy publishing: https://docs.ntfy.sh/publish/
Alerts are queued by the database in the same transaction as the priority record; this
worker only delivers them. Alert text has no caller name, address or phone number, so a
public ntfy server is acceptable for the demo. Use a long random topic (or a self-hosted
server / access token) because anyone who knows the topic can read it.
"""

import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

PRIORITY = {"emergency": 5, "urgent": 5, "elevated": 4, "callback_requested": 3}
TAGS = {
    "emergency": ["rotating_light"],
    "urgent": ["red_circle"],
    "elevated": ["orange_circle"],
    "callback_requested": ["telephone_receiver"],
}


def alerts_configured() -> bool:
    return bool(os.getenv("NTFY_TOPIC"))


class DispatcherAlertSender:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def send(self, alert: dict) -> dict:
        """Return a status for finish_dispatcher_alert. 'queued' asks for a retry."""
        if alert["call_id"].startswith(("eval_", "test_")):
            return {"status": "suppressed_test"}
        if not alerts_configured():
            return {
                "status": "blocked_unconfigured",
                "error": "ntfy_topic_not_configured",
            }
        headers = {}
        if os.getenv("NTFY_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["NTFY_TOKEN"]
        try:
            response = await self.client.post(
                os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
                headers=headers,
                json={
                    "topic": os.environ["NTFY_TOPIC"],
                    "title": alert["title"],
                    "message": alert["body"],
                    "priority": PRIORITY.get(alert["kind"], 3),
                    "tags": TAGS.get(alert["kind"], []),
                },
                timeout=5,
            )
            response.raise_for_status()
            return {"status": "sent"}
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            retry = code == 429 or code >= 500
            return {
                "status": "queued" if retry else "failed",
                "error": f"ntfy_http_{code}",
            }
        except httpx.HTTPError:
            return {"status": "queued", "error": "ntfy_unreachable"}


class DispatcherAlertWorker:
    def __init__(self, database: httpx.AsyncClient, sender: DispatcherAlertSender):
        self.database = database
        self.sender = sender
        self.wake = asyncio.Event()

    async def rpc(self, name: str, payload: dict):
        response = await self.database.post("rpc/" + name, json=payload, timeout=3)
        response.raise_for_status()
        return response.json()

    async def work_once(self) -> bool:
        alert = await self.rpc("claim_dispatcher_alert", {})
        if not alert:
            return False
        result = await self.sender.send(alert)
        await self.rpc(
            "finish_dispatcher_alert",
            {
                "p_id": alert["id"],
                "p_lease_token": alert["lease_token"],
                "p_status": result["status"],
                "p_error": result.get("error"),
            },
        )
        return True

    async def run(self) -> None:
        """Deliver queued alerts; tool calls set `wake` so a new alert goes out within ~1s."""
        while True:
            try:
                if await self.work_once():
                    continue
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                log.warning(
                    "Dispatcher alert worker unavailable; queued alerts are retained"
                )
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass
            self.wake.clear()
