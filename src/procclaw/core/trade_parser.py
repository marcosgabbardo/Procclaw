"""Trade Event Parser for ProcClaw.

Parses JSON lines from trade job stdout, classifies events,
and saves them to the database. Supports the Trade Event Protocol v1.

Lines that are not valid JSON or don't have an "event" field are ignored
(treated as normal log output).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database

# Known event types
KNOWN_EVENTS = {"scan", "decision", "trade_open", "trade_close", "portfolio"}


class TradeEventParser:
    """Parses trade protocol events from stdout lines and saves to DB."""

    def __init__(self, db: "Database", job_id: str, run_id: int | None = None):
        self.db = db
        self.job_id = job_id
        self.run_id = run_id
        self._event_count = 0
        self._error_count = 0

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def parse_line(self, line: str) -> bool:
        """Parse a single stdout line.

        Returns True if line was a trade event, False if normal log line.
        """
        line = line.strip()
        if not line:
            return False

        # Quick check: must start with { to be JSON
        if not line.startswith("{"):
            return False

        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return False

        if not isinstance(data, dict):
            return False

        event_type = data.get("event")
        if not event_type:
            return False

        # We have a trade event
        ts = data.get("ts", datetime.now(timezone.utc).isoformat())

        try:
            self._process_event(event_type, ts, data)
            self._event_count += 1
            return True
        except Exception as e:
            self._error_count += 1
            logger.warning(
                f"Error processing trade event for job '{self.job_id}': "
                f"{type(e).__name__}: {e}"
            )
            # Still store the raw event even if processing fails
            try:
                self.db.add_trade_event(
                    job_id=self.job_id,
                    event_type=event_type,
                    ts=ts,
                    data=json.dumps(data),
                    run_id=self.run_id,
                )
            except Exception:
                pass
            return True

    def _process_event(self, event_type: str, ts: str, data: dict) -> None:
        """Process a classified trade event."""
        raw_json = json.dumps(data)

        # Store raw event (always)
        event_id = self.db.add_trade_event(
            job_id=self.job_id,
            event_type=event_type,
            ts=ts,
            data=raw_json,
            run_id=self.run_id,
        )

        # Process by type
        if event_type == "scan":
            self._handle_scan(ts, data, event_id)
        elif event_type == "decision":
            self._handle_decision(ts, data, event_id)
        elif event_type == "trade_open":
            self._handle_trade_open(ts, data, event_id)
        elif event_type == "trade_close":
            self._handle_trade_close(ts, data, event_id)
        elif event_type == "portfolio":
            self._handle_portfolio(ts, data, event_id)
        # Unknown event types are stored as raw events only (extensibility)

    def _handle_scan(self, ts: str, data: dict, event_id: int) -> None:
        """Handle a scan event. No extra table — just stored in trade_events."""
        pass  # Stats recalculated from events

    def _handle_decision(self, ts: str, data: dict, event_id: int) -> None:
        """Handle a decision event."""
        market = data.get("market", "")
        action = data.get("action", "skip")
        reason = data.get("reason", "")
        market_id = data.get("market_id")
        details = data.get("details")

        self.db.add_trade_decision(
            job_id=self.job_id,
            ts=ts,
            market=market,
            action=action,
            market_id=market_id,
            reason=reason,
            details=json.dumps(details) if details else None,
            event_id=event_id,
        )

    def _handle_trade_open(self, ts: str, data: dict, event_id: int) -> None:
        """Handle a trade_open event."""
        trade_id = data.get("trade_id", "")
        if not trade_id:
            logger.warning(f"trade_open without trade_id for job '{self.job_id}'")
            return

        self.db.upsert_trade(
            job_id=self.job_id,
            trade_id=trade_id,
            market=data.get("market", ""),
            opened_at=ts,
            market_id=data.get("market_id"),
            side=data.get("side"),
            entry_price=data.get("entry_price"),
            size=data.get("size"),
            quantity=data.get("quantity"),
            target_price=data.get("target_price"),
            stop_loss=data.get("stop_loss"),
            strategy=data.get("strategy"),
            open_event_id=event_id,
        )

    def _handle_trade_close(self, ts: str, data: dict, event_id: int) -> None:
        """Handle a trade_close event."""
        trade_id = data.get("trade_id", "")
        if not trade_id:
            logger.warning(f"trade_close without trade_id for job '{self.job_id}'")
            return

        self.db.close_trade(
            job_id=self.job_id,
            trade_id=trade_id,
            exit_price=data.get("exit_price"),
            pnl=data.get("pnl"),
            pnl_pct=data.get("pnl_pct"),
            reason=data.get("reason"),
            closed_at=ts,
            hold_duration_hours=data.get("hold_duration_hours"),
            close_event_id=event_id,
        )

    def _handle_portfolio(self, ts: str, data: dict, event_id: int) -> None:
        """Handle a portfolio snapshot event."""
        self.db.add_portfolio_snapshot(
            job_id=self.job_id,
            ts=ts,
            initial_capital=data.get("initial_capital"),
            current_capital=data.get("current_capital"),
            cash=data.get("cash"),
            invested=data.get("invested"),
            unrealized_pnl=data.get("unrealized_pnl"),
            realized_pnl=data.get("realized_pnl"),
            open_positions=data.get("open_positions"),
            total_trades=data.get("total_trades"),
            win_count=data.get("win_count"),
            loss_count=data.get("loss_count"),
            event_id=event_id,
        )
