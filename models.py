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
