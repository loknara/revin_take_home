# Summit Air demo

Inbound residential HVAC intake and synthetic appointment booking through Vapi, a Python/FastAPI service on Render, and Supabase. **Bookings are demo reservations. No real technician, dispatcher notification, callback or emergency-service contact is connected.**

## Run locally

Requires Python 3.11+, uv and a Supabase project with the migrations applied.

```sh
uv sync
cp .env.example .env
# Fill in private credentials locally; never commit them.
uv run --env-file .env uvicorn revin_take_home.app:create_app --factory --host 0.0.0.0 --port 8000
```

Render build: `pip install .`  
Render start: `uvicorn revin_take_home.app:create_app --factory --host 0.0.0.0 --port $PORT`

Set `VAPI_ASSISTANT_ID`, `VAPI_WEBHOOK_SECRET`, `SUPABASE_URL`, and `SUPABASE_SECRET_KEY` in Render. `VAPI_API_KEY` and `SUPABASE_DB_PASSWORD` are local administration/testing credentials; the web service does not need them. Supabase secret/service-role keys belong only on the backend.

Apply migrations through the linked Supabase CLI, review the dry run first, then seed the synthetic directory:

```sh
uv run --env-file .env supabase db push --dry-run
uv run --env-file .env supabase db push --yes
# Execute supabase/seed.sql in the linked project's SQL editor once.
```

All application tables use RLS and deny anonymous/authenticated client access. Writes use narrow server-only RPCs. Caller ID is a candidate match, **not authentication**; this demo never changes account phone links.

## Phone configuration

Use a Vapi inbound number with dynamic assistant selection: phone number `server.url` points to `/vapi/assistant-request`, with a Vapi Custom Credential holding the matching Bearer webhook secret. Leave its assistant/squad/workflow assignment empty. The existing Summit assistant supplies the model, STT and TTS settings.

The configured STT/model/TTS baseline is versioned in `config/voice-baseline.json`; provider aliases and automatic STT fallback can change the actual serving implementation, so retain provider call metadata when evaluating.

Versioned prompt: `prompts/summit-air-system.txt`. Tool definitions: `tools/`. The active workflow uses update_intake, refresh_context, search_slots, propose_booking, confirm_booking, get_booking_status, request_followup and report_gas_emergency. Membership is returned in the intake response; refresh_context is for recovery, not routine membership polling. `resolve_membership` remains available in the backend for compatibility but is not attached to the completed agent.

Use `python -m revin_take_home.configure_vapi` to inspect the deployment plan; `--apply --phone-id ID` requires a deployed matching backend and updates the existing assistant with newly created versioned tools. It preserves STT/TTS/model settings, saves a private rollback snapshot, installs an authenticated end-of-call report endpoint, and configures a provider-hosted message for assistant-request failures. It starts no calls. See the command's `--help` for rollback.

Render Free can sleep. An assistant-request cold start can exceed Vapi's fixed deadline. The provider-hosted failure message is a graceful failure, **not an availability fix**. Warm the service and verify `/version` before the demo; choose always-on hosting for a production pilot. Do not run an endless keep-awake job to disguise this limit.

## Demo operating policy

- Residential HVAC repair/maintenance only. A landlord's residential property is eligible.
- Explicit ZIP allowlist: 10023, 10024, 10025, 10075 (New York County); 11201 (Kings); 11375 (Queens). This is partial coverage for a fictional demo, not confirmed customer policy.
- Seeded routine capacity: Monday–Saturday, 9am–noon and 1pm–4pm, America/New_York; two places per zone/job type/window, from tomorrow through the next 14 days. Existing capacity is never reset by reads.
- Gas reports use a dedicated Vapi blocking pre-execution safety message before the backend write and interrupt intake; no heat in reported cold or no cooling with vulnerability creates an early priority record. Urgent cases do not enter routine slot booking. Saved priority/follow-up records are not notifications.
- Membership is property-specific, cached for at most five minutes and through the current New York date. No plan benefits or prices are configured. Unknown membership does not block an otherwise eligible routine appointment.
- Full confirmed address, confirmed callback, contact name, residential HVAC job and routine risk assessment are booking prerequisites. Equipment/access details are optional.
- Proposals expire after two minutes. A separate caller acceptance is required. Capacity and current material facts are rechecked atomically. Lost responses are reconciled with get_booking_status; retries use the same proposal. A hangup does not cancel a committed booking.
- Once the name and eligible address are confirmed for routine service, intake commits first, then checks property-specific membership within a one-second wall-clock budget. A timeout returns unknown membership without undoing intake or blocking booking. Fresh matching results are reused; corrections invalidate the cache. The active app no longer starts a membership worker or exposes start_membership_check. Legacy job tables/functions remain for historical evidence and compatibility tests.
- Deploy the inline membership change in order: apply `20261122000100_inline_membership_lookup.sql`, deploy the backend, then apply the versioned Vapi configuration. A code push alone does not migrate Supabase or update Vapi. The migration removes automatic job creation; the backend performs the bounded lookup and includes its result in the same tool response.

## Testing and evidence

```sh
./scripts/go                  # Offline tests and scenario plan; no paid requests
uv run --env-file .env python -m revin_take_home.evaluation doctor
RUN_DATABASE_TESTS=1 uv run --env-file .env --with 'psycopg[binary]' pytest -q
```

Database tests create uniquely scoped synthetic rows and remove them afterward. Run them only against the linked demo database. Direct transaction fixtures use `test_` IDs, have isolated capacity, and are excluded from the production worker. Model simulations have a separate synthetic inventory from phone calls. Vapi simulations use server-created `eval_` sessions; they cannot spoof production call IDs or originating numbers. Reports and session transcripts stay in ignored `eval-results/`.

Paid conversation simulations are **opt-in** and require `--allow-paid-tests`. See `evals/README.md`. Run-count/duration limits are not a guaranteed dollar cap. Chat tests cover model decisions and actual backend writes; they do not test STT/TTS, acoustics, barge-in or telephone routing. The final release still needs repeated critical conversations, a declared routine trial set and human phone-call review.

## Inspect and recover

Use the call ID to inspect `call_intakes`, `intake_events`, `enrichment_jobs`, `call_membership_checks`, `priority_requests`, `availability_reads`, `booking_proposals`, `booking_agreements`, `appointments`, `followup_requests`, `call_audit` and `tool_audit`. The server binds call and operation identity; model arguments cannot choose them. Tool audit stores status, elapsed time and prompt/tool fingerprint without duplicating transcripts. Vapi retains the call transcript/audio according to its configured retention settings.

`GET /health` is liveness only. `GET /version` identifies deployed code/prompt/tool versions. A healthy process does not establish database or provider availability. Audit failures are logged and do not conceal a successful transaction; an end-of-call report returns 503 if its outcome could not be saved. A missed provider report must be reconciled from provider logs and current database state.

If booking confirmation times out, inspect get_booking_status for that exact call before retrying. Do not delete a booking to fix a transcript. Corrected facts after booking create a change request; automatic cancellation/rescheduling is not implemented. No customer communication is sent by the backend.

Before a real rollout, obtain actual service areas, hours, capacity, benefits, urgent routing and staffed fallback from the customer; connect real scheduling/dispatch, establish retention/access policy, use always-on hosting, run the release evaluation set, and pilot with human oversight. Optional geocoding/weather enrichment and a custom voice runtime remain deferred.

### Contact reuse and weather urgency

The intake save can prefill an **unconfirmed** callback from authenticated caller ID.
A supplied name must match a unique phone-linked customer before the backend returns
that customer's primary (or only) service address. A shared/ambiguous match returns no
address. The caller confirms the property before membership is resolved and confirms
the callback separately. Caller corrections win; no permanent phone link is created.
`contact_prefill_events` records provenance separately from caller quotations.

NWS hourly forecasts and active alerts are fetched for representative points in New
York, Kings and Queens counties at startup and every 15 minutes, outside the voice
request path. Cache validity is 30 minutes; a forecast over six hours old, incomplete
24-hour coverage, or an unavailable alert feed cannot become a mild-weather result.
See the [NWS API documentation](https://www.weather.gov/documentation/services-web-api).
This uses the existing configured territory; it does not expand the ZIP allowlist.

`WEATHER_HOT_F=90` and `WEATHER_COLD_F=40` are configurable **dispatch policy defaults**,
not medical safety boundaries. A matching outage plus threshold or heat/cold alert in
the next 24 hours creates a priority record. Before a location is known, a regional
trigger requires the condition across all three counties. Maintenance alone does not
become urgent. Weather is rechecked before search/proposal/booking and on recovery;
a changed decision invalidates the old revision. Per-call weather evidence is retained.
This version does not assess conditions beyond the rolling 24-hour forecast horizon.

There is no routine age/medical screening. Volunteered vulnerability, cold conditions,
or an inability to safely wait still escalates. Missing/stale weather requires one
neutral comfort/waiting question; uncertainty remains uncertainty. Outdoor conditions
do not establish indoor safety. Gas handling remains the first action.

The greeting and intake use normal service language. Synthetic reservations are still
identified before commitment, and priority records do not claim dispatch/notification.
Real scheduling and dispatch integrations remain necessary before customer production.

Database tests support an isolated local `TEST_DATABASE_URL` with `RUN_DATABASE_TESTS=1`.
The weather fixture tests require this isolated URL and roll back their changes. They
must not target the hosted database. No provider simulation is needed for these tests.
