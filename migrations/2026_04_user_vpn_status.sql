-- Run in Supabase SQL editor

-- ── Status column ─────────────────────────────────────────────────────────────
-- Default 'ready' so every existing row is immediately readable without migration.

alter table public.user_vpn
  add column if not exists status text not null default 'ready'
  constraint user_vpn_status_check check (status in ('creating', 'ready', 'failed'));

-- ── Atomic claim function ─────────────────────────────────────────────────────
-- Returns:
--   'claimed'  — caller owns the creation slot (inserted or recovered from 'failed')
--   'creating' — another process is already creating VPN for this user
--   'ready'    — configs already exist; caller should read them and return

create or replace function public.claim_user_vpn_creating(p_user_id bigint)
returns text
language plpgsql

security definer
as $$
declare
  v_rows int;
  v_status text;
begin
  -- Case 1: no row yet — insert a skeleton row and claim it.
  insert into public.user_vpn
    (user_id, status, server_id, reality_uuid, ws_uuid, reality_config, ws_config)
  values
    (p_user_id, 'creating', 0, '', '', '', '')
  on conflict (user_id) do nothing;

  get diagnostics v_rows = row_count;
  if v_rows > 0 then
    return 'claimed';
  end if;

  -- Case 2: row exists in 'failed' state — allow retry.
  update public.user_vpn
    set status = 'creating', updated_at = now()
  where user_id = p_user_id
    and status = 'failed';

  get diagnostics v_rows = row_count;
  if v_rows > 0 then
    return 'claimed';
  end if;

  -- Case 3: row is 'creating' or 'ready' — return actual status to caller.
  select status into v_status
  from public.user_vpn
  where user_id = p_user_id;

  return coalesce(v_status, 'creating');
end;
$$;
