ALTER TABLE public.servers
  ADD COLUMN IF NOT EXISTS server_role text NOT NULL DEFAULT 'direct',
  ADD COLUMN IF NOT EXISTS upstream_id bigint NULL,
  ADD COLUMN IF NOT EXISTS gateway_apply_status text NOT NULL DEFAULT 'idle',
  ADD COLUMN IF NOT EXISTS gateway_apply_error text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS gateway_applied_at timestamptz NULL,
  ADD COLUMN IF NOT EXISTS gateway_config_path text NOT NULL DEFAULT '',
  ADD COLUMN IF NOT EXISTS gateway_service_name text NOT NULL DEFAULT '';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'servers_server_role_check'
  ) THEN
    ALTER TABLE public.servers
      ADD CONSTRAINT servers_server_role_check
      CHECK (server_role IN ('direct', 'gateway'));
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.external_upstreams (
  id bigint generated always as identity primary key,
  name text NOT NULL,
  raw_json text NULL,
  config_hash text NOT NULL DEFAULT '',
  is_active boolean NOT NULL DEFAULT false,
  source_kind text NOT NULL DEFAULT 'db',
  source_path text NULL,
  default_route_mode text NOT NULL DEFAULT 'strict',
  validation_status text NOT NULL DEFAULT 'unknown',
  validation_error text NOT NULL DEFAULT '',
  last_applied_at timestamptz NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'external_upstreams_source_kind_check'
  ) THEN
    ALTER TABLE public.external_upstreams
      ADD CONSTRAINT external_upstreams_source_kind_check
      CHECK (source_kind IN ('db', 'file'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_external_upstreams_active
  ON public.external_upstreams(is_active)
  WHERE is_active = true;
