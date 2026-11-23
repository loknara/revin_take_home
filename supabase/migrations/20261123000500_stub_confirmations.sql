-- Explicit stub receipts exercise preference/consent without contacting a provider.
alter table public.appointment_notifications drop constraint appointment_notifications_status_check;
alter table public.appointment_notifications add constraint appointment_notifications_status_check check(status in (
 'awaiting_confirmation','declined','queued','blocked_unconfigured','sending','submitted','delivered','failed','delivery_unknown','suppressed_test','simulated'));
alter function public.confirm_notification(text,uuid,text,text,boolean) rename to confirm_notification_before_stub;
create function public.confirm_notification(p_call_id text,p_confirmation_token uuid,p_decision text,p_caller_quote text,
 p_sender_ready boolean default false,p_stub boolean default false) returns jsonb
language plpgsql security definer set search_path='' as $$
declare r jsonb; n public.appointment_notifications%rowtype;
begin
 r:=public.confirm_notification_before_stub(p_call_id,p_confirmation_token,p_decision,p_caller_quote,
   case when p_stub then false else p_sender_ready end);
 if p_stub and p_decision='accepted' and r->>'status' in ('blocked_unconfigured','suppressed_test','queued','simulated') then
  update public.appointment_notifications set status='simulated',provider_name='stub',provider_id=null,
   updated_at=clock_timestamp() where call_id=p_call_id and confirmation_token=p_confirmation_token
   and status in ('blocked_unconfigured','suppressed_test','queued','simulated') returning * into n;
  if found then return public.notification_summary(n)||'{"delivery_mode":"stub","external_message_sent":false}'::jsonb; end if;
 end if;
 return r;
end;
$$;
revoke all on function public.confirm_notification_before_stub(text,uuid,text,text,boolean),
 public.confirm_notification(text,uuid,text,text,boolean,boolean) from public,anon,authenticated,service_role;
grant execute on function public.confirm_notification(text,uuid,text,text,boolean,boolean) to service_role;
