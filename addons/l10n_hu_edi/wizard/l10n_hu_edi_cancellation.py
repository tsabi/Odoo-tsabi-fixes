# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields
from odoo.exceptions import UserError

import time


class L10nHuEdiCancellation(models.TransientModel):
    _name = 'l10n_hu_edi.cancellation'
    _description = 'Technical Annulment Wizard'

    invoice_id = fields.Many2one(
        comodel_name='account.move',
        string='Invoice to cancel',
    )
    code = fields.Selection(
        selection=[
            ('ERRATIC_DATA', 'ERRATIC_DATA - Erroneous data'),
            ('ERRATIC_INVOICE_NUMBER', 'ERRATIC_INVOICE_NUMBER - Erroneous invoice number'),
            ('ERRATIC_INVOICE_ISSUE_DATE', 'ERRATIC_INVOICE_ISSUE_DATE - Erroneous issue date'),
        ],
        string='Annulment Code',
        required=True,
    )
    reason = fields.Char(
        string='Annulment Reason',
        required=True,
    )

    def button_request_cancel(self):
        self.invoice_id._l10n_hu_edi_request_cancel(self.code, self.reason)

        if 'query_status' in self.invoice_id._l10n_hu_edi_get_valid_actions():
            time.sleep(2)
            self.invoice_id._l10n_hu_edi_query_status()

        formatted_message = self.env['account.move.send']._format_error_html(self.invoice_id.l10n_hu_edi_messages)
        self.invoice_id.with_context(no_new_invoice=True).message_post(body=formatted_message)

        if self.invoice_id.l10n_hu_edi_messages.get('blocking_level') == 'error':
            raise UserError(self.env['account.move.send']._format_error_text(self.invoice_id.l10n_hu_edi_messages))
