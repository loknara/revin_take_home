"""Review/apply the versioned assistant config to Vapi, or restore a saved deployment.

    python -m revin_take_home.configure_vapi                      # plan only, no network
    python -m revin_take_home.configure_vapi --apply --phone-id ID --backend-url https://<host>
    python -m revin_take_home.configure_vapi --apply --restore eval-results/deployment-.../backup.json

--apply waits until the backend at --backend-url serves the same prompt/tool fingerprint,
creates new versioned tools (never mutates tools a live call may be using), applies the
versioned voice settings in config/voice-baseline.json (model, transcriber/endpointing,
voice chunking, interruption and denoising plans), points the assistant and the phone
number's assistant-request webhook at that backend, and saves a private rollback snapshot
first. It never starts a call. The plan-only run prints the voice settings and every tool's
spoken messages so they can be reviewed before applying.
"""

import argparse
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import phonenumbers

from .audit import config_fingerprint
from .evaluation import ALLOWED_TOOLS, ROOT

DEFAULT_BACKEND = "https://revin-take-home-wt93.onrender.com"
# Tool names from earlier deployments, used only to discover the existing server credential.
LEGACY_TOOLS = {
    "resolve_membership",
    "start_membership_check",
    "report_gas_emergency",
    "prepare_confirmation",
    "confirm_notification",
    "get_confirmation_status",
}
FIRST_MESSAGE = "Thanks for calling Summit Air. I'm the virtual assistant. How can I help you today?"
FAILURE_MESSAGE = (
    "Summit Air's virtual assistant is temporarily unavailable. No service request has been saved. "
    "Please try again later. If you smell gas or your carbon monoxide alarm is sounding, "
    "leave the home with everyone and call 911 once you're safely outside."
)


def voice_settings() -> dict:
    """Versioned voice-layer settings: {"model": {...}, "transcriber": {...}, ...}.

    "model" fields are merged into the live model (prompt and tools are set separately);
    every other section replaces the assistant's section of the same name.
    """
    return json.loads((ROOT / "config/voice-baseline.json").read_text())


def contains(actual, expected) -> bool:
    """True when every field in expected appears in actual (Vapi adds defaults on readback)."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            contains(actual.get(k), v) for k, v in expected.items()
        )
    return actual == expected


def tool_messages() -> dict:
    """Spoken tool messages, as deployed (the transfer number stays a placeholder)."""
    names = sorted(ALLOWED_TOOLS) + ["transfer_call"]
    return {
        name: json.loads((ROOT / f"tools/{name}.json").read_text()).get("messages", [])
        for name in names
    }


def private_json(path: Path, value) -> None:
    """Write JSON with owner-only directory and file permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(value, indent=2))
    path.chmod(0o600)


def assistant_patch(live, tool_ids, backend, credential, settings=None):
    """Swap in the versioned prompt, tools and voice settings; keep other live model settings."""
    settings = voice_settings() if settings is None else settings
    model = copy.deepcopy(live["model"])
    model.update(copy.deepcopy(settings.get("model", {})))
    model["messages"] = [
        {
            "role": "system",
            "content": (ROOT / "prompts/summit-air-system.txt").read_text(),
        }
    ]
    model.pop("tools", None)
    model.pop("toolRefs", None)
    model["toolIds"] = tool_ids
    patch = {
        "model": model,
        "server": {
            "url": backend + "/vapi/events",
            "credentialId": credential,
            "timeoutSeconds": 5,
        },
        "serverMessages": ["end-of-call-report"],
        "firstMessage": FIRST_MESSAGE,
    }
    for section, value in settings.items():
        if section != "model":
            patch[section] = copy.deepcopy(value)
    return patch


def matching_tool(existing, desired):
    """Return the id of an existing tool identical to the desired definition, if any."""
    for tool in existing:
        if tool.get("type") != desired.get("type", tool.get("type")):
            continue
        same = (
            tool.get("function") == desired.get("function")
            and tool.get("async", False) == desired.get("async", False)
            and tool.get("messages", []) == desired.get("messages", [])
            and tool.get("destinations") == desired.get("destinations")
        )
        server = desired.get("server", {})
        if same and all(tool.get("server", {}).get(k) == v for k, v in server.items()):
            return tool["id"]
    return None


def transfer_tool() -> dict | None:
    """Vapi transferCall tool with the private destination filled in from TRANSFER_NUMBER."""
    raw = os.getenv("TRANSFER_NUMBER")
    if not raw:
        return None
    parsed = phonenumbers.parse(raw, "US")
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("TRANSFER_NUMBER is not a valid phone number")
    number = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    text = (
        (ROOT / "tools/transfer_call.json")
        .read_text()
        .replace("{{TRANSFER_NUMBER}}", number)
    )
    return json.loads(text)


def desired_tools(backend, credential):
    tools = []
    for name in sorted(ALLOWED_TOOLS):
        tool = json.loads((ROOT / f"tools/{name}.json").read_text())
        tool["server"] = {
            "url": backend + "/vapi/tools",
            "credentialId": credential,
            "timeoutSeconds": 10,
        }
        tools.append(tool)
    transfer = transfer_tool()
    if transfer:
        tools.append(transfer)
    return tools


def list_tools(api):
    listed = api.get("/tool")
    listed.raise_for_status()
    found = listed.json()
    return found if isinstance(found, list) else found.get("results", [])


def wait_for_backend(backend):
    """Refuse to repoint the phone until the backend serves exactly this prompt/tool version."""
    expected = config_fingerprint()["prompt_tools_sha256"]
    deadline = time.monotonic() + 300
    with httpx.Client(timeout=10) as client:
        while True:
            try:
                r = client.get(backend + "/version")
                r.raise_for_status()
                if r.json().get("prompt_tools_sha256") == expected:
                    return
            except (httpx.HTTPError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Backend not ready; live assistant unchanged")
            print("Waiting for backend readiness.", flush=True)
            time.sleep(5)


def restore(api, assistant_id, backup_path):
    backup = json.loads(backup_path.read_text())
    if backup["assistant"]["id"] != assistant_id:
        raise RuntimeError("Backup assistant differs from configured assistant")
    old = backup["assistant"]
    sections = ["model", "server", "serverMessages", "firstMessage"]
    sections += [k for k in voice_settings() if k != "model"]
    api.patch(
        "/assistant/" + assistant_id,
        json={k: old[k] for k in sections if k in old},
    ).raise_for_status()
    missing = [k for k in sections if k not in old]
    if missing:
        print(
            "Backup had no "
            + ", ".join(missing)
            + "; those sections keep their current values. Clear them in the dashboard if needed."
        )
    phone = backup["phone"]
    api.patch(
        "/phone-number/" + phone["id"],
        json={"hooks": phone.get("hooks", []), "server": phone.get("server")},
    ).raise_for_status()
    print("Restored previous assistant and phone settings; no call started.")


def apply(api, assistant_id, phone_id, backend):
    wait_for_backend(backend)
    live = api.get("/assistant/" + assistant_id)
    live.raise_for_status()
    live = live.json()
    settings = voice_settings()

    phone = api.get("/phone-number/" + phone_id)
    phone.raise_for_status()
    phone = phone.json()
    if phone.get("assistantId"):
        raise RuntimeError(
            "Phone has a static assistant; it must use the dynamic assistant-request webhook"
        )

    # Reuse the one Custom Credential (Bearer webhook secret) the existing tools use.
    servers = []
    for tool_id in live["model"].get("toolIds", []):
        tool = api.get("/tool/" + tool_id)
        tool.raise_for_status()
        tool = tool.json()
        if tool.get("function", {}).get("name") in ALLOWED_TOOLS | LEGACY_TOOLS:
            servers.append(tool.get("server", {}))
    credentials = {s.get("credentialId") for s in servers}
    if not servers or len(credentials) != 1 or None in credentials:
        raise RuntimeError("Missing/ambiguous existing server credential")
    credential = credentials.pop()

    folder = (
        Path.cwd()
        / "eval-results"
        / (
            "deployment-"
            + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + str(uuid4())[:8]
        )
    )
    private_json(folder / "backup.json", {"assistant": live, "phone": phone})

    ids = []
    existing = list_tools(api)
    for tool in desired_tools(backend, credential):
        tool_id = matching_tool(existing, tool)
        if tool_id is None:
            try:
                r = api.post("/tool", json=tool)
                r.raise_for_status()
                tool_id = r.json()["id"]
            except httpx.TimeoutException:
                # A timed-out creation may have succeeded. Reconcile; never blindly retry POST.
                tool_id = matching_tool(list_tools(api), tool)
                if tool_id is None:
                    raise RuntimeError(
                        "Tool creation unconfirmed; live assistant unchanged. Reconcile provider tools before retrying."
                    )
        ids.append(tool_id)
        private_json(folder / "created-tools.json", ids)

    patch = assistant_patch(live, ids, backend, credential, settings)
    api.patch("/assistant/" + assistant_id, json=patch).raise_for_status()

    hooks = [h for h in phone.get("hooks", []) if h.get("on") != "call.ending"]
    hooks.append({"on": "call.ending", "do": {"type": "say", "exact": FAILURE_MESSAGE}})
    server = {**(phone.get("server") or {}), "url": backend + "/vapi/assistant-request"}
    if not server.get("credentialId"):
        server["credentialId"] = credential
    api.patch(
        "/phone-number/" + phone_id, json={"hooks": hooks, "server": server}
    ).raise_for_status()

    readback = api.get("/assistant/" + assistant_id)
    readback.raise_for_status()
    readback = readback.json()
    checked_phone = api.get("/phone-number/" + phone_id)
    checked_phone.raise_for_status()
    checked_phone = checked_phone.json()
    if (
        readback["model"]["toolIds"] != ids
        or readback["model"]["messages"] != patch["model"]["messages"]
    ):
        raise RuntimeError("Assistant readback mismatch; use saved backup to restore")
    if (
        checked_phone.get("hooks") != hooks
        or checked_phone.get("server", {}).get("url") != server["url"]
    ):
        raise RuntimeError("Phone readback mismatch; use saved backup to restore")
    for section, fields in settings.items():
        if not contains(readback.get(section), fields):
            raise RuntimeError(
                "Voice settings readback mismatch in "
                + section
                + "; use saved backup to restore"
            )
    private_json(
        folder / "deployed.json",
        {
            "assistant": readback,
            "phone": checked_phone,
            "version": config_fingerprint(),
        },
    )
    print(
        "Activated versioned tools, prompt and voice settings at "
        + backend
        + ". Backup: "
        + str(folder / "backup.json")
    )


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--apply", action="store_true")
    p.add_argument("--phone-id")
    p.add_argument("--backend-url", default=os.getenv("BACKEND_URL", DEFAULT_BACKEND))
    p.add_argument(
        "--restore",
        type=Path,
        help="Restore assistant and phone settings from a prior backup.json; requires --apply",
    )
    args = p.parse_args(argv)
    backend = args.backend_url.rstrip("/")
    if not args.apply:
        print(
            json.dumps(
                {
                    "action": "plan_only",
                    "config": config_fingerprint(),
                    "tools": sorted(ALLOWED_TOOLS),
                    "live_transfer": bool(os.getenv("TRANSFER_NUMBER")),
                    "backend": backend,
                    "phone_fallback": FAILURE_MESSAGE,
                    "voice_settings": voice_settings(),
                    "tool_messages": tool_messages(),
                    "restore": str(args.restore) if args.restore else None,
                },
                indent=2,
            )
        )
        return 0
    if not backend.startswith("https://"):
        p.error("Use HTTPS")
    with httpx.Client(
        base_url="https://api.vapi.ai",
        timeout=30,
        headers={"Authorization": "Bearer " + os.environ["VAPI_API_KEY"]},
    ) as api:
        assistant_id = os.environ["VAPI_ASSISTANT_ID"]
        if args.restore:
            restore(api, assistant_id, args.restore)
            return 0
        if not args.phone_id:
            p.error("--phone-id is required with --apply")
        apply(api, assistant_id, args.phone_id, backend)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
