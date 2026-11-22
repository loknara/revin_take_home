-- End-of-call webhooks may race a tool still committing. Refresh the ended
-- call's outcome after business writes; disconnect never cancels a booking.
alter function public.call_disposition(text) volatile;
create function public.refresh_ended_call_disposition() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 update public.call_audit set final_disposition=public.call_disposition(new.call_id)
 where call_id=new.call_id and ended_at is not null;
 return new;
end;
$$;
create trigger refresh_ended_disposition after insert or update on public.call_intakes
 for each row execute function public.refresh_ended_call_disposition();
create trigger refresh_ended_disposition after insert or update on public.appointments
 for each row execute function public.refresh_ended_call_disposition();
create trigger refresh_ended_disposition after insert or update on public.followup_requests
 for each row execute function public.refresh_ended_call_disposition();
create trigger refresh_ended_disposition after insert or update on public.priority_requests
 for each row execute function public.refresh_ended_call_disposition();
revoke all on function public.refresh_ended_call_disposition() from public,anon,authenticated,service_role;
