-- Start independent membership work as a consequence of saved, confirmed facts.
-- The language model need not remember to schedule it at exactly the right turn.
alter function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) rename to save_call_intake;
create function public.update_call_intake(p_call_id text,p_operation_id text,p_originating_number text,
 p_expected_revision integer,p_changes jsonb,p_caller_quote text,p_confirmation_address text default null,p_confirmation_callback text default null) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r jsonb; d jsonb;
begin
 r:=public.save_call_intake(p_call_id,p_operation_id,p_originating_number,p_expected_revision,p_changes,p_caller_quote,p_confirmation_address,p_confirmation_callback);
 d:=r->'state'->'details';
 if r->>'status'='saved' and r->'state'->'triage'->>'level'='routine'
 and r->'readiness'->>'scope'='eligible' and r->'readiness'->>'coverage'='covered'
 and d->>'address_confirmed'='true' and nullif(trim(d->>'caller_name'),'') is not null then
   r:=r||jsonb_build_object('membership_lookup',public.start_membership_check(p_call_id,(r->'state'->>'revision')::integer));
 end if;
 return r;
end;
$$;
revoke all on function public.save_call_intake(text,text,text,integer,jsonb,text,text,text),public.update_call_intake(text,text,text,integer,jsonb,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) to service_role;
