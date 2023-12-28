from odoo import _, api, fields, models
from odoo.exceptions import UserError

import time
from datetime import timedelta

class AccountMoveSend(models.TransientModel):
    _inherit = 'account.move.send'

    l10n_hu_edi_actionable_errors = fields.Json(
        compute='_compute_l10n_hu_edi_enable_nav_30'
    )
    l10n_hu_edi_enable_nav_30 = fields.Boolean(
        compute='_compute_l10n_hu_edi_enable_nav_30'
    )
    l10n_hu_edi_checkbox_nav_30 = fields.Boolean(
        string='(HU) NAV 3.0',
        compute='_compute_l10n_hu_edi_checkbox_nav_30',
        store=True,
        readonly=False,
    )

    def _get_wizard_values(self):
        # EXTENDS 'account'
        return {
            **super()._get_wizard_values(),
            'l10n_hu_edi_checkbox_nav_30': self.l10n_hu_edi_checkbox_nav_30,
        }

    # -------------------------------------------------------------------------
    # COMPUTE METHODS
    # -------------------------------------------------------------------------

    @api.depends('move_ids')
    def _compute_l10n_hu_edi_enable_nav_30(self):
        for wizard in self:
            enabled_moves = wizard.move_ids.filtered(lambda m: m._l10n_hu_edi_can_process())._origin
            if wizard.mode in ('invoice_single', 'invoice_multi') and enabled_moves:
                wizard.l10n_hu_edi_enable_nav_30 = True
                wizard.l10n_hu_edi_actionable_errors = enabled_moves._l10n_hu_edi_check_invoices()

            else:
                wizard.l10n_hu_edi_enable_nav_30 = False
                wizard.l10n_hu_edi_actionable_errors = False

    @api.depends('l10n_hu_edi_enable_nav_30', 'l10n_hu_edi_actionable_errors')
    def _compute_l10n_hu_edi_checkbox_nav_30(self):
        for wizard in self:
            wizard.l10n_hu_edi_checkbox_nav_30 = wizard.l10n_hu_edi_enable_nav_30 and not wizard.l10n_hu_edi_actionable_errors

    # -------------------------------------------------------------------------
    # BUSINESS ACTIONS
    # -------------------------------------------------------------------------

    @api.model
    def _need_invoice_document(self, invoice):
        # EXTENDS 'account'
        # If the send & print will create a new active transaction, we want to re-generate the PDF at the same time.
        return super()._need_invoice_document(invoice) or invoice._l10n_hu_edi_can_create_transaction()

    @api.model
    def _hook_invoice_document_before_pdf_report_render(self, invoice, invoice_data):
        # EXTENDS 'account'
        super()._hook_invoice_document_before_pdf_report_render(invoice, invoice_data)
        if (
            invoice_data.get('l10n_hu_edi_checkbox_nav_30')
            and invoice._l10n_hu_edi_can_create_transaction()
            and (errors := invoice._l10n_hu_edi_check_invoices())
        ):
            invoice_data['error'] = {
                'error_title': _('Errors occurred while generating the e-invoice to send to NAV:'),
                'errors': [v['message'] for v in errors.values()],
            }

    @api.model
    def _call_web_service_before_invoice_pdf_render(self, invoices_data):
        # EXTENDS 'account'
        super()._call_web_service_before_invoice_pdf_render(invoices_data)

        invoices_hu = self.env['account.move'].browse([
            invoice.id
            for invoice, invoice_data in invoices_data.items()
            if invoice_data.get('l10n_hu_edi_checkbox_nav_30')
               and invoice._l10n_hu_edi_can_process()
        ])

        invoices_data_hu = {
            invoice: invoice_data
            for invoice, invoice_data in invoices_data.items()
            if invoice in invoices_hu
        }

        # STEP 1: Create new transactions for the invoices.
        invoices_hu.filtered(lambda m: m._l10n_hu_edi_can_create_transaction())._l10n_hu_edi_create_transactions()
        if self._can_commit():
            self.env.cr.commit()

        # STEPS 2-4: Transaction Upload / Timeout recovery / Query status
        self._l10n_hu_edi_process_transactions(invoices_data_hu)

        # STEP 5: Any transactions in non-final states should be retried in 10 minutes.
        if any(t.state not in ['confirmed', 'confirmed_warning'] for t in invoices_hu.l10n_hu_edi_active_transaction_id):
            self.env.ref('l10n_hu_edi.ir_cron_retry_non_final_transactions')._trigger(at=fields.Datetime.now() + timedelta(minutes=10))

        # STEP 6: Cleanup old transactions in 'rejected' state.
        invoices_hu._l10n_hu_edi_cleanup_old_transactions()

        # Finally, commit (otherwise, the UserError from error messages will cause a rollback)
        if self._can_commit():
            self.env.cr.commit()

    def _l10n_hu_edi_process_transactions(self, invoices_data):
        invoices = self.env['account.move'].browse([invoice.id for invoice in invoices_data])

        # Pre-emptively acquire write lock on all transactions to be processed
        # Otherwise, we will get a serialization error later
        # (bad, because Odoo will try to retry the entire request, leading to duplicate sending to NAV)
        with invoices.l10n_hu_edi_active_transaction_id._acquire_lock():

            # STEP 2: Call `upload` on the transactions.
            self._l10n_hu_edi_perform_action(invoices_data, 'upload')

            if any(t.state == 'sent' for t in invoices.l10n_hu_edi_active_transaction_id):
                # If any invoices were just sent, wait so that NAV has enough time to process them
                time.sleep(1)

            # STEP 3: Attempt timeout recovery if any transactions need it.
            self._l10n_hu_edi_perform_action(invoices_data, 'recover_timeout')

            # STEP 4: Call `query_status` on the transactions.
            self._l10n_hu_edi_perform_action(invoices_data, 'query_status')

    def _l10n_hu_edi_perform_action(self, invoices_data, action):
        """ Perform a state transition on the invoices in invoices_data,
        and log a message in the chatter / add an error in invoices_data if appropriate. """

        possible_actions = ['upload', 'query_status', 'recover_timeout']
        if not action in possible_actions:
            raise UserError(_('Action must be one of %s', possible_actions))

        invoices = self.env['account.move'].browse([invoice.id for invoice in invoices_data])
        transactions = invoices.l10n_hu_edi_active_transaction_id.filtered(lambda t: t._can_perform(action))

        if action == 'upload':
            transactions.upload()
        elif action == 'query_status':
            transactions.query_status()
        elif action == 'recover_timeout':
            transactions.recover_timeout()

        for transaction in transactions:
            # Visibly show an error in all states where the invoice number is not locked on NAV's side.
            if not transaction.is_active or transaction.state in ('to_send', 'token_error'):
                invoices_data[transaction.invoice_id].update({'error': transaction.messages})
            else:
                formatted_message = self._format_error_html(transaction.messages)
                transaction.invoice_id.with_context(no_new_invoice=True).message_post(body=formatted_message)

    @api.model
    def _l10n_hu_edi_cron_retry_non_final_transactions(self):
        non_final_transactions = self.env['l10n_hu_edi.transaction'].search([
            ('is_active', '=', True),
            ('state', 'not in', ['confirmed', 'confirmed_warning']),
        ])
        invoices_data = {invoice: {} for invoice in non_final_transactions.invoice_id}
        self._l10n_hu_edi_process_transactions(invoices_data)
        invoices_error = {
            invoice: invoice_data
            for invoice, invoice_data in invoices_data.items()
            if invoice_data.get('error')
        }
        if invoices_error:
            self._hook_if_errors(invoices_error, from_cron=True)
        if any(t.is_active and t.state not in ['confirmed', 'confirmed_warning'] for t in non_final_transactions):
            # Trigger cron again in 10 minutes.
            self.env.ref('l10n_hu_edi.ir_cron_retry_non_final_transactions')._trigger(at=fields.Datetime.now() + timedelta(minutes=10))
