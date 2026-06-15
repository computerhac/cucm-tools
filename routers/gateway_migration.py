"""
Gateway Migration Tool — migrate SCCP/MGCP analog gateways (e.g. VG224) to
CUCM-controlled SIP analog gateways (VG410/VG420/VG450) one port at a time.

Per port: snapshot source → remove source → add SIP endpoint → store snapshot
so the row can be rolled back later. Snapshots live in-memory for the life
of the process; the dashboard's Refresh button reconciles state on restart.
"""

import asyncio
import datetime
import re
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

import axl
import database as db
from models import (
    GatewayLookupRequest, GatewayLookupResponse, PortRecord,
    CreateSipGatewayRequest,
    MigratePortRequest, RollbackPortRequest, PortMigrationResult,
    MigrateBatchRequest, RollbackBatchRequest,
    RestoreBackupRequest, RestorePortResult,
)

router = APIRouter(prefix="/api/gateway-migration", tags=["gateway-migration"])

# CUCM AXL is throttle-sensitive — keep concurrency low even for 144-port batches.
_executor = ThreadPoolExecutor(max_workers=4)

# In-memory snapshot store: snapshot_id -> {kind, source_*, info, lines}
# Used for explicit per-port rollback after a successful or failed migrate.
_snapshots: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{12}$")


def _looks_like_mac(s: str) -> bool:
    return bool(_MAC_RE.match(re.sub(r"[^0-9A-Fa-f]", "", s)))


def _normalize_mac(s: str) -> str:
    return re.sub(r"[^0-9A-Fa-f]", "", s).upper()


def _sccp_gateway_name_from_mac(s: str) -> str:
    """
    CUCM names SCCP analog gateways `SKIGW<last10 of MAC>`. Same convention
    the GUI uses (Device → Gateway list). Accepts either a 12-char MAC or
    the 10-char tail directly.
    """
    hex_only = _normalize_mac(s)
    return "SKIGW" + hex_only[-10:]


def _parse_port_index_from_an(an_name: str, mac_substring: str) -> tuple[int, int, int]:
    """
    AN device names are AN + chassis-mac-suffix + 3 hex chars encoding
    (slot << 9) | (subunit << 7) | (port - 1). Return (slot, subunit, port)
    with port 1-based. Returns (0, 0, 1) on parse failure.
    """
    if not an_name.startswith("AN"):
        return (0, 0, 1)
    tail = an_name[2:]
    idx = tail.find(mac_substring)
    if idx < 0:
        return (0, 0, 1)
    suffix = tail[idx + len(mac_substring):]
    if not suffix:
        return (0, 0, 1)
    try:
        val = int(suffix, 16)
    except ValueError:
        return (0, 0, 1)
    slot    = (val >> 9) & 0x7
    subunit = (val >> 7) & 0x3
    port    = (val & 0x7F) + 1
    return (slot, subunit, port)


def _version_below_15(ver: str) -> bool:
    """Return True if the AXL-reported version string is below 15.0."""
    if not ver:
        return False
    m = re.match(r"(\d+)", ver)
    if not m:
        return False
    return int(m.group(1)) < 15


def _capacity_for_product(product: str) -> int:
    layout = axl.SIP_GATEWAY_CHASSIS.get(product)
    return layout["capacity"] if layout else 0


def _build_port_record_from_phone(side: str, device_info: dict,
                                  lines: list[dict], port_index: int,
                                  name_override: str = "") -> PortRecord:
    line = lines[0] if lines else {}
    return PortRecord(
        side=side,
        name=name_override or device_info.get("name", ""),
        index=port_index,
        dn=line.get("pattern") or None,
        partition=line.get("partition") or None,
        css=device_info.get("callingSearchSpaceName") or None,
        device_pool=device_info.get("devicePoolName") or None,
        location=device_info.get("locationName") or None,
        common_phone_config=device_info.get("commonPhoneConfigName") or None,
        display=line.get("display") or None,
        display_ascii=line.get("displayascii") or line.get("displayAscii") or None,
        alerting_name=line.get("alertingName") or None,
        e164_mask=line.get("e164mask") or line.get("e164Mask") or None,
        label=line.get("label") or None,
    )


def _build_port_record_from_endpoint(side: str, endpoint: dict,
                                     unit: int, subunit: int) -> PortRecord:
    port = endpoint.get("port", {}) or {}
    lines = port.get("lines", []) or []
    line = lines[0] if lines else {}
    try:
        idx = int(port.get("portNumber") or endpoint.get("index") or 1)
    except (TypeError, ValueError):
        idx = 1
    return PortRecord(
        side=side,
        name=endpoint.get("name", ""),
        index=idx,
        unit=unit,
        subunit=subunit,
        endpoint_index=int(endpoint.get("_index", endpoint.get("index", idx)) or idx),
        dn=line.get("pattern") or None,
        partition=line.get("partition") or None,
        css=endpoint.get("callingSearchSpaceName") or None,
        device_pool=endpoint.get("devicePoolName") or None,
        location=endpoint.get("locationName") or None,
        common_phone_config=endpoint.get("commonPhoneConfigName") or None,
        display=line.get("display") or None,
        display_ascii=line.get("displayAscii") or None,
        alerting_name=line.get("alertingName") or None,
        e164_mask=line.get("e164Mask") or None,
        label=line.get("label") or None,
    )


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _discover_sccp(creds: dict, chassis_mac: str,
                    source_card_size: int = 24) -> GatewayLookupResponse:
    mac_norm = _normalize_mac(chassis_mac)
    rows = axl.list_an_ports_by_mac(**creds, chassis_mac=mac_norm)
    mac_sub = axl._mac_to_an_substring(mac_norm)
    raw: list[tuple[PortRecord, int, int]] = []   # (record, slot, subunit)
    for row in rows:
        name = row.get("name", "")
        info = axl.get_device(**creds, device_name=name)
        if not info:
            continue
        lines = axl.get_device_lines(**creds, device_name=name)
        slot, subunit, port_1based = _parse_port_index_from_an(name, mac_sub)
        pr = _build_port_record_from_phone("source", info, lines, port_1based, name)
        pr.unit = slot
        pr.subunit = subunit
        raw.append((pr, slot, subunit))

    # Compute chassis_port from the slot/subunit ordering. Each unique
    # (slot, subunit) tuple is one physical FXS card; sort them and assign a
    # cumulative beginPort using `source_card_size`. SCCP gateways don't have
    # a central "gateway" record we can read card sizes from, hence the
    # caller-provided default (VG224 = 24, VG310/320/350 high-density = 48/72).
    tuples = sorted({(s, su) for (_, s, su) in raw})
    offsets = {t: i * source_card_size for i, t in enumerate(tuples)}
    ports: list[PortRecord] = []
    for pr, s, su in raw:
        pr.chassis_port = offsets.get((s, su), 0) + pr.index
        ports.append(pr)

    ports.sort(key=lambda p: (p.unit or 0, p.subunit or 0, p.index))
    return GatewayLookupResponse(
        kind="SCCP",
        mac=mac_norm,
        product=ports and "Cisco VG2xx (SCCP)" or None,
        capacity=len(ports) or None,
        ports=ports,
    )


def _discover_gateway_record(creds: dict, domain_name: str,
                              side: str) -> GatewayLookupResponse | None:
    gw = axl.get_gateway(**creds, domain_name=domain_name)
    if gw is None:
        return None
    ports: list[PortRecord] = []
    capacity = 0
    # Cumulative-offset map: (unit_index, subunit_index) -> beginPort (0-based).
    # Built by walking units/subunits in physical order and summing each
    # subunit's FXS capacity (parsed from the product string). Used to derive
    # chassis_port = offset + per_subunit_index so house cabling stays put
    # across an amphenol swap.
    subunit_offset: dict[tuple[int, int], int] = {}
    running = 0
    for unit in sorted(gw.get("units", []),
                        key=lambda u: int(u.get("index") or 0)):
        u_idx = int(unit.get("index") or 0)
        for sub in sorted(unit.get("subunits", []),
                           key=lambda s: int(s.get("index") or 0)):
            s_idx = int(sub.get("index") or 0)
            subunit_offset[(u_idx, s_idx)] = running
            m = re.search(r"(\d+)\s*FXS", sub.get("product", ""))
            if m:
                running += int(m.group(1))
    capacity = running

    for unit in gw.get("units", []):
        try:
            u_idx = int(unit.get("index", 0))
        except (TypeError, ValueError):
            u_idx = 0
        for sub in unit.get("subunits", []):
            try:
                s_idx = int(sub.get("index", 0))
            except (TypeError, ValueError):
                s_idx = 0
            for ep_summary in sub.get("endpoints", []):
                try:
                    ep_index = int(ep_summary.get("index", 0))
                except (TypeError, ValueError):
                    ep_index = 0
                full = axl.get_gateway_endpoint(
                    **creds, domain_name=domain_name,
                    unit=u_idx, subunit=s_idx, endpoint_index=ep_index,
                )
                if full is None:
                    continue
                full.setdefault("_unit", u_idx)
                full.setdefault("_subunit", s_idx)
                full.setdefault("_index", ep_index)
                pr = _build_port_record_from_endpoint(side, full, u_idx, s_idx)
                pr.chassis_port = subunit_offset.get((u_idx, s_idx), 0) + pr.index
                ports.append(pr)

    # Fallback: getGateway doesn't always inline the endpoints (notably it
    # returns empty endpoints[] for MGCP gateways too on CUCM 15). Scan the
    # device table for AALN-style names belonging to this domain and enrich
    # each via getPhone — same approach SCCP discovery uses.
    sccp_an_mac: str | None = None
    if domain_name.upper().startswith("SKIGW"):
        sccp_an_mac = _normalize_mac(domain_name[5:])

    if not ports and sccp_an_mac:
        # SCCP gateway record exists in CUCM but ports live as AN devices
        # rather than AALN. Scan device table by MAC and decode the HHH suffix
        # for each one.
        try:
            an_rows = axl.list_an_ports_by_mac(**creds, chassis_mac=sccp_an_mac)
        except Exception:
            an_rows = []
        mac_sub = axl._mac_to_an_substring(sccp_an_mac)
        for row in an_rows:
            name = row.get("name", "")
            info = axl.get_device(**creds, device_name=name)
            if not info:
                continue
            lines = axl.get_device_lines(**creds, device_name=name)
            slot, subunit, port_1based = _parse_port_index_from_an(name, mac_sub)
            pr = _build_port_record_from_phone(side, info, lines, port_1based, name)
            pr.unit = slot
            pr.subunit = subunit
            pr.chassis_port = subunit_offset.get((slot, subunit), 0) + port_1based
            ports.append(pr)

    if not ports:
        try:
            aaln_rows = axl.list_aaln_ports_by_domain(**creds, gateway_domain=domain_name)
        except Exception:
            aaln_rows = []
        for row in aaln_rows:
            name = row.get("name", "")
            parsed = axl.parse_aaln_name(name)
            if not parsed:
                continue
            slot, subunit, port_num, _ = parsed
            info = axl.get_device(**creds, device_name=name)
            if not info:
                continue
            lines = axl.get_device_lines(**creds, device_name=name)
            line = lines[0] if lines else {}
            chassis_port = subunit_offset.get((slot, subunit), 0) + port_num + 1
            ports.append(PortRecord(
                side=side,
                name=name,
                index=port_num + 1,         # 1-based for UI
                unit=slot,
                subunit=subunit,
                chassis_port=chassis_port,
                endpoint_index=port_num,    # 0-based for AXL coords
                dn=line.get("pattern") or None,
                partition=line.get("partition") or None,
                css=info.get("callingSearchSpaceName") or None,
                device_pool=info.get("devicePoolName") or None,
                location=info.get("locationName") or None,
                common_phone_config=info.get("commonPhoneConfigName") or None,
                display=line.get("display") or None,
                display_ascii=line.get("displayascii") or line.get("displayAscii") or None,
                alerting_name=line.get("alertingName") or None,
                e164_mask=line.get("e164mask") or line.get("e164Mask") or None,
                label=line.get("label") or None,
            ))

    # Filter "phantom" ports whose (slot, subunit) doesn't match any card
    # actually installed on the chassis — CUCM creates a service/control
    # endpoint at slot=7/subunit=3/port=128 (HHH=FFF) for the gateway's own
    # signaling that shouldn't appear as a migratable analog port.
    valid_slots = set(subunit_offset.keys())
    if valid_slots:
        ports = [p for p in ports if (p.unit, p.subunit) in valid_slots]

    ports.sort(key=lambda p: (p.unit or 0, p.subunit or 0, p.index))
    kind = (gw.get("protocol") or "").upper()
    if not kind:
        # Skinny gateway records (SKIGW…) sometimes omit the protocol element.
        # Fall back to the name prefix so the dashboard knows which migration
        # path to follow.
        kind = "SCCP" if domain_name.upper().startswith("SKIGW") else "MGCP"
    # Set both `mac` and `domain` for SCCP so JS callers that read either get
    # a usable identifier. The MAC reconstructs the chassis_mac from the
    # SKIGW name (which has only the last 10 hex chars).
    mac_field = None
    if domain_name.upper().startswith("SKIGW"):
        mac_field = _normalize_mac(domain_name[5:])
    return GatewayLookupResponse(
        kind=kind,
        domain=domain_name,
        mac=mac_field,
        product=gw.get("product") or None,
        capacity=capacity or None,
        units=gw.get("units"),
        ports=ports,
    )


# ---------------------------------------------------------------------------
# Per-port migrate / rollback workers
# ---------------------------------------------------------------------------

_SIP_DEFAULT_BUTTON_TEMPLATE      = "Standard SIP Analog"
# Canonical defaults captured from a working manually-created VG410 SIP analog
# endpoint (ANEFBEEF9993080) on a real CUCM 15 cluster. These are the cluster's
# default profiles — they exist on every CUCM out of the box.
_SIP_DEFAULT_SECURITY_PROFILE     = "Analog Phone - Standard SIP Non-Secure Profile"
_SIP_DEFAULT_SIP_PROFILE          = "Standard SIP Profile"
_SIP_DEFAULT_COMMON_PHONE_CONFIG  = "Standard Common Phone Profile"
_SIP_DEFAULT_PRESENCE_GROUP       = "Standard Presence group"
_SIP_DEFAULT_DND_OPTION           = "Ringer Off"
_SIP_DEFAULT_MTP_PREFERRED_CODEC  = "711ulaw"

# Canonical SCCP analog endpoint defaults (verified from ANEFBEEF9991400).
_SCCP_DEFAULT_BUTTON_TEMPLATE     = "Standard Analog"
_SCCP_DEFAULT_SECURITY_PROFILE    = "Analog Phone - Standard SCCP Non-Secure Profile"
_SCCP_DEFAULT_COMMON_PHONE_CONFIG = "Standard Common Phone Profile"
_SCCP_DEFAULT_PRESENCE_GROUP      = "Standard Presence group"
_SCCP_DEFAULT_DND_OPTION          = "Ringer Off"
_SCCP_DEFAULT_MTP_PREFERRED_CODEC = "711ulaw"

# Canonical MGCP analog endpoint defaults (verified from AALN/S2/0@mgcp01).
# MGCP endpoints are class=Gateway / protocol=Analog Access in CUCM, so they
# round-trip through addGatewayEndpointAnalogAccess (not addPhone).
_MGCP_DEFAULT_PRODUCT             = "Cisco MGCP FXS Port"
_MGCP_DEFAULT_COMMON_PHONE_CONFIG = "Standard Common Phone Profile"
_MGCP_DEFAULT_PRESENCE_GROUP      = "Standard Presence group"
_MGCP_DEFAULT_DND_OPTION          = "Ringer Off"
_MGCP_DEFAULT_MTP_PREFERRED_CODEC = "711ulaw"


def _build_rollback_phone_info_mgcp(snap: dict, target_name: str) -> dict:
    """
    Build device_info for the MGCP rollback addPhone path. Full source
    passthrough with floor defaults applied only where the source didn't
    have a value. See _build_target_phone_info for the rationale.
    """
    src_info: dict = snap.get("info") or {}
    info: dict = dict(src_info)

    if not (isinstance(info.get("description"), str) and info["description"].strip()):
        info["description"] = target_name

    floors = {
        "commonPhoneConfigName": _MGCP_DEFAULT_COMMON_PHONE_CONFIG,
        "presenceGroupName":     _MGCP_DEFAULT_PRESENCE_GROUP,
        "dndOption":             _MGCP_DEFAULT_DND_OPTION,
        "mtpPreferedCodec":      _MGCP_DEFAULT_MTP_PREFERRED_CODEC,
        "allowCtiControlFlag":   "true",
        "deviceTrustMode":       "Not Trusted",
    }
    for k, v in floors.items():
        cur = info.get(k)
        if not (isinstance(cur, str) and cur.strip()):
            info[k] = v
    return info


def _build_rollback_endpoint_mgcp(snap: dict) -> dict:
    """
    Legacy helper kept for the add_gateway_endpoint_smart fallback path.
    Builds the canonical MGCP endpoint payload from the snapshot.
    """
    src_info: dict = snap.get("info", {}) or {}
    src_lines = snap.get("lines", []) or []
    src_line = src_lines[0] if src_lines else {}

    # Minimal line payload — addGatewayEndpointAnalogAccess only needs the DN
    # and partition. Extra fields like label/display/e164Mask aren't accepted
    # on this API call.
    # CUCM stores E.164 numbers internally as "\+12345"; getPhone returns
    # them with the backslash, but addGatewayEndpointAnalogAccess rejects
    # the escape and wants the plain "+12345" form (unlike addPhone which
    # accepts both).
    raw_pattern = src_line.get("pattern", "")
    pattern = raw_pattern.lstrip("\\") if raw_pattern.startswith("\\") else raw_pattern
    line_payload = {
        "pattern":   pattern,
        "partition": src_line.get("partition", ""),
    }

    # AALN port is 0-based in the device name; portNumber is 1-based on AXL.
    port_number = (snap.get("source_index") or 0) + 1

    # Minimal endpoint payload mirroring the Cisco DevNet zeep sample.
    # Phone-level fields (commonPhoneConfigName, presenceGroupName, dndOption,
    # mtpPreferedCodec) live on the device row but aren't accepted on
    # addGatewayEndpointAnalogAccess; including them triggers the empty-
    # faultstring -1 error CUCM emits for unrecognized endpoint fields.
    endpoint: dict = {
        "index":          snap.get("source_index", 0),
        "name":           src_info.get("name", ""),
        "product":        _MGCP_DEFAULT_PRODUCT,
        "class":          "Gateway",
        "protocol":       "Analog Access",
        "protocolSide":   "User",
        "port_number":    port_number,
        "trunk":          "POTS",
        "trunkDirection": "Bothways",
        "line":           line_payload,
    }

    # Only the two fields the Cisco-published example sets.
    for field in ("devicePoolName", "locationName"):
        v = src_info.get(field)
        if isinstance(v, str) and v.strip():
            endpoint[field] = v
    return endpoint


def _build_rollback_phone_info_sccp(snap: dict, source_name: str) -> dict:
    """
    Build the device_info payload used to re-create the SCCP analog phone on
    rollback. Full source passthrough with floor defaults applied only
    where the source didn't have a value. See _build_target_phone_info
    for the rationale.
    """
    src_info: dict = snap.get("info") or {}
    info: dict = dict(src_info)

    if not (isinstance(info.get("description"), str) and info["description"].strip()):
        info["description"] = source_name

    floors = {
        "commonPhoneConfigName":  _SCCP_DEFAULT_COMMON_PHONE_CONFIG,
        "securityProfileName":    _SCCP_DEFAULT_SECURITY_PROFILE,
        "presenceGroupName":      _SCCP_DEFAULT_PRESENCE_GROUP,
        "dndOption":               _SCCP_DEFAULT_DND_OPTION,
        "mtpPreferedCodec":       _SCCP_DEFAULT_MTP_PREFERRED_CODEC,
        "allowCtiControlFlag":    "true",
    }
    for k, v in floors.items():
        cur = info.get(k)
        if not (isinstance(cur, str) and cur.strip()):
            info[k] = v
    return info


def _resolve_target_name(req: MigratePortRequest) -> str:
    """
    Always derive the target AN name from the gateway domain + slot encoding.
    CUCM enforces the AN<mac10><HHH> format for chassis-bound devices and
    rejects anything else with a misleading "invalid characters" error,
    so the UI's target_port_name field is intentionally ignored.
    """
    return axl.derive_an_endpoint_name(
        req.target_domain, req.target_unit, req.target_subunit, req.target_port_number,
    )


def _build_target_phone_info(snap: dict, req: MigratePortRequest, target_name: str) -> dict:
    """
    Build the device_info dict consumed by add_phone_smart for the SIP target.

    Full passthrough of the source's device_info, with three classes of
    overrides:
      1. Identity: description is rebuilt to the target's name.
      2. SIP-required: securityProfileName + sipProfileName — UI override
         wins, otherwise the canonical SIP analog defaults.
      3. Floor defaults: a handful of fields the SIP target needs that
         aren't always set on every source (devicePoolName, locationName,
         etc.) — applied only when the source didn't have them.

    Identity fields the caller emits explicitly (name, product, model,
    class, protocol, protocolSide, phoneTemplateName) are skipped by
    axl._PHONE_SKIP_TAGS so they don't survive the passthrough.

    add_phone_smart's retry loop drops any device-level field CUCM
    rejects for the SIP analog target, so it's safe to shovel
    everything through here and let the protocol-incompatible fields
    (e.g. softkeyTemplateName for SCCP source) fall out automatically.
    """
    src_info: dict = snap.get("info") or {}
    info: dict = dict(src_info)

    # Identity + SIP-required overrides.
    info["description"]         = target_name
    info["securityProfileName"] = req.target_security_profile or _SIP_DEFAULT_SECURITY_PROFILE
    info["sipProfileName"]      = req.target_sip_profile or _SIP_DEFAULT_SIP_PROFILE

    # Floor defaults — only applied if the source didn't have a value.
    floors = {
        "devicePoolName":         "Default",
        "locationName":           "Hub_None",
        "commonPhoneConfigName":  _SIP_DEFAULT_COMMON_PHONE_CONFIG,
        "presenceGroupName":      _SIP_DEFAULT_PRESENCE_GROUP,
        "dndOption":              _SIP_DEFAULT_DND_OPTION,
        "mtpPreferedCodec":       _SIP_DEFAULT_MTP_PREFERRED_CODEC,
        "allowCtiControlFlag":    "true",
    }
    for k, v in floors.items():
        cur = info.get(k)
        if not (isinstance(cur, str) and cur.strip()):
            info[k] = v

    return info


def _build_target_lines(snap: dict) -> list[dict]:
    """Return the source's lines (already in add_phone_smart's expected shape
    for both SCCP and MGCP snapshots, since both now use getPhone)."""
    return list(snap.get("lines", []) or [])


def _snapshot_source(creds: dict, req: MigratePortRequest) -> dict:
    if req.source_kind.upper() == "SCCP":
        info = axl.get_device(**creds, device_name=req.source_port_name)
        if not info:
            raise LookupError(f"Source device '{req.source_port_name}' not found")
        lines = axl.get_device_lines(**creds, device_name=req.source_port_name)
        # Capture the gateway membership so rollback can re-bind to the
        # same chassis at the same slot/subunit/port. Tolerant of schema
        # mismatch — without membership, rollback falls back to recreating
        # the AN device without a chassis bind (caller will see "phantom"
        # on rollback rather than the migrate flow blocking outright).
        try:
            membership = axl.get_device_gateway_membership(
                **creds, device_name=req.source_port_name,
            )
        except Exception:
            membership = None
        return {
            "kind":  "SCCP",
            "info":  info,
            "lines": lines,
            "source_port_name": req.source_port_name,
            "membership": membership,
        }
    # MGCP analog endpoints are in the device table too — use getPhone for
    # consistency with SCCP/SIP. getGatewayEndpointAnalogAccess uses a
    # different addressing convention than the slot/port shown in the AALN
    # name and would return "not found" for the same physical endpoint.
    info = axl.get_device(**creds, device_name=req.source_port_name)
    if not info:
        raise LookupError(f"MGCP endpoint '{req.source_port_name}' not found")
    lines = axl.get_device_lines(**creds, device_name=req.source_port_name)
    # getPhone returns an empty securityProfileName for MGCP endpoints — pull
    # it from the device row directly so the rollback can supply the same
    # value CUCM auto-assigned originally.
    if not info.get("securityProfileName"):
        sp = axl.get_device_security_profile(
            **creds, device_name=req.source_port_name,
        )
        if sp:
            info["securityProfileName"] = sp
    parsed = axl.parse_aaln_name(req.source_port_name)
    if parsed:
        slot, subunit, port_num, domain = parsed
    else:
        slot = req.source_unit or 0
        subunit = req.source_subunit or 0
        port_num = req.source_index or 0
        domain = req.source_identifier
    try:
        membership = axl.get_device_gateway_membership(
            **creds, device_name=req.source_port_name,
        )
    except Exception:
        membership = None
    return {
        "kind":  "MGCP",
        "info":  info,
        "lines": lines,
        "source_port_name":  req.source_port_name,
        "source_identifier": domain,
        "source_unit":       slot,
        "source_subunit":    subunit,
        "source_index":      port_num,
        "membership":        membership,
    }


_SIP_ENDPOINT_PRODUCT = "Cisco SIP FXS Port"  # typeproduct enum 36759


def _migrate_one(creds: dict, req: MigratePortRequest) -> PortMigrationResult:
    target_name = _resolve_target_name(req)

    # Retry path — caller supplied a snapshot_id from a prior failure.
    # Skip snapshot + deprovision; reuse the saved data for the target add.
    if req.snapshot_id and req.snapshot_id in _snapshots:
        snap = _snapshots[req.snapshot_id]
        snap_id = req.snapshot_id
    else:
        # 1. Snapshot source
        try:
            snap = _snapshot_source(creds, req)
        except Exception as e:
            return PortMigrationResult(
                port_name=req.source_port_name,
                target_port_name=target_name,
                target_port_number=req.target_port_number,
                status="failed",
                error=f"snapshot failed: {e}",
            )

        # 2. Deprovision source — releases the DN.
    #    SCCP: removePhone deletes the AN device row; mgcpdevicemember
    #          cascades; rollback recreates via addPhone + mgcpdevicemember bind.
    #    MGCP: CUCM 15 cannot recreate MGCP analog endpoints once removed,
    #          so we updatePhone to clear the line instead — the device row
    #          stays, the DN is released, and rollback just restores the line.
        try:
            if snap["kind"] == "MGCP":
                axl.update_phone_lines(
                    **creds, device_name=req.source_port_name, lines=[],
                )
            else:
                axl.remove_phone(**creds, device_name=req.source_port_name)
        except Exception as e:
            return PortMigrationResult(
                port_name=req.source_port_name,
                target_port_name=target_name,
                target_port_number=req.target_port_number,
                dn=_extract_dn(snap),
                status="failed",
                error=f"source deprovision failed (no changes made): {e}",
            )

        snap_id = uuid.uuid4().hex
        _snapshots[snap_id] = snap

    dn = _extract_dn(snap)

    # 3. Provision target — SIP analog endpoints are AN-prefix phone devices
    #    with product "Cisco SIP FXS Port" and model "SIP Station".
    try:
        target_info = _build_target_phone_info(snap, req, target_name)
        target_lines = _build_target_lines(snap)
        result = axl.add_phone_smart(
            **creds,
            name=target_name,
            model="SIP Station",
            product=_SIP_ENDPOINT_PRODUCT,
            protocol="SIP",
            phone_template=(req.target_button_template or _SIP_DEFAULT_BUTTON_TEMPLATE),
            device_info=target_info,
            lines=target_lines,
        )
    except Exception as e:
        return PortMigrationResult(
            port_name=req.source_port_name,
            target_port_name=target_name,
            target_port_number=req.target_port_number,
            dn=dn,
            status="failed",
            error=f"target provision failed (DN orphaned, snapshot saved): {e}",
            snapshot_id=snap_id,
        )

    # 4. Bind the new AN device to its parent gateway via mgcpdevicemember.
    #    addPhone alone leaves the device unlinked from the chassis. Without
    #    this row the device appears in the device table but not on the
    #    gateway page — the "phantom" failure mode.
    try:
        axl.bind_phone_to_gateway(
            **creds,
            device_name=target_name,
            gateway_name=req.target_domain,
            slot=req.target_unit,
            subunit=req.target_subunit,
            port_num=req.target_port_number - 1,  # mgcpdevicemember.port is 0-based
        )
    except Exception as e:
        # Bind failed after addPhone succeeded — orphan cleanup.
        try:
            axl.remove_phone(**creds, device_name=target_name)
        except Exception:
            pass
        return PortMigrationResult(
            port_name=req.source_port_name,
            target_port_name=target_name,
            target_port_number=req.target_port_number,
            dn=dn,
            status="failed",
            error=f"target bind to gateway failed (phone removed, DN orphaned): {e}",
            snapshot_id=snap_id,
        )

    return PortMigrationResult(
        port_name=req.source_port_name,
        target_port_name=target_name,
        target_port_number=req.target_port_number,
        dn=dn,
        status="migrated",
        transferred=result.get("transferred", []),
        skipped=result.get("skipped", []),
        snapshot_id=snap_id,
    )


def _extract_dn(snap: dict) -> str:
    if snap["kind"] == "SCCP":
        lines = snap.get("lines", []) or []
        return (lines[0].get("pattern") if lines else "") or ""
    port = (snap.get("info", {}) or {}).get("port", {}) or {}
    lines = port.get("lines", []) or []
    return (lines[0].get("pattern") if lines else "") or ""


def _rollback_one(creds: dict, req: RollbackPortRequest) -> PortMigrationResult:
    snap = _snapshots.get(req.snapshot_id)
    if snap is None:
        return PortMigrationResult(
            port_name="",
            target_port_number=req.target_port_number,
            status="failed",
            error="snapshot expired (process restart); refresh the dashboard and re-migrate manually",
        )

    dn = _extract_dn(snap)
    source_name = snap.get("source_port_name", "") or (
        snap.get("info", {}).get("name", "") if isinstance(snap.get("info"), dict) else ""
    )

    # Resolve the target AN device name — same derivation as on migrate.
    target_name = axl.derive_an_endpoint_name(
        req.target_domain, req.target_unit, req.target_subunit, req.target_port_number,
    )

    # 1. Remove the SIP endpoint (an AN phone) — releases the DN again.
    #    If the endpoint doesn't exist (e.g., migrate failed before addPhone
    #    succeeded), treat as a no-op and proceed straight to source restore.
    try:
        axl.remove_phone(**creds, device_name=target_name)
    except Exception as e:
        if "not found" not in str(e).lower() and "5007" not in str(e):
            return PortMigrationResult(
                port_name=source_name,
                target_port_number=req.target_port_number,
                dn=dn,
                status="failed",
                error=f"could not remove SIP endpoint {target_name}: {e}",
                snapshot_id=req.snapshot_id,
            )

    # 2. Re-provision the original — splat the snapshot's full device_info
    #    through add_phone_smart. Every captured field is preserved; CUCM-
    #    skip tags filter read-only fields and smart-retry drops anything
    #    CUCM doesn't accept on a fresh addPhone.
    try:
        if snap["kind"] == "SCCP":
            src_info = snap.get("info", {}) or {}
            rb_name = src_info.get("name") or snap.get("source_port_name") or source_name
            axl.add_phone_smart(
                **creds,
                name=rb_name,
                model=src_info.get("model") or "Analog Phone",
                product=src_info.get("product") or src_info.get("model") or "Analog Phone",
                protocol=src_info.get("protocol") or "SCCP",
                phone_template=src_info.get("phoneTemplateName") or _SCCP_DEFAULT_BUTTON_TEMPLATE,
                device_info=src_info,
                lines=snap.get("lines", []),
            )
            # Re-bind to source gateway using the captured membership snapshot.
            membership = snap.get("membership")
            if membership:
                try:
                    axl.bind_phone_to_gateway(
                        **creds, device_name=rb_name,
                        gateway_name=membership["gateway_name"],
                        slot=int(membership["slot"]),
                        subunit=int(membership["subunit"]),
                        port_num=int(membership["port"]),
                    )
                except Exception as bind_err:
                    try:
                        axl.remove_phone(**creds, device_name=rb_name)
                    except Exception:
                        pass
                    raise ConnectionError(
                        f"SCCP re-bind to gateway failed (phone removed): {bind_err}"
                    )
        else:  # MGCP — the source device row was never deleted on forward
               # migrate (we cleared its line instead). Rollback just restores
               # the line on the still-existing device via updatePhone, which
               # also re-claims the DN. No mgcpdevicemember work needed
               # because the chassis bind was preserved throughout.
            src_name = snap.get("source_port_name") or source_name
            axl.update_phone_lines(
                **creds, device_name=src_name, lines=snap.get("lines", []),
            )
    except Exception as e:
        return PortMigrationResult(
            port_name=source_name,
            target_port_number=req.target_port_number,
            dn=dn,
            status="orphaned",
            error=f"SIP endpoint removed but source re-add failed: {e}",
            snapshot_id=req.snapshot_id,
        )

    # Success — drop the snapshot since the port is back on the source
    _snapshots.pop(req.snapshot_id, None)
    return PortMigrationResult(
        port_name=source_name,
        target_port_number=req.target_port_number,
        dn=dn,
        status="rolled_back",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/lookup", response_model=GatewayLookupResponse)
async def lookup_gateway(payload: GatewayLookupRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    identifier = payload.identifier.strip()
    if not identifier:
        raise HTTPException(400, "Identifier is required.")

    loop = asyncio.get_event_loop()
    ccm_version = await loop.run_in_executor(
        _executor, lambda: axl.get_ccm_version(**creds)
    )
    version_warning = None
    if _version_below_15(ccm_version):
        version_warning = (
            f"CUCM {ccm_version} is below 15.0 — the SIP analog endpoint "
            f"device class may not be supported on this cluster."
        )

    # If the user gave a MAC, derive the canonical SCCP gateway name and try
    # that first. CUCM stores SCCP analog gateways as SKIGW<last10> records
    # with the same `getGateway` chassis layout (slots + subunit products)
    # used for MGCP/SIP, so we can read FXS card sizes directly instead of
    # asking the user.
    candidates: list[str] = []
    if _looks_like_mac(identifier):
        candidates.append(_sccp_gateway_name_from_mac(identifier))
    else:
        candidates.append(identifier)

    for cand in candidates:
        gw_resp = await loop.run_in_executor(
            _executor, lambda c=cand: _discover_gateway_record(creds, c, "source")
        )
        if gw_resp is not None:
            gw_resp.ccm_version = ccm_version
            gw_resp.version_warning = version_warning
            return gw_resp

    # Legacy SCCP fallback: chassis MAC without a SKIGW gateway record (older
    # CUCM, or VG224 not registered as a gateway). Uses caller-supplied
    # source_card_size since we can't read it from a non-existent record.
    if _looks_like_mac(identifier):
        card_size = payload.source_card_size or 24
        sccp_resp = await loop.run_in_executor(
            _executor, lambda: _discover_sccp(creds, identifier, card_size)
        )
        sccp_resp.ccm_version = ccm_version
        sccp_resp.version_warning = version_warning
        if sccp_resp.ports:
            return sccp_resp

    raise HTTPException(404, f"No gateway found for identifier '{identifier}'.")


@router.post("/create-target", response_model=GatewayLookupResponse)
async def create_target_gateway(payload: CreateSipGatewayRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            _executor,
            lambda: axl.add_gateway(
                **creds, domain_name=payload.domain_name,
                product=payload.product, protocol="SIP",
                description=payload.description or "",
                call_manager_group=payload.call_manager_group,
                units=payload.units,
            ),
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to create SIP gateway: {e}")

    gw_resp = await loop.run_in_executor(
        _executor,
        lambda: _discover_gateway_record(creds, payload.domain_name, "target"),
    )
    if gw_resp is None:
        raise HTTPException(500, "Gateway created but could not be re-read.")
    gw_resp.ccm_version = await loop.run_in_executor(
        _executor, lambda: axl.get_ccm_version(**creds)
    )
    return gw_resp


@router.post("/migrate-port", response_model=PortMigrationResult)
async def migrate_port(payload: MigratePortRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _migrate_one, creds, payload)


@router.post("/rollback-port", response_model=PortMigrationResult)
async def rollback_port(payload: RollbackPortRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _rollback_one, creds, payload)


@router.post("/migrate-all", response_model=list[PortMigrationResult])
async def migrate_all(payload: MigrateBatchRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    if not payload.ports:
        raise HTTPException(400, "No ports to migrate.")
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(_executor, _migrate_one, creds, req)
             for req in payload.ports]
    return await asyncio.gather(*tasks)


@router.post("/rollback-all", response_model=list[PortMigrationResult])
async def rollback_all(payload: RollbackBatchRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    if not payload.ports:
        raise HTTPException(400, "No ports to roll back.")
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(_executor, _rollback_one, creds, req)
             for req in payload.ports]
    return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Backup / restore — defends against the app being force-closed mid-migration.
# Backup serializes the full source state to JSON; restore reads JSON and
# recreates the source-side artifacts the migration may have deprovisioned.
# ---------------------------------------------------------------------------

def _read_full_port(creds: dict, port_name: str) -> dict:
    """Read getPhone + lines + chassis-membership for one port. Returns a
    serializable dict that captures everything the restore path needs."""
    info       = axl.get_device(**creds, device_name=port_name)
    lines      = axl.get_device_lines(**creds, device_name=port_name) if info else []
    membership = None
    try:
        membership = axl.get_device_gateway_membership(
            **creds, device_name=port_name,
        )
    except Exception:
        membership = None
    return {"name": port_name, "info": info, "lines": lines,
            "membership": membership}


@router.post("/backup")
async def backup_gateway(payload: GatewayLookupRequest):
    """Download a JSON snapshot of the gateway's full source state. Take this
    before starting a migration so that an app crash, browser close, or any
    other interruption can be undone by uploading the file to /restore."""
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    loop = asyncio.get_event_loop()
    # Reuse lookup so the source list is identical to what the user sees in
    # the dashboard. Try SKIGW first when given a MAC, same fallback chain.
    identifier = payload.identifier.strip()
    candidates: list[str] = []
    if _looks_like_mac(identifier):
        candidates.append(_sccp_gateway_name_from_mac(identifier))
    else:
        candidates.append(identifier)

    gw_resp: GatewayLookupResponse | None = None
    for cand in candidates:
        gw_resp = await loop.run_in_executor(
            _executor, lambda c=cand: _discover_gateway_record(creds, c, "source")
        )
        if gw_resp is not None:
            break
    if gw_resp is None and _looks_like_mac(identifier):
        sccp_resp = await loop.run_in_executor(
            _executor, lambda: _discover_sccp(creds, identifier)
        )
        if sccp_resp and sccp_resp.ports:
            gw_resp = sccp_resp
    if gw_resp is None:
        raise HTTPException(404, f"No gateway found for '{identifier}'.")

    # Pull full per-port data in parallel via the existing executor.
    port_tasks = [
        loop.run_in_executor(_executor, _read_full_port, creds, p.name)
        for p in gw_resp.ports
    ]
    full_ports = await asyncio.gather(*port_tasks)

    # Attach the chassis-position metadata from the lookup so restore knows
    # where each port physically lives without re-deriving from device names.
    for pr, fp in zip(gw_resp.ports, full_ports):
        fp["unit"]         = pr.unit
        fp["subunit"]      = pr.subunit
        fp["index"]        = pr.index
        fp["chassis_port"] = pr.chassis_port

    # Re-fetch the raw gateway record so backup can capture the description
    # and CallManagerGroup needed to recreate the chassis if the user deletes
    # it before restoring. Skipped for legacy SCCP gateways that have no
    # SKIGW record at all (gw_resp.domain is unset).
    gw_extra: dict = {}
    if gw_resp.domain:
        gw_extra = await loop.run_in_executor(
            _executor, lambda: axl.get_gateway(**creds, domain_name=gw_resp.domain)
        ) or {}

    body = {
        "format_version": 1,
        "created_at":     datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "cluster_id":     payload.cluster_id,
        "source": {
            "kind":               gw_resp.kind,
            "domain":             gw_resp.domain,
            "mac":                gw_resp.mac,
            "product":            gw_resp.product,
            "units":              gw_resp.units,
            "description":        gw_extra.get("description") or "",
            "call_manager_group": gw_extra.get("callManagerGroupName") or "Default",
        },
        "ports": full_ports,
    }
    fname_base = (gw_resp.domain or gw_resp.mac or "gateway").replace("/", "_")
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return JSONResponse(
        content=body,
        headers={
            "Content-Disposition":
                f'attachment; filename="gateway-backup-{fname_base}-{stamp}.json"',
        },
    )


def _restore_one(creds: dict, port: dict) -> RestorePortResult:
    """Restore a single port from a backup record.

    Three cases:
      * Device exists with lines populated → nothing to do (skip).
      * Device exists with empty/no lines → MGCP forward case, updatePhone
        to restore the captured line list.
      * Device doesn't exist → SCCP forward case, addPhone + chassis bind.
    """
    name = port.get("name") or ""
    info = port.get("info") or {}
    lines = port.get("lines") or []
    membership = port.get("membership") or {}

    if not name:
        return RestorePortResult(name="", status="failed",
                                  error="backup row missing 'name'")

    existing = axl.get_device(**creds, device_name=name)
    if existing:
        cur_lines = axl.get_device_lines(**creds, device_name=name)
        if any(ln.get("pattern") for ln in cur_lines):
            return RestorePortResult(name=name, status="exists",
                                      action="device intact, no action needed")
        # MGCP forward migrated path — restore the captured lines.
        try:
            axl.update_phone_lines(**creds, device_name=name, lines=lines)
        except Exception as e:
            return RestorePortResult(name=name, status="failed",
                                      error=f"updatePhone failed: {e}")
        return RestorePortResult(name=name, status="restored",
                                  action="lines restored")

    # SCCP forward migrated path — addPhone + chassis bind.
    try:
        axl.add_phone_smart(
            **creds,
            name=name,
            model=info.get("model") or "Analog Phone",
            product=info.get("product") or "Analog Phone",
            protocol=info.get("protocol") or "SCCP",
            phone_template=info.get("phoneTemplateName") or "Standard Analog",
            device_info=info,
            lines=lines,
        )
    except Exception as e:
        return RestorePortResult(name=name, status="failed",
                                  error=f"addPhone failed: {e}")
    if membership and membership.get("gateway_name"):
        try:
            axl.bind_phone_to_gateway(
                **creds,
                device_name=name,
                gateway_name=membership["gateway_name"],
                slot=int(membership.get("slot") or 0),
                subunit=int(membership.get("subunit") or 0),
                port_num=int(membership.get("port") or 0),
            )
        except Exception as e:
            return RestorePortResult(name=name, status="failed",
                                      error=f"chassis bind failed (phone "
                                            f"created but not on chassis): {e}")
    return RestorePortResult(name=name, status="restored",
                              action="device recreated + bound")


def _ensure_chassis(creds: dict, source: dict) -> str | None:
    """
    Make sure the source gateway record (mgcp table row) exists before any
    per-port restore runs. bind_phone_to_gateway resolves the chassis by
    domainName, so a deleted gateway means every chassis bind fails.

    Returns None on success / no-op. Returns an error string if recreate
    fails (caller surfaces this as a top-level restore error so the user
    isn't left guessing why all the per-port binds errored out).
    """
    domain = source.get("domain")
    if not domain:
        # Legacy SCCP fallback path — no gateway record was ever expected,
        # so chassis bind is also unused and we have nothing to recreate.
        return None
    try:
        existing = axl.get_gateway(**creds, domain_name=domain)
    except Exception:
        existing = None
    if existing:
        return None

    product = (source.get("product") or "").strip()
    # CUCM's addGateway expects the bare model name as enrolled in
    # TypeProduct (e.g. "VG224"), NOT the marketing-prefixed form
    # ("Cisco VG224"). Different lookup paths return one or the other, so
    # strip any "Cisco " prefix defensively before submitting.
    if product.lower().startswith("cisco "):
        product = product[6:].strip()
    if not product:
        return "backup is missing source.product — cannot recreate chassis"

    try:
        axl.add_gateway(
            **creds,
            domain_name=domain,
            product=product,
            protocol=source.get("kind") or "SCCP",
            description=source.get("description") or "",
            call_manager_group=source.get("call_manager_group") or "Default",
            units=source.get("units") or [],
        )
    except Exception as e:
        return f"addGateway({domain}) failed: {e}"
    return None


@router.post("/restore", response_model=list[RestorePortResult])
async def restore_gateway(payload: RestoreBackupRequest):
    """Apply a previously-downloaded /backup file. Only touches source-side
    artifacts — SIP target devices created by intervening migrations stay
    put (delete them separately if needed). If the gateway chassis itself
    was deleted, it's recreated first using the captured units/product."""
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    backup = payload.backup or {}
    if backup.get("format_version") != 1:
        raise HTTPException(400, "Unsupported or missing backup format_version.")
    ports = backup.get("ports") or []
    if not ports:
        raise HTTPException(400, "Backup contains no ports.")

    loop = asyncio.get_event_loop()
    chassis_err = await loop.run_in_executor(
        _executor, _ensure_chassis, creds, backup.get("source") or {}
    )
    if chassis_err:
        raise HTTPException(500, f"chassis restore failed: {chassis_err}")

    tasks = [loop.run_in_executor(_executor, _restore_one, creds, p)
             for p in ports]
    return await asyncio.gather(*tasks)


@router.post("/debug-probe-gateway")
def debug_probe_gateway(payload: dict):
    """
    Try to addGateway with the supplied product/units config, immediately
    getGateway to see what CUCM stored, then removeGateway to clean up.

    Returns one of:
      {"status": "success", "units": [...]}     — CUCM accepted the config;
                                                   the units array shows what
                                                   it actually persisted
      {"status": "create_failed", "error": ...} — addGateway rejected, never
                                                   created (no cleanup needed)
      {"status": "cleanup_failed", "error": ..., "units": [...]}
                                                — created OK, getGateway ran,
                                                   but removeGateway errored;
                                                   may need GUI cleanup

    Used to probe SIP_GATEWAY_CHASSIS variants for VG420/VG450 without
    burning manual GUI cycles. Run from the browser console.
    """
    cluster_id = payload.get("cluster_id")
    domain     = payload.get("domain_name", "")
    product    = payload.get("product", "")
    units      = payload.get("units", [])
    cm_group   = payload.get("call_manager_group", "Default")

    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    try:
        axl.add_gateway(
            **creds, domain_name=domain, product=product, protocol="SIP",
            description="probe", call_manager_group=cm_group, units=units,
        )
    except Exception as e:
        return {"status": "create_failed", "error": str(e)}

    try:
        gw = axl.get_gateway(**creds, domain_name=domain)
    except Exception:
        gw = None

    try:
        axl.remove_gateway(**creds, domain_name=domain)
    except Exception as e:
        return {"status": "cleanup_failed", "error": str(e),
                "units": gw.get("units") if gw else None}

    return {"status": "success",
            "units":  gw.get("units") if gw else None}


@router.get("/debug-getphone")
def debug_getphone(cluster_id: int, device_name: str):
    """
    Direct getPhone + getLines without device-name normalization. Used to
    capture canonical shapes for case-sensitive device names like the AALN
    MGCP naming pattern.
    """
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    info = axl.get_device(**creds, device_name=device_name)
    if not info:
        raise HTTPException(404, f"Device '{device_name}' not found.")
    lines = axl.get_device_lines(**creds, device_name=device_name)
    return {"device": info, "lines": lines}


@router.get("/debug-sql")
def debug_sql(cluster_id: int, sql: str):
    """
    Diagnostic: run a read-only SQL query and return the rows. Rejects anything
    that isn't a plain SELECT to keep this safe-ish for casual use.
    AXL errors are surfaced as JSON rather than letting FastAPI emit a plain
    "Internal Server Error" page.
    """
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    stripped = sql.strip().lstrip("(").lstrip()
    if not stripped.upper().startswith("SELECT"):
        raise HTTPException(400, "Only SELECT queries are allowed.")
    try:
        return {"rows": axl.raw_query(**creds, sql=sql)}
    except Exception as e:
        return {"rows": [], "error": str(e)}


@router.get("/debug-typeproduct")
def debug_typeproduct(cluster_id: int, like: str = "VG"):
    """
    Diagnostic: list typeproduct rows whose name contains `like`. Used to
    discover the canonical CUCM product strings on a real cluster.
    Example: /api/gateway-migration/debug-typeproduct?cluster_id=1&like=VG4
    """
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    safe = like.replace("'", "''")
    sql = f"""
        SELECT enum, name
        FROM typeproduct
        WHERE name LIKE '%{safe}%'
        ORDER BY name
    """
    return {"rows": axl.raw_query(**creds, sql=sql)}


@router.get("/sip-options")
def sip_options(cluster_id: int):
    """
    Return the valid choices for the three SIP-target override fields on
    this cluster, so the UI can render dropdowns instead of free-text inputs.
    Each fetch is isolated — one failure doesn't blank the whole response.
    """
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    def safe(fn):
        try:
            return fn()
        except Exception:
            return []

    return {
        "button_templates":  safe(lambda: axl.get_button_templates(**creds, model_name="SIP Station")),
        "security_profiles": safe(lambda: axl.get_phone_security_profiles(**creds, model_name="SIP Station")),
        "sip_profiles":      safe(lambda: axl.get_sip_profiles(**creds)),
        "defaults": {
            "button_template":  _SIP_DEFAULT_BUTTON_TEMPLATE,
            "security_profile": _SIP_DEFAULT_SECURITY_PROFILE,
            "sip_profile":      _SIP_DEFAULT_SIP_PROFILE,
        },
    }


@router.get("/products")
def list_target_products():
    """Return the catalogue of supported SIP analog target gateways + chassis layouts."""
    return {
        "products": [
            {"name": name, "capacity": data["capacity"], "variants": data["variants"]}
            for name, data in axl.SIP_GATEWAY_CHASSIS.items()
        ]
    }
