from pydantic import BaseModel
from typing import Optional


class ClusterCreate(BaseModel):
    name: str
    host: str
    username: str
    password: str
    port: int = 8443
    verify_ssl: bool = False


class ClusterUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    port: Optional[int] = None
    verify_ssl: Optional[bool] = None


class ClusterOut(BaseModel):
    id: int
    name: str
    host: str
    username: str
    port: int
    verify_ssl: bool


class SearchRequest(BaseModel):
    cluster_ids: list[int]
    mode: str               # "description" or "number"
    query: str


class SwitchRequest(BaseModel):
    cluster_id: int
    old_device_name: str    # normalized device name of existing device
    new_device_name: str    # normalized device name for new device
    new_model: str          # model name as it appears in CUCM
    button_template: Optional[str] = None   # required for physical phones
    remove_old: bool = False                # delete old device after switch


class SpeedDialPreviewRequest(BaseModel):
    cluster_id: int
    mac_list: list[str]


class SpeedDialUpdateRequest(BaseModel):
    cluster_id: int
    mac_list: list[str]
    sd_index: Optional[int] = None   # required when source is not set
    source: Optional[str] = None     # find-by-number — overrides sd_index
    dirn: str = ""
    label: str = ""
    remove: bool = False


class BLFUpdateRequest(BaseModel):
    cluster_id: int
    mac_list: list[str]
    blf_index: Optional[int] = None  # required when source is not set
    source: Optional[str] = None     # find-by-number — matches blfDest or blfDirn
    dest: str = ""
    label: str = ""
    dirn_pattern: Optional[str] = None
    dirn_partition: Optional[str] = None
    remove: bool = False


class FindDNRequest(BaseModel):
    cluster_id: int
    number: str


# ---------------------------------------------------------------------------
# Gateway Migration
# ---------------------------------------------------------------------------

class GatewayLookupRequest(BaseModel):
    cluster_id: int
    identifier: str          # 12-hex chassis MAC OR MGCP/SIP domain name
    # SCCP-only hint: ports per physical FXS card on the source chassis. Used
    # to compute chassis_port for multi-card SCCP gateways (VG310/VG320/VG350)
    # where AXL has no central gateway record to read card sizes from.
    # Ignored for MGCP/SIP — those report card sizes in their getGateway
    # response.
    source_card_size: Optional[int] = 24


class PortRecord(BaseModel):
    side: str                # "source" | "target"
    name: str                # AN... or MGCP/SIP endpoint name
    index: int               # 1-based port index *within its subunit*
    unit: Optional[int] = None
    subunit: Optional[int] = None
    # 1-based chassis-global port number. For single-card sources this matches
    # `index`; for multi-card sources (e.g. VG350 with two 72-port FXS modules)
    # this accumulates beginPort from earlier cards so port 1 on the second
    # card resolves to e.g. chassis port 73 — which is what an amphenol swap
    # requires to preserve house cabling.
    chassis_port: Optional[int] = None
    endpoint_index: Optional[int] = None
    dn: Optional[str] = None
    partition: Optional[str] = None
    css: Optional[str] = None
    device_pool: Optional[str] = None
    location: Optional[str] = None
    common_phone_config: Optional[str] = None
    display: Optional[str] = None
    display_ascii: Optional[str] = None
    alerting_name: Optional[str] = None
    e164_mask: Optional[str] = None
    label: Optional[str] = None


class GatewayLookupResponse(BaseModel):
    kind: str                # "SCCP" | "MGCP" | "SIP" | "unknown"
    domain: Optional[str] = None     # for MGCP/SIP
    mac: Optional[str] = None        # for SCCP
    product: Optional[str] = None
    capacity: Optional[int] = None
    units: Optional[list] = None     # chassis layout (target side rendering)
    ports: list[PortRecord]
    ccm_version: str = ""
    version_warning: Optional[str] = None


class CreateSipGatewayRequest(BaseModel):
    cluster_id: int
    domain_name: str
    product: str             # "Cisco VG410" | "Cisco VG420" | "Cisco VG450"
    description: Optional[str] = None
    call_manager_group: str
    units: list[dict]        # chassis skeleton; see axl.SIP_GATEWAY_CHASSIS


class MigratePortRequest(BaseModel):
    cluster_id: int
    source_kind: str         # "SCCP" | "MGCP"
    source_identifier: str   # MAC for SCCP, domain for MGCP
    source_port_name: str
    source_unit: Optional[int] = None
    source_subunit: Optional[int] = None
    source_index: Optional[int] = None
    target_domain: str
    target_unit: int = 0
    target_subunit: int = 0
    target_port_number: int
    target_port_name: Optional[str] = None   # if absent, derive from MAC+slot
    # SIP-endpoint overrides — applied to every port in the batch
    # SIP-target-only overrides — everything else (device pool, location,
    # CSS, common phone profile, presence group, DND, MTP codec) is inherited
    # from the source endpoint automatically.
    target_button_template: Optional[str] = None  # default "Standard SIP Analog"
    target_sip_profile: Optional[str] = None      # default "Standard SIP Profile"
    target_security_profile: Optional[str] = None # default "Analog Phone - Standard SIP Non-Secure Profile"
    # Retry support — if set, the snapshot+deprovision steps are skipped and
    # the existing snapshot is reused. The UI populates this on Retry Migrate
    # after a previous failure left the source already deprovisioned.
    snapshot_id: Optional[str] = None


class RollbackPortRequest(BaseModel):
    cluster_id: int
    target_domain: str
    target_unit: int
    target_subunit: int
    target_port_number: int
    snapshot_id: str


class PortMigrationResult(BaseModel):
    port_name: str
    target_port_name: str = ""
    target_port_number: int
    dn: str = ""
    status: str             # "migrated" | "rolled_back" | "failed" | "orphaned"
    transferred: list[str] = []
    skipped: list[str] = []
    error: Optional[str] = None
    snapshot_id: Optional[str] = None


class RestoreBackupRequest(BaseModel):
    cluster_id: int
    backup: dict             # parsed JSON from a previous /backup download


class RestorePortResult(BaseModel):
    name: str
    status: str              # "restored" | "exists" | "failed"
    action: str = ""         # human-readable description ("device recreated", etc.)
    error: Optional[str] = None


class MigrateBatchRequest(BaseModel):
    cluster_id: int
    ports: list[MigratePortRequest]


class RollbackBatchRequest(BaseModel):
    cluster_id: int
    ports: list[RollbackPortRequest]
