# -*- coding: utf-8 -*-
from dataclasses import dataclass, field
from decimal import Decimal
from lxml import etree

CFDI_NS_40 = "http://www.sat.gob.mx/cfd/4"
CFDI_NS_33 = "http://www.sat.gob.mx/cfd/3"
TFD_NS = "http://www.sat.gob.mx/TimbreFiscalDigital"

SUPPORTED_CFDI_NS = {CFDI_NS_40, CFDI_NS_33}


# Parsea: Comprobante, Emisor, Receptor, Conceptos, Impuestos y el TimbreFiscalDigital (UUID/FechaTimbrado).
# Soporta CFDI 3.3 y 4.0 — detecta la version por el namespace del nodo raiz.

def _d(val):
    if val is None or val == "":
        return None
    return Decimal(str(val))


@dataclass
class ParsedTaxLine:
    tipo: str = None  # traslado / retencion
    impuesto: str = None
    tipo_factor: str = None
    tasa_o_cuota: Decimal = None
    base: Decimal = None
    importe: Decimal = None


@dataclass
class ParsedConcept:
    clave_prod_serv: str = None
    no_identificacion: str = None
    cantidad: Decimal = None
    clave_unidad: str = None
    unidad: str = None
    descripcion: str = None
    valor_unitario: Decimal = None
    importe: Decimal = None
    descuento: Decimal = None
    impuestos: list[ParsedTaxLine] = field(default_factory=list)


@dataclass
class ParsedCfdi:
    version: str = None
    serie: str = None
    folio: str = None
    fecha: str = None
    lugar_expedicion: str = None
    tipo_comprobante: str = None
    moneda: str = None
    tipo_cambio: Decimal = None
    subtotal: Decimal = None
    descuento: Decimal = None
    total: Decimal = None
    metodo_pago: str = None
    forma_pago: str = None
    uso_cfdi: str = None

    rfc_emisor: str = None
    nombre_emisor: str = None
    rfc_receptor: str = None
    nombre_receptor: str = None

    uuid: str = None
    fecha_timbrado: str = None

    conceptos: list[ParsedConcept] = field(default_factory=list)


class CfdiParser:
    """
    Parser unico para CFDI 3.3 y 4.0.
    Detecta la version por el namespace del nodo Comprobante.
    """

    @staticmethod
    def _find_comprobante(root: etree._Element):
        """Localiza el nodo Comprobante aceptando cualquier namespace soportado."""
        tag = root.tag  # ej: {http://www.sat.gob.mx/cfd/4}Comprobante
        for ns in SUPPORTED_CFDI_NS:
            if tag == f"{{{ns}}}Comprobante":
                return root, ns
            comp = root.find(f".//{{{ns}}}Comprobante")
            if comp is not None:
                return comp, ns
        return None, None

    @staticmethod
    def parse(xml_bytes: bytes) -> ParsedCfdi:
        root = etree.fromstring(xml_bytes, parser=etree.XMLParser(huge_tree=True))
        comp, cfdi_ns = CfdiParser._find_comprobante(root)
        if comp is None:
            raise ValueError(
                "No se reconoce como CFDI valido (namespace no soportado). "
                "Se esperaba CFDI 3.3 o 4.0."
            )

        ns = {"cfdi": cfdi_ns, "tfd": TFD_NS}
        p = ParsedCfdi()
        a = comp.attrib

        # En 3.3 el atributo se llama "version" (minuscula), en 4.0 "Version"
        p.version = a.get("Version") or a.get("version")
        p.serie = a.get("Serie")
        p.folio = a.get("Folio")
        p.fecha = a.get("Fecha")
        p.lugar_expedicion = a.get("LugarExpedicion")
        p.tipo_comprobante = a.get("TipoDeComprobante")
        p.moneda = a.get("Moneda")
        p.tipo_cambio = _d(a.get("TipoCambio"))
        p.subtotal = _d(a.get("SubTotal"))
        p.descuento = _d(a.get("Descuento"))
        p.total = _d(a.get("Total"))
        p.metodo_pago = a.get("MetodoPago")
        p.forma_pago = a.get("FormaPago")

        emisor = comp.find("cfdi:Emisor", namespaces=ns)
        if emisor is not None:
            p.rfc_emisor = emisor.get("Rfc")
            p.nombre_emisor = emisor.get("Nombre")

        receptor = comp.find("cfdi:Receptor", namespaces=ns)
        if receptor is not None:
            p.rfc_receptor = receptor.get("Rfc")
            p.nombre_receptor = receptor.get("Nombre")
            p.uso_cfdi = receptor.get("UsoCFDI")

        # TimbreFiscalDigital — mismo namespace en 3.3 y 4.0
        tfd = comp.find(".//tfd:TimbreFiscalDigital", namespaces=ns)
        if tfd is not None:
            p.uuid = tfd.get("UUID")
            p.fecha_timbrado = tfd.get("FechaTimbrado")

        # Conceptos
        conceptos = comp.find("cfdi:Conceptos", namespaces=ns)
        if conceptos is not None:
            for c in conceptos.findall("cfdi:Concepto", namespaces=ns):
                pc = ParsedConcept(
                    clave_prod_serv=c.get("ClaveProdServ"),
                    no_identificacion=c.get("NoIdentificacion"),
                    cantidad=_d(c.get("Cantidad")),
                    clave_unidad=c.get("ClaveUnidad"),
                    unidad=c.get("Unidad"),
                    descripcion=c.get("Descripcion"),
                    valor_unitario=_d(c.get("ValorUnitario")),
                    importe=_d(c.get("Importe")),
                    descuento=_d(c.get("Descuento")),
                )

                impuestos = c.find("cfdi:Impuestos", namespaces=ns)
                if impuestos is not None:
                    traslados = impuestos.find("cfdi:Traslados", namespaces=ns)
                    if traslados is not None:
                        for t in traslados.findall("cfdi:Traslado", namespaces=ns):
                            pc.impuestos.append(ParsedTaxLine(
                                tipo="traslado",
                                impuesto=t.get("Impuesto"),
                                tipo_factor=t.get("TipoFactor"),
                                tasa_o_cuota=_d(t.get("TasaOCuota")),
                                base=_d(t.get("Base")),
                                importe=_d(t.get("Importe")),
                            ))
                    retenciones = impuestos.find("cfdi:Retenciones", namespaces=ns)
                    if retenciones is not None:
                        for r in retenciones.findall("cfdi:Retencion", namespaces=ns):
                            pc.impuestos.append(ParsedTaxLine(
                                tipo="retencion",
                                impuesto=r.get("Impuesto"),
                                tipo_factor=r.get("TipoFactor"),
                                tasa_o_cuota=_d(r.get("TasaOCuota")),
                                base=_d(r.get("Base")),
                                importe=_d(r.get("Importe")),
                            ))

                p.conceptos.append(pc)

        return p


# Alias de compatibilidad — el codigo existente importa Cfdi40Parser
Cfdi40Parser = CfdiParser

