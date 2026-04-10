# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class BeiSatDownloadWizard(models.TransientModel):
    _name = "bei.sat.download.wizard"
    _description = "Wizard - Descargar CFDI SAT"

    credential_id = fields.Many2one("bei.sat.credential", required=True, ondelete="restrict")
    date_start = fields.Datetime(required=True)
    date_end = fields.Datetime(required=True)

    tipo_documento = fields.Selection(
        [("recibidos", "Recibidos"), ("emitidos", "Emitidos")],
        required=True,
        string="Tipo Documento",
    )

    tipo_comprobante = fields.Selection(
        [
            ("I", "I - Ingreso"),
            ("E", "E - Egreso"),
            ("P", "P - Pago"),
            ("N", "N - Nómina"),
            ("T", "T - Traslado"),
        ],
        required=True,
        string="Tipo Comprobante",
    )

    @api.onchange("credential_id")
    def _onchange_credential_id(self):
        if self.credential_id:
            self.tipo_documento = self.credential_id.tipo_documento
            self.tipo_comprobante = self.credential_id.tipo_comprobante

    def action_download(self):
        self.ensure_one()
        if self.date_start >= self.date_end:
            raise UserError(_("La fecha inicial debe ser menor a la fecha final."))

        # Sincronizar configuración a la credencial si el usuario la modificó en el wizard
        # self.credential_id.sudo().write({
        #     "tipo_documento": self.tipo_documento,
        #     "tipo_comprobante": self.tipo_comprobante,
        # })

        req = self.env["bei.sat.download.request"].create_and_run(
            self.credential_id,
            self.date_start,
            self.date_end,
            tipo_documento=self.tipo_documento,
            tipo_comprobante=self.tipo_comprobante,
        )

        return {
            "type": "ir.actions.act_window",
            "name": _("Solicitud SAT"),
            "res_model": "bei.sat.download.request",
            "view_mode": "form",
            "res_id": req.id,
            "target": "current",
        }
