# -*- coding: utf-8 -*-
import base64
import io
import zipfile
from datetime import datetime
from odoo import api, fields, models, _
from odoo.exceptions import UserError

from ..services.sat_fiel import BeiSatFiel
from ..services.sat_client import BeiSatDescargaMasivaClient, SatSoapError
from ..services.cfdi_parser import Cfdi40Parser

import logging
_logger = logging.getLogger(__name__)


class BeiSatDownloadRequest(models.Model):
    _name = "bei.sat.download.request"
    _description = "SAT - Solicitudes Descarga Masiva"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "create_date desc"

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)
    credential_id = fields.Many2one("bei.sat.credential", required=True, ondelete="restrict", index=True)

    date_start = fields.Datetime(required=True, index=True)
    date_end = fields.Datetime(required=True, index=True)

    tipo_documento = fields.Selection(
        [("recibidos", "Recibidos"), ("emitidos", "Emitidos")],
        string="Tipo Documento",
        required=True,
    )
    tipo_comprobante = fields.Selection(
        [("E", "E - Egreso"), ("I", "I - Ingreso"), ("P", "P - Pago"), ("N", "N - Nómina"), ("T", "T - Traslado")],
        string="Tipo Comprobante",
        required=True,
    )

    sat_id_solicitud = fields.Char(string="IdSolicitud SAT", index=True, tracking=True)
    sat_estado_solicitud = fields.Selection(
        [
            ("1", "Aceptada"),
            ("2", "En proceso"),
            ("3", "Terminada"),
            ("4", "Error"),
            ("5", "Rechazada"),
            ("6", "Vencida"),
        ],
        string="Estado Solicitud (SAT)",
        tracking=True,
    )

    sat_codestatus = fields.Char(string="CodEstatus", tracking=True)
    sat_mensaje = fields.Char(string="Mensaje SAT", tracking=True)

    package_ids = fields.One2many("bei.sat.download.package", "request_id", string="Paquetes")

    state = fields.Selection(
        [
            ("draft", "Borrador"),
            ("requested", "Solicitada"),
            ("verifying", "En proceso SAT"),
            ("done", "Descargada"),
            ("no_data", "Sin información"),
            ("error", "Error"),
        ],
        default="draft",
        tracking=True,
        index=True,
    )
    sat_codigo_estado_solicitud = fields.Char(string="CodigoEstadoSolicitud", tracking=True)
    sat_numero_cfdis = fields.Integer(string="Número CFDIs", tracking=True)
    last_error = fields.Text()

    # -------------------------
    # API pública
    # -------------------------
    @api.model
    def create_and_run(self, credential, date_start, date_end, tipo_documento=None, tipo_comprobante=None):
        _logger.info("--- create_and_run")
        req = self.create({
            "company_id": credential.company_id.id,
            "credential_id": credential.id,
            "date_start": date_start,
            "date_end": date_end,
            "tipo_documento": tipo_documento or credential.tipo_documento,
            "tipo_comprobante": tipo_comprobante or credential.tipo_comprobante,
            "state": "draft",
        })
        req.action_run_now()
        return req

    def action_run_now(self):
        """Paso 1: solicitar al SAT. Intenta verificar de inmediato."""
        for req in self:
            try:
                req._step_solicitar()
            except Exception as e:
                req.state = "error"
                req.last_error = str(e)
                req.message_post(body=_("❌ Error en solicitud SAT: %s") % str(e))

    def action_verificar(self):
        """Paso 2 (manual): verificar estado y descargar si ya terminó."""
        for req in self.filtered(lambda r: r.state in ("requested", "verifying") and r.sat_id_solicitud):
            try:
                req._step_verificar_y_descargar()
            except Exception as e:
                req.state = "error"
                req.last_error = str(e)
                req.message_post(body=_("❌ Error en verificación SAT: %s") % str(e))

    # -------------------------
    # Cron polling
    # -------------------------
    @api.model
    def cron_verificar_pendientes(self):
        """
        Retoma todas las solicitudes pendientes (requested / verifying).
        Ejecutar cada 15 minutos.
        """
        pendientes = self.search([
            ("state", "in", ("requested", "verifying")),
            ("sat_id_solicitud", "!=", False),
        ])
        for req in pendientes:
            try:
                req._step_verificar_y_descargar()
            except Exception as e:
                req.state = "error"
                req.last_error = str(e)
                req.message_post(body=_("❌ Error en verificación automática SAT: %s") % str(e))

    # -------------------------
    # Pasos internos
    # -------------------------
    def _build_client(self):
        """Construye FIEL + cliente SAT desde la credencial."""

        _logger.info("Construye FIEL + cliente SAT desde la credencial.......")
        self.ensure_one()
        cred = self.credential_id
        _logger.info(cred.key_password)
        if not cred.cer_file or not cred.key_file:
            raise UserError(_("La credencial SAT no tiene CER/KEY configurados."))
        fiel = BeiSatFiel(
            cer_der=base64.b64decode(cred.cer_file),
            key_der=base64.b64decode(cred.key_file),
            password=cred.key_password or "",
        )
        return BeiSatDescargaMasivaClient(fiel=fiel, timeout=90, verify_ssl=True)

    def _step_solicitar(self):
        _logger.info("_step_solicitar")
        self.ensure_one()
        cred = self.credential_id
        client = self._build_client()
        token = client.autentica()

        # Leer los valores fijos guardados en la solicitud (no de la credencial)
        tipo_documento = self.tipo_documento
        tipo_comprobante = self.tipo_comprobante

        _logger.info("_step_solicitar: tipo_documento=%s, tipo_comprobante=%s", tipo_documento, tipo_comprobante)

        if tipo_documento == "emitidos":
            sol = client.solicita_descarga_emitidos(
                token=token,
                rfc_solicitante=cred.rfc,
                rfc_emisor=cred.rfc,
                fecha_ini=fields.Datetime.to_datetime(self.date_start),
                fecha_fin=fields.Datetime.to_datetime(self.date_end),
                tipo_comprobante=tipo_comprobante,
                tipo_solicitud="CFDI",
            )
        else:
            sol = client.solicita_descarga_recibidos(
                token=token,
                rfc_solicitante=cred.rfc,
                rfc_receptor=cred.rfc,
                fecha_ini=fields.Datetime.to_datetime(self.date_start),
                fecha_fin=fields.Datetime.to_datetime(self.date_end),
                tipo_comprobante=tipo_comprobante,
                tipo_solicitud="CFDI",
            )

        self.sat_id_solicitud = sol.get("id_solicitud")
        self.sat_codestatus = sol.get("cod_estatus")
        self.sat_mensaje = sol.get("mensaje")
        self.state = "requested"

        if not self.sat_id_solicitud:
            raise UserError(_("SAT no devolvió IdSolicitud. CodEstatus=%s Mensaje=%s") %
                            (self.sat_codestatus, self.sat_mensaje))

        self.message_post(
            body=_("✅ Solicitud %s enviada al SAT. TipoComprobante=%s. IdSolicitud: %s") % (
                tipo_documento.capitalize(), tipo_comprobante, self.sat_id_solicitud
            )
        )

        # Intentar verificar inmediatamente (a veces el SAT ya terminó)
        self._step_verificar_y_descargar(client=client, token=token)

    def _step_verificar_y_descargar(self, client=None, token=None):
        """
        Verifica el estado en el SAT y descarga paquetes si ya terminó.
        Importante:
        - sat_codestatus = estado técnico de la llamada SOAP
        - sat_estado_solicitud = estado funcional de la solicitud
        - sat_codigo_estado_solicitud = detalle del resultado funcional
        """
        self.ensure_one()
        cred = self.credential_id

        if not client or not token:
            client = self._build_client()
            token = client.autentica()

        ver = client.verifica_solicitud(
            token=token,
            rfc_solicitante=cred.rfc,
            id_solicitud=self.sat_id_solicitud,
        )

        estado = str(ver.get("estado_solicitud") or "")
        codigo_estado = str(ver.get("codigo_estado_solicitud") or "")
        codestatus = ver.get("cod_estatus")
        mensaje = ver.get("mensaje")
        numero_cfdis = int(ver.get("numero_cfdis") or 0)

        self.sat_codestatus = codestatus
        self.sat_estado_solicitud = estado if estado in ("1", "2", "3", "4", "5", "6") else False
        self.sat_codigo_estado_solicitud = codigo_estado or False
        self.sat_numero_cfdis = numero_cfdis
        self.sat_mensaje = mensaje

        paquetes = ver.get("paquetes") or []
        self._sync_packages(paquetes)

        # 3 = Terminada -> descargar
        if estado == "3":
            total_ok = total_skip = total_err = 0
            for pkg in self.package_ids.filtered(lambda p: not p.downloaded):
                zip_bytes = client.descarga_paquete(
                    token=token,
                    rfc_solicitante=cred.rfc,
                    id_paquete=pkg.package_id,
                )

                att_zip = self.env["ir.attachment"].sudo().create({
                    "name": f"SAT_{cred.rfc}_{pkg.package_id}.zip",
                    "type": "binary",
                    "datas": base64.b64encode(zip_bytes),
                    "mimetype": "application/zip",
                    "res_model": "bei.sat.download.package",
                    "res_id": pkg.id,
                })
                pkg.attachment_id = att_zip.id
                pkg.downloaded = True
                ok, skip, err = self._import_cfdi_zip(zip_bytes, self.company_id)
                total_ok += ok
                total_skip += skip
                total_err += err

            self.state = "done"
            self.message_post(
                body=_("✅ Descarga completada. %d paquete(s). CFDIs: %d nuevos, %d ya existían, %d errores.")
                % (len(paquetes), total_ok, total_skip, total_err)
            )
            return

        # 5 + 5004 = sin información -> no reintentar
        if estado == "5" and codigo_estado == "5004":
            self.state = "no_data"
            self.last_error = False
            self.message_post(
                body=_(
                    "ℹ️ Solicitud SAT sin resultados. CodEstatus=%s, EstadoSolicitud=%s, "
                    "CodigoEstadoSolicitud=%s, CFDIs=%s, Mensaje=%s"
                ) % (
                    codestatus or "",
                    estado or "",
                    codigo_estado or "",
                    numero_cfdis,
                    mensaje or "",
                )
            )
            return

        # 4 / 5 / 6 = casos finales con error
        if estado in ("4", "5", "6"):
            self.state = "error"
            self.last_error = (
                f"SAT terminó con EstadoSolicitud={estado}, "
                f"CodigoEstadoSolicitud={codigo_estado or ''}, "
                f"CodEstatus={codestatus or ''}, "
                f"Mensaje={mensaje or ''}"
            )
            self.message_post(
                body=_(
                    "❌ SAT rechazó/finalizó con error. EstadoSolicitud=%s, "
                    "CodigoEstadoSolicitud=%s, CodEstatus=%s, Mensaje=%s"
                ) % (
                    estado or "",
                    codigo_estado or "",
                    codestatus or "",
                    mensaje or "",
                )
            )
            return

        # 1 = Aceptada, 2 = En proceso
        self.state = "verifying"
        self.message_post(
            body=_(
                "⏳ SAT en proceso. CodEstatus=%s, EstadoSolicitud=%s, "
                "CodigoEstadoSolicitud=%s. El cron verificará automáticamente cada 15 min."
            ) % (
                codestatus or "",
                estado or "",
                codigo_estado or "",
            )
        )

    def _sync_packages(self, paquetes: list):
        self.ensure_one()
        existing = {p.package_id for p in self.package_ids}
        for pid in paquetes:
            if pid not in existing:
                self.env["bei.sat.download.package"].create({
                    "request_id": self.id,
                    "package_id": pid,
                })

    # -------------------------
    # Import ZIP → CFDI Table
    # -------------------------
    def _import_cfdi_zip(self, zip_bytes: bytes, company):
        ok = skip = err = 0
        new_cfdi_ids = []
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            _logger.info("SAT ZIP: %d XMLs encontrados", len(names))
            for name in names:
                try:
                    result = self._upsert_cfdi_from_xml(company, name, zf.read(name))
                    if result is None:
                        # XML sin UUID valido, no se puede importar
                        skip += 1
                        _logger.warning("SAT ZIP: descartado (sin UUID) %s", name)
                    elif result["created"]:
                        ok += 1
                        new_cfdi_ids.append(result["id"])
                    else:
                        skip += 1  # ya existia
                except Exception as e:
                    err += 1
                    _logger.warning("SAT ZIP: error procesando %s: %s", name, str(e))
        _logger.info("SAT ZIP: %d nuevos, %d omitidos, %d errores", ok, skip, err)
        # Conciliacion automatica sobre los recien importados
        if new_cfdi_ids:
            _logger.info("SAT ZIP: lanzando auto_reconcile para %d CFDIs", len(new_cfdi_ids))
            self.env["bei.sat.cfdi"].auto_reconcile_batch(new_cfdi_ids)
        return ok, skip, err

    def _upsert_cfdi_from_xml(self, company, filename: str, xml_bytes: bytes):
        parsed = Cfdi40Parser.parse(xml_bytes)
        if not parsed.uuid:
            return None

        Cfdi = self.env["bei.sat.cfdi"].sudo()
        existing = Cfdi.search([("company_id", "=", company.id), ("uuid", "=", parsed.uuid)], limit=1)
        if existing:
            update_vals = {}
            if not existing.xml_attachment_id:
                att = self.env["ir.attachment"].sudo().create({
                    "name": filename, "type": "binary",
                    "datas": base64.b64encode(xml_bytes),
                    "mimetype": "application/xml",
                    "res_model": "bei.sat.cfdi", "res_id": existing.id,
                })
                update_vals["xml_attachment_id"] = att.id
                update_vals["xml_filename"] = filename
            if not existing.download_request_id:
                update_vals["download_request_id"] = self.id
            if update_vals:
                existing.write(update_vals)
            return {"id": existing.id, "created": False}

        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename, "type": "binary",
            "datas": base64.b64encode(xml_bytes),
            "mimetype": "application/xml",
            "res_model": "bei.sat.cfdi", "res_id": 0,
        })

        # Convertir fechas string → formato Odoo (espera "YYYY-MM-DD HH:MM:SS", SAT manda "YYYY-MM-DDTHH:MM:SS")
        def _parse_dt(s):
            if not s:
                return False
            try:
                return s[:19].replace("T", " ")
            except Exception:
                return False

        # Resolver currency_id desde el código ISO del CFDI (MXN, USD, EUR, etc.)
        # Si la moneda no existe en el sistema o es "XXX" (no aplica), usar la de la empresa
        currency_id = company.currency_id.id
        if parsed.moneda and parsed.moneda.upper() != "XXX":
            currency = self.env["res.currency"].sudo().search(
                [("name", "=", parsed.moneda.upper())], limit=1
            )
            if currency:
                currency_id = currency.id
            else:
                _logger.warning(
                    "SAT CFDI %s: moneda '%s' no encontrada en el sistema, usando moneda de la empresa.",
                    parsed.uuid, parsed.moneda,
                )

        cfdi = Cfdi.create({
            "company_id": company.id,
            "uuid": parsed.uuid,
            "version": parsed.version,
            "serie": parsed.serie,
            "folio": parsed.folio,
            "fecha": _parse_dt(parsed.fecha),
            "fecha_timbrado": _parse_dt(parsed.fecha_timbrado),
            "lugar_expedicion": parsed.lugar_expedicion,
            "tipo_comprobante": parsed.tipo_comprobante,
            "moneda": parsed.moneda,
            "tipo_cambio": float(parsed.tipo_cambio) if parsed.tipo_cambio else False,
            "currency_id": currency_id,
            "subtotal": float(parsed.subtotal) if parsed.subtotal else 0.0,
            "descuento": float(parsed.descuento) if parsed.descuento else 0.0,
            "total": float(parsed.total) if parsed.total else 0.0,
            "metodo_pago": parsed.metodo_pago,
            "forma_pago": parsed.forma_pago,
            "uso_cfdi": parsed.uso_cfdi,
            "rfc_emisor": parsed.rfc_emisor,
            "nombre_emisor": parsed.nombre_emisor,
            "rfc_receptor": parsed.rfc_receptor,
            "nombre_receptor": parsed.nombre_receptor,
            "xml_filename": filename,
            "xml_attachment_id": attachment.id,
            "download_request_id": self.id,
        })
        attachment.write({"res_id": cfdi.id})

        for c in parsed.conceptos:
            concept = self.env["bei.sat.cfdi.concept"].sudo().create({
                "cfdi_id": cfdi.id,
                "clave_prod_serv": c.clave_prod_serv,
                "no_identificacion": c.no_identificacion,
                "cantidad": float(c.cantidad) if c.cantidad else 0.0,
                "clave_unidad": c.clave_unidad,
                "unidad": c.unidad,
                "descripcion": c.descripcion,
                "valor_unitario": float(c.valor_unitario) if c.valor_unitario else 0.0,
                "importe": float(c.importe) if c.importe else 0.0,
                "descuento": float(c.descuento) if c.descuento else 0.0,
            })
            for t in c.impuestos:
                self.env["bei.sat.cfdi.concept.tax"].sudo().create({
                    "concept_id": concept.id,
                    "tipo": t.tipo,
                    "impuesto": t.impuesto,
                    "tipo_factor": t.tipo_factor,
                    "tasa_o_cuota": float(t.tasa_o_cuota) if t.tasa_o_cuota else 0.0,
                    "base": float(t.base) if t.base else 0.0,
                    "importe": float(t.importe) if t.importe else 0.0,
                })

        return {"id": cfdi.id, "created": True}


class BeiSatDownloadPackage(models.Model):
    _name = "bei.sat.download.package"
    _description = "SAT - Paquetes Descargados"

    request_id = fields.Many2one("bei.sat.download.request", required=True, ondelete="cascade", index=True)
    package_id = fields.Char(string="IdPaquete SAT", required=True, index=True)
    downloaded = fields.Boolean(default=False, index=True)
    attachment_id = fields.Many2one("ir.attachment", string="ZIP adjunto", ondelete="set null")
