# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, api, _, release
from odoo.tools import cleanup_xml_node

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
import dateutil.parser
import uuid
import requests
import binascii
from lxml import etree

import logging

_logger = logging.getLogger(__name__)


def format_bool(value):
    return 'true' if value else 'false'

def format_timestamp(value):
    return value.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

class L10nHuEdiConnectionError(Exception):
    def __init__(self, errors, code=None):
        if not isinstance(errors, list):
            errors = [errors]
        self.errors = errors
        self.code = code
        super().__init__('\n'.join(errors))

class L10nHuEdiConnection(models.AbstractModel):
    _name = 'l10n_hu_edi.connection'
    _description = 'Methods to call NAV API endpoints'

    # === API-calling methods === #

    @api.model
    def _do_token_exchange(self, credentials):
        """ Request a token for invoice submission.
        :param credentials: a dictionary {'vat': str, 'mode': 'production' || 'test', 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :return: a dictionary {'token': str, 'token_validity_to': datetime}
        :raise: L10nHuEdiConnectionError
        """
        def decrypt_aes128(key, encrypted_token):
            """ Decrypt AES-128 encrypted bytes.
            :param key bytes: the 128-bit key
            :param encrypted_token bytes: the bytes to decrypt
            :return: the decrypted bytes
            """
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            decrypted_token = decryptor.update(encrypted_token) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            unpadded_token = unpadder.update(decrypted_token) + unpadder.finalize()
            return unpadded_token

        template_values = self._get_header_values(credentials)
        request_data = self.env['ir.qweb']._render('l10n_hu_edi.token_exchange_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        response_xml = self._call_nav_endpoint(credentials['mode'], 'tokenExchange', request_data)
        self._parse_error_response(response_xml)

        encrypted_token = response_xml.findtext('{*}encodedExchangeToken')
        token_validity_to = response_xml.findtext('{*}tokenValidityTo')
        try:
            # Convert into a naive UTC datetime, since Odoo can't store timezone-aware datetimes
            token_validity_to = dateutil.parser.isoparse(token_validity_to).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            _logger.warning('Could not parse token validity end timestamp!')
            token_validity_to = datetime.utcnow() + timedelta(minutes=5)

        if not encrypted_token:
            raise L10nHuEdiConnectionError(_('Missing token in response from NAV.'))

        try:
            token = decrypt_aes128(credentials['replacement_key'].encode(), b64decode(encrypted_token.encode())).decode()
        except ValueError:
            raise L10nHuEdiConnectionError(_('Error during decryption of ExchangeToken.'))

        return {'token': token, 'token_validity_to': token_validity_to}

    @api.model
    def _do_manage_invoice(self, credentials, token, invoice_operations):
        """ Submit one or more invoices.
        :param credentials: a dictionary {'vat': str, 'mode': 'production' || 'test', 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :param token: a token obtained via `_do_token_exchange`
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
            template_values['invoices'].append({
                'index': invoice_operation['index'],
                'invoiceOperation': invoice_operation['operation'],
                'invoiceData': invoice_data_b64,
            })
            invoice_hashes.append(self._calculate_invoice_hash(invoice_operation['operation'] + invoice_data_b64))

        template_values.update(self._get_header_values(credentials, invoice_hashs=invoice_hashes))

        request_data = self.env['ir.qweb']._render('l10n_hu_edi.manage_invoice_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        response_xml = self._call_nav_endpoint(credentials['mode'], 'manageInvoice', request_data, timeout=60)
        self._parse_error_response(response_xml)

        transaction_code = response_xml.findtext('{*}transactionId')
        if not transaction_code:
            raise L10nHuEdiConnectionError(_('Invoice Upload failed: NAV did not return a Transaction ID.'))

        return transaction_code

    @api.model
    def _do_query_transaction_status(self, credentials, transaction_code, return_original_request=False):
        """ Query the status of a transaction.
        :param credentials: a dictionary {'vat': str, 'mode': 'production' || 'test', 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
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

        response_xml = self._call_nav_endpoint(credentials['mode'], 'queryTransactionStatus', request_data)
        self._parse_error_response(response_xml)

        results = {
            'processing_results': [],
            'annulment_status': response_xml.findtext('{*}processingResults/{*}annulmentData/{*}annulmentVerificationStatus'),
        }
        for processing_result_xml in response_xml.findall('{*}processingResults/{*}processingResult'):
            processing_result = {
                'index': processing_result_xml.findtext('{*}index'),
                'invoice_status': processing_result_xml.findtext('{*}invoiceStatus'),
                'business_validation_messages': [],
                'technical_validation_messages': [],
            }
            for message_xml in processing_result_xml.findall('{*}businessValidationMessages'):
                processing_result['business_validation_messages'].append({
                    'validation_result_code': message_xml.findtext('{*}validationResultCode'),
                    'validation_error_code': message_xml.findtext('{*}validationErrorCode'),
                    'message': message_xml.findtext('{*}message'),
                })
            for message_xml in processing_result_xml.findall('{*}technicalValidationMessages'):
                processing_result['technical_validation_messages'].append({
                    'validation_result_code': message_xml.findtext('{*}validationResultCode'),
                    'validation_error_code': message_xml.findtext('{*}validationErrorCode'),
                    'message': message_xml.findtext('{*}message'),
                })
            if return_original_request:
                try:
                    original_file = b64decode(processing_result_xml.findtext('{*}originalRequest'))
                    original_xml = etree.fromstring(original_file)
                except binascii.Error as e:
                    raise L10nHuEdiConnectionError(str(e))
                except etree.ParserError as e:
                    raise L10nHuEdiConnectionError(str(e))

                processing_result.update({
                    'original_file': original_file.decode(),
                    'original_xml': original_xml,
                })

            results['processing_results'].append(processing_result)

        return results

    @api.model
    def _do_query_transaction_list(self, credentials, datetime_from, datetime_to, page=1):
        """ Query the transactions that were submitted in a given time interval.
        :param credentials: a dictionary {'vat': str, 'mode': 'production' || 'test', 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
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

        response_xml = self._call_nav_endpoint(credentials['mode'], 'queryTransactionList', request_data)
        self._parse_error_response(response_xml)

        available_pages = response_xml.findtext('{*}transactionListResult/{*}availablePage')
        try:
            available_pages = int(available_pages)
        except ValueError:
            available_pages = 1

        transactions = [
            {
                'transaction_code': transaction_xml.findtext('{*}transactionId'),
                'annulment': transaction_xml.findtext('{*}technicalAnnulment') == 'true',
                'username': transaction_xml.findtext('{*}insCusUser'),
                'source': transaction_xml.findtext('{*}source'),
                'send_time': datetime.fromisoformat(transaction_xml.findtext('{*}insDate').replace('Z', '')),
            }
            for transaction_xml in response_xml.findall('{*}transactionListResult/{*}transaction')
        ]

        return {"transactions": transactions, "available_pages": available_pages}

    @api.model
    def _do_manage_annulment(self, credentials, token, annulment_operations):
        """ Request technical annulment of one or more invoices.
        :param credentials: a dictionary {'vat': str, 'mode': 'production' || 'test', 'username': str, 'password': str, 'signature_key': str, 'replacement_key': str}
        :param token: a token obtained via `_do_token_exchange`
        :param annulment_operations: a list of dictionaries:
            {
                'index': <index given to invoice>,
                'annulmentReference': the name of the invoice to annul,
                'annulmentCode': one of ('ERRATIC_DATA', 'ERRATIC_INVOICE_NUMBER', 'ERRATIC_INVOICE_ISSUE_DATE', 'ERRATIC_ELECTRONIC_HASH_VALUE'),
                'annulmentReason': a plain-text explanation of the reason for annulment,
            }
        :return str: The transaction code issued by NAV.
        :raise: L10nHuEdiConnectionError, with code='timeout' if a timeout occurred.
        """
        template_values = {
            'exchangeToken': token,
            'annulments': []
        }

        annulment_hashes = []
        for annulment_operation in annulment_operations:
            annulment_operation['annulmentTimestamp'] = format_timestamp(datetime.utcnow())
            annulment_data = self.env['ir.qweb']._render('l10n_hu_edi.invoice_annulment', annulment_operation)
            annulment_data_b64 = b64encode(annulment_data.encode()).decode('utf-8')
            template_values['annulments'].append({
                'index': annulment_operation['index'],
                'annulmentOperation': 'ANNUL',
                'invoiceAnnulment': annulment_data_b64,
            })
            annulment_hashes.append(self._calculate_invoice_hash('ANNUL' + annulment_data_b64))

        template_values.update(self._get_header_values(credentials, invoice_hashs=annulment_hashes))

        request_data = self.env['ir.qweb']._render('l10n_hu_edi.manage_annulment_request', template_values)
        request_data = etree.tostring(cleanup_xml_node(request_data, remove_blank_nodes=False), xml_declaration=True, encoding='UTF-8')

        response_xml = self._call_nav_endpoint(credentials['mode'], 'manageAnnulment', request_data, timeout=60)
        self._parse_error_response(response_xml)

        transaction_code = response_xml.findtext('{*}transactionId')
        if not transaction_code:
            raise L10nHuEdiConnectionError(_('Invoice Upload failed: NAV did not return a Transaction ID.'))

        return transaction_code

    # === Helpers: XML generation === #

    def _get_header_values(self, credentials, invoice_hashs=None):
        timestamp = datetime.utcnow()
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

        services = ['tokenExchange', 'queryTaxpayer', 'manageInvoice', 'queryTransactionStatus', 'queryTransactionList', 'manageAnnulment']
        if service in services:
            url += service
        else:
            raise L10nHuEdiConnectionError(_('Service should be one of %s!', ', '.join(services)))

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

        try:
            response_xml = etree.fromstring(response_object.text.encode())
        except etree.ParseError:
            raise L10nHuEdiConnectionError(_('Invalid NAV response!'))

        return response_xml

    # === Helpers: Response parsing === #

    def _parse_error_response(self, response_xml):
        if response_xml.tag == '{http://schemas.nav.gov.hu/OSA/3.0/api}GeneralErrorResponse':
            errors = []
            for message_xml in response_xml.findall('{*}technicalValidationMessages'):
                message = message_xml.findtext('{*}message')
                error_code = message_xml.findtext('{*}validationErrorCode')
                errors.append(f'{error_code}: {message}')
            raise L10nHuEdiConnectionError(errors)

        if response_xml.tag == '{http://schemas.nav.gov.hu/OSA/3.0/api}GeneralExceptionResponse':
            message = response_xml.findtext('{*}message')
            code = response_xml.findtext('{*}errorCode')
            raise L10nHuEdiConnectionError(f'{code}: {message}')

        func_code = response_xml.findtext('{*}result/{*}funcCode')
        if func_code != 'OK':
            raise L10nHuEdiConnectionError(_('NAV replied with non-OK funcCode: %s', func_code))
