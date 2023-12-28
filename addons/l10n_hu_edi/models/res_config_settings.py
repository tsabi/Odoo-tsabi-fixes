# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    l10n_hu_edi_credentials_ids = fields.One2many(
        related='company_id.l10n_hu_edi_credentials_ids',
        readonly=False,
    )
    l10n_hu_tax_regime = fields.Selection(
        related='company_id.l10n_hu_tax_regime',
        readonly=False,
    )
