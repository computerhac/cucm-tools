"""
AXL SOAP client for CUCM 15.
Uses executeSQLQuery for reads and typed AXL operations for writes.

Confirmed tkpatternusage values (verified against live CUCM 15):
  2  - Directory Number
  3  - Translation Pattern
  4  - Call Park
  5  - Route Pattern
  6  - Meet Me
  7  - Call Pickup
  8  - Group Call Pickup
  9  - Other Group Pickup
  10 - Hunt Pilot
  11 - Line Template
  12 - Transformation Pattern
  15 - Calling Party Transformation Pattern
  20 - Called Party Transformation Pattern
  vmpilot (voicemessagingpilot table) - Voice Mail Pilot
"""

import ipaddress
import re
import xml.etree.ElementTree as ET
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

PATTERN_USAGE = {
    "2":        "Directory Number",
    "3":        "Translation Pattern",
    "4":        "Call Park",
    "5":        "Route Pattern",
    "6":        "Meet Me",
    "7":        "Call Pickup",
    "8":        "Group Call Pickup",
    "9":        "Other Group Pickup",
    "10":       "Hunt Pilot",
    "11":       "Line Template",
    "12":       "Transformation Pattern",
    "15":       "Calling Party Transformation Pattern",
    "20":       "Called Party Transformation Pattern",
    "vmpilot":  "Voice Mail Pilot",
}

# Device type prefixes and their labels
DEVICE_PREFIXES = {
    "SEP": "Physical Phone",
    "CSF": "Jabber for Windows/Mac",
    "TCT": "Jabber for iPhone",
    "BOT": "Jabber for Android",
    "TAB": "Jabber for iPad",
}

# tkdeviceprotocol → protocol name for addPhone
PROTOCOL_MAP = {
    "0": "SCCP",
    "1": "SIP",
}


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _envelope(operation: str, body_content: str, version: str = "15.0") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope
    xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:ns="http://www.cisco.com/AXL/API/{version}">
  <soapenv:Header/>
  <soapenv:Body>
    {body_content}
  </soapenv:Body>
</soapenv:Envelope>"""


def _post(host: str, port: int, username: str, password: str,
          verify_ssl: bool, operation: str, body: str) -> requests.Response:
    url = f"https://{host}:{port}/axl/"
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction":   f'"CUCM:DB ver=15.0 {operation}"',
    }
    resp = requests.post(url, data=body.encode("utf-8"), headers=headers,
                         auth=(username, password), verify=verify_ssl, timeout=30)
    if resp.status_code == 401:
        raise PermissionError("Authentication failed — check username/password.")
    return resp


def _parse_rows(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    rows = root.findall(".//row")
    results = []
    for row in rows:
        usage_code = (row.findtext("usage_type") or "").strip()
        results.append({
            "pattern":     (row.findtext("pattern") or "").strip(),
            "description": (row.findtext("description") or "").strip(),
            "partition":   (row.findtext("partition") or "<none>").strip(),
            "type":        PATTERN_USAGE.get(usage_code, f"Unknown ({usage_code})"),
        })
    return results


def _parse_raw_rows(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    return [{child.tag: (child.text or "").strip() for child in row}
            for row in root.findall(".//row")]


def _sql_body(sql: str) -> str:
    return f"<ns:executeSQLQuery><sql>{sql}</sql></ns:executeSQLQuery>"


# ---------------------------------------------------------------------------
# Route Plan Search
# ---------------------------------------------------------------------------

def _build_search_sql(mode: str, query: str) -> str:
    safe = query.replace("'", "''")
    if mode == "description":
        numplan_where = f"LOWER(np.description) LIKE LOWER('%{safe}%')"
        vm_where      = f"LOWER(vmp.description) LIKE LOWER('%{safe}%')"
    else:
        numplan_where = f"np.dnorpattern LIKE '%{safe}%'"
        vm_where      = f"vmp.directorynumber LIKE '%{safe}%'"

    return f"""
        SELECT
            np.dnorpattern                         AS pattern,
            np.description                         AS description,
            CAST(np.tkpatternusage AS VARCHAR(20)) AS usage_type,
            rp.name                                AS partition
        FROM numplan np
        LEFT JOIN routepartition rp ON np.fkroutepartition = rp.pkid
        WHERE {numplan_where}

        UNION

        SELECT
            vmp.directorynumber  AS pattern,
            vmp.description      AS description,
            'vmpilot'            AS usage_type,
            css.name             AS partition
        FROM voicemessagingpilot vmp
        LEFT JOIN callingsearchspace css ON css.pkid = vmp.fkcallingsearchspace
        WHERE {vm_where}

        ORDER BY pattern
    """


def search(host: str, port: int, username: str, password: str,
           verify_ssl: bool, mode: str, query: str) -> list[dict]:
    sql = _build_search_sql(mode, query)
    body = _envelope("executeSQLQuery", _sql_body(sql))
    resp = _post(host, port, username, password, verify_ssl, "executeSQLQuery", body)
    if resp.status_code != 200:
        raise ConnectionError(f"AXL returned HTTP {resp.status_code}: {resp.text[:300]}")
    return _parse_rows(resp.text)


# ---------------------------------------------------------------------------
# Device Switcher — reads
# ---------------------------------------------------------------------------

def normalize_device_name(raw: str) -> str:
    """
    Normalize user input to a CUCM device name.
    - 12 hex chars (with or without : - . separators) → SEP + uppercase MAC
    - Already prefixed with SEP/CSF/BOT/TAB/TCT → uppercase as-is
    - Anything else → uppercase as-is
    """
    stripped = raw.strip().upper().replace(":", "").replace("-", "").replace(".", "")
    if len(stripped) == 12 and all(c in "0123456789ABCDEF" for c in stripped):
        return f"SEP{stripped}"
    return raw.strip().upper()


def _strip_ns(tag: str) -> str:
    """Strip XML namespace from a tag string."""
    return tag.split("}")[-1] if "}" in tag else tag


def _fetch_phone(host: str, port: int, username: str, password: str,
                 verify_ssl: bool, device_name: str):
    """
    Call AXL getPhone and return the <phone> XML element, or None if not found.
    Using the typed getPhone operation avoids SQL column-name guessing entirely —
    AXL returns every setting in the exact string format addPhone expects.
    """
    body = _envelope("getPhone", f"<ns:getPhone><name>{device_name}</name></ns:getPhone>")
    resp = _post(host, port, username, password, verify_ssl, "getPhone", body)
    if resp.status_code != 200:
        return None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None
    # <phone> is nested inside <return> inside the response envelope
    for tag in (
        "{http://www.cisco.com/AXL/API/15.0}phone",
        "phone",
    ):
        el = root.find(f".//{tag}")
        if el is not None:
            return el
    return None


def _serialize_vendor_config(vc_el) -> str:
    """Serialize <vendorConfig> inner children to a clean XML string without namespaces."""
    inner = "".join(
        f"<{_strip_ns(child.tag)}>{(child.text or '').strip()}</{_strip_ns(child.tag)}>"
        for child in vc_el
        if (child.text or "").strip()
    )
    return f"<vendorConfig>{inner}</vendorConfig>" if inner else ""


def get_device(host: str, port: int, username: str, password: str,
               verify_ssl: bool, device_name: str) -> dict | None:
    phone_el = _fetch_phone(host, port, username, password, verify_ssl, device_name)
    if phone_el is None:
        return None

    d: dict = {}
    for child in phone_el:
        tag = _strip_ns(child.tag)
        if tag in ("lines", "speeddials", "busyLampFields", "addOnModules",
                   "services", "subscribeCallingSearchSpaceName"):
            continue
        if tag == "vendorConfig":
            # Store as raw XML so _build_phone_xml can include it without escaping
            d["vendorConfig"] = _serialize_vendor_config(child)
        else:
            d[tag] = (child.text or "").strip()

    # Lowercase aliases so the rest of the code (UI, _OPTIONAL_SETTINGS) works
    # with both the camelCase AXL names and the legacy lowercase SQL names.
    d["model"]           = d.get("model") or d.get("product", "")
    d["devicepool"]      = d.get("devicePoolName", "")
    d["css"]             = d.get("callingSearchSpaceName", "")
    d["softkeytemplate"] = d.get("softkeyTemplateName", "")
    d["phonetemplate"]   = d.get("phoneTemplateName", "")
    d["location"]        = d.get("locationName", "")
    d["owneruserid"]     = d.get("ownerUserName", "")

    if not d.get("protocol"):
        d["protocol"] = "SIP"

    name = d.get("name", "")
    d["device_type"] = DEVICE_PREFIXES.get(name[:3], "Unknown")
    return d


def get_device_lines(host: str, port: int, username: str, password: str,
                     verify_ssl: bool, device_name: str) -> list[dict]:
    phone_el = _fetch_phone(host, port, username, password, verify_ssl, device_name)
    if phone_el is None:
        return []

    # Find <lines> child (may have namespace)
    lines_el = None
    for child in phone_el:
        if _strip_ns(child.tag) == "lines":
            lines_el = child
            break
    if lines_el is None:
        return []

    lines = []
    for line_el in lines_el:
        if _strip_ns(line_el.tag) != "line":
            continue
        line: dict = {}
        for child in line_el:
            tag = _strip_ns(child.tag)
            if tag == "dirn":
                for dirn_child in child:
                    dt = _strip_ns(dirn_child.tag)
                    if dt == "pattern":
                        line["pattern"] = (dirn_child.text or "").strip()
                    elif dt == "routePartitionName":
                        line["partition"] = (dirn_child.text or "").strip()
            else:
                line[tag] = (child.text or "").strip()
        # Normalize keys to match what _build_phone_xml expects
        line["line_index"]   = line.pop("index", "")
        line["displayascii"] = line.pop("displayAscii", "") or line.get("displayascii", "")
        line["e164mask"]     = line.pop("e164Mask", "") or line.get("e164mask", "")
        if "pattern" in line:
            lines.append(line)

    return sorted(lines, key=lambda ln: int(ln.get("line_index") or 0))


def get_phone_models(host: str, port: int, username: str, password: str,
                     verify_ssl: bool) -> list[dict]:
    """Return all Cisco phone/endpoint models available on this cluster."""
    sql = """
        SELECT DISTINCT tm.enum, tm.name
        FROM typemodel tm
        WHERE tm.name LIKE 'Cisco %'
        ORDER BY tm.name
    """
    body = _envelope("executeSQLQuery", _sql_body(sql))
    resp = _post(host, port, username, password, verify_ssl, "executeSQLQuery", body)
    if resp.status_code != 200:
        raise ConnectionError(f"AXL returned HTTP {resp.status_code}: {resp.text[:300]}")
    return _parse_raw_rows(resp.text)


def get_button_templates(host: str, port: int, username: str, password: str,
                         verify_ssl: bool, model_name: str) -> list[str]:
    """Return button template names valid for the given model."""
    safe = model_name.replace("'", "''")
    sql = f"""
        SELECT pt.name
        FROM phonetemplate pt
        WHERE pt.tkmodel = (SELECT enum FROM typemodel WHERE name = '{safe}')
        ORDER BY pt.name
    """
    body = _envelope("executeSQLQuery", _sql_body(sql))
    resp = _post(host, port, username, password, verify_ssl, "executeSQLQuery", body)
    if resp.status_code != 200:
        raise ConnectionError(f"AXL returned HTTP {resp.status_code}: {resp.text[:300]}")
    rows = _parse_raw_rows(resp.text)
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# Device Switcher — writes
# ---------------------------------------------------------------------------

def _xml_opt(tag: str, value: str) -> str:
    """Return an XML element only if value is non-empty."""
    if value and value.strip():
        escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<{tag}>{escaped}</{tag}>"
    return ""


# Tags that are always set explicitly in addPhone or are read-only.
# Everything else from device_info is attempted dynamically.
_PHONE_SKIP_TAGS = {
    # Set at the top of the addPhone body explicitly
    "name", "description", "product", "model", "class", "protocol", "protocolSide",
    # Phone template is passed via the user-selected value, not copied from old device
    "phoneTemplateName",
    # Container elements — lines handled separately, rest not transferred
    "lines", "speeddials", "busyLampFields", "services", "addOnModules",
    "subscribeCallingSearchSpaceName",
    # Model+protocol specific — always auto-assigned by CUCM for the target model
    "phoneSecurityProfileName", "securityProfileName",
    # Read-only / not settable via addPhone
    "uuid", "loadInformation", "versionStamp", "tkModel", "tkDeviceProtocol",
    "certificateOperation", "certificateStatus", "isProtected",
    "numberOfButtons", "remoteDevice", "isActive",
    # Lowercase display aliases added by get_device — camelCase AXL versions are used
    "device_type", "devicepool", "css", "softkeytemplate",
    "phonetemplate", "location", "owneruserid",
}

# Human-readable labels for AXL tag names (used in switch result reporting).
# Any tag without an entry here falls back to showing the raw tag name.
SETTING_LABELS = {
    "devicePoolName":                    "Device Pool",
    "callingSearchSpaceName":            "Calling Search Space",
    "softkeyTemplateName":               "Softkey Template",
    "phoneTemplateName":                 "Button Template",
    "locationName":                      "Location",
    "ownerUserName":                     "Owner User ID",
    "commonPhoneConfigName":             "Common Phone Config",
    "networkLocale":                     "Network Locale",
    "userLocale":                        "User Locale",
    "mediaResourceListName":             "Media Resource Group List",
    "automatedAlternateRoutingCssName":  "AAR Calling Search Space",
    "aarNeighborhoodName":               "AAR Group",
    "webAccessEnabled":                  "Web Access",
    "traceFlag":                         "Trace Enabled",
    "allowHotelingFlag":                 "Allow Hoteling",
    "privacyInfoStatus":                 "Privacy",
    "singleButtonBarge":                 "Single Button Barge",
    "joinAcrossLines":                   "Join Across Lines",
    "builtInBridgeStatus":               "Built-in Bridge",
    "allowCtiControlFlag":               "Allow CTI Control",
    "retryVideoCallAsAudio":             "Retry Video Call as Audio",
    "requireDtmfReception":              "Require DTMF Reception",
    "rfc2543Hold":                       "RFC 2543 Hold",
    "packetCaptureMode":                 "Packet Capture Mode",
    "phoneSecurityProfileName":          "Security Profile",
    "vendorConfig":                      "Device-Specific Settings (PC Port, Enhanced Line Mode, etc.)",
}


def _build_phone_xml(name: str, model: str, protocol: str, phone_template: str,
                     device_info: dict, lines: list[dict],
                     excluded_tags: set[str]) -> str:
    """
    Build the <phone> XML body for addPhone.

    Dynamically includes every field from device_info that isn't in
    _PHONE_SKIP_TAGS or excluded_tags — including vendorConfig which carries
    model-specific settings like PC Port and Enhanced Line Mode.
    """
    lines_xml = ""
    for line in lines:
        lines_xml += f"""
            <line>
                <index>{line["line_index"]}</index>
                {_xml_opt("label", line.get("label", ""))}
                {_xml_opt("display", line.get("display", ""))}
                {_xml_opt("displayAscii", line.get("displayascii", ""))}
                {_xml_opt("e164Mask", line.get("e164mask", ""))}
                <dirn>
                    <pattern>{line["pattern"]}</pattern>
                    {_xml_opt("routePartitionName", line.get("partition", ""))}
                </dirn>
            </line>"""

    optional_xml = ""
    for tag, value in device_info.items():
        if tag in _PHONE_SKIP_TAGS or tag in excluded_tags or not value:
            continue
        if tag == "vendorConfig":
            optional_xml += value          # already serialised XML — include raw
        else:
            optional_xml += _xml_opt(tag, value)

    # Phone template uses the user-selected value (must match target model)
    if "phoneTemplateName" not in excluded_tags:
        optional_xml += _xml_opt("phoneTemplateName", phone_template)

    return f"""
        <name>{name}</name>
        {_xml_opt("description", device_info.get("description", ""))}
        <product>{model}</product>
        <model>{model}</model>
        <class>Phone</class>
        <protocol>{protocol}</protocol>
        <protocolSide>User</protocolSide>
        {optional_xml}
        <lines>{lines_xml}</lines>
    """


def _extract_unsupported_field(error_text: str) -> str | None:
    """
    Parse a CUCM AXL error message and return the field name to drop.

    Handles two cases:
    1. Generic "field X not supported" messages — extracted via regex.
    2. Known model-specific value mismatches — CUCM reports the problem
       without naming the field, so we map the error message to the field.
    """
    # Generic patterns where CUCM names the field
    patterns = [
        r"not supported[^'\"]*['\"](\w+)['\"]",
        r"['\"](\w+)['\"][^'\"]*not supported",
        r"not applicable[^:]*:\s*(\w+)",
        r"Element[^'\"]*['\"](\w+)['\"].*(?:not supported|invalid|applicable)",
        r"(?:field|element|tag)\s+['\"]?(\w+)['\"]?\s+(?:is not|cannot)",
    ]
    for pattern in patterns:
        m = re.search(pattern, error_text, re.IGNORECASE)
        if m:
            return m.group(1)

    # Known value-mismatch errors: CUCM doesn't name the field, but the message
    # uniquely identifies which setting needs to be dropped.
    known = [
        (r"security profile.*not valid|not valid.*security profile",
         "securityProfileName"),
        (r"softkey template.*not valid|not valid.*softkey",
         "softkeyTemplateName"),
        (r"phone button template.*not valid|not valid.*phone button template",
         "phoneTemplateName"),
        (r"common phone config.*not valid|not valid.*common phone config",
         "commonPhoneConfigName"),
        # Jabber only accepts "Ringer Off" for DND; physical phone value is rejected
        (r"dnd option",
         "dndOption"),
        # Jabber does not support MLPP features
        (r"mlpp preemption.*disabled|preemption.*not support",
         "preemption"),
        (r"mlpp indication.*disabled|mlpp indication.*not support",
         "mlppIndicationStatus"),
    ]
    for pattern, field in known:
        if re.search(pattern, error_text, re.IGNORECASE):
            return field

    return None


def add_phone_smart(host: str, port: int, username: str, password: str, verify_ssl: bool,
                    name: str, model: str, protocol: str, phone_template: str,
                    device_info: dict, lines: list[dict]) -> dict:
    """
    Create a new phone via AXL addPhone with automatic field compatibility detection.

    If CUCM rejects a field as unsupported for the target model, that field is
    removed and the request is retried. Continues until success or a non-field
    error occurs.

    Returns a dict with:
      - transferred: list of AXL tag names that were successfully applied
      - skipped:     list of AXL tag names CUCM rejected as unsupported for this model
    """
    excluded: set[str] = set()
    # Candidate tags = every non-skipped field in device_info, plus phoneTemplateName
    all_tags = {tag for tag, value in device_info.items()
                if tag not in _PHONE_SKIP_TAGS and value} | {"phoneTemplateName"}
    max_attempts = len(all_tags) + 2

    for _ in range(max_attempts):
        phone_xml = _build_phone_xml(name, model, protocol, phone_template,
                                     device_info, lines, excluded)
        body = _envelope("addPhone", f"<ns:addPhone><phone>{phone_xml}</phone></ns:addPhone>")
        resp = _post(host, port, username, password, verify_ssl, "addPhone", body)

        if resp.status_code == 200:
            return {
                "transferred": sorted(all_tags - excluded),
                "skipped":     sorted(excluded),
            }

        error_text = resp.text
        field = _extract_unsupported_field(error_text)
        if field and field not in excluded:
            excluded.add(field)
            all_tags.add(field)  # track for reporting even if not in original device_info
            continue

        # Not a field-compatibility error — raise with full detail
        raise ConnectionError(f"addPhone failed (HTTP {resp.status_code}): {error_text[:600]}")

    raise ConnectionError("addPhone failed after exhausting all field combinations.")


def remove_phone(host: str, port: int, username: str, password: str,
                 verify_ssl: bool, device_name: str):
    """Remove a phone by device name."""
    body = _envelope("removePhone", f"<ns:removePhone><name>{device_name}</name></ns:removePhone>")
    resp = _post(host, port, username, password, verify_ssl, "removePhone", body)
    if resp.status_code != 200:
        raise ConnectionError(f"removePhone failed (HTTP {resp.status_code}): {resp.text[:500]}")


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Subnet Search
# ---------------------------------------------------------------------------

def get_device_ip(host: str, port: int, username: str, password: str,
                  verify_ssl: bool, device_name: str) -> str | None:
    """Return the last known IP address for a device, or None if not found."""
    safe = device_name.replace("'", "''")
    sql = f"""
        SELECT rd.lastknownipaddress
        FROM registrationdynamic rd
        JOIN device d ON rd.fkdevice = d.pkid
        WHERE d.name = '{safe}'
    """
    rows = raw_query(host, port, username, password, verify_ssl, sql)
    if rows:
        return rows[0].get("lastknownipaddress", "") or None
    return None


def search_phones_by_subnet(host: str, port: int, username: str, password: str,
                             verify_ssl: bool, seed_ip: str,
                             prefix_len: int) -> list[dict]:
    """
    Return all phones whose last known IP falls within the subnet
    defined by seed_ip/prefix_len (prefix_len 22-29).

    Sorted by datetimestamp DESC — most recently registered first.
    """
    network = ipaddress.ip_network(f"{seed_ip}/{prefix_len}", strict=False)
    net_str = str(network.network_address)
    net_parts = net_str.split(".")

    # Pre-filter in SQL using LIKE; Python does exact membership check
    if prefix_len <= 23:
        like_prefix = f"{net_parts[0]}.{net_parts[1]}."
    else:
        like_prefix = f"{net_parts[0]}.{net_parts[1]}.{net_parts[2]}."

    safe_prefix = like_prefix.replace("'", "''")
    sql = f"""
        SELECT
            d.name           AS device_name,
            d.description    AS description,
            rd.lastknownipaddress AS ip_address,
            rd.datetimestamp AS last_seen,
            rd.lastknownucm  AS last_ucm
        FROM registrationdynamic rd
        JOIN device d ON rd.fkdevice = d.pkid
        WHERE rd.lastknownipaddress LIKE '{safe_prefix}%'
        ORDER BY rd.datetimestamp DESC
    """
    rows = raw_query(host, port, username, password, verify_ssl, sql)

    results = []
    for row in rows:
        ip_str = row.get("ip_address", "")
        try:
            if ipaddress.ip_address(ip_str) in network:
                results.append(row)
        except ValueError:
            pass
    return results


def raw_query(host: str, port: int, username: str, password: str,
              verify_ssl: bool, sql: str) -> list[dict]:
    body = _envelope("executeSQLQuery", _sql_body(sql))
    resp = _post(host, port, username, password, verify_ssl, "executeSQLQuery", body)
    if not resp.ok:
        raise ConnectionError(f"AXL returned HTTP {resp.status_code}")
    return _parse_raw_rows(resp.text)


def test_connection(host: str, port: int, username: str, password: str,
                    verify_ssl: bool) -> str:
    try:
        search(host, port, username, password, verify_ssl,
               mode="number", query="TESTCONNECTION_NORESULTS_XYZ123")
        return "OK"
    except PermissionError as e:
        return str(e)
    except Exception as e:
        return f"Connection failed: {e}"
