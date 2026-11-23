"""Drive the real assistant turn by turn over Vapi's Chat API, for hands-on testing.

Same prompt, tools, model and backend as the phone line; tool calls go to an isolated
`eval_` session (synthetic inventory, alerts suppressed). No audio, STT, TTS or barge-in.

    python -m revin_take_home.chat_driver start NAME --fixture maya [--context '{"office_open": true}'] [--with-transfer]
    python -m revin_take_home.chat_driver say NAME "caller words"
    python -m revin_take_home.chat_driver evidence NAME
    python -m revin_take_home.chat_driver close NAME

Requires BACKEND_URL (serving ENABLE_EVALUATION_API=1), VAPI_API_KEY, VAPI_WEBHOOK_SECRET,
VAPI_ASSISTANT_ID, SUPABASE_URL and SUPABASE_SECRET_KEY. Each turn costs ~$0.015 in Vapi
model + chat charges. Transcripts are saved under eval-results/chats/.
"""

import argparse
import json
import os
from pathlib import Path

import httpx

from .audit import ROOT
from .evaluation import ALLOWED_TOOLS

STATE = Path.cwd() / "eval-results" / "chats"
# A fictional destination: chat has no phone leg, so this only shows the model's intent.
FAKE_TRANSFER = "+12125550142"


def clients():
    backend = httpx.Client(
        base_url=os.environ["BACKEND_URL"].rstrip("/"),
        headers={"Authorization": "Bearer " + os.environ["VAPI_WEBHOOK_SECRET"]},
        timeout=30,
    )
    vapi = httpx.Client(
        base_url="https://api.vapi.ai",
        headers={"Authorization": "Bearer " + os.environ["VAPI_API_KEY"]},
        timeout=90,
    )
    return backend, vapi


def load(name):
    return json.loads((STATE / f"{name}.json").read_text())


def save(name, state):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{name}.json").write_text(json.dumps(state, indent=2))


def build_assistant(vapi, session, backend_url, context_override, with_transfer):
    live = vapi.get("/assistant/" + os.environ["VAPI_ASSISTANT_ID"]).json()
    credential = None
    for tool_id in live["model"]["toolIds"]:
        tool = vapi.get("/tool/" + tool_id).json()
        credential = credential or tool.get("server", {}).get("credentialId")
    context = {**session["caller_context"], **context_override}
    if with_transfer:
        context["live_transfer_configured"] = True
    prompt = (ROOT / "prompts/summit-air-system.txt").read_text()
    model = {k: v for k, v in live["model"].items() if k in ("provider", "model", "temperature", "maxTokens")}
    model["messages"] = [{"role": "system", "content": prompt.replace("{{caller_context}}", json.dumps(context))}]
    model["tools"] = []
    for name in sorted(ALLOWED_TOOLS):
        tool = json.loads((ROOT / f"tools/{name}.json").read_text())
        tool["server"] = {
            "url": backend_url + "/evaluation/sessions/" + session["session_id"] + "/tools",
            "credentialId": credential,
            "timeoutSeconds": 10,
        }
        model["tools"].append(tool)
    if with_transfer:
        transfer = json.loads((ROOT / "tools/transfer_call.json").read_text().replace("{{TRANSFER_NUMBER}}", FAKE_TRANSFER))
        transfer["destinations"][0]["numberE164CheckEnabled"] = False
        model["tools"].append(transfer)
    return {"name": "Summit Air chat test", "model": model, "firstMessage": live.get("firstMessage")}, context


def render(items):
    """Print what a caller would hear, plus the tool traffic behind it."""
    lines = []
    for item in items:
        role = item.get("role")
        for call in item.get("tool_calls") or item.get("toolCalls") or []:
            fn = call.get("function", {})
            args = fn.get("arguments")
            try:
                args = json.loads(args) if isinstance(args, str) else args
            except ValueError:
                pass
            lines.append(f"  [tool call] {fn.get('name')} {json.dumps(args)}")
        content = item.get("content")
        if role == "tool":
            try:
                result = json.loads(content)
            except (TypeError, ValueError):
                result = {"raw": str(content)[:200]}
            summary = {k: result.get(k) for k in ("status", "message") if k in result}
            state = result.get("state") or {}
            if state.get("triage"):
                t = state["triage"]
                summary["triage"] = {k: t.get(k) for k in ("level", "priority_tier", "reasons", "missing_risk_fields", "ask_next")}
            if result.get("readiness"):
                r = result["readiness"]
                summary["readiness"] = {k: r.get(k) for k in ("coverage", "scope", "can_book", "next_required")}
            if result.get("membership"):
                summary["membership"] = result["membership"].get("membership_status")
            if result.get("priority_request"):
                summary["alert"] = result["priority_request"].get("dispatcher_alert")
            for key in ("slots", "arrival_window", "technician_name", "reference", "dispatch"):
                if key in result:
                    summary[key] = result[key] if key != "slots" else [s.get("arrival_window") for s in result[key]]
            lines.append(f"  [tool result] {json.dumps(summary)}")
        elif content:
            lines.append(f"AGENT: {content}")
        elif role not in ("assistant", "tool"):
            lines.append(f"  [{role}] {json.dumps(item)[:300]}")
    return "\n".join(lines)


def start(args):
    backend, vapi = clients()
    r = backend.post("/evaluation/sessions", json={"fixture_key": args.fixture})
    r.raise_for_status()
    session = r.json()
    assistant, context = build_assistant(
        vapi, session, os.environ["BACKEND_URL"].rstrip("/"), json.loads(args.context or "{}"), args.with_transfer
    )
    save(args.name, {"session": session, "assistant": assistant, "context": context, "chat_id": None, "turns": [], "cost": 0.0})
    print(f"Session {session['call_id']} (fixture {args.fixture}); context: {json.dumps(context)}")
    print(f"AGENT: {assistant['firstMessage']}")


def say(args):
    _, vapi = clients()
    state = load(args.name)
    body = {"assistant": state["assistant"], "input": args.text}
    if state["chat_id"]:
        body["previousChatId"] = state["chat_id"]
    r = vapi.post("/chat", json=body)
    if not r.is_success:
        print(f"Vapi chat failed: HTTP {r.status_code} {r.text[:500]}")
        raise SystemExit(1)
    out = r.json()
    state["chat_id"] = out["id"]
    state["cost"] += out.get("cost") or 0
    state["turns"].append({"caller": args.text, "output": out.get("output", [])})
    save(args.name, state)
    print(f"CALLER: {args.text}")
    print(render(out.get("output", [])))


def evidence(args):
    backend, _ = clients()
    state = load(args.name)
    sid = state["session"]["session_id"]
    ev = backend.get("/evaluation/sessions/" + sid).json()["evidence"]
    intake = (ev.get("call_intakes") or [{}])[0]
    details = {k: v for k, v in (intake.get("details") or {}).items() if not k.startswith("_") and "quote" not in k}
    alerts = [{k: a.get(k) for k in ("kind", "title", "body", "status")} for a in ev.get("dispatcher_alerts", [])]
    print(json.dumps({
        "details": details,
        "triage": intake.get("triage"),
        "priority_requests": [{k: p.get(k) for k in ("highest_level", "priority_tier", "observed_reasons", "needs_review")} for p in ev.get("priority_requests", [])],
        "membership_checks": [c.get("result", {}).get("membership_status") for c in ev.get("call_membership_checks", [])],
        "appointments": [{k: a.get(k) for k in ("status", "dispatch_status", "hold_reason")} | (a.get("slot") or {}) for a in ev.get("appointments", [])],
        "agreements": [{k: a.get(k) for k in ("decision", "caller_quote")} for a in ev.get("booking_agreements", [])],
        "followups": [f.get("reasons") for f in ev.get("followup_requests", [])],
        "dispatcher_alerts": alerts,
        "intake_saves": len(ev.get("intake_events", [])),
        "chat_cost_usd": round(state["cost"], 3),
    }, indent=1, default=str))


def close(args):
    backend, _ = clients()
    state = load(args.name)
    backend.post("/evaluation/sessions/" + state["session"]["session_id"] + "/close")
    print(f"Closed {args.name}; chat cost ${state['cost']:.3f}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    s.add_argument("name")
    s.add_argument("--fixture", choices=["maya", "shared", "unknown", "withheld"], default="unknown")
    s.add_argument("--context", help="JSON merged into the call-start context, e.g. office hours")
    s.add_argument("--with-transfer", action="store_true", help="attach a transfer tool with a fictional number")
    s.set_defaults(fn=start)
    s = sub.add_parser("say")
    s.add_argument("name")
    s.add_argument("text")
    s.set_defaults(fn=say)
    for command, fn in (("evidence", evidence), ("close", close)):
        s = sub.add_parser(command)
        s.add_argument("name")
        s.set_defaults(fn=fn)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
