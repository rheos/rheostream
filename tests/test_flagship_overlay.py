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
    is_application_host,
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


# Issue #158: Recallatron's subdomain moved from `recallatron.` to `recall.`. The
# old host survives only as a permanent redirect at the proxy.
LEGACY_RECALLATRON_HOST = f"recallatron.{EXAMPLE_BASE_HOST}"
LEGACY_REDIRECT_ROUTERS = frozenset(
    {"rheostream-recallatron", "rheostream-recallatron-http"}
)
LEGACY_REDIRECT_MIDDLEWARE = "rheostream-recall-redirect"

# Issue #174: the requirements keep `app.` in reserve as a permanent redirect to
# the shell on `circuit.`. Like `recallatron.`, it is redirect-only at the proxy.
RESERVED_APP_HOST = f"app.{EXAMPLE_BASE_HOST}"
APP_REDIRECT_ROUTERS = frozenset({"rheostream-app", "rheostream-app-http"})
APP_REDIRECT_MIDDLEWARE = "rheostream-circuit-redirect"

# Every router that only redirects, and the one host each pair carries.
REDIRECT_ROUTERS = LEGACY_REDIRECT_ROUTERS | APP_REDIRECT_ROUTERS


def _resolve_base_host(value: str) -> str:
    """The labels carry the un-interpolated `${RHEO_BASE_HOST:?}` placeholder
    (docker compose resolves it at deploy time, not something this parse does);
    substitute the example file's own value to compare against resolved routing."""
    return value.replace("${RHEO_BASE_HOST:?}", EXAMPLE_BASE_HOST)


def _router_rules() -> dict[str, str]:
    """Router name to its rule, across both labelled services."""
    rules: dict[str, str] = {}
    for service in (CORE, WEB):
        for key, value in _labels_as_dict(service).items():
            match = re.fullmatch(r"traefik\.http\.routers\.([^.]+)\.rule", key)
            if match is not None:
                rules[match.group(1)] = value
    return rules


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


# --- health checks (#156) ---------------------------------------------------------


LOCAL_COMPOSE = yaml.safe_load(
    (ROOT / "deploy" / "compose.yaml").read_text(encoding="utf-8")
)["services"]


def _duration_seconds(value: str) -> int:
    match = re.fullmatch(r"(\d+)s", value)
    assert match, f"expected a whole-second duration: {value!r}"
    return int(match.group(1))


def test_every_service_has_a_health_check() -> None:
    """Coolify reports `running:unknown` for a service with no health check, so
    every service needs one for the application to read `running:healthy`."""
    for name, service in SERVICES.items():
        assert "healthcheck" in service, f"{name} has no healthcheck"
        test = service["healthcheck"]["test"]
        assert test[0] in ("CMD", "CMD-SHELL"), f"{name}: {test!r}"


@pytest.mark.parametrize("name", ["core", "worker", "web"])
def test_app_health_checks_leave_room_for_a_cold_start(name: str) -> None:
    """A cold start re-syncs the venv and runs migrations before anything listens;
    a start_period shorter than that would count those minutes as failures."""
    check = SERVICES[name]["healthcheck"]
    start_period = _duration_seconds(check["start_period"])
    assert start_period >= (300 if name in ("core", "worker") else 120)
    assert _duration_seconds(check["start_interval"]) <= 10
    assert _duration_seconds(check["timeout"]) < _duration_seconds(check["interval"])
    assert check["retries"] >= 3


def test_core_and_web_probe_their_own_healthz_without_curl() -> None:
    """Each probe uses the interpreter its image ships: python3 for core, node for
    web (whose image has no curl). Both hit a route that calls nothing else."""
    core_test = CORE["healthcheck"]["test"]
    assert core_test[:3] == ["CMD", "python3", "-c"]
    assert "http://127.0.0.1:8000/healthz" in core_test[3]
    web_test = WEB["healthcheck"]["test"]
    assert web_test[:3] == ["CMD", "node", "-e"]
    assert "http://127.0.0.1:3000/healthz" in web_test[3]
    assert (ROOT / "apps" / "web" / "src" / "app" / "healthz" / "route.ts").is_file()


def test_worker_health_check_reads_the_heartbeat_the_loop_writes() -> None:
    from rheo_app_worker.heartbeat import HEARTBEAT_FILE_VARIABLE

    assert WORKER["healthcheck"]["test"] == [
        "CMD",
        "/app/.venv/bin/python",
        "-m",
        "rheo_app_worker.heartbeat",
    ]
    heartbeat_file = WORKER["environment"][HEARTBEAT_FILE_VARIABLE]
    assert heartbeat_file.startswith("/tmp/")
    assert not heartbeat_file.startswith(ANCHOR["RHEO_DATA_ROOT"])
    assert HEARTBEAT_FILE_VARIABLE not in CORE["environment"]


def test_the_core_probe_answers_zero_against_a_real_healthz() -> None:
    """Run the overlay's own core probe command against a stand-in server that
    answers /healthz the way core does, then against one that answers 503."""
    import http.server
    import threading

    statuses = iter([200, 503])

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's method name
            code = next(statuses) if self.path == "/healthz" else 404
            self.send_response(code)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        code = CORE["healthcheck"]["test"][3].replace(
            "127.0.0.1:8000", f"127.0.0.1:{port}"
        )
        results = [
            subprocess.run(
                [sys.executable, "-c", code], capture_output=True, timeout=30
            ).returncode
            for _ in range(2)
        ]
    finally:
        server.shutdown()
    assert results[0] == 0
    assert results[1] != 0


def test_local_compose_uses_the_same_core_and_worker_health_checks() -> None:
    for name in ("core", "worker"):
        assert LOCAL_COMPOSE[name]["healthcheck"] == SERVICES[name]["healthcheck"]
    assert (
        LOCAL_COMPOSE["worker"]["environment"]["RHEO_WORKER_HEARTBEAT_FILE"]
        == WORKER["environment"]["RHEO_WORKER_HEARTBEAT_FILE"]
    )


# --- data root mode (#161) --------------------------------------------------------


DOCKERFILE_TEXT = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT_PATH = ROOT / "deploy" / "core-entrypoint.sh"


def test_the_image_creates_the_data_root_owner_only() -> None:
    """The in-image data root is 0700 (Docker copies it into a fresh named
    volume), and every container start re-tightens an older, wider volume."""
    data_root = ANCHOR["RHEO_DATA_ROOT"]
    assert f"RUN install -d -m 0700 {data_root}\n" in DOCKERFILE_TEXT
    assert not re.search(rf"mkdir[^\n]*{re.escape(data_root)}", DOCKERFILE_TEXT)
    assert re.search(
        r"^COPY --chmod=0755 deploy/core-entrypoint\.sh "
        r"/usr/local/bin/rheo-core-entrypoint$",
        DOCKERFILE_TEXT,
        re.MULTILINE,
    )
    assert re.search(
        r'^ENTRYPOINT \["/usr/local/bin/rheo-core-entrypoint"\]$',
        DOCKERFILE_TEXT,
        re.MULTILINE,
    )
    assert "\nUSER " not in DOCKERFILE_TEXT, (
        "the entrypoint's chmod assumes the process user owns the data root"
    )


def _run_entrypoint(tmp_path: Path, root: Path) -> subprocess.CompletedProcess[str]:
    script = ENTRYPOINT_PATH.read_text(encoding="utf-8").replace(
        "root=/var/lib/rheo-stream", f"root={root}"
    )
    assert f"root={root}" in script
    patched = tmp_path / "entrypoint.sh"
    patched.write_text(script, encoding="utf-8")
    return subprocess.run(
        ["/bin/sh", str(patched), "sh", "-c", "echo ran:$0", "arg0"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_the_entrypoint_tightens_a_wide_root_and_runs_the_command(
    tmp_path: Path,
) -> None:
    import stat

    assert "root=/var/lib/rheo-stream\n" in ENTRYPOINT_PATH.read_text(encoding="utf-8")
    root = tmp_path / "rheo-stream"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    result = _run_entrypoint(tmp_path, root)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ran:arg0"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_the_entrypoint_leaves_a_symlink_or_missing_root_alone(
    tmp_path: Path,
) -> None:
    import stat

    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    link = tmp_path / "rheo-stream"
    link.symlink_to(target)
    result = _run_entrypoint(tmp_path, link)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o755

    missing = tmp_path / "missing"
    result = _run_entrypoint(tmp_path, missing)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ran:arg0"
    assert not missing.exists()


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


REQUIRED_INTERPOLATION_RE = re.compile(r"\$\{[A-Z][A-Z0-9_]*:\?\}")


def test_every_interpolation_in_the_file_is_the_required_form() -> None:
    """Every ``$`` in the file opens a ``${NAME:?}``: no ``:-`` or ``-`` default,
    no bare ``${NAME}``, no brace-less ``$NAME``. A missing value must fail the
    deploy loudly, never fall back to a default or an empty string."""
    required = REQUIRED_INTERPOLATION_RE.findall(RAW_TEXT)
    assert required, "the overlay interpolates nothing, which cannot be right"
    offenders = [
        RAW_TEXT[match.start() : match.start() + 40].splitlines()[0]
        for match in re.finditer(r"\$", RAW_TEXT)
        if not REQUIRED_INTERPOLATION_RE.match(RAW_TEXT, match.start())
    ]
    assert not offenders, f"non-required interpolations: {offenders}"
    assert len(required) == RAW_TEXT.count("$")


def test_every_secret_bearing_value_is_exactly_its_required_interpolation() -> None:
    assert POSTGRES["environment"]["POSTGRES_PASSWORD"] == "${RHEO_PG_PASSWORD:?}"
    for service in (CORE, WEB):
        assert (
            service["environment"]["RHEO_INTERNAL_SECRET"]
            == "${RHEO_INTERNAL_SECRET:?}"
        )
    assert (
        CORE["environment"]["RHEO_GITHUB_CLIENT_SECRET"]
        == "${RHEO_GITHUB_CLIENT_SECRET:?}"
    )
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
    """Every host a router actually serves (sends to core or web rather than
    redirecting) is an application host, `api.` or `mcp.`, and every one of those
    is served. The redirect routers are the only exception: the `recallatron.`
    pair carries the legacy host alone, and the `app.` pair the reserved one."""
    config = _resolved_routing_config(monkeypatch)
    expected = application_hosts(config) | {
        f"api.{EXAMPLE_BASE_HOST}",
        f"mcp.{EXAMPLE_BASE_HOST}",
    }
    served: set[str] = set()
    redirected: dict[str, set[str]] = {}
    for router, rule in _router_rules().items():
        hosts = {_resolve_base_host(host) for host in HOST_RULE_RE.findall(rule)}
        if router in REDIRECT_ROUTERS:
            redirected[router] = hosts
        else:
            served |= hosts
    assert served == expected
    assert redirected == {
        **dict.fromkeys(LEGACY_REDIRECT_ROUTERS, {LEGACY_RECALLATRON_HOST}),
        **dict.fromkeys(APP_REDIRECT_ROUTERS, {RESERVED_APP_HOST}),
    }
    assert LEGACY_RECALLATRON_HOST not in application_hosts(config)
    assert RESERVED_APP_HOST not in application_hosts(config)


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
        "rheostream-recallatron",
        "rheostream-app",
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


# --- issue #158: the legacy `recallatron.` host redirects to `recall.` ------------


def _redirect_target(
    labels: dict[str, str], url: str, middleware: str = LEGACY_REDIRECT_MIDDLEWARE
) -> str:
    """What Traefik's redirectregex does with ``url``: Go's ``ReplaceAllString``
    over the raw request URL (scheme, authority, request URI), after the compose
    interpolation of the replacement. The regex is RE2-compatible and anchored,
    so Python's ``re.sub`` gives the same result."""
    prefix = f"traefik.http.middlewares.{middleware}.redirectregex"
    regex = labels[f"{prefix}.regex"]
    replacement = _resolve_base_host(labels[f"{prefix}.replacement"])
    assert re.match(regex, url), f"{regex!r} does not match {url!r}"
    return re.sub(regex, replacement, url)


def test_the_legacy_recallatron_host_permanently_redirects_to_recall() -> None:
    web_labels = _labels_as_dict(WEB)
    prefix = f"traefik.http.middlewares.{LEGACY_REDIRECT_MIDDLEWARE}.redirectregex"
    assert web_labels[f"{prefix}.permanent"] == "true"
    # Coolify's label escaping is off, so compose interpolates every dollar sign;
    # the replacement must carry none beyond the required base-host interpolation,
    # or a capture-group reference would be eaten (or fail) at deploy time.
    assert web_labels[f"{prefix}.replacement"] == "https://recall.${RHEO_BASE_HOST:?}"
    assert "$" not in web_labels[f"{prefix}.regex"]

    for router in LEGACY_REDIRECT_ROUTERS:
        assert (
            web_labels[f"traefik.http.routers.{router}.rule"]
            == "Host(`recallatron.${RHEO_BASE_HOST:?}`)"
        )
        assert (
            web_labels[f"traefik.http.routers.{router}.middlewares"]
            == LEGACY_REDIRECT_MIDDLEWARE
        )
    assert web_labels["traefik.http.routers.rheostream-recallatron.tls"] == "true"
    assert (
        web_labels["traefik.http.routers.rheostream-recallatron-http.entrypoints"]
        == "http"
    )
    assert not any(
        key.startswith("traefik.http.routers.rheostream-recallatron-http.tls")
        for key in web_labels
    )

    # Path and query carry over, and plain http lands on https in one hop.
    for scheme in ("https", "http"):
        assert (
            _redirect_target(
                web_labels, f"{scheme}://{LEGACY_RECALLATRON_HOST}/x?q=a%20b&k=1"
            )
            == f"https://recall.{EXAMPLE_BASE_HOST}/x?q=a%20b&k=1"
        )
        assert (
            _redirect_target(web_labels, f"{scheme}://{LEGACY_RECALLATRON_HOST}/")
            == f"https://recall.{EXAMPLE_BASE_HOST}/"
        )


# --- issue #174: the reserved `app.` host redirects to `circuit.` -----------------


def test_the_reserved_app_host_permanently_redirects_to_circuit() -> None:
    web_labels = _labels_as_dict(WEB)
    prefix = f"traefik.http.middlewares.{APP_REDIRECT_MIDDLEWARE}.redirectregex"
    assert web_labels[f"{prefix}.permanent"] == "true"
    # Same dollar-sign rule as the `recallatron.` redirect: compose interpolates
    # every dollar sign, so the replacement carries only the base-host one.
    assert web_labels[f"{prefix}.replacement"] == "https://circuit.${RHEO_BASE_HOST:?}"
    assert web_labels[f"{prefix}.regex"] == "^https?://[^/]+"
    assert "$" not in web_labels[f"{prefix}.regex"]

    for router in APP_REDIRECT_ROUTERS:
        assert (
            web_labels[f"traefik.http.routers.{router}.rule"]
            == "Host(`app.${RHEO_BASE_HOST:?}`)"
        )
        assert (
            web_labels[f"traefik.http.routers.{router}.middlewares"]
            == APP_REDIRECT_MIDDLEWARE
        )
        assert web_labels[f"traefik.http.routers.{router}.service"] == "rheostream-web"
    assert web_labels["traefik.http.routers.rheostream-app.tls"] == "true"
    assert web_labels["traefik.http.routers.rheostream-app-http.entrypoints"] == "http"
    assert not any(
        key.startswith("traefik.http.routers.rheostream-app-http.tls")
        for key in web_labels
    )
    # No other router carries the reserved host, so nothing serves it directly.
    for router, rule in _router_rules().items():
        if router not in APP_REDIRECT_ROUTERS:
            assert "`app." not in rule, f"{router} matches the reserved host"

    # Path and query carry over, and plain http lands on https in one hop.
    for scheme in ("https", "http"):
        assert (
            _redirect_target(
                web_labels,
                f"{scheme}://{RESERVED_APP_HOST}/x?q=a%20b&k=1",
                APP_REDIRECT_MIDDLEWARE,
            )
            == f"https://circuit.{EXAMPLE_BASE_HOST}/x?q=a%20b&k=1"
        )
        assert (
            _redirect_target(
                web_labels, f"{scheme}://{RESERVED_APP_HOST}/", APP_REDIRECT_MIDDLEWARE
            )
            == f"https://circuit.{EXAMPLE_BASE_HOST}/"
        )


def test_no_generated_url_uses_the_legacy_recallatron_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every link, redirect and callback goes through ``url_for`` with the real
    manifest's host, so none of them may name `recallatron.`: the redirect is for
    old or external links only. Recallatron's own screens land on `recall.`, and
    the legacy host is not an application host, so a return or continue URL
    naming it is refused rather than followed."""
    config = _resolved_routing_config(monkeypatch)
    web = RECALLATRON_MANIFEST.web
    assert web is not None
    assert web.surface.host == "recall"
    assert web.surface.path == "/recallatron"
    module_paths = {route.path for route in web.routes} | {
        entry.path for entry in web.navigation
    }
    for path in module_paths:
        url = url_for(config, web.surface.surface, path)
        assert url.startswith(f"https://recall.{EXAMPLE_BASE_HOST}/"), url

    surfaces = [
        name
        for name, spec in config.surfaces.named().items()
        if not (spec.external or spec.reserved)
    ] + list(config.surfaces.modules)
    generated = [url_for(config, name, "/") for name in surfaces]
    generated.append(url_for(config, IDENTITY, "/callback"))
    generated.extend(application_hosts(config))
    for value in generated:
        assert "recallatron." not in value, value
    assert not is_application_host(config, LEGACY_RECALLATRON_HOST)


@pytest.mark.parametrize("target_label", ["recall", "circuit"])
def test_the_redirect_target_is_served_not_redirected_again(
    monkeypatch: pytest.MonkeyPatch, target_label: str
) -> None:
    """No loop: each redirect's target host is an application host, and no router
    that carries a redirect middleware matches it."""
    config = _resolved_routing_config(monkeypatch)
    target_host = f"{target_label}.{EXAMPLE_BASE_HOST}"
    assert target_host in application_hosts(config)
    labels = {**_labels_as_dict(CORE), **_labels_as_dict(WEB)}
    https_routers_for_target = []
    for router, rule in _router_rules().items():
        hosts = {_resolve_base_host(host) for host in HOST_RULE_RE.findall(rule)}
        if target_host not in hosts:
            continue
        assert router not in REDIRECT_ROUTERS
        middlewares = labels.get(f"traefik.http.routers.{router}.middlewares", "")
        entrypoints = labels[f"traefik.http.routers.{router}.entrypoints"]
        if entrypoints == "https":
            assert middlewares == "", f"{router} redirects the redirect target"
            https_routers_for_target.append(router)
    assert sorted(https_routers_for_target) == ["rheostream-auth", "rheostream-web"]


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
