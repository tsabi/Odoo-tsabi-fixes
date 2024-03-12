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
            enabled_moves = wizard.move_ids.filtered(
                lambda m: m._l10n_hu_edi_can_process(['start', 'upload', 'query_status', 'recover_timeout'])
            )._origin
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
        # If the send & print will create a new NAV 3.0 XML, we want to re-generate the PDF at the same time.
        return super()._need_invoice_document(invoice) or invoice._l10n_hu_edi_can_process(['start'])

    @api.model
    def _hook_invoice_document_before_pdf_report_render(self, invoice, invoice_data):
        # EXTENDS 'account'
        super()._hook_invoice_document_before_pdf_report_render(invoice, invoice_data)
        if (
            invoice_data.get('l10n_hu_edi_checkbox_nav_30')
            and invoice._l10n_hu_edi_can_process(['start'])
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
               and invoice._l10n_hu_edi_can_process(['start', 'upload', 'query_status', 'recover_timeout'])
        ])

        invoices_data_hu = {
            invoice: invoice_data
            for invoice, invoice_data in invoices_data.items()
            if invoice in invoices_hu
        }

        # Pre-emptively acquire write lock on all invoices to be processed
        # Otherwise, we will get a serialization error later
        # (bad, because Odoo will try to retry the entire request, leading to duplicate sending to NAV)
        with invoices_hu._l10n_hu_edi_acquire_lock():
            # STEP 1: Generate the XML for the invoices.
            invoices_hu.filtered(lambda m: m._l10n_hu_edi_can_process(['start']))._l10n_hu_edi_start()

            # STEPS 2-4: Upload / Timeout recovery / Query status
            self._l10n_hu_edi_process_pending(invoices_data_hu)

            # STEP 5: Any pending invoices should be retried in 10 minutes.
            if any(m.l10n_hu_edi_state not in [False, 'confirmed', 'confirmed_warning', 'rejected'] for m in invoices_hu):
                self.env.ref('l10n_hu_edi.ir_cron_retry_pending')._trigger(at=fields.Datetime.now() + timedelta(minutes=10))


    def _l10n_hu_edi_process_pending(self, invoices_data):
        invoices = self.env['account.move'].browse([invoice.id for invoice in invoices_data])
        # STEP 2: Call `upload` on the invoices.
        self._l10n_hu_edi_perform_action(invoices_data, 'upload')

        if any(m.l10n_hu_edi_state == 'sent' for m in invoices):
            # If any invoices were just sent, wait so that NAV has enough time to process them
            time.sleep(2)

        # STEP 3: Attempt timeout recovery if any invoices need it.
        self._l10n_hu_edi_perform_action(invoices_data, 'recover_timeout')

        # STEP 4: Call `query_status` on the invoices.
        self._l10n_hu_edi_perform_action(invoices_data, 'query_status')

    def _l10n_hu_edi_perform_action(self, invoices_data, action):
        """ Perform a state transition on the invoices in invoices_data,
        and log a message in the chatter / add an error in invoices_data if appropriate. """

        possible_actions = ['upload', 'query_status', 'recover_timeout']
        if not action in possible_actions:
            raise UserError(_('Action must be one of %s', possible_actions))

        invoices = self.env['account.move'].browse([invoice.id for invoice in invoices_data]).filtered(lambda m: m._l10n_hu_edi_can_process([action]))

        if action == 'upload':
            invoices._l10n_hu_edi_upload()
        elif action == 'query_status':
            invoices._l10n_hu_edi_query_status()
        elif action == 'recover_timeout':
            invoices._l10n_hu_edi_recover_timeout()

        for invoice in invoices:
            if invoice.l10n_hu_edi_messages.get('blocking_level') == 'error':
                invoices_data[invoice].update({'error': invoice.l10n_hu_edi_messages})
            else:
                formatted_message = self._format_error_html(invoice.l10n_hu_edi_messages)
                invoice.with_context(no_new_invoice=True).message_post(body=formatted_message)

    @api.model
    def _l10n_hu_edi_cron_retry_pending(self):
        invoices_pending = self.env['account.move'].search([
            ('l10n_hu_edi_state', 'not in', [False, 'confirmed', 'confirmed_warning', 'rejected']),
        ])
        invoices_data = {invoice: {} for invoice in invoices_pending}

        # Pre-emptively acquire write lock
        with invoices_pending._l10n_hu_edi_acquire_lock():
            self._l10n_hu_edi_process_pending(invoices_data)

        invoices_error = {
            invoice: invoice_data
            for invoice, invoice_data in invoices_data.items()
            if invoice_data.get('error')
        }
        if invoices_error:
            self._hook_if_errors(invoices_error, from_cron=True)
        if any(m.state not in [False, 'confirmed', 'confirmed_warning', 'rejected'] for m in invoices_pending):
            # Trigger cron again in 10 minutes.
            self.env.ref('l10n_hu_edi.ir_cron_retry_pending')._trigger(at=fields.Datetime.now() + timedelta(minutes=10))
