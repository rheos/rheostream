"""Readiness caches immutable deployment metadata, not database compatibility."""

from types import SimpleNamespace

from rheo_core.modules import readiness, reset_surfaces


def test_schema_heads_are_cached_until_module_reload(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness, "build_config", lambda module: module)

    def scripts(config):
        calls.append(config)
        return SimpleNamespace(get_heads=lambda: ["synthetic_head"])

    monkeypatch.setattr(readiness.ScriptDirectory, "from_config", scripts)
    readiness.expected_heads.cache_clear()
    try:
        assert readiness.expected_heads("synthetic") == {"synthetic_head"}
        assert readiness.expected_heads("synthetic") == {"synthetic_head"}
        assert calls == ["synthetic"]
        reset_surfaces()
        assert readiness.expected_heads("synthetic") == {"synthetic_head"}
        assert calls == ["synthetic", "synthetic"]
    finally:
        readiness.expected_heads.cache_clear()
