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


def get_phone_security_profiles(host: str, port: int, username: str, password: str,
                                 verify_ssl: bool, model_name: str | None = None) -> list[str]:
    """
    Return phone security profile names. When model_name is set, filter to
    profiles defined for that typemodel; otherwise return all profiles.
    Per a real CUCM 15 device-row dump the FK column is fksecurityprofile,
    so by Cisco's convention the table is securityprofile (not
    phonesecurityprofile, which doesn't exist on this schema).
    """
    if model_name:
        safe = model_name.replace("'", "''")
        sql = f"""
            SELECT sp.name FROM securityprofile sp
            WHERE sp.tkdeviceprofile = (SELECT enum FROM typemodel WHERE name = '{safe}')
            ORDER BY sp.name
        """
    else:
        sql = "SELECT name FROM securityprofile ORDER BY name"
    rows = raw_query(host, port, username, password, verify_ssl, sql)
    return [r["name"] for r in rows if r.get("name")]


def get_sip_profiles(host: str, port: int, username: str, password: str,
                     verify_ssl: bool) -> list[str]:
    """Return all SIP profile names defined on the cluster."""
    rows = raw_query(host, port, username, password, verify_ssl,
                     "SELECT name FROM sipprofile ORDER BY name")
    return [r["name"] for r in rows if r.get("name")]


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
    # Legacy SCCP security profile name — auto-assigned by CUCM. The newer
    # securityProfileName (without "phone" prefix) IS required for SIP Station
    # endpoints so it stays emittable.
    "phoneSecurityProfileName",
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
                     excluded_tags: set[str], product: str | None = None,
                     phone_class: str = "Phone") -> str:
    """
    Build the <phone> XML body for addPhone.

    `product` defaults to `model` for regular IP phones where the typeproduct
    and typemodel strings match. Pass an explicit value for device classes
    where they differ — e.g. SIP analog endpoints
    (product="Cisco SIP FXS Port", model="SIP Station").

    Dynamically includes every field from device_info that isn't in
    _PHONE_SKIP_TAGS or excluded_tags — including vendorConfig which carries
    model-specific settings like PC Port and Enhanced Line Mode.
    """
    # Additional line fields safe to forward to addPhone on SIP analog
    # endpoints. Smart-retry only filters device-level field rejections,
    # so the line list is curated rather than emit-everything — if CUCM
    # rejects one of these for a model, it has to be removed here. Nested
    # elements like callForwardAll aren't in this set because
    # get_device_lines parses them as empty (an existing limitation worth
    # addressing later with a richer line parser).
    _LINE_EXTRA_TAGS = (
        "alertingName", "alertingNameAscii",
        "asciiAlertingName",
        "maxNumCalls", "busyTrigger",
        "voiceMailProfileName",
        "callerName", "callerNumber",
        "audibleMessageWaitingIndicator",
        "visualMessageWaitingIndicatorPolicy",
        "ringSettingIdleAndActive", "ringSettingActiveAndBusy",
        "ringSettingIdlePickupAlert", "ringSettingActivePickupAlert",
        "aarDestinationMask", "aarKeepCallHistory", "aarVoiceMailEnabled",
        "recordingFlag", "recordingMediaSource", "recordingProfileName",
        "monitoringCssName",
        "partyEntranceTone",
        "patternUrgency",
        "associatedDevices",
        "consumerStartIndex",
    )

    lines_xml = ""
    for line in lines:
        extra_xml = "".join(
            _xml_opt(tag, line.get(tag, ""))
            for tag in _LINE_EXTRA_TAGS
            if line.get(tag)
        )
        lines_xml += f"""
            <line>
                <index>{line["line_index"]}</index>
                {_xml_opt("label", line.get("label", ""))}
                {_xml_opt("display", line.get("display", ""))}
                {_xml_opt("displayAscii", line.get("displayascii", ""))}
                {_xml_opt("e164Mask", line.get("e164mask", ""))}
                {extra_xml}
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
        <product>{product or model}</product>
        <model>{model}</model>
        <class>{phone_class}</class>
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
                    device_info: dict, lines: list[dict],
                    product: str | None = None,
                    phone_class: str = "Phone") -> dict:
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

    last_body = ""
    for _ in range(max_attempts):
        phone_xml = _build_phone_xml(name, model, protocol, phone_template,
                                     device_info, lines, excluded,
                                     product=product, phone_class=phone_class)
        body = _envelope("addPhone", f"<ns:addPhone><phone>{phone_xml}</phone></ns:addPhone>")
        last_body = body
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

        # Not a field-compatibility error — surface the actual XML we sent so
        # the caller can diagnose what CUCM is rejecting.
        import sys
        print(f"[axl.add_phone_smart] addPhone REJECTED. Request body was:\n{last_body}\n"
              f"Response: {error_text[:800]}", file=sys.stderr, flush=True)
        raise ConnectionError(f"addPhone failed (HTTP {resp.status_code}): {error_text[:600]}")

    raise ConnectionError("addPhone failed after exhausting all field combinations.")


def update_phone_lines(host: str, port: int, username: str, password: str,
                        verify_ssl: bool, device_name: str,
                        lines: list[dict]) -> None:
    """
    Replace the <lines> block on a phone via updatePhone. Pass an empty list
    to release all lines (frees up any owned DNs) while leaving the device
    row in place. Used for MGCP analog source deprovisioning since CUCM 15
    can't recreate MGCP endpoints once deleted — clearing the line instead
    of removing the device keeps rollback possible via a second updatePhone.
    """
    lines_xml = ""
    for line in lines:
        lines_xml += (
            f"<line>"
            f"<index>{line.get('line_index', 1)}</index>"
            f"{_xml_opt('label', line.get('label', ''))}"
            f"{_xml_opt('display', line.get('display', ''))}"
            f"{_xml_opt('displayAscii', line.get('displayascii') or line.get('displayAscii', ''))}"
            f"{_xml_opt('e164Mask', line.get('e164mask') or line.get('e164Mask', ''))}"
            f"<dirn>"
            f"<pattern>{_xml_esc(line.get('pattern', ''))}</pattern>"
            f"{_xml_opt('routePartitionName', line.get('partition', ''))}"
            f"</dirn>"
            f"</line>"
        )
    body = _envelope(
        "updatePhone",
        f"<ns:updatePhone>"
        f"<name>{_xml_esc(device_name)}</name>"
        f"<lines>{lines_xml}</lines>"
        f"</ns:updatePhone>",
    )
    resp = _post(host, port, username, password, verify_ssl, "updatePhone", body)
    if resp.status_code != 200:
        msg = ""
        m = re.search(r"<faultstring>([^<]+)</faultstring>", resp.text)
        if m:
            msg = m.group(1).strip()
        raise ConnectionError(
            f"updatePhone failed (HTTP {resp.status_code})"
            + (f": {msg}" if msg else f": {resp.text[:300]}")
        )


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


def execute_sql_update(host: str, port: int, username: str, password: str,
                       verify_ssl: bool, sql: str) -> str:
    """
    Execute an AXL executeSQLUpdate operation (INSERT/UPDATE/DELETE).
    Returns the raw response text (rowsUpdated count is parseable from it).
    Raises ConnectionError with the CUCM faultstring on failure.
    """
    body = _envelope("executeSQLUpdate",
                     f"<ns:executeSQLUpdate><sql>{_xml_esc(sql)}</sql></ns:executeSQLUpdate>")
    resp = _post(host, port, username, password, verify_ssl, "executeSQLUpdate", body)
    if not resp.ok:
        msg = ""
        m = re.search(r"<faultstring>([^<]+)</faultstring>", resp.text)
        if m:
            msg = m.group(1).strip()
        raise ConnectionError(
            f"executeSQLUpdate returned HTTP {resp.status_code}"
            + (f": {msg}" if msg else f": {resp.text[:300]}")
        )
    return resp.text


def get_device_security_profile(host: str, port: int, username: str,
                                 password: str, verify_ssl: bool,
                                 device_name: str) -> str | None:
    """
    Look up the phone security profile name CUCM has assigned to a device,
    joining device.fksecurityprofile → securityprofile.pkid. Returns the
    profile name on success, None if the device has no profile (e.g. MGCP
    analog endpoints, which CUCM creates without one via the addGateway
    path) or if the lookup fails.
    """
    safe = device_name.replace("'", "''")
    sql = f"""
        SELECT sp.name AS name
        FROM device d
        LEFT JOIN securityprofile sp ON sp.pkid = d.fksecurityprofile
        WHERE d.name = '{safe}'
    """
    try:
        rows = raw_query(host, port, username, password, verify_ssl, sql)
    except Exception:
        return None
    return rows[0].get("name") if rows and rows[0].get("name") else None


def clear_device_security_profile(host: str, port: int, username: str,
                                   password: str, verify_ssl: bool,
                                   device_name: str) -> None:
    """
    Null out the fksecurityprofile FK on a device row. Used to restore the
    "no security profile" state CUCM allows for MGCP analog endpoints but
    that addPhone won't let us produce directly (addPhone refuses a missing
    profile, so we addPhone with a placeholder then clear it via SQL).
    """
    safe = device_name.replace("'", "''")
    execute_sql_update(
        host, port, username, password, verify_ssl,
        f"UPDATE device SET fksecurityprofile = NULL WHERE name = '{safe}'",
    )


def get_device_pkid(host: str, port: int, username: str, password: str,
                    verify_ssl: bool, device_name: str) -> str | None:
    """Look up the pkid of a device by name."""
    safe = device_name.replace("'", "''")
    rows = raw_query(host, port, username, password, verify_ssl,
                     f"SELECT pkid FROM device WHERE name = '{safe}'")
    return rows[0]["pkid"] if rows else None


def get_gateway_pkid(host: str, port: int, username: str, password: str,
                     verify_ssl: bool, gateway_name: str) -> str | None:
    """Look up the pkid of a gateway entry in the mgcp table by its domain name."""
    safe = gateway_name.replace("'", "''")
    rows = raw_query(host, port, username, password, verify_ssl,
                     f"SELECT pkid FROM mgcp WHERE domainname = '{safe}'")
    return rows[0]["pkid"] if rows else None


def get_device_gateway_membership(host: str, port: int, username: str,
                                   password: str, verify_ssl: bool,
                                   device_name: str) -> dict | None:
    """
    Return the mgcpdevicemember row for the device joined to its parent
    gateway's domain name. None if the device isn't bound to any gateway.
    """
    safe = device_name.replace("'", "''")
    sql = f"""
        SELECT m.domainname AS gateway_name, mdm.slot, mdm.subunit, mdm.port
        FROM mgcpdevicemember mdm
        JOIN mgcp m ON m.pkid = mdm.fkmgcp
        JOIN device d ON d.pkid = mdm.fkdevice
        WHERE d.name = '{safe}'
    """
    rows = raw_query(host, port, username, password, verify_ssl, sql)
    return rows[0] if rows else None


def bind_phone_to_gateway(host: str, port: int, username: str, password: str,
                          verify_ssl: bool, device_name: str, gateway_name: str,
                          slot: int, subunit: int, port_num: int) -> None:
    """
    Insert a row into mgcpdevicemember linking an already-created AN-style
    analog phone device to its parent gateway. addPhone alone leaves the
    device unlinked; without this row CUCM treats the device as a phantom.
    """
    device_pkid = get_device_pkid(host, port, username, password, verify_ssl, device_name)
    if not device_pkid:
        raise ConnectionError(f"Cannot bind: device '{device_name}' not found")
    gateway_pkid = get_gateway_pkid(host, port, username, password, verify_ssl, gateway_name)
    if not gateway_pkid:
        raise ConnectionError(f"Cannot bind: gateway '{gateway_name}' not in mgcp table")
    sql = (
        f"INSERT INTO mgcpdevicemember (fkmgcp, fkdevice, slot, subunit, port) "
        f"VALUES ('{gateway_pkid}', '{device_pkid}', {int(slot)}, {int(subunit)}, {int(port_num)})"
    )
    execute_sql_update(host, port, username, password, verify_ssl, sql)


def raw_query(host: str, port: int, username: str, password: str,
              verify_ssl: bool, sql: str) -> list[dict]:
    body = _envelope("executeSQLQuery", _sql_body(sql))
    resp = _post(host, port, username, password, verify_ssl, "executeSQLQuery", body)
    if not resp.ok:
        # Try to extract the CUCM faultstring for a useful message
        msg = ""
        m = re.search(r"<faultstring>([^<]+)</faultstring>", resp.text)
        if m:
            msg = m.group(1).strip()
        raise ConnectionError(
            f"AXL returned HTTP {resp.status_code}"
            + (f": {msg}" if msg else f": {resp.text[:300]}")
        )
    return _parse_raw_rows(resp.text)


# ---------------------------------------------------------------------------
# Speed Dial / BLF Updater — helpers
# ---------------------------------------------------------------------------

def _xml_esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _strip_cucm_escape(s: str) -> str:
    """CUCM stores E.164 numbers as \\+12345 internally. Strip the leading backslash
    so speed dial numbers and BLF destinations write back as +12345, which AXL accepts."""
    return s.lstrip("\\") if s.startswith("\\") else s


def _parse_sds_from_phone(phone_el) -> list[dict]:
    sd_el = None
    for child in phone_el:
        if _strip_ns(child.tag) == "speeddials":
            sd_el = child
            break
    if sd_el is None:
        return []
    sds = []
    for sd in sd_el:
        if _strip_ns(sd.tag) != "speeddial":
            continue
        entry = {_strip_ns(c.tag): (c.text or "").strip() for c in sd}
        if "index" in entry:
            if "dirn" in entry:
                entry["dirn"] = _strip_cucm_escape(entry["dirn"])
            sds.append(entry)
    return sorted(sds, key=lambda x: int(x.get("index") or 0))


def _parse_blfs_from_phone(phone_el) -> list[dict]:
    blf_el = None
    for child in phone_el:
        if _strip_ns(child.tag) == "busyLampFields":
            blf_el = child
            break
    if blf_el is None:
        return []
    blfs = []
    for blf in blf_el:
        if _strip_ns(blf.tag) != "busyLampField":
            continue
        # blfDirn is a flat text field (the DN pattern e.g. \+10001112222).
        # routePartition is a sibling of blfDirn, not a child.
        entry = {"blfDest": "", "blfDirn_pattern": "", "blfDirn_partition": "", "label": "", "index": ""}
        for child in blf:
            tag = _strip_ns(child.tag)
            if tag == "blfDirn":
                entry["blfDirn_pattern"] = (child.text or "").strip()
            elif tag == "routePartition":
                entry["blfDirn_partition"] = (child.text or "").strip()
            elif tag == "blfDest":
                entry["blfDest"] = _strip_cucm_escape((child.text or "").strip())
            elif tag in ("label", "index"):
                entry[tag] = (child.text or "").strip()
        if entry["index"]:
            blfs.append(entry)
    return sorted(blfs, key=lambda x: int(x.get("index") or 0))


def _build_blf_xml(blf: dict) -> str:
    # blfDirn is a flat text field; routePartition is a sibling (not child) of blfDirn.
    parts = []
    if blf.get("blfDest"):
        parts.append(f"<blfDest>{_xml_esc(blf['blfDest'])}</blfDest>")
    if blf.get("blfDirn_pattern"):
        parts.append(f"<blfDirn>{_xml_esc(blf['blfDirn_pattern'])}</blfDirn>")
        if blf.get("blfDirn_partition"):
            parts.append(f"<routePartition>{_xml_esc(blf['blfDirn_partition'])}</routePartition>")
    if blf.get("label"):
        parts.append(f"<label>{_xml_esc(blf['label'])}</label>")
    parts.append(f"<index>{blf['index']}</index>")
    return f"<busyLampField>{''.join(parts)}</busyLampField>"


# ---------------------------------------------------------------------------
# Speed Dial / BLF Updater — public API
# ---------------------------------------------------------------------------

def get_speed_dials_and_blfs(host: str, port: int, username: str, password: str,
                              verify_ssl: bool, device_name: str) -> tuple[list[dict], list[dict]] | tuple[None, None]:
    """
    Fetch both speed dials and BLFs for a phone in a single AXL call.
    Returns (None, None) if the device does not exist.
    """
    phone_el = _fetch_phone(host, port, username, password, verify_ssl, device_name)
    if phone_el is None:
        return None, None
    return _parse_sds_from_phone(phone_el), _parse_blfs_from_phone(phone_el)


def get_speed_dials(host: str, port: int, username: str, password: str,
                    verify_ssl: bool, device_name: str) -> list[dict] | None:
    """Return speed dials for a phone. Returns None if device not found."""
    phone_el = _fetch_phone(host, port, username, password, verify_ssl, device_name)
    if phone_el is None:
        return None
    return _parse_sds_from_phone(phone_el)


def update_speed_dial(host: str, port: int, username: str, password: str,
                      verify_ssl: bool, device_name: str,
                      dirn: str, label: str,
                      sd_index: int | None = None,
                      source: str = "") -> None:
    """
    Replace one speed dial on a phone (all other speed dials are preserved).
    Provide sd_index to target by position, or source to find by current number.
    If dirn is empty the matched entry is removed instead of updated.
    Raises LookupError if source is given but no matching entry is found.
    """
    phone_el = _fetch_phone(host, port, username, password, verify_ssl, device_name)
    if phone_el is None:
        raise ValueError(f"Device {device_name} not found.")
    current = _parse_sds_from_phone(phone_el)

    if source:
        norm = _strip_cucm_escape(source)
        match = next((sd for sd in current
                      if _strip_cucm_escape(sd.get("dirn", "")) == norm), None)
        if match is None:
            raise LookupError(f"No speed dial found matching '{source}'")
        sd_index = int(match["index"])

    updated = [sd for sd in current if str(sd.get("index")) != str(sd_index)]
    if dirn:
        updated.append({"index": str(sd_index), "dirn": dirn, "label": label})
    updated.sort(key=lambda x: int(x.get("index") or 0))

    sds_xml = "".join(
        f"<speeddial><dirn>{_xml_esc(sd['dirn'])}</dirn>"
        f"<label>{_xml_esc(sd.get('label', ''))}</label>"
        f"<index>{sd['index']}</index></speeddial>"
        for sd in updated
    )
    resp = _post(host, port, username, password, verify_ssl, "updatePhone",
                 _envelope("updatePhone",
                           f"<ns:updatePhone><name>{device_name}</name>"
                           f"<speeddials>{sds_xml}</speeddials></ns:updatePhone>"))
    if resp.status_code != 200:
        raise ConnectionError(f"updatePhone failed (HTTP {resp.status_code}): {resp.text[:500]}")


def update_blf(host: str, port: int, username: str, password: str,
               verify_ssl: bool, device_name: str,
               dest: str, label: str,
               blf_index: int | None = None,
               source: str = "",
               dirn_pattern: str = "", dirn_partition: str = "") -> None:
    """
    Replace one BLF entry on a phone (all other BLFs are preserved).
    Provide blf_index to target by position, or source to find by current number.
    source matches against blfDest or blfDirn (either field).
    If dirn_pattern is provided the entry is written as a monitored BLF DN.
    Raises LookupError if source is given but no matching entry is found.
    """
    phone_el = _fetch_phone(host, port, username, password, verify_ssl, device_name)
    if phone_el is None:
        raise ValueError(f"Device {device_name} not found.")
    current = _parse_blfs_from_phone(phone_el)

    if source:
        norm = _strip_cucm_escape(source)
        match = next(
            (b for b in current if
             _strip_cucm_escape(b.get("blfDest", "")) == norm or
             _strip_cucm_escape(b.get("blfDirn_pattern", "")) == norm),
            None,
        )
        if match is None:
            raise LookupError(f"No BLF found matching '{source}'")
        blf_index = int(match["index"])

    # blfDest is a dial string — strip CUCM's internal \+ escaping.
    # blfDirn is a numplan reference — keep \+ so CUCM can resolve the DN record.
    # When a DN is set, blfDest should be empty (matches CUCM GUI behaviour).
    dest = "" if dirn_pattern else _strip_cucm_escape(dest)

    updated = [b for b in current if str(b.get("index")) != str(blf_index)]
    if dest or dirn_pattern:
        updated.append({
            "index":             str(blf_index),
            "blfDest":           dest,
            "blfDirn_pattern":   dirn_pattern,
            "blfDirn_partition": dirn_partition,
            "label":             label,
        })
    updated.sort(key=lambda x: int(x.get("index") or 0))

    blfs_xml = "".join(_build_blf_xml(b) for b in updated)
    resp = _post(host, port, username, password, verify_ssl, "updatePhone",
                 _envelope("updatePhone",
                           f"<ns:updatePhone><name>{device_name}</name>"
                           f"<busyLampFields>{blfs_xml}</busyLampFields></ns:updatePhone>"))
    if resp.status_code != 200:
        raise ConnectionError(f"updatePhone failed (HTTP {resp.status_code}): {resp.text[:500]}")


def find_dn_partitions(host: str, port: int, username: str, password: str,
                       verify_ssl: bool, number: str) -> list[dict]:
    """Return all Directory Numbers matching number with their partition names.
    Searches both +12345 and \\+12345 forms since CUCM stores E.164 with \\+."""
    safe = number.replace("'", "''")
    if safe.startswith("+"):
        alt = "\\+" + safe[1:]
        where = f"(np.dnorpattern = '{safe}' OR np.dnorpattern = '{alt}')"
    elif safe.startswith("\\+"):
        alt = safe[1:]  # strip the backslash
        where = f"(np.dnorpattern = '{safe}' OR np.dnorpattern = '{alt}')"
    else:
        where = f"np.dnorpattern = '{safe}'"
    sql = f"""
        SELECT np.dnorpattern AS pattern, rp.name AS partition
        FROM numplan np
        LEFT JOIN routepartition rp ON np.fkroutepartition = rp.pkid
        WHERE {where}
        AND np.tkpatternusage = 2
        ORDER BY rp.name
    """
    return raw_query(host, port, username, password, verify_ssl, sql)


def get_phone_xml_debug(host: str, port: int, username: str, password: str,
                        verify_ssl: bool, device_name: str) -> dict:
    """
    Return the raw tag structure of speeddials and busyLampFields from getPhone,
    for debugging parse issues.
    """
    body = _envelope("getPhone", f"<ns:getPhone><name>{device_name}</name></ns:getPhone>")
    resp = _post(host, port, username, password, verify_ssl, "getPhone", body)
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}", "raw": resp.text[:2000]}
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {"error": str(e)}

    phone_el = None
    for tag in ("{http://www.cisco.com/AXL/API/15.0}phone", "phone"):
        phone_el = root.find(f".//{tag}")
        if phone_el is not None:
            break
    if phone_el is None:
        return {"error": "phone element not found", "raw": resp.text[:2000]}

    def _el_to_dict(el, depth=0):
        tag = _strip_ns(el.tag)
        text = (el.text or "").strip()
        children = [_el_to_dict(c, depth + 1) for c in el]
        return {"tag": tag, "text": text, "children": children} if children else {"tag": tag, "text": text}

    result = {"top_level_tags": [], "speeddials": [], "busyLampFields": []}
    for child in phone_el:
        tag = _strip_ns(child.tag)
        result["top_level_tags"].append(tag)
        if tag == "speeddials":
            result["speeddials"] = [_el_to_dict(c) for c in child]
        elif tag == "busyLampFields":
            result["busyLampFields"] = [_el_to_dict(c) for c in child]
    return result


# ---------------------------------------------------------------------------
# Gateway Migration — constants
# ---------------------------------------------------------------------------

# The CUCM device product string used for CUCM-controlled SIP analog endpoints.
# Confirm against a lab cluster (see plan §H): manually create one SIP analog
# endpoint in CUCM GUI, then getGatewayEndpointAnalogAccess to capture the exact
# <product> value. If wrong, add_gateway_endpoint_smart will surface the AXL
# rejection with the canonical string in the error text.
SIP_ENDPOINT_PRODUCT = "Cisco SIP FXS Port"

# Tags excluded from endpoint copy across protocols. Mirrors _PHONE_SKIP_TAGS:
# explicit/structural fields, read-only fields, and SCCP-only fields the SIP
# analog endpoint doesn't accept. The smart-retry path handles additional
# rejections at runtime.
_GATEWAY_ENDPOINT_SKIP_TAGS = {
    # Explicit / structural
    "name", "description", "product", "model", "class", "protocol", "protocolSide",
    "domainName", "unit", "subunit", "index", "endpoint", "port",
    # Container elements
    "lines", "ports", "endpoints", "subunits", "units",
    # Auto-assigned per target
    "phoneSecurityProfileName", "securityProfileName",
    # Read-only
    "uuid", "loadInformation", "versionStamp", "tkModel", "tkDeviceProtocol",
    "certificateOperation", "certificateStatus", "isProtected", "isActive",
}

# Default chassis layouts for SIP analog gateway products. Keyed by the CUCM
# <product> string. Each layout drives the addGateway units/subunits skeleton
# and the UI's port-count expectations. Confirm against `typeproduct` rows on
# the lab cluster — the products endpoint falls back to this when SQL returns
# nothing usable.
# Verified VG410 + VG420 strings from a real CUCM 15 cluster (getGateway
# response). VG450 still needs lab confirmation — capture it by manually
# creating one in CUCM GUI then calling /api/gateway-migration/lookup against
# it.
SIP_GATEWAY_CHASSIS = {
    "VG410": {
        "capacity": 48,
        "variants": [
            {"label": "VG410 24-port (1×24 FXS)", "capacity": 24,
             "units": [{"index": 0, "product": "VG-1NIM-MBRD",
                        "subunits": [{"index": 1, "product": "VG-24FXS-SIP", "beginPort": 0}]}]},
            {"label": "VG410 48-port (2×24 FXS)", "capacity": 48,
             "units": [{"index": 0, "product": "VG-1NIM-MBRD",
                        "subunits": [{"index": 1, "product": "VG-24FXS-SIP", "beginPort": 0},
                                     {"index": 2, "product": "VG-24FXS-SIP", "beginPort": 24}]}]},
        ],
    },
    "VG420": {
        "capacity": 144,
        "variants": [
            {"label": "VG420 144-port (1×144 FXS)", "capacity": 144,
             "units": [{"index": 1, "product": "ANALOG",
                        "subunits": [{"index": 0, "product": "SM-V-144FXS-SIP", "beginPort": 0}]}]},
        ],
    },
    "VG450": {
        "capacity": 144,
        "variants": [
            {"label": "VG450 144-port (UNVERIFIED — confirm against lab)", "capacity": 144,
             "units": [{"index": 1, "product": "ANALOG",
                        "subunits": [{"index": 0, "product": "SM-V-144FXS-SIP", "beginPort": 0}]}]},
        ],
    },
}


# ---------------------------------------------------------------------------
# Gateway Migration — discovery / reads
# ---------------------------------------------------------------------------

def derive_an_endpoint_name(domain_or_mac: str, unit: int, subunit: int,
                            port_number: int) -> str:
    """
    Generate the CUCM device name for an AN-style analog endpoint.

    CUCM 15 stores SIP analog endpoints (on VG410/VG420/VG450) as regular
    `device` rows with names `AN<mac10><HHH>`, where:
      - <mac10> is the last 10 hex of the chassis MAC (the same chars that
        appear after `SIPGW` in a SIP gateway's domain name)
      - <HHH> is 3 hex chars encoding (slot << 9) | (subunit << 7) | (port-1)

    The same encoding is used by SCCP VG224 (which uses slot=2 on the
    motherboard) and SIP VG410 (slot=0, subunit=1 for the FXS module).
    """
    s = domain_or_mac.strip().upper()
    candidate = s[5:] if s.startswith("SIPGW") else s
    hex_only = re.sub(r"[^0-9A-F]", "", candidate)
    # Always reduce to the last 10 hex chars, regardless of input length.
    mac_suffix = hex_only[-10:] if len(hex_only) >= 10 else hex_only
    encoding = (unit << 9) | (subunit << 7) | (port_number - 1)
    return f"AN{mac_suffix}{encoding:03X}"


def _mac_to_an_substring(raw: str) -> str:
    """
    Convert a chassis MAC in any format to the hex substring that appears in
    the SCCP analog port device name. VG224 derives port MACs by dropping the
    leading 3 hex chars of the chassis MAC; the resulting 9 hex chars appear
    in every AN* device on that chassis.
    """
    hex_only = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    return hex_only[3:] if len(hex_only) == 12 else hex_only


_AALN_NAME_RE = re.compile(
    r"^AALN/S(\d+)(?:/SU(\d+))?/(\d+)@(.+)$", re.IGNORECASE
)


def parse_aaln_name(name: str) -> tuple[int, int, int, str] | None:
    """
    Parse an AALN-style MGCP endpoint device name into its components.

    AALN/S<slot>/[SU<subunit>/]<port>@<domain>
      - VG2xx (single subunit): AALN/S2/0@mgcp01.test.com    → (2, 0, 0, "mgcp01.test.com")
      - VG3xx (multi-subunit):  AALN/S2/SU1/3@gw.example.com → (2, 1, 3, "gw.example.com")
    Returns (slot, subunit, port, domain) or None if the name doesn't match.
    """
    m = _AALN_NAME_RE.match(name.strip())
    if not m:
        return None
    return (
        int(m.group(1)),
        int(m.group(2) or 0),
        int(m.group(3)),
        m.group(4).strip(),
    )


def list_aaln_ports_by_domain(host: str, port: int, username: str, password: str,
                               verify_ssl: bool, gateway_domain: str) -> list[dict]:
    """
    Find every MGCP analog endpoint device (AALN-style name) belonging to a
    gateway, regardless of whether getGateway lists them inline in the
    chassis structure.
    """
    safe = gateway_domain.replace("'", "''")
    sql = f"""
        SELECT d.name, d.description, dp.name AS devicepool_name
        FROM device d
        LEFT JOIN devicepool dp ON dp.pkid = d.fkdevicepool
        WHERE d.name LIKE 'AALN/%@{safe}'
        ORDER BY d.name
    """
    return raw_query(host, port, username, password, verify_ssl, sql)


def list_an_ports_by_mac(host: str, port: int, username: str, password: str,
                          verify_ssl: bool, chassis_mac: str) -> list[dict]:
    """
    Find all SCCP analog port phones (AN*) belonging to a chassis identified
    by its MAC address. Returns rows with name, description, and device pool.
    """
    mac_sub = _mac_to_an_substring(chassis_mac)
    if not mac_sub:
        return []
    safe = mac_sub.replace("'", "''")
    sql = f"""
        SELECT d.name AS name, d.description AS description, dp.name AS devicepool_name
        FROM device d
        LEFT JOIN devicepool dp ON dp.pkid = d.fkdevicepool
        WHERE d.name LIKE 'AN%{safe}%'
        ORDER BY d.name
    """
    return raw_query(host, port, username, password, verify_ssl, sql)


def _fetch_gateway(host: str, port: int, username: str, password: str,
                   verify_ssl: bool, domain_name: str):
    """Call AXL getGateway and return the <gateway> XML element, or None."""
    body = _envelope("getGateway",
                     f"<ns:getGateway><domainName>{_xml_esc(domain_name)}</domainName></ns:getGateway>")
    resp = _post(host, port, username, password, verify_ssl, "getGateway", body)
    if resp.status_code != 200:
        return None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None
    for tag in ("{http://www.cisco.com/AXL/API/15.0}gateway", "gateway"):
        el = root.find(f".//{tag}")
        if el is not None:
            return el
    return None


def _parse_endpoint_summary(ep_el) -> dict:
    """Flatten a <endpoint> child of getGateway's units/subunits to a summary dict."""
    out: dict = {}
    for child in ep_el:
        tag = _strip_ns(child.tag)
        out[tag] = (child.text or "").strip()
    return out


def get_gateway(host: str, port: int, username: str, password: str,
                verify_ssl: bool, domain_name: str) -> dict | None:
    """
    Return the MGCP/SIP gateway chassis as a dict with units/subunits/endpoints.
    Returns None if the gateway does not exist.
    """
    gw_el = _fetch_gateway(host, port, username, password, verify_ssl, domain_name)
    if gw_el is None:
        return None

    gw: dict = {"domainName": domain_name, "units": []}
    for child in gw_el:
        tag = _strip_ns(child.tag)
        if tag == "units":
            for unit_el in child:
                if _strip_ns(unit_el.tag) != "unit":
                    continue
                unit: dict = {"index": "0", "product": "", "subunits": []}
                for u_child in unit_el:
                    ut = _strip_ns(u_child.tag)
                    if ut == "subunits":
                        for su_el in u_child:
                            if _strip_ns(su_el.tag) != "subunit":
                                continue
                            sub: dict = {"index": "0", "product": "", "endpoints": []}
                            for su_child in su_el:
                                st = _strip_ns(su_child.tag)
                                if st == "endpoints":
                                    for ep_el in su_child:
                                        if _strip_ns(ep_el.tag) == "endpoint":
                                            sub["endpoints"].append(
                                                _parse_endpoint_summary(ep_el))
                                else:
                                    sub[st] = (su_child.text or "").strip()
                            unit["subunits"].append(sub)
                    else:
                        unit[ut] = (u_child.text or "").strip()
                gw["units"].append(unit)
        else:
            gw[tag] = (child.text or "").strip()
    return gw


def _fetch_gateway_endpoint(host: str, port: int, username: str, password: str,
                            verify_ssl: bool, domain_name: str,
                            unit: int, subunit: int, endpoint_index: int):
    body = _envelope(
        "getGatewayEndpointAnalogAccess",
        f"<ns:getGatewayEndpointAnalogAccess>"
        f"<domainName>{_xml_esc(domain_name)}</domainName>"
        f"<unit>{unit}</unit>"
        f"<subunit>{subunit}</subunit>"
        f"<endpoint><index>{endpoint_index}</index></endpoint>"
        f"</ns:getGatewayEndpointAnalogAccess>",
    )
    resp = _post(host, port, username, password, verify_ssl,
                 "getGatewayEndpointAnalogAccess", body)
    if resp.status_code != 200:
        return None
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return None
    for tag in ("{http://www.cisco.com/AXL/API/15.0}endpoint", "endpoint"):
        el = root.find(f".//{tag}")
        if el is not None:
            return el
    return None


def get_gateway_endpoint(host: str, port: int, username: str, password: str,
                         verify_ssl: bool, domain_name: str,
                         unit: int, subunit: int, endpoint_index: int) -> dict | None:
    """
    Return a fully-flattened analog endpoint dict, including nested port/line/dirn
    data. Returns None if the endpoint does not exist.
    """
    ep_el = _fetch_gateway_endpoint(host, port, username, password, verify_ssl,
                                     domain_name, unit, subunit, endpoint_index)
    if ep_el is None:
        return None

    out: dict = {"_unit": unit, "_subunit": subunit, "_index": endpoint_index}
    for child in ep_el:
        tag = _strip_ns(child.tag)
        if tag == "port":
            port_dict: dict = {"lines": []}
            for p_child in child:
                pt = _strip_ns(p_child.tag)
                if pt == "lines":
                    for line_el in p_child:
                        if _strip_ns(line_el.tag) != "line":
                            continue
                        line: dict = {}
                        for l_child in line_el:
                            lt = _strip_ns(l_child.tag)
                            if lt == "dirn":
                                for d_child in l_child:
                                    dt = _strip_ns(d_child.tag)
                                    if dt == "pattern":
                                        line["pattern"] = (d_child.text or "").strip()
                                    elif dt == "routePartitionName":
                                        line["partition"] = (d_child.text or "").strip()
                            else:
                                line[lt] = (l_child.text or "").strip()
                        port_dict["lines"].append(line)
                else:
                    port_dict[pt] = (p_child.text or "").strip()
            out["port"] = port_dict
        elif tag == "vendorConfig":
            out["vendorConfig"] = _serialize_vendor_config(child)
        else:
            out[tag] = (child.text or "").strip()
    return out


def get_ccm_version(host: str, port: int, username: str, password: str,
                    verify_ssl: bool) -> str:
    """Return the AXL-reported CUCM version string (e.g. '15.0.1.10000-1')."""
    body = _envelope("getCCMVersion", "<ns:getCCMVersion/>")
    resp = _post(host, port, username, password, verify_ssl, "getCCMVersion", body)
    if resp.status_code != 200:
        return ""
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError:
        return ""
    for tag in ("{http://www.cisco.com/AXL/API/15.0}version", "version"):
        el = root.find(f".//{tag}")
        if el is not None and el.text:
            return el.text.strip()
    return ""


# ---------------------------------------------------------------------------
# Gateway Migration — writes
# ---------------------------------------------------------------------------

def _build_units_xml(units: list[dict]) -> str:
    """Serialize the units/subunits skeleton for addGateway."""
    parts = []
    for u in units:
        sub_parts = []
        for su in u.get("subunits", []):
            sub_parts.append(
                f"<subunit>"
                f"<index>{su.get('index', 0)}</index>"
                f"<product>{_xml_esc(str(su.get('product', '')))}</product>"
                f"{_xml_opt('beginPort', str(su.get('beginPort', '')))}"
                f"</subunit>"
            )
        parts.append(
            f"<unit>"
            f"<index>{u.get('index', 0)}</index>"
            f"<product>{_xml_esc(str(u.get('product', '')))}</product>"
            f"<subunits>{''.join(sub_parts)}</subunits>"
            f"</unit>"
        )
    return f"<units>{''.join(parts)}</units>"


def add_gateway(host: str, port: int, username: str, password: str, verify_ssl: bool,
                domain_name: str, product: str, protocol: str,
                description: str, call_manager_group: str,
                units: list[dict]) -> None:
    """Create a new gateway chassis in CUCM. protocol is 'SIP' or 'MGCP'."""
    body = _envelope(
        "addGateway",
        f"<ns:addGateway><gateway>"
        f"<domainName>{_xml_esc(domain_name)}</domainName>"
        f"<product>{_xml_esc(product)}</product>"
        f"<protocol>{_xml_esc(protocol)}</protocol>"
        f"{_xml_opt('description', description)}"
        f"{_xml_opt('callManagerGroupName', call_manager_group)}"
        f"{_build_units_xml(units)}"
        f"</gateway></ns:addGateway>",
    )
    resp = _post(host, port, username, password, verify_ssl, "addGateway", body)
    if resp.status_code != 200:
        raise ConnectionError(f"addGateway failed (HTTP {resp.status_code}): {resp.text[:600]}")


def remove_gateway(host: str, port: int, username: str, password: str,
                   verify_ssl: bool, domain_name: str) -> None:
    body = _envelope(
        "removeGateway",
        f"<ns:removeGateway><domainName>{_xml_esc(domain_name)}</domainName></ns:removeGateway>",
    )
    resp = _post(host, port, username, password, verify_ssl, "removeGateway", body)
    if resp.status_code != 200:
        raise ConnectionError(f"removeGateway failed (HTTP {resp.status_code}): {resp.text[:500]}")


def remove_gateway_endpoint(host: str, port: int, username: str, password: str,
                            verify_ssl: bool, domain_name: str,
                            unit: int, subunit: int, endpoint_index: int) -> None:
    body = _envelope(
        "removeGatewayEndpointAnalogAccess",
        f"<ns:removeGatewayEndpointAnalogAccess>"
        f"<domainName>{_xml_esc(domain_name)}</domainName>"
        f"<unit>{unit}</unit>"
        f"<subunit>{subunit}</subunit>"
        f"<endpoint><index>{endpoint_index}</index></endpoint>"
        f"</ns:removeGatewayEndpointAnalogAccess>",
    )
    resp = _post(host, port, username, password, verify_ssl,
                 "removeGatewayEndpointAnalogAccess", body)
    if resp.status_code != 200:
        raise ConnectionError(
            f"removeGatewayEndpointAnalogAccess failed (HTTP {resp.status_code}): {resp.text[:500]}"
        )


def _build_endpoint_xml(endpoint: dict, excluded: set[str]) -> str:
    """
    Build the <endpoint> XML body for addGatewayEndpointAnalogAccess.
    Required keys: name, product, port_number, line (dict with pattern, partition,
    label, display, displayAscii, e164Mask). Optional: devicePoolName, locationName,
    callingSearchSpaceName, commonPhoneConfigName, vendorConfig (pre-serialized XML),
    and any other tag CUCM accepts on the endpoint.
    """
    line = endpoint.get("line", {}) or {}
    line_xml = (
        "<line>"
        "<index>1</index>"
        f"{_xml_opt('label', line.get('label', ''))}"
        f"{_xml_opt('display', line.get('display', ''))}"
        f"{_xml_opt('displayAscii', line.get('displayAscii', '') or line.get('displayascii', ''))}"
        f"{_xml_opt('e164Mask', line.get('e164Mask', '') or line.get('e164mask', ''))}"
        "<dirn>"
        f"<pattern>{_xml_esc(line.get('pattern', ''))}</pattern>"
        f"{_xml_opt('routePartitionName', line.get('partition', ''))}"
        "</dirn>"
        "</line>"
    )

    port_number = endpoint.get("port_number", endpoint.get("portNumber", 1))
    port_xml = (
        "<port>"
        f"<portNumber>{port_number}</portNumber>"
        f"<lines>{line_xml}</lines>"
        f"<trunk>{_xml_esc(endpoint.get('trunk', 'POTS'))}</trunk>"
        f"<trunkDirection>{_xml_esc(endpoint.get('trunkDirection', 'Bothways'))}</trunkDirection>"
        "</port>"
    )

    # Required envelope fields
    head = (
        f"<index>{endpoint.get('index', port_number)}</index>"
        f"<name>{_xml_esc(endpoint.get('name', ''))}</name>"
        f"<product>{_xml_esc(endpoint.get('product', SIP_ENDPOINT_PRODUCT))}</product>"
        f"<class>{_xml_esc(endpoint.get('class', 'Gateway'))}</class>"
        f"<protocol>{_xml_esc(endpoint.get('protocol', 'Analog Access'))}</protocol>"
        f"<protocolSide>{_xml_esc(endpoint.get('protocolSide', 'User'))}</protocolSide>"
    )

    # Optional copy-through fields (everything else CUCM accepts)
    optional_xml = ""
    for tag, value in endpoint.items():
        if tag in _GATEWAY_ENDPOINT_SKIP_TAGS or tag in excluded:
            continue
        if tag in ("line", "port_number", "portNumber", "trunk", "trunkDirection"):
            continue
        if tag.startswith("_"):
            continue
        if tag == "vendorConfig":
            optional_xml += value or ""        # pre-serialized
        elif value:
            optional_xml += _xml_opt(tag, str(value))

    return f"<endpoint>{head}{optional_xml}{port_xml}</endpoint>"


def update_gateway_endpoint_smart(host: str, port: int, username: str, password: str,
                                   verify_ssl: bool, domain_name: str,
                                   unit: int, subunit: int, endpoint_index: int,
                                   endpoint: dict) -> dict:
    """
    Update an existing analog endpoint slot on a gateway via
    updateGatewayEndpointAnalogAccess. Same payload shape as the add variant.

    Use this when the chassis slot is already allocated (e.g. immediately
    after removePhone, where CUCM keeps the slot placeholder but clears the
    device row). addGatewayEndpointAnalogAccess returns the opaque axlcode=-1
    in that state because it sees a slot conflict.
    """
    excluded: set[str] = set()
    candidate_tags = {
        tag for tag, value in endpoint.items()
        if value and tag not in _GATEWAY_ENDPOINT_SKIP_TAGS
        and tag not in ("line", "port_number", "portNumber", "trunk", "trunkDirection")
        and not tag.startswith("_")
    }
    max_attempts = len(candidate_tags) + 4

    # updateGatewayEndpointAnalogAccess requires the endpoint <name> at the
    # top level of the operation (the AXL update pattern: identify target,
    # then send changes). Nesting it inside <endpoint> like the add variant
    # produces "No uuid or name element found".
    endpoint_name = endpoint.get("name", "")
    for _ in range(max_attempts):
        ep_xml = _build_endpoint_xml(endpoint, excluded)
        body = _envelope(
            "updateGatewayEndpointAnalogAccess",
            f"<ns:updateGatewayEndpointAnalogAccess>"
            f"<name>{_xml_esc(endpoint_name)}</name>"
            f"<domainName>{_xml_esc(domain_name)}</domainName>"
            f"<unit>{unit}</unit><subunit>{subunit}</subunit>"
            f"{ep_xml}"
            f"</ns:updateGatewayEndpointAnalogAccess>",
        )
        resp = _post(host, port, username, password, verify_ssl,
                     "updateGatewayEndpointAnalogAccess", body)

        if resp.status_code == 200:
            return {
                "transferred": sorted(candidate_tags - excluded),
                "skipped":     sorted(excluded),
            }

        field = _extract_unsupported_field(resp.text)
        if field and field not in excluded:
            excluded.add(field)
            candidate_tags.add(field)
            continue

        import sys
        print(f"[axl.update_gateway_endpoint_smart] "
              f"updateGatewayEndpointAnalogAccess REJECTED. Request body was:\n"
              f"{body}\nResponse: {resp.text[:800]}",
              file=sys.stderr, flush=True)
        raise ConnectionError(
            f"updateGatewayEndpointAnalogAccess failed (HTTP {resp.status_code}): {resp.text[:600]}"
        )

    raise ConnectionError(
        "updateGatewayEndpointAnalogAccess failed after exhausting all field combinations."
    )


def add_gateway_endpoint_smart(host: str, port: int, username: str, password: str,
                                verify_ssl: bool, domain_name: str,
                                unit: int, subunit: int, endpoint_index: int,
                                endpoint: dict) -> dict:
    """
    Add an analog endpoint to a gateway with automatic field-compatibility
    detection. Mirrors add_phone_smart: if CUCM rejects a field as unsupported,
    drop it and retry. Returns {transferred: [...], skipped: [...]}.
    """
    excluded: set[str] = set()
    candidate_tags = {
        tag for tag, value in endpoint.items()
        if value and tag not in _GATEWAY_ENDPOINT_SKIP_TAGS
        and tag not in ("line", "port_number", "portNumber", "trunk", "trunkDirection")
        and not tag.startswith("_")
    }
    max_attempts = len(candidate_tags) + 4

    for _ in range(max_attempts):
        ep_xml = _build_endpoint_xml(endpoint, excluded)
        body = _envelope(
            "addGatewayEndpointAnalogAccess",
            f"<ns:addGatewayEndpointAnalogAccess>"
            f"<domainName>{_xml_esc(domain_name)}</domainName>"
            f"<unit>{unit}</unit><subunit>{subunit}</subunit>"
            f"{ep_xml}"
            f"</ns:addGatewayEndpointAnalogAccess>",
        )
        resp = _post(host, port, username, password, verify_ssl,
                     "addGatewayEndpointAnalogAccess", body)

        if resp.status_code == 200:
            return {
                "transferred": sorted(candidate_tags - excluded),
                "skipped":     sorted(excluded),
            }

        field = _extract_unsupported_field(resp.text)
        if field and field not in excluded:
            excluded.add(field)
            candidate_tags.add(field)
            continue

        import sys
        print(f"[axl.add_gateway_endpoint_smart] addGatewayEndpointAnalogAccess "
              f"REJECTED. Request body was:\n{body}\nResponse: {resp.text[:800]}",
              file=sys.stderr, flush=True)
        raise ConnectionError(
            f"addGatewayEndpointAnalogAccess failed (HTTP {resp.status_code}): {resp.text[:600]}"
        )

    raise ConnectionError(
        "addGatewayEndpointAnalogAccess failed after exhausting all field combinations."
    )


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
