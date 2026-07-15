"""Workflow promotion — credential extraction/rewrite and activation guarding.

Covers the pure transforms (no live n8n needed) plus the activation-refusal
contract: a promoted workflow with unmapped credentials must never be
activated, and n8n error bodies must surface, not be reduced to bare codes.
"""

import httpx
import pytest

from backend.modules.n8n_promote.promote import (
    _extract_node_credentials,
    _rewrite_node_credentials,
)


def _wf(nodes):
    return {"name": "wf", "nodes": nodes, "connections": {}}


def _node(name, creds=None):
    n = {"name": name, "type": "n8n-nodes-base.noOp", "parameters": {}}
    if creds is not None:
        n["credentials"] = creds
    return n


class TestExtractNodeCredentials:
    def test_dedupes_by_type_and_id(self):
        wf = _wf([
            _node("a", {"httpHeaderAuth": {"id": "c1", "name": "Hdr"}}),
            _node("b", {"httpHeaderAuth": {"id": "c1", "name": "Hdr"}}),
            _node("c", {"anthropicApi": {"id": "c2", "name": "Anthropic"}}),
        ])
        creds = _extract_node_credentials(wf)
        assert len(creds) == 2
        assert {c["source_id"] for c in creds} == {"c1", "c2"}

    def test_nodes_without_credentials_yield_nothing(self):
        # A node can *require* a credential type without having one configured;
        # such nodes carry no credentials block and extraction must not invent one.
        assert _extract_node_credentials(_wf([_node("bare")])) == []

    def test_non_dict_refs_skipped(self):
        wf = _wf([_node("odd", {"httpHeaderAuth": "not-a-dict"})])
        assert _extract_node_credentials(wf) == []


class TestRewriteNodeCredentials:
    def test_mapped_id_rewritten_with_name(self):
        wf = _wf([_node("a", {"anthropicApi": {"id": "src1", "name": "Src Name"}})])
        out, unmapped = _rewrite_node_credentials(wf, {"src1": "tgt1"}, {"src1": "Target Name"})
        assert unmapped == []
        assert out["nodes"][0]["credentials"]["anthropicApi"] == {"id": "tgt1", "name": "Target Name"}

    def test_mapped_id_keeps_source_name_when_no_override(self):
        wf = _wf([_node("a", {"anthropicApi": {"id": "src1", "name": "Src Name"}})])
        out, _ = _rewrite_node_credentials(wf, {"src1": "tgt1"})
        assert out["nodes"][0]["credentials"]["anthropicApi"]["name"] == "Src Name"

    def test_unmapped_left_in_place_and_reported_once(self):
        ref = {"id": "srcX", "name": "Hdr"}
        wf = _wf([
            _node("a", {"httpHeaderAuth": dict(ref)}),
            _node("b", {"httpHeaderAuth": dict(ref)}),
        ])
        out, unmapped = _rewrite_node_credentials(wf, {})
        assert out["nodes"][0]["credentials"]["httpHeaderAuth"]["id"] == "srcX"
        assert len(unmapped) == 1
        assert unmapped[0]["source_id"] == "srcX"

    def test_input_workflow_not_mutated(self):
        wf = _wf([_node("a", {"anthropicApi": {"id": "src1", "name": "n"}})])
        _rewrite_node_credentials(wf, {"src1": "tgt1"})
        assert wf["nodes"][0]["credentials"]["anthropicApi"]["id"] == "src1"


class TestActivationErrorDetail:
    @pytest.mark.anyio
    async def test_set_workflow_active_surfaces_n8n_message(self, monkeypatch):
        """A 400 on activate must carry n8n's message (missing creds/params),
        not collapse to a bare 'HTTP 400'."""
        from backend.modules.n8n_proxy import client as n8n_client

        msg = 'Cannot publish workflow: Node "X": Missing required credential: openAiApi'

        async def fake_post(self, url, **kwargs):
            return httpx.Response(400, json={"message": msg}, request=httpx.Request("POST", url))

        monkeypatch.setattr(n8n_client, "_base_url", lambda: "http://n8n.test")
        monkeypatch.setattr(n8n_client, "_headers", lambda: {})
        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        out = await n8n_client.set_workflow_active("wf1", True)
        assert out["success"] is False
        assert msg in out["error"]

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"


class TestPromoteRefusesActivationWhenUnmapped:
    @pytest.mark.anyio
    async def test_unmapped_creds_block_activation(self, monkeypatch):
        from backend.modules.n8n_promote import promote as svc

        src = {"id": "s1", "name": "src", "url": "http://s", "api_key": "k"}
        tgt = {"id": "t1", "name": "tgt", "url": "http://t", "api_key": "k"}
        monkeypatch.setattr(svc, "get_instance_by_id", lambda i: {"s1": src, "t1": tgt}.get(i))

        async def probe_ok(inst):
            return True, "ok"

        monkeypatch.setattr(svc, "_probe_instance", probe_ok)

        wf = _wf([_node("a", {"httpHeaderAuth": {"id": "srcX", "name": "Hdr"}})])

        async def fake_export(wf_id):
            return dict(wf)

        async def fake_import(w, name_override="", tags=None):
            return {"success": True, "workflow_id": "new1", "name": name_override}

        activated_calls = []

        async def fake_activate(wf_id, active):
            activated_calls.append(wf_id)
            return {"success": True, "active": active}

        monkeypatch.setattr(svc.client, "export_workflow", fake_export)
        monkeypatch.setattr(svc.client, "import_workflow", fake_import)
        monkeypatch.setattr(svc.client, "set_workflow_active", fake_activate)

        out = await svc.promote("s1", "t1", ["w1"], cred_map={}, activate=True)
        res = out["results"][0]
        assert res["success"] is True
        assert res["activated"] is False
        assert "unmapped" in res["activation_error"].lower() or "Not activated" in res["activation_error"]
        assert activated_calls == []  # activation never attempted

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"
