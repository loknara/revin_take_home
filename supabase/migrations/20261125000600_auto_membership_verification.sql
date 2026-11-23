-- Round 3 finding R11: a caller who says they're on a maintenance plan that the lookup
-- can't confirm gets a membership_verification follow-up automatically, once.
-- In testing the claim came before the lookup said "expired", and the agent never went
-- back to record the follow-up. Whichever arrives second (the claim or a non-active
-- lookup) now records it. Emergency calls are left alone.

create function public.ensure_membership_followup(p_call_id text) returns void
language plpgsql security definer set search_path='' as $$
declare s public.call_intakes%rowtype; latest jsonb;
begin
 select * into s from public.call_intakes where call_id=p_call_id;
 if not found or coalesce(s.details->>'membership_claim','')<>'true'
    or s.triage->>'level'='emergency' then return; end if;
 select result into latest from public.call_membership_checks
  where call_id=p_call_id order by intake_revision desc, checked_at desc limit 1;
 if latest is null or coalesce(latest->>'membership_status','unknown')='active' then return; end if;
 insert into public.followup_requests(call_id,reasons,details,caller_quote)
 values(p_call_id,'["membership_verification"]'::jsonb,s.details,
        'Caller said they have a maintenance plan; the lookup did not confirm it (recorded automatically).')
 on conflict(call_id) do update set
  reasons=public.followup_requests.reasons||'["membership_verification"]'::jsonb,
  details=excluded.details,updated_at=clock_timestamp()
  where not public.followup_requests.reasons ? 'membership_verification';
end;
$$;
revoke all on function public.ensure_membership_followup(text) from public,anon,authenticated,service_role;

create function public.membership_followup_trigger() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 perform public.ensure_membership_followup(new.call_id);
 return null;
end;
$$;
revoke all on function public.membership_followup_trigger() from public,anon,authenticated,service_role;

create trigger membership_followup_after_check
 after insert or update on public.call_membership_checks
 for each row execute function public.membership_followup_trigger();

create trigger membership_followup_after_claim
 after update of details on public.call_intakes
 for each row
 when (new.details->>'membership_claim'='true' and (old.details->>'membership_claim') is distinct from 'true')
 execute function public.membership_followup_trigger();
