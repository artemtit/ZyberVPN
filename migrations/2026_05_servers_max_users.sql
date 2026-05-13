-- Run in Supabase SQL editor
ALTER TABLE servers ADD COLUMN IF NOT EXISTS max_users integer DEFAULT 0;
UPDATE servers SET max_users = 100 WHERE id IN (1, 2);
