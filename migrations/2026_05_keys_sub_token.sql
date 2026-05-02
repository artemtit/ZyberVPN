-- Per-key subscription tokens
ALTER TABLE public.keys ADD COLUMN IF NOT EXISTS sub_token text UNIQUE;
CREATE UNIQUE INDEX IF NOT EXISTS idx_keys_sub_token
  ON public.keys(sub_token) WHERE sub_token IS NOT NULL;
