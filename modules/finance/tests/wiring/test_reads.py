"""The opt-in reads as core sees them (RS-6d, ADR-0028). No database.

- L1: the registered names are exactly the eight reader reads, the reader-READ filter
  over the catalogue; each tool is its spec's, names its declaration and is READ.
- L2: every declaration is READ, owner-only, unaudited, ``Idempotency.NONE`` and not
  long-running; the manifest is ``explicit_only`` and passes core's tool rule.
- L3: core's own walks find no restricted field and nothing unrenderable in any
  registered output, and every field at every depth carries core's marker. Control:
  finance's walk does find ``rows.amount`` restricted in ``lines.ListOut``.
- L4: walked beside finance's model, every kept field has its finance tier and every
  restricted path is absent (L3 alone passes a view that maps restricted to public).
- L5: planted models: a restricted element in a list drops its field, an untiered
  field and a model held in a ``dict`` refuse, a default is kept.
- L6: every registered output, built with sentinel values in each restricted field,
  dumps through its view without either sentinel.
- L7: the adapter over a recording connection and a synthetic layout: the role is set
  and reset around the handler, the layout bound for it and released; finance
  failures leave as their fixed code with nothing chained; anything else, and core's
  own refusals, pass unchanged; a missing layout refuses before any statement.

Values are synthetic.
"""

from __future__ import annotations

import datetime as dt
import types
import typing
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, Literal

import pytest
from pydantic import BaseModel, ConfigDict
from rheo_contracts import Idempotency, Role, SafetyClass
from rheo_core.modules.manifest import check_explicit_only_tools
from rheo_core.modules.readiness import ModuleNotReady
from rheo_core.operations.refusals import OperationRefused
from rheo_core.operations.registry import OperationRegistry
from rheo_core.redaction.tiers import (
    SensitivityTier,
    annotation_tier,
    nested_structured,
    restricted_fields,
    structured_fields,
    unrenderable_paths,
)
from rheo_core.storage.backend import StorageRefusal
from rheo_core.tokens.sets import ToolRegistry
from sqlalchemy import text

from rheo_finance import catalogue, db
from rheo_finance.context import FinanceContext
from rheo_finance.contracts import Role as FinanceRole
from rheo_finance.contracts import SafetyClass as FinanceSafetyClass
from rheo_finance.contracts import Tiered
from rheo_finance.operations import OperationSpec
from rheo_finance.reports import lines, owed
from rheo_finance.settings import SettingsError
from rheo_finance.tier import Tier
from rheo_finance.wiring import reads
from rheo_finance.wiring.manifest import MANIFEST

P, INTERNAL, R = Tier.PUBLIC, Tier.INTERNAL, Tier.RESTRICTED

EIGHT = (
    ("finance.coverage.get", "finance_coverage"),
    ("finance.document.list", "finance_documents"),
    ("finance.openitem.list", "finance_open_items"),
    ("finance.owed.list", "finance_owed"),
    ("finance.owed.trace", "finance_trace"),
    ("finance.sink.state", "finance_sinks"),
    ("finance.status.get", "finance_status"),
    ("finance.txn.list", "finance_lines"),
)

SENTINEL_AMOUNT = Decimal("4242.42")
SENTINEL_TEXT = "synthetic restricted sentinel text"


def _declarations() -> dict[str, Any]:
    return {declaration.name: declaration for declaration, _ in MANIFEST.operations}


def _specs() -> dict[str, OperationSpec]:
    return catalogue.load_all()


# --- L1, L2: what is registered -----------------------------------------------------


def test_l1_exactly_the_eight_reader_reads_with_their_tools() -> None:
    specs = _specs()
    reader_reads = sorted(
        name
        for name, spec in specs.items()
        if spec.safety_class is FinanceSafetyClass.READ
        and spec.connection == "reader"
        and not spec.owner_only
    )
    names = tuple(declaration.name for declaration, _ in MANIFEST.operations)
    assert names == tuple(name for name, _ in EIGHT)
    assert list(names) == reader_reads
    tools = {tool.operation: tool for tool in MANIFEST.tools}
    assert {(name, tools[name].name) for name in names} == set(EIGHT)
    for name in names:
        tool = tools[name]
        assert tool.name == specs[name].tool_name
        assert tool.safety_class is SafetyClass.READ
        assert tool.input_model is specs[name].input_model
        assert tool.description == specs[name].description


def test_l2_owner_only_unaudited_reads_and_explicit_only() -> None:
    specs = _specs()
    for declaration, handler in MANIFEST.operations:
        assert declaration.safety_class is SafetyClass.READ
        assert declaration.roles == frozenset({Role.OWNER})
        assert declaration.audit is None
        assert declaration.idempotency is Idempotency.NONE
        assert declaration.long_running is False
        assert declaration.input_model is specs[declaration.name].input_model
        assert callable(handler)
    assert MANIFEST.package_sets == "explicit_only"
    check_explicit_only_tools(
        MANIFEST.package_sets, tools=MANIFEST.tools, declarations=_declarations()
    )


def test_l2_core_registries_accept_every_declaration_and_tool() -> None:
    """What the loader does with them, into fresh registries: nothing refused (an
    unrenderable output, a reserved input field), each recorded ``explicit_only``, and a
    second identical registration is a no-op."""
    operations = OperationRegistry()
    tools = ToolRegistry()
    for declaration, handler in MANIFEST.operations:
        first = operations.register(
            declaration, handler, origin="finance", explicit_only=True
        )
        again = operations.register(
            declaration, handler, origin="finance", explicit_only=True
        )
        assert first is again and first.explicit_only
    for tool in MANIFEST.tools:
        tools.register(tool, origin="finance")
    assert tools.names() == {tool for _, tool in EIGHT}


def test_l2_each_handler_is_built_once() -> None:
    """The registry compares handlers by identity: a second build of the manifest's
    table would refuse as a different registration."""
    assert [handler for _, handler in MANIFEST.operations] == [
        handler for _, handler in reads.OPERATIONS
    ]
    assert len({id(handler) for _, handler in MANIFEST.operations}) == 8


# --- walks ----------------------------------------------------------------------


def _finance_paths(model: type[BaseModel], prefix: str = "") -> dict[str, Tier | None]:
    """Each field of a finance model as a dotted path with its finance tier, nested
    models walked under every field that is not restricted."""
    found: dict[str, Tier | None] = {}
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        spelling = (
            Annotated[(info.annotation, *info.metadata)]
            if info.metadata
            else info.annotation
        )
        tier = reads.finance_tier(spelling)
        found[path] = tier
        if tier is R:
            continue
        for nested in nested_structured(info.annotation):
            assert isinstance(nested, type) and issubclass(nested, BaseModel)
            found |= _finance_paths(nested, f"{path}.")
    return found


def _view_paths(view: type, prefix: str = "") -> dict[str, SensitivityTier | None]:
    """Each field of a view as a dotted path with core's tier, nested included."""
    found: dict[str, SensitivityTier | None] = {}
    for name, annotation in structured_fields(view).items():
        path = f"{prefix}{name}"
        found[path] = annotation_tier(annotation)
        for nested in nested_structured(annotation):
            found |= _view_paths(nested, f"{path}.")
    return found


def _view_classes(view: type, seen: set[type] | None = None) -> set[type]:
    seen = set() if seen is None else seen
    if view in seen:
        return seen
    seen.add(view)
    for annotation in structured_fields(view).values():
        for nested in nested_structured(annotation):
            _view_classes(nested, seen)
    return seen


# --- L3, L4: the tiers ------------------------------------------------------------


def test_l3_core_finds_nothing_restricted_or_unrenderable_and_every_field_marked() -> (
    None
):
    control = _finance_paths(lines.ListOut)
    assert control["rows.amount"] is R
    assert control["rows.cad_amount"] is R
    for declaration, _ in MANIFEST.operations:
        view = declaration.output
        assert restricted_fields(view) == (), declaration.name
        assert unrenderable_paths(view) == (), declaration.name
        paths = _view_paths(view)
        assert paths, declaration.name
        unmarked = [path for path, tier in paths.items() if tier is None]
        assert unmarked == [], (declaration.name, unmarked)


def test_l3_view_names_are_distinct_per_finance_class() -> None:
    classes: set[type] = set()
    for declaration, _ in MANIFEST.operations:
        classes |= _view_classes(declaration.output)
    names = [cls.__name__ for cls in classes]
    assert len(names) == len(set(names)), sorted(names)
    assert all(cls.__module__ == "rheo_finance.wiring.reads" for cls in classes)


def test_l4_every_kept_field_has_its_finance_tier_and_no_restricted_path_remains() -> (
    None
):
    specs = _specs()
    dropped_somewhere = False
    for declaration, _ in MANIFEST.operations:
        finance = _finance_paths(specs[declaration.name].output_model)
        view = _view_paths(declaration.output)
        restricted = {path for path, tier in finance.items() if tier is R}
        dropped_somewhere = dropped_somewhere or bool(restricted)
        assert not restricted & set(view), declaration.name
        assert set(view) == {path for path in finance if path not in restricted}
        for path, tier in view.items():
            expected = finance[path]
            assert expected is not None and tier is not None
            assert tier.value == expected.value, (declaration.name, path)
    assert dropped_somewhere  # the control: four outputs hold restricted fields


def test_l4_the_dropped_fields_are_the_designs() -> None:
    specs = _specs()

    def dropped(name: str) -> set[str]:
        finance = _finance_paths(specs[name].output_model)
        return {path for path, tier in finance.items() if tier is R}

    assert dropped("finance.txn.list") == {"rows.amount", "rows.cad_amount"}
    for name in (
        "finance.status.get",
        "finance.coverage.get",
        "finance.document.list",
        "finance.sink.state",
    ):
        assert dropped(name) == set(), name
    owed_list = dropped("finance.owed.list")
    assert {"due_in_window", "overdue", "carried", "headline", "credits.line"} <= (
        owed_list
    )
    assert {"rows.amount_due", "credits.sums.amount", "credits.items.amount"} <= (
        owed_list
    )
    trace = dropped("finance.owed.trace")
    assert {
        "obligations.applications.applied_amount",
        "obligations.applications.payment_amount",
        "obligations.row.remaining",
    } <= trace


# --- L5: planted models ------------------------------------------------------------


class _Inner(BaseModel):
    model_config = ConfigDict(frozen=True)
    label: Annotated[str, Tiered(P)]
    secret: Annotated[Decimal, Tiered(R)]


class _Planted(BaseModel):
    model_config = ConfigDict(frozen=True)
    kept: Annotated[str, Tiered(P)]
    internal: Annotated[int, Tiered(INTERNAL)]
    hidden: list[Annotated[Decimal, Tiered(R)]]
    inner: Annotated[list[_Inner], Tiered(P)]
    maybe: Annotated[_Inner | None, Tiered(P)] = None
    choice: Annotated[Literal["a", "b"], Tiered(P)] = "a"
    defaulted: Annotated[int, Tiered(P)] = 3


class _Untiered(BaseModel):
    kept: Annotated[str, Tiered(P)]
    loose: str


class _InDict(BaseModel):
    held: Annotated[dict[str, _Inner], Tiered(P)]


def test_l5_a_restricted_element_drops_its_field_and_defaults_are_kept() -> None:
    view = reads.output_view(_Planted)
    assert view.__name__ == "test_reads__Planted"
    assert set(view.model_fields) == {
        "kept",
        "internal",
        "inner",
        "maybe",
        "choice",
        "defaulted",
    }
    assert view.model_fields["defaulted"].default == 3
    assert view.model_fields["maybe"].default is None
    tiers = _view_paths(view)
    assert tiers["internal"] is SensitivityTier.INTERNAL
    assert tiers["kept"] is SensitivityTier.PUBLIC
    assert "inner.secret" not in tiers and "inner.label" in tiers
    assert reads.output_view(_Planted) is view  # cached


def test_l5_an_untiered_field_refuses() -> None:
    with pytest.raises(ValueError, match=r"_Untiered\.loose has no tier"):
        reads.output_view(_Untiered)


def test_l5_a_model_in_a_dict_refuses() -> None:
    with pytest.raises(ValueError, match="no view keeps"):
        reads.output_view(_InDict)


# --- L6: sentinels never reach a dump -------------------------------------------------


def _base(annotation: object) -> object:
    while typing.get_origin(annotation) is Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _synthetic(annotation: object, restricted: bool) -> object:
    annotation = _base(annotation)
    origin = typing.get_origin(annotation)
    if origin is typing.Union or isinstance(annotation, types.UnionType):
        arm = next(a for a in typing.get_args(annotation) if a is not type(None))
        return _synthetic(arm, restricted)
    if origin is Literal:
        return typing.get_args(annotation)[0]
    if origin is list:
        return [_synthetic(typing.get_args(annotation)[0], restricted)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _built(annotation)
    if annotation is Decimal:
        return SENTINEL_AMOUNT if restricted else Decimal("0.50")
    if annotation is str:
        return SENTINEL_TEXT if restricted else "synthetic"
    if annotation is bool:
        return True
    if annotation is int:
        return 7
    if annotation is dt.datetime:
        return dt.datetime(2001, 2, 3, 4, 5, tzinfo=dt.UTC)
    if annotation is dt.date:
        return dt.date(2001, 2, 3)
    if annotation is uuid.UUID:
        return uuid.UUID(int=7)
    raise AssertionError(f"no synthetic value for {annotation!r}")


def _built(model: type[BaseModel]) -> BaseModel:
    values: dict[str, object] = {}
    for name, info in model.model_fields.items():
        spelling = (
            Annotated[(info.annotation, *info.metadata)]
            if info.metadata
            else info.annotation
        )
        values[name] = _synthetic(info.annotation, reads.finance_tier(spelling) is R)
    return model.model_validate(values)


@pytest.mark.parametrize("name", [name for name, _ in EIGHT])
def test_l6_no_sentinel_survives_the_view(name: str) -> None:
    spec = _specs()[name]
    out = _built(spec.output_model)
    raw = out.model_dump_json()
    dumped = reads.as_view(spec.output_model, out).model_dump_json()
    assert "synthetic" in dumped  # the control: the kept text reached the dump
    assert str(SENTINEL_AMOUNT) not in dumped
    assert SENTINEL_TEXT not in dumped
    if name in {"finance.txn.list", "finance.owed.list", "finance.owed.trace"}:
        assert str(SENTINEL_AMOUNT) in raw  # the control: finance's own dump has it


def test_l6_the_owed_and_lines_outputs_named_by_the_design() -> None:
    for model in (owed.ListOut, lines.ListOut):
        out = _built(model)
        dumped = reads.as_view(model, out).model_dump_json()
        assert str(SENTINEL_AMOUNT) not in dumped and SENTINEL_TEXT not in dumped
    owed_view = reads.as_view(owed.ListOut, _built(owed.ListOut))
    assert not {"due_in_window", "overdue", "carried", "headline"} & set(
        owed_view.model_dump()
    )


# --- L7: the adapter ---------------------------------------------------------------


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> None:
        self.statements.append(str(statement))


class _In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)
    label: Annotated[str, Tiered(P)]
    amount: Annotated[Decimal, Tiered(R)]


class _Other(BaseModel):
    model_config = ConfigDict(frozen=True)


SET = f"SET LOCAL ROLE {db.READER_ROLE}"
RESET = "RESET ROLE"
MARK = "SELECT 'synthetic handler mark'"


class _Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.layout = db.layout_under(tmp_path / "uploads")
        self.connection = RecordingConnection()
        self.seen: list[FinanceContext] = []
        self.bound: list[db.FinanceLayout | None] = []
        self.raises: BaseException | None = None
        self.returns: BaseModel | None = None
        monkeypatch.setattr(reads, "layout_for", lambda _ctx: self.layout)

    def make(self) -> None:
        for directory in self.layout.directories:
            directory.mkdir(parents=True, exist_ok=True)

    def handler(self, ctx: FinanceContext, uow: Any, _data: _In) -> BaseModel:
        self.seen.append(ctx)
        self.bound.append(db.bound_layout())
        uow.execute(text(MARK))
        if self.raises is not None:
            raise self.raises
        if self.returns is not None:
            return self.returns
        return _Out(label="synthetic", amount=SENTINEL_AMOUNT)

    def run(self) -> BaseModel:
        spec = OperationSpec(
            name="finance.planted.read",
            input_model=_In,
            output_model=_Out,
            handler=self.handler,
            safety_class=FinanceSafetyClass.READ,
            roles=frozenset({FinanceRole.OWNER}),
            connection="reader",
            description="A synthetic read for the adapter test.",
        )
        handle = reads.read_handler(spec)
        uow = SimpleNamespace(connection=self.connection)
        return handle(object(), uow, _In())  # type: ignore[arg-type]


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    assert db.bound_layout() is None
    yield _Rig(tmp_path, monkeypatch)
    assert db.bound_layout() is None  # released, whatever the test raised


def test_l7_the_role_is_set_and_reset_around_the_handler_under_the_layout(
    rig: _Rig,
) -> None:
    rig.make()
    out = rig.run()
    assert rig.connection.statements == [SET, MARK, RESET]
    assert rig.bound == [rig.layout]
    (ctx,) = rig.seen
    assert ctx.role is FinanceRole.OWNER
    assert ctx._databases is None
    assert ctx.paths.data == rig.layout.data
    assert out.model_dump() == {"label": "synthetic"}
    assert type(out) is reads.output_view(_Out)


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (db.LayoutRefused(SENTINEL_TEXT), "finance_layout_invalid"),
        (SettingsError(SENTINEL_TEXT), "finance_settings_invalid"),
        (db.WriterBusy(SENTINEL_TEXT), "finance_writer_busy"),
    ],
    ids=["layout", "settings", "writer"],
)
def test_l7_a_finance_failure_leaves_as_its_code_alone(
    rig: _Rig, exc: BaseException, code: str
) -> None:
    rig.make()
    rig.raises = exc
    with pytest.raises(OperationRefused) as caught:
        rig.run()
    refused = caught.value
    assert (refused.state, refused.detail) == (code, code)
    assert SENTINEL_TEXT not in str(refused)
    assert refused.__cause__ is None
    assert refused.__context__ is None
    # No reset after a failure: core's rollback ends the transaction and the role.
    assert rig.connection.statements == [SET, MARK]


@pytest.mark.parametrize(
    "exc",
    [
        KeyError(SENTINEL_TEXT),
        ModuleNotReady("module_unavailable", "synthetic retry"),
        StorageRefusal("workspace_unavailable", "synthetic detail"),
    ],
    ids=["other", "not-ready", "storage"],
)
def test_l7_anything_else_passes_unchanged(rig: _Rig, exc: BaseException) -> None:
    rig.make()
    rig.raises = exc
    with pytest.raises(type(exc)) as caught:
        rig.run()
    assert caught.value is exc


def test_l7_a_missing_layout_refuses_before_any_statement(rig: _Rig) -> None:
    with pytest.raises(OperationRefused) as caught:
        rig.run()
    assert caught.value.state == "finance_layout_invalid"
    assert rig.connection.statements == []
    assert rig.seen == []


def test_l7_an_output_of_another_type_refuses(rig: _Rig) -> None:
    rig.make()
    rig.returns = _Other()
    with pytest.raises(OperationRefused) as caught:
        rig.run()
    assert (caught.value.state, caught.value.detail) == (
        "finance_output_invalid",
        "finance_output_invalid",
    )
    assert rig.connection.statements == [SET, MARK, RESET]
