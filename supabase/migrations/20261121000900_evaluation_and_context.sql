create function public.configure_evaluation_delay(p_session_id uuid,p_delay_ms integer) returns void
language plpgsql security definer set search_path='' as $$
begin
 if p_delay_ms is null or p_delay_ms not between 0 and 8000 then raise exception 'Invalid evaluation delay'; end if;
 update public.evaluation_sessions set lookup_delay_ms=p_delay_ms where id=p_session_id and closed_at is null and expires_at>clock_timestamp();
 if not found then raise exception 'Unknown open evaluation session'; end if;
end;
$$;
revoke all on function public.configure_evaluation_delay(uuid,integer) from public,anon,authenticated,service_role;
grant execute on function public.configure_evaluation_delay(uuid,integer) to service_role;

-- Every explicit refresh includes actual booking/follow-up state, not an assumed not-booked flag.
alter function public.refresh_call_context(text) rename to refresh_intake_context;
create function public.refresh_call_context(p_call_id text) returns jsonb
language sql security definer set search_path='' as $$
 select public.refresh_intake_context(p_call_id)||jsonb_build_object('outcome',public.call_disposition(p_call_id));
$$;
revoke all on function public.refresh_intake_context(text),public.refresh_call_context(text) from public,anon,authenticated,service_role;
grant execute on function public.refresh_call_context(text) to service_role;

-- Full address dependency: city/state corrections invalidate membership too.
create or replace function public.membership_input_snapshot(details jsonb) returns jsonb
language sql immutable set search_path='' as $$
 select jsonb_build_object('name',details->'caller_name','street',details->'service_address',
 'unit',details->'service_unit','zip',details->'service_zip','city',details->'service_city','state',details->'service_state',
 'confirmed',details->'address_confirmed','property',details->'property_type','claim',details->'membership_claim');
$$;
