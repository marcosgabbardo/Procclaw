"""Trade Analytics Engine for ProcClaw.

Calculates trading metrics from stored events and trades.
Updates trade_job_stats after each event cycle.
"""

from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from procclaw.db import Database


class TradeAnalytics:
    """Calculates and updates trading metrics for trade jobs."""

    def __init__(self, db: "Database"):
        self.db = db

    def recalculate_stats(self, job_id: str, initial_capital: float = 1000.0) -> dict:
        """Recalculate all stats for a job and save to trade_job_stats.

        Args:
            job_id: The job ID
            initial_capital: Initial capital for PnL% calculation

        Returns:
            The calculated stats dict
        """
        # Get all closed trades
        closed_trades = self.db.get_all_trades_for_stats(job_id)
        open_trades = self.db.get_trades(job_id, status="open", limit=1000)

        # Get latest portfolio snapshot
        snapshots = self.db.get_portfolio_snapshots(job_id, limit=1)
        latest_snapshot = None
        if snapshots:
            # get_portfolio_snapshots orders by ts ASC, so get last
            all_snaps = self.db.get_portfolio_snapshots(job_id, limit=10000)
            if all_snaps:
                latest_snapshot = all_snaps[-1]

        # Get scan event counts
        scan_count = self.db.get_trade_events_count(job_id, event_type="scan")
        decision_count = self.db.get_trade_events_count(job_id, event_type="decision")

        # Calculate opportunities found from scan events
        opportunities_found = 0
        scan_events = self.db.get_trade_events(job_id, event_type="scan", limit=10000)
        for se in scan_events:
            try:
                data = json.loads(se["data"])
                opportunities_found += data.get("opportunities_found", 0)
            except (json.JSONDecodeError, KeyError):
                pass

        # Basic counts
        total_closed = len(closed_trades)
        total_open = len(open_trades)
        wins = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
        losses = [t for t in closed_trades if (t.get("pnl") or 0) < 0]
        breakeven = [t for t in closed_trades if (t.get("pnl") or 0) == 0]

        # PnL
        total_pnl = sum(t.get("pnl") or 0 for t in closed_trades)

        # Current capital from latest snapshot or calculate from trades
        current_capital = initial_capital + total_pnl
        if latest_snapshot:
            current_capital = latest_snapshot.get("current_capital") or current_capital
            initial_capital = latest_snapshot.get("initial_capital") or initial_capital

        total_pnl_pct = ((current_capital - initial_capital) / initial_capital * 100) if initial_capital > 0 else 0

        # Win rate
        win_rate = (len(wins) / total_closed * 100) if total_closed > 0 else 0

        # Average win/loss
        avg_win = statistics.mean([t.get("pnl") or 0 for t in wins]) if wins else 0
        avg_loss = statistics.mean([t.get("pnl") or 0 for t in losses]) if losses else 0

        # Best/worst trades
        best_trade_pnl = 0.0
        best_trade_market = ""
        worst_trade_pnl = 0.0
        worst_trade_market = ""
        if closed_trades:
            best = max(closed_trades, key=lambda t: t.get("pnl") or 0)
            worst = min(closed_trades, key=lambda t: t.get("pnl") or 0)
            best_trade_pnl = best.get("pnl") or 0
            best_trade_market = best.get("market", "")
            worst_trade_pnl = worst.get("pnl") or 0
            worst_trade_market = worst.get("market", "")

        # Average hold time
        hold_times = [t.get("hold_duration_hours") for t in closed_trades if t.get("hold_duration_hours")]
        avg_hold_hours = statistics.mean(hold_times) if hold_times else 0

        # Profit factor
        gross_wins = sum(t.get("pnl") or 0 for t in wins)
        gross_losses = abs(sum(t.get("pnl") or 0 for t in losses))
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (float("inf") if gross_wins > 0 else 0)
        if math.isinf(profit_factor):
            profit_factor = 999.99  # Cap for DB storage

        # Sharpe ratio (simplified: avg return per trade / stddev)
        returns = [t.get("pnl_pct") or 0 for t in closed_trades]
        if len(returns) >= 2:
            avg_return = statistics.mean(returns)
            std_return = statistics.stdev(returns)
            sharpe_ratio = (avg_return / std_return) if std_return > 0 else 0
        else:
            sharpe_ratio = 0

        # Max drawdown from portfolio snapshots
        max_drawdown_pct = self._calculate_max_drawdown(job_id)

        # Last scan/trade timestamps
        last_scan_at = None
        if scan_events:
            last_scan_at = scan_events[0].get("ts")  # Most recent (DESC order)

        last_trade_at = None
        if closed_trades:
            last_trade_at = closed_trades[-1].get("closed_at")  # Most recent (ASC order)

        now = datetime.now(timezone.utc).isoformat()

        stats = {
            "initial_capital": initial_capital,
            "current_capital": round(current_capital, 2),
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "win_rate": round(win_rate, 2),
            "total_trades": total_closed,
            "open_trades": total_open,
            "wins": len(wins),
            "losses": len(losses),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "best_trade_pnl": round(best_trade_pnl, 2),
            "best_trade_market": best_trade_market[:100] if best_trade_market else "",
            "worst_trade_pnl": round(worst_trade_pnl, 2),
            "worst_trade_market": worst_trade_market[:100] if worst_trade_market else "",
            "avg_hold_hours": round(avg_hold_hours, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "profit_factor": round(profit_factor, 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "scans_total": scan_count,
            "decisions_total": decision_count,
            "opportunities_found": opportunities_found,
            "last_scan_at": last_scan_at,
            "last_trade_at": last_trade_at,
            "updated_at": now,
        }

        # Save to DB
        self.db.upsert_trade_job_stats(job_id, stats)

        return stats

    def _calculate_max_drawdown(self, job_id: str) -> float:
        """Calculate max drawdown percentage from portfolio snapshots."""
        snapshots = self.db.get_portfolio_snapshots(job_id, limit=100000)
        if not snapshots:
            return 0

        peak = 0
        max_dd = 0

        for snap in snapshots:
            capital = snap.get("current_capital") or 0
            if capital > peak:
                peak = capital
            if peak > 0:
                dd = (peak - capital) / peak * 100
                if dd > max_dd:
                    max_dd = dd

        return max_dd

    def recalculate_all(self) -> int:
        """Recalculate stats for all trade jobs.

        Returns:
            Number of jobs recalculated
        """
        all_stats = self.db.get_all_trade_job_stats()
        job_ids = set(s["job_id"] for s in all_stats)

        # Also find jobs that have events but no stats yet
        # We do this by checking trade_events for distinct job_ids
        try:
            import sqlite3
            with self.db._connect() as conn:
                cursor = conn.execute("SELECT DISTINCT job_id FROM trade_events")
                for row in cursor.fetchall():
                    job_ids.add(row["job_id"])
        except Exception:
            pass

        count = 0
        for job_id in job_ids:
            try:
                self.recalculate_stats(job_id)
                count += 1
            except Exception as e:
                logger.warning(f"Failed to recalculate stats for {job_id}: {e}")

        return count
