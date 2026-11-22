-- Cache freshness is enforced on every read, not merely on job display.
create or replace function public.current_call_membership(p_call_id text) returns jsonb
language sql volatile security definer set search_path='' as $$
 select coalesce((select checks.result from public.call_membership_checks checks
 join public.call_intakes intake using(call_id)
 where checks.call_id=p_call_id and checks.input_snapshot=public.membership_input_snapshot(intake.details)
 and checks.checked_at>clock_timestamp()-interval '5 minutes'
 and (checks.checked_at at time zone 'America/New_York')::date=(clock_timestamp() at time zone 'America/New_York')::date
 order by checks.intake_revision desc limit 1),
 '{"status":"not_checked_for_current_details","membership_status":"unknown","verification_required":true}'::jsonb);
$$;
