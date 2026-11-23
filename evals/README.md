# Repeatable Summit Air checks

`./scripts/go` runs offline tests and prints the conversation-test plan. It does not start chat, voice, or telephone calls. The current instruction is **setup only: do not run paid tests until the user explicitly authorizes a batch**.

This harness covers intake, urgency, membership, demo coverage and booking. Membership now returns inline with intake saves; background conversation overlap is no longer part of the normal flow.

## Commands

Run from the repository root; `uv` must be on PATH (or set `UV_BIN`).

```sh
./scripts/go
./scripts/go plan --case active_member --case gas_emergency --profile calm --profile noisy
./scripts/go doctor
```

`doctor` waits up to five minutes for the automatic Render deployment, polling every five seconds. It checks the test bridge and configured live tools; it starts no simulations. It does not ask the user to deploy merely because the first probe sees the old build.

After explicit authorization for paid tests, a small example batch would be:

```sh
./scripts/go run --case active_member --profile calm --mode chat --max-runs 1 --max-seconds 180 --allow-paid-tests
```

Then rerun selected successful chat scenarios in voice mode. `--case` and `--profile` can be repeated. Maximum 20 sequential conversations per invocation, each capped at 30–300 seconds; default is one 180-second conversation. Provider queuing/evaluation has a separate polling deadline. Stop at the first failure. These are run/duration limits, **not a guaranteed dollar cap**: reported target-call cost may omit tester, evaluator and other charges. A 402/billing error stops the batch. Never automatically retry a start request with an uncertain outcome.

## What the loop does

1. Run deterministic backend/unit tests.
2. Wait for the deployed evaluation API and inspect the current model, voice, transcriber and tool credentials.
3. Create a fresh authenticated synthetic session for one conversation. The session supplies fixture identity, not the simulated caller or language model.
4. Compile a transient Vapi target using the versioned local prompt/tool schemas and current voice configuration. Experimental changes never patch the live phone assistant. Only the active workflow tools are allowed; they point to the isolated evaluation session route.
5. Run an AI caller over `vapi.webchat` or `vapi.websocket`. Scenario facts and caller personality are separate. The caller can end the conversation; no real telephone number is dialed.
6. Fetch provider evaluations, transcript/recording references and the exact session's Supabase evidence. Require both conversation judgment and deterministic database assertions. Empty, skipped or unavailable evidence cannot pass.
7. Save JSON and HTML reports under ignored `eval-results/`, close the session, and stop on failure. Closed/expired sessions reject further writes. Test records use `eval_<session UUID>` and remain available for diagnosis; they are never real customer appointments.
8. Codex reads the report, diagnoses the failure and makes a focused fix. Run affected cases plus the critical regression set again, within the separately authorized batch limits. Do not weaken the rubric or expected records to hide failures. A later authorized “go” can invoke this workflow; `/go` is not a built-in endless autonomous service.

Code changes and regression reports are reviewable independently. Do not automatically promote candidate prompts/settings to the live assistant merely because an AI judge passes them. Promotion follows the user's requested implementation scope and the agreed checks. If a provider, billing or deployment prerequisite blocks a run, report the concrete blocker instead of retrying indefinitely.

## Isolation and identity

The production `/vapi/tools` endpoint still requires inbound phone metadata. Simulations have different metadata, and overriding `customer.number` in simulation variables does not change the server-message caller. The `/evaluation/sessions/{id}/tools` bridge instead uses a server-created, expiring fixture session and the same intake/membership services.

Creating sessions, invoking their tools, reading evidence and closing sessions all require the existing webhook credential. The only fixture identities are synthetic Maya, the shared synthetic number, an unknown synthetic number, or withheld caller ID. Model arguments cannot choose real call IDs or account numbers. Production tool URLs and phone routing are not changed by the runner. This tests the real domain integration but does **not** independently test inbound telephony routing.

## Coverage and evidence

The suite contains active, inactive, expired/disputed and absent plans; alternate/shared/withheld numbers; unknown customers; street/unit corrections; commercial requests; routine cooling; no cooling with vulnerability; no heat in cold conditions; and gas reports. Four profiles use Elliot/Clara voices with calm, hesitant, impatient and city-noise conditions. Voice IDs are from Vapi's documented active catalog; their actual behavior still requires an authorized voice run.

Database checks inspect the final intake, matching membership input snapshot, priority level/delivery state, operation deduplication, separate address confirmation and risk-before-address event order. Conversation judgment checks safety guidance, scope, corrections, claims and confirmations. An AI judge can be wrong: review failed cases and a sample of passing recordings. Small samples are not production reliability evidence.

Use occasional controlled phone calls for the carrier/inbound-number path and representative human speech. Synthetic voices do not cover every accent, real interruption, noisy microphone or degraded connection. Voice timing measurements and recordings should be reviewed separately from a text-based safety judgment. The existing gas-path “Just a sec” behavior is a known refinement the safety scenario is intended to catch.

## Configuration and secrets

The ignored `.env` needs `VAPI_API_KEY`, `VAPI_ASSISTANT_ID` and `VAPI_WEBHOOK_SECRET` for hosted runs. The backend retains its existing Supabase settings; no new Render environment variables are needed. Optional `EVAL_BACKEND_URL` selects the backend, which must match the live tools during preflight.

Do not commit transcripts, credentials, generated payloads or results. Reports have private local file permissions. `report.json` records source commit, prompt hash, settings, requested limits, provider run IDs and evidence. `active-run.json` is retained when cancellation is uncertain; inspect that run before authorizing another attempt.

Before a session/CI rollout, separately choose record retention and a cleanup policy for the clearly marked evaluation records. No broad table deletion is part of this runner.

## Provider contracts

Verified against Vapi's public OpenAPI and guides on November 21, 2026:

- [Testing layers](https://docs.vapi.ai/test/voice-testing)
- [Simulation API quickstart](https://docs.vapi.ai/observability/simulations-quickstart)
- [Simulation metadata and mocks](https://docs.vapi.ai/observability/simulations-advanced)
- [Caller personalities and noise](https://docs.vapi.ai/observability/simulations-configure-ai-tester)
- [Active voice catalog](https://docs.vapi.ai/providers/voice/vapi-voices)
- [Public OpenAPI](https://api.vapi.ai/api-json)

No paid run has been performed during harness setup. Live simulator payload delivery, audio quality and provider billing availability remain unverified until an authorized smoke run.

## Completed workflow coverage

The suite now includes routine bookings, corrected addresses/units, property-specific membership, differing callbacks, commercial/non-HVAC/out-of-area rejection, landlord intake, declined offers, manipulation attempts and gas emerging during booking. `inline_membership_booking` checks that membership reaches the model through the intake response without creating background jobs. Lookup timeout/failure behavior is covered by deterministic backend tests. The earlier artificially delayed background scenario and its historical reports are not evidence for the current inline flow. Failed trials remain failed in their reports, even if later code fixes them.

Backend transaction checks separately cover last-place races, expired/stale offers, corrections that return to their original value, idempotent replay after a lost response, post-commit correction/hangup, unknown storage outcomes, worker lease recovery, expired reads and stale property results. These checks are deterministic; conversation assertions also inspect tool ordering and the actual persisted appointment/agreement. A provider judge passing is insufficient.

Chat simulation message timestamps may be synthetic sequential values. Do not use those values to claim STT/TTS latency or time to spoken safety guidance. `tool_audit.elapsed_ms` measures backend operation time only. Voice-quality/release gates still need authorized voice runs and human phone review. Voice profiles are configured but are not exercised by chat transport.

The simulation endpoint is available only to a holder of the backend webhook credential. It uses fixed synthetic phone fixtures and server-generated call IDs. Simulations use a separate synthetic inventory from phone calls; each direct database fixture also has its own inventory. Three early chat bookings from before inventory isolation remain in the original demo inventory as historical evidence. No real customer calendar exists. Do not run this harness against a real dispatch system without a separate isolated test tenant.
