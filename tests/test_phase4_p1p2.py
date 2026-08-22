"""Phase 4 P1/P2 regressions (bug-hunt remediation 2026-08-16).

BUG-005: trace_id_for_execution must scope by instance; n8n execution ids are
sequential ints per instance and collide across the fleet.
"""

import json

import pytest

from backend.modules.observability import health, storage


def _span_row(trace_id: str, span_id: str, instance_id: str, execution_id: str, start_ns: int) -> dict:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_id": "",
        "instance_id": instance_id,
        "workflow_id": "wf-1",
        "workflow_name": "WF",
        "execution_id": execution_id,
        "name": "workflow.execute",
        "kind": 1,
        "start_ns": start_ns,
        "end_ns": start_ns + 1_000_000,
        "status": "OK",
        "attributes_json": "{}",
        "origin": None,
        "received_at": None,
    }


async def _purge(*trace_ids: str) -> None:
    from backend.database import get_db

    db = await get_db()
    for tid in trace_ids:
        await db.execute("DELETE FROM otel_spans WHERE trace_id = ?", (tid,))
    await db.commit()


@pytest.mark.asyncio
async def test_trace_lookup_is_instance_scoped(client):
    """Two instances share execution id '123'; each lookup returns its own trace,
    and an instance with no trace for that id gets '' even though another has one."""
    t_a, t_b = "p4-bug5-a" * 2, "p4-bug5-b" * 2
    await _purge(t_a, t_b)
    try:
        # B written LAST: the unscoped bug returned the most recent writer.
        await storage.insert_spans([_span_row(t_a, "a1a1a1a1", "inst-a", "123", 1_000)])
        await storage.insert_spans([_span_row(t_b, "b1b1b1b1", "inst-b", "123", 2_000)])
        assert await storage.trace_id_for_execution("123", "inst-a") == t_a
        assert await storage.trace_id_for_execution("123", "inst-b") == t_b
        assert await storage.trace_id_for_execution("123", "inst-c") == ""
    finally:
        await _purge(t_a, t_b)


# ── BUG-006: manual export paginates past 250 workflows ──────────────────────


@pytest.mark.asyncio
async def test_export_all_workflows_paginates(monkeypatch):
    from backend.modules.n8n_proxy import client as n8n_client

    pages = {
        "": {"data": [{"id": str(i)} for i in range(250)], "nextCursor": "c2"},
        "c2": {"data": [{"id": str(i)} for i in range(250, 300)], "nextCursor": ""},
    }
    seen: list[str] = []

    async def fake_get(path, params=None):
        cursor = (params or {}).get("cursor", "")
        seen.append(cursor)
        return pages[cursor]

    monkeypatch.setattr(n8n_client, "_get", fake_get)
    workflows = await n8n_client.export_all_workflows()
    assert len(workflows) == 300
    assert seen == ["", "c2"]


# ── BUG-013: promote mirror reuse is validated, not blind ────────────────────


class _PostCapture:
    def __init__(self, calls: list, *a, **k):
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self._calls.append(url)

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"id": "fresh-cred", "name": "Fresh"}

        return R()


@pytest.mark.asyncio
async def test_provision_checks_gate_reuse(monkeypatch):
    """A prior mirror must not bypass the scope/host checks."""
    from backend.modules.n8n_promote import promote as svc

    monkeypatch.setattr(svc, "_load_mirrors",
                        lambda: {"t1": {"KEY": {"credential_id": "old",
                                                "credential_type": "httpHeaderAuth"}}})
    monkeypatch.setattr(svc, "_resolve_instance_creds", lambda inst: ("http://localhost:5678", "k"))

    def deny(*a):
        raise ValueError("secret is not scoped to this instance")

    monkeypatch.setattr(svc, "_assert_provision_allowed", deny)
    with pytest.raises(ValueError, match="not scoped"):
        await svc._provision_credential({"id": "t1"}, "KEY", "httpHeaderAuth")


@pytest.mark.asyncio
async def test_mirror_type_mismatch_provisions_fresh(monkeypatch):
    """A prior mirror of a DIFFERENT credential type falls through to fresh
    provisioning instead of handing back the wrong-typed credential id."""
    import asyncio

    from backend.modules.n8n_promote import promote as svc

    monkeypatch.setattr(svc, "_load_mirrors",
                        lambda: {"t1": {"KEY": {"credential_id": "old",
                                                "credential_type": "openAiApi"}}})
    monkeypatch.setattr(svc, "_resolve_instance_creds", lambda inst: ("http://localhost:5678", "k"))
    monkeypatch.setattr(svc, "_assert_provision_allowed", lambda *a: None)
    monkeypatch.setattr(svc, "_schemas_for_instance", lambda tid: asyncio.sleep(0, result={}))
    monkeypatch.setattr(svc, "_resolve_secret", lambda name: "value")
    monkeypatch.setattr(svc, "build_credential_payload",
                        lambda *a, **k: {"name": "c", "type": "t", "data": {}})

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(svc, "_record_mirror", _noop)
    posts: list = []
    monkeypatch.setattr(svc.httpx, "AsyncClient", lambda *a, **k: _PostCapture(posts, *a, **k))
    cred_id, _ = await svc._provision_credential({"id": "t1"}, "KEY", "httpHeaderAuth")
    assert cred_id == "fresh-cred"
    assert posts, "type mismatch must provision fresh, not reuse"


# ── BUG-017: agent-fleet single-flight claim is atomic ───────────────────────


def test_claim_is_single_flight():
    from backend.modules.agent_fleet import runner

    runner._live_run_id = None
    try:
        assert runner.claim("run-1") is True
        assert runner.claim("run-2") is False  # double-click loser
        assert runner.is_live() == "run-1"
    finally:
        runner._live_run_id = None


@pytest.mark.asyncio
async def test_resume_not_parked_releases_router_claim():
    """An aborted resume (run no longer parked) must not wedge the slot."""
    from backend.modules.agent_fleet import runner

    runner._live_run_id = None
    try:
        assert runner.claim("run-9")
        await runner.resume("run-9", {"action": "approve"})
        assert runner.is_live() is None
    finally:
        runner._live_run_id = None


# ── BUG-019: notes FTS search never 500s on hostile syntax ───────────────────


@pytest.mark.asyncio
async def test_notes_search_survives_fts_syntax(client):
    from backend.modules.notes import index

    for q in (":)", '"foo', "AND", "a NEAR/ b", '"unbalanced OR ('):
        rows = await index.search(q)
        assert isinstance(rows, list)  # results or empty, never OperationalError


# ── BUG-050: one bad job callable must not kill the scheduler loop ───────────


@pytest.mark.asyncio
async def test_scheduler_survives_raising_job_callables(monkeypatch):
    import backend.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "TICK_SECONDS", 0.01)
    sched = sched_mod.Scheduler()
    ran: list[int] = []

    def boom() -> bool:
        raise RuntimeError("bad config read")

    async def bad_job():
        return {}

    async def good_job():
        ran.append(1)
        return {}

    # "bad" registers first so its raising enabled_fn is evaluated before "good"
    # every tick; the loop must keep going and still fire "good".
    sched.register("bad", bad_job, interval_fn=lambda: 0.01, enabled_fn=boom)
    sched.register("good", good_job, interval_fn=lambda: 0.01, enabled_fn=lambda: True)
    sched.start()
    try:
        import asyncio

        await asyncio.sleep(0.2)
    finally:
        await sched.stop()
    assert ran, "healthy job starved: the raising sibling killed the tick loop"


@pytest.mark.asyncio
async def test_fire_reschedules_despite_raising_interval_fn():
    import backend.scheduler as sched_mod

    async def job():
        return {}

    def boom() -> float:
        raise RuntimeError("bad interval read")

    j = sched_mod.Job(id="j", func=job, interval_fn=boom, enabled_fn=lambda: True)
    sched = sched_mod.Scheduler()
    await sched._fire(j)  # must not raise
    assert j.last_status == "ok"
    assert j.next_run is not None


# ── BUG-004: health enrichment must survive a failed run-data fetch ──────────


@pytest.mark.asyncio
async def test_health_enrich_survives_raising_fetch(client, monkeypatch):
    """A raising get_execution_raw_by_instance previously hit an unbound `raw`
    at the dead-man's switch, discarding the span-only health updates."""
    tid = "p4-bug4-trace-1"
    await _purge(tid)
    root = _span_row(tid, "r0r0r0r0", "inst-a", "777", 1_000)
    root["attributes_json"] = json.dumps({"n8n.execution.status": "success"})
    node = _span_row(tid, "n0n0n0n0", "inst-a", "777", 2_000)
    node["parent_id"] = "r0r0r0r0"
    node["name"] = "node.execute"
    node["attributes_json"] = json.dumps({
        "n8n.node.name": "Fetch Orders", "n8n.node.id": "node-1",
        "n8n.node.items.output": 3, "n8n.node.items.input": 3,
    })
    await storage.insert_spans([root, node])

    async def boom(exec_id, instance_id):
        raise RuntimeError("instance unreachable")

    monkeypatch.setattr(health.n8n_client, "get_execution_raw_by_instance", boom)
    try:
        written = await health.enrich_trace_health(tid)
        assert written >= 1
        assert await storage.has_health(tid), \
            "span-only health updates must persist despite the failed fetch"
    finally:
        await _purge(tid)
