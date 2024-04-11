# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import _lt, fields, models, api

_SELECTION_TAX_TYPE = [
    ('VAT', 'Normal VAT (percent based)'),
    ('AAM', 'AAM - Personal tax exemption'),
    ('TAM', 'TAM - tax-exempt activity or tax-exempt due to being in public interest or special in nature'),
    ('KBAET', 'KBAET - intra-Community exempt supply, without new means of transport'),
    ('KBAUK', 'KBAUK - tax-exempt, intra-Community sales of new means of transport'),
    ('EAM', 'EAM - tax-exempt, extra-Community sales of goods (export of goods to a non-EU country)'),
    ('NAM', 'NAM - tax-exempt on other grounds related to international transactions'),
    ('ATK', 'ATK - Outside the scope of VAT'),
    ('EUFAD37', 'EUFAD37 - Based on section 37 of the VAT Act, a reverse charge transaction carried out in another Member State'),
    ('EUFADE', 'EUFADE - Reverse charge transaction carried out in another Member State, not subject to Section 37 of the VAT Act'),
    ('EUE', 'EUE - Non-reverse charge transaction performed in another Member State'),
    ('HO', 'HO - Transaction in a third country'),
    ('DOMESTIC_REVERSE', 'DOMESTIC_REVERSE - Domestic reverse-charge regime'),
    ('TRAVEL_AGENCY', 'TRAVEL_AGENCY - Profit-margin based regime for travel agencies'),
    ('SECOND_HAND', 'SECOND_HAND - Profit-margin based regime for second-hand sales'),
    ('ARTWORK', 'ARTWORK - Profit-margin based regime for artwork sales'),
    ('ANTIQUES', 'ANTIQUES - Profit-margin based regime for antique sales'),
    ('REFUNDABLE_VAT', 'REFUNDABLE_VAT - VAT incurred under sections 11 or 14, without an agreement from the beneficiary to reimburse VAT'),
    ('NONREFUNDABLE_VAT', 'NONREFUNDABLE_VAT - VAT incurred under sections 11 or 14, with an agreement from the beneficiary to reimburse VAT'),
    ('NO_VAT', 'VAT not applicable pursuant to section 17 of the VAT Act'),
]

_DEFAULT_TAX_REASONS = {
    'AAM': _lt('AAM Alanyi adómentes'),
    'TAM': _lt('TAM Tárgyi adómentes'),
    'KBAET': _lt('KBAET EU-ba eladás - ÁFA tv.89.§'),
    'KBAUK': _lt('KBAUK Új közlekedési eszköz EU-n belülre - ÁFA tv.89.§(2)'),
    'EAM': _lt('EAM Termékexport 3.országba - ÁFA tv.98-109.§'),
    'NAM': _lt('NAM egyéb export ügylet ÁFA tv 110-118.§'),
    'ATK': _lt('ATK ÁFA tárgyán kívüli - ÁFA tv.2-3.§'),
    'EUFAD37': _lt('EUFAD37 ÁFA tv. 37.§ (1) Fordított ÁFA másik EU-s országban'),
    'EUFADE': _lt('EUFADE Fordított ÁFA másik EU-s országban nem ÁFA tv. 37.§ (1)'),
    'EUE': _lt('EUE 2.EU-s országban teljesített eladás'),
    'HO': _lt('HO Szolgáltatás 3.országba'),
}


class AccountTax(models.Model):
    _inherit = 'account.tax'

    l10n_hu_tax_type = fields.Selection(
        _SELECTION_TAX_TYPE,
        string='(HU) VAT Tax Type',
        help='Precise identification of the VAT tax for the Hungarian authority.',
    )
    l10n_hu_tax_reason = fields.Char(
        string='(HU) VAT Tax Exemption Reason',
        help='May be used to provide support for the use of a VAT-exempt VAT tax type.',
        compute='_compute_l10n_hu_tax_reason',
        readonly=False,
    )

    @api.depends('l10n_hu_tax_type')
    def _compute_l10n_hu_tax_reason(self):
        for tax in self:
            tax.l10n_hu_tax_reason = _DEFAULT_TAX_REASONS.get(tax.l10n_hu_tax_type, False)
