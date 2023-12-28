# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, api, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import formatLang, float_round, float_repr, cleanup_xml_node, groupby
from odoo.addons.base_iban.models.res_partner_bank import normalize_iban
from odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection import format_bool

import base64
import math
from lxml import etree
import logging
import re

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    # === Technical fields === #
    l10n_hu_invoice_chain_index = fields.Integer(
        string='(HU) Invoice Chain Index',
        help='For base invoices: the length of the chain. For modification invoices: the index in the chain.',
        copy=False,
    )
    l10n_hu_edi_transaction_ids = fields.One2many(
        comodel_name='l10n_hu_edi.transaction',
        inverse_name='invoice_id',
        string='(HU) Upload Transaction History',
        copy=False,
    )
    l10n_hu_edi_active_transaction_id = fields.Many2one(
        comodel_name='l10n_hu_edi.transaction',
        string='(HU) Active Upload Transaction',
        compute='_compute_l10n_hu_edi_active_transaction_id',
        search='_search_l10n_hu_edi_active_transaction_id',
    )
    l10n_hu_edi_credentials_mode = fields.Selection(
        related='l10n_hu_edi_active_transaction_id.credentials_mode',
    )

    # === Constraints === #

    @api.constrains('l10n_hu_edi_transaction_ids', 'state')
    def _check_only_one_active_transaction(self):
        """ Enforce the constraint that posted invoices have at most one active transaction,
        and draft invoices and cancelled invoices have no active transactions.
        This means that you cannot create a transaction on an invoice if it already has an active transaction,
        and you cannot reset to draft / cancel a posted invoice if it still has an active transaction.
        """
        for move in self:
            num_active_transactions = len(move.l10n_hu_edi_transaction_ids.filtered(lambda t: t.is_active))
            if num_active_transactions > 1:
                raise ValidationError(_('Cannot create a new NAV transaction for an invoice while an existing transaction is active!'))
            if move.state in ['draft', 'cancel'] and num_active_transactions > 0:
                raise ValidationError(_('Cannot reset to draft or cancel invoice %s because an electronic document was already sent to NAV!', move.name))

    # === Computes / Getters === #

    @api.depends('l10n_hu_edi_transaction_ids')
    def _compute_l10n_hu_edi_active_transaction_id(self):
        """ A move's active transaction is the only one in a state that still has the potential to be confirmed/rejected. """
        for move in self:
            move.l10n_hu_edi_active_transaction_id = move.l10n_hu_edi_transaction_ids.filtered(lambda t: t.is_active)

    @api.model
    def _search_l10n_hu_edi_active_transaction_id(self, operator, value):
        return ['&', ('l10n_hu_edi_transaction_ids', operator, value), ('l10n_hu_edi_transaction_ids.is_active', '=', True)]

    @api.depends('l10n_hu_edi_active_transaction_id.state', 'state')
    def _compute_show_reset_to_draft_button(self):
        super()._compute_show_reset_to_draft_button()
        self.filtered(lambda m: not m.l10n_hu_edi_active_transaction_id._can_perform('abort')).show_reset_to_draft_button = False

    def _l10n_hu_get_chain_base(self):
        """ Get the base invoice of the invoice chain, or None if this is already a base invoice. """
        self.ensure_one()
        base_invoice = self
        while base_invoice.reversed_entry_id:
            base_invoice = base_invoice.reversed_entry_id
        return base_invoice if base_invoice != self else None

    def _l10n_hu_get_chain_invoices(self):
        """ Given a base invoice, get all invoices in the chain. """
        self.ensure_one()
        chain_invoices = self
        while chain_invoices != chain_invoices | chain_invoices.reversal_move_id:
            chain_invoices = chain_invoices | chain_invoices.reversal_move_id
        return chain_invoices - self

    def _l10n_hu_get_currency_rate(self):
        """ Get the invoice currency / HUF rate.

        If the company currency is HUF, we estimate this based on the invoice lines,
        using a MMSE estimator assuming random (Gaussian) rounding errors.

        Otherwise, we get the rate from the currency rates.
        """
        if self.currency_id.name == 'HUF':
            return 1
        if self.company_id.currency_id.name == 'HUF':
            squared_amount_currency = sum(line.amount_currency ** 2 for line in self.invoice_line_ids)
            squared_balance = sum(line.balance ** 2 for line in self.invoice_line_ids)
            return math.sqrt(squared_balance / squared_amount_currency)
        return self.env['res.currency']._get_conversion_rate(
            from_currency=self.currency_id,
            to_currency=self.env.ref('base.HUF'),
            company=self.company_id,
            date=self.invoice_date,
        )

    def _l10n_hu_edi_can_create_transaction(self):
        """ Determines whether a new NAV 3.0 transaction can be created for this invoice """
        self.ensure_one()
        return (
            self.country_code == 'HU'
            and self.is_sale_document()
            and self.state == 'posted'
            and not self.l10n_hu_edi_active_transaction_id
            # Ensure that invoices created before l10n_hu_edi is installed can't be sent.
            and self.l10n_hu_invoice_chain_index is not False
        )

    def _l10n_hu_edi_can_process(self):
        """ Determines whether any NAV 3.0 flow is applicable to this invoice """
        self.ensure_one()
        return (
            self._l10n_hu_edi_can_create_transaction()
            or (
                self.l10n_hu_edi_active_transaction_id
                and self.l10n_hu_edi_active_transaction_id.state not in ['confirmed', 'confirmed_warning']
            )
        )

    # === Overrides === #

    def button_draft(self):
        # EXTEND account
        self.l10n_hu_edi_active_transaction_id.filtered(lambda t: t._can_perform('abort')).abort()
        return super().button_draft()

    def action_reverse(self):
        # EXTEND account
        unconfirmed = self.filtered(lambda m: m._l10n_hu_edi_can_process())
        if unconfirmed:
            raise UserError(_(
                'Invoices %s have not yet been confirmed by NAV. Please wait for confirmation before issuing a modification invoice.',
                unconfirmed.mapped('name'))
            )
        return super().action_reverse()

    def _post(self, soft=True):
        # EXTEND account
        to_post = self.filtered(lambda move: move.date <= fields.Date.context_today(self)) if soft else self
        for move in to_post.filtered(lambda m: m.country_code == 'HU' and m.is_sale_document()):
            move._l10n_hu_edi_set_chain_index_and_line_number()
        return super()._post(soft=soft)

    def _l10n_hu_edi_set_chain_index_and_line_number(self):
        """ Set the l10n_hu_invoice_chain_index and l10n_hu_line_number fields at posting. """
        self.ensure_one()
        base_invoice = self._l10n_hu_get_chain_base()
        if base_invoice is None:
            if not self.l10n_hu_invoice_chain_index:
                # This field has a meaning only for modification invoices, however, in our implementation, we also set it
                # on base invoices as a way of controlling concurrency, to ensure that the chain sequence is unique and gap-less.
                self.l10n_hu_invoice_chain_index = 0
            next_line_number = 1
        else:
            if not self.l10n_hu_invoice_chain_index:
                base_invoice.l10n_hu_invoice_chain_index += 1
                # If two invoices of the same chain are posted simultaneously, this will trigger a serialization error,
                # ensuring sequence integrity.
                base_invoice.flush_recordset(fnames=['l10n_hu_invoice_chain_index'])
                self.l10n_hu_invoice_chain_index = base_invoice.l10n_hu_invoice_chain_index

            prev_chain_invoices = base_invoice._l10n_hu_get_chain_invoices() - self
            if prev_chain_invoices:
                last_chain_invoice = max(prev_chain_invoices, key=lambda m: m.l10n_hu_invoice_chain_index)
            else:
                last_chain_invoice = base_invoice
            next_line_number = (max(last_chain_invoice.line_ids.mapped('l10n_hu_line_number')) or 0) + 1

        # Set l10n_hu_line_number consecutively, first on product lines, then on rounding line
        for product_line in self.invoice_line_ids.filtered(lambda l: l.display_type == 'product'):
            product_line.l10n_hu_line_number = next_line_number
            next_line_number += 1
        for rounding_line in self.line_ids.filtered(lambda l: l.display_type == 'rounding'):
            rounding_line.l10n_hu_line_number = next_line_number
            next_line_number += 1

    # === EDI: Flow === #

    def _l10n_hu_edi_check_invoices(self):
        errors = []
        hu_vat_regex = re.compile(r'^\d{8}-[1-5]-\d{2}$')

        checks = {
            'company_vat_missing': {
                'records': self.company_id.filtered(lambda c: not c.vat),
                'message': _('Please set company VAT number!'),
                'action_text': _('View Company/ies'),
            },
            'company_vat_address': {
                'records': self.company_id.filtered(
                    lambda c: (
                        c.vat and not hu_vat_regex.fullmatch(c.vat)
                        or c.l10n_hu_group_vat and not hu_vat_regex.fullmatch(c.l10n_hu_group_vat)
                    )
                ),
                'message': _('Please enter the Hungarian VAT (and/or Group VAT) number in 12345678-1-12 format!'),
                'action_text': _('View Company/ies'),
            },
            'company_address_missing': {
                'records': self.company_id.filtered(lambda c: not c.country_id or not c.zip or not c.city or not c.street),
                'message': _('Please set company Country, Zip, City and Street!'),
                'action_text': _('View Company/ies'),
            },
            'company_not_huf': {
                'records': self.company_id.filtered(lambda c: c.currency_id.name != 'HUF'),
                'message': _('Please use HUF as company currency!'),
                'action_text': _('View Company/ies'),
            },
            'partner_vat_missing': {
                'records': self.partner_id.commercial_partner_id.filtered(
                    lambda p: p.is_company and not p.vat
                ),
                'message': _('Please set partner Tax ID on company partners!'),
                'action_text': _('View partner(s)'),
            },
            'partner_vat_invalid': {
                'records': self.partner_id.commercial_partner_id.filtered(
                    lambda p: (
                        p.is_company and p.country_code == 'HU'
                        and (
                            p.vat and not hu_vat_regex.fullmatch(p.vat)
                            or p.l10n_hu_group_vat and not hu_vat_regex.fullmatch(p.l10n_hu_group_vat)
                        )
                    )
                ),
                'message': _('Please enter the Hungarian VAT (and/or Group VAT) number in 12345678-1-12 format!'),
                'action_text': _('View partner(s)'),
            },
            'partner_address_missing': {
                'records': self.partner_id.commercial_partner_id.filtered(
                    lambda p: p.is_company and (not p.country_id or not p.zip or not p.city or not p.street),
                ),
                'message': _('Please set partner Country, Zip, City and Street!'),
                'action_text': _('View partner(s)'),
            },
            'invoice_date_not_today': {
                'records': self.filtered(lambda m: m.invoice_date != fields.Date.context_today(m)),
                'message': _('Please set invoice date to today!'),
                'action_text': _('View invoice(s)'),
            },
            'invoice_line_not_one_vat_tax': {
                'records': self.filtered(
                    lambda m: any(
                        len(l.tax_ids.filtered(lambda t: t.l10n_hu_tax_type)) != 1
                        for l in m.invoice_line_ids.filtered(lambda l: l.display_type == 'product')
                    )
                ),
                'message': _('Please set exactly one VAT tax on each invoice line!'),
                'action_text': _('View invoice(s)'),
            },
            'invoice_line_non_vat_taxes_misconfigured': {
                'records': self.invoice_line_ids.tax_ids.filtered(
                    lambda t: not t.l10n_hu_tax_type and (not t.price_include or not t.include_base_amount)
                ),
                'message': _("Please set any non-VAT (excise) taxes to be 'Included in Price' and 'Affects subsequent taxes'!"),
                'action_text': _('View tax(es)'),
            },
            'invoice_line_vat_taxes_misconfigured': {
                'records': self.invoice_line_ids.tax_ids.filtered(
                    lambda t: t.l10n_hu_tax_type and not t.is_base_affected
                ),
                'message': _("Please set any VAT taxes to be 'Affected by previous taxes'!"),
                'action_text': _('View tax(es)'),
            },
        }

        errors = {
            check: {
                'message': values['message'],
                'action_text': values['action_text'],
                'action': values['records']._get_records_action(name=values['action_text']),
            }
            for check, values in checks.items()
            if values['records']
        }

        if companies_missing_credentials := self.company_id.filtered(lambda c: not c.l10n_hu_edi_primary_credentials_id):
            errors['company_credentials_missing'] = {
                'message': _('Please set NAV credentials in the Accounting Settings!'),
                'action_text': _('Open Accounting Settings'),
                'action': self.env.ref('account.action_account_config').with_company(companies_missing_credentials[0])._get_action_dict(),
            }

        return errors

    def _l10n_hu_edi_create_transactions(self):
        for invoice in self:
            if not invoice.company_id.l10n_hu_edi_primary_credentials_id:
                raise UserError(_('Please set NAV credentials in the Accounting Settings!'))
            self.env['l10n_hu_edi.transaction'].create({
                'invoice_id': invoice.id,
                'credentials_id': invoice.company_id.l10n_hu_edi_primary_credentials_id.id,
                'operation': 'MODIFY' if invoice.reversed_entry_id else 'CREATE',
                'attachment_file': base64.b64encode(invoice._l10n_hu_edi_generate_xml()),
            })

    def _l10n_hu_edi_cleanup_old_transactions(self):
        for invoice in self:
            # Remove any rejected transaction except if it is the latest transaction
            invoice.l10n_hu_edi_transaction_ids[1:].filtered(lambda t: t.state == 'rejected').unlink()

    # === EDI: XML generation === #

    def _l10n_hu_edi_generate_xml(self):
        invoice_data = self.env['ir.qweb']._render(
            self._l10n_hu_edi_get_electronic_invoice_template(),
            self._l10n_hu_edi_get_invoice_values(),
        )
        return etree.tostring(cleanup_xml_node(invoice_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

    def _l10n_hu_edi_get_electronic_invoice_template(self):
        """ For feature extensibility. """
        return 'l10n_hu_edi.nav_online_invoice_xml_3_0'

    def _l10n_hu_edi_get_invoice_values(self):
        eu_country_codes = set(self.env.ref('base.europe').country_ids.mapped('code'))
        def get_vat_data(partner, force_vat=None):
            if partner.country_code == 'HU' or force_vat:
                return {
                    'tax_number': partner.l10n_hu_group_vat or (force_vat or partner.vat),
                    'group_member_tax_number': partner.l10n_hu_group_vat and (force_vat or partner.vat),
                }
            elif partner.country_code in eu_country_codes:
                return {'community_vat_number': partner.vat}
            else:
                return {'third_state_tax_id': partner.vat}

        def format_bank_account_number(bank_account):
            # Normalize IBANs (no spaces!)
            if bank_account.acc_type == 'iban':
                return normalize_iban(bank_account.acc_number)
            else:
                return bank_account.acc_number

        supplier = self.company_id.partner_id
        customer = self.partner_id.commercial_partner_id

        currency_huf = self.env.ref('base.HUF')
        currency_rate = self._l10n_hu_get_currency_rate()

        invoice_values = {
            'invoice': self,
            'invoiceIssueDate': self.invoice_date,
            'completenessIndicator': False,
            'modifyWithoutMaster': False,
            'base_invoice': self._l10n_hu_get_chain_base(),
            'supplier': supplier,
            'supplier_vat_data': get_vat_data(supplier, self.fiscal_position_id.foreign_vat),
            'supplierBankAccountNumber': format_bank_account_number(self.partner_bank_id or supplier.bank_ids[:1]),
            'individualExemption': self.company_id.l10n_hu_tax_regime == 'ie',
            'customer': customer,
            'customerVatStatus': (not customer.is_company and 'PRIVATE_PERSON') or (customer.country_code == 'HU' and 'DOMESTIC') or 'OTHER',
            'customer_vat_data': get_vat_data(customer) if customer.is_company else None,
            'customerBankAccountNumber': format_bank_account_number(customer.bank_ids[:1]),
            'smallBusinessIndicator': self.company_id.l10n_hu_tax_regime == 'sb',
            'exchangeRate': currency_rate,
            'cashAccountingIndicator': self.company_id.l10n_hu_tax_regime == 'ca',
            'shipping_partner': self.partner_shipping_id,
            'sales_partner': self.user_id,
            'mergedItemIndicator': False,
            'format_bool': format_bool,
            'float_repr': float_repr,
            'lines_values': [],
        }

        sign = self.is_inbound() and 1.0 or -1.0
        line_number_offset = min(n for n in self.invoice_line_ids.mapped('l10n_hu_line_number') if n) - 1

        for line in self.line_ids.filtered(lambda l: l.l10n_hu_line_number).sorted(lambda l: l.l10n_hu_line_number):
            line_values = {
                'line': line,
                'lineNumber': line.l10n_hu_line_number - line_number_offset,
                'lineNumberReference': line.l10n_hu_line_number,
                'lineExpressionIndicator': line.product_id and line.product_uom_id,
                'lineNatureIndicator': {False: 'OTHER', 'service': 'SERVICE'}.get(line.product_id.type, 'PRODUCT'),
                'lineDescription': line.name.replace('\n', ' '),
            }

            if line.display_type == 'product':
                vat_tax = line.tax_ids.filtered(lambda t: t.l10n_hu_tax_type)
                price_unit_signed = sign * line.price_unit
                price_net_signed = self.currency_id.round(price_unit_signed * line.quantity * (1 - line.discount / 100.0))
                discount_value_signed = self.currency_id.round(price_unit_signed * line.quantity - price_net_signed)
                price_total_signed = sign * line.price_total
                vat_amount_signed = self.currency_id.round(price_total_signed - price_net_signed)

                line_values.update({
                    'vat_tax': vat_tax,
                    'vatPercentage': float_round(vat_tax.amount / 100.0, 4),
                    'quantity': line.quantity,
                    'unitPrice': price_unit_signed,
                    'unitPriceHUF': currency_huf.round(price_unit_signed * currency_rate),
                    'discountValue': discount_value_signed,
                    'discountRate': line.discount / 100.0,
                    'lineNetAmount': price_net_signed,
                    'lineNetAmountHUF': currency_huf.round(price_net_signed * currency_rate),
                    'lineVatData': not self.currency_id.is_zero(vat_amount_signed),
                    'lineVatAmount': vat_amount_signed,
                    'lineVatAmountHUF': currency_huf.round(vat_amount_signed * currency_rate),
                    'lineGrossAmountNormal': price_total_signed,
                    'lineGrossAmountNormalHUF': currency_huf.round(price_total_signed * currency_rate),
                })

            elif line.display_type == 'rounding':
                atk_tax = self.env['account.tax'].search([('l10n_hu_tax_type', '=', 'ATK'), ('company_id', '=', self.company_id.id)], limit=1)
                if not atk_tax:
                    raise UserError(_('Please create an ATK (outside the scope of the VAT Act) type of tax!'))

                amount_huf = line.balance if self.company_id.currency_id == currency_huf else currency_huf.round(line.amount_currency * currency_rate)
                line_values.update({
                    'vat_tax': atk_tax,
                    'vatPercentage': float_round(atk_tax.amount / 100.0, 4),
                    'quantity': 1.0,
                    'unitPrice': -line.amount_currency,
                    'unitPriceHUF': -amount_huf,
                    'lineNetAmount': -line.amount_currency,
                    'lineNetAmountHUF': -amount_huf,
                    'lineVatData': False,
                    'lineGrossAmountNormal': -line.amount_currency,
                    'lineGrossAmountNormalHUF': -amount_huf,
                })

            invoice_values['lines_values'].append(line_values)

        is_company_huf = self.company_id.currency_id == currency_huf
        tax_amounts_by_tax = {
            line.tax_line_id: {
                'vatRateVatAmount': -line.amount_currency,
                'vatRateVatAmountHUF': -line.balance if is_company_huf else currency_huf.round(-line.amount_currency * currency_rate),
            }
            for line in self.line_ids.filtered(lambda l: l.tax_line_id.l10n_hu_tax_type)
        }

        invoice_values['tax_summary'] = [
            {
                'vat_tax': vat_tax,
                'vatPercentage': float_round(vat_tax.amount / 100.0, 4),
                'vatRateNetAmount': self.currency_id.round(sum(l['lineNetAmount'] for l in lines_values_by_tax)),
                'vatRateNetAmountHUF': currency_huf.round(sum(l['lineNetAmountHUF'] for l in lines_values_by_tax)),
                'vatRateVatAmount': tax_amounts_by_tax.get(vat_tax, {}).get('vatRateVatAmount', 0.0),
                'vatRateVatAmountHUF': tax_amounts_by_tax.get(vat_tax, {}).get('vatRateVatAmountHUF', 0.0),
            }
            for vat_tax, lines_values_by_tax in groupby(invoice_values['lines_values'], lambda l: l['vat_tax'])
        ]

        total_vat = self.currency_id.round(sum(tax_vals['vatRateVatAmount'] for tax_vals in invoice_values['tax_summary']))
        total_vat_huf = currency_huf.round(sum(tax_vals['vatRateVatAmountHUF'] for tax_vals in invoice_values['tax_summary']))

        total_gross = self.amount_total_in_currency_signed
        total_gross_huf = self.amount_total_signed if is_company_huf else currency_huf.round(self.amount_total_in_currency_signed * currency_rate)

        total_net = self.currency_id.round(total_gross - total_vat)
        total_net_huf = currency_huf.round(total_gross_huf - total_vat_huf)

        invoice_values.update({
            'invoiceNetAmount': total_net,
            'invoiceNetAmountHUF': total_net_huf,
            'invoiceVatAmount': total_vat,
            'invoiceVatAmountHUF': total_vat_huf,
            'invoiceGrossAmount': total_gross,
            'invoiceGrossAmountHUF': total_gross_huf,
        })

        return invoice_values

    # === PDF generation === #

    def _get_name_invoice_report(self):
        self.ensure_one()
        return self.country_code == 'HU' and 'l10n_hu_edi.report_invoice_document' or super()._get_name_invoice_report()

    def _l10n_hu_get_invoice_totals_for_report(self):

        def invert_dict(dictionary, keys_to_invert):
            """ Replace the values of keys_to_invert by their negative. """
            dictionary.update({
                key: -value
                for key, value in dictionary.items() if key in keys_to_invert
            })
            keys_to_reformat = {f'formatted_{x}': x for x in keys_to_invert}
            dictionary.update({
                key: formatLang(self.env, dictionary[keys_to_reformat[key]], currency_obj=self.company_id.currency_id)
                for key, value in dictionary.items() if key in keys_to_reformat
            })

        self.ensure_one()

        tax_totals = self.tax_totals
        if not isinstance(tax_totals, dict):
            return tax_totals

        tax_totals['display_tax_base'] = True

        if 'refund' in self.move_type:
            invert_dict(tax_totals, ['amount_total', 'amount_untaxed', 'rounding_amount', 'amount_total_rounded'])

            for subtotal in tax_totals['subtotals']:
                invert_dict(subtotal, ['amount'])

            for tax_list in tax_totals['groups_by_subtotal'].values():
                for tax in tax_list:
                    keys_to_invert = ['tax_group_amount', 'tax_group_base_amount', 'tax_group_amount_company_currency', 'tax_group_base_amount_company_currency']
                    invert_dict(tax, keys_to_invert)

        currency_huf = self.env.ref('base.HUF')
        currency_rate = self._l10n_hu_get_currency_rate()

        tax_totals['total_vat_amount_in_huf'] = sum(
            -line.balance if self.company_id.currency_id == currency_huf else currency_huf.round(-line.amount_currency * currency_rate)
            for line in self.line_ids.filtered(lambda l: l.tax_line_id.l10n_hu_tax_type)
        )
        tax_totals['formatted_total_vat_amount_in_huf'] = formatLang(
            self.env, tax_totals['total_vat_amount_in_huf'], currency_obj=currency_huf
        )

        return tax_totals


class AccountInvoiceLine(models.Model):
    _inherit = 'account.move.line'

    # === Technical fields === #
    l10n_hu_line_number = fields.Integer(
        string='(HU) Line Number',
        help='A consecutive indexing of invoice lines within the invoice chain.',
        copy=False,
    )

    @api.depends('move_id.delivery_date')
    def _compute_currency_rate(self):
        super()._compute_currency_rate()
        # In Hungary, the currency rate should be based on the delivery date.
        for line in self.filtered(lambda l: l.move_id.country_code == 'HU' and l.currency_id):
            line.currency_rate = self.env['res.currency']._get_conversion_rate(
                from_currency=line.company_currency_id,
                to_currency=line.currency_id,
                company=line.company_id,
                date=line.move_id.delivery_date or line.move_id.invoice_date or line.move_id.date or fields.Date.context_today(line),
            )
