# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import tools
from odoo.tests.common import tagged
from odoo.exceptions import UserError
from odoo.addons.account.tests.test_account_move_send import TestAccountMoveSendCommon
from odoo.addons.l10n_hu_edi.tests.common import L10nHuEdiTestCommon

import requests
from unittest import mock
from freezegun import freeze_time
import contextlib


@tagged('post_install_l10n', '-at_install', 'post_install')
class L10nHuEdiTestFlowsMocked(L10nHuEdiTestCommon, TestAccountMoveSendCommon):
    """ Test the Hungarian EDI flows using mocked data from the test servers. """
    @classmethod
    def setUpClass(cls, chart_template_ref='hu'):
        with freeze_time('2024-01-25T15:28:53Z'):
            super().setUpClass(chart_template_ref=chart_template_ref)

    def test_send_invoice_and_credit_note(self):
        with self.patch_post(), \
                freeze_time('2024-01-25T15:28:53Z'):
            invoice = self.create_invoice_simple()
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])

            credit_note = self.create_reversal(invoice)
            credit_note.action_post()
            send_and_print = self.create_send_and_print(credit_note, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(credit_note, [{'l10n_hu_edi_state': 'confirmed'}])

    def test_send_invoice_warning(self):
        with tools.file_open('l10n_hu_edi/tests/mocked_requests/queryTransactionStatus_response_warning.xml', 'r') as response_file:
            response_data = response_file.read()
        with self.patch_post({'queryTransactionStatus': response_data}), \
                freeze_time('2024-01-25T15:28:53Z'):
            invoice = self.create_invoice_simple()
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed_warning'}])

    def test_send_invoice_error(self):
        with tools.file_open('l10n_hu_edi/tests/mocked_requests/queryTransactionStatus_response_error.xml', 'r') as response_file:
            response_data = response_file.read()
        with self.patch_post({'queryTransactionStatus': response_data}), \
                freeze_time('2024-01-25T15:28:53Z'):
            invoice = self.create_invoice_simple()
            invoice.action_post()
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            with self.assertRaises(UserError):
                send_and_print.action_send_and_print()

    def test_timeout_recovery_fail(self):
        with freeze_time('2024-01-25T15:28:53Z'), \
                self.patch_post({'manageInvoice': requests.Timeout()}):
            invoice = self.create_invoice_simple()
            invoice.action_post()

            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'send_timeout'}])

        with tools.file_open('l10n_hu_edi/tests/mocked_requests/queryTransactionStatus_response_original.xml', 'r') as response_file:
            response_data = response_file.read()
        # Advance 10 minutes so the timeout recovery mechanism triggers.
        with freeze_time('2024-01-25T15:38:53Z'), \
                self.patch_post({'queryTransactionStatus': response_data}):
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            with contextlib.suppress(UserError):
                send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'to_send'}])

    def test_timeout_recovery_success(self):
        with freeze_time('2024-01-25T15:28:53Z'), \
                self.patch_post({'manageInvoice': requests.Timeout()}):
            invoice = self.create_invoice_simple()
            invoice.name = 'INV/2024/00999'  # This matches the invoice name in the XML returned by queryTransactionStatus.
            invoice.action_post()

            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'send_timeout'}])

        # This returns an original XML with name INV/2024/00999
        with tools.file_open('l10n_hu_edi/tests/mocked_requests/queryTransactionStatus_response_original.xml', 'r') as response_file:
            response_data = response_file.read()

        # Advance 10 minutes so the timeout recovery mechanism triggers.
        with freeze_time('2024-01-25T15:38:53Z'), \
                self.patch_post({'queryTransactionStatus': response_data}):
            send_and_print = self.create_send_and_print(invoice, l10n_hu_edi_enable_nav_30=True)
            send_and_print.action_send_and_print()
            self.assertRecordValues(invoice, [{'l10n_hu_edi_state': 'confirmed'}])

    # === Helpers === #

    @contextlib.contextmanager
    def patch_post(self, responses=None):
        """ Patch requests.post in l10n_hu_edi.connection.
        :param responses: If specified, a dict {service: response} that gives, for any service,
                          bytes that should be served as response data, or an Exception that should be raised.
                          Otherwise, will use the default responses stored under
                          mocked_requests/{service}_response.xml
        """
        def mock_post(url, data, headers, timeout=None):
            prod_url = 'https://api.onlineszamla.nav.gov.hu/invoiceService/v3'
            demo_url = 'https://api-test.onlineszamla.nav.gov.hu/invoiceService/v3'
            mocked_requests = ['manageInvoice', 'queryTaxpayer', 'tokenExchange', 'queryTransactionStatus', 'queryTransactionList']

            base_url, __, service = url.rpartition('/')
            if base_url not in (prod_url, demo_url) or service not in mocked_requests:
                self.fail(f'Invalid POST url: {url}')

            with tools.file_open(f'l10n_hu_edi/tests/mocked_requests/{service}_request.xml', 'rb') as expected_request_file:
                self.assertXmlTreeEqual(
                    self.get_xml_tree_from_string(data),
                    self.get_xml_tree_from_string(expected_request_file.read()),
                )

            mock_response = mock.Mock(spec=requests.Response)
            mock_response.status_code = 200
            mock_response.headers = ''

            if responses and service in responses:
                if isinstance(responses[service], Exception):
                    raise responses[service]
                mock_response.text = responses[service]
            else:
                with tools.file_open(f'l10n_hu_edi/tests/mocked_requests/{service}_response.xml', 'r') as response_file:
                    mock_response.text = response_file.read()
            return mock_response

        with mock.patch('odoo.addons.l10n_hu_edi.models.l10n_hu_edi_connection.requests.post', side_effect=mock_post, autospec=True):
            yield
