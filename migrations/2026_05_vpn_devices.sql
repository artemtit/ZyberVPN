-- Run in Supabase SQL editor

create table if not exists public.vpn_devices (
  id bigint generated always as identity primary key,
  server_id bigint not null,
  user_id bigint not null,
  key_id bigint not null,
  email text not null,
  device_hash text not null,
  user_agent text,
  ip text,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null,
  updated_at timestamptz not null default now()
);

create unique index if not exists idx_vpn_devices_server_key_hash
  on public.vpn_devices(server_id, key_id, device_hash);

create index if not exists idx_vpn_devices_recent_key
  on public.vpn_devices(key_id, last_seen desc);

create index if not exists idx_vpn_devices_recent_user_key
  on public.vpn_devices(user_id, key_id, last_seen desc);
