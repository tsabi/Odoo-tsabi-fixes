-- Disable production mode for Hungary EDI
UPDATE res_company
   SET l10n_hu_edi_server_mode = 'test' WHERE l10n_hu_edi_server_mode = 'production';
