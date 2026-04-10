# -*- coding: utf-8 -*-
{
    "name": "BEI - Descarga SAT CFDI (Recibidos/Egreso)",
    "version": "18.0.1.0.0",
    "category": "Accounting/Localization",
    "summary": "Descarga masiva de CFDI desde el SAT (e.firma) y conciliación automática con asientos contables.",
    "description": """
BEI - Descarga Masiva de CFDI desde el SAT
===========================================

Conecta Odoo directamente con el servicio de **Descarga Masiva del SAT**
para obtener de forma automática todos los CFDI emitidos y recibidos de la
empresa, y conciliarlos contra los documentos contables existentes
(facturas de proveedores y clientes) sin intervención manual.

¿Qué problema resuelve?
-----------------------

Los equipos de contabilidad en México dedican horas cada mes a descargar
manualmente comprobantes del portal del SAT, importarlos y compararlos contra
los registros de Odoo. Este módulo elimina ese proceso por completo: la
descarga, el almacenamiento y la conciliación ocurren de forma automática y
programada dentro de Odoo.

Funcionalidades principales
----------------------------

* **Descarga automática vía web service oficial**: solicita y descarga paquetes
  ZIP de CFDI directamente al SAT usando la **e.firma (FIEL)** del
  contribuyente, sin acceder manualmente al portal.
* **CFDI emitidos y recibidos**: soporta todos los tipos de comprobante —
  Ingreso (I), Egreso (E), Pago (P), Nómina (N) y Traslado (T).
* **Almacenamiento estructurado**: cada CFDI se registra con UUID, datos del
  emisor y receptor, importes, moneda, método de pago y el XML original adjunto.
* **Conciliación automática en dos pasos**:

  1. Al importar los ZIP, vincula cada CFDI con su asiento contable por UUID
     o por coincidencia de RFC, fecha e importe.
  2. Para los CFDI sin match, permite crear el ``account.move`` directamente
     desde el XML con un solo clic, adjuntando el comprobante al asiento.

* **Credenciales e.firma por compañía**: gestiona de forma segura el
  certificado (.cer) y la llave privada (.key) de cada empresa.
* **Tarea programada (cron)**: ejecuta solicitudes y descargas de forma
  automática en el horario configurado.
* **Multi-empresa**: cada compañía gestiona sus propias credenciales y CFDI
  de forma independiente.

Flujo de uso
------------

1. Registra las credenciales e.firma (.cer + .key) en **SAT › Credenciales**.
2. Configura el tipo de descarga (recibidos / emitidos) y tipo de comprobante.
3. Ejecuta la descarga manualmente o deja que el cron la programe.
4. Revisa los CFDI en **SAT › CFDI Descargados**: los conciliados quedan
   vinculados a su factura; los pendientes se resuelven en un solo paso.

Requisitos técnicos
-------------------

* Odoo 18.0 Community o Enterprise.
* e.firma (FIEL) vigente del contribuyente (certificado .cer + llave .key).
* Librerías Python: ``zeep``, ``cryptography``, ``lxml``
  (incluidas en instalaciones estándar de Odoo).
""",
    "author": "BEI",
    "website": "https://bei.mx",
    "support": "ventas@bei.mx",
    "license": "OPL-1",
    "price": 120.00,
    "currency": "USD",
    "depends": ["base", "account", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/sat_download_wizard_views.xml",
        "views/sat_credential_views.xml",
        "views/sat_download_request_views.xml",
        "views/sat_cfdi_views.xml",
        "data/ir_cron.xml",
        "data/menus.xml",
    ],
    "images": [
        "static/description/banner.png",
    ],
    "installable": True,
    "application": True,
    "auto_install": False,
}
