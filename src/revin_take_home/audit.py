"""Versioned operational evidence; no transcript/credential dumping."""

import hashlib
import json
import logging
import os
from pathlib import Path

import httpx
from .notifications import delivery_mode

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "prompts/summit-air-system.txt").exists():
    ROOT = Path(__file__).resolve().parent  # Resources bundled in a non-editable wheel.


def config_fingerprint():
    """Hash prompt, tools, and voice baseline into a config fingerprint."""
    files = [
        ROOT / "config/voice-baseline.json",
        ROOT / "prompts/summit-air-system.txt",
        *sorted((ROOT / "tools").glob("*.json")),
    ]
    digest = hashlib.sha256()
    for path in files:
        if path.exists():
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return {
        "prompt_tools_sha256": digest.hexdigest(),
        "backend_commit": os.getenv("RENDER_GIT_COMMIT", "local"),
        "configured_pipeline": json.loads(
            (ROOT / "config/voice-baseline.json").read_text()
        ),
        "confirmation_delivery_mode": delivery_mode(),
        "policy_version": "triage-v2",
        "weather_thresholds_f": {
            "hot": os.getenv("WEATHER_HOT_F", "90"),
            "cold": os.getenv("WEATHER_COLD_F", "40"),
        },
        "runtime": "vapi-cascade",
        "timezone": "America/New_York",
    }


class AuditStore:
    def __init__(self, client):
        self.client = client

    async def record(self, name, payload):
        """Post an audit RPC and report whether it persisted."""
        try:
            r = await self.client.post("rpc/" + name, json=payload, timeout=0.8)
            r.raise_for_status()
            return True
        except httpx.HTTPError:
            log.warning("Audit persistence unavailable: %s", name)
            return False

    async def call(self, call_id, ended_reason=None):
        """Record call start or end against the current config fingerprint."""
        return await self.record(
            "record_call_event",
            {
                "p_call_id": call_id,
                "p_config": config_fingerprint(),
                "p_ended_reason": ended_reason,
            },
        )

    async def tool(self, call_id, operation_id, name, status, elapsed_ms):
        """Record one tool invocation's status and elapsed time."""
        return await self.record(
            "record_tool_event",
            {
                "p_call_id": call_id,
                "p_operation_id": operation_id,
                "p_tool_name": name,
                "p_result_status": status,
                "p_elapsed_ms": elapsed_ms,
                "p_config_version": config_fingerprint()["prompt_tools_sha256"],
            },
        )
