"""Structured JSON logs that never carry database error detail.

A psycopg error's message can quote the failing statement's parameters (``DETAIL: Key (id)=(...)``),
and a connection error can quote the connection string. Neither belongs in a log line. Code in this
service logs a database failure only as ``describe_db_error(exc)`` — the exception class and the
SQLSTATE — and the ``DbErrorScrubFilter`` enforces it for anything that slips through: a record whose
``exc_info`` holds a psycopg error loses the traceback and gains the class and SQLSTATE instead.
tests/test_ops.py pins both halves."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys

import psycopg


def describe_db_error(exc: BaseException) -> str:
    """The only representation of a database error that may reach a log line."""
    sqlstate = getattr(exc, "sqlstate", None) or "-"
    return f"{type(exc).__name__} sqlstate={sqlstate}"


def _db_error_in_chain(exc: BaseException | None) -> BaseException | None:
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, psycopg.Error):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


class DbErrorScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith("psycopg"):
            # psycopg's own records (e.g. the pool's "error connecting ...") embed server messages.
            record.msg = f"{record.name}: driver event at {record.levelname} (detail scrubbed)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            return True
        if record.exc_info and record.exc_info[1] is not None:
            db_exc = _db_error_in_chain(record.exc_info[1])
            if db_exc is not None:
                record.msg = f"{record.getMessage()} [db_error {describe_db_error(db_exc)}]"
                record.args = ()
                record.exc_info = None
                record.exc_text = None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": dt.datetime.fromtimestamp(record.created, dt.UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms", "job_id", "attempt", "worker"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_MARK = "_fabrication_prep_handler"


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(getattr(h, _MARK, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        setattr(handler, _MARK, True)
        root.addHandler(handler)
    for handler in root.handlers:
        if not any(isinstance(f, DbErrorScrubFilter) for f in handler.filters):
            handler.addFilter(DbErrorScrubFilter())
    # uvicorn's own loggers propagate to root. Its access log is silenced: the app writes one
    # structured line per request (with request id, without client addresses).
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("psycopg").setLevel(logging.WARNING)
    logging.getLogger("psycopg.pool").setLevel(logging.WARNING)
