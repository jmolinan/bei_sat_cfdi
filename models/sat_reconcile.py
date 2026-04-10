# models/sat_reconcile.py
# -*- coding: utf-8 -*-
import base64
import logging
from odoo import api, fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class BeiSatCfdiReconcile(models.Model):
    """
    Conciliacion SAT en 2 pasos:

    PASO 1 - auto_reconcile() [se llama al terminar de importar el ZIP]:
      - Para cada CFDI nuevo: get_or_create partner desde rfc_emisor
      - Intenta match por UUID  -> vincula move_id
      - Intenta match fuzzy (partner+fecha+monto) -> vincula move_id + adjunta XML
      - Los que no matchean quedan sin move_id para revision del usuario

    PASO 2 - action_create_moves() [boton "Conciliar seleccionados"]:
      - Solo opera sobre los que YA tienen partner_id
      - Crea account.move con las lineas de concepto
      - Usa account_id_base del cfdi, o del partner, o del journal (en ese orden)
      - Adjunta el XML al move y postea
    """
    _inherit = "bei.sat.cfdi"

    # ================================================================== #
    #  PASO 1 — Conciliacion automatica (se llama desde _import_cfdi_zip) #
    # ================================================================== #
    @api.model
    def auto_reconcile_batch(self, cfdi_ids):
        """
        Recibe una lista de IDs de bei.sat.cfdi recien importados.
        Para cada uno: resuelve partner y trata de hacer match con move existente.
        """
        records = self.browse(cfdi_ids)
        for cfdi in records:
            try:
                cfdi._auto_reconcile_one()
            except Exception as e:
                _logger.warning("SAT auto_reconcile error CFDI %s: %s", cfdi.uuid, str(e))

    def _auto_reconcile_one(self):
        """
        1. Resuelve/crea partner desde rfc_emisor y lo graba en partner_id
        2. Propone account_id_base desde el partner si no hay
        3. Intenta match UUID
        4. Intenta match fuzzy
        Los que no matchean quedan con partner_id pero sin move_id (para revision)
        """
        self.ensure_one()
        if self.move_id:
            return  # ya conciliado

        # -- Resolver partner -------------------------------------------------
        partner = self._get_or_create_partner()
        vals = {}
        if partner and not self.partner_id:
            vals["partner_id"] = partner.id
        # Proponer cuenta del partner si el cfdi no tiene
        if partner and not self.account_id_base and partner.account_id_base:
            vals["account_id_base"] = partner.account_id_base.id
        if vals:
            self.sudo().write(vals)

        if not partner:
            return  # sin RFC no podemos hacer nada mas

        # -- a) Match por UUID ------------------------------------------------
        # Para tipo P buscar en account.payment; para el resto en account.move
        if self.tipo_comprobante == "P":
            payment = self._find_matching_payment()
            if payment:
                self.sudo().write({"move_id": payment.move_id.id})
                self.message_post(body=_("Auto-conciliado pago por UUID: %s") % (payment.name or payment.id))
                return
        else:
            move = self.env["account.move"].sudo().search([
                ("company_id", "=", self.company_id.id),
                ("l10n_mx_edi_cfdi_uuid", "=ilike", self.uuid),
                ("move_type", "in", ("in_invoice", "in_refund")),
            ], limit=1)
            if move:
                self.sudo().write({"move_id": move.id})
                self._attach_xml_to_move(move)
                self.message_post(body=_("Auto-conciliado por UUID con factura %s") % move.name)
                return

        # -- b) Match fuzzy (partner + fecha + monto sin UUID) ----------------
        if self.tipo_comprobante == "P":
            # Para pagos el fuzzy ya lo cubre _find_matching_payment
            _logger.info("SAT: CFDI Pago %s sin match automatico — pendiente de revision", self.uuid)
            return

        if self.fecha and self.total:
            fecha_date = self.fecha.date()
            # amount_total en account.move siempre esta en la moneda del move (currency_id)
            # self.total viene en la moneda del CFDI — coinciden directamente
            # (si el move es en USD y el CFDI es en USD, ambos tienen el mismo valor)
            move = self.env["account.move"].sudo().search([
                ("company_id", "=", self.company_id.id),
                ("partner_id", "=", partner.id),
                ("invoice_date", "=", fecha_date),
                ("amount_total", "=", float(self.total)),
                ("currency_id.name", "=", (self.moneda or "MXN").upper()),
                ("move_type", "in", ("in_invoice", "in_refund")),
                "|",
                ("l10n_mx_edi_cfdi_uuid", "=", False),
                ("l10n_mx_edi_cfdi_uuid", "=", ""),
            ], limit=1)
            if move:
                move.sudo().write({"l10n_mx_edi_cfdi_uuid": self.uuid})
                self.sudo().write({"move_id": move.id})
                self._attach_xml_to_move(move)
                self.message_post(body=_(
                    "Auto-conciliado por partner/fecha/monto con factura %s"
                ) % move.name)
                return

        # Sin match: queda listo para que el usuario revise partner y cuenta
        # antes de ejecutar el Paso 2
        _logger.info("SAT: CFDI %s sin match automatico — pendiente de revision", self.uuid)

    # ================================================================== #
    #  PASO 2 — Creacion de polizas (boton desde la lista)               #
    # ================================================================== #
    def action_create_moves(self):
        """
        Crea account.move o concilia account.payment según el tipo de comprobante:
          I / E / N → crea factura de proveedor (in_invoice / in_refund)
          P         → busca y vincula un account.payment existente
          T         → solo registra, sin poliza contable
        """
        sin_partner = self.filtered(lambda r: not r.move_id and not r.partner_id)
        if sin_partner:
            uuids = ", ".join(sin_partner.mapped("uuid")[:5])
            raise UserError(_(
                "Los siguientes CFDIs no tienen proveedor asignado. "
                "Asigne el proveedor en la lista antes de continuar:\n%s"
            ) % uuids)

        records = self.filtered(lambda r: not r.move_id)
        if not records:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Crear Polizas SAT"),
                    "message": _("Todos los registros seleccionados ya tienen factura."),
                    "type": "info",
                },
            }

        created = skipped = errors = 0
        for cfdi in records:
            try:
                cfdi._auto_reconcile_one()
                if cfdi.move_id:
                    continue  # el auto_reconcile ya lo resolvio

                tipo = cfdi.tipo_comprobante or ""

                if tipo == "T":
                    # Traslado: sin poliza contable, solo marcar revisado
                    skipped += 1
                    cfdi.message_post(body=_("CFDI Traslado (T): no genera poliza contable."))

                elif tipo == "P":
                    # Pago: buscar account.payment existente y vincular
                    payment = cfdi._find_matching_payment()
                    if payment:
                        cfdi.sudo().write({"move_id": payment.move_id.id})
                        cfdi.message_post(
                            body=_("Pago vinculado: %s") % (payment.name or payment.ref or payment.id)
                        )
                        created += 1
                    else:
                        skipped += 1
                        cfdi.message_post(body=_(
                            "CFDI Pago (P): no se encontró un pago coincidente. "
                            "Verifique en Contabilidad > Pagos."
                        ))

                else:
                    # I, E, N → factura / nota de crédito
                    move = cfdi._create_vendor_bill(cfdi.partner_id)
                    cfdi.sudo().write({"move_id": move.id})
                    cfdi.message_post(body=_("Factura creada: %s") % move.name)
                    created += 1

            except Exception as e:
                errors += 1
                _logger.warning("SAT create_move error CFDI %s: %s", cfdi.uuid, str(e))
                cfdi.message_post(body=_("Error al crear factura: %s") % str(e))

        msg = _("Procesados: %(created)d creados/vinculados  |  %(skipped)d omitidos  |  %(errors)d errores") % {
            "created": created, "skipped": skipped, "errors": errors}
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Crear Polizas SAT"),
                "message": msg,
                "type": "success" if not errors else "warning",
                "sticky": True,
            },
        }

    # ================================================================== #
    #  Helpers compartidos                                                 #
    # ================================================================== #
    def _find_matching_payment(self):
        """
        Busca un account.payment que corresponda a este CFDI tipo P (Pago).

        Estrategia en orden de prioridad:
          1. Match por UUID en l10n_mx_edi_cfdi_uuid del payment
          2. Match fuzzy: partner + fecha + monto + moneda
        Retorna el account.payment encontrado o False.
        """
        self.ensure_one()
        Payment = self.env["account.payment"].sudo()

        # 1) Match por UUID
        payment = Payment.search([
            ("company_id", "=", self.company_id.id),
            ("l10n_mx_edi_cfdi_uuid", "=ilike", self.uuid),
        ], limit=1)
        if payment:
            return payment

        # 2) Match fuzzy: partner + fecha + monto + moneda
        if not self.partner_id or not self.fecha or not self.total:
            return False

        fecha_date = self.fecha.date()
        moneda = (self.moneda or "MXN").upper()

        payment = Payment.search([
            ("company_id", "=", self.company_id.id),
            ("partner_id", "=", self.partner_id.id),
            ("date", "=", fecha_date),
            ("amount", "=", float(self.total)),
            ("currency_id.name", "=", moneda),
            ("payment_type", "=", "outbound"),
            ("state", "in", ("posted", "reconciled")),
            "|",
            ("l10n_mx_edi_cfdi_uuid", "=", False),
            ("l10n_mx_edi_cfdi_uuid", "=", ""),
        ], limit=1)
        if payment:
            # Grabar UUID en el pago para evitar doble match futuro
            try:
                payment.sudo().write({"l10n_mx_edi_cfdi_uuid": self.uuid})
            except Exception:
                pass
            return payment

        return False

    def _get_or_create_partner(self):
        self.ensure_one()
        if not self.rfc_emisor:
            return False
        # Si ya tiene partner_id grabado, usarlo directamente
        if self.partner_id:
            return self.partner_id
        Partner = self.env["res.partner"].sudo()
        partner = Partner.search([("vat", "=ilike", self.rfc_emisor)], limit=1)
        if not partner:
            partner = Partner.create({
                "name": self.nombre_emisor or self.rfc_emisor,
                "vat": self.rfc_emisor.upper(),
                "company_type": "company",
                "supplier_rank": 1,
            })
            _logger.info("SAT: proveedor creado RFC=%s nombre=%s", self.rfc_emisor, self.nombre_emisor)
        return partner

    def _attach_xml_to_move(self, move):
        """
        Adjunta el XML del CFDI al account.move:
        1. ir.attachment estandar (descargable desde la factura)
        2. account.edi.document si l10n_mx_edi esta instalado
        3. Escribe l10n_mx_edi_cfdi_uuid como texto de respaldo
        """
        self.ensure_one()
        if not self.xml_attachment_id:
            return

        xml_bytes = base64.b64decode(self.xml_attachment_id.datas)
        filename = self.xml_filename or ("%s.xml" % self.uuid)

        # 1) ir.attachment en el move
        existing = self.env["ir.attachment"].sudo().search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", move.id),
            ("name", "=", filename),
        ], limit=1)
        if not existing:
            att = self.env["ir.attachment"].sudo().create({
                "name": filename,
                "type": "binary",
                "datas": base64.b64encode(xml_bytes),
                "mimetype": "application/xml",
                "res_model": "account.move",
                "res_id": move.id,
            })
        else:
            att = existing

        # 2) account.edi.document (l10n_mx_edi)
        EdiDoc = self.env.get("account.edi.document")
        if EdiDoc is not None:
            edi_format = self.env["account.edi.format"].sudo().search([
                ("code", "in", ("cfdi_4_0", "mx_cfdi_4_0", "cfdi40"))
            ], limit=1)
            if edi_format:
                existing_edi = EdiDoc.sudo().search([
                    ("move_id", "=", move.id),
                    ("edi_format_id", "=", edi_format.id),
                ], limit=1)
                if not existing_edi:
                    EdiDoc.sudo().create({
                        "move_id": move.id,
                        "edi_format_id": edi_format.id,
                        "attachment_id": att.id,
                        "state": "sent",
                    })

        # 3) UUID como texto de respaldo
        if not move.l10n_mx_edi_cfdi_uuid:
            try:
                move.sudo().write({"l10n_mx_edi_cfdi_uuid": self.uuid})
            except Exception as e:
                _logger.warning("SAT: no se pudo escribir l10n_mx_edi_cfdi_uuid: %s", str(e))

    def _create_vendor_bill(self, partner):
        """
        Crea y postea un account.move desde este CFDI.
          I / T → in_invoice  (factura de proveedor)
          E     → in_refund   (nota de crédito de proveedor)
          N     → in_invoice  en diario de nómina (o compra si no existe)
        """
        self.ensure_one()

        tipo = self.tipo_comprobante or "I"

        # ── Diario ───────────────────────────────────────────────────────────
        if tipo == "N":
            # Nómina: preferir diario con tipo 'general' y nombre que contenga "nómina"
            journal = self.env["account.journal"].sudo().search([
                ("company_id", "=", self.company_id.id),
                ("type", "=", "general"),
                ("name", "ilike", "nómin"),
            ], limit=1)
            if not journal:
                journal = self.env["account.journal"].sudo().search([
                    ("company_id", "=", self.company_id.id),
                    ("type", "=", "general"),
                    ("name", "ilike", "nomin"),
                ], limit=1)
            if not journal:
                # Fallback al diario de compras
                journal = self.env["account.journal"].sudo().search([
                    ("company_id", "=", self.company_id.id),
                    ("type", "=", "purchase"),
                ], limit=1)
        else:
            journal = self.env["account.journal"].sudo().search([
                ("company_id", "=", self.company_id.id),
                ("type", "=", "purchase"),
            ], limit=1)

        if not journal:
            raise UserError(
                _("No existe un diario de tipo Compra en la empresa %s") % self.company_id.name)

        # ── Tipo de movimiento ────────────────────────────────────────────────
        move_type = "in_refund" if tipo == "E" else "in_invoice"

        fecha_date = self.fecha.date() if self.fecha else fields.Date.today()

        # ── Moneda ───────────────────────────────────────────────────────────
        # Si el CFDI viene en moneda extranjera, resolver la currency_id de Odoo
        company_currency = self.company_id.currency_id
        cfdi_moneda = (self.moneda or "MXN").upper().strip()
        es_moneda_extranjera = cfdi_moneda != company_currency.name

        if es_moneda_extranjera:
            move_currency = self.env["res.currency"].sudo().search(
                [("name", "=", cfdi_moneda), ("active", "in", [True, False])], limit=1)
            if not move_currency:
                _logger.warning(
                    "SAT: moneda '%s' no encontrada en Odoo, usando MXN", cfdi_moneda)
                move_currency = company_currency
                es_moneda_extranjera = False
        else:
            move_currency = company_currency

        move_vals = {
            "move_type": move_type,
            "company_id": self.company_id.id,
            "partner_id": partner.id,
            "invoice_date": fecha_date,
            "ref": ("%s%s" % (self.serie or "", self.folio or "")).strip() or self.uuid,
            "journal_id": journal.id,
            "currency_id": move_currency.id,
        }

        def _has_field(fname):
            return bool(self.env["ir.model.fields"].sudo().search(
                [("model", "=", "account.move"), ("name", "=", fname)], limit=1))

        if _has_field("l10n_mx_edi_payment_policy") and self.metodo_pago:
            move_vals["l10n_mx_edi_payment_policy"] = self.metodo_pago
        if self.forma_pago and _has_field("l10n_mx_edi_payment_method_id"):
            pm = self.env["l10n_mx_edi.payment.method"].sudo().search(
                [("code", "=", self.forma_pago)], limit=1)
            if pm:
                move_vals["l10n_mx_edi_payment_method_id"] = pm.id

        # Lineas — los montos van en la moneda del CFDI (Odoo convierte con el TC)
        lines = []
        for concept in self.concept_ids:
            line_vals = self._build_invoice_line(concept, partner, journal)
            if line_vals:
                lines.append((0, 0, line_vals))

        if not lines:
            account = self._get_account(partner, journal)
            if not account:
                raise UserError(
                    _("CFDI %s: no se encontro cuenta contable. "
                      "Asigne una cuenta al proveedor o al CFDI.") % self.uuid)
            lines.append((0, 0, {
                "name": "CFDI %s" % self.uuid,
                "quantity": 1.0,
                "price_unit": float(self.subtotal or self.total or 0.0),
                "account_id": account.id,
            }))

        move_vals["invoice_line_ids"] = lines
        move = self.env["account.move"].sudo().create(move_vals)

        # ── Forzar tipo de cambio del SAT ────────────────────────────────────
        # Si es moneda extranjera Y viene TC en el CFDI, lo aplicamos
        # para que el equivalente en MXN coincida exactamente con el CFDI.
        # Odoo guarda el TC como: 1 unidad de moneda extranjera = X MXN
        # El campo en account.move es invoice_currency_rate (Odoo 17+/18)
        # que representa cuantas unidades de company_currency = 1 move_currency
        if es_moneda_extranjera and self.tipo_cambio and self.tipo_cambio != 0:
            tc_sat = float(self.tipo_cambio)  # MXN por 1 USD (o la que sea)
            try:
                # invoice_currency_rate existe en Odoo 17+
                if _has_field("invoice_currency_rate"):
                    move.sudo().write({"invoice_currency_rate": tc_sat})
                else:
                    # Fallback: forzar via el campo interno de la tasa
                    # Odoo usa rate como: company_currency / move_currency
                    move.sudo().with_context(
                        check_move_validity=False
                    ).write({"invoice_currency_rate": tc_sat})
            except Exception as e:
                _logger.warning(
                    "SAT: no se pudo forzar tipo de cambio TC=%s para %s: %s",
                    tc_sat, move.name, str(e))

        # Adjuntar XML ANTES de postear
        self._attach_xml_to_move(move)

        try:
            move.sudo().action_post()
        except Exception as e:
            _logger.warning("SAT: no se pudo postear %s: %s", move.name, str(e))

        return move

    def _build_invoice_line(self, concept, partner, journal):
        product = self._find_product_by_unspsc(concept.clave_prod_serv)
        account = None
        if product:
            account = (
                product.property_account_expense_id
                or (product.categ_id and product.categ_id.property_account_expense_categ_id)
            )
        if not account:
            account = self._get_account(partner, journal)
        if not account:
            _logger.warning("SAT: sin cuenta para concepto '%s'", concept.descripcion)
            return None
        taxes = self._find_taxes(concept)
        line_vals = {
            "name": (concept.descripcion or concept.clave_prod_serv or "Concepto")[:256],
            "quantity": float(concept.cantidad or 1.0),
            "price_unit": float(concept.valor_unitario or 0.0),
            "account_id": account.id,
        }
        if product:
            line_vals["product_id"] = product.id
        if taxes:
            line_vals["tax_ids"] = [(6, 0, taxes.ids)]
        return line_vals

    def _get_account(self, partner, journal):
        """
        Prioridad de cuenta contable:
          1. cfdi.account_id_base  (definido directo en el CFDI)
          2. partner.account_id_base  (definido en el proveedor)
          3. journal.default_account_id  (default del diario de compras)
        """
        if self.account_id_base:
            return self.account_id_base
        if hasattr(partner, "account_id_base") and partner.account_id_base:
            return partner.account_id_base
        return journal.default_account_id or False

    def _find_product_by_unspsc(self, clave_prod_serv):
        if not clave_prod_serv:
            return False
        try:
            unspsc = self.env["product.unspsc.code"].sudo().search(
                [("code", "=", clave_prod_serv)], limit=1)
            if not unspsc:
                return False
            tmpl = self.env["product.template"].sudo().search(
                [("unspsc_code_id", "=", unspsc.id)], limit=1)
            return tmpl.product_variant_id if tmpl else False
        except Exception:
            return False

    def _find_taxes(self, concept):
        taxes = self.env["account.tax"].sudo().browse()
        for tl in concept.tax_line_ids:
            if not tl.tasa_o_cuota:
                continue
            amount = round(float(tl.tasa_o_cuota) * 100, 4)
            tax = self.env["account.tax"].sudo().search([
                ("company_id", "=", self.company_id.id),
                ("type_tax_use", "=", "purchase"),
                ("amount", "=", amount),
                ("amount_type", "=", "percent"),
            ], limit=1)
            if tax:
                taxes |= tax
        return taxes


