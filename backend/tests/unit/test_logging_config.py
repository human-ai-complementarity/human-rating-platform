"""Unit tests for the JSON logging formatter and configure_logging().

These are pure-stdlib tests — no database, no FastAPI, no fixtures from
conftest.py.  They run locally without Docker.
"""

from __future__ import annotations

import io
import json
import logging
import sys

import pytest

from logging_config import SERVICE_NAME, _OtelJsonFormatter, configure_logging


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    msg: str = "test message",
    level: int = logging.INFO,
    *,
    attributes: dict | None = None,
    exc_info: tuple | None = None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=level,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    if attributes is not None:
        record.attributes = attributes
    return record


@pytest.fixture(autouse=True)
def _restore_root_logger():
    """Restore root logger state after each test so tests don't bleed into each other."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(saved_handlers)
    root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# _OtelJsonFormatter
# ---------------------------------------------------------------------------


class TestOtelJsonFormatter:
    def _format(self, record: logging.LogRecord) -> dict:
        return json.loads(_OtelJsonFormatter().format(record))

    # --- Required fields ---

    def test_output_is_valid_json(self):
        raw = _OtelJsonFormatter().format(_make_record())
        assert isinstance(json.loads(raw), dict)

    def test_required_fields_present(self):
        out = self._format(_make_record())
        for field in ("timestamp", "severity", "body", "service.name", "logger"):
            assert field in out, f"missing field: {field}"

    def test_service_name(self):
        assert self._format(_make_record())["service.name"] == SERVICE_NAME

    def test_body_is_message(self):
        assert self._format(_make_record("hello world"))["body"] == "hello world"

    def test_logger_name(self):
        assert self._format(_make_record())["logger"] == "test.logger"

    # --- Severity mapping ---

    def test_severity_debug(self):
        assert self._format(_make_record(level=logging.DEBUG))["severity"] == "DEBUG"

    def test_severity_info(self):
        assert self._format(_make_record(level=logging.INFO))["severity"] == "INFO"

    def test_severity_warning_maps_to_warn(self):
        # OTel uses WARN, not WARNING
        assert self._format(_make_record(level=logging.WARNING))["severity"] == "WARN"

    def test_severity_error(self):
        assert self._format(_make_record(level=logging.ERROR))["severity"] == "ERROR"

    def test_severity_critical_maps_to_fatal(self):
        assert self._format(_make_record(level=logging.CRITICAL))["severity"] == "FATAL"

    # --- attributes field ---

    def test_attributes_absent_when_not_set(self):
        assert "attributes" not in self._format(_make_record())

    def test_attributes_included_when_present(self):
        attrs = {"experiment_id": 42, "key": "val"}
        out = self._format(_make_record(attributes=attrs))
        assert out["attributes"] == attrs

    def test_attributes_types_are_preserved(self):
        attrs = {"count": 3, "flag": True, "name": "x"}
        out = self._format(_make_record(attributes=attrs))
        assert out["attributes"]["count"] == 3
        assert out["attributes"]["flag"] is True
        assert out["attributes"]["name"] == "x"

    # --- exc_info ---

    def test_exception_folded_into_attributes(self):
        try:
            raise ValueError("boom")
        except ValueError:
            exc = sys.exc_info()

        out = self._format(_make_record(level=logging.ERROR, exc_info=exc))
        assert "attributes" in out
        assert "exception" in out["attributes"]
        assert "ValueError" in out["attributes"]["exception"]
        assert "boom" in out["attributes"]["exception"]

    def test_exception_merged_with_existing_attributes(self):
        try:
            raise RuntimeError("bad")
        except RuntimeError:
            exc = sys.exc_info()

        out = self._format(
            _make_record(level=logging.ERROR, attributes={"ctx": "test"}, exc_info=exc)
        )
        assert out["attributes"]["ctx"] == "test"
        assert "exception" in out["attributes"]

    def test_no_attributes_key_when_exc_info_is_none(self):
        """No exc_info and no attributes → no attributes key at all."""
        record = _make_record()
        record.exc_info = (None, None, None)
        assert "attributes" not in self._format(record)

    # --- extra={"attributes": {...}} round-trip via a real Logger call ---

    def test_extra_keyword_arg_reaches_attributes_in_json(self):
        """Confirm that logger.info(..., extra={"attributes": {...}}) works correctly.

        Python's Logger.info signature is info(msg, *args, **kwargs) — the
        extra= keyword is collected into **kwargs and forwarded to LogRecord
        creation; it does NOT become a fourth positional %s argument.
        """
        formatter = _OtelJsonFormatter()
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(formatter)

        isolated = logging.getLogger("test.extra_kwarg_roundtrip")
        isolated.propagate = False
        isolated.setLevel(logging.DEBUG)
        isolated.addHandler(handler)
        try:
            isolated.info(
                "%s completed in %s ms",
                "export",
                42,
                extra={"attributes": {"experiment_id": 7, "row_count": 100}},
            )
            out = json.loads(buf.getvalue().strip())
            assert out["body"] == "export completed in 42 ms"
            assert out["attributes"] == {"experiment_id": 7, "row_count": 100}
        finally:
            isolated.removeHandler(handler)
            isolated.propagate = True


# ---------------------------------------------------------------------------
# configure_logging()
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_attaches_exactly_one_handler_to_root(self):
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1

    def test_idempotent_does_not_accumulate_handlers(self):
        configure_logging("INFO")
        configure_logging("INFO")
        configure_logging("DEBUG")
        assert len(logging.getLogger().handlers) == 1

    def test_handler_uses_otel_formatter(self):
        configure_logging("INFO")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, _OtelJsonFormatter)

    def test_handler_writes_to_stdout(self):
        configure_logging("INFO")
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout

    def test_log_level_info(self):
        configure_logging("INFO")
        assert logging.getLogger().level == logging.INFO

    def test_log_level_debug(self):
        configure_logging("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_log_level_warning(self):
        configure_logging("WARNING")
        assert logging.getLogger().level == logging.WARNING

    def test_log_level_error(self):
        configure_logging("ERROR")
        assert logging.getLogger().level == logging.ERROR

    def test_log_level_case_insensitive(self):
        configure_logging("debug")
        assert logging.getLogger().level == logging.DEBUG

    def test_replaces_existing_handlers(self):
        # Install a dummy handler first, then verify configure_logging clears it.
        logging.getLogger().addHandler(logging.NullHandler())
        configure_logging("INFO")
        assert len(logging.getLogger().handlers) == 1
        assert isinstance(logging.getLogger().handlers[0].formatter, _OtelJsonFormatter)
