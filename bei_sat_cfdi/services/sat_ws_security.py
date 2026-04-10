# -*- coding: utf-8 -*-
"""WS-Security + XMLDSig helpers for SAT Descarga Masiva (Terceros).

Soporta 2 estilos de firma:

1) Autenticacion/Autentica:
   - wsse/o:Security con Timestamp (wsu:Id)
   - BinarySecurityToken con el certificado X509 (wsu:Id = bst_id)
   - ds:Signature referencia el Timestamp (Reference URI="#<TimestampId>")
   - ds:KeyInfo contiene wsse:SecurityTokenReference apuntando al BST (URI="#<bst_id>")

2) Solicitud / Verifica / Descarga:
   - ds:Signature sobre el elemento con wsu:Id (normalmente <solicitud>)
   - ds:KeyInfo usa X509Data (Cert + IssuerSerial)

La función `sign_element_with_reference` hace ambas:
- si `bst_id` viene, usa SecurityTokenReference
- si no, usa X509Data
"""

import base64
import io
import hashlib
from lxml import etree

DSIG_NS = "http://www.w3.org/2000/09/xmldsig#"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
EC_NS = "http://www.w3.org/2001/10/xml-exc-c14n#"

DS_NS = "http://www.w3.org/2000/09/xmldsig#"
SOAPENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

def _c14n_exclusive(element: etree._Element, inclusive_prefixes=None) -> bytes:
    prefixes = None
    if inclusive_prefixes:
        seen = set()
        prefixes = []
        for p in inclusive_prefixes:
            if not p or not isinstance(p, str):
                continue
            if p in seen:
                continue
            seen.add(p)
            prefixes.append(p)
        if not prefixes:
            prefixes = None
    return etree.tostring(
        element,
        method="c14n",
        exclusive=True,
        with_comments=False,
        inclusive_ns_prefixes=prefixes,
    )



def _c14n_inclusive(element: etree._Element) -> bytes:
    """Canonicalize with inclusive C14N (REC-xml-c14n-20010315).

    Uses etree.tostring(method='c14n', exclusive=False) which correctly propagates
    namespace declarations from ancestor elements (e.g. xmlns:s, xmlns:u from the
    Envelope), matching exactly what the SAT verifies when validating the XMLDSig.
    The previous re-parse approach lost that ancestor namespace context.
    """
    return etree.tostring(element, method="c14n", exclusive=False, with_comments=False)

def _sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode("utf-8")


def _build_signature(reference_uri: str, bst_id: str | None, inclusive_prefixes=None) -> etree._Element:
    nsmap = {"ds": DSIG_NS}
    if inclusive_prefixes:
        nsmap["ec"] = EC_NS
    sig = etree.Element(etree.QName(DSIG_NS, "Signature"), nsmap=nsmap)

    signed_info = etree.SubElement(sig, etree.QName(DSIG_NS, "SignedInfo"))
    etree.SubElement(
        signed_info,
        etree.QName(DSIG_NS, "CanonicalizationMethod"),
        Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    if inclusive_prefixes:
        prefix_list = " ".join([p for p in inclusive_prefixes if p])
        canon_method = signed_info.find(".//{http://www.w3.org/2000/09/xmldsig#}CanonicalizationMethod")
        etree.SubElement(canon_method, etree.QName(EC_NS, "InclusiveNamespaces"), PrefixList=prefix_list)
    etree.SubElement(
        signed_info,
        etree.QName(DSIG_NS, "SignatureMethod"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1",
    )

    ref = etree.SubElement(signed_info, etree.QName(DSIG_NS, "Reference"), URI=reference_uri)
    transforms = etree.SubElement(ref, etree.QName(DSIG_NS, "Transforms"))
    etree.SubElement(
        transforms,
        etree.QName(DSIG_NS, "Transform"),
        Algorithm="http://www.w3.org/2001/10/xml-exc-c14n#",
    )
    if inclusive_prefixes:
        prefix_list = " ".join([p for p in inclusive_prefixes if p])
        transform = transforms.find(".//{http://www.w3.org/2000/09/xmldsig#}Transform")
        etree.SubElement(transform, etree.QName(EC_NS, "InclusiveNamespaces"), PrefixList=prefix_list)
    etree.SubElement(ref, etree.QName(DSIG_NS, "DigestMethod"), Algorithm="http://www.w3.org/2000/09/xmldsig#sha1")
    etree.SubElement(ref, etree.QName(DSIG_NS, "DigestValue"))

    etree.SubElement(sig, etree.QName(DSIG_NS, "SignatureValue"))

    key_info = etree.SubElement(sig, etree.QName(DSIG_NS, "KeyInfo"))
    if bst_id:
        str_el = etree.SubElement(key_info, etree.QName(WSSE_NS, "SecurityTokenReference"), nsmap={"wsse": WSSE_NS})
        etree.SubElement(
            str_el,
            etree.QName(WSSE_NS, "Reference"),
            URI=f"#{bst_id}",
            ValueType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-x509-token-profile-1.0#X509v3",
        )
    else:
        x509_data = etree.SubElement(key_info, etree.QName(DSIG_NS, "X509Data"))
        etree.SubElement(x509_data, etree.QName(DSIG_NS, "X509Certificate"))
        issuer_serial = etree.SubElement(x509_data, etree.QName(DSIG_NS, "X509IssuerSerial"))
        etree.SubElement(issuer_serial, etree.QName(DSIG_NS, "X509IssuerName"))
        etree.SubElement(issuer_serial, etree.QName(DSIG_NS, "X509SerialNumber"))

    return sig


def sign_element_with_reference(fiel, element_to_digest: etree._Element, reference_uri: str, bst_id: str | None = None, inclusive_prefixes=None) -> etree._Element:
    sig = _build_signature(reference_uri, bst_id, inclusive_prefixes=inclusive_prefixes)

    digest_value_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}DigestValue")
    signed_info_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}SignedInfo")
    signature_value_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}SignatureValue")

    digest_value_node.text = _sha1_b64(_c14n_exclusive(element_to_digest, inclusive_prefixes=inclusive_prefixes))

    signed_info_c14n = _c14n_exclusive(signed_info_node, inclusive_prefixes=inclusive_prefixes)
    signature_value_node.text = fiel.firmar_sha1(signed_info_c14n).decode("utf-8")

    if not bst_id:
        x509_cert_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate")
        issuer_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}X509IssuerName")
        serial_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}X509SerialNumber")

        x509_cert_node.text = fiel.cer_to_base64().decode("utf-8")

        if hasattr(fiel, "cer_issuer") and hasattr(fiel, "cer_serial_number"):
            issuer_node.text = fiel.cer_issuer()
            serial_node.text = fiel.cer_serial_number()
        elif hasattr(fiel, "get_issuer_and_serial"):
            issuer, serial = fiel.get_issuer_and_serial()
            issuer_node.text = issuer
            serial_node.text = str(serial)
        else:
            issuer_node.text = ""
            serial_node.text = ""

    return sig


def _build_signature_enveloped() -> etree._Element:
    sig = etree.Element(etree.QName(DS_NS, "Signature"), nsmap={"ds": DS_NS})
    signed_info = etree.SubElement(sig, etree.QName(DS_NS, "SignedInfo"))

    etree.SubElement(
        signed_info,
        etree.QName(DS_NS, "CanonicalizationMethod"),
        Algorithm="http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
    )
    etree.SubElement(
        signed_info,
        etree.QName(DS_NS, "SignatureMethod"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#rsa-sha1",
    )

    ref = etree.SubElement(signed_info, etree.QName(DS_NS, "Reference"), URI="")
    transforms = etree.SubElement(ref, etree.QName(DS_NS, "Transforms"))
    etree.SubElement(
        transforms,
        etree.QName(DS_NS, "Transform"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature",
    )
    etree.SubElement(
        ref,
        etree.QName(DS_NS, "DigestMethod"),
        Algorithm="http://www.w3.org/2000/09/xmldsig#sha1",
    )
    etree.SubElement(ref, etree.QName(DS_NS, "DigestValue")).text = ""

    etree.SubElement(sig, etree.QName(DS_NS, "SignatureValue")).text = ""

    key_info = etree.SubElement(sig, etree.QName(DS_NS, "KeyInfo"))
    x509_data = etree.SubElement(key_info, etree.QName(DS_NS, "X509Data"))
    etree.SubElement(x509_data, etree.QName(DS_NS, "X509Certificate")).text = ""
    issuer_serial = etree.SubElement(x509_data, etree.QName(DS_NS, "X509IssuerSerial"))
    etree.SubElement(issuer_serial, etree.QName(DS_NS, "X509IssuerName")).text = ""
    etree.SubElement(issuer_serial, etree.QName(DS_NS, "X509SerialNumber")).text = ""

    return sig


def sign_element_enveloped(fiel, element_to_sign: etree._Element) -> etree._Element:
    """Create an enveloped signature inside element_to_sign (SAT Descarga Masiva style).
    
    The enveloped-signature transform means: digest is over the element WITHOUT <Signature>.
    We compute the digest first (before appending sig), then append sig, then sign SignedInfo.
    """
    for prev in element_to_sign.xpath(".//ds:Signature", namespaces={"ds": DS_NS}):
        prev.getparent().remove(prev)

    sig = _build_signature_enveloped()

    digest_value_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}DigestValue")
    signed_info_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}SignedInfo")
    signature_value_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}SignatureValue")

    # Step 1: Digest = C14N of element WITHOUT Signature (compute BEFORE appending sig)
    digest_value_node.text = _sha1_b64(_c14n_inclusive(element_to_sign))

    # Step 2: Append Signature into element so SignedInfo inherits correct namespace context
    element_to_sign.append(sig)

    # Step 3: Sign SignedInfo (inclusive C14N, now part of the full tree)
    signed_info_c14n = _c14n_inclusive(signed_info_node)
    signature_value_node.text = fiel.firmar_sha1(signed_info_c14n).decode("utf-8")

    # Step 4: KeyInfo
    x509_cert_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate")
    issuer_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}X509IssuerName")
    serial_node = sig.find(".//{http://www.w3.org/2000/09/xmldsig#}X509SerialNumber")

    x509_cert_node.text = fiel.cer_to_base64().decode("utf-8")

    if hasattr(fiel, "cer_issuer") and hasattr(fiel, "cer_serial_number"):
        issuer_node.text = fiel.cer_issuer()
        serial_node.text = fiel.cer_serial_number()
    elif hasattr(fiel, "get_issuer_and_serial"):
        issuer, serial = fiel.get_issuer_and_serial()
        issuer_node.text = issuer
        serial_node.text = str(serial)

    return sig
