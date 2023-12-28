# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, api, _
from odoo.exceptions import UserError


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.ondelete(at_uninstall=False)
    def _unlink_except_active_transactions(self):
        # Prevent unlinking of an invoice's main PDF if there is an active transaction in production mode.
        if any(
            att.res_model == 'account.move'
            and att.res_field == 'invoice_pdf_report_file'
            and (move := self.env['account.move'].browse(att.res_id).exists())
            and move.l10n_hu_edi_active_transaction_id
            and move.l10n_hu_edi_credentials_mode == 'production'
            for att in self
        ):
            raise UserError(_('Cannot delete a PDF once the invoice has been sent to NAV!'))

        # Prevent unlinking an attachment of a an active transaction in production mode.
        elif any(
            att.res_model == 'l10n_hu_edi.transaction'
            and att.res_field == 'attachment_file'
            and (transaction := self.env['l10n_hu_edi.transaction'].browse(att.res_id).exists())
            and transaction.is_active
            and transaction.credentials_mode == 'production'
            for att in self
        ):
            raise UserError(_('Cannot delete the XML of an active transaction!'))
