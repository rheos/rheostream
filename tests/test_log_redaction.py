"""The access-log query redaction (issue #287, FR 17, AC-17's log half).

Seams under test: :class:`rheo_core.log_config.RedactQueryFilter` on a record shaped
the way uvicorn writes one, and the ``uvicorn.access`` logger as
``rheo_app_core.serve.server_configs`` leaves it, which is the construction path
``serve()`` itself runs. uvicorn's ``dictConfig`` runs inside every
``uvicorn.Config(...)``, so the filter only counts if it is still on the logger
after both Configs exist; the production-path test builds them through
``server_configs`` and reads the line the real ``access`` handler writes.

``capsys`` is taken before the Configs are built so the handler uvicorn's
``dictConfig`` installs (``ext://sys.stdout``) writes into the captured stream:
the assertion is on the bytes the production handler and formatter emit.
"""

import logging
from collections.abc import Iterator

import pytest
import uvicorn
from rheo_app_core import serve
from rheo_app_core.internal_app import internal_app
from rheo_app_core.main import public_app
from rheo_core.log_config import (
    ACCESS_LOGGER,
    REDACTED_TARGET,
    RedactQueryFilter,
    attach_access_redaction,
)
from uvicorn.logging import AccessFormatter

UVICORN_ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'
"""The format string uvicorn's HTTP protocols pass to ``access_logger.info``."""

CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
GITHUB_CODE = "example-github-code-0123456789"
AUTHORIZE = (
    "/auth/oauth/authorize?response_type=code&client_id=c1"
    f"&code_challenge={CHALLENGE}&code_challenge_method=S256"
)
CALLBACK = f"/auth/callback?code={GITHUB_CODE}&state=s1"

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@pytest.fixture(autouse=True)
def restore_uvicorn_loggers() -> Iterator[None]:
    """``uvicorn.Config`` reconfigures the process's uvicorn loggers; put them back."""
    saved = {
        name: (
            list(logger.handlers),
            list(logger.filters),
            logger.propagate,
            logger.level,
            logger.disabled,
        )
        for name in _UVICORN_LOGGERS
        for logger in [logging.getLogger(name)]
    }
    yield
    for name, (handlers, filters, propagate, level, disabled) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.filters[:] = filters
        logger.propagate = propagate
        logger.setLevel(level)
        logger.disabled = disabled


def _access_record(target: str, args: object = None) -> logging.LogRecord:
    record = logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, UVICORN_ACCESS_FORMAT, None, None
    )
    record.args = (  # type: ignore[assignment]
        ("192.0.2.1:40000", "GET", target, "1.1", 303) if args is None else args
    )
    return record


def _rendered(record: logging.LogRecord) -> str:
    """The line uvicorn's own access formatter writes for ``record``."""
    return AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    ).format(record)


# --- the filter --------------------------------------------------------------------


@pytest.mark.parametrize("target", [AUTHORIZE, CALLBACK])
def test_the_query_is_stripped_and_the_path_kept(target: str) -> None:
    record = _access_record(target)
    assert RedactQueryFilter().filter(record) is True
    line = _rendered(record)
    path = target.split("?", 1)[0]
    assert line == f'192.0.2.1:40000 - "GET {path} HTTP/1.1" 303 See Other'
    for secret in (CHALLENGE, GITHUB_CODE, "?"):
        assert secret not in line


def test_a_target_without_a_query_is_untouched() -> None:
    record = _access_record("/healthz")
    before = record.args
    assert RedactQueryFilter().filter(record) is True
    assert record.args == before


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("192.0.2.1:40000", "GET", AUTHORIZE),
        ("192.0.2.1:40000", "GET", AUTHORIZE.encode(), "1.1", 303),
        {"target": AUTHORIZE},
        ("192.0.2.1:40000", "GET", AUTHORIZE, "1.1", 303, "extra"),
    ],
    ids=["empty", "short", "bytes-target", "mapping", "long"],
)
def test_an_unrecognised_shape_fails_closed(args: object) -> None:
    """Not uvicorn's five-tuple with a string target: the whole target is redacted,
    the record is kept, and uvicorn's formatter can still render it."""
    record = _access_record("", args=args)
    assert RedactQueryFilter().filter(record) is True
    line = _rendered(record)
    assert REDACTED_TARGET in line
    assert CHALLENGE not in line


def test_a_preformatted_message_fails_closed() -> None:
    record = logging.LogRecord(
        ACCESS_LOGGER, logging.INFO, __file__, 1, f"GET {AUTHORIZE}", None, None
    )
    assert RedactQueryFilter().filter(record) is True
    line = _rendered(record)
    assert CHALLENGE not in line and REDACTED_TARGET in line


def test_attaching_twice_adds_one_filter() -> None:
    attach_access_redaction()
    attach_access_redaction()
    logger = logging.getLogger(ACCESS_LOGGER)
    assert sum(isinstance(f, RedactQueryFilter) for f in logger.filters) == 1


# --- the production construction path ----------------------------------------------


@pytest.mark.parametrize(
    ("index", "app", "port"),
    [(0, public_app, 8000), (1, internal_app, 8100)],
    ids=["public", "internal"],
)
@pytest.mark.parametrize("target", [AUTHORIZE, CALLBACK])
def test_serve_configs_leave_the_filter_on_the_access_logger(
    capsys: pytest.CaptureFixture[str],
    index: int,
    app: object,
    port: int,
    target: str,
) -> None:
    """Each of serve's two ``uvicorn.Config`` objects, built by the function
    ``serve()`` calls: the filter is on ``uvicorn.access`` after construction, and a
    request line logged the way uvicorn logs it reaches the configured handler with
    no query."""
    logger = logging.getLogger(ACCESS_LOGGER)
    logger.filters[:] = []
    configs = serve.server_configs()
    config = configs[index]
    assert isinstance(config, uvicorn.Config)
    assert config.app is app and config.port == port

    assert any(isinstance(f, RedactQueryFilter) for f in logger.filters)
    assert logger.handlers, "uvicorn's dictConfig installs the access handler"
    capsys.readouterr()
    logger.info(
        UVICORN_ACCESS_FORMAT,
        f"192.0.2.1:{port}",
        "GET",
        target,
        "1.1",
        200,
    )
    written = capsys.readouterr().out
    path = target.split("?", 1)[0]
    assert f'"GET {path} HTTP/1.1" 200' in written, written
    for secret in (CHALLENGE, GITHUB_CODE):
        assert secret not in written
