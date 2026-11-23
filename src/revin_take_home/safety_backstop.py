"""End-of-call safety backstop.

A caller who reports gas, a CO alarm, smoke/burning or sparking is told to leave and may hang
up before (or while) report_safety_emergency reaches the backend. When the call ends, scan
what the CALLER said; if they reported a current hazard and the call has no successful
emergency save, record it through the same RPC so the dispatcher alert still goes out.

Only caller turns are read (the assistant's own safety guidance mentions every hazard), and
hypothetical or negated mentions ("what if I smell gas?", "no gas smell") don't count. The
operation id is fixed per hazard, so a replayed end-of-call-report can't record twice, and
the database queues at most one emergency alert per call.
"""

import json
import logging
import re

from .workflow import SafetyEmergency, WorkflowUnavailable

log = logging.getLogger(__name__)

# Checked in this order; the first hazard found is the one recorded.
HAZARD_PATTERNS = {
    "gas_smell": [
        r"\b(i|we)\s+(can\s+|could\s+|do\s+|still\s+|just\s+)?(smell|smelled|smelt|keep smelling|am smelling|'m smelling|are smelling)\s+(\w+\s+){0,2}?(gas|propane|rotten eggs?)\b",
        r"\bsmells?\s+(like|of)\s+(\w+\s+)?(gas|propane|rotten eggs?)\b",
        r"\b(gas|propane)\s+(smell|odou?r)\b",
        r"\b(smell|odou?r)\s+of\s+(gas|propane|rotten eggs?)\b",
        r"\brotten[- ]eggs?\s+smell\b",
        r"\b(there'?s|there is|i have|we have|it'?s|maybe|might be)\s+(a\s+)?gas\s+leak\b",
        r"\bleaking\s+gas\b",
        r"\bgas\s+(is\s+)?leaking\b",
    ],
    "carbon_monoxide_alarm": [
        r"\b(carbon[- ]monoxide|co)\s+(alarm|detector|monitor|sensor)s?\b[^.?!]{0,40}?\b(going off|went off|goes off|go off|gone off|keeps going|beep\w*|sound\w*|ring\w*|rang|blaring|alarming|triggered|flashing)\b",
        r"\b(alarm|detector)\b[^.?!]{0,30}\b(for|says|reads|reading|detecting|detected)\b[^.?!]{0,15}\b(carbon[- ]monoxide|co)\b",
    ],
    "smoke_or_burning_smell": [
        r"\bsmoke\b[^.?!]{0,30}\b(coming|pouring|out of|from)\b",
        r"\b(furnace|boiler|heater|unit|ac|a/c|air conditioner|vents?|system|it|radiator|thermostat|wires?|wiring|outlet)\s+(is\s+|are\s+|was\s+|started\s+)?(smoking|smoke)\b",
        r"\bburning\s+(smell|odou?r)\b",
        r"\bsmells?\s+(like\s+|of\s+)?(something\s+)?(burning|burnt|burned|smoke|smoky)\b",
        r"\b(smell|odou?r)\s+of\s+(something\s+)?(burning|smoke)\b",
        r"\b(i|we)\s+(can\s+)?(smell|smelled|see|saw)\s+(\w+\s+){0,2}?(smoke|burning)\b",
    ],
    "electrical_sparking": [
        r"\bspark(s|ing|ed)?\b",
        r"\barcing\b",
        r"\b(melting|melted)\s+(wires?|wiring|plug|outlet)\b",
        r"\b(wires?|wiring)\s+(is\s+|are\s+)?(melting|melted|sparking)\b",
    ],
}
# A hazard phrase preceded (in the same sentence) by one of these is a question about a
# possible future situation, not a report.
HYPOTHETICAL = re.compile(
    r"\b(if|in case|suppose|supposing|hypothetical(ly)?|should (i|we) ever|what happens|"
    r"how (do|would|can|will) (i|we) (know|tell)|in the future|ever (smell|see|notice))\b",
    re.I,
)
CLAUSE = re.compile(r"[,;:]|\b(?:but|and|so|because)\b", re.I)
NEGATION = re.compile(
    r"\b(no|not|don'?t|doesn'?t|didn'?t|never|isn'?t|aren'?t|wasn'?t|without|nothing)\b",
    re.I,
)
COMPILED = {
    hazard: [re.compile(p, re.I) for p in patterns]
    for hazard, patterns in HAZARD_PATTERNS.items()
}
SUCCESS = {"saved", "recorded"}


def caller_turns(artifact: dict | None) -> list[str]:
    """What the caller said, from artifact.messages (or the plain transcript)."""
    artifact = artifact or {}
    messages = artifact.get("messages")
    if isinstance(messages, list):
        return [
            m["message"]
            for m in messages
            if isinstance(m, dict)
            and m.get("role") in ("user", "customer")
            and isinstance(m.get("message"), str)
        ]
    transcript = artifact.get("transcript")
    if isinstance(transcript, str):
        return [
            line.split(":", 1)[1]
            for line in transcript.splitlines()
            if line.lower().startswith(("user:", "customer:"))
        ]
    return []


def _sentences(text: str) -> list[str]:
    # Join spoken "C O" / "C.O." into "co" so the CO patterns see one word.
    text = re.sub(r"\bc\.?\s?o\b\.?", "co", text, flags=re.I)
    return [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]


def detect_hazard(turns: list[str]) -> tuple[str, str] | None:
    """(hazard, caller sentence) for the first current hazard the caller reported."""
    sentences = [s for turn in turns for s in _sentences(turn)]
    for hazard, patterns in COMPILED.items():
        for sentence in sentences:
            for pattern in patterns:
                for match in pattern.finditer(sentence):
                    before = sentence[: match.start()]
                    # Only the clause the hazard is in: "not sure, but I smell gas" counts.
                    clause = CLAUSE.split(before)[-1]
                    if HYPOTHETICAL.search(clause):
                        continue
                    # "no gas smell", "it's not sparking", "I smell no gas".
                    if NEGATION.search(" ".join(before.split()[-2:] + [match.group(0)])):
                        continue
                    return hazard, sentence
    return None


def _result(content) -> dict:
    if isinstance(content, dict):
        return content
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def emergency_recorded(artifact: dict | None) -> bool:
    """True when report_safety_emergency returned a successful save during the call."""
    for m in (artifact or {}).get("messages") or []:
        if (
            isinstance(m, dict)
            and m.get("role") == "tool_call_result"
            and m.get("name") == "report_safety_emergency"
            and _result(m.get("result")).get("status") in SUCCESS
        ):
            return True
    return False


async def record_missed_emergency(store, call_id: str, artifact: dict | None) -> str | None:
    """Record a reported hazard the call didn't save. Returns the hazard recorded, if any.

    Raises WorkflowUnavailable when the save can't be confirmed.
    """
    if store is None or emergency_recorded(artifact):
        return None
    found = detect_hazard(caller_turns(artifact))
    if found is None:
        return None
    hazard, sentence = found
    request = SafetyEmergency(hazard=hazard, caller_quote=sentence[:1000])
    result = await store.execute(
        "report_safety_emergency",
        call_id,
        "end-of-call-backstop:" + hazard,
        request,
    )
    if result.get("status") not in SUCCESS:
        log.error("Safety backstop save not confirmed: %s", result.get("status"))
        raise WorkflowUnavailable
    log.warning("Safety backstop recorded %s after the call ended", hazard)
    return hazard
