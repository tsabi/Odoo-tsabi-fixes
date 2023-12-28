# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

{
    'name': 'Hungary E-invoicing - Sales',
    'version': '1.0.0',
    'category': 'Accounting/Localizations/EDI',
    'countries': ['hu'],
    'author': 'OdooTech Zrt., BDSC Business Consulting Kft. & Odoo S.A.',
    'description': """
This module enables advance and final invoices created by the Sales application to be correctly handled when issuing E-invoices in Hungary.
    """,
    'website': 'https://www.odootech.hu',
    'depends': [
        'l10n_hu_edi',
        'sale',
    ],
    'auto_install': True,
    'license': 'LGPL-3',
}
