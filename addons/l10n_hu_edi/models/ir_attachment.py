# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, api, _
from odoo.exceptions import UserError


class IrAttachment(models.Model):
    _inherit = 'ir.attachment'

    @api.ondelete(at_uninstall=False)
    def _unlink_except_active_invoices(self):
        # Prevent unlinking of an invoice's main PDF and EDI attachment if the invoice XML was sent in production mode.
        if any(
            att.res_model == 'account.move'
            and att.res_field in ['invoice_pdf_report_file', 'l10n_hu_edi_attachment']
            and (move := self.env['account.move'].browse(att.res_id).exists())
            and move.l10n_hu_edi_state not in [False, 'rejected', 'cancelled']
            and move.l10n_hu_edi_server_mode == 'production'
            for att in self
        ):
            raise UserError(_('Cannot delete a document once the invoice has been sent to NAV!'))
