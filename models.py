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
