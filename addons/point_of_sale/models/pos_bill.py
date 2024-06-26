from odoo import api, fields, models, _
from odoo.exceptions import UserError


class Bill(models.Model):
    _name = "pos.bill"
    _order = "value"
    _description = "Coins/Bills"

    name = fields.Char("Name")
    value = fields.Float("Coin/Bill Value", required=True, digits=0)
    for_all_config = fields.Boolean("For All PoS", default=True, help="If checked, this coin/bill will be available in all PoS.")
    pos_config_ids = fields.Many2many("pos.config", string="Point of Sales")

    @api.model
    def name_create(self, name):
        try:
            value = float(name)
        except ValueError:
            raise UserError(_("The name of the Coins/Bills must be a number."))
        result = super().create({"name": name, "value": value})
        return result.id, result.display_name
