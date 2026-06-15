import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from fastapi import APIRouter, HTTPException

import axl
import database as db
from models import (
    SpeedDialPreviewRequest, SpeedDialUpdateRequest,
    BLFUpdateRequest, FindDNRequest,
)

router = APIRouter(prefix="/api/sd-updater", tags=["sd-updater"])

_executor = ThreadPoolExecutor(max_workers=10)


def _preview_one(creds: dict, mac: str) -> dict:
    device_name = axl.normalize_device_name(mac)
    try:
        sds, blfs = axl.get_speed_dials_and_blfs(**creds, device_name=device_name)
        if sds is None:
            return {"mac": mac, "device_name": device_name, "error": "Device not found",
                    "speed_dials": [], "blfs": []}
        return {"mac": mac, "device_name": device_name, "error": None,
                "speed_dials": sds, "blfs": blfs}
    except Exception as e:
        return {"mac": mac, "device_name": device_name, "error": str(e),
                "speed_dials": [], "blfs": []}


def _update_sd_one(creds: dict, mac: str, sd_index: int | None,
                   source: str, dirn: str, label: str) -> dict:
    device_name = axl.normalize_device_name(mac)
    try:
        axl.update_speed_dial(**creds, device_name=device_name,
                               sd_index=sd_index, source=source, dirn=dirn, label=label)
        return {"mac": mac, "device_name": device_name, "status": "success", "error": None}
    except LookupError as e:
        return {"mac": mac, "device_name": device_name, "status": "skipped", "error": str(e)}
    except Exception as e:
        return {"mac": mac, "device_name": device_name, "status": "error", "error": str(e)}


def _update_blf_one(creds: dict, mac: str, blf_index: int | None,
                    source: str, dest: str, label: str,
                    dirn_pattern: str, dirn_partition: str) -> dict:
    device_name = axl.normalize_device_name(mac)
    try:
        axl.update_blf(**creds, device_name=device_name, blf_index=blf_index,
                        source=source, dest=dest, label=label,
                        dirn_pattern=dirn_pattern, dirn_partition=dirn_partition)
        return {"mac": mac, "device_name": device_name, "status": "success", "error": None}
    except LookupError as e:
        return {"mac": mac, "device_name": device_name, "status": "skipped", "error": str(e)}
    except Exception as e:
        return {"mac": mac, "device_name": device_name, "status": "error", "error": str(e)}


@router.post("/preview")
async def preview_speed_dials(payload: SpeedDialPreviewRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    macs = [m.strip() for m in payload.mac_list if m.strip()]
    if not macs:
        raise HTTPException(400, "No MAC addresses provided.")
    loop = asyncio.get_event_loop()
    tasks = [loop.run_in_executor(_executor, _preview_one, creds, mac) for mac in macs]
    return await asyncio.gather(*tasks)


@router.post("/update")
async def update_speed_dials(payload: SpeedDialUpdateRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    macs = [m.strip() for m in payload.mac_list if m.strip()]
    if not macs:
        raise HTTPException(400, "No MAC addresses provided.")
    if payload.source is None and payload.sd_index is None:
        raise HTTPException(400, "Either position or source number must be provided.")
    if not payload.remove and not payload.dirn.strip():
        raise HTTPException(400, "Speed dial number is required.")
    loop = asyncio.get_event_loop()
    dirn   = "" if payload.remove else payload.dirn.strip()
    label  = "" if payload.remove else payload.label.strip()
    source = (payload.source or "").strip()
    tasks = [
        loop.run_in_executor(_executor, _update_sd_one, creds, mac,
                              payload.sd_index, source, dirn, label)
        for mac in macs
    ]
    return await asyncio.gather(*tasks)


@router.post("/update-blf")
async def update_blfs(payload: BLFUpdateRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    macs = [m.strip() for m in payload.mac_list if m.strip()]
    if not macs:
        raise HTTPException(400, "No MAC addresses provided.")
    if payload.source is None and payload.blf_index is None:
        raise HTTPException(400, "Either position or source number must be provided.")
    if not payload.remove and not payload.dest.strip() and not payload.dirn_pattern:
        raise HTTPException(400, "Destination or Directory Number is required.")
    loop = asyncio.get_event_loop()
    if payload.remove:
        dest, label, dirn_pattern, dirn_partition = "", "", "", ""
    else:
        dest           = payload.dest.strip()
        label          = payload.label.strip()
        dirn_pattern   = (payload.dirn_pattern  or "").strip()
        dirn_partition = (payload.dirn_partition or "").strip()
    source = (payload.source or "").strip()
    tasks = [
        loop.run_in_executor(_executor, _update_blf_one, creds, mac,
                              payload.blf_index, source, dest, label,
                              dirn_pattern, dirn_partition)
        for mac in macs
    ]
    return await asyncio.gather(*tasks)


@router.get("/debug-phone")
async def debug_phone(cluster_id: int, device_name: str):
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    loop = asyncio.get_event_loop()
    fn = partial(axl.get_phone_xml_debug, **creds,
                 device_name=axl.normalize_device_name(device_name))
    return await loop.run_in_executor(_executor, fn)


@router.post("/find-dn")
async def find_dn(payload: FindDNRequest):
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    if not payload.number.strip():
        raise HTTPException(400, "Number is required.")
    loop = asyncio.get_event_loop()
    fn = partial(axl.find_dn_partitions, **creds, number=payload.number.strip())
    return await loop.run_in_executor(_executor, fn)
