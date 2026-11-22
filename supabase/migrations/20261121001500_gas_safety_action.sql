-- Vapi speaks this action's fixed blocking safety message before invoking this RPC.
-- A hazard report is monotonic new evidence: serialize on current state rather
-- than let a stale model revision delay recording it.
create function public.report_gas_emergency(p_call_id text,p_operation_id text,p_caller_quote text) returns jsonb
language plpgsql security definer set search_path='' as $$
declare revision integer; prior_revision integer;
begin
 if nullif(trim(p_call_id),'') is null or nullif(trim(p_operation_id),'') is null then raise exception 'Invalid call scope'; end if;
 insert into public.call_intakes(call_id) values(p_call_id) on conflict(call_id) do nothing;
 select i.revision into revision from public.call_intakes i where call_id=p_call_id for update;
 select expected_revision into prior_revision from public.intake_events where call_id=p_call_id and operation_id=p_operation_id;
 return public.update_call_intake(p_call_id,p_operation_id,null,coalesce(prior_revision,revision),
  '{"gas_smell_reported":true}',p_caller_quote,null,null);
end;
$$;
revoke all on function public.report_gas_emergency(text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.report_gas_emergency(text,text,text) to service_role;
