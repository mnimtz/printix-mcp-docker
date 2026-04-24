"""
IPP-Protokoll-Parser + Response-Builder (v6.5.0)
=================================================
Minimal-viable Implementierung von RFC 8010/8011 für genau den Use-Case:
Printix sendet einen Print-Job via IPPS an unseren Endpoint.

Wir parsen:
  - IPP-Header (Version, Operation-ID, Request-ID)
  - Attribute-Groups (operation-attributes, job-attributes)
  - Document-Data (nach end-of-attributes-Tag)

Wir unterstützen folgende Operationen (minimal):
  - 0x0002 Print-Job     → akzeptieren, parsen, Daten + Metadaten zurückgeben
  - 0x0004 Validate-Job  → sofortige OK-Response (Printix prüft damit)
  - 0x000B Get-Printer-Attributes → einfache Printer-Infos
  - 0x000A Get-Jobs      → leere Liste

Alle anderen Operationen antworten mit 0x0501 (client-error-operation-not-supported).

IPP-Message-Format:
    +---------------+----------------------------+
    | version-number| 2 Bytes (major + minor)    |
    | operation-id  | 2 Bytes                    |
    | request-id    | 4 Bytes                    |
    | attr-groups   | variable (TLV-codiert)     |
    | end-tag 0x03  | 1 Byte                     |
    | data          | rest of body               |
    +---------------+----------------------------+
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("printix.cloudprint.ipp")


# ─── Protocol Constants (RFC 8010) ───────────────────────────────────────────

# Delimiter-Tags (begin/end Attribute-Groups)
TAG_OPERATION_ATTRIBUTES_TAG = 0x01
TAG_JOB_ATTRIBUTES_TAG       = 0x02
TAG_END_OF_ATTRIBUTES        = 0x03
TAG_PRINTER_ATTRIBUTES_TAG   = 0x04
TAG_UNSUPPORTED_ATTRIBUTES   = 0x05

# Value-Tags (intrinsic)
TAG_UNSUPPORTED   = 0x10
TAG_UNKNOWN       = 0x12
TAG_NO_VALUE      = 0x13
TAG_INTEGER       = 0x21
TAG_BOOLEAN       = 0x22
TAG_ENUM          = 0x23
TAG_OCTETSTRING   = 0x30
TAG_DATETIME      = 0x31
TAG_RESOLUTION    = 0x32
TAG_RANGE         = 0x33
TAG_TEXT_LANG     = 0x35
TAG_NAME_LANG     = 0x36
TAG_TEXT          = 0x41
TAG_NAME          = 0x42
TAG_KEYWORD       = 0x44
TAG_URI           = 0x45
TAG_URI_SCHEME    = 0x46
TAG_CHARSET       = 0x47
TAG_NATURAL_LANG  = 0x48
TAG_MIME_TYPE     = 0x49
TAG_MEMBER_NAME   = 0x4A

# Operation-IDs
OP_PRINT_JOB                = 0x0002
OP_VALIDATE_JOB             = 0x0004
OP_GET_JOBS                 = 0x000A
OP_GET_PRINTER_ATTRIBUTES   = 0x000B
OP_CANCEL_JOB               = 0x0008
OP_CREATE_JOB               = 0x0005
OP_SEND_DOCUMENT            = 0x0006

# Status-Codes
STATUS_SUCCESSFUL_OK          = 0x0000
STATUS_SUCCESSFUL_OK_IGNORED  = 0x0001
STATUS_CLIENT_ERROR_BAD       = 0x0400
STATUS_CLIENT_ERROR_UNSUP_OP  = 0x0501

# Job-States
JOB_STATE_PENDING       = 3
JOB_STATE_PROCESSING    = 5
JOB_STATE_COMPLETED     = 9

STRING_TAGS = {
    TAG_TEXT, TAG_NAME, TAG_KEYWORD, TAG_URI, TAG_URI_SCHEME,
    TAG_CHARSET, TAG_NATURAL_LANG, TAG_MIME_TYPE, TAG_OCTETSTRING,
    TAG_MEMBER_NAME,
}


# ─── Request-Datenklassen ────────────────────────────────────────────────────

@dataclass
class IppAttribute:
    name: str
    value_tag: int
    value: Any
    # Bei Multi-Value: alle Werte in list
    values: list = field(default_factory=list)


@dataclass
class IppRequest:
    version: tuple[int, int]                # (major, minor)
    operation_id: int
    request_id: int
    operation_attrs: dict[str, IppAttribute]
    job_attrs: dict[str, IppAttribute]
    data: bytes                             # Rest nach end-of-attributes-Tag
    data_offset: int                        # Byte-Position wo Daten starten
    # v6.7.3: Alle weiteren Attribute-Gruppen (printer-attrs, document-attrs,
    # subscription-attrs, custom usw.) — gemappt nach Group-Tag-Hex.
    other_groups: dict[int, dict[str, IppAttribute]] = field(default_factory=dict)

    def attr(self, name: str, default: Any = "") -> Any:
        """Convenience-Accessor: sucht in operation_attrs zuerst, dann job_attrs,
        dann in allen anderen Gruppen.
        """
        a = self.operation_attrs.get(name) or self.job_attrs.get(name)
        if a is None:
            for group_attrs in self.other_groups.values():
                a = group_attrs.get(name)
                if a is not None:
                    break
        if a is None:
            return default
        if a.values:
            return a.values[0]
        return a.value if a.value is not None else default

    def all_groups(self) -> dict[str, dict[str, IppAttribute]]:
        """Alle Attribut-Gruppen mit lesbaren Namen — für Logging."""
        result: dict[str, dict[str, IppAttribute]] = {
            "operation": self.operation_attrs,
            "job":       self.job_attrs,
        }
        names = {
            0x04: "printer", 0x05: "unsupported", 0x06: "subscription",
            0x07: "event-notification", 0x08: "resource", 0x09: "document",
        }
        for tag, attrs in self.other_groups.items():
            result[names.get(tag, f"group-0x{tag:02x}")] = attrs
        return result


# ─── Parser ──────────────────────────────────────────────────────────────────

class IppParseError(Exception):
    pass


def parse_request(body: bytes) -> IppRequest:
    """Parst einen IPP-Request-Body. Nicht alle Felder werden inhaltlich
    interpretiert — nur was wir brauchen.
    """
    if len(body) < 8:
        raise IppParseError(f"Body zu kurz: {len(body)} Bytes")

    # Header
    major, minor = body[0], body[1]
    operation_id = int.from_bytes(body[2:4], "big")
    request_id   = int.from_bytes(body[4:8], "big")

    pos = 8
    current_group: Optional[int] = None
    operation_attrs: dict[str, IppAttribute] = {}
    job_attrs: dict[str, IppAttribute] = {}
    other_groups: dict[int, dict[str, IppAttribute]] = {}
    last_attr: Optional[IppAttribute] = None

    while pos < len(body):
        tag = body[pos]
        pos += 1

        # Delimiter-Tag? (<= 0x0F)
        if tag <= 0x0F:
            if tag == TAG_END_OF_ATTRIBUTES:
                break
            current_group = tag
            last_attr = None
            continue

        # Value-Tag → Attribut-Eintrag
        if pos + 2 > len(body):
            raise IppParseError("Abrupt: name-length expected")
        name_len = int.from_bytes(body[pos:pos+2], "big")
        pos += 2

        name = body[pos:pos+name_len].decode("utf-8", errors="replace")
        pos += name_len

        if pos + 2 > len(body):
            raise IppParseError("Abrupt: value-length expected")
        value_len = int.from_bytes(body[pos:pos+2], "big")
        pos += 2

        raw_value = body[pos:pos+value_len]
        pos += value_len

        parsed_value = _decode_value(tag, raw_value)

        if name:
            # Neues Attribut — registrieren in der Gruppe in der wir gerade sind.
            # v6.7.3: Wir tracken jetzt ALLE Gruppen (printer, document,
            # subscription, custom), nicht mehr "alles in job_attrs".
            attr = IppAttribute(name=name, value_tag=tag, value=parsed_value)
            attr.values.append(parsed_value)
            if current_group == TAG_OPERATION_ATTRIBUTES_TAG:
                operation_attrs[name] = attr
            elif current_group == TAG_JOB_ATTRIBUTES_TAG:
                job_attrs[name] = attr
            elif current_group is not None:
                bucket = other_groups.setdefault(current_group, {})
                bucket[name] = attr
            else:
                # Kein Group-Tag aktiv — sehr ungewöhnlich, dump als job_attrs
                job_attrs[name] = attr
            last_attr = attr
        else:
            # Zusatzwert für vorheriges Attribut (Multi-Value)
            if last_attr is not None:
                last_attr.values.append(parsed_value)

    data = body[pos:]
    return IppRequest(
        version=(major, minor),
        operation_id=operation_id,
        request_id=request_id,
        operation_attrs=operation_attrs,
        job_attrs=job_attrs,
        data=data,
        data_offset=pos,
        other_groups=other_groups,
    )


def _decode_value(tag: int, raw: bytes) -> Any:
    """Dekodiert den Bytes-Wert anhand des Tag."""
    try:
        if tag in STRING_TAGS:
            return raw.decode("utf-8", errors="replace")
        if tag in (TAG_TEXT_LANG, TAG_NAME_LANG):
            # 2-Byte-Length lang + lang-text + 2-Byte-Length text + text
            if len(raw) < 4:
                return raw.decode("utf-8", errors="replace")
            ll = int.from_bytes(raw[0:2], "big")
            lang = raw[2:2+ll].decode("utf-8", errors="replace")
            tl = int.from_bytes(raw[2+ll:4+ll], "big")
            text = raw[4+ll:4+ll+tl].decode("utf-8", errors="replace")
            return f"{text} ({lang})" if lang else text
        if tag == TAG_INTEGER or tag == TAG_ENUM:
            if len(raw) >= 4:
                return struct.unpack(">i", raw[:4])[0]
            return 0
        if tag == TAG_BOOLEAN:
            return bool(raw[0]) if raw else False
        if tag == TAG_RANGE:
            if len(raw) >= 8:
                lo, hi = struct.unpack(">ii", raw[:8])
                return (lo, hi)
            return (0, 0)
        if tag == TAG_RESOLUTION:
            if len(raw) >= 9:
                x, y, unit = struct.unpack(">iib", raw[:9])
                return {"x": x, "y": y, "unit": unit}
            return {}
        if tag == TAG_NO_VALUE or tag == TAG_UNKNOWN or tag == TAG_UNSUPPORTED:
            return None
        # Fallback: Bytes
        return raw
    except Exception as e:
        logger.debug("IPP decode error (tag=0x%02x): %s", tag, e)
        return raw


# ─── Response-Builder ────────────────────────────────────────────────────────

def _encode_attribute(tag: int, name: str, value: Any) -> bytes:
    """Encodiert ein einzelnes IPP-Attribut (ohne Multi-Value-Unterstützung)."""
    name_b = name.encode("utf-8") if name else b""

    if tag in STRING_TAGS:
        val_b = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    elif tag == TAG_INTEGER or tag == TAG_ENUM:
        val_b = struct.pack(">i", int(value))
    elif tag == TAG_BOOLEAN:
        val_b = bytes([1 if value else 0])
    else:
        val_b = value if isinstance(value, (bytes, bytearray)) else str(value).encode("utf-8")

    out = bytearray()
    out.append(tag & 0xFF)
    out += len(name_b).to_bytes(2, "big")
    out += name_b
    out += len(val_b).to_bytes(2, "big")
    out += val_b
    return bytes(out)


def build_response(request_id: int, status_code: int,
                    operation_attrs: Optional[list[tuple[int, str, Any]]] = None,
                    job_attrs: Optional[list[tuple[int, str, Any]]] = None,
                    printer_attrs: Optional[list[tuple[int, str, Any]]] = None,
                    version: tuple[int, int] = (1, 1)) -> bytes:
    """Baut einen IPP-Response. Attrs als Liste von (tag, name, value)-Tupeln.

    Operation-Attributes beinhalten IMMER attributes-charset + attributes-natural-language
    (nach RFC 8010). Wir fügen sie automatisch hinzu falls nicht gesetzt.
    """
    out = bytearray()
    out.append(version[0] & 0xFF)
    out.append(version[1] & 0xFF)
    out += status_code.to_bytes(2, "big")
    out += request_id.to_bytes(4, "big", signed=False)

    # operation-attributes-tag + charset + natural-language
    out.append(TAG_OPERATION_ATTRIBUTES_TAG)
    out += _encode_attribute(TAG_CHARSET, "attributes-charset", "utf-8")
    out += _encode_attribute(TAG_NATURAL_LANG, "attributes-natural-language", "en")

    for tag, name, value in (operation_attrs or []):
        if name in ("attributes-charset", "attributes-natural-language"):
            continue  # schon gesetzt
        out += _encode_attribute(tag, name, value)

    if job_attrs:
        out.append(TAG_JOB_ATTRIBUTES_TAG)
        for tag, name, value in job_attrs:
            out += _encode_attribute(tag, name, value)

    if printer_attrs:
        out.append(TAG_PRINTER_ATTRIBUTES_TAG)
        for tag, name, value in printer_attrs:
            out += _encode_attribute(tag, name, value)

    out.append(TAG_END_OF_ATTRIBUTES)
    return bytes(out)


def build_print_job_response(request_id: int, job_id: int,
                              printer_uri: str,
                              job_state: int = JOB_STATE_PROCESSING) -> bytes:
    """Baut eine Standard-Response für Print-Job (Status: successful-ok)."""
    job_uri = f"{printer_uri}/jobs/{job_id}"
    return build_response(
        request_id=request_id,
        status_code=STATUS_SUCCESSFUL_OK,
        job_attrs=[
            (TAG_URI,     "job-uri",           job_uri),
            (TAG_INTEGER, "job-id",            job_id),
            (TAG_ENUM,    "job-state",         job_state),
            (TAG_KEYWORD, "job-state-reasons", "none"),
        ],
    )


def build_validate_job_response(request_id: int) -> bytes:
    """Validate-Job: sofortiges OK ohne weitere Attribute."""
    return build_response(request_id=request_id, status_code=STATUS_SUCCESSFUL_OK)


def build_get_printer_attributes_response(request_id: int, printer_uri: str,
                                            printer_name: str = "Cloud Print Port") -> bytes:
    """Minimal-Attributset für Get-Printer-Attributes.

    Printix fragt das wahrscheinlich nicht ab, aber CUPS/Testtools tun's.
    Wir liefern die wichtigsten Standard-Felder damit der Drucker als
    'ready' erkannt wird.
    """
    return build_response(
        request_id=request_id,
        status_code=STATUS_SUCCESSFUL_OK,
        printer_attrs=[
            (TAG_URI,     "printer-uri-supported",         printer_uri),
            (TAG_KEYWORD, "uri-authentication-supported",  "none"),
            (TAG_KEYWORD, "uri-security-supported",        "tls"),
            (TAG_TEXT,    "printer-name",                  printer_name),
            (TAG_ENUM,    "printer-state",                 3),  # 3 = idle
            (TAG_KEYWORD, "printer-state-reasons",         "none"),
            (TAG_KEYWORD, "operations-supported",          "print-job"),
            (TAG_ENUM,    "operations-supported",          OP_VALIDATE_JOB),
            (TAG_CHARSET, "charset-configured",            "utf-8"),
            (TAG_CHARSET, "charset-supported",             "utf-8"),
            (TAG_NATURAL_LANG, "natural-language-configured", "en"),
            (TAG_NATURAL_LANG, "generated-natural-language-supported", "en"),
            (TAG_MIME_TYPE,    "document-format-default",    "application/octet-stream"),
            (TAG_MIME_TYPE,    "document-format-supported",  "application/octet-stream"),
            (TAG_BOOLEAN, "printer-is-accepting-jobs",     True),
            (TAG_INTEGER, "queued-job-count",              0),
            (TAG_KEYWORD, "pdl-override-supported",        "not-attempted"),
            (TAG_INTEGER, "multiple-document-jobs-supported", 0),
            (TAG_INTEGER, "ipp-versions-supported",        0x00010001),  # 1.1
        ],
    )


def build_unsupported_op_response(request_id: int) -> bytes:
    return build_response(request_id=request_id,
                          status_code=STATUS_CLIENT_ERROR_UNSUP_OP)


def build_get_job_attributes_response(request_id: int, job_id: int,
                                        printer_uri: str,
                                        job_state: int = JOB_STATE_COMPLETED) -> bytes:
    """Get-Job-Attributes (0x0009) → Dummy „Job ist fertig"-Antwort.

    Printix fragt nach dem Submit den Job-Status ab. Wir tracken die Jobs
    nicht per IPP-Job-ID (das macht Printix selbst), antworten also einfach
    immer mit `job-state=completed (9)` damit Printix den Job als erledigt
    aus seiner Outbound-Queue rausnimmt.
    """
    job_uri = f"{printer_uri}/jobs/{job_id}"
    return build_response(
        request_id=request_id,
        status_code=STATUS_SUCCESSFUL_OK,
        job_attrs=[
            (TAG_URI,     "job-uri",           job_uri),
            (TAG_INTEGER, "job-id",            job_id),
            (TAG_ENUM,    "job-state",         job_state),
            (TAG_KEYWORD, "job-state-reasons", "job-completed-successfully"),
        ],
    )


# ─── Convenience: Metadata-Extraktion ────────────────────────────────────────

def extract_job_metadata(req: IppRequest) -> dict:
    """Extrahiert die für Delegate-Print relevanten Felder.

    Das ist der Killer-Vorteil gegenüber LPR: die User-Identität kommt
    direkt als strukturiertes IPP-Attribut, wir müssen sie nicht aus
    Printix' API zurücklesen.
    """
    def _get(name: str, default: str = "") -> str:
        v = req.attr(name, default)
        return str(v) if v is not None else default

    return {
        "requesting_user_name":        _get("requesting-user-name"),
        "job_originating_user_name":   _get("job-originating-user-name"),
        "job_originating_host_name":   _get("job-originating-host-name"),
        "job_name":                    _get("job-name"),
        "document_format":             _get("document-format"),
        "document_name":               _get("document-name"),
        "printer_uri":                 _get("printer-uri"),
        "copies":                      int(req.attr("copies", 1) or 1),
        "requested_attributes":        [v for v in (req.operation_attrs.get("requested-attributes").values
                                                      if "requested-attributes" in req.operation_attrs else [])],
    }
