-- Ordinary intake no longer queues a background membership job. The backend
-- commits this save, then performs a bounded membership RPC before returning
-- the same tool response. A membership timeout cannot roll back the intake.
-- Keep legacy job functions/tables for historical evidence and compatibility.
create or replace function public.update_call_intake(
 p_call_id text,p_operation_id text,p_originating_number text,
 p_expected_revision integer,p_changes jsonb,p_caller_quote text,
 p_confirmation_address text default null,p_confirmation_callback text default null
) returns jsonb
language sql security definer set search_path='' as $$
 select public.save_call_intake(
   p_call_id,p_operation_id,p_originating_number,p_expected_revision,
   p_changes,p_caller_quote,p_confirmation_address,p_confirmation_callback
 );
$$;
revoke all on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) from public,anon,authenticated,service_role;
grant execute on function public.update_call_intake(text,text,text,integer,jsonb,text,text,text) to service_role;
