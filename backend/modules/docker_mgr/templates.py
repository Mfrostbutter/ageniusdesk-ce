"""Deployment templates for one-click container provisioning.

Built-in templates are defined in Python with typed builders. Community
templates are JSON files dropped into /app/data/templates/ — they are
loaded fresh on every GET /templates request so new files appear without
a restart.

Community template JSON schema: see /app/data/templates/example.json
(or docs/community-template-schema.md in the source repo).
"""

from __future__ import annotations

import json
import logging
import secrets
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.config import settings

from . import bundle as bundle_mod
from . import template_state

logger = logging.getLogger(__name__)


def _otel_export_env() -> list[str]:
    """OTLP export env so a provisioned n8n self-registers its telemetry with the
    dashboard's embedded receiver — no manual per-instance step.

    Empty (a no-op) when the receiver is off, or when the dashboard's public URL
    is unknown: auto-export needs a container-reachable endpoint, and
    AGD_PUBLIC_URL is that contract. n8n 2.29.7+ emits native OTel
    (workflow.execute / node.execute spans) and appends /v1/traces to the
    endpoint. A loopback public URL is rewritten to host.docker.internal so the
    endpoint resolves from inside the bridged container; a LAN-IP URL is already
    reachable and used as-is.
    """
    if not settings.agd_otel_enabled:
        return []
    base = (settings.agd_public_url or "").strip().rstrip("/")
    if not base:
        logger.warning(
            "OTLP auto-export skipped for new n8n instance: AGD_PUBLIC_URL is unset. "
            "Set it to the dashboard's container-reachable URL to auto-instrument."
        )
        return []
    parts = urlsplit(base)
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        base = urlunsplit(parts._replace(netloc=netloc))
    env = [
        "N8N_OTEL_ENABLED=true",
        f"N8N_OTEL_EXPORTER_OTLP_ENDPOINT={base}/api/otel",
    ]
    if settings.agd_otel_token:
        env.append(f"N8N_OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer {settings.agd_otel_token}")
    return env

COMMUNITY_TEMPLATE_DIR = Path("/app/data/templates")


@dataclass
class TemplateField:
    id: str
    label: str
    type: str                  # text | password | number | select
    default: Any = ""
    placeholder: str = ""
    required: bool = True
    options: list[str] = field(default_factory=list)
    hint: str = ""


@dataclass
class Template:
    id: str
    name: str
    description: str
    image: str
    icon: str
    category: str
    fields: list[TemplateField]
    # callable(fields_dict) -> bundle.BuildResult
    # Legacy single-container builders may still return (config, volumes); the
    # deployer normalises both shapes via bundle.normalise_build_result().
    build: Any
    documentation_url: str = ""
    post_deploy_hooks: list[str] = field(default_factory=list)
    # Set when this template is bundle-shaped (build() returns multiple
    # ContainerSpecs). None for single-container templates. The router uses
    # this to route to deployer.deploy_bundle() vs the legacy deploy() path.
    # See docs/specs/multi-container-templates-2026-05-21.md §1.
    bundle_id: str | None = None
    # Auto-generated secret field names (e.g. ["db_password", "encryption_key"]).
    # Each is minted by bundle.mint_shared_secrets() if the user did not supply
    # a value, persisted under template_state namespace "bundle:<template_id>",
    # and threaded into spec configs via string substitution.
    auto_secrets: list[str] = field(default_factory=list)


def _rand_key(n: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _managed_labels(template_id: str, instance_name: str) -> dict:
    return {
        "ageniusdesk.managed": "true",
        "ageniusdesk.template": template_id,
        "ageniusdesk.instance": instance_name,
    }


# ── n8n ───────────────────────────────────────────────────────────────────────

def _build_n8n(f: dict) -> list[bundle_mod.ContainerSpec]:
    """Build n8n as a two-container bundle: the n8n main container running task
    runners in EXTERNAL mode, plus the `n8nio/runners` sidecar that ships the JS
    and Python runners.

    Why a bundle and not a single container: the stock `n8nio/n8n` image has no
    Python 3, so the in-process Python runner can't start and Python Code nodes
    fail. External mode moves all Code-node execution (JS + Python) into the
    runners sidecar, which is n8n's recommended production posture AND gives
    Python Code nodes a real interpreter. The two share a per-bundle network
    (the sidecar reaches the broker at http://n8n:5679 via the "n8n" alias) and
    a minted auth token.
    """
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-n8n-{instance_name}"
    cfg_volume = f"agd-n8n-{instance_name}-runnercfg"
    # One tag drives BOTH images. n8n requires the runners image version to
    # match the n8n version; using the same string guarantees it (they are
    # published in lockstep on Docker Hub).
    version = str(f.get("n8n_version") or "latest").strip() or "latest"

    # Encryption-key durability:
    # n8n stores credentials encrypted on the data volume with N8N_ENCRYPTION_KEY.
    # If a fresh key is generated on every redeploy, the existing volume's
    # encrypted credentials become unreadable and the container crash-loops with
    # "Mismatching encryption keys". Persist the key per instance outside the
    # volume so redeploys reuse it.
    persisted = template_state.load("n8n", instance_name)
    encryption_key = (
        f.get("encryption_key")
        or persisted.get("encryption_key")
        or _rand_key()
    )
    if persisted.get("encryption_key") != encryption_key:
        template_state.update_field("n8n", instance_name, "encryption_key", encryption_key)

    # Shared secret the runners sidecar uses to authenticate to the n8n broker.
    # Minted + persisted by the bundle deployer via the template's auto_secrets
    # (namespace "bundle:n8n") before build() runs; _rand_key() is only a
    # belt-and-suspenders fallback for a direct unit-test call.
    auth_token = f.get("runners_auth_token") or _rand_key()

    n8n_env = [
        "N8N_BASIC_AUTH_ACTIVE=true",
        f"N8N_BASIC_AUTH_USER={f['username']}",
        f"N8N_BASIC_AUTH_PASSWORD={f['password']}",
        f"N8N_ENCRYPTION_KEY={encryption_key}",
        f"GENERIC_TIMEZONE={f['timezone']}",
        "N8N_PROTOCOL=http",
        "N8N_HOST=0.0.0.0",
        "N8N_PORT=5678",
        "N8N_PUBLIC_API_DISABLED=false",
        # n8n defaults its auth cookie to Secure, which the browser only sends
        # over HTTPS or to localhost. AgeniusDesk deploys n8n for plain-HTTP
        # access on a LAN IP / host.docker.internal, where a Secure cookie makes
        # the sign-in page unreachable ("configured to use a secure cookie").
        # Disable it so the deployed instance is reachable; operators putting
        # n8n behind TLS can flip this back on.
        "N8N_SECURE_COOKIE=false",
        # Task runners in EXTERNAL mode (see the docstring). The broker must
        # listen on 0.0.0.0 so the sidecar container can reach it, not just
        # localhost. N8N_NATIVE_PYTHON_RUNNER turns on the Python runner.
        "N8N_RUNNERS_ENABLED=true",
        "N8N_RUNNERS_MODE=external",
        "N8N_RUNNERS_BROKER_LISTEN_ADDRESS=0.0.0.0",
        f"N8N_RUNNERS_AUTH_TOKEN={auth_token}",
        "N8N_NATIVE_PYTHON_RUNNER=true",
    ]
    if f.get("webhook_url"):
        n8n_env.append(f"WEBHOOK_URL={f['webhook_url'].rstrip('/')}/")
    # Auto-instrument: every AGD-provisioned n8n exports OTLP to the dashboard's
    # embedded receiver so observability (and silent-failure detection) works out
    # of the box. No-op when the receiver is off or the public URL is unset.
    n8n_env.extend(_otel_export_env())

    n8n_spec = bundle_mod.ContainerSpec(
        name="n8n",
        role="primary",
        expose_port=port,
        volumes=[volume_name],
        config={
            "Image": f"n8nio/n8n:{version}",
            "Env": n8n_env,
            "Labels": _managed_labels("n8n", instance_name),
            "HostConfig": {
                "PortBindings": {"5678/tcp": [{"HostPort": str(port)}]},
                "Binds": [f"{volume_name}:/home/node/.n8n"],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
        },
    )

    # The runners launcher hard-pins Python stdlib access to "" in its stock
    # /etc/n8n-task-runners.json (an env-override that beats any container env
    # var), so a plain env var can't open the standard library. We seed a
    # PATCHED copy into a small config volume — derived from the image's OWN
    # stock config at deploy time (sed, with a cp fallback) so it stays correct
    # across runner versions — and point the launcher at it via
    # N8N_RUNNERS_CONFIG_PATH. Result: `import json/datetime/re/hashlib/...`
    # works in Python Code nodes. Third-party (pip) modules still require baking
    # into a custom image, so N8N_RUNNERS_EXTERNAL_ALLOW stays locked.
    seed_cmd = (
        "sed 's/\"N8N_RUNNERS_STDLIB_ALLOW\"[[:space:]]*:[[:space:]]*\"\"/"
        "\"N8N_RUNNERS_STDLIB_ALLOW\": \"*\"/' /etc/n8n-task-runners.json "
        "> /config/n8n-task-runners.json "
        "|| cp /etc/n8n-task-runners.json /config/n8n-task-runners.json"
    )
    runners_spec = bundle_mod.ContainerSpec(
        name="runners",
        role="service",
        depends_on=["n8n"],
        volumes=[cfg_volume],
        init={
            "image": f"n8nio/runners:{version}",
            "binds": [f"{cfg_volume}:/config"],
            "cmd": [seed_cmd],
            # Write as root: the fresh -runnercfg volume is root-owned and the
            # runners image runs as a non-root user (would hit Permission denied).
            "user": "0",
        },
        config={
            "Image": f"n8nio/runners:{version}",
            "Env": [
                # "n8n" resolves to the main container over the bundle network.
                "N8N_RUNNERS_TASK_BROKER_URI=http://n8n:5679",
                f"N8N_RUNNERS_AUTH_TOKEN={auth_token}",
                "N8N_RUNNERS_CONFIG_PATH=/config/n8n-task-runners.json",
            ],
            "Labels": _managed_labels("n8n", instance_name),
            "HostConfig": {
                "Binds": [f"{cfg_volume}:/config"],
                "RestartPolicy": {"Name": "unless-stopped"},
            },
        },
    )
    return [n8n_spec, runners_spec]


N8N = Template(
    id="n8n",
    name="n8n",
    description="Self-hosted workflow automation with basic auth and persistent storage.",
    image="n8nio/n8n:latest",
    icon="⚡",
    category="automation",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="n8n-1", placeholder="n8n-1",
                      hint="Used for the container name and data volume."),
        TemplateField(id="port", label="Host port", type="number",
                      default=5678, placeholder="5678",
                      hint="Port on the host machine. Must not be in use."),
        TemplateField(id="username", label="Admin username", type="text",
                      default="admin", placeholder="admin"),
        TemplateField(id="password", label="Admin password", type="password",
                      default="", placeholder="••••••••",
                      hint="Min 8 characters."),
        TemplateField(id="timezone", label="Timezone", type="text",
                      default="America/New_York", placeholder="America/New_York"),
        TemplateField(id="webhook_url", label="Webhook URL (optional)", type="text",
                      default="", placeholder="https://n8n.example.com", required=False,
                      hint="Public-facing URL for incoming webhooks."),
        TemplateField(id="n8n_version", label="n8n version", type="text",
                      default="latest", placeholder="latest", required=False,
                      hint="Image tag for BOTH n8n and its runners sidecar "
                           "(pinned together so their versions match). "
                           "e.g. 'latest' or '2.25.6'."),
    ],
    build=_build_n8n,
    # Two-container bundle (n8n + runners sidecar). bundle_id being set routes
    # deploys through deploy_bundle(). auto_secrets mints + persists the shared
    # broker auth token before build() runs.
    bundle_id="n8n",
    auto_secrets=["runners_auth_token"],
)


# ── Postgres ──────────────────────────────────────────────────────────────────

def _build_postgres(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-postgres-{instance_name}"

    config = {
        "Image": "postgres:16",
        "Env": [
            f"POSTGRES_DB={f['db_name']}",
            f"POSTGRES_USER={f['username']}",
            f"POSTGRES_PASSWORD={f['password']}",
        ],
        "Labels": _managed_labels("postgres", instance_name),
        "HostConfig": {
            "PortBindings": {"5432/tcp": [{"HostPort": str(port)}]},
            "Binds": [f"{volume_name}:/var/lib/postgresql/data"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


POSTGRES = Template(
    id="postgres",
    name="PostgreSQL",
    description="Production-grade relational database. Persistent volume for data durability.",
    image="postgres:16",
    icon="🐘",
    category="database",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="pg-1", placeholder="pg-1",
                      hint="Used for the container name and data volume."),
        TemplateField(id="port", label="Host port", type="number",
                      default=5432, placeholder="5432"),
        TemplateField(id="db_name", label="Database name", type="text",
                      default="app", placeholder="app"),
        TemplateField(id="username", label="Username", type="text",
                      default="postgres", placeholder="postgres"),
        TemplateField(id="password", label="Password", type="password",
                      default="", placeholder="••••••••"),
    ],
    build=_build_postgres,
)


# ── Redis ─────────────────────────────────────────────────────────────────────

def _build_redis(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-redis-{instance_name}"

    cmd = ["redis-server", "--appendonly", "yes", "--save", "60", "1"]
    if f.get("password"):
        cmd += ["--requirepass", f["password"]]

    config = {
        "Image": "redis:7-alpine",
        "Cmd": cmd,
        "Labels": _managed_labels("redis", instance_name),
        "HostConfig": {
            "PortBindings": {"6379/tcp": [{"HostPort": str(port)}]},
            "Binds": [f"{volume_name}:/data"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


REDIS = Template(
    id="redis",
    name="Redis",
    description="In-memory key-value store. Append-only persistence enabled by default.",
    image="redis:7-alpine",
    icon="🔴",
    category="database",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="redis-1", placeholder="redis-1"),
        TemplateField(id="port", label="Host port", type="number",
                      default=6379, placeholder="6379"),
        TemplateField(id="password", label="Password (optional)", type="password",
                      default="", placeholder="leave blank for no auth", required=False,
                      hint="Recommended for any non-localhost deployment."),
    ],
    build=_build_redis,
)


# ── Qdrant ────────────────────────────────────────────────────────────────────

def _build_qdrant(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-qdrant-{instance_name}"

    config = {
        "Image": "qdrant/qdrant",
        "Labels": _managed_labels("qdrant", instance_name),
        "HostConfig": {
            "PortBindings": {
                "6333/tcp": [{"HostPort": str(port)}],
                "6334/tcp": [{"HostPort": str(port + 1)}],
            },
            "Binds": [f"{volume_name}:/qdrant/storage"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


QDRANT = Template(
    id="qdrant",
    name="Qdrant",
    description="Vector database for AI/ML workloads. REST on the base port, gRPC on +1.",
    image="qdrant/qdrant",
    icon="🔮",
    category="ai",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="qdrant-1", placeholder="qdrant-1"),
        TemplateField(id="port", label="REST port", type="number",
                      default=6333, placeholder="6333",
                      hint="gRPC will be bound to port+1 (e.g. 6334)."),
    ],
    build=_build_qdrant,
)


# ── Ollama ────────────────────────────────────────────────────────────────────

def _build_ollama(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-ollama-{instance_name}"

    config = {
        "Image": "ollama/ollama",
        "Labels": _managed_labels("ollama", instance_name),
        "HostConfig": {
            "PortBindings": {"11434/tcp": [{"HostPort": str(port)}]},
            "Binds": [f"{volume_name}:/root/.ollama"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


OLLAMA = Template(
    id="ollama",
    name="Ollama",
    description="Run large language models locally. Pull models via the API after deploy.",
    image="ollama/ollama",
    icon="🦙",
    category="ai",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="ollama-1", placeholder="ollama-1"),
        TemplateField(id="port", label="Host port", type="number",
                      default=11434, placeholder="11434",
                      hint="After deploy, run: docker exec <name> ollama pull llama3.2"),
    ],
    build=_build_ollama,
)


# ── Flowise ───────────────────────────────────────────────────────────────────

def _build_flowise(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-flowise-{instance_name}"

    config = {
        "Image": "flowiseai/flowise",
        "Env": [
            f"FLOWISE_USERNAME={f['username']}",
            f"FLOWISE_PASSWORD={f['password']}",
            "PORT=3000",
        ],
        "Labels": _managed_labels("flowise", instance_name),
        "HostConfig": {
            "PortBindings": {"3000/tcp": [{"HostPort": str(port)}]},
            "Binds": [f"{volume_name}:/root/.flowise"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


FLOWISE = Template(
    id="flowise",
    name="Flowise",
    description="No-code AI workflow builder. Build LLM chains and agents visually.",
    image="flowiseai/flowise",
    icon="🌊",
    category="automation",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="flowise-1", placeholder="flowise-1"),
        TemplateField(id="port", label="Host port", type="number",
                      default=3001, placeholder="3001"),
        TemplateField(id="username", label="Username", type="text",
                      default="admin", placeholder="admin"),
        TemplateField(id="password", label="Password", type="password",
                      default="", placeholder="••••••••"),
    ],
    build=_build_flowise,
)


# ── MongoDB ───────────────────────────────────────────────────────────────────

def _build_mongodb(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    volume_name = f"agd-mongodb-{instance_name}"

    config = {
        "Image": "mongo:7",
        "Env": [
            f"MONGO_INITDB_ROOT_USERNAME={f['username']}",
            f"MONGO_INITDB_ROOT_PASSWORD={f['password']}",
            f"MONGO_INITDB_DATABASE={f['db_name']}",
        ],
        "Labels": _managed_labels("mongodb", instance_name),
        "HostConfig": {
            "PortBindings": {"27017/tcp": [{"HostPort": str(port)}]},
            "Binds": [f"{volume_name}:/data/db"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


MONGODB = Template(
    id="mongodb",
    name="MongoDB",
    description="Document database. Persistent volume, root user auto-provisioned on first boot.",
    image="mongo:7",
    icon="🍃",
    category="database",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="mongo-1", placeholder="mongo-1",
                      hint="Used for the container name and data volume."),
        TemplateField(id="port", label="Host port", type="number",
                      default=27017, placeholder="27017"),
        TemplateField(id="db_name", label="Initial database", type="text",
                      default="app", placeholder="app",
                      hint="Created on first boot. Additional DBs can be added later."),
        TemplateField(id="username", label="Root username", type="text",
                      default="root", placeholder="root"),
        TemplateField(id="password", label="Root password", type="password",
                      default="", placeholder="••••••••",
                      hint="Used for the root admin connection string."),
    ],
    build=_build_mongodb,
)


# ── MinIO ─────────────────────────────────────────────────────────────────────

def _build_minio(f: dict) -> tuple[dict, list[str]]:
    instance_name = f["instance_name"].strip().replace(" ", "-").lower()
    port = int(f["port"])
    console_port = int(f["console_port"])
    volume_name = f"agd-minio-{instance_name}"

    config = {
        "Image": "minio/minio",
        "Cmd": ["server", "/data", "--console-address", ":9001"],
        "Env": [
            f"MINIO_ROOT_USER={f['root_user']}",
            f"MINIO_ROOT_PASSWORD={f['root_password']}",
        ],
        "Labels": _managed_labels("minio", instance_name),
        "HostConfig": {
            "PortBindings": {
                "9000/tcp": [{"HostPort": str(port)}],
                "9001/tcp": [{"HostPort": str(console_port)}],
            },
            "Binds": [f"{volume_name}:/data"],
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    return config, [volume_name]


MINIO = Template(
    id="minio",
    name="MinIO",
    description="S3-compatible object storage. API on the base port, web console on +1.",
    image="minio/minio",
    icon="🪣",
    category="storage",
    fields=[
        TemplateField(id="instance_name", label="Instance name", type="text",
                      default="minio-1", placeholder="minio-1"),
        TemplateField(id="port", label="API port", type="number",
                      default=9000, placeholder="9000"),
        TemplateField(id="console_port", label="Console port", type="number",
                      default=9001, placeholder="9001"),
        TemplateField(id="root_user", label="Root user", type="text",
                      default="minioadmin", placeholder="minioadmin"),
        TemplateField(id="root_password", label="Root password", type="password",
                      default="", placeholder="••••••••",
                      hint="Min 8 characters."),
    ],
    build=_build_minio,
)


# ── Built-in registry ─────────────────────────────────────────────────────────

TEMPLATES: list[Template] = [N8N, POSTGRES, MONGODB, REDIS, QDRANT, OLLAMA, FLOWISE, MINIO]
TEMPLATES_BY_ID: dict[str, Template] = {t.id: t for t in TEMPLATES}


# ── Community template loader ─────────────────────────────────────────────────


class UnsafeTemplateError(ValueError):
    """A community template declares a host-escaping HostConfig."""


# HostConfig fields that make a container host-root-equivalent. Community
# templates are plain JSON dropped under data/templates/ (not authored through
# an authenticated route), so one file must not be able to declare a privileged
# / host-mounted / host-namespace container. Built-in templates are trusted and
# skip this check. Because field substitution is leaf-only (see _apply_subs),
# these keys can only be set by the template author, so validating the authored
# config at load time is sufficient.
def _assert_safe_community_hostconfig(config: dict, where: str) -> None:
    hc = config.get("HostConfig") if isinstance(config, dict) else None
    if not isinstance(hc, dict):
        return

    def _is_host_or_container(v) -> bool:
        s = str(v or "").lower()
        return s == "host" or s.startswith("container:")

    if hc.get("Privileged"):
        raise UnsafeTemplateError(f"{where}: HostConfig.Privileged is not allowed")
    for key in ("Binds", "Devices", "DeviceRequests", "DeviceCgroupRules",
                "CapAdd", "GroupAdd", "Sysctls", "CgroupParent"):
        if hc.get(key):
            raise UnsafeTemplateError(f"{where}: HostConfig.{key} is not allowed")
    # Bind-type entries in the structured Mounts list are host mounts too.
    for m in hc.get("Mounts") or []:
        if isinstance(m, dict) and str(m.get("Type", "")).lower() == "bind":
            raise UnsafeTemplateError(f"{where}: bind Mounts are not allowed")
    for mode_key in ("PidMode", "NetworkMode", "IpcMode", "UTSMode",
                     "UsernsMode", "CgroupnsMode"):
        if _is_host_or_container(hc.get(mode_key)):
            raise UnsafeTemplateError(f"{where}: HostConfig.{mode_key}={hc[mode_key]!r} is not allowed")
    for opt in hc.get("SecurityOpt") or []:
        if "unconfined" in str(opt).lower() or "disable" in str(opt).lower():
            raise UnsafeTemplateError(f"{where}: SecurityOpt {opt!r} is not allowed")


def _apply_subs(obj, subs: dict[str, str]):
    """Recursively substitute `{key}` placeholders in the STRING LEAVES of an
    already-parsed JSON structure.

    Operating on the parsed object (not the serialized JSON text) is what makes
    this safe: an operator-supplied field value containing quotes or braces can
    only ever land as a plain string value — it can never break out and inject
    new JSON structure (e.g. a `HostConfig.Privileged` / bind mount). Every
    placeholder in a template file is authored inside a JSON string already, so
    leaf-only substitution is behavior-preserving for all valid templates.
    """
    if isinstance(obj, str):
        for key, val in subs.items():
            obj = obj.replace(f"{{{key}}}", val)
        return obj
    if isinstance(obj, list):
        return [_apply_subs(v, subs) for v in obj]
    if isinstance(obj, dict):
        return {k: _apply_subs(v, subs) for k, v in obj.items()}
    return obj


def _build_community(template_def: dict):
    """Return a build function for a JSON-defined community template.

    Two shapes are supported:

    1. Single-container (legacy): top-level `container_config` + `volumes`.
       Returns (config, volume_list) — the original signature.

    2. Bundle (new): top-level `containers: [...]` array. Each entry has
       `name`, `config`, `volumes` (map of key -> volume name template),
       `depends_on`, `role`, `expose_port`. Returns a list[ContainerSpec].

    Field values are substituted into stringified config via {field_id}
    placeholders. The bundle shape adds two new substitutions:
      - `{volume:<key>}`  resolves to the named volume defined on that spec.
      - `{bundle_host:<name>}` resolves to the sibling spec.name (DNS-resolvable
        within the bundle network).
    """
    is_bundle = "containers" in template_def

    if is_bundle:
        return _build_community_bundle(template_def)

    def builder(f: dict) -> tuple[dict, list[str]]:
        instance_name = f.get("instance_name", "instance").strip().replace(" ", "-").lower()
        prefix = template_def.get("volume_prefix", f"agd-{template_def['id']}")
        volume_name = f"{prefix}-{instance_name}"

        subs: dict[str, str] = {
            **{k: str(v) for k, v in f.items()},
            "instance_name": instance_name,
            "volume_name": volume_name,
        }

        config = _apply_subs(template_def.get("container_config", {}), subs)

        volumes_raw: list[str] = template_def.get("volumes", [])
        volumes = []
        for v in volumes_raw:
            for key, val in subs.items():
                v = v.replace(f"{{{key}}}", val)
            volumes.append(v)

        return config, volumes

    return builder


def _build_community_bundle(template_def: dict):
    """Return a build function for a bundle-shaped community template.

    The returned builder yields a list[bundle_mod.ContainerSpec] suitable for
    deployer.deploy_bundle().
    """

    def builder(f: dict) -> list[bundle_mod.ContainerSpec]:
        instance_name = f.get("instance_name", "instance").strip().replace(" ", "-").lower()

        subs: dict[str, str] = {
            **{k: str(v) for k, v in f.items()},
            "instance_name": instance_name,
        }

        specs: list[bundle_mod.ContainerSpec] = []
        for entry in template_def["containers"]:
            spec_name = entry["name"]
            # Resolve {volume:<key>} → fully-qualified volume name.
            volumes_map: dict[str, str] = {}
            for vkey, vtmpl in (entry.get("volumes") or {}).items():
                # Substitute fields into the volume name template.
                resolved = vtmpl
                for k, v in subs.items():
                    resolved = resolved.replace(f"{{{k}}}", v)
                volumes_map[vkey] = resolved

            # Merge subs for this spec: fields + volume:* + bundle_host:* are
            # all resolvable in this spec's config text.
            spec_subs = {**subs}
            for vkey, vname in volumes_map.items():
                spec_subs[f"volume:{vkey}"] = vname
            # Sibling resolution via DNS alias = sibling spec.name on the
            # bundle network.
            for other in template_def["containers"]:
                spec_subs[f"bundle_host:{other['name']}"] = other["name"]

            config = _apply_subs(entry.get("config", {}), spec_subs)

            # expose_port may be a string template like "{port}"; coerce.
            expose_port: int | None = None
            raw_expose = entry.get("expose_port")
            if raw_expose is not None:
                resolved = str(raw_expose)
                for k, v in subs.items():
                    resolved = resolved.replace(f"{{{k}}}", v)
                try:
                    expose_port = int(resolved)
                except (TypeError, ValueError):
                    expose_port = None

            specs.append(bundle_mod.ContainerSpec(
                name=spec_name,
                config=config,
                volumes=list(volumes_map.values()),
                depends_on=list(entry.get("depends_on", [])),
                role=entry.get("role", "service"),
                expose_port=expose_port,
            ))

        return specs

    return builder


def load_community_templates() -> list[Template]:
    """Scan COMMUNITY_TEMPLATE_DIR for *.json files and return Template objects."""
    if not COMMUNITY_TEMPLATE_DIR.exists():
        return []

    # Import here to avoid a circular import at module-level.
    # post_deploy_hooks imports templates transitively via deployer; keeping the
    # import local breaks the cycle.
    from backend.modules.docker_mgr.post_deploy_hooks import (  # noqa: PLC0415
        UnknownHookError,
        validate_hook_names,
    )

    result: list[Template] = []
    for path in sorted(COMMUNITY_TEMPLATE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            fields = [
                TemplateField(
                    id=fd["id"],
                    label=fd.get("label", fd["id"]),
                    type=fd.get("type", "text"),
                    default=fd.get("default", ""),
                    placeholder=fd.get("placeholder", ""),
                    required=fd.get("required", True),
                    options=fd.get("options", []),
                    hint=fd.get("hint", ""),
                )
                for fd in data.get("fields", [])
            ]
            hooks: list[str] = data.get("post_deploy_hooks", [])
            try:
                validate_hook_names(hooks)
            except UnknownHookError as exc:
                logger.warning("Skipping community template %s: %s", path.name, exc)
                continue
            is_bundle = "containers" in data
            bundle_id_marker = data["id"] if is_bundle else None
            auto_secrets = list(data.get("auto_secrets", []) or [])

            # Reject host-escaping HostConfig before the template reaches the UI.
            try:
                if is_bundle:
                    for entry in data.get("containers", []):
                        _assert_safe_community_hostconfig(
                            entry.get("config", {}), f"{path.name}:{entry.get('name', '?')}"
                        )
                else:
                    _assert_safe_community_hostconfig(
                        data.get("container_config", {}), path.name
                    )
            except UnsafeTemplateError as exc:
                logger.warning("Skipping unsafe community template %s: %s", path.name, exc)
                continue

            # Validate bundle shape at load time so a malformed template never
            # reaches the UI. We call the builder with empty fields to materialise
            # the specs and run validate_bundle on them.
            if is_bundle:
                try:
                    probe_fields = {fd.id: fd.default or "x" for fd in fields}
                    probe_specs = _build_community_bundle(data)(probe_fields)
                    bundle_mod.validate_bundle(probe_specs)
                except bundle_mod.BundleError as exc:
                    logger.warning("Skipping bundle template %s: %s", path.name, exc)
                    continue

            result.append(Template(
                id=data["id"],
                name=data["name"],
                description=data.get("description", ""),
                image=data.get("image", ""),
                icon=data.get("icon", "📦"),
                category=data.get("category", "community"),
                fields=fields,
                build=_build_community(data),
                documentation_url=data.get("documentation_url", ""),
                post_deploy_hooks=hooks,
                bundle_id=bundle_id_marker,
                auto_secrets=auto_secrets,
            ))
        except Exception as exc:
            logger.warning("Skipping community template %s: %s", path.name, exc)

    return result


# ── Public API ────────────────────────────────────────────────────────────────

def get(template_id: str) -> Template | None:
    if template_id in TEMPLATES_BY_ID:
        return TEMPLATES_BY_ID[template_id]
    for t in load_community_templates():
        if t.id == template_id:
            return t
    return None


def as_json() -> list[dict]:
    community = load_community_templates()
    community_ids = {t.id for t in community}
    all_templates = TEMPLATES + community
    return [
        {
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "image": t.image,
            "icon": t.icon,
            "category": t.category,
            "community": t.id in community_ids,
            "documentation_url": t.documentation_url,
            "post_deploy_hooks": t.post_deploy_hooks,
            "bundle": t.bundle_id is not None,
            "auto_secrets": t.auto_secrets,
            "fields": [
                {
                    "id": f.id,
                    "label": f.label,
                    "type": f.type,
                    "default": f.default,
                    "placeholder": f.placeholder,
                    "required": f.required,
                    "options": f.options,
                    "hint": f.hint,
                }
                for f in t.fields
            ],
        }
        for t in all_templates
    ]
