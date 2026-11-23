import os

# Database tests save an address and confirm it a few milliseconds later. The production
# guard refuses that as a same-turn confirmation (no caller can answer a readback that
# fast), so test connections lower the gap. libpq reads PGOPTIONS for every connection;
# the guard tests reconnect with an explicit gap to exercise the real rule.
os.environ.setdefault("PGOPTIONS", "-c summit.confirmation_gap_ms=0")
