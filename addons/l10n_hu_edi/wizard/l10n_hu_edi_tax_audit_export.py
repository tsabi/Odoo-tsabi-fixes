# Part of Odoo. See LICENSE file for full copyright and licensing details.

import base64
import contextlib
import io
import zipfile
from functools import partial
from markupsafe import Markup
from datetime import date, datetime

from odoo import api, models, fields, _
from odoo.exceptions import UserError
from odoo.tools import float_round


class L10nHuEdiTaxAuditExport(models.TransientModel):
    _name = 'l10n_hu_edi.tax_audit_export'
    _description = 'Tax audit export - Adóhatósági Ellenőrzési Adatszolgáltatás'

    selection_mode = fields.Selection(
        string='Selection mode',
        selection=[
            ('date', 'By date'),
            ('name', 'By serial number'),
        ],
        default='date',
    )
    date_from = fields.Date(
        string='Date From'
    )
    date_to = fields.Date(
        string='Date To'
    )
    name_from = fields.Char(
        string='Name From'
    )
    name_to = fields.Char(
        string='Name To'
    )
    filename = fields.Char(
        string='File name',
        compute='_compute_filename'
    )
    export_file = fields.Binary(
        string='Generated File',
        readonly=True
    )
    use_cdata = fields.Boolean("Render with CDATA marker", default=True)
    export_format = fields.Selection([("nav3.0", "NAV 3.0 XML"), ("aee", "AEE XML")], string="Export format", required=True, default="nav3.0")

    @api.depends('selection_mode', 'date_from', 'date_to', 'name_from', 'name_to')
    def _compute_filename(self):
        domain = [
            ('move_type', 'in', ('out_invoice', 'out_refund')),
            ('state', '=', 'posted'),
            ('country_code', '=', 'HU'),
        ]
        if self.selection_mode == 'date':
            date_from = self.date_from
            if not date_from:
                first_invoice = self.env['account.move'].search(domain, order='date', limit=1)
                date_from = first_invoice.date
            date_to = self.date_to or fields.Date.today()
            self.filename = f'export_{date_from}_{date_to}.zip'

        else:
            name_from = self.name_from
            if not name_from:
                first_invoice = self.env['account.move'].search(domain, order='name', limit=1)
                name_from = first_invoice.name
            name_to = self.name_to
            if not name_to:
                last_invoice = self.env['account.move'].search(domain, order='name desc', limit=1)
                name_to = last_invoice.name
            filename_end = "zip"
            if self.export_format == "aee":
                filename_end = "xml"
            self.filename = f'export_{name_from.replace("/", "")}_{name_to.replace("/", "")}.{filename_end}'

    @api.model
    def format_cdata(self, value, use_cdata):
        if use_cdata:
            return Markup("<![CDATA[{0}]]>").format(value)
        else:
            return f"{value}"

    @api.model
    def format_num(self, value):
        if isinstance(value, float):
            return "{:.2f}".format(float_round(value, precision_digits=2))
        return f"{value}"

    @api.model
    def _gen_nav_format_bool(self, value):
        if bool(value):
            return "true"
        else:
            return "false"

    @api.model
    def _gen_nav_format_date(self, day=None):
        if not day:
            day = datetime.utcnow()
        elif isinstance(day, str):
            day = fields.Date.to_date(day)
        return day.strftime("%Y-%m-%d")

    def _get_invoice_xml(self, invoice):
        return self.env["ir.qweb"]._render(
            "l10n_hu_edi.nav_AEA_invoice_xml",
            {
                **invoice._l10n_hu_edi_get_invoice_values(),
                "format_text": partial(self.format_cdata, use_cdata=self.use_cdata),
                "format_num": self.format_num,
            },
        )

    def action_export(self):
        self.ensure_one()
        domain = [
            ("move_type", "in", ("out_invoice", "out_refund")),
            ("state", "=", "posted"),
            ("country_code", "=", "HU"),
            # customer only hungarian
            ("partner_id.commercial_partner_id.country_id.code", "=", "HU"),
        ]
        if self.selection_mode == 'date':
            if self.date_from:
                domain.append(('date', '>=', self.date_from))
            if self.date_to:
                domain.append(('date', '<=', self.date_to))
        else:
            if self.name_from:
                domain.append(('name', '>=', self.name_from))
            if self.name_to:
                domain.append(('name', '<=', self.name_to))

        invoices = self.env['account.move'].search(domain)
        if not invoices:
            raise UserError(_('No invoice to export!'))

        if self.export_format == "nav3.0":
            with io.BytesIO() as buf:
                with zipfile.ZipFile(buf, mode='w', compression=zipfile.ZIP_DEFLATED, allowZip64=False) as zf:
                    # To correctly generate the XML for invoices created before l10n_hu_edi was installed,
                    # we need to temporarily set the chain index and line numbers, so we do this in a savepoint.
                    with contextlib.closing(self.env.cr.savepoint(flush=False)):
                        for invoice in invoices.sorted(lambda i: i.create_date):
                            if invoice.l10n_hu_edi_state:
                                # Case 1: An XML was already generated for this invoice.
                                invoice_xml = base64.b64decode(invoice.l10n_hu_edi_attachment)
                            else:
                                # Case 2: No XML was generated for this invoice.
                                if not invoice.l10n_hu_invoice_chain_index:
                                    invoice._l10n_hu_edi_set_chain_index()
                                invoice_xml = invoice._l10n_hu_edi_generate_xml()

                            filename = f'{invoice.name.replace("/", "_")}.xml'
                            zf.writestr(filename, invoice_xml)
                self.export_file = base64.b64encode(buf.getvalue())

        elif self.export_format == "aee":
            render_datas = {
                "invoices": invoices,
                "invoice_xmls": [self._get_invoice_xml(i) for i in invoices],
                "export_date": fields.Date.context_today(self),
                "format_text": partial(self.format_cdata, use_cdata=self.use_cdata),
                "format_bool": self._gen_nav_format_bool,
                "format_date": self._gen_nav_format_date,
                "format_num": self.format_num,
            }

            xml_content = Markup("<?xml version='1.0' encoding='UTF-8'?>") + self.env["ir.qweb"]._render(
                "l10n_hu_edi.nav_AEA_xml", render_datas
            )

            self.export_file = base64.b64encode(xml_content.encode("UTF-8"))

        else:
            raise UserError(_('Unknown export format!'))

        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'view_mode': 'form',
            'res_id': self.id,
            'views': [(False, 'form')],
            'target': 'new',
        }
