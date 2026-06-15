import asyncio
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, HTTPException

import axl
import database as db
from models import SearchRequest

router = APIRouter(prefix="/api/route-plan", tags=["route-plan"])
executor = ThreadPoolExecutor(max_workers=10)


@router.post("/search")
async def search(payload: SearchRequest):
    if not payload.query.strip():
        raise HTTPException(400, "Search query cannot be empty.")
    if payload.mode not in ("description", "number"):
        raise HTTPException(400, "mode must be 'description' or 'number'.")

    all_clusters = db.list_clusters()
    targets = [r for r in all_clusters if r["id"] in payload.cluster_ids] \
              if payload.cluster_ids else list(all_clusters)

    if not targets:
        raise HTTPException(400, "No clusters selected or configured.")

    loop = asyncio.get_event_loop()

    async def search_one(cluster_row):
        creds = db.get_cluster_credentials(cluster_row["id"])
        cluster_name = cluster_row["name"]
        try:
            rows = await loop.run_in_executor(
                executor,
                lambda c=creds: axl.search(
                    host=c["host"], port=c["port"],
                    username=c["username"], password=c["password"],
                    verify_ssl=c["verify_ssl"],
                    mode=payload.mode, query=payload.query
                )
            )
            return {"cluster": cluster_name, "results": rows, "error": None}
        except Exception as e:
            return {"cluster": cluster_name, "results": [], "error": str(e)}

    results = await asyncio.gather(*[search_one(c) for c in targets])
    return {"results": list(results)}
