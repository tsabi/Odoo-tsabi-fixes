# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, api, _, release
from odoo.tools import cleanup_xml_node

from cryptography.hazmat.primitives import hashes, ciphers
from base64 import b64decode, b64encode
import datetime
import dateutil.parser
import uuid
import requests
import binascii
import contextlib
from lxml import etree

import logging

_logger = logging.getLogger(__name__)


def format_bool(value):
    return 'true' if value else 'false'

def format_timestamp(value):
    return value.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

class L10nHuEdiConnectionError(Exception):
    def __init__(self, errors, code=None):
        super().__init__()
        if not isinstance(errors, list):
            errors = [errors]
        self.errors = errors
        self.code = code

    def __str__(self):
        return '\n'.join(self.errors)


class AES_ECB_Cipher(object):
    """
    Usage:
        c = AES_ECB_Cipher('password').encrypt('message')
        m = AES_ECB_Cipher('password').decrypt(c)
    Tested under Python 3.10.10 and cryptography==3.4.8.
    """

    def __init__(self, key):
        self.bs = int(ciphers.algorithms.AES.block_size / 8)
        self.key = key.encode()

    def encrypt(self, message):
        encryptor = self._get_cipher().encryptor()
        ct = encryptor.update(self._pad(message).encode()) + encryptor.finalize()
        return b64encode(ct).decode('utf-8')

    def decrypt(self, enc):
        decryptor = self._get_cipher().decryptor()
        with contextlib.suppress(binascii.Error):
            enc = b64decode(enc)
        ct = decryptor.update(enc) + decryptor.finalize()
        return self._unpad(ct).decode('utf-8')

    def _get_cipher(self):
        return ciphers.Cipher(ciphers.algorithms.AES(self.key), ciphers.modes.ECB())

    def _pad(self, s):
        return s + (self.bs - len(s) % self.bs) * chr(self.bs - len(s) % self.bs)

    @staticmethod
    def _unpad(s):
        return s[: -ord(s[len(s) - 1:])]


class L10nHuEdiConnection(models.AbstractModel):
    _name = 'l10n_hu_edi.connection'
    _description = 'Methods to call NAV API endpoints'

    # === API-calling methods === #

    @api.model
    def do_token_exchange(self, credentials):
        """ Request a token for invoice submission.
        :param credentials: a dictionary {'vat': str, 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :return: a dictionary {'token': str, 'token_validity_to': datetime.datetime}
        :raise: L10nHuEdiConnectionError
        """
        template_values = self._get_header_values(credentials)
        request_data = self.env['ir.qweb']._render('l10n_hu_edi.token_exchange_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        status_code, response_xml = self._call_nav_endpoint(credentials['mode'], 'tokenExchange', request_data)
        self._parse_error_response(status_code, response_xml)

        encoded_token = response_xml.findtext('{*}encodedExchangeToken')
        token_validity_to = response_xml.findtext('{*}tokenValidityTo')
        try:
            # Convert into a naive UTC datetime, since Odoo can't store timezone-aware datetimes
            token_validity_to = dateutil.parser.isoparse(token_validity_to).astimezone(datetime.timezone.utc).replace(tzinfo=None)
        except ValueError:
            _logger.warning('Could not parse token validity end timestamp!')
            token_validity_to = datetime.datetime.utcnow() + datetime.timedelta(minutes=5)

        if not encoded_token:
            raise L10nHuEdiConnectionError(_('Missing token in response from NAV.'))

        try:
            token = AES_ECB_Cipher(credentials['replacement_key']).decrypt(encoded_token)
        except ValueError:
            raise L10nHuEdiConnectionError(_('NAV Communication: XML Parse Error during decryption of ExchangeToken'))

        return {'token': token, 'token_validity_to': token_validity_to}

    @api.model
    def do_manage_invoice(self, credentials, token, invoice_operations):
        """ Submit one or more invoices.
        :param credentials: a dictionary {'vat': str, 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :param token: a token obtained via `do_token_exchange`
        :param invoice_operations: a list of dictionaries:
            {
                'index': <index given to invoice>,
                'operation': 'CREATE' or 'MODIFY',
                'invoice_data': <XML data of the invoice as bytes>
            }
        :return str: The transaction code issued by NAV.
        :raise: L10nHuEdiConnectionError, with code='timeout' if a timeout occurred.
        """
        template_values = {
            'exchangeToken': token,
            'compressedContent': False,
            'invoices': [],
        }
        invoice_hashes = []
        for invoice_operation in invoice_operations:
            invoice_data_b64 = b64encode(invoice_operation['invoice_data']).decode('utf-8')
            invoice_values = {
                'index': invoice_operation['index'],
                'invoiceOperation': invoice_operation['operation'],
                'invoiceData': invoice_data_b64,
            }
            template_values['invoices'].append(invoice_values)
            invoice_hashes.append(self._calculate_invoice_hash(invoice_operation['operation'] + invoice_data_b64))

        template_values.update(self._get_header_values(credentials, invoice_hashs=invoice_hashes))

        request_data = self.env['ir.qweb']._render('l10n_hu_edi.manage_invoice_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        status_code, response_xml = self._call_nav_endpoint(credentials['mode'], 'manageInvoice', request_data, timeout=60)
        self._parse_error_response(status_code, response_xml)

        transaction_code = response_xml.findtext('{*}transactionId')
        if not transaction_code:
            raise L10nHuEdiConnectionError(_('Invoice Upload failed: NAV did not return a Transaction ID.'))

        return transaction_code

    @api.model
    def do_query_transaction_status(self, credentials, transaction_code, return_original_request=False):
        """ Query the status of a transaction.
        :param credentials: a dictionary {'vat': str, 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :param transaction_code: the code of the transaction to query
        :param return_original_request: whether to request the submitted invoice XML.
        :return: a list of dicts {'index': str, 'invoice_status': str, 'business_validation_messages', 'technical_validation_messages'}
        :raise: L10nHuEdiConnectionError
        """
        template_values = {
            **self._get_header_values(credentials),
            'transactionId': transaction_code,
            'returnOriginalRequest': return_original_request,
        }
        request_data = self.env['ir.qweb']._render('l10n_hu_edi.query_transaction_status_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        status_code, response_xml = self._call_nav_endpoint(credentials['mode'], 'queryTransactionStatus', request_data)
        self._parse_error_response(status_code, response_xml)

        invoices_results = []
        for invoice_xml in response_xml.findall('{*}processingResults/{*}processingResult'):
            invoice_result = {
                'index': invoice_xml.findtext('{*}index'),
                'invoice_status': invoice_xml.findtext('{*}invoiceStatus'),
                'business_validation_messages': [],
                'technical_validation_messages': [],
            }
            for message_xml in invoice_xml.findall('{*}businessValidationMessages'):
                invoice_result['business_validation_messages'].append({
                    'validation_result_code': message_xml.findtext('{*}validationResultCode'),
                    'validation_error_code': message_xml.findtext('{*}validationErrorCode'),
                    'message': message_xml.findtext('{*}message'),
                })
            for message_xml in invoice_xml.findall('{*}technicalValidationMessages'):
                invoice_result['technical_validation_messages'].append({
                    'validation_result_code': message_xml.findtext('{*}validationResultCode'),
                    'validation_error_code': message_xml.findtext('{*}validationErrorCode'),
                    'message': message_xml.findtext('{*}message'),
                })
            if return_original_request:
                try:
                    original_invoice_xml = etree.fromstring(b64decode(invoice_xml.findtext('{*}originalRequest')))
                except binascii.Error as e:
                    raise L10nHuEdiConnectionError(str(e))
                except etree.ParserError as e:
                    raise L10nHuEdiConnectionError(str(e))

                invoice_result['original_invoice_xml'] = original_invoice_xml

            invoices_results.append(invoice_result)

        return invoices_results

    @api.model
    def do_query_transaction_list(self, credentials, datetime_from, datetime_to, page=1):
        """ Query the transactions that were submitted in a given time interval.
        :param credentials: a dictionary {'vat': str, 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :param datetime_from: start of the time interval to query
        :param datetime_to: end of the time interval to query
        :return: a dict {'transaction_codes': list[str], 'available_pages': int}
        :raise: L10nHuEdiConnectionError
        """

        template_values = {
            **self._get_header_values(credentials),
            'page': page,
            'dateTimeFrom': format_timestamp(datetime_from),
            'dateTimeTo': format_timestamp(datetime_to),
        }
        request_data = self.env['ir.qweb']._render('l10n_hu_edi.query_transaction_list_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        status_code, response_xml = self._call_nav_endpoint(credentials['mode'], 'queryTransactionList', request_data)
        self._parse_error_response(status_code, response_xml)

        available_pages = response_xml.findtext('{*}transactionListResult/{*}availablePage')
        try:
            available_pages = int(available_pages)
        except ValueError:
            available_pages = 1

        transactions = [
            {
                'transaction_code': transaction_xml.findtext('{*}transactionId'),
                'username': transaction_xml.findtext('{*}insCusUser'),
                'source': transaction_xml.findtext('{*}source'),
            }
            for transaction_xml in response_xml.findall('{*}transactionListResult/{*}transaction')
        ]

        return {"transactions": transactions, "available_pages": available_pages}

    # === Helpers: XML generation === #

    def _get_header_values(self, credentials, invoice_hashs=None):
        timestamp = datetime.datetime.utcnow()
        request_id = ('ODOO' + str(uuid.uuid4()).replace('-', ''))[:30]
        request_signature = self._calculate_request_signature(credentials['signature_key'], request_id, timestamp, invoice_hashs=invoice_hashs)
        odoo_version = release.version
        module_version = self.env['ir.module.module'].get_module_info('l10n_hu_edi').get('version').replace('saas~', '').replace('.', '')

        return {
            'requestId': request_id,
            'timestamp': format_timestamp(timestamp),
            'login': credentials['username'],
            'passwordHash': self._calculate_password_hash(credentials['password']),
            'taxNumber': credentials['vat'][:8],
            'requestSignature': request_signature,
            'softwareId': f'BE477472701-{module_version}'[:18],
            'softwareName': 'Odoo Enterprise',
            'softwareOperation': 'ONLINE_SERVICE',
            'softwareMainVersion': odoo_version,
            'softwareDevName': 'Odoo SA',
            'softwareDevContact': 'andu@odoo.com',
            'softwareDevCountryCode': 'BE',
            'softwareDevTaxNumber': '477472701',
            'format_bool': format_bool,
        }

    def _calculate_password_hash(self, password):
        digest = hashes.Hash(hashes.SHA512())
        digest.update(password.encode())
        return digest.finalize().hex().upper()

    def _calculate_invoice_hash(self, value):
        digest = hashes.Hash(hashes.SHA3_512())
        digest.update(value.encode())
        return digest.finalize().hex().upper()

    def _calculate_request_signature(self, key_sign, reqid, reqdate, invoice_hashs=None):
        strings = [reqid, reqdate.strftime('%Y%m%d%H%M%S'), key_sign]

        # merge the invoice CRCs if we got
        if invoice_hashs:
            strings += invoice_hashs

        # return back the uppered hexdigest
        return self._calculate_invoice_hash(''.join(strings))

    # === Helpers: HTTP Post === #

    def _call_nav_endpoint(self, mode, service, data, timeout=20):
        if mode == 'production':
            url = 'https://api.onlineszamla.nav.gov.hu/invoiceService/v3/'
        elif mode == 'test':
            url = 'https://api-test.onlineszamla.nav.gov.hu/invoiceService/v3/'
        else:
            raise L10nHuEdiConnectionError(_('Mode should be Production or Test!'))

        if service in ['tokenExchange', 'queryTaxpayer', 'manageInvoice', 'queryTransactionStatus', 'queryTransactionList']:
            url += service
        else:
            raise L10nHuEdiConnectionError(_('Service should be one of tokenExchange, queryTaxpayer, manageInvoice, queryTransactionStatus, queryTransactionList!'))

        headers = {'content-type': 'application/xml', 'accept': 'application/xml'}
        if self.env.context.get('nav_comm_debug'):
            _logger.warning('REQUEST: POST: %s==>headers:%s\ndata:%s', str(url), str(headers), str(data))

        try:
            response_object = requests.post(url, data=data, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            if isinstance(e, requests.Timeout):
                raise L10nHuEdiConnectionError(
                    _('Connection to NAV servers timed out.'),
                    code='timeout',
                )
            raise L10nHuEdiConnectionError(str(e))

        if self.env.context.get('nav_comm_debug'):
            _logger.warning(
                'RESPONSE: status_code:%s\nheaders:%s\ndata:%s',
                response_object.status_code,
                response_object.headers,
                response_object.text,
            )

        if response_object.status_code != 200:
            raise L10nHuEdiConnectionError(_('NAV returned status code %s: %s', response_object.status_code, response_object.text))

        return response_object.status_code, etree.fromstring(response_object.text.encode())

    # === Helpers: Response parsing === #

    def _parse_error_response(self, status_code, response_xml):
        if response_xml.tag == '{http://schemas.nav.gov.hu/OSA/3.0/api}GeneralErrorResponse':
            errors = []
            for message_xml in response_xml.findall('{*}technicalValidationMessages'):
                message = message_xml.findtext('{*}message')
                result_code = message_xml.findtext('{*}validationResultCode')
                error_code = message_xml.findtext('{*}validationErrorCode')
                errors.append(f'{message} ({result_code},{error_code})')
            raise L10nHuEdiConnectionError(errors)

        if response_xml.tag == '{http://schemas.nav.gov.hu/OSA/3.0/api}GeneralExceptionResponse':
            message = response_xml.findtext('{*}message')
            code = response_xml.findtext('{*}errorCode')
            raise L10nHuEdiConnectionError(f'{message} ({code})')

        func_code = response_xml.findtext('{*}result/{*}funcCode')
        if func_code != 'OK':
            raise L10nHuEdiConnectionError(_('NAV replied with non-OK funcCode: %s', func_code))
