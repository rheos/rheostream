"""The routing configuration and the URL builders, driven by the shared fixture.

Seam: ``rheo_core.routing.url_for`` — ``url_for``, ``identity_path``,
``application_hosts`` — exercised from ``tests/fixtures/routing/*.json``. Those three
files are the contract, not a convenience: the web tier's vitest suite reads the same
bytes, so the Python and TypeScript implementations are proven **equal** rather than
merely alike (B10). A fixture entry is therefore the authority — if an expected string
and this implementation disagree, the implementation is wrong.

Two things the fixture alone cannot pin are asserted against literals here as well, so
that editing a fixture entry cannot quietly retire them: the documented OAuth callback
URL in both modes, and the single-slash join for a surface whose path is the root.

One assertion reads a **deployment file** rather than a fixture — the `base_host` in
`deploy/compose.yaml`, issue #37. It is here because the property it defends is this
module's
(`is_application_host`'s exact compare against an already-stripped host), and because
that file is the only shipped routing configuration in the tree: nothing else would
catch it drifting back.

No Postgres. The routing configuration is deployment settings and pure functions.
"""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from rheo_app_core.auth_routes import normalize_host
from rheo_core.routing import (
    API,
    DOCS,
    IDENTITY,
    INTEGRATION,
    MCP,
    SHELL,
    RoutingConfig,
    RoutingMode,
    application_hosts,
    identity_path,
    is_application_host,
    url_for,
)
from rheo_core.settings import resolve

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
MODES = ("path", "subdomain")
COMPOSE = Path(__file__).resolve().parents[1] / "deploy" / "compose.yaml"


def read_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_config(mode: str) -> RoutingConfig:
    return RoutingConfig.model_validate(read_fixture(f"{mode}-mode.json"))


CONFIGS: dict[str, RoutingConfig] = {mode: load_config(mode) for mode in MODES}
EXPECTATIONS: list[dict[str, Any]] = read_fixture("expected-urls.json")

URL_CASES = [case for case in EXPECTATIONS if "surface" in case]
IDENTITY_PATH_CASES = [case for case in EXPECTATIONS if case.get("identity_path")]
HOST_CASES = [case for case in EXPECTATIONS if "application_hosts" in case]


def url_id(case: dict[str, Any]) -> str:
    return f"{case['mode']}-{case['surface']}-{case['path']}"


def path_id(case: dict[str, Any]) -> str:
    return f"{case['mode']}-identity_path-{case['path']}"


# --- the fixture is the contract ----------------------------------------------------


def test_the_fixture_covers_both_modes_and_every_non_external_surface() -> None:
    """A vacuous fixture would make every assertion below pass by covering nothing."""
    for mode in MODES:
        surfaces = {case["surface"] for case in URL_CASES if case["mode"] == mode}
        assert surfaces == {SHELL, IDENTITY, API, MCP}
        assert [case for case in IDENTITY_PATH_CASES if case["mode"] == mode]
        assert len([case for case in HOST_CASES if case["mode"] == mode]) == 1


@pytest.mark.parametrize("case", URL_CASES, ids=url_id)
def test_url_for_matches_the_fixture(case: dict[str, Any]) -> None:
    config = CONFIGS[case["mode"]]
    assert url_for(config, case["surface"], case["path"]) == case["expected"]


@pytest.mark.parametrize("case", IDENTITY_PATH_CASES, ids=path_id)
def test_identity_path_matches_the_fixture(case: dict[str, Any]) -> None:
    config = CONFIGS[case["mode"]]
    assert identity_path(config, case["path"]) == case["expected"]


@pytest.mark.parametrize("case", HOST_CASES, ids=lambda case: str(case["mode"]))
def test_application_hosts_match_the_fixture(case: dict[str, Any]) -> None:
    config = CONFIGS[case["mode"]]
    expected = frozenset(case["application_hosts"])
    assert application_hosts(config) == expected
    for host in expected:
        assert is_application_host(config, host)
    assert not is_application_host(config, "elsewhere.example.test")
    assert not is_application_host(config, "docs.example.test")


@pytest.mark.parametrize("mode", MODES)
def test_the_fixture_round_trips_through_the_model(mode: str) -> None:
    """The serialised form 08 serves and 10 reads carries every fixture field."""
    raw = read_fixture(f"{mode}-mode.json")
    dumped = CONFIGS[mode].model_dump(mode="json")
    assert (dumped["mode"], dumped["scheme"], dumped["base_host"]) == (
        raw["mode"],
        raw["scheme"],
        raw["base_host"],
    )
    for name, surface in raw["surfaces"].items():
        if name == "modules":
            assert dumped["surfaces"]["modules"] == surface
            continue
        for field, value in surface.items():
            assert dumped["surfaces"][name][field] == value


# --- the two rules the fixture must not be able to retire ---------------------------


def test_the_identity_callback_is_the_documented_url() -> None:
    assert (
        url_for(CONFIGS["subdomain"], IDENTITY, "/callback")
        == "https://auth.example.test/auth/callback"
    )
    assert (
        url_for(CONFIGS["path"], IDENTITY, "/callback")
        == "https://example.test/auth/callback"
    )


def test_a_root_surface_path_does_not_double_the_slash() -> None:
    """``shell``'s path is ``/``; naive concatenation yields ``example.test//login``."""
    assert url_for(CONFIGS["path"], SHELL, "/login") == "https://example.test/login"
    assert (
        url_for(CONFIGS["subdomain"], SHELL, "/login")
        == "https://circuit.example.test/login"
    )
    assert url_for(CONFIGS["path"], SHELL, "/") == "https://example.test/"


def relocated_identity(mode: str, identity_prefix: str) -> RoutingConfig:
    """The fixture config with ``routing.identity.path`` moved, as an operator may."""
    raw = read_fixture(f"{mode}-mode.json")
    raw["surfaces"]["identity"]["path"] = identity_prefix
    return RoutingConfig.model_validate(raw)


@pytest.mark.parametrize("mode", MODES)
def test_a_root_identity_prefix_is_not_protocol_relative(mode: str) -> None:
    """``routing.identity.path`` is operator-settable with no closed choice set.

    Raw concatenation turned ``"/"`` into ``"//continue"``, which a browser reads as
    scheme-relative — a URL for a host named ``continue``, not a path on this one.
    """
    config = relocated_identity(mode, "/")
    assert identity_path(config, "/continue") == "/continue"
    assert not identity_path(config, "/continue").startswith("//")


@pytest.mark.parametrize("mode", MODES)
def test_identity_path_agrees_with_url_for_on_a_relocated_prefix(mode: str) -> None:
    """The two disagreed in exactly the case the join helper exists for."""
    config = relocated_identity(mode, "/")
    assert url_for(config, IDENTITY, "/continue").endswith(
        identity_path(config, "/continue")
    )


@pytest.mark.parametrize("mode", MODES)
def test_a_path_without_a_leading_slash_is_normalised(mode: str) -> None:
    """``"/api" + "v1/ops"`` is ``/apiv1/ops`` — silently wrong, not loudly wrong.

    Chunk 10 mirrors ``_join_prefix`` from its description rather than importing it,
    so the rule has to be the one the docstring states.
    """
    config = CONFIGS[mode]
    assert url_for(config, API, "v1/operations") == url_for(
        config, API, "/v1/operations"
    )
    assert identity_path(config, "continue") == "/auth/continue"
    assert url_for(config, SHELL, "") == url_for(config, SHELL, "/")


def test_the_two_modes_are_not_vacuously_identical() -> None:
    """Every surface case covered in both modes must produce two different URLs."""
    by_case: dict[tuple[str, str], dict[str, str]] = {}
    for case in URL_CASES:
        by_case.setdefault((case["surface"], case["path"]), {})[case["mode"]] = case[
            "expected"
        ]
    both = {key: values for key, values in by_case.items() if len(values) == len(MODES)}
    assert (IDENTITY, "/callback") in both
    assert (SHELL, "/login") in both
    for key, values in both.items():
        assert len(set(values.values())) == len(MODES), key


# --- refusals ------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
def test_an_unknown_surface_refuses(mode: str) -> None:
    with pytest.raises(ValueError, match="nonesuch"):
        url_for(CONFIGS[mode], "nonesuch", "/")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("surface", [DOCS, INTEGRATION])
def test_external_and_reserved_surfaces_refuse(surface: str, mode: str) -> None:
    """This application never builds a link to ``docs.`` or ``tuttle.``.

    Matched on the refusal's own words, not the surface name: the name alone would
    also match the *unknown-surface* error, so the assertion would still pass against
    an implementation that had simply lost the surface.
    """
    with pytest.raises(ValueError, match="not served by this application"):
        url_for(CONFIGS[mode], surface, "/")


def test_modules_is_empty_in_this_release() -> None:
    for mode in MODES:
        assert dict(CONFIGS[mode].surfaces.modules) == {}


# --- the reference deployment's own base_host (issue #37) ----------------------------


def compose_core_environment() -> dict[str, str]:
    """``deploy/compose.yaml``'s ``core`` service ``environment:`` block, from the file.

    An indentation scan rather than a YAML parse: PyYAML is not a declared dependency
    of this tree and nothing else in it imports one, so a single assertion would be
    adding a dependency to read six lines. The scan reads the **real** file — a copy of
    its literal here would assert only that this module can quote itself.
    """
    values: dict[str, str] = {}
    in_core = in_environment = False
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 2 and stripped.endswith(":"):
            in_core = stripped == "core:"
            in_environment = False
        elif in_core and indent == 4:
            in_environment = stripped == "environment:"
        elif in_core and in_environment and indent == 6 and ":" in stripped:
            key, _, value = stripped.partition(":")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def test_the_reference_deployments_base_host_is_port_free() -> None:
    """Issue #37: `deploy/compose.yaml` set a `base_host` that can never match.

    ``is_application_host`` is an exact compare, and every caller hands it a host it
    has already stripped the port from (``rheo_app_core.auth_routes.normalize_host``,
    on the ``Host`` header and on an ``Origin``). So a ``base_host`` spelled with a
    port matches nothing a browser sends, and ``/auth/*`` refuses every request —
    which ``deploy/README.md`` states as a rule and the reference config on the
    adjacent page contradicted.

    The round trip below is the real assertion; the ``":" not in`` check above it is
    the message an operator wants to read when it fails.
    """
    environment = compose_core_environment()
    # Positive control: a scan that matched nothing would satisfy every assertion
    # below by never reaching one.
    assert {"RHEO__routing__mode", "RHEO__routing__scheme"} <= set(environment), (
        "the compose scan found no core routing keys; the assertions below are vacuous",
        sorted(environment),
    )
    base_host = environment["RHEO__routing__base_host"]
    assert ":" not in base_host, (
        f"deploy/compose.yaml sets base_host {base_host!r}, which carries a port; "
        "normalize_host strips the port before the application-host compare, so this "
        "value can never match and /auth/* refuses every request (issue #37)"
    )
    config = RoutingConfig.model_validate(
        {**read_fixture("path-mode.json"), "base_host": base_host}
    )
    assert is_application_host(config, normalize_host(base_host)), (
        f"a request arriving at {base_host!r} is not recognised as this application's "
        "own host"
    )


def test_the_compose_callback_is_composed_from_that_one_port_free_value() -> None:
    """The other half of the same invariant, and the cost of it, pinned rather than
    left to be rediscovered.

    ``base_host`` is doing two jobs with opposite requirements and there is no third
    key to split them onto. ``is_application_host`` compares it against a host the
    caller has already stripped the port from, so it must be **port-free** (the test
    above). ``url_for`` composes ``{scheme}://{base_host}{path}`` in path mode —
    ``RoutingConfig`` carries ``mode``, ``scheme``, ``base_host`` and ``surfaces`` and
    nothing else, so ``base_host`` is the *entire* authority of every URL this codebase
    builds, the OAuth callback at ``auth_routes.py:222`` included.

    **The consequence, stated because a reader will otherwise hit it as a surprise:**
    on a **direct** deployment serving a non-default port, the callback that
    ``rheo routing hosts`` prints and that ``/auth/login`` builds is missing that port.
    The documented topology is a reverse proxy terminating on 443 (ratified D10), which
    has no port to lose and is unaffected; the reference stack here disables the GitHub
    provider outright, so it builds no callback at all. This is a pre-existing property
    of the one-key design, not of the port-free spelling — with a port in ``base_host``
    the callback carried one but ``/auth/*`` refused every request, so there is no
    setting of this one key that satisfies both readers.

    Asserting the literal rather than re-deriving it: a re-derivation would restate
    ``url_for`` and pass against any composition. This line is what the operator is
    told to register, and it breaks the moment a separate public-authority key is
    introduced — which is exactly when someone should be made to read this docstring.
    """
    environment = compose_core_environment()
    config = RoutingConfig.model_validate(
        {
            **read_fixture("path-mode.json"),
            "mode": environment["RHEO__routing__mode"],
            "scheme": environment["RHEO__routing__scheme"],
            "base_host": environment["RHEO__routing__base_host"],
        }
    )
    callback = url_for(config, IDENTITY, "/callback")
    assert callback == "http://localhost/auth/callback", (
        "this is the line `rheo routing hosts` prints for an operator to register on "
        "the identity provider; it changed without this test being read"
    )
    assert ":" not in urlsplit(callback).netloc, (
        f"the callback authority {urlsplit(callback).netloc!r} carries a port, so "
        "base_host does too — and then /auth/* refuses every request (issue #37)"
    )


# --- the settings path ---------------------------------------------------------------


def test_from_settings_builds_the_package_default_topology() -> None:
    config = RoutingConfig.from_settings(resolve())
    assert config.mode is RoutingMode.PATH
    assert (config.scheme, config.base_host) == ("https", "localhost")
    surfaces = config.surfaces
    assert (surfaces.shell.host, surfaces.shell.path) == ("circuit", "/")
    assert (surfaces.identity.host, surfaces.identity.path) == ("auth", "/auth")
    assert (surfaces.api.host, surfaces.api.path) == ("api", "/api")
    assert (surfaces.mcp.host, surfaces.mcp.path) == ("mcp", "/mcp")
    assert (surfaces.docs.host, surfaces.docs.external) == ("docs", True)
    assert surfaces.integration.external and surfaces.integration.reserved
    assert dict(surfaces.modules) == {}
    assert application_hosts(config) == frozenset({"localhost"})
    assert url_for(config, IDENTITY, "/callback") == "https://localhost/auth/callback"


def test_identity_is_the_only_fixed_path_surface() -> None:
    """``fixed_path`` is an invariant in code, not a settings key an operator can
    move: every application host serves ``/auth/*`` in both modes."""
    surfaces = RoutingConfig.from_settings(resolve()).surfaces
    fixed = {name for name, surface in surfaces.named().items() if surface.fixed_path}
    assert fixed == {IDENTITY}
