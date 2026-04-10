# models/sat_cfdi.py
# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class BeiSatCfdi(models.Model):
    _name = "bei.sat.cfdi"
    _description = "SAT - CFDI Descargado"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "fecha desc, uuid"

    # Incluye campo de adjunto XML (Many2one a ir.attachment) y campos típicos del XML.
    ##################################################################################

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company, index=True)

    # Identidad CFDI
    uuid = fields.Char(string="UUID", index=True, required=True, tracking=True)
    tipo_comprobante = fields.Selection(
        [("I","Ingreso"),("E","Egreso"),("T","Traslado"),("N","Nómina"),("P","Pago")],
        index=True,
        tracking=True,
    )
    version = fields.Char()
    serie = fields.Char()
    folio = fields.Char()

    # Fechas
    fecha = fields.Datetime(string="Fecha (Comprobante)", index=True, tracking=True)
    fecha_timbrado = fields.Datetime(string="Fecha Timbrado", index=True)

    # Emisor / Receptor
    rfc_emisor = fields.Char(index=True)
    nombre_emisor = fields.Char()
    rfc_receptor = fields.Char(index=True)
    nombre_receptor = fields.Char()

    # Importes
    moneda = fields.Char()
    tipo_cambio = fields.Float(digits=(16, 6))
    subtotal = fields.Monetary(currency_field="currency_id")
    descuento = fields.Monetary(currency_field="currency_id")
    total = fields.Monetary(currency_field="currency_id")

    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id, required=True)

    # SAT extra
    uso_cfdi = fields.Char(string="Uso CFDI")
    metodo_pago = fields.Char(string="Método de pago")
    forma_pago = fields.Char(string="Forma de pago")
    lugar_expedicion = fields.Char()

    # Adjuntos
    xml_attachment_id = fields.Many2one("ir.attachment", string="XML (adjunto)", ondelete="set null")
    xml_filename = fields.Char()

    # Trazabilidad: solicitud de descarga que originó este CFDI
    download_request_id = fields.Many2one(
        "bei.sat.download.request",
        string="Solicitud de descarga",
        ondelete="set null",
        index=True,
        readonly=True,
    )

    move_id = fields.Many2one("account.move", string="Factura", ondelete="set null", index=True, tracking=True)

    # Partner resuelto en la conciliacion automatica — editable desde lista
    partner_id = fields.Many2one(
        "res.partner", string="Proveedor",
        index=True, tracking=True,
        help="Resuelto desde rfc_emisor. Editable para corregir antes de crear polizas.",
    )

    # Cuenta contable: prioridad cfdi > partner.account_id_base > journal.default_account_id
    account_id_base = fields.Many2one(
        "account.account", string="Cuenta contable",
        domain="[('deprecated','=',False)]",
        tracking=True,
        help="Cuenta para lineas de gasto. Vacio = usa la del proveedor o del diario.",
    )

    # Conceptos
    concept_ids = fields.One2many("bei.sat.cfdi.concept", "cfdi_id", string="Conceptos")

    _sql_constraints = [
        ("uuid_company_uniq", "unique(company_id, uuid)", "Ya existe este UUID para la empresa."),
    ]

    @api.onchange("partner_id")
    def _onchange_partner_id(self):
        """Al seleccionar proveedor, proponer su cuenta si el CFDI no tiene una."""
        for rec in self:
            if rec.partner_id and not rec.account_id_base:
                if rec.partner_id.account_id_base:
                    rec.account_id_base = rec.partner_id.account_id_base

    @api.onchange("account_id_base")
    def _onchange_account_id_base(self):
        """
        Cuando el usuario asigna/cambia la cuenta en este CFDI,
        actualizar tambien la cuenta en el partner para que aplique
        a futuros CFDIs del mismo proveedor.
        Nota: el guardado real en res.partner ocurre en write().
        """
        pass  # La logica real esta en write(); aqui solo sirve para UI feedback

    def write(self, vals):
        res = super().write(vals)
        # Propagar account_id_base al partner cuando el usuario la graba en el CFDI
        if "account_id_base" in vals:
            account_id = vals["account_id_base"]  # puede ser int o False
            for rec in self:
                if rec.partner_id and account_id:
                    # Solo actualizar si el partner tiene cuenta diferente (o sin cuenta)
                    if rec.partner_id.account_id_base.id != account_id:
                        rec.partner_id.sudo().write({"account_id_base": account_id})
                        cfdis_pendientes = self.env['bei.sat.cfdi'].search([('partner_id','=',rec.partner_id.id)])
                        for cfdi_rec in cfdis_pendientes:
                            if not cfdi_rec.account_id_base:
                                cfdi_rec.account_id_base = account_id
        return res

class BeiSatCfdiConcept(models.Model):
    _name = "bei.sat.cfdi.concept"
    _description = "SAT - CFDI Concepto"

    cfdi_id = fields.Many2one("bei.sat.cfdi", required=True, ondelete="cascade", index=True)

    clave_prod_serv = fields.Char()
    no_identificacion = fields.Char()
    cantidad = fields.Float(digits=(16, 6))
    clave_unidad = fields.Char()
    unidad = fields.Char()
    descripcion = fields.Char()
    valor_unitario = fields.Float(digits=(16, 6))
    importe = fields.Float(digits=(16, 6))
    descuento = fields.Float(digits=(16, 6))

    tax_line_ids = fields.One2many("bei.sat.cfdi.concept.tax", "concept_id", string="Impuestos (concepto)")


class BeiSatCfdiConceptTax(models.Model):
    _name = "bei.sat.cfdi.concept.tax"
    _description = "SAT - CFDI Impuesto Concepto"

    concept_id = fields.Many2one("bei.sat.cfdi.concept", required=True, ondelete="cascade", index=True)

    tipo = fields.Selection([("traslado","Traslado"),("retencion","Retención")], required=True)
    impuesto = fields.Char()       # ej: 002 IVA
    tipo_factor = fields.Char()    # Tasa/Cuota/Exento
    tasa_o_cuota = fields.Float(digits=(16, 6))
    base = fields.Float(digits=(16, 6))
    importe = fields.Float(digits=(16, 6))


