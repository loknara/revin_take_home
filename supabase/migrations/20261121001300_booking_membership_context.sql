-- A completed background lookup must reach the model even when it finished
-- after an early refresh. Booking outcome carries the exact membership used.
alter function public.get_booking_status(text) rename to booking_status_record;
create function public.get_booking_status(p_call_id text) returns jsonb
language sql security definer set search_path='' as $$
 select public.booking_status_record(p_call_id)||jsonb_build_object('membership',
   coalesce((select membership_snapshot from public.appointments where call_id=p_call_id),public.current_call_membership(p_call_id)));
$$;
revoke all on function public.booking_status_record(text),public.get_booking_status(text) from public,anon,authenticated,service_role;
grant execute on function public.get_booking_status(text) to service_role;
