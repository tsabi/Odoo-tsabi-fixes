# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import fields, models, api, _
from odoo.http import request
from odoo.exceptions import UserError, ValidationError
from odoo.tools import formatLang, float_round, float_repr, cleanup_xml_node, groupby
from odoo.addons.base_iban.models.res_partner_bank import normalize_iban
from odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection import format_bool, L10nHuEdiConnectionError
from odoo.addons.l10n_hu_edi.models.res_company import L10N_HU_EDI_SERVER_MODE_SELECTION

import base64
import math
from lxml import etree
import logging
import re
from datetime import timedelta
import contextlib
from psycopg2 import OperationalError

_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = 'account.move'

    # === EDI Fields === #
    l10n_hu_edi_state = fields.Selection(
        ######################################################################################################################
        # STATE DIAGRAM
        # * False, rejected, cancelled --[upload]--> False, sent, send_timeout
        # * sent --[query_status]--> sent, confirmed, confirmed_warning, rejected
        # * confirmed, confirmed_warning --[request_cancel]--> cancel_sent, cancel_timeout
        # * cancel_sent, cancel_pending --[query_status]--> confirmed_warning, cancel_pending, cancelled
        # * send_timeout --[recover_timeout]--> False, send_timeout, confirmed, confirmed_warning, rejected,
        # * cancel_timeout --[recover_timeout]--> confirmed_warning, cancel_sent, cancel_timeout, cancel_pending, cancelled
        ######################################################################################################################
        selection=[
            ('sent', 'Sent, waiting for response'),
            ('send_timeout', 'Timeout when sending'),
            ('confirmed', 'Confirmed'),
            ('confirmed_warning', 'Confirmed with warnings'),
            ('rejected', 'Rejected'),
            ('cancel_sent', 'Cancellation request sent'),
            ('cancel_timeout', 'Timeout when requesting cancellation'),
            ('cancel_pending', 'Cancellation request pending'),
            ('cancelled', 'Cancelled'),
        ],
        string='NAV 3.0 status',
        copy=False,
        index='btree_not_null',
    )
    l10n_hu_edi_batch_upload_index = fields.Integer(
        string='Index of invoice within a batch upload',
        copy=False,
    )
    l10n_hu_edi_attachment = fields.Binary(
        string='Invoice XML file',
        attachment=True,
        copy=False,
    )
    l10n_hu_edi_send_time = fields.Datetime(
        string='Invoice upload time',
        copy=False,
    )
    l10n_hu_edi_transaction_code = fields.Char(
        string='Transaction Code',
        index='trigram',
        copy=False,
        tracking=True,
    )
    l10n_hu_edi_messages = fields.Json(
        string='Transaction messages (JSON)',
        copy=False,
    )
    l10n_hu_invoice_chain_index = fields.Integer(
        string='Invoice Chain Index',
        help='Index in the chain of modification invoices.',
        copy=False,
    )
    l10n_hu_edi_server_mode = fields.Selection(
        selection=L10N_HU_EDI_SERVER_MODE_SELECTION,
        string='Server Mode',
    )
    l10n_hu_edi_show_button_update_status = fields.Char(
        string='Technical field to show the Update Status button',
        compute='_compute_l10n_hu_edi_show_button_update_status',
    )
    l10n_hu_edi_attachment_filename = fields.Char(
        string='Invoice XML filename',
        compute='_compute_l10n_hu_edi_attachment_filename',
    )
    l10n_hu_edi_message_html = fields.Html(
        string='Transaction messages',
        compute='_compute_message_html',
    )

    # === Constraints === #

    @api.constrains('l10n_hu_edi_state', 'state')
    def _check_posted_if_active(self):
        """ Enforce the constraint that you cannot reset to draft / cancel a posted invoice if it was already sent to NAV. """
        for move in self:
            if move.state in ['draft', 'cancel'] and move.l10n_hu_edi_state not in [False, 'rejected', 'cancelled']:
                raise ValidationError(_('Cannot reset to draft or cancel invoice %s because an electronic document was already sent to NAV!', move.name))

    # === Computes === #

    @api.depends('l10n_hu_edi_messages')
    def _compute_message_html(self):
        for move in self:
            if move.l10n_hu_edi_messages:
                move.l10n_hu_edi_message_html = self.env['account.move.send']._format_error_html(move.l10n_hu_edi_messages)
            else:
                move.l10n_hu_edi_message_html = False

    @api.depends('l10n_hu_edi_state', 'state')
    def _compute_show_reset_to_draft_button(self):
        super()._compute_show_reset_to_draft_button()
        self.filtered(lambda m: m.l10n_hu_edi_state not in [False, 'rejected', 'cancelled']).show_reset_to_draft_button = False

    @api.depends('l10n_hu_edi_state')
    def _compute_need_cancel_request(self):
        # EXTEND 'account' to add dependencies
        return super()._compute_need_cancel_request()

    @api.depends('name')
    def _compute_l10n_hu_edi_attachment_filename(self):
        for move in self:
            move.l10n_hu_edi_attachment_filename = f'{move.name.replace("/", "_")}.xml'

    @api.depends('l10n_hu_edi_state')
    def _compute_l10n_hu_edi_show_button_update_status(self):
        for move in self:
            move.l10n_hu_edi_show_button_update_status = move._l10n_hu_edi_get_valid_action() in ['query_status', 'recover_timeout']

    # === Overrides === #

    def action_reverse(self):
        # EXTEND account
        unconfirmed = self.filtered(lambda m: m.l10n_hu_edi_state not in ['confirmed', 'confirmed_warning'])
        if unconfirmed:
            raise UserError(_(
                'Invoices %s have not yet been confirmed by NAV. Please wait for confirmation before issuing a modification invoice.',
                unconfirmed.mapped('name'))
            )
        return super().action_reverse()

    def _need_cancel_request(self):
        # EXTEND account
        # Technical annulment should be available only in debug mode
        return super()._need_cancel_request() or (self.l10n_hu_edi_state in ['confirmed', 'confirmed_warning'] and request.session.debug)

    def button_request_cancel(self):
        # EXTEND 'account'
        if self._need_cancel_request() and self.l10n_hu_edi_state in ['confirmed', 'confirmed_warning']:
            return {
                "name": _("Technical Annulment"),
                "type": "ir.actions.act_window",
                "view_type": "form",
                "view_mode": "form",
                "res_model": "l10n_hu_edi.cancellation",
                "target": "new",
                "context": {"default_invoice_id": self.id},
            }

        return super().button_request_cancel()

    def _post(self, soft=True):
        # EXTEND account
        to_post = self.filtered(lambda move: move.date <= fields.Date.context_today(self)) if soft else self
        for move in to_post.filtered(lambda m: m.country_code == 'HU' and m.is_sale_document()):
            move._l10n_hu_edi_set_chain_index_and_line_number()
        return super()._post(soft=soft)

    # === Actions === #

    def l10n_hu_edi_button_update_status(self, from_cron=False):
        """ Attempt to update the status of the invoices in `self` """
        self = self.filtered(lambda m: m._l10n_hu_edi_get_valid_action() in ['recover_timeout', 'query_status'])

        with self._l10n_hu_edi_acquire_lock():
            # Attempt timeout recovery if any invoices need it.
            self.filtered(lambda m: m._l10n_hu_edi_get_valid_action() == 'recover_timeout')._l10n_hu_edi_recover_timeout()

            # Call `query_status` on the invoices.
            self.filtered(lambda m: m._l10n_hu_edi_get_valid_action() == 'query_status')._l10n_hu_edi_query_status()

            # Error handling.
            for invoice in self:
                # Log invoice status in chatter.
                formatted_message = self.env['account.move.send']._format_error_html(invoice.l10n_hu_edi_messages)
                invoice.with_context(no_new_invoice=True).message_post(body=formatted_message)

            for invoice in self:
                # If blocking errors, raise UserError.
                if invoice.l10n_hu_edi_messages.get('blocking_level') == 'error' and not from_cron:
                    raise UserError(self.env['account.move.send']._format_error_text(invoice.l10n_hu_edi_messages))

    # === Helpers === #

    def _l10n_hu_edi_get_valid_action(self):
        """ If a NAV 3.0 flow is applicable to the given invoice, return it, else None.
        """
        self.ensure_one()
        states_by_action = {
            'upload': [False, 'rejected', 'cancelled'],
            'query_status': ['sent', 'cancel_sent', 'cancel_pending'],
            'recover_timeout': ['send_timeout', 'cancel_timeout'],
            'request_cancel': ['confirmed', 'confirmed_warning'],
        }
        for action, states in states_by_action.items():
            if self.l10n_hu_edi_state in states:
                break
        else:
            return

        if (
            self.country_code == 'HU'
            and self.is_sale_document()
            and self.state == 'posted'
            # Only process moves that are in the same server mode (test/production) as the company (except if we are sending a new XML)
            and (action == 'upload' or self.l10n_hu_edi_server_mode == self.company_id.l10n_hu_edi_server_mode)
        ):
            return action

    def _l10n_hu_edi_check_action(self, action):
        """ Raise an error if the given action cannot be performed with the invoices in self. """
        bad_invoices = self.filtered(lambda m: m._l10n_hu_edi_get_valid_action() != action)
        if bad_invoices:
            raise UserError(_('Action %s cannot be processed for invoices %s!', action, bad_invoices.mapped('name')))

    def _l10n_hu_get_chain_base(self):
        """ Get the base invoice of the invoice chain. """
        modification_invoices = self
        base_invoices = self.env['account.move']
        while modification_invoices:
            base_invoices |= modification_invoices.filtered(lambda m: not m.reversed_entry_id and not m.debit_origin_id)
            modification_invoices = modification_invoices.reversed_entry_id | modification_invoices.debit_origin_id
        return base_invoices

    def _l10n_hu_get_chain_invoices(self):
        """ Given base invoices, get all invoices in the chain that already have an index. """
        chain_invoices = self
        next_invoices = self
        while (next_invoices := next_invoices.reversal_move_id | next_invoices.debit_note_ids):
            chain_invoices |= next_invoices
        return chain_invoices.filtered(lambda m: m.l10n_hu_invoice_chain_index)

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

    def _l10n_hu_edi_set_chain_index_and_line_number(self):
        """ Set the l10n_hu_invoice_chain_index and l10n_hu_line_number fields. """
        self.ensure_one()
        base_invoice = self._l10n_hu_get_chain_base()
        if base_invoice == self:
            self.l10n_hu_invoice_chain_index = -1  # -1 indicates a base invoice (0 indicates the chain index was not set).
            next_line_number = 1
        else:
            # Lock base invoice to prevent concurrent updates, ensuring sequence integrity.
            base_invoice._l10n_hu_edi_acquire_lock(commit=False)

            prev_chain_invoices = base_invoice._l10n_hu_get_chain_invoices()
            if not self.l10n_hu_invoice_chain_index:
                last_chain_invoice = prev_chain_invoices.sorted(lambda m: m.l10n_hu_invoice_chain_index)[-1]
                self.l10n_hu_invoice_chain_index = last_chain_invoice.l10n_hu_invoice_chain_index + 1 or 1
            else:
                last_chain_invoice = prev_chain_invoices.filtered(lambda m: m.l10n_hu_invoice_chain_index == (self.l10n_hu_invoice_chain_index - 1 or -1))

            next_line_number = (max(last_chain_invoice.line_ids.mapped('l10n_hu_line_number')) or 0) + 1

        # Set l10n_hu_line_number consecutively, first on product lines, then on rounding line
        for line_number, line in enumerate(
            self.line_ids.filtered(lambda l: l.display_type in ['product', 'rounding']).sorted(lambda l: l.display_type),
            start=next_line_number,
        ):
            line.l10n_hu_line_number = line_number

    @contextlib.contextmanager
    def _l10n_hu_edi_acquire_lock(self, commit=True):
        """ Acquire a write lock on the invoices in self.
        :param commit bool: On exit, commit to DB.
        """
        if not self:
            yield
            return
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute('SELECT * FROM account_move WHERE id IN %s FOR UPDATE NOWAIT', [tuple(self.ids)])
        except OperationalError as e:
            if e.pgcode == '55P03':
                raise UserError(_('Could not acquire lock on invoices - is another user performing operations on them?'))
            raise
        yield
        if self.env['account.move.send']._can_commit() and commit:
            self.env.cr.commit()

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
            'company_vat_invalid': {
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
            'invoice_chain_not_confirmed': {
                'records': self.env['account.move'].union(*[
                    move._l10n_hu_get_chain_base()._l10n_hu_get_chain_invoices().filtered(
                        lambda m: (
                            m.l10n_hu_invoice_chain_index < move.l10n_hu_invoice_chain_index
                            and m.l10n_hu_edi_state not in ['confirmed', 'confirmed_warning']
                        )
                    )
                    for move in self
                ]),
                'message': _('Please wait for all invoices in the chain to be confirmed before sending!'),
                'action_text': _('View invoice(s)'),
            },
            'invoice_chain_index_not_set': {
                'records': self.filtered(lambda m: not m.l10n_hu_invoice_chain_index),
                'message': _('The invoice was created before Hungarian e-invoicing was installed, before sending please reset to draft and re-confirm.'),
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

        if companies_missing_credentials := self.company_id.filtered(lambda c: not c.l10n_hu_edi_server_mode):
            errors['company_credentials_missing'] = {
                'message': _('Please set NAV credentials in the Accounting Settings!'),
                'action_text': _('Open Accounting Settings'),
                'action': self.env.ref('account.action_account_config').with_company(companies_missing_credentials[0])._get_action_dict(),
            }

        return errors

    def _l10n_hu_edi_upload(self):
        """ Generate invoice XMLs and send to NAV. """
        self._l10n_hu_edi_check_action('upload')

        for invoice in self:
            invoice.write({
                'l10n_hu_edi_server_mode': invoice.company_id.l10n_hu_edi_server_mode,
                'l10n_hu_edi_attachment': base64.b64encode(invoice._l10n_hu_edi_generate_xml()),
            })
            # Set name & mimetype on newly-created attachment.
            attachment = self.env['ir.attachment'].search([
                ('res_model', '=', self._name),
                ('res_id', 'in', self.ids),
                ('res_field', '=', 'l10n_hu_edi_attachment'),
            ])
            attachment.write({
                'name': invoice.l10n_hu_edi_attachment_filename,
                'mimetype': 'application/xml',
            })

        with self._l10n_hu_edi_acquire_lock():
            # Batch by company, with max 100 invoices per batch.
            for __, batch_company in groupby(self, lambda m: m.company_id):
                for __, batch in groupby(enumerate(batch_company), lambda x: x[0] // 100):
                    self.env['account.move'].browse([m.id for __, m in batch])._l10n_hu_edi_upload_single_batch()

    def _l10n_hu_edi_upload_single_batch(self):
        self._l10n_hu_edi_check_action('upload')
        for i, invoice in enumerate(self, start=1):
            invoice.l10n_hu_edi_batch_upload_index = i

        invoice_operations = [
            {
                'index': invoice.l10n_hu_edi_batch_upload_index,
                'operation': (
                    'CREATE' if (base_invoice := invoice._l10n_hu_get_chain_base()) == invoice else (
                        'STORNO' if (
                            base_invoice._get_reconciled_amls().move_id == invoice
                            and invoice.currency_id.is_zero(invoice.amount_residual)
                            and base_invoice.currency_id.is_zero(base_invoice.amount_residual)
                        ) else 'MODIFY'
                    )
                ),
                'invoice_data': base64.b64decode(invoice.l10n_hu_edi_attachment),
            }
            for invoice in self
        ]

        try:
            token_result = self.env['l10n_hu_edi.connection']._do_token_exchange(self.company_id._l10n_hu_edi_get_credentials_dict())
        except L10nHuEdiConnectionError as e:
            return self.write({
                'l10n_hu_edi_state': 'rejected',
                'l10n_hu_edi_messages': {
                    'error_title': _('Could not authenticate with NAV. Check your credentials and try again.'),
                    'errors': e.errors,
                    'blocking_level': 'error',
                },
            })

        self.write({'l10n_hu_edi_send_time': fields.Datetime.now()})

        try:
            transaction_code = self.env['l10n_hu_edi.connection']._do_manage_invoice(
                self.company_id._l10n_hu_edi_get_credentials_dict(),
                token_result['token'],
                invoice_operations,
            )
        except L10nHuEdiConnectionError as e:
            if e.code == 'timeout':
                return self.write({
                    'l10n_hu_edi_state': 'send_timeout',
                    'l10n_hu_edi_messages': {
                        'error_title': _('Invoice submission timed out. Please wait at least 6 minutes before updating status.'),
                        'errors': e.errors,
                        'blocking_level': 'error_but_continue',
                    },
                })
            return self.write({
                'l10n_hu_edi_state': 'rejected',
                'l10n_hu_edi_messages': {
                    'error_title': _('Invoice submission failed.'),
                    'errors': e.errors,
                    'blocking_level': 'error',
                },
            })

        self.write({
            'l10n_hu_edi_state': 'sent',
            'l10n_hu_edi_transaction_code': transaction_code,
            'l10n_hu_edi_messages': {
                'error_title': _('Invoice submitted, waiting for response.'),
                'errors': [],
            }
        })

    def _l10n_hu_edi_query_status(self):
        """ Check the NAV invoice status. """
        # We should update all invoices with the same company and transaction code at once.
        self |= self.search([
            ('company_id', 'in', self.company_id.ids),
            ('l10n_hu_edi_transaction_code', 'in', self.mapped('l10n_hu_edi_transaction_code')),
            ('l10n_hu_edi_state', 'in', ['sent', 'cancel_sent']),
        ])
        self._l10n_hu_edi_check_action('query_status')

        with self._l10n_hu_edi_acquire_lock():
            # Querying status should be grouped by company and transaction code
            for __, invoices in groupby(self, lambda m: (m.company_id, m.l10n_hu_edi_transaction_code)):
                self.env['account.move'].browse([m.id for m in invoices])._l10n_hu_edi_query_status_single_batch()

    def _l10n_hu_edi_query_status_single_batch(self):
        """ Check the NAV status for invoices that share the same transaction code (uploaded in a single batch). """
        self._l10n_hu_edi_check_action('query_status')
        try:
            results = self.env['l10n_hu_edi.connection']._do_query_transaction_status(
                self.company_id._l10n_hu_edi_get_credentials_dict(),
                self[0].l10n_hu_edi_transaction_code,
            )
        except L10nHuEdiConnectionError as e:
            if self.l10n_hu_edi_state == 'sent':
                return self.write({
                    'l10n_hu_edi_messages': {
                        'error_title': _('The invoice was sent to the NAV, but there was an error querying its status.'),
                        'errors': e.errors,
                        'blocking_level': 'error_but_continue',
                    },
                })
            else:
                return self.write({
                    'l10n_hu_edi_messages': {
                        'error_title': _('The annulment was sent to the NAV, but there was an error querying its status.'),
                        'errors': e.errors,
                        'blocking_level': 'error_but_continue',
                    },
                })

        for processing_result in results['processing_results']:
            invoice = self.filtered(lambda m: str(m.l10n_hu_edi_batch_upload_index) == processing_result['index'])
            if not invoice:
                _logger.error(_('Could not match NAV transaction_code %s, index %s to an invoice in Odoo',
                                self[0].l10n_hu_edi_transaction_code,
                                processing_result['index']))
                continue

            invoice._l10n_hu_edi_process_query_transaction_result(processing_result, results['annulment_status'])

    def _l10n_hu_edi_process_query_transaction_result(self, processing_result, annulment_status):
        def get_errors_from_processing_result(processing_result):
            return [
                f'({message["validation_result_code"]}) {message["validation_error_code"]}: {message["message"]}'
                for message in processing_result.get('business_validation_messages', []) + processing_result.get('technical_validation_messages', [])
            ]

        self.ensure_one()

        if processing_result['invoice_status'] in ['RECEIVED', 'PROCESSING', 'SAVED']:
            # The invoice/annulment has not been processed yet.
            if self.l10n_hu_edi_state in ['sent', 'send_timeout']:
                self.write({
                    'l10n_hu_edi_state': 'sent',
                    'l10n_hu_edi_messages': {
                        'error_title': _('The invoice was received by the NAV, but has not been confirmed yet.'),
                        'errors': get_errors_from_processing_result(processing_result),
                        'blocking_level': 'error_but_continue',
                    },
                })
            elif self.l10n_hu_edi_state in ['cancel_sent', 'cancel_timeout']:
                self.write({
                    'l10n_hu_edi_state': 'cancel_sent',
                    'l10n_hu_edi_messages': {
                        'error_title': _('The annulment request was received by the NAV, but has not been confirmed yet.'),
                        'errors': get_errors_from_processing_result(processing_result),
                        'blocking_level': 'error_but_continue',
                    },
                })

        elif processing_result['invoice_status'] == 'DONE':
            if self.l10n_hu_edi_state in ['sent', 'send_timeout']:
                if not processing_result['business_validation_messages'] and not processing_result['technical_validation_messages']:
                    self.write({
                        'l10n_hu_edi_state': 'confirmed',
                        'l10n_hu_edi_messages': {
                            'error_title': _('The invoice was successfully accepted by the NAV.'),
                            'errors': get_errors_from_processing_result(processing_result),
                        },
                    })
                else:
                    self.write({
                        'l10n_hu_edi_state': 'confirmed_warning',
                        'l10n_hu_edi_messages': {
                            'error_title': _(
                                'The invoice was accepted by the NAV, but warnings were reported. '
                                'To reverse, create a credit note / debit note.'
                            ),
                            'errors': get_errors_from_processing_result(processing_result),
                            'blocking_level': 'error_but_continue',
                        },
                    })
            elif self.l10n_hu_edi_state in ['cancel_sent', 'cancel_timeout', 'cancel_pending']:
                if annulment_status == 'NOT_VERIFIABLE':
                    self.write({
                        'l10n_hu_edi_state': 'confirmed_warning',
                        'l10n_hu_edi_messages': {
                            'error_title': _('The annulment request was rejected by NAV.'),
                            'errors': get_errors_from_processing_result(processing_result),
                            'blocking_level': 'error_but_continue',
                        },
                    })
                elif annulment_status == 'VERIFICATION_PENDING':
                    self.write({
                        'l10n_hu_edi_state': 'cancel_pending',
                        'l10n_hu_edi_messages': {
                            'error_title': _('The annulment request is pending, please confirm it on the OnlineSzámla portal.'),
                            'errors': get_errors_from_processing_result(processing_result),
                            'blocking_level': 'error_but_continue',
                        }
                    })
                elif annulment_status == 'VERIFICATION_DONE':
                    # Annulling a base invoice will also annul all its modification invoices on NAV.
                    to_cancel = self if self.reversed_entry_id or self.debit_origin_id else self._l10n_hu_get_chain_invoices().filtered(lambda m: m.l10n_hu_edi_state)
                    to_cancel.write({
                        'l10n_hu_edi_state': 'cancelled',
                        'l10n_hu_edi_messages': {
                            'error_title': _('The annulment request has been approved by the user on the OnlineSzámla portal.'),
                            'errors': get_errors_from_processing_result(processing_result),
                        }
                    })
                    to_cancel.button_cancel()
                elif annulment_status == 'VERIFICATION_REJECTED':
                    self.write({
                        'l10n_hu_edi_state': 'confirmed_warning',
                        'l10n_hu_edi_messages': {
                            'error_title': _('The annulment request was rejected by the user on the OnlineSzámla portal.'),
                            'errors': get_errors_from_processing_result(processing_result),
                            'blocking_level': 'error',
                        }
                    })

        elif processing_result['invoice_status'] == 'ABORTED':
            if self.l10n_hu_edi_state in ['sent', 'send_timeout']:
                self.write({
                    'l10n_hu_edi_state': 'rejected',
                    'l10n_hu_edi_messages': {
                        'error_title': _('The invoice was rejected by the NAV.'),
                        'errors': get_errors_from_processing_result(processing_result),
                        'blocking_level': 'error',
                    },
                })
            elif self.l10n_hu_edi_state in ['cancel_sent', 'cancel_timeout', 'cancel_pending']:
                self.write({
                    'l10n_hu_edi_state': 'confirmed_warning',
                    'l10n_hu_edi_messages': {
                        'error_title': _('The cancellation request could not be performed.'),
                        'errors': get_errors_from_processing_result(processing_result),
                        'blocking_level': 'error',
                    },
                })

    def _l10n_hu_edi_recover_timeout(self):
        """ Attempt to recover all invoices in `self` from an upload timeout """
        # Only attempt to recover from a timeout for invoices sent more than 6 minutes ago.
        self._l10n_hu_edi_check_action('recover_timeout')
        self = self.filtered(lambda m: m.l10n_hu_edi_send_time <= fields.Datetime.now() - timedelta(minutes=6))
        with self._l10n_hu_edi_acquire_lock():
            # Group by company.
            for __, invoices_by_company in groupby(self, lambda m: m.company_id):
                # Further group by 7-minute time intervals (more precisely, time intervals which don't have more than 7 minutes between missing invoices)
                time_interval_groups = []
                for invoice in sorted(invoices_by_company, key=lambda m: m.l10n_hu_edi_send_time):
                    if not time_interval_groups or invoice.l10n_hu_edi_send_time >= time_interval_groups[-1][-1].l10n_hu_edi_send_time + timedelta(minutes=5):
                        time_interval_groups.append(invoice)
                    else:
                        time_interval_groups[-1] += invoice

                for invoices in time_interval_groups:
                    invoices._l10n_hu_edi_recover_timeout_single_batch()

    def _l10n_hu_edi_recover_timeout_single_batch(self):
        self._l10n_hu_edi_check_action('recover_timeout')
        datetime_from = min(self.mapped('l10n_hu_edi_send_time'))
        datetime_to = max(self.mapped('l10n_hu_edi_send_time')) + timedelta(minutes=7)

        page = 1
        available_pages = 1

        while page <= available_pages:
            try:
                transaction_list = self.env['l10n_hu_edi.connection']._do_query_transaction_list(
                    self.company_id._l10n_hu_edi_get_credentials_dict(),
                    datetime_from,
                    datetime_to,
                    page
                )
            except L10nHuEdiConnectionError as e:
                return self.write({
                    'l10n_hu_edi_messages': {
                        'error_title': _('Error querying active transactions while attempting timeout recovery.'),
                        'errors': e.errors,
                        'blocking_level': 'error_but_continue',
                    },
                })

            transaction_codes_to_query = [
                t['transaction_code']
                for t in transaction_list['transactions']
                if t['username'] == self.company_id.l10n_hu_edi_username
                   and t['source'] == 'MGM'
            ]

            for transaction_code in transaction_codes_to_query:
                try:
                    results = self.env['l10n_hu_edi.connection']._do_query_transaction_status(
                        self.company_id._l10n_hu_edi_get_credentials_dict(),
                        transaction_code,
                        return_original_request=True,
                    )
                except L10nHuEdiConnectionError as e:
                    return self.write({
                        'l10n_hu_edi_messages': {
                            'error_title': _('Error querying active transactions while attempting timeout recovery.'),
                            'errors': e.errors,
                            'blocking_level': 'error_but_continue',
                        },
                    })

                for processing_result in results['processing_results']:
                    # Match invoice if the returned XML is the same as the one stored in Odoo.
                    # Match annulment if the invoice name matches.
                    matched_invoice = self.filtered(
                        lambda m: (
                            (
                                m.l10n_hu_edi_state == 'send_timeout'
                                and etree.canonicalize(base64.b64decode(m.l10n_hu_edi_attachment).decode())
                                    == etree.canonicalize(processing_result['original_file'])
                            ) or (
                                m.l10n_hu_edi_state == 'cancel_timeout'
                                and m.name == processing_result['original_xml'].findtext('{*}annulmentReference')
                            )
                        )
                    )

                    if matched_invoice:
                        # Set the correct transaction code on the matched invoice
                        matched_invoice.l10n_hu_edi_transaction_code = transaction_code
                        matched_invoice._l10n_hu_edi_process_query_transaction_result(processing_result, results['annulment_status'])

            available_pages = transaction_list['available_pages']
            page += 1

        # Any invoices that could not be matched to the query results should be regarded as not received by NAV.
        self.filtered(lambda m: m.l10n_hu_edi_state == 'send_timeout').write({
            'l10n_hu_edi_state': False,
            'l10n_hu_edi_messages': {
                'error_title': _('Sending failed due to time-out.'),
                'errors': [],
            }
        })
        self.filtered(lambda m: m.l10n_hu_edi_state == 'cancel_timeout').write({
            'l10n_hu_edi_state': 'confirmed_warning',
            'l10n_hu_edi_messages': {
                'error_title': _('Annulment failed due to time-out.'),
                'errors': [],
            }
        })

    def _l10n_hu_edi_request_cancel(self, code, reason):
        """ Send a cancellation request for all invoices in `self`. """
        self._l10n_hu_edi_check_action('request_cancel')
        with self._l10n_hu_edi_acquire_lock():
            # Batch by company, with max 100 annulment requests per batch.
            for __, batch_company in groupby(self, lambda m: m.company_id):
                for __, batch in groupby(enumerate(batch_company), lambda x: x[0] // 100):
                    self.env['account.move'].browse([m.id for __, m in batch])._l10n_hu_edi_request_cancel_single_batch(code, reason)

    def _l10n_hu_edi_request_cancel_single_batch(self, code, reason):
        self._l10n_hu_edi_check_action('request_cancel')
        for i, invoice in enumerate(self, start=1):
            invoice.l10n_hu_edi_batch_upload_index = i

        annulment_operations = [
            {
                'index': invoice.l10n_hu_edi_batch_upload_index,
                'annulmentReference': invoice.name,
                'annulmentCode': code,
                'annulmentReason': reason,
            }
            for invoice in self
        ]

        try:
            token_result = self.env['l10n_hu_edi.connection']._do_token_exchange(self.company_id._l10n_hu_edi_get_credentials_dict())
        except L10nHuEdiConnectionError as e:
            return self.write({
                'l10n_hu_edi_messages': {
                    'error_title': _('Could not authenticate with NAV. Check your credentials and try again.'),
                    'errors': e.errors,
                    'blocking_level': 'error',
                },
            })

        self.write({'l10n_hu_edi_send_time': fields.Datetime.now()})

        try:
            transaction_code = self.env['l10n_hu_edi.connection']._do_manage_annulment(
                self.company_id._l10n_hu_edi_get_credentials_dict(),
                token_result['token'],
                annulment_operations,
            )
        except L10nHuEdiConnectionError as e:
            if e.code == 'timeout':
                return self.write({
                    'l10n_hu_edi_state': 'cancel_timeout',
                    'l10n_hu_edi_messages': {
                        'error_title': _('Cancellation request timed out.'),
                        'errors': e.errors,
                        'blocking_level': 'error_but_continue',
                    },
                })
            return self.write({
                'l10n_hu_edi_messages': {
                    'error_title': _('Cancellation request failed.'),
                    'errors': e.errors,
                    'blocking_level': 'error',
                },
            })

        self.write({
            'l10n_hu_edi_state': 'cancel_sent',
            'l10n_hu_edi_transaction_code': transaction_code,
            'l10n_hu_edi_messages': {
                'error_title': _('Cancellation request submitted, waiting for response.'),
                'errors': [],
            }
        })

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

        base_invoice = self._l10n_hu_get_chain_base()

        invoice_values = {
            'invoice': self,
            'invoiceIssueDate': self.invoice_date,
            'completenessIndicator': False,
            'modifyWithoutMaster': False,
            'base_invoice': base_invoice if base_invoice != self else None,
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
                'lineNumberReference': base_invoice != self and line.l10n_hu_line_number,
                'lineExpressionIndicator': line.product_id and line.product_uom_id,
                'lineNatureIndicator': {False: 'OTHER', 'service': 'SERVICE'}.get(line.product_id.type, 'PRODUCT'),
                'lineDescription': line.name.replace('\n', ' '),
            }

            # Advance invoices case 1: this is an advance invoice
            with contextlib.suppress(AttributeError):
                if line.is_downpayment:
                    line_values['advanceIndicator'] = True

            # Advance invoices case 2: this is a final invoice that deducts an advance invoice
            advance_invoices = line._get_downpayment_lines().mapped('move_id').filtered(lambda m: m.state == 'posted')
            if advance_invoices:
                line_values.update({
                    'advanceIndicator': True,
                    'advanceOriginalInvoice': advance_invoices[0].name,
                    'advancePaymentDate': advance_invoices[0].invoice_date,
                    'advanceExchangeRate': advance_invoices[0]._l10n_hu_get_currency_rate(),
                })

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
