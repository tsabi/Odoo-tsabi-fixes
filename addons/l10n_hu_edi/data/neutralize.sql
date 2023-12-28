-- Disable production mode for Hungary EDI
UPDATE l10n_hu_nav_credentials
   SET is_active = 'f' WHERE mode = 'production';
