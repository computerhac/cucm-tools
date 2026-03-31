"""
CUCM Tools — FastAPI application entry point.
Start via: python launch.py  (handles password unlock before starting the server)
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import database as db
import axl
from models import ClusterCreate, ClusterUpdate
from routers import route_plan, device, subnet, route_plan_audit

app = FastAPI(title="CUCM Tools")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

app.include_router(route_plan.router)
app.include_router(device.router)
app.include_router(subnet.router)
app.include_router(route_plan_audit.router)

db.init_db()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ---------------------------------------------------------------------------
# Cluster management
# ---------------------------------------------------------------------------

@app.get("/api/clusters")
def list_clusters():
    return [dict(r) for r in db.list_clusters()]


@app.post("/api/clusters", status_code=201)
def add_cluster(payload: ClusterCreate):
    try:
        cluster_id = db.create_cluster(
            name=payload.name, host=payload.host, port=payload.port,
            username=payload.username, password=payload.password,
            verify_ssl=payload.verify_ssl,
        )
        return {"id": cluster_id, "name": payload.name}
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(400, "A cluster with that name already exists.")
        raise HTTPException(500, str(e))


@app.put("/api/clusters/{cluster_id}")
def edit_cluster(cluster_id: int, payload: ClusterUpdate):
    if not db.get_cluster(cluster_id):
        raise HTTPException(404, "Cluster not found.")
    fields = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "No fields to update.")
    db.update_cluster(cluster_id, fields)
    return {"status": "updated"}


@app.delete("/api/clusters/{cluster_id}")
def remove_cluster(cluster_id: int):
    if not db.get_cluster(cluster_id):
        raise HTTPException(404, "Cluster not found.")
    db.delete_cluster(cluster_id)
    return {"status": "deleted"}


@app.post("/api/clusters/{cluster_id}/test")
def test_cluster(cluster_id: int):
    creds = db.get_cluster_credentials(cluster_id)
    if not creds:
        raise HTTPException(404, "Cluster not found.")
    return {"status": axl.test_connection(**creds)}
