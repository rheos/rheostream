"""``check_audit_paths(registry)``: the wiring layer of criterion 14's three-layer
guarantee that a mutating operation cannot run without an audit path.

The three layers and what each one catches:

- **Declaration** — ``OperationRegistry.register`` refuses a non-``READ`` declaration
  carrying no ``AuditSpec``. Catches the module author who forgot to declare one.
- **Wiring** — this function, called from ``apps/core``'s startup after
  ``load_modules()``. Catches the module author who declared an ``AuditSpec`` and
  supplied no ``audit_sink``, which registration cannot see because the sink arrives
  through a *manifest* rather than through a declaration.
- **Dispatch** — ``dispatch()`` refuses ``audit_sink_missing``. Unreachable in a
  process whose startup ran this check, and it is what makes the guarantee hold in a
  process whose startup did not — the CLI, a test that registers directly, a caller
  that built its own registry.

**It lives under ``operations/`` and not under ``audit/``, and that is deliberate.**
It has to read a registry, and ``rheo_core.audit`` may not import
``rheo_core.operations`` — ``operations/__init__.py`` imports ``core_ops`` as its first
statement and ``dispatch.py`` imports ``rheo_core.audit``, so the reverse import closes
a real cycle. ``tests/test_audit_sink.py``'s
``test_the_audit_module_does_not_import_the_dispatcher`` pins that direction, and
putting this function beside the sink registry is precisely how a run would break it.
"""

from rheo_contracts import SafetyClass

from rheo_core.audit import sink_for
from rheo_core.operations.registry import REGISTRY, OperationRegistry


def check_audit_paths(registry: OperationRegistry = REGISTRY) -> None:
    """Raise unless every registered non-``READ`` operation's module has a sink.

    **One ``RuntimeError`` naming every offender, not the first one** (AC 22). A
    deployment that wired three modules wrongly should learn all three from one boot,
    not discover them one restart at a time; the operations are listed in name order so
    two runs of the same misconfiguration produce the same message.

    ``RuntimeError`` rather than ``RegistrationRefused``: nothing is being registered
    here, and the fault is in how the process was *assembled* — the same class
    ``apps/core``'s own production-scheme and control-chain failures raise, and the
    same effect, which is that the lifespan does not complete.
    """
    offenders: list[str] = []
    for name in sorted(registry.names()):
        operation = registry.lookup(name)
        if operation is None:  # pragma: no cover - names() is lookup()'s own key set
            continue
        if operation.declaration.safety_class is SafetyClass.READ:
            continue
        if sink_for(operation.module_id) is None:
            offenders.append(f"{name} (module {operation.module_id!r})")
    if offenders:
        raise RuntimeError(
            "no audit sink is installed for the owning module of: "
            + ", ".join(offenders)
            + ". An operation above the read class writes an audit record through its "
            "module's sink, so a module that registers one and supplies no sink would "
            "have every call to it refused at dispatch."
        )
