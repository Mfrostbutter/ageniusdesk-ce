"""Minimal MQTT 3.1.1 publisher and a Home Assistant discovery bridge.

Publish-only, QoS 0, retained state + discovery; removed metrics get their
retained discovery config cleared. Blocking sockets: call from a thread.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import struct
import threading
import time
from typing import Any, Optional, Sequence

from backend.modules.llm_cost.models import Snapshot

logger = logging.getLogger(__name__)

CONNECT, CONNACK, PUBLISH, PINGREQ, DISCONNECT = 1, 2, 3, 12, 14
KEEPALIVE_SEC = 60


def encode_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            break
    return bytes(out)


def encode_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    return struct.pack("!H", len(raw)) + raw


def publish_packet(topic: str, payload: str, retain: bool = False) -> bytes:
    body = encode_string(topic) + payload.encode("utf-8")
    return bytes([(PUBLISH << 4) | (0x01 if retain else 0x00)]) + encode_length(len(body)) + body


def connect_packet(client_id: str, username: Optional[str] = None, password: Optional[str] = None) -> bytes:
    flags = 0x02
    payload = encode_string(client_id)
    if username:
        flags |= 0x80
        payload += encode_string(username)
        if password:
            flags |= 0x40
            payload += encode_string(password)
    variable = encode_string("MQTT") + bytes([4, flags]) + struct.pack("!H", KEEPALIVE_SEC)
    return bytes([CONNECT << 4]) + encode_length(len(variable + payload)) + variable + payload


class MqttClient:
    """Just enough MQTT to publish. Reconnects on demand."""

    def __init__(self, host: str, port: int = 1883, username: Optional[str] = None,
                 password: Optional[str] = None, client_id: str = "agd-llm-cost", timeout: float = 10.0) -> None:
        self.host, self.port = host, int(port)
        self.username, self.password = username, password
        self.client_id, self.timeout = client_id, timeout
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._last_activity = 0.0

    def _connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.sendall(connect_packet(self.client_id, self.username, self.password))
        header = sock.recv(4)
        if len(header) < 4 or header[0] >> 4 != CONNACK:
            sock.close()
            raise OSError(f"no CONNACK from {self.host}:{self.port}")
        if header[3] != 0:
            sock.close()
            raise OSError(f"broker refused connection, code {header[3]}")
        self._sock = sock
        self._last_activity = time.time()

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            self._connect()
        elif time.time() - self._last_activity > KEEPALIVE_SEC - 10:
            try:
                self._sock.sendall(bytes([PINGREQ << 4, 0]))
                self._last_activity = time.time()
            except OSError:
                self.close()
                self._connect()
        if self._sock is None:
            raise ConnectionError("mqtt socket unavailable after connect")
        return self._sock

    def publish(self, topic: str, payload: str, retain: bool = False) -> bool:
        packet = publish_packet(topic, payload, retain)
        with self._lock:
            for attempt in (0, 1):
                try:
                    self._ensure().sendall(packet)
                    self._last_activity = time.time()
                    return True
                except OSError as exc:
                    self.close()
                    if attempt:
                        logger.warning("mqtt publish to %s failed: %s", topic, exc)
                        return False
        return False

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.sendall(bytes([DISCONNECT << 4, 0]))
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value).lower()).strip("_") or "x"


def provider_state(snap: Snapshot, now: Optional[float] = None) -> dict[str, Any]:
    """Flat state document for one source."""
    state: dict[str, Any] = {"health": snap.health,
                             "age_sec": max(0, int((time.time() if now is None else now) - snap.observed_at))}
    for q in snap.quotas:
        key = slug(q.scope if q.scope != "model_weekly" else f"{q.scope}_{q.label}")
        state[f"quota_{key}_pct"] = q.pct
        state[f"quota_{key}_resets_in"] = q.resets_in_seconds(now)
    for s in snap.spend:
        state[f"spend_{slug(s.window)}"] = s.amount
    for c in snap.counters:
        state[f"counter_{slug(c.key)}"] = c.value
    return state


class HomeAssistantBridge:
    """Publishes each source as HA sensors via MQTT discovery (one state topic per source)."""

    def __init__(self, client, base_topic: str = "agd_llm_cost", discovery_prefix: str = "homeassistant",
                 config_key: str = "") -> None:
        self.client = client
        self.base = base_topic.strip("/") or "agd_llm_cost"
        self.prefix = discovery_prefix.strip("/") or "homeassistant"
        self.config_key = config_key
        self._announced: dict[str, set] = {}

    @classmethod
    def from_settings(cls, cfg: dict) -> "HomeAssistantBridge":
        from backend.modules.llm_cost.providers.base import resolve_secret

        client = MqttClient(host=str(cfg.get("host")), port=int(cfg.get("port") or 1883),
                            username=resolve_secret(cfg.get("username_ref")) if cfg.get("username_ref") else None,
                            password=resolve_secret(cfg.get("password_ref")) if cfg.get("password_ref") else None,
                            client_id=f"agd-llm-cost-{int(time.time()) % 100000}")
        return cls(client, str(cfg.get("base_topic") or "agd_llm_cost"),
                   str(cfg.get("discovery_prefix") or "homeassistant"), config_key=_cfg_key(cfg))

    def same_config(self, cfg: dict) -> bool:
        return self.config_key == _cfg_key(cfg)

    def close(self) -> None:
        self.client.close()

    def publish_all(self, snapshots: Sequence[Snapshot], overview: dict) -> int:
        published = sum(1 for snap in snapshots if self.publish_source(snap))
        worst = overview.get("worstQuota") or {}
        self.client.publish(f"{self.base}/overview/state", json.dumps({
            "spend_today": overview["spend"]["today"], "spend_mtd": overview["spend"]["mtd"],
            "sources": overview["sourceCount"], "healthy": overview["healthyCount"],
            "alerts": len(overview["alerts"]), "worst_pct": worst.get("pct"),
        }), retain=True)
        self._announce_overview()
        return published

    def publish_source(self, snap: Snapshot) -> bool:
        node = slug(snap.source_id)
        state = provider_state(snap)
        ok = self.client.publish(f"{self.base}/{node}/state", json.dumps(state), retain=True)
        if ok:
            self._announce_source(snap, node, state)
        return ok

    def _device(self, node: str, name: str) -> dict:
        return {"identifiers": [f"agd_llm_cost_{node}"], "name": f"LLM Cost {name}",
                "manufacturer": "AgeniusDesk", "model": "LLM Cost"}

    def _announce_source(self, snap: Snapshot, node: str, state: dict) -> None:
        keys = set(state)
        previous = self._announced.get(node)
        if previous == keys:
            return
        for gone in sorted((previous or set()) - keys):
            self.client.publish(f"{self.prefix}/sensor/{node}/{gone}/config", "", retain=True)
        self._announced[node] = keys
        topic = f"{self.base}/{node}/state"
        device = self._device(node, snap.display_name)
        for key in state:
            if key == "health":
                config = {"icon": "mdi:heart-pulse"}
            elif key.endswith("_pct"):
                config = {"unit_of_measurement": "%", "state_class": "measurement", "icon": "mdi:gauge"}
            elif key.startswith("spend_"):
                config = {"unit_of_measurement": "USD", "device_class": "monetary", "state_class": "total",
                          "icon": "mdi:cash"}
            elif key.endswith("_resets_in") or key == "age_sec":
                config = {"unit_of_measurement": "s", "device_class": "duration", "icon": "mdi:timer-outline"}
            else:
                config = {"state_class": "measurement", "icon": "mdi:counter"}
            unique = f"agd_llm_cost_{node}_{key}"
            self.client.publish(f"{self.prefix}/sensor/{node}/{key}/config", json.dumps({
                "name": f"{snap.display_name} {key.replace('_', ' ')}", "unique_id": unique, "object_id": unique,
                "state_topic": topic, "value_template": "{{ value_json.%s }}" % key,
                "availability_topic": topic,
                "availability_template": "{{ 'online' if value_json.health in ['ok','stale'] else 'offline' }}",
                "device": device, **config,
            }), retain=True)

    def _announce_overview(self) -> None:
        if "overview" in self._announced:
            return
        self._announced["overview"] = set()
        topic = f"{self.base}/overview/state"
        device = self._device("overview", "Overview")
        sensors = (
            ("spend_today", {"unit_of_measurement": "USD", "device_class": "monetary", "state_class": "total"}),
            ("spend_mtd", {"unit_of_measurement": "USD", "device_class": "monetary", "state_class": "total"}),
            ("worst_pct", {"unit_of_measurement": "%", "state_class": "measurement"}),
            ("alerts", {"state_class": "measurement"}),
            ("healthy", {"state_class": "measurement"}),
            ("sources", {"state_class": "measurement"}),
        )
        for key, config in sensors:
            unique = f"agd_llm_cost_overview_{key}"
            self.client.publish(f"{self.prefix}/sensor/agd_llm_cost/{key}/config", json.dumps({
                "name": f"LLM Cost {key.replace('_', ' ')}", "unique_id": unique, "object_id": unique,
                "state_topic": topic, "value_template": "{{ value_json.%s }}" % key, "device": device, **config,
            }), retain=True)


def _cfg_key(cfg: dict) -> str:
    return "|".join(str(cfg.get(k) or "") for k in ("host", "port", "username_ref", "password_ref", "base_topic",
                                                     "discovery_prefix"))


def check_connection(cfg: dict) -> dict:
    """Connect, publish one ping, disconnect. Blocking."""
    bridge = HomeAssistantBridge.from_settings(cfg)
    try:
        ok = bridge.client.publish(f"{bridge.base}/ping", json.dumps({"at": int(time.time())}), retain=False)
        return {"ok": ok}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        bridge.close()
