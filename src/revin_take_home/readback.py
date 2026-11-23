"""Did the caller actually hear their address or number read back? (round 3 finding R9)

The database guard can only judge how a confirmation's quote is worded and when it
arrives. In testing the agent still marked details confirmed after the caller said
something unrelated ("Please have someone from your office call me"), without any
readback. Vapi sends the conversation so far with every tool call
(`message.artifact.messages`, roles `bot` / `user`), so before a confirmation is saved we
check that one of the agent's last few turns really read that street or those digits.

Deliberately lenient: it only asks whether a readback happened, not whether it was word
perfect (the database already checks the confirmed value against what is saved). When the
conversation isn't available the check is skipped rather than blocking the call.
"""

import re

AGENT_ROLES = {"bot", "assistant"}
CALLER_ROLES = {"user"}
# How many recent agent turns may contain the readback: a caller who corrects one part
# gets only that part read again, so the other part was read a turn or two earlier.
AGENT_TURNS = 3

DIGIT_WORDS = {
    "zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}
# Words that say nothing about which street it is.
GENERIC = {
    "street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "place", "pl",
    "lane", "ln", "drive", "dr", "parkway", "pkwy", "court", "ct", "terrace", "way",
    "nd", "th", "apt", "apartment", "unit", "floor", "fl", "the", "of", "and",
}


def recent_agent_text(artifact, turns=AGENT_TURNS):
    """Text of the agent's last few turns before the caller's latest words, or None."""
    if not isinstance(artifact, dict) or not isinstance(artifact.get("messages"), list):
        return None
    messages = [m for m in artifact["messages"] if isinstance(m, dict)]
    last_caller = max(
        (i for i, m in enumerate(messages) if m.get("role") in CALLER_ROLES), default=None
    )
    if last_caller is None:
        return None
    blocks, current, i = [], [], last_caller - 1
    while i >= 0 and len(blocks) < turns:
        m = messages[i]
        if m.get("role") in AGENT_ROLES:
            current.insert(0, str(m.get("message") or m.get("content") or ""))
        elif current and m.get("role") in CALLER_ROLES:
            blocks.append(" ".join(current))
            current = []
        i -= 1
    if current and len(blocks) < turns:
        blocks.append(" ".join(current))
    return " ".join(reversed(blocks))


def _digits(text):
    """Digits in text, counting spoken digits ("two one two") as digits too."""
    words = re.findall(r"[a-z]+|\d", text.lower())
    return "".join(DIGIT_WORDS.get(w, w) for w in words if w.isdigit() or w in DIGIT_WORDS)


def number_was_read(agent_text, number):
    """The last four digits of the number were spoken (or "the number you're calling from")."""
    if re.search(r"number you(?:'| a)re calling from|calling from", agent_text, re.I):
        return True
    tail = re.sub(r"\D", "", number or "")[-4:]
    if len(tail) < 4:
        return len(_digits(agent_text)) >= 4
    return tail in _digits(agent_text)


def address_was_read(agent_text, street):
    """Something identifying the street was spoken: a distinctive word or its numbers."""
    spoken = agent_text.lower()
    tokens = re.findall(r"\d+|[a-z]+", (street or "").lower())
    distinctive = [t for t in tokens if t not in GENERIC]
    if not distinctive:
        return bool(re.search(r"\b(" + "|".join(sorted(GENERIC)) + r")\b", spoken))
    spoken_words = set(re.findall(r"[a-z]+", spoken))
    spoken_numbers = set(re.findall(r"\d+", spoken))
    return any(t in spoken_numbers if t.isdigit() else t in spoken_words for t in distinctive)


def unheard_confirmations(artifact, changes, confirmation_address, confirmation_callback):
    """Which requested confirmations the caller can't have heard read back."""
    agent_text = recent_agent_text(artifact)
    if agent_text is None:
        return []
    unheard = []
    if changes.get("address_confirmed") is True and not address_was_read(agent_text, confirmation_address):
        unheard.append("address_confirmed")
    if changes.get("callback_confirmed") is True and not number_was_read(agent_text, confirmation_callback):
        unheard.append("callback_confirmed")
    return unheard


# A booking needs an offer the caller heard (voice testing, 2026-11-23): with spoken fillers
# the model twice booked on "Sure. Thank you." without ever saying the window or asking.
WINDOW_WORDS = re.compile(
    r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|hoy|mañana|lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)\b",
    re.I,
)
TIME_WORDS = re.compile(
    r"\b(between|from|noon|morning|afternoon|a\.?m\.?|p\.?m\.?|entre|mediodía|mediodia|tarde)\b|\d",
    re.I,
)


def offer_was_heard(artifact):
    """The agent's latest turn said a day and a time (the window it is asking about).

    None when the conversation isn't available (the check is then skipped)."""
    agent_text = recent_agent_text(artifact, turns=1)
    if agent_text is None:
        return None
    return bool(WINDOW_WORDS.search(agent_text) and TIME_WORDS.search(agent_text))
