# -*- coding: utf-8 -*-
from odoo import fields, models


class ResPartnerSatExt(models.Model):
    """Extiende res.partner con campo de cuenta contable default para importar CFDIs del SAT."""
    _inherit = "res.partner"

    account_id_base = fields.Many2one(
        "account.account",
        string="Cuenta default para importar CFDI's del SAT",
        company_dependent=True,
        domain="[('deprecated', '=', False)]",
    )
