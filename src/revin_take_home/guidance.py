"""Tool-result guidance for the agent: what failed, what to send, and what to do next.

Shared contract (the prompt is written against it):
* Every non-success result carries ``status``, ``missing`` (field names, possibly empty),
  ``expected`` (exact saved values to echo, possibly empty) and a one-sentence ``hint``.
* When one tool has failed twice with the same status in a call, its next failure becomes
  ``stop_retrying``: record ``request_followup(reason="technical_issue")`` and tell the
  caller the team will follow up.
* Slots carry ``spoken``, a ready-to-say window relative to today in New York.
"""

from collections import Counter, OrderedDict
from datetime import datetime, timedelta

from .intake import IntakeChanges
from .office import DAY_NAMES, NEW_YORK

# Statuses that mean "this call did not do what the agent wanted; fix something and retry".
FAILURES = {
    "invalid_arguments",
    "revision_conflict",
    "operation_conflict",
    "confirmation_mismatch",
    "needs_caller_yes",
    "needs_intake",
    "needs_details",
    "not_eligible",
    "stale_availability",
    "invalid_slot",
    "unknown_proposal",
    "stale_proposal",
    "slot_unavailable",
    "slot_conflict",
    "technician_unavailable",
    "save_unconfirmed",
    "outcome_unconfirmed",
    "unsupported_tool",
}
# Final answers rather than errors: they get a hint, but retrying them is not a loop.
OUTCOMES = {
    "out_of_scope",
    "emergency_guidance_required",
    "no_availability",
    "already_booked",
}
# Outcomes that end a run of failures for that tool.
SUCCESSES = {
    "saved",
    "available",
    "no_availability",
    "offered",
    "booked",
    "declined",
    "recorded",
    "current",
    "not_booked",
    "on_hold",
}

RETRY_STATUSES = {
    "stale_availability",
    "invalid_slot",
    "unknown_proposal",
    "stale_proposal",
    "slot_unavailable",
    "slot_conflict",
    "technician_unavailable",
}

HINTS = {
    "invalid_arguments": "Fix the fields named in bad_fields and send the call again; do not resend the same arguments.",
    "operation_conflict": "That call id was already used with different content; send a fresh call built from the latest state.",
    "needs_intake": "Save what the caller said with update_intake first, then try again.",
    "not_eligible": "Not ready yet: collect and save the fields in missing (start with next_required) before trying again.",
    "save_unconfirmed": "The save may not have happened: do not say it was saved; call refresh_context to check, then retry once if it is missing.",
    "outcome_unconfirmed": "The result is unknown: do not claim success; call get_booking_status (or refresh_context) to check before retrying.",
    "unsupported_tool": "That tool does not exist; use only the listed tools.",
    "out_of_scope": "Summit Air pages a person only for customers who need home heating or cooling service: if the caller needs service, save what it is in issue_summary and try again; if they are selling something or calling about anything else, politely decline and take no message.",
    "emergency_guidance_required": "This is a safety emergency: repeat the evacuation and 911 guidance only; do not record follow-ups or book.",
    "no_availability": "No windows are open: record request_followup with reason no_availability and tell the caller the team will call to schedule; never invent a time.",
    "already_booked": "This call already has a booking: call get_booking_status and read that window instead of booking again.",
}
for _status in RETRY_STATUSES:
    HINTS[_status] = "That window is no longer valid: call search_slots again and offer a fresh window from the new result."

ADDRESS_PARTS = {
    "service_address",
    "service_unit",
    "service_zip",
    "service_city",
    "service_state",
}
UPDATE_TOP_LEVEL = {
    "expected_revision",
    "caller_quote",
    "changes",
    "confirmation_address",
    "confirmation_callback",
}


def normalize_update_intake(arguments):
    """Repair the common malformed update_intake shapes instead of losing the facts.

    Confirmation fields (and the quote/revision) found inside ``changes`` move to the top
    level; known fact fields sent at the top level move into ``changes``. Unknown fields are
    left alone so validation still names them. Returns (arguments, moved_field_names).
    """
    if not isinstance(arguments, dict):
        return arguments, []
    arguments = dict(arguments)
    moved = []
    changes = arguments.get("changes", {})
    if not isinstance(changes, dict):
        return arguments, []
    changes = dict(changes)
    for key in ("confirmation_address", "confirmation_callback", "caller_quote", "expected_revision"):
        if key in changes:
            value = changes.pop(key)
            arguments.setdefault(key, value)
            moved.append("changes." + key)
    for key in list(arguments):
        if key in IntakeChanges.model_fields and key not in UPDATE_TOP_LEVEL:
            changes.setdefault(key, arguments.pop(key))
            moved.append(key)
    if moved or "changes" in arguments:
        arguments["changes"] = changes
    return arguments, moved


def _details(result):
    state = result.get("state")
    return state.get("details", {}) if isinstance(state, dict) else {}


def _revision(result):
    state = result.get("state")
    if isinstance(state, dict) and isinstance(state.get("revision"), int):
        return state["revision"]
    return result.get("current_revision")


def _saved_values(details, address=True, callback=True):
    expected = {}
    if address and details.get("service_address"):
        expected["confirmation_address"] = details["service_address"]
    if callback and details.get("callback_number"):
        expected["confirmation_callback"] = details["callback_number"]
    return expected


def _bad_fields(errors):
    fields = []
    for error in errors:
        name = ".".join(str(part) for part in error.get("field", []))
        if name and name not in fields:
            fields.append(name)
    return fields


def _intake_guidance(result, request):
    """Fill missing/expected/hint for an update_intake that wasn't saved."""
    status = result["status"]
    details = _details(result)
    changes = (request or {}).get("changes") or {}
    wants_address = changes.get("address_confirmed") is True
    wants_callback = changes.get("callback_confirmed") is True
    expected = _saved_values(details, wants_address, wants_callback)
    if status == "confirmation_mismatch":
        missing = []
        if wants_address and not (request or {}).get("confirmation_address"):
            missing.append("confirmation_address")
        if wants_callback and not (request or {}).get("confirmation_callback"):
            missing.append("confirmation_callback")
        if wants_address and not details.get("service_address"):
            hint = "No address is saved yet: save service_address, unit and ZIP first, read them back, and confirm after the caller says yes."
        elif missing:
            hint = "Add " + " and ".join(missing) + " at the top level (not inside changes), copied from expected, with the caller's yes as caller_quote."
        else:
            hint = "Your readback doesn't match what is saved: read expected back to the caller, and if they say yes send it exactly as shown in expected."
        return missing, expected, hint
    if status == "needs_caller_yes":
        if result.get("saved_other_changes"):
            hint = "The new details are saved but not confirmed: read expected back to the caller now, END YOUR TURN, and confirm only after they say yes."
        elif result.get("reason") == "quote_is_readback":
            hint = "caller_quote must be the caller's own answer, not your readback: read expected back, END YOUR TURN, and confirm only after the caller says yes."
        else:
            hint = "You confirmed in the same turn the value was saved: read expected back to the caller now, END YOUR TURN, and confirm only after they say yes."
        return [], expected, hint
    if status == "revision_conflict":
        revision = _revision(result)
        return [], {"expected_revision": revision}, f"Someone saved first: resend with expected_revision={revision} (the latest state.revision)."
    return [], {}, HINTS.get(status, "Check the result, fix the call, and try again.")


def shape(tool, result, request=None, now=None):
    """Add the contract fields to a non-success result and spoken labels to slots."""
    now = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    status = result.get("status")
    if tool == "refresh_context":
        result.setdefault("today", today_label(now))
    _add_spoken(tool, result, now)
    if status not in FAILURES | OUTCOMES:
        return result
    if tool == "update_intake" and status in {
        "confirmation_mismatch",
        "needs_caller_yes",
        "revision_conflict",
    }:
        missing, expected, hint = _intake_guidance(result, request)
    elif status == "invalid_arguments":
        bad = _bad_fields(result.get("errors", []))
        result.setdefault("bad_fields", bad)
        missing = [
            ".".join(str(p) for p in e["field"])
            for e in result.get("errors", [])
            if e.get("type") == "missing"
        ]
        expected = {}
        hint = (
            "Fix " + ", ".join(bad) + " (see errors) and send the call again; do not resend the same arguments."
            if bad
            else HINTS["invalid_arguments"]
        )
    elif status == "revision_conflict":
        revision = _revision(result)
        missing, expected = [], {"expected_revision": revision}
        hint = f"Resend with expected_revision={revision} (the latest revision)."
    elif status == "needs_details":
        missing = result.get("missing") or []
        expected = result.get("expected") or {}
        hint = (
            "Before recording a callback request, get the caller's name and confirm the callback number"
            + (" (read back expected.confirmation_callback)" if expected else "")
            + ", save them with update_intake, then call request_followup again."
        )
    elif status == "not_eligible":
        readiness = result.get("readiness") or {}
        missing = list(readiness.get("missing_fields") or [])
        expected = {}
        hint = HINTS["not_eligible"]
        if readiness.get("next_required"):
            hint = f"Not ready yet: next collect and save {readiness['next_required']}, then the rest of missing, before trying again."
    else:
        missing = result.get("missing") or []
        expected = result.get("expected") or {}
        hint = HINTS.get(status, "Check the result, fix the call, and try again.")
    result.setdefault("missing", missing)
    result.setdefault("expected", expected)
    result.setdefault("hint", hint)
    return result


class RetryTracker:
    """Per-call memory of failed tool calls (in-process; bounded)."""

    def __init__(self, max_calls=2000):
        self.calls = OrderedDict()
        self.max_calls = max_calls

    def observe(self, call_id, tool, result):
        status = result.get("status")
        tools = self.calls.setdefault(call_id, {})
        self.calls.move_to_end(call_id)
        while len(self.calls) > self.max_calls:
            self.calls.popitem(last=False)
        counts = tools.setdefault(tool, Counter())
        if status in SUCCESSES:
            counts.clear()
            return result
        if status not in FAILURES:
            return result
        repeated = max(counts.values(), default=0) >= 2
        counts[status] += 1
        if not repeated:
            return result
        hint = (
            "Stop retrying: tell the caller you couldn't record the request and give the office hours so they can call back."
            if tool == "request_followup"
            else f"Stop calling {tool}: call request_followup with reason technical_issue, then tell the caller the team will follow up (no time promised)."
        )
        return result | {"status": "stop_retrying", "failed_status": status, "hint": hint}


# Spoken dates and times -----------------------------------------------------------------

ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen",
]
TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty"}
ORDINAL_ONES = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth", 7: "seventh",
    8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth", 13: "thirteenth",
    14: "fourteenth", 15: "fifteenth", 16: "sixteenth", 17: "seventeenth",
    18: "eighteenth", 19: "nineteenth",
}
MONTHS = [
    "January", "February", "March", "April", "May", "June", "July", "August",
    "September", "October", "November", "December",
]


def number_words(n):
    if n < 20:
        return ONES[n]
    tens, ones = divmod(n, 10)
    return TENS[tens] + ("-" + ONES[ones] if ones else "")


def ordinal_words(n):
    if n in ORDINAL_ONES:
        return ORDINAL_ONES[n]
    tens, ones = divmod(n, 10)
    if not ones:
        return TENS[tens][:-1] + "ieth"
    return TENS[tens] + "-" + ORDINAL_ONES[ones]


def time_words(value):
    if value.minute == 0 and value.hour == 12:
        return "noon"
    if value.minute == 0 and value.hour == 0:
        return "midnight"
    hour = number_words(value.hour % 12 or 12)
    if value.minute == 0:
        return hour
    if value.minute < 10:
        return f"{hour} oh {number_words(value.minute)}"
    return f"{hour} {number_words(value.minute)}"


def date_words(day):
    return f"{DAY_NAMES[day.weekday()]}, {MONTHS[day.month - 1]} {ordinal_words(day.day)}"


def spoken_window(starts_at, ends_at, now):
    """'today between nine and noon', 'tomorrow, Tuesday, November twenty-fourth, between one and four'."""
    start = _as_local(starts_at)
    end = _as_local(ends_at)
    between = f"between {time_words(start)} and {time_words(end)}"
    if start.hour < 7 or start.hour >= 17:
        between += " in the evening" if start.hour >= 17 else " in the early morning"
    today = now.astimezone(NEW_YORK).date()
    if start.date() == today:
        return f"today {between}"
    if start.date() == today + timedelta(days=1):
        return f"tomorrow, {date_words(start.date())}, {between}"
    return f"{date_words(start.date())}, {between}"


def today_label(now):
    local = now.astimezone(NEW_YORK)
    return f"{DAY_NAMES[local.weekday()]}, {MONTHS[local.month - 1]} {local.day}, {local.year}"


def _as_local(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.astimezone(NEW_YORK)


def _label(window, now):
    if isinstance(window, dict) and window.get("starts_at") and window.get("ends_at"):
        try:
            window["spoken"] = spoken_window(window["starts_at"], window["ends_at"], now)
        except (TypeError, ValueError):
            pass


def _add_spoken(tool, result, now):
    if tool == "search_slots":
        for slot in result.get("slots") or []:
            _label(slot, now)
    elif tool == "propose_booking" and isinstance(result.get("arrival"), dict):
        _label(result["arrival"], now)
        if "spoken" in result["arrival"]:
            result["spoken"] = result["arrival"]["spoken"]
    elif tool in {"get_booking_status", "confirm_booking"} and result.get("starts_at"):
        _label(result, now)
