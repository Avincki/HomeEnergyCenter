from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from energy_orchestrator.config.models import LoggingConfig
from energy_orchestrator.monitoring import configure_logging


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(autouse=True)
def _reset_root_logging() -> Iterator[None]:
    """Strip our handlers between tests so each test starts clean."""
    yield
    root = logging.getLogger()
    root.handlers = [
        h for h in root.handlers if not getattr(h, "_energy_orchestrator_handler", False)
    ]
    structlog.contextvars.clear_contextvars()


def test_configure_logging_creates_log_dir_and_file(tmp_path: Path) -> None:
    cfg = LoggingConfig(log_dir=tmp_path / "logs", level="INFO", retention_days=3)
    configure_logging(cfg)

    log = structlog.stdlib.get_logger("test")
    log.info("hello world", run="t1")

    log_file = tmp_path / "logs" / "energy_orchestrator.log"
    assert log_file.exists()
    records = _read_jsonl(log_file)
    matched = any(r.get("event") == "hello world" and r.get("run") == "t1" for r in records)
    assert matched, f"missing record in {records}"


def test_log_records_carry_iso_timestamp_and_level(tmp_path: Path) -> None:
    cfg = LoggingConfig(log_dir=tmp_path / "logs", level="INFO")
    configure_logging(cfg)

    log = structlog.stdlib.get_logger("test")
    log.warning("watch out", code=42)

    records = _read_jsonl(tmp_path / "logs" / "energy_orchestrator.log")
    rec = next(r for r in records if r.get("event") == "watch out")
    assert rec["level"] == "warning"
    assert rec["code"] == 42
    assert "timestamp" in rec
    # ISO 8601 UTC suffix.
    assert rec["timestamp"].endswith("Z") or "+00:00" in rec["timestamp"]


def test_bound_contextvars_appear_in_records(tmp_path: Path) -> None:
    cfg = LoggingConfig(log_dir=tmp_path / "logs", level="INFO")
    configure_logging(cfg)
    log = structlog.stdlib.get_logger("test")

    with structlog.contextvars.bound_contextvars(tick_at="2026-05-01T12:00:00+00:00"):
        log.info("inside tick", source="sonnen")
    log.info("outside tick")

    records = _read_jsonl(tmp_path / "logs" / "energy_orchestrator.log")
    inside = next(r for r in records if r.get("event") == "inside tick")
    outside = next(r for r in records if r.get("event") == "outside tick")
    assert inside["tick_at"] == "2026-05-01T12:00:00+00:00"
    assert inside["source"] == "sonnen"
    assert "tick_at" not in outside


def test_stdlib_loggers_also_route_through_pipeline(tmp_path: Path) -> None:
    """Libraries using ``logging.getLogger`` (uvicorn, sqlalchemy) should
    write to the same JSON file."""
    cfg = LoggingConfig(log_dir=tmp_path / "logs", level="INFO")
    configure_logging(cfg)

    stdlib_log = logging.getLogger("uvicorn.access")
    stdlib_log.info("access %s %s", "GET", "/")

    records = _read_jsonl(tmp_path / "logs" / "energy_orchestrator.log")
    assert any("access" in str(r.get("event", "")) for r in records)


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    cfg = LoggingConfig(log_dir=tmp_path / "logs", level="INFO")
    configure_logging(cfg)
    configure_logging(cfg)

    log = structlog.stdlib.get_logger("test")
    log.info("once")

    records = _read_jsonl(tmp_path / "logs" / "energy_orchestrator.log")
    # Exactly one record per emit — no duplicate handler installed.
    matches = [r for r in records if r.get("event") == "once"]
    assert len(matches) == 1


def test_does_not_clobber_foreign_handlers(tmp_path: Path) -> None:
    """A pre-existing third-party handler (e.g. pytest's caplog) must
    survive ``configure_logging`` calls."""
    foreign = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(foreign)
    try:
        cfg = LoggingConfig(log_dir=tmp_path / "logs", level="INFO")
        configure_logging(cfg)
        assert foreign in root.handlers
    finally:
        root.removeHandler(foreign)


def test_level_threshold_respected(tmp_path: Path) -> None:
    cfg = LoggingConfig(log_dir=tmp_path / "logs", level="WARNING")
    configure_logging(cfg)
    log = structlog.stdlib.get_logger("test")
    log.info("filtered out")
    log.warning("kept")

    records = _read_jsonl(tmp_path / "logs" / "energy_orchestrator.log")
    events = [r["event"] for r in records]
    assert "filtered out" not in events
    assert "kept" in events
