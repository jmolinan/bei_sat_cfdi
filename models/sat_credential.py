# models/sat_credential.py
# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from datetime import timedelta


class BeiSatCredential(models.Model):
    _name = "bei.sat.credential"
    _description = "Credenciales SAT (Descarga Masiva)"
    _rec_name = "display_name"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    #  Guarda e.firma y configuración de descarga.

    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    #company_id = fields.Many2one('res.company', string='Compañía', default=lambda self: self.env.company)
    display_name = fields.Char(compute="_compute_display_name", store=True)

    rfc = fields.Char(string="RFC (Solicitante)", required=True, tracking=True)

    # e.firma
    cer_file = fields.Binary(string="Certificado (.cer)", required=True, attachment=True)
    cer_filename = fields.Char(string="Nombre .cer")
    key_file = fields.Binary(string="Llave privada (.key)", required=True, attachment=True)
    key_filename = fields.Char(string="Nombre .key")
    key_password = fields.Char(string="Contraseña llave privada", required=True)

    request_count = fields.Integer(compute="_compute_request_count")

    # Config
    download_mode = fields.Selection(
        [("cfdi", "CFDI"), ("metadata", "Metadata")],
        default="cfdi",
        required=True,
        tracking=True,
    )
    tipo_documento = fields.Selection(
        [("recibidos", "Recibidos"), ("emitidos", "Emitidos")],
        default="recibidos",
        required=True,
        tracking=True,
    )
    tipo_comprobante = fields.Selection(
        [("E", "E - Egreso"), ("I", "I - Ingreso"), ("P", "P - Pago"), ("N", "N - Nómina"), ("T", "T - Traslado")],
        default="E",
        required=True,
        tracking=True,
    )

    # Para el cron: cuántos días hacia atrás descargar (por ejemplo 7)
    cron_days_back = fields.Integer(string="Días hacia atrás (Cron)", default=7)

    @api.depends("company_id", "rfc")
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f"{rec.company_id.name} - {rec.rfc or ''}"

    @api.constrains("rfc")
    def _check_rfc(self):
        for rec in self:
            if rec.rfc and len(rec.rfc.strip()) not in (12, 13):
                raise ValidationError(_("RFC inválido (longitud 12 o 13)."))

    def _compute_request_count(self):
        Request = self.env["bei.sat.download.request"]
        for rec in self:
            rec.request_count = Request.search_count([("credential_id", "=", rec.id)])

    def action_open_download_wizard(self):
        self.ensure_one()
        # default: últimos 7 días o lo que tenga configurado
        days = max(1, int(self.cron_days_back or 7))
        dt_end = fields.Datetime.now()
        dt_start = dt_end - timedelta(days=days)

        return {
            "type": "ir.actions.act_window",
            "name": _("Descargar CFDI SAT"),
            "res_model": "bei.sat.download.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {
                "default_credential_id": self.id,
                "default_date_start": dt_start,
                "default_date_end": dt_end,
                "default_tipo_documento": self.tipo_documento,
                "default_tipo_comprobante": self.tipo_comprobante,
            },
        }

    def action_view_requests(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Solicitudes SAT"),
            "res_model": "bei.sat.download.request",
            "view_mode": "list,form",
            "domain": [("credential_id", "=", self.id)],
            "context": {"search_default_groupby_state": 1},
        }