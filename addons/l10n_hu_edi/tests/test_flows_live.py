from odoo import tools
from odoo.exceptions import UserError
from odoo.tests.common import tagged
from odoo.addons.account.tests.test_account_move_send import TestAccountMoveSendCommon
from odoo.addons.l10n_hu_edi.tests.common import L10nHuEdiTestCommon
from odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection import L10nHuEdiConnectionError

from unittest import skipIf, mock
import contextlib
from datetime import timedelta

TEST_CRED = {}
last_invoice = {'INV/2024/': 20, 'RINV/2024/': 12}
with contextlib.suppress(ImportError):
    # Private credentials.py. Sorry, we can't share this file.
    from .credentials import TEST_CRED, last_invoice


@tagged('external_l10n', 'post_install', '-at_install', '-standard', 'external')
@skipIf(not TEST_CRED, 'no NAV credentials')
class L10nHuEdiTestFlowsLive(L10nHuEdiTestCommon, TestAccountMoveSendCommon):
    """ Test the Hungarian EDI flows with the NAV test servers. """

    # === Overrides === #

    @classmethod
    def create_edi_credentials(cls):
        # OVERRIDE
        return cls.env['l10n_hu_edi.credentials'].with_context(nav_comm_debug=True).sudo().create([
            {
                'company_id': cls.company_data['company'].id,
                **TEST_CRED,
            }
        ])

    # === Tests === #

    def test_send_invoice_and_credit_note(self):
        invoice = self.create_invoice_simple()
        with self.set_invoice_name(invoice, 'INV/2024/'):
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])

        credit_note = self.create_reversal(invoice)
        with self.set_invoice_name(credit_note, 'RINV/2024/'):
            credit_note.action_post()
            send_and_print = self.create_send_and_print(credit_note, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(credit_note, [{'l10n_hu_edi_state': 'confirmed'}])

            cancel_wizard = self.env['l10n_hu_edi.cancellation'].with_context({"default_invoice_id": credit_note.id}).create({
                'code': 'ERRATIC_DATA',
                'reason': 'Some reason...',
            })
            cancel_wizard.button_request_cancel()
            self.assertRecordValues(credit_note, [{'l10n_hu_edi_state': 'cancel_pending'}])

    def test_send_invoice_complex_huf(self):
        invoice = self.create_invoice_complex_huf()
        with self.set_invoice_name(invoice, 'INV/2024/'):
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])

    def test_send_invoice_complex_eur(self):
        invoice = self.create_invoice_complex_eur()
        with self.set_invoice_name(invoice, 'INV/2024/'):
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])

    def test_timeout_recovery_fail(self):
        invoice = self.create_invoice_simple()
        invoice.action_post()

        send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
        with self.patch_call_nav_endpoint('manageInvoice', make_request=False):
            send_and_print.action_send_and_print()

        self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'send_timeout'}])

        # Set the send time 6 minutes in the past so the timeout recovery mechanism triggers.
        invoice.l10n_hu_edi_send_time -= timedelta(minutes=6)
        send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
        with contextlib.suppress(UserError):
            send_and_print.action_send_and_print()
        self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'to_send'}])

    def test_timeout_recovery_success(self):
        invoice = self.create_invoice_simple()
        with self.set_invoice_name(invoice, 'INV/2024/'):
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            with self.patch_call_nav_endpoint('manageInvoice'):
                send_and_print.action_send_and_print()

            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'send_timeout'}])

            # Set the send time 6 minutes in the past so the timeout recovery mechanism triggers.
            invoice.l10n_hu_edi_send_time -= timedelta(minutes=6)
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])

    def test_cancel_invoice_pending(self):
        invoice, cancel_wizard = self.create_cancel_wizard()
        self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])
        cancel_wizard.button_request_cancel()
        self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'cancel_pending'}])

    # === Helpers === #

    @contextlib.contextmanager
    def set_invoice_name(self, invoice, prefix):
        try:
            last_invoice[prefix] = last_invoice.get(prefix, 0) + 1
            invoice.name = f'{prefix}{last_invoice[prefix]:05}'
            yield
        finally:
            if invoice.l10n_hu_edi_state not in ['confirmed', 'confirmed_warning', 'cancel_sent', 'cancel_pending', 'cancelled']:
                last_invoice[prefix] -= 1
            else:
                with tools.file_open('l10n_hu_edi/tests/credentials.py', 'a') as credentials_file:
                    credentials_file.write(f'last_invoice = {last_invoice}\n')

    @contextlib.contextmanager
    def patch_call_nav_endpoint(self, endpoint, make_request=True):
        """ Patch requests.post in l10n_hu_edi.connection, so that a Timeout is raised on the specified endpoint.
        :param endpoint: the endpoint for which to raise a Timeout
        :param make_request bool: If true, will still make the request before raising the timeout.
        """
        real_call_nav_endpoint = type(self.env['l10n_hu_edi.connection'])._call_nav_endpoint
        def mock_call_nav_endpoint(self, mode, service, data, timeout=20):
            if service == endpoint:
                if make_request:
                    real_call_nav_endpoint(self, mode, service, data, timeout=timeout)
                raise L10nHuEdiConnectionError('Freeze! This is a timeout!', code='timeout')
            else:
                return real_call_nav_endpoint(self, mode, service, data, timeout=timeout)

        with mock.patch.object(type(self.env['l10n_hu_edi.connection']), '_call_nav_endpoint', new=mock_call_nav_endpoint):
            yield
