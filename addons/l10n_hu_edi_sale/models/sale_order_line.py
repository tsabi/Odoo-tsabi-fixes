# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def _prepare_invoice_line(self, **optional_values):
        """ When deducting a down payment, provide a reference to the advance invoice. """
        res = super()._prepare_invoice_line(**optional_values)

        if self.order_id.company_id.country_code == 'HU' and self.is_downpayment:
            advance_invoices = self.invoice_lines.filtered(lambda line: line.is_downpayment).mapped('move_id') \
                                   .filtered(lambda m: m.state == 'posted')

            if advance_invoices:
                res['name'] = res['name'] + ' - ' + ', '.join([move.name for move in advance_invoices])

        return res
