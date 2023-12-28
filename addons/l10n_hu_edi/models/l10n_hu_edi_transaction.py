# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, fields, api, _
from odoo.tools import groupby
from odoo.exceptions import UserError
from odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection import L10nHuEdiConnectionError

import base64
from contextlib import contextmanager
import logging
from psycopg2 import OperationalError
from datetime import timedelta

_logger = logging.getLogger(__name__)


_ACTIVE_STATES = ['to_send', 'token_error', 'sent', 'send_timeout', 'confirmed', 'confirmed_warning', 'query_error']

def check_state_machine(action):
    """ Decorator to help flag unintended state transitions.
    Any unintended start state will cause a UserError to be raised,
    any unintended end state will be logged as an error.
    """
    def decorate(fn):
        def decorated_fn(self, *args, **kwargs):
            states = self._get_action_info()[action]
            start_states, end_states = states['start_states'], states['end_states']
            if any(t.state not in start_states for t in self):
                raise UserError(_(
                    'Recordset %s: Method %s called with bad start state(s): %s, allowed states are %s',
                    self,
                    fn.__name__,
                    set(self.mapped('state')),
                    start_states,
                ))
            fn(self, *args, **kwargs)
            if any(t.state not in end_states for t in self):
                _logger.error(_(
                    'Recordset %s: Method %s returned with bad end state(s): %s, allowed states are %s',
                    self,
                    fn.__name__,
                    set(self.mapped('state')),
                    end_states,
                ))
        return decorated_fn
    return decorate


class L10nHuEdiTransaction(models.Model):
    #################################################################################################
    # OVERVIEW
    #
    # * This model works as a state machine.
    # * A transaction represents an invoice object on NAV's side.
    # * New transactions are created in the 'to_send' state.
    # * The state can be advanced through the `upload`, `abort` and `query_status` methods.
    # * Some states are 'active': in these states, the invoice number is reserved on NAV's side,
    #       therefore a new transaction must not be created for the invoice.
    # * Some states are final, which means that once the transaction has arrived into them, they can
    #       no longer be updated.
    # * Non-active final states are not blocking: the user can reset the invoice to draft,
    # * Active final states are blocking: in those states, the user can no longer delete the invoice,
    #       and must create a credit note it if he wants to annul it.
    #
    # STATE DIAGRAM
    # * to_send, token_error --[upload]--> token_error, sent, send_error, send_timeout
    # * to_send, token_error --[abort]--> unsent
    # * sent, query_error --[query_status]--> sent, confirmed, confirmed_warning, rejected, query_error
    # * send_timeout --[recover_timeout]--> to_send, sent, send_timeout
    # * send_error: is a final state due to recording requirements (to retry, create a new transaction)
    # * confirmed, confirmed_warning, rejected, unsent: are final states
    #################################################################################################
    _name = 'l10n_hu_edi.transaction'
    _description = 'Hungarian Tax Authority Invoice Upload Transaction'
    _order = 'create_date desc'
    _rec_name = 'id'

    state = fields.Selection(
        selection=[
            ('unsent', 'Unsent (aborted)'),
            ('to_send', 'To Send'),
            ('token_error', 'To Retry, could not get an authentication token'),
            ('sent', 'Sent, waiting for response'),
            ('send_error', 'Error when sending'),
            ('send_timeout', 'Timeout when sending'),
            ('confirmed', 'Confirmed'),
            ('confirmed_warning', 'Confirmed with warnings'),
            ('rejected', 'Rejected'),
            ('query_error', 'To Retry, error when requesting status'),
        ],
        string='Status',
        required=True,
        copy=False,
        default='to_send',
        index='btree_not_null',
    )
    is_active = fields.Boolean(
        string='Is Active',
        help="Active transactions are those where the invoice number is potentially locked on NAV's side.",
        compute='_compute_is_active',
        search='_search_is_active',
    )
    credentials_id = fields.Many2one(
        comodel_name='l10n_hu_edi.credentials',
        string='Credentials',
        required=True,
        ondelete='restrict',
        index='btree_not_null',
    )
    invoice_id = fields.Many2one(
        comodel_name='account.move',
        string='Invoice',
        required=True,
        ondelete='restrict',
    )
    operation = fields.Selection(
        selection=[
            ('CREATE', 'Create'),
            ('MODIFY', 'Modify'),
        ],
        string='Upload invoice operation type',
    )
    index = fields.Integer(
        string='Index of invoice within a batch upload',
        copy=False
    )
    attachment_file = fields.Binary(
        attachment=True,
        string='Invoice XML file',
    )
    send_time = fields.Datetime(
        string='Invoice upload time',
        copy=False,
    )
    transaction_code = fields.Char(
        string='Transaction Code',
        index='trigram',
        copy=False,
    )
    messages = fields.Json(
        string='Transaction messages (JSON)',
        copy=False,
    )
    message_html = fields.Html(
        string='Transaction messages',
        compute='_compute_message_html',
    )
    credentials_mode = fields.Selection(
        related='credentials_id.mode',
    )
    credentials_username = fields.Char(
        related='credentials_id.username',
    )

    # === Computes / Getters === #

    @api.depends('messages')
    def _compute_message_html(self):
        for transaction in self:
            transaction.message_html = self.env['account.move.send']._format_error_html(transaction.messages)

    @api.depends('state')
    def _compute_is_active(self):
        """ Active transactions are those where the invoice number is potentially locked on NAV's side.
        - If an active transaction exists, any new transaction with the same invoice number may be rejected by NAV.
        - Practically, a transaction is active if its state can potentially lead to 'Confirmed' or 'Confirmed with Warnings'.
        - New transactions start as active. An inactive transaction is guaranteed to remain inactive.
        """
        for transaction in self:
            transaction.is_active = transaction.state in _ACTIVE_STATES

    def _search_is_active(self, operator, value):
        if (operator, value) in [('=', True), ('!=', False)]:
            return [('state', 'in', _ACTIVE_STATES)]
        elif (operator, value) in [('=', False), ('!=', True)]:
            return [('state', 'not in', _ACTIVE_STATES)]
        else:
            raise UserError(_('Invalid operator!'))

    def _get_action_info(self):
        return {
            'upload': {
                'start_states': ['to_send', 'token_error'],
                'end_states': ['token_error', 'sent', 'send_error', 'send_timeout'],
            },
            'abort': {
                'start_states': ['to_send', 'token_error'],
                'end_states': ['unsent'],
            },
            'query_status': {
                'start_states': ['sent', 'query_error'],
                'end_states': ['sent', 'confirmed', 'confirmed_warning', 'rejected', 'query_error'],
            },
            'recover_timeout': {
                'start_states': ['send_timeout'],
                'end_states': ['to_send', 'sent', 'confirmed', 'confirmed_warning', 'rejected', 'query_error', 'send_timeout'],
            },
        }

    def _can_perform(self, action):
        """ Returns whether the given action ('upload', 'abort' or 'query_status') may be performed
        on a given transaction, given its state.

        You MUST call this method to check whether the action can be performed, before calling the action.

        This ensures integrity of the state machine."""
        if not self:
            return True
        self.ensure_one()
        return self.state in self._get_action_info().get(action)['start_states']

    # === State transitions === #

    @api.model_create_multi
    def create(self, vals_list):
        """ When creating a transaction for an invoice, generate the XML. """
        if any(not vals.get(field_name) for field_name in ['invoice_id', 'credentials_id', 'operation', 'attachment_file'] for vals in vals_list):
            raise UserError(_('New transactions must reference an invoice, NAV credentials, the invoice operation, and the invoice XML to send!'))
        transactions = super().create(vals_list)
        # Set name & mimetype on newly-created attachments.
        attachments = self.env['ir.attachment'].search([
            ('res_model', '=', self._name),
            ('res_id', 'in', transactions.ids),
            ('res_field', '=', 'attachment_file'),
        ])
        for attachment in attachments:
            attachment.write({
                'name': self.browse(attachment.res_id)._get_xml_file_name(),
                'mimetype': 'application/xml',
            })
        return transactions

    @check_state_machine(action='upload')
    def upload(self):
        """ Send the invoice XMLs of the transactions in `self` to NAV. """
        with self._acquire_lock():
            # Batch by credentials, with max 100 invoices per batch.
            for __, batch_credentials in groupby(self, lambda t: t.credentials_id):
                for __, batch in groupby(enumerate(batch_credentials), lambda x: x[0] // 100):
                    self.env['l10n_hu_edi.transaction'].browse([t.id for __, t in batch])._upload_single_batch()

    @check_state_machine(action='upload')
    def _upload_single_batch(self):
        for i, transaction in enumerate(self, start=1):
            transaction.index = i

        invoice_operations = [
            {
                'index': transaction.index,
                'operation': transaction.operation,
                'invoice_data': base64.b64decode(transaction.attachment_file),
            }
            for transaction in self
        ]

        try:
            token_result = self.env['l10n_hu_edi.connection'].do_token_exchange(self.credentials_id.sudo())
        except L10nHuEdiConnectionError as e:
            return self.write({
                'state': 'token_error',
                'messages': {
                    'error_title': _('Could not authenticate with NAV. Check your credentials and try again.'),
                    'errors': e.errors,
                },
            })

        self.write({'send_time': fields.Datetime.now()})

        try:
            transaction_code = self.env['l10n_hu_edi.connection'].do_manage_invoice(self.credentials_id.sudo(), token_result['token'], invoice_operations)
        except L10nHuEdiConnectionError as e:
            if e.code == 'timeout':
                return self.write({
                    'state': 'send_timeout',
                    'messages': {
                        'error_title': _('Invoice submission timed out.'),
                        'errors': e.errors,
                    },
                })
            return self.write({
                'state': 'send_error',
                'messages': {
                    'error_title': _('Invoice submission failed.'),
                    'errors': e.errors,
                },
            })

        self.write({
            'state': 'sent',
            'transaction_code': transaction_code,
            'messages': {
                'error_title': _('Invoice submitted, waiting for response.'),
                'errors': [],
            }
        })

    @check_state_machine(action='abort')
    def abort(self):
        """ Mark the transactions in `self` as aborted, so they will not be retried. """
        self.write({'state': 'unsent'})

    @check_state_machine(action='query_status')
    def query_status(self):
        """ Check the NAV status for all transactions in `self`. """
        # We should update all transactions with the same credentials and transaction code at once.
        self |= self.search([
            ('credentials_id', 'in', self.credentials_id.ids),
            ('transaction_code', 'in', self.mapped('transaction_code')),
            ('state', 'in', ['sent', 'query_error']),
        ])

        with self._acquire_lock():
            # Querying status should be grouped by credentials and transaction code
            for __, transactions in groupby(self, lambda t: (t.credentials_id, t.transaction_code)):
                self.env['l10n_hu_edi.transaction'].browse([t.id for t in transactions])._query_status_single_batch()

    @check_state_machine(action='query_status')
    def _query_status_single_batch(self):
        """ Check the NAV status for invoices that share the same transaction code (uploaded in a single batch). """
        transaction_codes = set(self.mapped('transaction_code'))
        if False in transaction_codes or len(transaction_codes) != 1:
            raise UserError('All transactions queried together must share the same transaction code!')

        try:
            invoices_results = self.env['l10n_hu_edi.connection'].do_query_transaction_status(self.credentials_id.sudo(), self[0].transaction_code)
        except L10nHuEdiConnectionError as e:
            return self.write({
                'state': 'query_error',
                'messages': {
                    'error_title': _('The invoice was sent to the NAV, but there was an error querying its status.'),
                    'errors': e.errors,
                },
            })

        for invoice_result in invoices_results:
            transaction = self.filtered(lambda t: str(t.index) == invoice_result['index'])
            if not transaction:
                _logger.error(_('Could not match NAV transaction_code %s, index %s to a transaction in Odoo', self[0].transaction_code, invoice_result['index']))
                continue

            transaction._process_query_transaction_result(invoice_result)

    def _process_query_transaction_result(self, invoice_result):
        def get_errors_from_invoice_result(invoice_result):
            return [
                f'({message["validation_result_code"]}) {message["validation_error_code"]}: {message["message"]}'
                for message in invoice_result.get('business_validation_messages', []) + invoice_result.get('technical_validation_messages', [])
            ]

        self.ensure_one()
        if invoice_result['invoice_status'] in ['RECEIVED', 'PROCESSING', 'SAVED']:
            # The invoice has not been processed yet, so stay in state='sent'.
            if self.state != 'sent':
                # This is triggered if we come from the send_timeout state
                self.write({
                    'state': 'sent',
                    'messages': {
                        'error_title': _('The invoice was sent to the NAV, waiting for reply.'),
                        'errors': [],
                    },
                })

        elif invoice_result['invoice_status'] == 'DONE':
            if not invoice_result['business_validation_messages'] and not invoice_result['technical_validation_messages']:
                self.write({
                    'state': 'confirmed',
                    'messages': {
                        'error_title': _('The invoice was successfully accepted by the NAV.'),
                        'errors': [],
                    },
                })
            else:
                self.write({
                    'state': 'confirmed_warning',
                    'messages': {
                        'error_title': _('The invoice was accepted by the NAV, but warnings were reported. To reverse, create a credit note.'),
                        'errors': get_errors_from_invoice_result(invoice_result),
                    },
                })

        elif invoice_result['invoice_status'] == 'ABORTED':
            self.write({
                'state': 'rejected',
                'messages': {
                    'error_title': _('The invoice was rejected by the NAV.'),
                    'errors': get_errors_from_invoice_result(invoice_result),
                },
            })

        else:
            self.write({
                'state': 'query_error',
                'messages': {
                    'error_title': _('NAV returned a non-standard invoice status: %s', invoice_result['invoice_status']),
                    'errors': [],
                },
            })

    @check_state_machine(action='recover_timeout')
    def recover_timeout(self):
        """ Attempt to recover all transactions in `self` from an upload timeout """
        # Only attempt to recover from a timeout for transactions more than 6 minutes old.
        self = self.filtered(lambda t: t.send_time <= fields.Datetime.now() - timedelta(minutes=6))
        with self._acquire_lock():
            # Group by credentials.
            for __, transactions_by_credentials in groupby(self, lambda t: t.credentials_id):
                # Further group by 7-minute time intervals (more precisely, time intervals which don't have more than 7 minutes between missing invoices)
                time_interval_groups = []
                for transaction in sorted(transactions_by_credentials, key=lambda t: t.send_time):
                    if not time_interval_groups or transaction.send_time >= time_interval_groups[-1][-1].send_time + timedelta(minutes=5):
                        time_interval_groups.append(transaction)
                    else:
                        time_interval_groups[-1] += transaction

                for transactions in time_interval_groups:
                    transactions._recover_timeout_single_batch()

    @check_state_machine(action='recover_timeout')
    def _recover_timeout_single_batch(self):
        datetime_from = min(self.mapped('send_time'))
        datetime_to = max(self.mapped('send_time')) + timedelta(minutes=7)

        page = 1
        available_pages = 1

        while page <= available_pages:
            try:
                transaction_list = self.env['l10n_hu_edi.connection'].do_query_transaction_list(self.credentials_id.sudo(), datetime_from, datetime_to, page)
            except L10nHuEdiConnectionError as e:
                return self.write({
                    'messages': {
                        'error_title': _('Error querying active transactions while attempting timeout recovery.'),
                        'errors': e.errors,
                    },
                })

            transaction_codes_to_query = [
                t['transaction_code']
                for t in transaction_list['transactions']
                if t['username'] == self.credentials_username
                   and t['source'] == 'MGM'
            ]

            for transaction_code in transaction_codes_to_query:
                try:
                    invoices_results = self.env['l10n_hu_edi.connection'].do_query_transaction_status(
                        self.credentials_id.sudo(),
                        transaction_code,
                        return_original_request=True,
                    )
                except L10nHuEdiConnectionError as e:
                    return self.write({
                        'messages': {
                            'error_title': _('Error querying active transactions while attempting timeout recovery.'),
                            'errors': e.errors,
                        },
                    })

                for invoice_result in invoices_results:
                    invoice_name = invoice_result['original_invoice_xml'].findtext('{*}invoiceNumber')
                    matched_transaction = self.filtered(lambda t: t.invoice_id.name == invoice_name)

                    if matched_transaction:
                        # Set the correct transaction code on the matched transaction
                        matched_transaction.transaction_code = transaction_code
                        matched_transaction._process_query_transaction_result(invoice_result)

            available_pages = transaction_list['available_pages']
            page += 1

        # Any transactions that could not be matched to the query results should be regarded as not received by NAV.
        self.filtered(lambda t: t.state == 'send_timeout').write({
            'state': 'to_send',
            'messages': {
                'error_title': _('Sending failed due to time-out.'),
                'errors': [],
            }
        })

    @api.ondelete(at_uninstall=False)
    def _unlink_except_production(self):
        if (True, 'production') in self.mapped(lambda t: (t.is_active, t.credentials_mode)):
            raise UserError(_('Cannot delete active transactions in production mode!'))

    # === Actions === #

    def action_download_file(self):
        """ Download the XML file linked to the document.

        :return: An action to download the attachment.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/l10n_hu_edi.transaction/{self.id}/attachment_file/{self._get_xml_file_name()}?download=true',
        }

    def action_process(self):
        self.ensure_one()
        if self.state in ['to_send', 'token_error']:
            self.upload()
        elif self.state in ['sent', 'query_error']:
            self.query_status()
        elif self.state == 'send_timeout':
            self.recover_timeout()

    # === Helpers === #

    @contextmanager
    def _acquire_lock(self, no_commit=False):
        """ Acquire a write lock on the transactions in self.
            On exit, commit to DB unless no_commit is True.
        """
        if not self:
            yield
            return
        try:
            with self.env.cr.savepoint(flush=False):
                self.env.cr.execute('SELECT * FROM l10n_hu_edi_transaction WHERE id IN %s FOR UPDATE NOWAIT', [tuple(self.ids)])
        except OperationalError as e:
            if e.pgcode == '55P03':
                raise UserError(_('Could not acquire lock on transactions - is another user performing operations on them?'))
            raise
        yield
        if self.env['account.move.send']._can_commit() and not no_commit:
            self.env.cr.commit()

    def _get_xml_file_name(self):
        self.ensure_one()
        return f'{self.invoice_id.name.replace("/", "_")}_{self.id}.xml'
