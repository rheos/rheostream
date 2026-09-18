"""Process-local adapter lookup. No module-level production instance."""

from rheo_contracts import RuntimeAdapter


class AdapterRegistry:
    """``runtime_id -> RuntimeAdapter``.

    Production populates one instance in the worker.
    """

    __slots__ = ("_adapters",)

    def __init__(self) -> None:
        self._adapters: dict[str, RuntimeAdapter] = {}

    def register(self, runtime_id: str, adapter: RuntimeAdapter) -> None:
        if not isinstance(runtime_id, str) or not runtime_id:
            raise ValueError("a runtime id is a non-empty string")
        self._adapters[runtime_id] = adapter

    def lookup(self, runtime_id: str) -> RuntimeAdapter | None:
        return self._adapters.get(runtime_id)

    def names(self) -> frozenset[str]:
        return frozenset(self._adapters)
