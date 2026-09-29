"""LLM Cost: standalone forwarder parsing and the MQTT publisher."""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from backend.modules.llm_cost import mqtt
from backend.modules.llm_cost.models import Counter, Quota, Snapshot, Spend
from backend.modules.llm_cost.providers.push import parse_payload

ROOT = Path(__file__).resolve().parents[1]
FORWARDER = ROOT / "install" / "llm-cost-forward.py"
BACKEND_TAILER = ROOT / "backend" / "modules" / "llm_cost" / "providers" / "claude_code_local.py"


@pytest.fixture(scope="module")
def fwd():
    spec = importlib.util.spec_from_file_location("llm_cost_forward", FORWARDER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["llm_cost_forward"] = module
    spec.loader.exec_module(module)
    return module


def _block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    return text[text.index("# --- vendored tailer: begin ---"):text.index("# --- vendored tailer: end ---")]


def test_vendored_tailer_matches_backend_copy():
    assert _block(FORWARDER) == _block(BACKEND_TAILER)


def test_forwarder_is_stdlib_only():
    text = FORWARDER.read_text(encoding="utf-8")
    assert "from backend" not in text and "import httpx" not in text and "os.uname(" not in text


def test_limit_kinds_map_to_scopes(fwd):
    payload = {"limits": [
        {"kind": "session", "percent": 12.5, "resets_at": "2026-09-25T15:00:00+00:00"},
        {"kind": "weekly_all", "percent": 40},
        {"kind": "weekly_scoped", "percent": 70, "scope": {"model": {"display_name": "Opus"}}},
        {"kind": "mystery", "percent": 1}, {"kind": "session", "percent": "high"}, "junk"]}
    quotas = fwd.limits_to_quotas(payload)
    assert [(q["scope"], q["label"]) for q in quotas] == [
        ("session", "SESSION"), ("weekly", "WEEK ALL"), ("model_weekly", "WEEK OPUS")]
    assert quotas[0]["resetsAt"] == 1790348400
    assert fwd.limits_to_quotas({}) == [] and fwd.limits_to_quotas(None) == []


def test_missing_model_name_still_labels(fwd):
    assert fwd.limits_to_quotas({"limits": [{"kind": "weekly_scoped", "percent": 1}]})[0]["label"] == "WEEK MODEL"


def test_bad_credentials_file_reads_as_none(fwd, tmp_path, monkeypatch):
    monkeypatch.setattr(fwd.sys, "platform", "linux")
    bad = tmp_path / "creds.json"
    bad.write_text("{nope")
    assert fwd.read_oauth_token(bad) is None
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok"}}))
    assert fwd.read_oauth_token(good) == "tok"


def test_plan_quota_cache_spacing_and_bound(fwd):
    calls = []
    results = [[{"scope": "session", "pct": 1}], None, None, None]

    def fetcher():
        calls.append(1)
        return results[len(calls) - 1]

    src = fwd.PlanQuotaSource(fetcher)
    assert src.get(now=0) and len(calls) == 1
    assert src.get(now=100) and len(calls) == 1
    assert src.get(now=301) and len(calls) == 2
    assert src.get(now=2000) is None


def test_host_name_is_cross_platform(fwd, monkeypatch):
    monkeypatch.setattr(fwd.socket, "gethostname", lambda: "mbp.local")
    assert fwd.host_name() == "mbp"
    monkeypatch.setattr(fwd.socket, "gethostname", lambda: "")
    monkeypatch.setattr(fwd.platform, "node", lambda: "DESKTOP-1")
    assert fwd.host_name() == "DESKTOP-1"


def test_payload_round_trips_through_ingest_validation(fwd, tmp_path, monkeypatch):
    from backend import pricing

    monkeypatch.setattr(pricing, "PRICE_BOOK_FILE", tmp_path / "pb.json")
    monkeypatch.setattr(pricing, "_cache", None)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 5))
    (tmp_path / "a.jsonl").write_text(json.dumps({
        "timestamp": ts, "requestId": "r", "sessionId": "s",
        "message": {"id": "m", "model": "claude-haiku-4-5", "usage": {"input_tokens": 1_000_000}}}) + "\n")
    scan = fwd.ClaudeLogTailer(str(tmp_path)).scan(time.time())
    body = fwd.build_payload(scan, "mbp", plan_quotas=[{"scope": "session", "label": "SESSION", "pct": 33}])
    assert "providerId" not in body
    snap = parse_payload(json.loads(json.dumps(body)), "claude-code")
    assert snap.meta["host"] == "mbp" and snap.quotas[0].pct == 33
    assert snap.spend_for("today").amount == pytest.approx(1.0) and snap.spend_for("today").estimated


def test_url_validation_and_token_file(fwd, tmp_path):
    with pytest.raises(ValueError):
        fwd.require_http_url("ftp://x")
    assert fwd.require_http_url("https://agd.test/") == "https://agd.test"
    tok = tmp_path / "token"
    tok.write_text("agdlc_abc\n")

    class A:
        token = ""
        token_file = str(tok)

    assert fwd.resolve_token(A()) == "agdlc_abc"


def test_dry_run_once_prints_payload(fwd, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(fwd.signal, "signal", lambda *a: None)
    monkeypatch.setattr(fwd.logging, "basicConfig", lambda **k: None)
    (tmp_path / "x.jsonl").write_text("")
    assert fwd.main(["--dry-run", "--once", "--no-plan-quotas", "--projects-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert '"counters"' in out


# ── mqtt ───────────────────────────────────────────────────────────────────


def test_remaining_length_encoding():
    assert mqtt.encode_length(0) == b"\x00"
    assert mqtt.encode_length(127) == b"\x7f"
    assert mqtt.encode_length(128) == b"\x80\x01"
    assert mqtt.encode_length(16383) == b"\xff\x7f"
    assert mqtt.encode_length(2097151) == b"\xff\xff\x7f"


def test_string_encoding_is_length_prefixed_utf8():
    assert mqtt.encode_string("é") == b"\x00\x02\xc3\xa9"


def test_publish_packet_retain_flag():
    packet = mqtt.publish_packet("a/b", "{}", retain=True)
    assert packet[0] == 0x31 and packet[1] == len(packet) - 2
    assert mqtt.publish_packet("a/b", "{}")[0] == 0x30


def test_connect_packet_flags():
    assert mqtt.connect_packet("c")[9] == 0x02
    assert mqtt.connect_packet("c", "u", "p")[9] == 0xC2


class _FakeClient:
    def __init__(self):
        self.sent = []

    def publish(self, topic, payload, retain=False):
        self.sent.append((topic, payload, retain))
        return True

    def close(self):
        pass


def _snap(**kw):
    return Snapshot("anthropic", "Anthropic", int(time.time()),
                    quotas=kw.get("quotas", (Quota("weekly", "WEEK", pct=40, resets_at=int(time.time()) + 60),)),
                    spend=(Spend("today", 1.5),), counters=kw.get("counters", (Counter("total_tokens", 9),)))


def test_quota_fields_publish_pct_and_reset():
    state = mqtt.provider_state(_snap())
    assert state["quota_weekly_pct"] == 40 and 0 < state["quota_weekly_resets_in"] <= 60
    assert state["spend_today"] == 1.5 and state["counter_total_tokens"] == 9


def test_unchanged_keys_announce_once_and_removed_metric_clears_config():
    client = _FakeClient()
    bridge = mqtt.HomeAssistantBridge(client)
    bridge.publish_source(_snap())
    configs = [t for t, _, _ in client.sent if t.endswith("/config")]
    assert configs and all(r for _, _, r in client.sent)
    client.sent.clear()
    bridge.publish_source(_snap())
    assert not [t for t, _, _ in client.sent if t.endswith("/config")]
    bridge.publish_source(_snap(counters=()))
    cleared = [(t, p) for t, p, _ in client.sent if t.endswith("counter_total_tokens/config")]
    assert cleared == [("homeassistant/sensor/anthropic/counter_total_tokens/config", "")]


def test_publish_all_writes_overview():
    client = _FakeClient()
    bridge = mqtt.HomeAssistantBridge(client, base_topic="agd")
    overview = {"spend": {"today": 1.0, "mtd": 2.0}, "sourceCount": 1, "healthyCount": 1, "alerts": [],
                "worstQuota": {"pct": 40}}
    assert bridge.publish_all([_snap()], overview) == 1
    state = next(json.loads(p) for t, p, _ in client.sent if t == "agd/overview/state")
    assert state["worst_pct"] == 40 and state["spend_mtd"] == 2.0
