-- Run in Supabase SQL editor after 2026_04_mvp_supabase.sql

-- ── Row Level Security ────────────────────────────────────────────────────────
-- The backend uses the service_role key, which bypasses RLS in Supabase.
-- Enabling RLS blocks the anon / authenticated roles from direct table access.

alter table public.users         enable row level security;
alter table public.servers       enable row level security;
alter table public.user_vpn      enable row level security;
alter table public.keys          enable row level security;
alter table public.payments      enable row level security;
alter table public.subscriptions enable row level security;
alter table public.idempotency_keys enable row level security;

-- Explicit restrictive deny for anon — belt-and-suspenders, no policy = no
-- access when RLS is on, but this makes the intent unambiguous.
create policy "anon_deny_users"          on public.users          as restrictive for all to anon using (false);
create policy "anon_deny_servers"        on public.servers        as restrictive for all to anon using (false);
create policy "anon_deny_user_vpn"       on public.user_vpn       as restrictive for all to anon using (false);
create policy "anon_deny_keys"           on public.keys           as restrictive for all to anon using (false);
create policy "anon_deny_payments"       on public.payments       as restrictive for all to anon using (false);
create policy "anon_deny_subscriptions"  on public.subscriptions  as restrictive for all to anon using (false);
create policy "anon_deny_idempotency"    on public.idempotency_keys as restrictive for all to anon using (false);

-- ── Atomic balance increment RPC ─────────────────────────────────────────────
-- Called by users.add_balance() to avoid read-modify-write race conditions.

create or replace function public.increment_user_balance(p_tg_id bigint, p_amount int)
returns void
language sql
security definer
as $$
  update public.users
  set balance = balance + p_amount
  where tg_id = p_tg_id;
$$;

