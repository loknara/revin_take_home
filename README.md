# Summit Air demo

Inbound residential HVAC service coordination through Vapi, FastAPI on Render, and Supabase. Bookings reserve capacity and assign work orders against a clearly synthetic technician roster. SMS/email confirmations use an optional transactional outbox and require configured senders. No actual human technician, outbound callback, or emergency-service contact is connected.

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

Versioned prompt: `prompts/summit-air-system.txt`. Tool definitions: `tools/`. The active workflow uses update_intake, refresh_context, search_slots, propose_booking, confirm_booking, get_booking_status, request_followup, report_gas_emergency, prepare_confirmation, confirm_notification and get_confirmation_status. Membership is returned in the intake response; refresh_context is for recovery, not routine membership polling. `resolve_membership` remains available in the backend for compatibility but is not attached to the completed agent.

Use `python -m revin_take_home.configure_vapi` to inspect the deployment plan; `--apply --phone-id ID` requires a deployed matching backend and updates the existing assistant with newly created versioned tools. It preserves STT/TTS/model settings, saves a private rollback snapshot, installs an authenticated end-of-call report endpoint, and configures a provider-hosted message for assistant-request failures. It starts no calls. See the command's `--help` for rollback.

Render Free can sleep. An assistant-request cold start can exceed Vapi's fixed deadline. The provider-hosted failure message is a graceful failure, **not an availability fix**. Warm the service and verify `/version` before the demo; choose always-on hosting for a production pilot. Do not run an endless keep-awake job to disguise this limit.

## Demo operating policy

- Residential HVAC repair/maintenance only. A landlord's residential property is eligible.
- Explicit ZIP allowlist: 10023, 10024, 10025, 10075 (New York County); 11201 (Kings); 11375 (Queens). This is partial coverage for a fictional demo, not confirmed customer policy.
- Seeded routine capacity: Monday–Saturday, 9am–noon and 1pm–4pm, America/New_York; up to 40 places per zone/job type/window, constrained by actual eligible technician availability, from tomorrow through the next 14 days. Existing capacity is never reset by reads.
- Gas reports use a dedicated Vapi blocking pre-execution safety message before the backend write and interrupt intake; no heat in reported cold or no cooling with vulnerability creates an early priority record. Priority cases can book earlier on-call windows after essentials are confirmed; gas emergencies cannot book. Saved priority/follow-up records are not notifications.
- Membership is property-specific, cached for at most five minutes and through the current New York date. No plan benefits or prices are configured. Unknown membership does not block an otherwise eligible routine appointment.
- Full confirmed address, confirmed callback, contact name, residential HVAC job and a resolved routine/priority risk assessment are booking prerequisites. Equipment/access details are optional.
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

The authorized customer-demo call uses normal service language. Bookings assign synthetic
technicians; outbound confirmation messages identify simulated service visits. Real staff
and technician notification/acceptance integrations remain necessary for customer production.

Database tests support an isolated local `TEST_DATABASE_URL` with `RUN_DATABASE_TESTS=1`.
The weather fixture tests require this isolated URL and roll back their changes. They
must not target the hosted database. No provider simulation is needed for these tests.

### Booking, technician assignment, and confirmations

New bookings atomically reserve a window and create an `assigned` dispatch job. The
roster contains 40 explicitly synthetic technicians, including eight on-call staff.
Scheduling checks zone, repair/maintenance skills, working hours, slot capacity and
existing assignments. A database exclusion constraint prevents overlapping work in
the same inventory, including a 30-minute travel buffer. Evaluation inventory remains
separate. Assigned does not mean a human accepted the job or is en route.

Priority repairs can use earlier two-hour on-call windows, with a minimum 30-minute
lead time. Routine slots keep their existing three-hour windows/two-hour lead time.
Gas emergencies never enter appointment scheduling. No available technician means a
fresh search or truthful priority follow-up, not an invented assignment.

After booking the agent asks whether the caller wants SMS, email, or neither. SMS uses
the confirmed callback from the appointment. Email is prepared, read back and confirmed
separately; corrections invalidate the old confirmation token. Choosing both is two
independent confirmations. A durable outbox records consent and tracks queued,
submitted, delivered, failed and uncertain outcomes. Sending does not hold up booking;
provider failure cannot cancel or duplicate an appointment. Timed-out sends are not
blindly retried. A worker that dies during a send leaves a recoverable review record,
not a second message. Delivery-status reads can retry without resending.

Customer-demo calls use natural booking language. The roster remains synthetic; actual
outbound texts/emails clearly identify the reservation as a simulated service visit.
No medical/vulnerability details enter confirmations. Automated `eval_`/`test_` calls
are suppressed in both the database queue and provider adapter.

#### Connect SMS and email delivery

No sender credentials are needed for booking/assignment. Until a sender is configured,
confirmation requests return `blocked_unconfigured`; the agent must not claim a send.

1. In Twilio, obtain an SMS-capable sender and complete its provider-required approval.
   Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, and `TWILIO_FROM_NUMBER` in Render's
   Environment page and the ignored local `.env`. The Vapi inbound number is not
   automatically an SMS sender. See [Twilio Messaging](https://www.twilio.com/docs/messaging/api/message-resource).
2. In Resend, verify the sending domain, choose a sender address, and create a key with
   sending and delivery-status read access. Set `RESEND_API_KEY` and
   `CONFIRMATION_FROM_EMAIL`. See [Resend sending](https://resend.com/docs/api-reference/emails/send-email)
   and [delivery-status reads](https://resend.com/docs/api-reference/emails/retrieve-email).
3. Redeploy Render. Make one authorized call, book, choose a channel, and inspect
   `appointment_notifications`: `submitted` means accepted by the provider, while
   `delivered` means its delivery receipt was observed. Do not share keys in chat/Git.
4. Previously blocked requests do not automatically send after setup. Start a fresh
   confirmation test; do not bulk-send historical demo reservations.

The notification worker uses the web process. Render Free can suspend an idle service;
production delivery needs an always-on worker/service. External technician CRM/mobile
integrations, rescheduling/cancellation, route optimization and the operator dashboard
remain separate increments. Do not present synthetic staff as real workers outside the
authorized role-play.


### Stub confirmation mode

`CONFIRMATION_DELIVERY_MODE=stub` is the default. Caller preference and recipient consent
still run normally, but a successful confirmation saves `status=simulated`,
`provider_name=stub`, and makes no provider request. The agent acknowledges the prepared
confirmation without claiming a text/email was delivered. This also applies to simulation
sessions. Credentials alone cannot turn delivery on: explicitly set the mode to `live`
when real delivery is wanted. `/version` reports the active mode. Historical queued sends
are blocked by the provider adapter while stub mode is active.
