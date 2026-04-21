from fastapi import APIRouter, HTTPException

import axl
import database as db
from models import SwitchRequest

router = APIRouter(prefix="/api/device", tags=["device"])


@router.get("/lookup")
def lookup_device(cluster_id: int, device_name: str):
    """Look up a device by name or MAC. Returns device info and line associations."""
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    normalized = axl.normalize_device_name(device_name)
    device = axl.get_device(**creds, device_name=normalized)
    if not device:
        raise HTTPException(404, f"Device '{normalized}' not found on this cluster.")

    lines = axl.get_device_lines(**creds, device_name=normalized)
    return {"device": device, "lines": lines}


@router.get("/models")
def list_models(cluster_id: int):
    """Return all Cisco phone/endpoint models available on this cluster."""
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    models = axl.get_phone_models(**creds)
    return {"models": models}


@router.get("/button-templates")
def list_button_templates(cluster_id: int, model_name: str):
    """Return button templates valid for the given model."""
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    templates = axl.get_button_templates(**creds, model_name=model_name)
    return {"templates": templates}


@router.post("/switch")
def switch_device(payload: SwitchRequest):
    """
    Create a new device copying all settings and line associations from the old one.
    Optionally removes the old device after successful creation.
    """
    creds = db.get_cluster_credentials(payload.cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    # Fetch current device info
    old_device = axl.get_device(**creds, device_name=payload.old_device_name)
    if not old_device:
        raise HTTPException(404, f"Device '{payload.old_device_name}' not found.")

    lines = axl.get_device_lines(**creds, device_name=payload.old_device_name)

    # Determine protocol — Jabber devices are always SIP
    new_prefix = payload.new_device_name[:3].upper()
    if new_prefix in ("CSF", "BOT", "TAB", "TCT"):
        protocol = "SIP"
    else:
        protocol = old_device.get("protocol", "SIP")

    try:
        smart_result = axl.add_phone_smart(
            **creds,
            name=payload.new_device_name,
            model=payload.new_model,
            protocol=protocol,
            phone_template=payload.button_template or "",
            device_info=old_device,
            lines=lines,
        )
    except Exception as e:
        raise HTTPException(500, f"Failed to create new device: {e}")

    if payload.remove_old:
        try:
            axl.remove_phone(**creds, device_name=payload.old_device_name)
        except Exception as e:
            # New device was created — warn but don't fail the whole operation
            return {
                "status": "partial",
                "message": f"New device '{payload.new_device_name}' created, "
                           f"but failed to remove old device: {e}",
                "transferred": smart_result.get("transferred", []),
                "skipped": smart_result.get("skipped", []),
            }

    return {
        "status": "ok",
        "new_device": payload.new_device_name,
        "old_device": payload.old_device_name,
        "removed_old": payload.remove_old,
        "transferred": smart_result.get("transferred", []),
        "skipped": smart_result.get("skipped", []),
    }
