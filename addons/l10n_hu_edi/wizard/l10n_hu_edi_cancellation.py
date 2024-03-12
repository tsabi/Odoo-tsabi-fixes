# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields

import time


class L10nHuEdiCancellation(models.TransientModel):
    _name = 'l10n_hu_edi.cancellation'
    _description = 'Invoice Cancellation Wizard'

    invoice_id = fields.Many2one(
        comodel_name='account.move',
        string='Invoice to cancel',
    )
    code = fields.Selection(
        selection=[
            ('ERRATIC_DATA', 'Erroneous data'),
            ('ERRATIC_INVOICE_NUMBER', 'Erroneous invoice number'),
            ('ERRATIC_INVOICE_ISSUE_DATE', 'Erroneous issue date'),
        ],
        string='Cancellation code',
        required=True,
    )
    reason = fields.Char(
        string='Cancellation Reason',
        required=True,
    )

    def button_request_cancel(self):
        self.invoice_id._l10n_hu_edi_request_cancel(self.code, self.reason)
        if self.invoice_id._l10n_hu_edi_can_process(['query_status']):
            time.sleep(2)
            self.invoice_id._l10n_hu_edi_query_status()
