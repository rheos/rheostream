"""The flagship deployment overlay (Prompt 5): `deploy/compose.flagship.yaml`.

Seams under test: the parsed overlay file itself (labels, networks, volumes,
secret interpolation) against the routing configuration the anchor's own
``RHEO__routing__*`` values produce, and the `rheo routing hosts` CLI seam
(AC-7) against the same resolved configuration.

No Postgres: this parses a YAML file and calls the same pure routing functions
`tests/test_routing.py` does. The one process spawn (AC-7) never touches the
database either — `rheo routing hosts` prints and exits before any query.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from rheo_core.routing import (
    IDENTITY,
    MCP,
    RoutingConfig,
    RoutingMode,
    SurfaceConfig,
    application_hosts,
    url_for,
)
from rheo_core.settings import resolve
from rheo_recallatron.manifest import MANIFEST as RECALLATRON_MANIFEST

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "compose.flagship.yaml"
ENV_EXAMPLE_PATH = ROOT / "deploy" / ".env.flagship.example"

HOST_RULE_RE = re.compile(r"Host\(`([^`]+)`\)")
LABEL_KEY_RE = re.compile(r"^(traefik\.http\.(?:routers|services|middlewares))\.")


def _load_compose() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def _load_env_example() -> dict[str, str]:
    """A minimal `KEY=value` parse of the committed placeholder env file."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _labels_as_dict(service: dict[str, Any]) -> dict[str, str]:
    labels = service.get("labels", [])
    result: dict[str, str] = {}
    for label in labels:
        key, _, value = label.partition("=")
        assert key not in result, f"duplicate label key: {key}"
        result[key] = value
    return result


def _volume_mounts(service: dict[str, Any]) -> dict[str, str]:
    """``{source: target}`` for this service's short-form volume strings."""
    mounts: dict[str, str] = {}
    for entry in service.get("volumes", []):
        assert isinstance(entry, str), f"expected short-form volume string: {entry!r}"
        parts = entry.split(":")
        assert len(parts) >= 2, f"unparseable volume entry: {entry!r}"
        mounts[parts[0]] = parts[1]
    return mounts


COMPOSE = _load_compose()
SERVICES = COMPOSE["services"]
POSTGRES = SERVICES["postgres"]
CORE = SERVICES["core"]
WORKER = SERVICES["worker"]
WEB = SERVICES["web"]
ANCHOR = COMPOSE["x-rheo-settings"]
ENV_EXAMPLE = _load_env_example()
EXAMPLE_BASE_HOST = ENV_EXAMPLE["RHEO_BASE_HOST"]

RAW_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")

DSN_USER_MATCH = re.match(r"^postgresql://([^:@]+):", ANCHOR["RHEO_CLUSTER_DSN"])
assert DSN_USER_MATCH, (
    f"could not parse a user out of the anchor DSN: {ANCHOR['RHEO_CLUSTER_DSN']!r}"
)
DSN_USER = DSN_USER_MATCH.group(1)


def _recallatron_surface_config() -> SurfaceConfig:
    assert RECALLATRON_MANIFEST.web is not None
    web = RECALLATRON_MANIFEST.web.surface
    return SurfaceConfig(host=web.host, path=web.path)


def _resolved_routing_config(monkeypatch: pytest.MonkeyPatch) -> RoutingConfig:
    monkeypatch.setenv("RHEO__routing__mode", ANCHOR["RHEO__routing__mode"])
    monkeypatch.setenv("RHEO__routing__scheme", ANCHOR["RHEO__routing__scheme"])
    monkeypatch.setenv("RHEO__routing__base_host", EXAMPLE_BASE_HOST)
    return RoutingConfig.from_settings(
        resolve(),
        modules={RECALLATRON_MANIFEST.module_id: _recallatron_surface_config()},
    )


# --- postgres ---------------------------------------------------------------------


def test_postgres_has_no_published_ports_and_the_right_image_and_volume() -> None:
    assert "ports" not in POSTGRES
    assert POSTGRES["image"] == "pgvector/pgvector:pg16"
    assert POSTGRES["environment"]["POSTGRES_USER"] == DSN_USER
    mounts = _volume_mounts(POSTGRES)
    assert mounts.get("pgdata") == "/var/lib/postgresql/data"


def test_no_service_anywhere_publishes_a_host_port() -> None:
    for name, service in SERVICES.items():
        assert "ports" not in service, f"{name} publishes ports"


# --- rheodata volume, shared build block ------------------------------------------


def test_core_and_worker_mount_rheodata_at_the_anchors_data_root() -> None:
    data_root = ANCHOR["RHEO_DATA_ROOT"]
    assert _volume_mounts(CORE).get("rheodata") == data_root
    assert _volume_mounts(WORKER).get("rheodata") == data_root
    assert COMPOSE["volumes"].keys() == {"pgdata", "rheodata"}
    assert "rheodata" not in _volume_mounts(WEB)
    assert "rheodata" not in _volume_mounts(POSTGRES)


def test_workers_build_block_equals_cores() -> None:
    assert WORKER["build"] == CORE["build"]
    assert "image" not in WORKER


# --- routing: the identity path clause, and no bare PathPrefix -------------------


def test_the_auth_router_path_clause_matches_resolved_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_routing_config(monkeypatch)
    identity_prefix = config.surfaces.identity.path
    core_labels = _labels_as_dict(CORE)
    auth_rule = core_labels["traefik.http.routers.rheostream-auth.rule"]
    expected_clause = f"(Path(`{identity_prefix}`) || PathPrefix(`{identity_prefix}/`))"
    assert expected_clause in auth_rule
    bare = f"PathPrefix(`{identity_prefix}`)"
    for service in (CORE, WEB):
        for value in _labels_as_dict(service).values():
            assert bare not in value, f"bare {bare} in {value!r}"


# --- secrets: interpolated, never literal -----------------------------------------


def test_every_secret_bearing_value_is_a_required_interpolation() -> None:
    for var in (
        "RHEO_PG_PASSWORD",
        "RHEO_INTERNAL_SECRET",
        "RHEO_GITHUB_CLIENT_SECRET",
    ):
        assert f"${{{var}:?}}" in RAW_TEXT, f"{var} is not interpolated as ${{{var}:?}}"
        # never the bare, non-required form
        assert f"${{{var}}}" not in RAW_TEXT
    dsn_password = re.search(
        r"^postgresql://[^:@]+:([^@]+)@", ANCHOR["RHEO_CLUSTER_DSN"]
    )
    assert dsn_password is not None
    assert dsn_password.group(1) == "${RHEO_PG_PASSWORD:?}"
    assert "rheo_dev_only" not in RAW_TEXT


def test_core_and_worker_have_identical_rheo_double_underscore_keys() -> None:
    core_keys = {k for k in CORE["environment"] if k.startswith("RHEO__")}
    worker_keys = {k for k in WORKER["environment"] if k.startswith("RHEO__")}
    assert core_keys == worker_keys
    assert core_keys  # not vacuously empty


# --- routing: mode/scheme/base_host, callback and mcp URLs, host-set equality ----


def test_resolved_routing_matches_the_anchors_declared_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_routing_config(monkeypatch)
    assert config.mode is RoutingMode.SUBDOMAIN
    assert config.scheme == "https"
    assert ":" not in config.base_host
    assert (
        url_for(config, IDENTITY, "/callback")
        == f"https://auth.{EXAMPLE_BASE_HOST}/auth/callback"
    )
    assert url_for(config, MCP, "/") == f"https://mcp.{EXAMPLE_BASE_HOST}/"


def test_router_host_set_matches_application_hosts_plus_api_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _resolved_routing_config(monkeypatch)
    expected = application_hosts(config) | {
        f"api.{EXAMPLE_BASE_HOST}",
        f"mcp.{EXAMPLE_BASE_HOST}",
    }
    found: set[str] = set()
    for service in (CORE, WEB):
        for value in _labels_as_dict(service).values():
            found.update(HOST_RULE_RE.findall(value))
    # The labels carry the un-interpolated `${RHEO_BASE_HOST:?}` placeholder
    # (docker compose resolves it at deploy time, not something this parse does);
    # substitute the example file's own value to compare against resolved routing.
    found = {host.replace("${RHEO_BASE_HOST:?}", EXAMPLE_BASE_HOST) for host in found}
    assert found == expected


# --- traefik: only core/web labelled, service/port/network wiring ----------------


def test_only_core_and_web_carry_traefik_labels() -> None:
    for name, service in SERVICES.items():
        labels = _labels_as_dict(service)
        has_traefik = any(key.startswith("traefik.") for key in labels)
        if name in ("core", "web"):
            assert has_traefik, f"{name} should carry traefik labels"
        else:
            assert not has_traefik, f"{name} should carry no traefik labels"


def test_traefik_network_and_service_port_families() -> None:
    core_labels = _labels_as_dict(CORE)
    web_labels = _labels_as_dict(WEB)
    for labels in (core_labels, web_labels):
        assert labels["traefik.enable"] == "true"
        assert labels["traefik.docker.network"] == "${RHEO_TRAEFIK_NETWORK:?}"
    assert (
        core_labels["traefik.http.services.rheostream-core.loadbalancer.server.port"]
        == "8000"
    )
    assert (
        web_labels["traefik.http.services.rheostream-web.loadbalancer.server.port"]
        == "3000"
    )
    # Every router's `.service=` names a service defined on the SAME container.
    defined_services = {
        "core": {
            key.split(".")[3]
            for key in core_labels
            if key.startswith("traefik.http.services.")
        },
        "web": {
            key.split(".")[3]
            for key in web_labels
            if key.startswith("traefik.http.services.")
        },
    }
    for name, labels in (("core", core_labels), ("web", web_labels)):
        for key, value in labels.items():
            if key.startswith("traefik.http.routers.") and key.endswith(".service"):
                assert value in defined_services[name], (
                    f"{key}={value} names a service not defined on {name}"
                )
    for labels in (core_labels, web_labels):
        for value in labels.values():
            assert "8100" not in value, f"a label mentions the internal port: {value}"


def test_every_router_service_and_middleware_name_starts_with_rheostream() -> None:
    for service in (CORE, WEB):
        for key in _labels_as_dict(service):
            match = LABEL_KEY_RE.match(key)
            if match is None:
                continue
            name = key[len(match.group(0)) :].split(".")[0]
            assert name.startswith("rheostream-"), f"{key} names {name!r}"


def test_https_routers_carry_tls_and_http_router_redirects() -> None:
    core_labels = _labels_as_dict(CORE)
    web_labels = _labels_as_dict(WEB)
    all_labels = {**core_labels, **web_labels}
    https_routers = {
        "rheostream-auth",
        "rheostream-api",
        "rheostream-mcp",
        "rheostream-web",
    }
    for router in https_routers:
        assert all_labels[f"traefik.http.routers.{router}.entrypoints"] == "https"
        assert (
            all_labels[f"traefik.http.routers.{router}.tls.certresolver"]
            == "${RHEO_TLS_CERTRESOLVER:?}"
        )
        assert (
            all_labels[f"traefik.http.routers.{router}.tls.domains[0].main"]
            == "*.${RHEO_BASE_HOST:?}"
        )
        assert f"traefik.http.routers.{router}.tls.domains[0].sans" not in all_labels
    # No label may reference the letsencrypt HTTP-01 resolver by name (only our own
    # placeholder interpolation, which mentions "letsencrypt" only as a substring of
    # "letsencrypt-dns" in the committed env example, never in the compose file).
    assert "letsencrypt" not in RAW_TEXT
    assert web_labels["traefik.http.routers.rheostream-http.entrypoints"] == "http"
    assert (
        web_labels["traefik.http.routers.rheostream-http.middlewares"]
        == "rheostream-https-redirect"
    )
    assert not any(
        key.startswith("traefik.http.routers.rheostream-http.tls") for key in web_labels
    )


# --- AC-7: `rheo routing hosts`, run for real -------------------------------------


def test_rheo_routing_hosts_prints_the_callback_url_last() -> None:
    """The overlay's resolved routing env, fed to the real CLI command."""
    import os

    env = {
        **os.environ,
        "RHEO__routing__mode": ANCHOR["RHEO__routing__mode"],
        "RHEO__routing__scheme": ANCHOR["RHEO__routing__scheme"],
        "RHEO__routing__base_host": EXAMPLE_BASE_HOST,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from rheo_app_cli.main import main\n"
            "sys.exit(main(['routing', 'hosts']))\n",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line]
    assert lines[-1] == f"https://auth.{EXAMPLE_BASE_HOST}/auth/callback"
