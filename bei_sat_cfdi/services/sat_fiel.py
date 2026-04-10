# -*- coding: utf-8 -*-
import base64
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


@dataclass
class BeiSatFiel:
    cer_der: bytes
    key_der: bytes
    password: str

    def __post_init__(self):
        self._cert = x509.load_der_x509_certificate(self.cer_der)
        self._key = serialization.load_der_private_key(
            self.key_der,
            password=(self.password or "").encode("utf-8") if self.password is not None else None,
        )
        # Validación fuerte: CER y KEY deben corresponder
        pub_cert = self._cert.public_key().public_numbers()
        pub_key = self._key.public_key().public_numbers()
        if pub_cert.n != pub_key.n or pub_cert.e != pub_key.e:
            raise ValueError("El CER no corresponde con el KEY (par incorrecto). Verifica que sea la e.firma correcta.")

    # -----------------------------
    # Cert helpers
    # -----------------------------
    def cer_to_base64(self) -> bytes:
        return base64.b64encode(self.cer_der)

    def get_certificate_b64(self) -> str:
        return self.cer_to_base64().decode("utf-8")

    def cer_serial_number(self) -> str:
        return str(self._cert.serial_number)

    def cer_issuer(self) -> str:
        parts = []
        for rdn in self._cert.issuer.rdns:
            for attr in rdn:
                parts.append(f"{attr.oid._name}={attr.value}")
        return ",".join(parts)

    def get_issuer_and_serial(self):
        return self.cer_issuer(), self.cer_serial_number()

    # -----------------------------
    # Signing helpers (RSA-SHA1)
    # -----------------------------
    def sign_sha1_raw(self, data: bytes) -> bytes:
        """Firma RSA-SHA1 y regresa BYTES RAW (no base64)."""
        return self._key.sign(data, padding.PKCS1v15(), hashes.SHA1())

    # Compat (algunos callers esperan sign_sha1/firmar_sha1)
    def sign_sha1(self, data: bytes) -> bytes:
        """Compat: regresa BYTES RAW (no base64)."""
        return self.sign_sha1_raw(data)

    def firmar_sha1(self, data: bytes) -> bytes:
        """Compat legacy: regresa BASE64 de la firma raw."""
        return base64.b64encode(self.sign_sha1_raw(data))
