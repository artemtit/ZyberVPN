-- Add percentage discount support to promo_codes.
-- discount_percent = 0 → existing behaviour (promo gives free days)
-- discount_percent > 0, days = 0 → purchase discount code (reduces payment price by N%)

ALTER TABLE public.promo_codes
    ADD COLUMN IF NOT EXISTS discount_percent INT NOT NULL DEFAULT 0;
