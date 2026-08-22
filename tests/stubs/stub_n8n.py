"""Stub n8n Public API for AGD walkthroughs and the Phase 1 XSS acceptance gate.

Serves canned /api/v1 responses derived from the recorded execution fixture so
instance-dependent AGD views render real content. Run one process per fake
instance: python stub_n8n.py <port> <instance-label> [--hostile].

--hostile names workflows, nodes, and errors with attribute-breakout and
element-injection payloads so a Playwright pass can assert every sink renders
them as inert text. A dialog, an eval, or markup in the DOM is a failed gate.
"""

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request, Response

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "execution_25173_raw.json").read_text(encoding="utf-8")
)
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5811
LABEL = sys.argv[2] if len(sys.argv) > 2 else "stub-a"
HOSTILE = "--hostile" in sys.argv[3:]

# Attribute-breakout and element-injection payloads. Each is designed to pop a
# dialog if it reaches an unescaped attribute or innerHTML sink.
XSS_ATTR = '" onmouseover="alert(1)" data-x="'
XSS_IMG = '<img src=x onerror=alert(2)>'
XSS_QUOTE = "x'\"><svg onload=alert(3)>"


def _name(base: str) -> str:
    return f'{base} {XSS_IMG} {XSS_ATTR}' if HOSTILE else f"[{LABEL}] {base}"


app = FastAPI(title=f"stub-n8n-{LABEL}{'-hostile' if HOSTILE else ''}")
NOW = datetime.now(timezone.utc)

WF_MAIN = FIXTURE["workflowData"]
if HOSTILE:
    for n in WF_MAIN["nodes"]:
        n["name"] = f'{n["name"]} {XSS_QUOTE}'

WORKFLOWS = [
    {
        "id": WF_MAIN["id"],
        "name": _name("order-sync"),
        "active": True,
        "nodes": WF_MAIN["nodes"],
        "connections": WF_MAIN.get("connections", {}),
        "settings": WF_MAIN.get("settings", {}),
        "createdAt": (NOW - timedelta(days=30)).isoformat(),
        "updatedAt": (NOW - timedelta(days=1)).isoformat(),
        "versionId": WF_MAIN.get("versionId", "v1"),
    },
    {
        "id": "wfEmailStub01",
        "name": _name("email-digest"),
        "active": True,
        "nodes": WF_MAIN["nodes"][:3],
        "connections": {},
        "settings": {},
        "createdAt": (NOW - timedelta(days=10)).isoformat(),
        "updatedAt": (NOW - timedelta(hours=3)).isoformat(),
        "versionId": "v7",
    },
    {
        "id": "wfNoSave0001",
        "name": _name("fire-and-forget"),
        "active": True,
        "nodes": WF_MAIN["nodes"][:2],
        "connections": {},
        "settings": {"saveDataSuccessExecution": "none"},
        "createdAt": (NOW - timedelta(days=5)).isoformat(),
        "updatedAt": (NOW - timedelta(days=5)).isoformat(),
        "versionId": "v2",
    },
]

# 40 executions over the last 36h, newest first; every 7th errors
EXECUTIONS = []
for i in range(40):
    started = NOW - timedelta(minutes=45 * i + 5)
    status = "error" if i % 7 == 3 else "success"
    EXECUTIONS.append({
        "id": str(100 + (39 - i)),
        "workflowId": WF_MAIN["id"] if i % 3 else "wfEmailStub01",
        "status": status,
        "mode": "trigger",
        "finished": True,
        "startedAt": started.isoformat().replace("+00:00", "Z"),
        "stoppedAt": (started + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
    })
EXECUTIONS.sort(key=lambda e: int(e["id"]), reverse=True)
EXEC_BY_ID = {e["id"]: e for e in EXECUTIONS}


def _exec_with_data(meta: dict) -> dict:
    raw = copy.deepcopy(FIXTURE)
    raw["id"] = meta["id"]
    raw["status"] = meta["status"]
    raw["mode"] = meta["mode"]
    raw["startedAt"] = meta["startedAt"]
    raw["stoppedAt"] = meta["stoppedAt"]
    raw["workflowId"] = meta["workflowId"]
    base_ms = int(datetime.fromisoformat(meta["startedAt"].replace("Z", "+00:00")).timestamp() * 1000)
    run_data = raw["data"]["resultData"]["runData"]
    offset = 0
    for _node, runs in run_data.items():
        for r in runs:
            r["startTime"] = base_ms + offset
            offset += int(r.get("executionTime", 1)) + 10
    if meta["status"] == "error":
        rd = raw["data"]["resultData"]
        rd["error"] = {
            "message": f'boom {XSS_IMG}' if HOSTILE else "Request failed with status 500",
            "name": "NodeApiError",
            "node": {"name": list(run_data.keys())[-1]},
        }
    if int(meta["id"]) % 5 == 0:
        last = list(run_data.values())[-1]
        for r in last:
            r["data"] = {"main": [[]]}
    return raw


@app.get("/api/v1/workflows")
async def workflows(limit: int = 100, active: str = ""):
    data = [w for w in WORKFLOWS if active != "true" or w["active"]]
    return {"data": data[:limit], "nextCursor": None}


@app.get("/api/v1/workflows/{wf_id}")
async def workflow(wf_id: str):
    for w in WORKFLOWS:
        if w["id"] == wf_id:
            return w
    return Response(status_code=404, content='{"message":"not found"}')


@app.post("/api/v1/workflows")
async def create_workflow(req: Request):
    body = await req.json()
    body["id"] = f"imported{len(WORKFLOWS):03d}"
    body.setdefault("active", False)
    WORKFLOWS.append(body)
    return body


@app.post("/api/v1/workflows/{wf_id}/activate")
async def activate(wf_id: str):
    for w in WORKFLOWS:
        if w["id"] == wf_id:
            w["active"] = True
            return w
    return Response(status_code=404, content='{"message":"not found"}')


@app.post("/api/v1/workflows/{wf_id}/deactivate")
async def deactivate(wf_id: str):
    for w in WORKFLOWS:
        if w["id"] == wf_id:
            w["active"] = False
            return w
    return Response(status_code=404, content='{"message":"not found"}')


@app.get("/api/v1/executions")
async def executions(limit: int = 100, status: str = "", cursor: str = "", workflowId: str = ""):
    data = EXECUTIONS
    if status:
        data = [e for e in data if e["status"] == status]
    if workflowId:
        data = [e for e in data if e["workflowId"] == workflowId]
    start = int(cursor) if cursor else 0
    page = data[start:start + limit]
    next_cursor = str(start + limit) if start + limit < len(data) else None
    return {"data": page, "nextCursor": next_cursor}


@app.get("/api/v1/executions/{exec_id}")
async def execution(exec_id: str, includeData: bool = False):
    meta = EXEC_BY_ID.get(exec_id)
    if not meta:
        return Response(status_code=404, content='{"message":"not found"}')
    return _exec_with_data(meta) if includeData else meta


@app.get("/api/v1/credentials/schema/{cred_type}")
async def cred_schema(cred_type: str):
    return {"type": "object", "properties": {"apiKey": {"type": "string"}}}


@app.post("/api/v1/credentials")
async def create_credential(req: Request):
    body = await req.json()
    body["id"] = f"cred{PORT}"
    return body


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
