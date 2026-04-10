# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import io
import zipfile
from datetime import datetime, timedelta, timezone
import requests
import uuid
from lxml import etree

from .sat_ws_security import sign_element_with_reference, sign_element_enveloped
from .sat_fiel import BeiSatFiel

import logging

_logger = logging.getLogger(__name__)
from . import sat_ws_security

_logger.info("sat_ws_security loaded from: %s", sat_ws_security.__file__)

SOAPENV = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"

SAT_NS_SOL = "http://DescargaMasivaTerceros.sat.gob.mx"
SAT_NS_AUTH = "http://DescargaMasivaTerceros.gob.mx"


class SatSoapError(Exception):
    pass


class BeiSatDescargaMasivaClient:
    """
    Cliente mínimo para Descarga Masiva (SAT).
    Token debe enviarse como Authorization: WRAP access_token="Token" :contentReference[oaicite:2]{index=2}
    """

    URL_AUTENTICACION = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/Autenticacion/Autenticacion.svc"
    SOAP_ACTION_AUTENTICA = "http://DescargaMasivaTerceros.gob.mx/IAutenticacion/Autentica"

    URL_SOLICITUD = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/SolicitaDescargaService.svc"
    SOAP_ACTION_SOLICITA_RECIBIDOS = "http://DescargaMasivaTerceros.sat.gob.mx/ISolicitaDescargaService/SolicitaDescargaRecibidos"
    SOAP_ACTION_SOLICITA_EMITIDOS = "http://DescargaMasivaTerceros.sat.gob.mx/ISolicitaDescargaService/SolicitaDescargaEmitidos"

    URL_VERIFICA = "https://cfdidescargamasivasolicitud.clouda.sat.gob.mx/VerificaSolicitudDescargaService.svc"
    SOAP_ACTION_VERIFICA = "http://DescargaMasivaTerceros.sat.gob.mx/IVerificaSolicitudDescargaService/VerificaSolicitudDescarga"

    URL_DESCARGA = "https://cfdidescargamasiva.clouda.sat.gob.mx/DescargaMasivaService.svc"
    SOAP_ACTION_DESCARGA = "http://DescargaMasivaTerceros.sat.gob.mx/IDescargaMasivaTercerosService/Descargar"

    def __init__(self, fiel: BeiSatFiel, timeout=60, verify_ssl=True):
        self.fiel = fiel
        self.timeout = timeout
        self.verify_ssl = verify_ssl

    # ---------------------------
    # Helpers
    # ---------------------------
    def _post(self, url, soap_action, xml_bytes: bytes, token: str = None) -> etree._Element:
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "Accept": "text/xml",
            "SOAPAction": soap_action,
        }
        if token:
            # SAT pide: Authorization: WRAP access_token="Token" :contentReference[oaicite:3]{index=3}
            headers["Authorization"] = f'WRAP access_token="{token}"'

        _logger.info("=== SAT SOAP REQUEST ===")
        _logger.info(xml_bytes.decode("utf-8"))
        _logger.info("SAT Headers: %s", headers)
        _logger.info("SAT URL: %s", url)

        resp = requests.post(url, data=xml_bytes, headers=headers, timeout=self.timeout, verify=self.verify_ssl)

        _logger.info("=== SAT SOAP RESPONSE ===")
        _logger.info(resp.content.decode("utf-8"))
        try:
            root = etree.fromstring(resp.content, parser=etree.XMLParser(huge_tree=True))
        except Exception as e:
            raise SatSoapError(f"Respuesta no XML: {resp.text}") from e

        if resp.status_code != 200:
            fault = root.find(f".//{{{SOAPENV}}}Fault")
            if fault is not None:
                msg = fault.findtext("faultstring") or resp.text
                raise SatSoapError(msg)
            raise SatSoapError(resp.text)

        return root

    def _c14n(self, element: etree._Element) -> bytes:
        return etree.tostring(element, method="c14n", exclusive=True)

    # ---------------------------
    # 1) Autenticación (WS-Security en Header)
    # ---------------------------
    def autentica(self, seconds_valid=300) -> str:
        # IMPORTANT: include u (WSU) prefix in Envelope nsmap
        # nsmap = {"s": SOAPENV, "o": WSSE, "u": WSU}
        nsmap = {"s": SOAPENV, "o": WSSE, "u": WSU}
        env = etree.Element(etree.QName(SOAPENV, "Envelope"), nsmap=nsmap)
        header = etree.SubElement(env, etree.QName(SOAPENV, "Header"))

        sec = etree.SubElement(header, etree.QName(WSSE, "Security"))
        sec.set(etree.QName(SOAPENV, "mustUnderstand"), "1")

        ts = etree.SubElement(sec, etree.QName(WSU, "Timestamp"))
        ts.set(etree.QName(WSU, "Id"), "_0")

        now = datetime.utcnow()
        created = etree.SubElement(ts, etree.QName(WSU, "Created"))
        expires = etree.SubElement(ts, etree.QName(WSU, "Expires"))
        created.text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        expires.text = (now + timedelta(seconds=seconds_valid)).strftime("%Y-%m-%dT%H:%M:%SZ")

        bst_id = f"uuid-{uuid.uuid4()}-1"
        bst = etree.SubElement(sec, etree.QName(WSSE, "BinarySecurityToken"))
        bst.set(etree.QName(WSU, "Id"), bst_id)
        bst.set("ValueType", "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3")
        bst.set("EncodingType",
                "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary")
        bst.text = self.fiel.get_certificate_b64()

        # Sign TIMESTAMP and reference BST in KeyInfo
        sig = sat_ws_security.sign_element_with_reference(self.fiel, ts, "#_0", bst_id=bst_id)
        sec.append(sig)

        body = etree.SubElement(env, etree.QName(SOAPENV, "Body"))
        etree.SubElement(body, etree.QName(SAT_NS_AUTH, "Autentica"), nsmap={None: SAT_NS_AUTH})

        xml_bytes = etree.tostring(env, xml_declaration=True, encoding="utf-8")
        root = self._post(self.URL_AUTENTICACION, self.SOAP_ACTION_AUTENTICA, xml_bytes)

        token = root.findtext(f".//{{{SAT_NS_AUTH}}}AutenticaResult")
        if not token:
            raise SatSoapError("No se obtuvo token en AutenticaResult")
        return token

    # ---------------------------
    # 2) Solicitar descarga recibidos
    # ---------------------------
    def solicita_descarga_recibidos(
            self,
            token: str,
            rfc_solicitante: str,
            fecha_ini: datetime,
            fecha_fin: datetime,
            tipo_comprobante: str = "I",  # I=Ingreso (facturas de proveedores), E=Egreso, N=Nómina, P=Pago
            tipo_solicitud: str = "CFDI",
            rfc_receptor: str | None = None,
            estado_comprobante: str = "Vigente",  # 1=Vigente, 0=Cancelado  (omitir = ambos)
    ) -> dict:
        """
        Solicita CFDIs recibidos por el RFC solicitante.
        TipoComprobante: I=Ingreso, E=Egreso, N=Nómina, P=Pago, T=Traslado.
        """
        # Sin xmlns:u en el Envelope (el SAT no lo espera aquí)
        nsmap = {"s": SOAPENV, "des": SAT_NS_SOL}
        env = etree.Element(etree.QName(SOAPENV, "Envelope"), nsmap=nsmap)
        etree.SubElement(env, etree.QName(SOAPENV, "Header"))
        body = etree.SubElement(env, etree.QName(SOAPENV, "Body"))

        op = etree.SubElement(body, etree.QName(SAT_NS_SOL, "SolicitaDescargaRecibidos"))
        solicitud = etree.SubElement(op, etree.QName(SAT_NS_SOL, "solicitud"))

        # EstadoComprobante: 1=Vigente, 0=Cancelado. Omitir = traer ambos.
        if estado_comprobante is not None:
            solicitud.set("EstadoComprobante", estado_comprobante)

        # Fechas en UTC sin zona horaria (formato que acepta el SAT)
        fecha_ini_str = fecha_ini.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        fecha_fin_str = fecha_fin.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        solicitud.set("FechaFinal", fecha_fin_str)
        solicitud.set("FechaInicial", fecha_ini_str)

        # Para "Recibidos", RfcReceptor = el RFC que recibe (NEX)
        solicitud.set("RfcReceptor", (rfc_receptor or rfc_solicitante).upper())
        solicitud.set("RfcSolicitante", rfc_solicitante.upper())
        solicitud.set("TipoSolicitud", tipo_solicitud)
        solicitud.set("TipoComprobante", tipo_comprobante)
        # SIN u:Id — no es necesario para enveloped-signature y contamina el digest

        # Firma (SAT): ENVELOPED signature dentro del nodo <solicitud> (Reference URI="").
        sign_element_enveloped(self.fiel, solicitud)

        xml_bytes = etree.tostring(env, xml_declaration=True, encoding="utf-8")
        root = self._post(self.URL_SOLICITUD, self.SOAP_ACTION_SOLICITA_RECIBIDOS, xml_bytes, token=token)

        result = root.find(f".//{{{SAT_NS_SOL}}}SolicitaDescargaRecibidosResult")
        if result is None:
            raise SatSoapError("No se encontró SolicitaDescargaRecibidosResult")

        return {
            "id_solicitud": result.get("IdSolicitud"),
            "cod_estatus": result.get("CodEstatus"),
            "mensaje": result.get("Mensaje"),
        }

    # ---------------------------
    # 2b) Solicitar descarga emitidos
    # ---------------------------
    def solicita_descarga_emitidos(
            self,
            token: str,
            rfc_solicitante: str,
            fecha_ini: datetime,
            fecha_fin: datetime,
            tipo_comprobante: str = "E",
            tipo_solicitud: str = "CFDI",
            rfc_emisor: str | None = None,
            estado_comprobante: str = "Vigente",
    ) -> dict:
        """
        Solicita CFDIs emitidos por el RFC solicitante.
        Para descargar facturas que el RFC ha emitido a sus clientes.
        Usa RfcEmisor en lugar de RfcReceptor.
        """
        nsmap = {"s": SOAPENV, "des": SAT_NS_SOL}
        env = etree.Element(etree.QName(SOAPENV, "Envelope"), nsmap=nsmap)
        etree.SubElement(env, etree.QName(SOAPENV, "Header"))
        body = etree.SubElement(env, etree.QName(SOAPENV, "Body"))

        op = etree.SubElement(body, etree.QName(SAT_NS_SOL, "SolicitaDescargaEmitidos"))
        solicitud = etree.SubElement(op, etree.QName(SAT_NS_SOL, "solicitud"))

        if estado_comprobante is not None:
            solicitud.set("EstadoComprobante", estado_comprobante)

        fecha_ini_str = fecha_ini.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        fecha_fin_str = fecha_fin.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        solicitud.set("FechaFinal", fecha_fin_str)
        solicitud.set("FechaInicial", fecha_ini_str)

        # Para "Emitidos", RfcEmisor = el RFC que emite (el solicitante)
        solicitud.set("RfcEmisor", (rfc_emisor or rfc_solicitante).upper())
        solicitud.set("RfcSolicitante", rfc_solicitante.upper())
        solicitud.set("TipoSolicitud", tipo_solicitud)
        solicitud.set("TipoComprobante", tipo_comprobante)

        sign_element_enveloped(self.fiel, solicitud)

        xml_bytes = etree.tostring(env, xml_declaration=True, encoding="utf-8")
        root = self._post(self.URL_SOLICITUD, self.SOAP_ACTION_SOLICITA_EMITIDOS, xml_bytes, token=token)

        result = root.find(f".//{{{SAT_NS_SOL}}}SolicitaDescargaEmitidosResult")
        if result is None:
            raise SatSoapError("No se encontró SolicitaDescargaEmitidosResult")

        return {
            "id_solicitud": result.get("IdSolicitud"),
            "cod_estatus": result.get("CodEstatus"),
            "mensaje": result.get("Mensaje"),
        }

    # ---------------------------
    # 3) Verificar solicitud
    # ---------------------------
    def verifica_solicitud(self, token: str, rfc_solicitante: str, id_solicitud: str) -> dict:
        nsmap = {"s": SOAPENV, "des": SAT_NS_SOL}
        env = etree.Element(etree.QName(SOAPENV, "Envelope"), nsmap=nsmap)
        etree.SubElement(env, etree.QName(SOAPENV, "Header"))
        body = etree.SubElement(env, etree.QName(SOAPENV, "Body"))

        op = etree.SubElement(body, etree.QName(SAT_NS_SOL, "VerificaSolicitudDescarga"))
        solicitud = etree.SubElement(op, etree.QName(SAT_NS_SOL, "solicitud"))
        solicitud.set("IdSolicitud", id_solicitud)
        solicitud.set("RfcSolicitante", rfc_solicitante.upper())

        sign_element_enveloped(self.fiel, solicitud)

        xml_bytes = etree.tostring(env, xml_declaration=True, encoding="utf-8")
        root = self._post(self.URL_VERIFICA, self.SOAP_ACTION_VERIFICA, xml_bytes, token=token)

        result = root.find(f".//{{{SAT_NS_SOL}}}VerificaSolicitudDescargaResult")
        if result is None:
            raise SatSoapError("No se encontró VerificaSolicitudDescargaResult")

        paquetes = [n.text for n in result.findall(f".//{{{SAT_NS_SOL}}}IdsPaquetes") if n.text]

        return {
            "cod_estatus": result.get("CodEstatus"),
            "estado_solicitud": result.get("EstadoSolicitud"),
            "codigo_estado_solicitud": result.get("CodigoEstadoSolicitud"),
            "numero_cfdis": result.get("NumeroCFDIs"),
            "mensaje": result.get("Mensaje"),
            "paquetes": paquetes,
        }

    # ---------------------------
    # 4) Descargar paquete
    # ---------------------------
    def descarga_paquete(self, token: str, rfc_solicitante: str, id_paquete: str) -> bytes:
        nsmap = {"s": SOAPENV, "des": SAT_NS_SOL}
        env = etree.Element(etree.QName(SOAPENV, "Envelope"), nsmap=nsmap)
        etree.SubElement(env, etree.QName(SOAPENV, "Header"))
        body = etree.SubElement(env, etree.QName(SOAPENV, "Body"))

        op = etree.SubElement(body, etree.QName(SAT_NS_SOL, "PeticionDescargaMasivaTercerosEntrada"))
        pet = etree.SubElement(op, etree.QName(SAT_NS_SOL, "peticionDescarga"))
        pet.set("IdPaquete", id_paquete)
        pet.set("RfcSolicitante", rfc_solicitante.upper())

        sign_element_enveloped(self.fiel, pet)

        xml_bytes = etree.tostring(env, xml_declaration=True, encoding="utf-8")
        root = self._post(self.URL_DESCARGA, self.SOAP_ACTION_DESCARGA, xml_bytes, token=token)

        # El ZIP viene como base64 dentro del nodo Paquete
        paquete_node = root.find(f".//{{{SAT_NS_SOL}}}Paquete")
        if paquete_node is None or not paquete_node.text:
            raise SatSoapError("No se recibió Paquete (base64)")

        return base64.b64decode(paquete_node.text)

    # ---------------------------
    # ZIP -> XMLs
    # ---------------------------
    @staticmethod
    def iter_xmls_from_zip(zip_bytes: bytes):
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xml"):
                    yield name, zf.read(name)

