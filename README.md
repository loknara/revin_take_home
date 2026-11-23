# Summit Air: AI phone agent for residential HVAC

An inbound voice agent for Summit Air, a 40-technician residential HVAC company serving
Manhattan, Brooklyn and Queens. It answers the phone, works out what the caller needs,
catches urgent situations, and books a technician into a real schedule, or gives the
caller an honest next step and alerts a dispatcher.

**Stack:** Vapi (telephony, Soniox STT, GPT-4.1, Vapi TTS) → FastAPI backend → Supabase
Postgres. Live weather comes from the National Weather Service. Dispatcher alerts go out as
ntfy push notifications.

## What it does, mapped to Summit Air's requirements

| Requirement | How the agent handles it |
|---|---|
| Figure out the HVAC issue | Classifies repair vs. maintenance and heat vs. AC outage from the caller's own words, and saves it in the first turn. No diagnostic questionnaire. |
| Residential or commercial | "My home/apartment" is residential; landlords count. Commercial or non-HVAC work is politely declined. |
| Already a member? | Checked automatically against the confirmed name and service address, within a one-second budget, for routine **and** priority calls. "Active" is mentioned once; an unverified result is never called "not a member". |
| Collect what a technician needs | Name, full service address with unit, confirmed callback, and the issue. Returning callers get their address on file offered back ("I have 152 West 72nd on your account. Is that where?"). Address and callback are confirmed in one question. |
| Routine vs. urgent | **Emergency:** gas smell, CO alarm, smoke/burning smell, sparking. Evacuation and 911 guidance is the first thing the caller hears and plays while the emergency is recorded, there is no booking, and the dispatcher is paged; if the caller hangs up before the tool request lands, an end-of-call check records it from the transcript. **Urgent priority:** no heat or no AC with a vulnerable person at home, or the caller can't safely wait. **Elevated priority:** an outage during qualifying cold or heat in the live forecast (≤40°F / ≥90°F or an NWS alert). |
| Flag priority immediately | Every priority or emergency case is recorded and a dispatcher alert is pushed within about a second, in the same database transaction, with no caller PII in the alert. Priority calls can book earlier on-call windows. |
| Book or confirm next steps | Real availability (technician zones, skills, shifts, existing jobs) → one offered window → explicit yes → atomic booking with an assigned technician. If nothing fits: an honest follow-up record plus an alert, never a made-up ETA. |
| Wants a person | During office hours: live transfer. After hours: callback request plus dispatcher alert, or transfer to the on-call line for priority calls. |

## Architecture

```
Caller ─► Vapi phone number
            │  assistant-request (caller ID)        tool calls             end-of-call report
            ▼                                          ▼                         ▼
        FastAPI backend ── /vapi/assistant-request ── /vapi/tools ──────── /vapi/events
            │   caller lookup + office hours        validates arguments,      audit
            │   → prompt context                    binds call identity
            ▼
        Supabase Postgres: all business rules live in server-only RPCs
            intake + triage + readiness · membership · availability + proposals · booking +
            technician assignment · follow-ups · dispatcher alert outbox · audit
            ▲                                   ▲
   weather worker (NWS, every 15 min)   alert worker (ntfy push, woken after each tool call)
```

**The model drives the conversation; the database decides the facts.** The LLM can't
choose a call ID, invent a slot, book without a recorded yes, or mark a case routine. It
reports what the caller said; Postgres computes triage, eligibility and availability and
enforces every booking rule in one transaction.

## Key decisions and tradeoffs

- **One neutral vulnerability question for every heat/AC outage.** "Is anyone at home who'd
  be at higher risk without heat, like an older adult, a baby, or someone with a health
  condition?" This is how we meet "no heat with an elderly person is priority" without a
  demographic interview. Only a yes/no/unknown is stored, never ages or conditions. The
  backend blocks routine booking until it's answered, so the requirement can't be skipped.
- **Weather-aware triage, tiered.** In a NYC January almost every no-heat call is "cold
  weather", so priority is split into *urgent* (a person at risk) and *elevated* (weather
  only). Alerts are ranked the same way, so a cold-snap queue still shows who matters most.
- **One offer, one yes.** The agent proposes the earliest window and asks "Should I book
  that?" The proposal is created before the offer, so the caller's yes is agreement to
  that exact window. Proposals expire in two minutes and are rechecked atomically at
  booking.
- **Safety beats everything.** The emergency tool's fixed evacuation message is spoken by
  Vapi the moment the tool is called, so the guidance doesn't depend on our backend being
  up. The request runs in parallel (a blocking message lost the record when callers hung
  up to evacuate), and the end-of-call report re-checks what the caller said: a reported
  gas smell, CO alarm, smoke/burning smell or sparking with no saved emergency is recorded
  and paged then (`safety_backstop.py`).
- **Caller ID is a hint, not identity.** It pre-fills an *unconfirmed* callback, and an
  address only after a unique name match. Nothing is used without the caller's yes.
- **Coverage by ZIP.** All residential ZIPs in New York, Kings and Queens counties.
  Neighborhood names (Astoria, Williamsburg) don't break coverage.
- **Honest outcomes only.** The agent says "booked" only after the database returns
  booked, never says a technician is en route, and never quotes prices or plan benefits
  (no approved policy yet).

## Built vs. deliberately skipped

| Built | Skipped for now (and why) |
|---|---|
| Atomic booking + synthetic 40-tech roster with zones, skills, shifts, travel buffer | Real dispatch/CRM integration (needs Summit Air's system and credentials) |
| Live NWS weather triage, tiered priority, dispatcher push alerts | Reserving on-call capacity for urgent over elevated callers (needs Summit Air's on-call policy) |
| Office-hours aware live transfer + callback requests | Warm transfer with a spoken summary (needs a staffed line to test) |
| Membership lookup by property | Plan benefits, pricing and discounts (no approved policy) |
| SMS/email confirmation outbox (parked in `tools/optional/`) | Turning it on: needs a registered Twilio sender / verified email domain. The agent doesn't offer what can't arrive. |
| English | Spanish and other languages (STT is configured for English) |
| Text-based Vapi simulations + database invariant tests | Reschedule/cancel self-service, photo upload of the unit's nameplate |

## Run it

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), the Supabase CLI and an ngrok
account with a free static domain.

```sh
uv sync
cp .env.example .env          # fill in secrets locally; never commit them
supabase db push --dry-run    # review, then: supabase db push --yes
                              # seed once: run supabase/seed.sql in the SQL editor
./scripts/serve-local         # uvicorn + ngrok on NGROK_DOMAIN, Mac kept awake
uv run --env-file .env python -m revin_take_home.configure_vapi --apply \
    --phone-id <VAPI_PHONE_ID> --backend-url https://<NGROK_DOMAIN>
```

`configure_vapi --apply` waits until the backend serves this exact prompt and tool
fingerprint, creates new versioned tools (live calls keep their old ones), applies the
voice settings in `config/voice-baseline.json` (Soniox endpointing and vocabulary, TTS
chunking, interruption threshold, denoising), points the
assistant and the phone number's `assistant-request` webhook at the backend, adds the
transfer tool if `TRANSFER_NUMBER` is set, and saves a private rollback snapshot under
`eval-results/`. Roll back with `--apply --restore <backup.json>`. It never starts a call.

**Self-hosting caveat:** with ngrok the Mac must stay plugged in, online and awake for the
whole demo window. If it doesn't, Vapi speaks a fixed "temporarily unavailable" message
(including gas/CO guidance). For a pilot, move to always-on hosting; `render.yaml` is kept
for that.

Subscribe to your `NTFY_TOPIC` in the ntfy phone app to receive dispatcher alerts.

### Configuration

| Variable | Purpose |
|---|---|
| `VAPI_ASSISTANT_ID`, `VAPI_WEBHOOK_SECRET` | Assistant to serve; Bearer secret Vapi sends to every endpoint |
| `SUPABASE_URL`, `SUPABASE_SECRET_KEY` | Backend-only database access (RLS denies all client roles) |
| `NGROK_DOMAIN`, `BACKEND_URL` | Local hosting and the default `configure_vapi` target |
| `NTFY_TOPIC` (+ `NTFY_SERVER`, `NTFY_TOKEN`) | Dispatcher alerts; unset means alerts are recorded but marked `blocked_unconfigured` |
| `TRANSFER_NUMBER`, `OFFICE_HOURS` | Live-transfer destination (private) and office hours, America/New_York |
| `WEATHER_HOT_F`, `WEATHER_COLD_F` | Dispatch policy thresholds (defaults 90/40°F), not medical limits |
| `ENABLE_EVALUATION_API` | Serve `/evaluation/*` for paid simulations; off by default |

## Testing

```sh
./scripts/go                  # offline unit tests + the simulation plan (no paid requests)
supabase db start             # local Postgres with all migrations + seed
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres \
  RUN_DATABASE_TESTS=1 SUPABASE_DB_PASSWORD=postgres \
  uv run --with 'psycopg[binary]' pytest -q      # ~330 tests incl. every SQL invariant
```

Database tests run each scenario against real migrations in a transaction that is rolled
back. They cover triage, coverage, booking races, proposal expiry, idempotent retries,
emergency handling and alert leasing. Paid Vapi chat simulations (`evals/scenarios.json`, 28
scenarios, opt-in with `--allow-paid-tests`) check both the conversation and what was
actually written to the database. `--mode voice --candidate-voice` tries this checkout's
tool messages and voice settings on the simulated agent only, without touching the live
assistant. They don't test real phone audio, accents, barge-in or telephone
routing; those need real calls.

## Where the logic lives

| Concern | Code |
|---|---|
| Conversation | `prompts/summit-air-system.txt`, `tools/*.json` |
| Webhooks, argument validation, call binding | `src/revin_take_home/app.py`, `intake.py`, `workflow.py` |
| Triage (emergency / urgent / elevated / routine) | `assess_intake_risk` in `supabase/migrations/20261124000200_triage_v2.sql` |
| Coverage and readiness | `20261124000100_county_coverage.sql` |
| Weather cache and per-call evidence | `weather.py`, `20261123000200_weather_urgency.sql` |
| Availability, proposals, booking, technician assignment | `20261121000700_transactional_booking.sql`, `20261123000300_technician_dispatch.sql` |
| Membership | `membership.py`, `20261121001000_worker_and_location_hardening.sql` |
| Dispatcher alerts | `alerts.py`, `20261124000300_dispatcher_alerts.sql` |
| Office hours, transfer | `office.py`, `tools/transfer_call.json`, `configure_vapi.py` |

Migrations are append-only because the hosted database already has the earlier ones
applied. Where a function was extended, the old version is kept under a `*_before_*` name
and wrapped, so each migration shows exactly what changed.

## Operating notes

- `GET /health` is liveness only. `GET /version` shows the deployed prompt/tool fingerprint.
- Inspect a call by its Vapi call ID in `call_intakes`, `intake_events`, `priority_requests`,
  `dispatcher_alerts`, `booking_proposals`, `appointments`, `dispatch_jobs`,
  `followup_requests`, `call_audit` and `tool_audit`.
- A lost booking response is reconciled with `get_booking_status`, never by booking again.
  Hanging up never cancels a booking; changes after booking become a follow-up.
- An alert push that times out may be retried (up to 3 attempts), so a rare duplicate
  page is possible; a missed page is not.

## Before a real rollout

Confirm Summit Air's actual service ZIPs, hours, on-call policy, membership benefits and
pricing language; connect their scheduling/dispatch system and real roster; route alerts
to their dispatch channel; move to always-on hosting; run the simulation suite repeatedly
plus reviewed real calls; then pilot on overflow and after-hours calls with a human
watching the alert channel.
