# -*- coding: utf-8 -*-
from datetime import datetime, timedelta
from odoo import api, models, _
from odoo.exceptions import UserError


class BeiSatDownloadService(models.AbstractModel):
    _name = "bei.sat.download.service"
    _description = "Servicio Descarga Masiva SAT"

    @api.model
    def _get_active_credentials(self):
        return self.env["bei.sat.credential"].sudo().search([("active", "=", True)])

    @api.model
    def cron_download_egresos_recibidos(self):
        """
        Cron diario: crea nuevas solicitudes de descarga para cada credencial activa.
        El polling lo hace cron_verificar_pendientes (bei.sat.download.request).
        """
        creds = self._get_active_credentials()
        for cred in creds:
            try:
                self._create_request_for_credential(cred)
            except Exception as e:
                # Log y continuar con la siguiente credencial
                import logging
                logging.getLogger(__name__).error(
                    "Error creando solicitud SAT para %s: %s", cred.rfc, str(e))

    @api.model
    def _create_request_for_credential(self, cred):
        if not cred.cer_file or not cred.key_file:
            raise UserError(_("Credencial SAT sin CER/KEY configurados."))

        days_back = cred.cron_days_back or 1
        dt_end = datetime.utcnow()
        dt_start = dt_end - timedelta(days=days_back)

        # Delegar toda la lógica al request model (solicitar + verificar + descargar)
        self.env["bei.sat.download.request"].create_and_run(cred, dt_start, dt_end)
