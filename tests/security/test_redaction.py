"""R23: secrets never reach log output; config never reveals secrets."""

import io
import logging
from collections.abc import Iterator

import pytest

from app.config import Settings
from app.observability import REDACTED, RedactingFilter, configure_logging

SECRET = "sk_live_supersecretvalue123"


def _record(msg: str, *args: object) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, args, None)


def test_known_secret_values_are_redacted() -> None:
    f = RedactingFilter([SECRET])
    rec = _record("calling with key=%s", SECRET)
    f.filter(rec)
    assert SECRET not in rec.getMessage() and REDACTED in rec.getMessage()


def test_authorization_headers_are_redacted_even_if_unknown() -> None:
    f = RedactingFilter([])
    for msg in [
        "Authorization: Bearer abcdefghijklmnop",
        "{'Api-Key': 'zzzzzzzzzzzz'}",
        "Basic dXNlcjpwYXNzd29yZA==",
    ]:
        rec = _record(msg)
        f.filter(rec)
        assert "abcdefghijklmnop" not in rec.getMessage()
        assert "zzzzzzzzzzzz" not in rec.getMessage()
        assert "dXNlcjpwYXNzd29yZA" not in rec.getMessage()


def test_exception_text_is_redacted() -> None:
    f = RedactingFilter([SECRET])
    try:
        raise RuntimeError(f"failed with {SECRET}")
    except RuntimeError:
        import sys

        rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "oops", None, sys.exc_info())
    f.filter(rec)
    assert SECRET not in (rec.exc_text or "")


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    """configure_logging replaces the root handlers; put pytest's back afterwards."""
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    root.handlers, root.level = saved[0], saved[1]
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)


def test_configure_logging_redacts_everything_the_process_logs(restore_root_logging: None) -> None:
    """The handler actually installed at startup (web and cron) scrubs secrets from
    messages, arguments and tracebacks of ANY logger, including third-party ones."""
    redactor = configure_logging([SECRET, ""], level=logging.INFO)
    root = logging.getLogger()
    assert len(root.handlers) == 1 and redactor in root.handlers[0].filters
    out = io.StringIO()
    root.handlers[0].setStream(out)  # type: ignore[attr-defined]

    logging.getLogger("app.integrations.synthetic").info("key=%s", SECRET)
    logging.getLogger("some.library").warning("Authorization: Bearer abcdefghijklmnop")
    try:
        raise RuntimeError(f"upstream echoed {SECRET}")
    except RuntimeError:
        logging.getLogger("app.services.synthetic").exception("call failed")

    text = out.getvalue()
    assert SECRET not in text and "abcdefghijklmnop" not in text
    assert text.count(REDACTED) >= 3 and "call failed" in text and "RuntimeError" in text
    # httpx logs full request URLs at INFO (query strings can carry identifiers).
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_settings_repr_hides_secrets() -> None:
    s = Settings(_env_file=None, evertrace_api_key=SECRET)  # type: ignore[call-arg]
    assert SECRET not in repr(s) and SECRET not in str(s.model_dump())
    assert SECRET in s.secret_values()


def test_settings_normalise_postgres_url_and_domains() -> None:
    s = Settings(
        _env_file=None, database_url="postgres://u:p@h:5432/db", dsw_email_domains="dsw.vc, Other.com"
    )  # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql+psycopg://")
    assert s.dsw_email_domains == ("dsw.vc", "other.com")
    assert not s.is_production
