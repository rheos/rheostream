"""The connector OAuth secret scope is built in one source file only.

``connectors/oauth.py`` is the one place that names the ``connector-oauth`` scope
component (it builds the scope through ``SecretStore.scope_for``, never by writing
``SecretScope(``; ``tests/test_boundary_scans.py`` guards that part). This walks the
tracked Python sources under ``packages/core/src``, ``apps`` and ``modules`` and
asserts the string literal appears in exactly that one file.
"""

import ast
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ROOTS = ("packages/core/src", "apps", "modules")
_LITERAL = "connector-oauth"
_OWNER = "packages/core/src/rheo_core/connectors/oauth.py"


def _tracked_sources() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "--", *_ROOTS],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return sorted(path for path in listed if path.endswith(".py"))


def _names_literal(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Constant) and node.value == _LITERAL
        for node in ast.walk(tree)
    )


def test_the_scope_literal_appears_only_in_the_oauth_module() -> None:
    sources = _tracked_sources()
    assert _OWNER in sources, "the scan did not reach the oauth module"
    assert len(sources) > 100, "the tracked-source walk is suspiciously small"
    owners = [path for path in sources if _names_literal(_REPO_ROOT / path)]
    assert owners == [_OWNER]


def test_the_scan_detects_the_literal_elsewhere(tmp_path: Path) -> None:
    planted = tmp_path / "planted.py"
    planted.write_text(f'SCOPE = "{_LITERAL}"\n', encoding="utf-8")
    assert _names_literal(planted)
    clean = tmp_path / "clean.py"
    clean.write_text('SCOPE = "connector-provisioning"\n', encoding="utf-8")
    assert not _names_literal(clean)
