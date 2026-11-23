-- Access details given after booking reach the booked visit (live-call feedback, 2026-11-23).
-- The agent now asks, once a visit is booked, who will let the technician in and anything
-- they should know (doorman, parking, pets). Those fields don't count as a booking change
-- (booking_snapshot ignores them), so they're copied onto the visit's snapshot, which is
-- what the technician's job reads.

create function public.sync_visit_access_details() returns trigger
language plpgsql security definer set search_path='' as $$
begin
 update public.appointments a
    set intake_snapshot = a.intake_snapshot || jsonb_strip_nulls(jsonb_build_object(
          'access_notes', new.details->'access_notes',
          'onsite_contact', new.details->'onsite_contact',
          'equipment_notes', new.details->'equipment_notes'))
  where a.call_id = new.call_id;
 return null;
end;
$$;
revoke all on function public.sync_visit_access_details() from public,anon,authenticated,service_role;

create trigger sync_visit_access_details
 after update of details on public.call_intakes
 for each row
 when (old.details->'access_notes' is distinct from new.details->'access_notes'
    or old.details->'onsite_contact' is distinct from new.details->'onsite_contact'
    or old.details->'equipment_notes' is distinct from new.details->'equipment_notes')
 execute function public.sync_visit_access_details();
