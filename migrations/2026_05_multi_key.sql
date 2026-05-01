-- Run in Supabase SQL editor

ALTER TABLE public.keys
  ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS traffic_limit_gb INT NOT NULL DEFAULT 60,
  ADD COLUMN IF NOT EXISTS device_limit INT NOT NULL DEFAULT 5,
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

ALTER TABLE public.payments
  ADD COLUMN IF NOT EXISTS purchase_type TEXT NOT NULL DEFAULT 'new',
  ADD COLUMN IF NOT EXISTS renew_key_id BIGINT NULL;

ALTER TABLE public.user_vpn DROP CONSTRAINT IF EXISTS user_vpn_user_id_key;
DROP INDEX IF EXISTS idx_user_vpn_user_id;
ALTER TABLE public.user_vpn ADD COLUMN IF NOT EXISTS key_id BIGINT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_user_vpn_user_key
  ON public.user_vpn(user_id, key_id) NULLS NOT DISTINCT;

CREATE OR REPLACE FUNCTION claim_user_vpn_creating(
  p_user_id BIGINT,
  p_key_id BIGINT DEFAULT NULL
) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
  v_status TEXT;
BEGIN
  -- Try to find existing row
  SELECT status INTO v_status
  FROM public.user_vpn
  WHERE user_id = p_user_id
    AND (key_id = p_key_id OR (key_id IS NULL AND p_key_id IS NULL))
  LIMIT 1
  FOR UPDATE SKIP LOCKED;

  IF v_status = 'ready' THEN
    RETURN 'ready';
  END IF;

  IF v_status = 'creating' THEN
    RETURN 'creating';
  END IF;

  -- No row or failed/limit_exceeded — insert or update to 'creating'
  INSERT INTO public.user_vpn (user_id, key_id, server_id, reality_uuid,
    ws_uuid, reality_config, ws_config, status, created_at, updated_at)
  VALUES (p_user_id, p_key_id, 0, '', '', '', '', 'creating',
    NOW(), NOW())
  ON CONFLICT (user_id, key_id) DO UPDATE
    SET status = 'creating', updated_at = NOW()
    WHERE user_vpn.status IN ('failed', 'limit_exceeded');

  RETURN 'claimed';
END;
$$;
