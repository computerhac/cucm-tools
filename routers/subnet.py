import ipaddress
import re

from fastapi import APIRouter, HTTPException

import axl
import database as db

router = APIRouter(prefix="/api/subnet", tags=["subnet"])

_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


@router.get("/search")
def search_subnet(cluster_id: int, query: str, prefix: int):
    """
    Search for phones in the subnet containing the given seed IP.

    query can be:
      - An IPv4 address (used directly as the seed)
      - A MAC address or CUCM device name (resolved to IP via registrationdynamic)

    prefix must be 22-29.
    """
    if prefix < 22 or prefix > 29:
        raise HTTPException(400, "Prefix length must be between 22 and 29.")

    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")

    query = query.strip()
    source_device = None

    if _IP_RE.match(query):
        seed_ip = query
    else:
        device_name = axl.normalize_device_name(query)
        seed_ip = axl.get_device_ip(**creds, device_name=device_name)
        if not seed_ip:
            raise HTTPException(
                404,
                f"No registration record found for '{device_name}'. "
                "The device may be unregistered or the name may be incorrect.",
            )
        source_device = device_name

    # Validate the seed IP
    try:
        ipaddress.ip_address(seed_ip)
    except ValueError:
        raise HTTPException(400, f"'{seed_ip}' is not a valid IP address.")

    network = str(ipaddress.ip_network(f"{seed_ip}/{prefix}", strict=False))

    try:
        phones = axl.search_phones_by_subnet(**creds, seed_ip=seed_ip, prefix_len=prefix)
    except Exception as e:
        raise HTTPException(500, str(e))

    return {
        "network":       network,
        "seed_ip":       seed_ip,
        "source_device": source_device,
        "count":         len(phones),
        "phones":        phones,
    }
