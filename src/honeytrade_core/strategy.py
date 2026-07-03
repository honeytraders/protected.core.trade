import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd
import talib.abstract as ta
from freqtrade.persistence import Order, Trade
from pandas import DataFrame

try:
    from honeytrade_telemetry import (
        emit_equity_snapshot,
        emit_health,
        emit_important_log,
        emit_trade_filled,
    )
except ImportError:
    def emit_equity_snapshot(*_args, **_kwargs):  # type: ignore
        pass

    def emit_health(*_args, **_kwargs):  # type: ignore
        pass

    def emit_important_log(*_args, **_kwargs):  # type: ignore
        pass

    def emit_trade_filled(*_args, **_kwargs):  # type: ignore
        pass

logger = logging.getLogger(__name__)


class _SuppressBenignDIStepWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Could not find step di in pipeline, returning None" not in record.getMessage()


logging.getLogger("datasieve.pipeline").addFilter(_SuppressBenignDIStepWarning())

ASCII_LOGO = "\n".join(
    [
        " ██╗  ██╗ ██████╗ ███╗   ██╗███████╗██╗   ██╗████████╗██████╗  █████╗ ██████╗ ███████╗",
        " ██║  ██║██╔═══██╗████╗  ██║██╔════╝╚██╗ ██╔╝╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝",
        " ███████║██║   ██║██╔██╗ ██║█████╗   ╚████╔╝    ██║   ██████╔╝███████║██║  ██║█████╗",
        " ██╔══██║██║   ██║██║╚██╗██║██╔══╝    ╚██╔╝     ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝",
        " ██║  ██║╚██████╔╝██║ ╚████║███████╗   ██║      ██║   ██║  ██║██║  ██║██████╔╝███████╗",
        " ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝╚══════╝   ╚═╝      ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝",
    ]
)


class HoneyTradeStrategyCore:
    def _utc_now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _as_utc(self, value: Optional[datetime]) -> datetime:
        target = value or self._utc_now()
        if target.tzinfo is None:
            return target.replace(tzinfo=timezone.utc)
        return target.astimezone(timezone.utc)

    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_minutes": 15},
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 48,
                "trade_limit": 1,
                "stop_duration_minutes": 1440,
                "max_allowed_drawdown": 0.05,
            },
        ]

    def _get_honeytrade_settings(self) -> Dict:
        defaults = {
            "telegram": {
                "heartbeat_interval_minutes": 60,
                "command_poll_interval_seconds": 15,
            },
            "risk": {
                "base_position_size": 35.0,
                "capital_growth_fraction": 0.05,
                "max_position_size": 180.0,
                "max_position_fraction": 0.14,
                "conviction_floor": 0.85,
                "conviction_ceiling": 1.15,
                "volatility_floor": 0.88,
                "volatility_ceiling": 1.02,
            },
        }

        overrides = self.config.get("honeytrade", {})
        merged = json.loads(json.dumps(defaults))
        for section, values in overrides.items():
            if isinstance(values, dict) and isinstance(merged.get(section), dict):
                merged[section].update(values)
            else:
                merged[section] = values
        return merged

    def _get_wallet_value(self, method_name: str) -> float:
        wallets = getattr(self, "wallets", None)
        if wallets is None:
            return 0.0

        method = getattr(wallets, method_name, None)
        if not callable(method):
            return 0.0

        try:
            return float(method() or 0.0)
        except Exception:
            return 0.0

    def _get_wallet_context(self) -> tuple[float, float]:
        total_stake = self._get_wallet_value("get_total_stake_amount")
        available_stake = self._get_wallet_value("get_available_stake_amount")

        if total_stake <= 0:
            dry_run_wallet = float(self.config.get("dry_run_wallet", 0) or 0)
            utilization = float(self.config.get("tradable_balance_ratio", 0) or 0)
            total_stake = dry_run_wallet * utilization if dry_run_wallet > 0 and utilization > 0 else dry_run_wallet

        if available_stake <= 0:
            available_stake = total_stake

        return max(total_stake, 0.0), max(available_stake, 0.0)

    def _format_money(self, value: float) -> str:
        if abs(value) >= 1:
            return f"${value:,.2f}"
        return f"${value:,.4f}"

    def _get_sample_confidence(self, closed_trade_count: int) -> str:
        if closed_trade_count >= 30:
            return "HIGH"
        if closed_trade_count >= 10:
            return "MEDIUM"
        return "LOW"

    def _get_lifecycle_phase(self, closed_trade_count: int) -> str:
        if closed_trade_count >= 30:
            return "ACTIVE"
        return "OBSERVATION"

    def _get_closed_trades(self) -> List[Trade]:
        return list(Trade.get_trades_proxy(is_open=False))

    def _get_open_trades(self) -> List[Trade]:
        return list(Trade.get_trades_proxy(is_open=True))

    def _build_operating_snapshot(self, current_time: Optional[datetime] = None) -> Dict:
        stamp = self._as_utc(current_time)
        open_trades = self._get_open_trades()
        closed_trades = self._get_closed_trades()
        total_stake, available_stake = self._get_wallet_context()
        closed_trade_count = len(closed_trades)
        metrics = self.calculate_quant_metrics(closed_trades)

        previous = getattr(self, "_last_operating_snapshot", None) or {}
        previous_closed_count = int(previous.get("closed_trade_count", 0) or 0)

        if closed_trade_count < previous_closed_count:
            closed_trade_count = previous_closed_count
            metrics = previous.get("metrics", metrics)

        sample_confidence = self._get_sample_confidence(closed_trade_count)
        lifecycle_phase = self._get_lifecycle_phase(closed_trade_count)

        snapshot = {
            "timestamp": stamp,
            "open_trades": open_trades,
            "open_trade_count": len(open_trades),
            "closed_trade_count": closed_trade_count,
            "total_stake": total_stake,
            "available_stake": available_stake,
            "metrics": metrics,
            "sample_confidence": sample_confidence,
            "lifecycle_phase": lifecycle_phase,
            "metrics_ready": closed_trade_count >= 5,
            "metrics_preliminary": 5 <= closed_trade_count < 30,
        }

        self._last_operating_snapshot = snapshot

        try:
            emit_equity_snapshot(total_stake, available_stake, total_stake)
            emit_health(
                open_trades=len(open_trades),
                closed_trade_count=closed_trade_count,
                lifecycle_phase=lifecycle_phase,
            )
        except Exception:
            pass

        return snapshot

    def _get_telegram_keyboard(self) -> List[List[str]]:
        keyboard = self.config.get("telegram", {}).get("keyboard")
        if isinstance(keyboard, list) and keyboard:
            return [[str(item) for item in row] for row in keyboard if isinstance(row, list)]
        return [
            ["/status", "/balance", "/profit"],
            ["/positions", "/logs", "/health"],
            ["/show_config", "/help"],
        ]

    def _get_telegram_target(self) -> Optional[tuple[str, str]]:
        telegram_cfg = self.config.get("telegram", {})
        token = telegram_cfg.get("token")
        chat_id = telegram_cfg.get("chat_id")
        if not token or not chat_id:
            return None
        return str(token), str(chat_id)

    def _telegram_api_request(self, method: str, payload: Optional[Dict] = None) -> Optional[Dict]:
        target = self._get_telegram_target()
        if not target:
            return None

        token, _ = target
        normalized_payload: Dict[str, str] = {}
        for key, value in (payload or {}).items():
            if isinstance(value, (dict, list)):
                normalized_payload[key] = json.dumps(value)
            elif value is None:
                continue
            else:
                normalized_payload[key] = str(value)

        encoded_payload = urllib.parse.urlencode(normalized_payload).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=encoded_payload,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                if response.status >= 400:
                    logger.warning("Telegram API request %s failed with HTTP %s", method, response.status)
                    return None
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {"ok": True}
        except Exception as exc:
            logger.warning("Telegram API request %s failed: %s", method, exc)
            return None

    def _send_telegram_message(self, message: str, include_keyboard: bool = False, html: bool = False) -> None:
        target = self._get_telegram_target()
        if not target:
            return

        _, chat_id = target
        payload: Dict = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }

        if html:
            payload["parse_mode"] = "HTML"

        if include_keyboard:
            payload["reply_markup"] = {
                "keyboard": self._get_telegram_keyboard(),
                "resize_keyboard": True,
                "one_time_keyboard": False,
            }

        self._telegram_api_request("sendMessage", payload)

    def _build_telegram_snapshot(self, current_time: Optional[datetime] = None) -> str:
        profile_name = self.config.get("bot_name", "Unknown")
        mode = "DRY-RUN" if self.config.get("dry_run", True) else "LIVE"
        balance_ratio = float(self.config.get("tradable_balance_ratio", 0) or 0)
        max_trades = int(self.config.get("max_open_trades", 0) or 0)
        snapshot = self._build_operating_snapshot(current_time)
        metrics = snapshot["metrics"]

        lines = [
            "HoneyTrade · status",
            f"Profile: {profile_name}",
            f"Mode: {mode}",
            f"Session: {self._session_id or 'UNSET'}",
            f"Phase: {snapshot['lifecycle_phase']}",
            f"Sample Confidence: {snapshot['sample_confidence']}",
            f"Closed Trades: {snapshot['closed_trade_count']}",
            f"Open Trades: {snapshot['open_trade_count']}/{max_trades}",
            f"Capital Utilization: {balance_ratio:.0%}",
            f"Managed Capital: {self._format_money(float(snapshot['total_stake']))}",
            f"Available Capital: {self._format_money(float(snapshot['available_stake']))}",
            f"Timestamp: {snapshot['timestamp'].strftime('%Y-%m-%d %H:%M:%S UTC')}",
        ]

        if snapshot["metrics_ready"] and isinstance(metrics, dict):
            lines.extend(
                [
                    f"Win Rate: {metrics.get('Win Rate', '0.00%')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                    f"Sharpe: {metrics.get('Sharpe Ratio', '0.00')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                    f"Profit Factor: {metrics.get('Profit Factor', '0.00')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                    f"Expectancy: {metrics.get('Expectancy', '0.00%')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                ]
            )
        else:
            lines.append("Metrics: collecting minimum sample")

        return "\n".join(lines)

    def _build_balance_snapshot(self) -> str:
        total_stake, available_stake = self._get_wallet_context()
        utilization = float(self.config.get("tradable_balance_ratio", 0) or 0)
        max_trades = int(self.config.get("max_open_trades", 0) or 0)
        mode = "DRY-RUN" if self.config.get("dry_run", True) else "LIVE"
        return "\n".join(
            [
                "HoneyTrade · balance",
                f"Mode: {mode}",
                f"Session: {self._session_id or 'UNSET'}",
                f"Managed Capital: {self._format_money(total_stake)}",
                f"Available Capital: {self._format_money(available_stake)}",
                f"Capital Utilization Limit: {utilization:.0%}",
                f"Max Concurrent Trades: {max_trades}",
            ]
        )

    def _build_profit_snapshot(self) -> str:
        snapshot = self._build_operating_snapshot()
        closed_trades = self._get_closed_trades()
        metrics = snapshot["metrics"]
        total_pnl = sum(float(getattr(trade, "close_profit_abs", 0) or 0) for trade in closed_trades)

        lines = [
            "HoneyTrade · performance",
            f"Session: {self._session_id or 'UNSET'}",
            f"Phase: {snapshot['lifecycle_phase']}",
            f"Sample Confidence: {snapshot['sample_confidence']}",
            f"Closed Trades: {snapshot['closed_trade_count']}",
            f"Realized P&L: {self._format_money(total_pnl)}",
        ]

        if snapshot["metrics_ready"] and isinstance(metrics, dict):
            lines.extend(
                [
                    f"Win Rate: {metrics.get('Win Rate', '0.00%')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                    f"Sharpe: {metrics.get('Sharpe Ratio', '0.00')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                    f"Profit Factor: {metrics.get('Profit Factor', '0.00')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                    f"Expectancy: {metrics.get('Expectancy', '0.00%')}{' (preliminary)' if snapshot['metrics_preliminary'] else ''}",
                ]
            )
        else:
            lines.append("Metrics: collecting minimum sample")

        return "\n".join(lines)

    def _build_positions_snapshot(self) -> str:
        open_trades = Trade.get_trades_proxy(is_open=True)
        if not open_trades:
            return "HoneyTrade · positions\nNo active positions."

        lines = ["HoneyTrade · positions"]
        for trade in open_trades[:5]:
            pair = getattr(trade, "pair", "UNKNOWN")
            amount = float(getattr(trade, "amount", 0) or 0)
            open_rate = float(getattr(trade, "open_rate", 0) or 0)
            current_rate = float(getattr(trade, "open_rate_requested", open_rate) or open_rate)
            profit_pct = float(getattr(trade, "close_profit", 0) or 0)
            lines.extend(
                [
                    f"{pair} · {amount:.6f}",
                    f"Entry: {open_rate:.6f}",
                    f"Mark: {current_rate:.6f}",
                    f"P&L: {profit_pct:.2%}",
                ]
            )
        return "\n".join(lines)

    def _build_config_snapshot(self) -> str:
        profile_name = self.config.get("bot_name", "Unknown")
        mode = "DRY-RUN" if self.config.get("dry_run", True) else "LIVE"
        max_trades = int(self.config.get("max_open_trades", 0) or 0)
        whitelist = self.config.get("exchange", {}).get("pair_whitelist", [])
        controls = self._get_honeytrade_settings()["risk"]
        return "\n".join(
            [
                "HoneyTrade · configuration",
                f"Profile: {profile_name}",
                f"Mode: {mode}",
                f"Session: {self._session_id or 'UNSET'}",
                f"Timeframe: {self.timeframe}",
                f"Max Trades: {max_trades}",
                f"Pairs: {len(whitelist)}",
                "Sizing Model: capped compound",
                f"Base Ticket: {self._format_money(float(controls.get('base_position_size', 0) or 0))}",
                f"Hard Cap: {self._format_money(float(controls.get('max_position_size', 0) or 0))}",
            ]
        )

    def _build_health_snapshot(self) -> str:
        profile_name = self.config.get("bot_name", "Unknown")
        mode = "DRY-RUN" if self.config.get("dry_run", True) else "LIVE"
        snapshot = self._build_operating_snapshot()
        return "\n".join(
            [
                "HoneyTrade · health",
                f"Profile: {profile_name}",
                f"Mode: {mode}",
                f"Session: {self._session_id or 'UNSET'}",
                f"Phase: {snapshot['lifecycle_phase']}",
                f"Sample Confidence: {snapshot['sample_confidence']}",
                f"Open Trades: {snapshot['open_trade_count']}",
                f"Closed Trades: {snapshot['closed_trade_count']}",
                f"Managed Capital: {self._format_money(float(snapshot['total_stake']))}",
                f"Available Capital: {self._format_money(float(snapshot['available_stake']))}",
                "State: engine online and telemetry active",
            ]
        )

    def _tail_recent_logs(self, limit: int = 5) -> str:
        path = Path("/freqtrade/user_data/logs/honeytrade.log")
        if not path.exists():
            return "HoneyTrade · logs\nLog file not available yet."

        lines = [line.strip() for line in path.read_text(errors="ignore").splitlines() if line.strip()]
        if not lines:
            return "HoneyTrade · logs\nNo log lines available."

        return "HoneyTrade · logs\n" + "\n".join(lines[-limit:])

    def _build_help_snapshot(self) -> str:
        return "\n".join(
            [
                "HoneyTrade · commands",
                "/status - operating snapshot",
                "/balance - capital summary",
                "/profit - realized performance",
                "/positions - open positions",
                "/logs - recent runtime logs",
                "/health - system health",
                "/show_config - active profile",
                "/help - command reference",
            ]
        )

    def _build_start_snapshot(self) -> str:
        profile_name = self.config.get("bot_name", "Unknown")
        max_trades = self.config.get("max_open_trades", 0)
        balance_ratio = float(self.config.get("tradable_balance_ratio", 0) or 0)
        mode = "DRY-RUN" if self.config.get("dry_run", True) else "LIVE"
        runtime_note = "Simulation mode active." if self.config.get("dry_run", True) else "Live execution active."

        return (
            f"<pre>{ASCII_LOGO}</pre>\n"
            "HoneyTrade · startup\n"
            f"Profile: {profile_name}\n"
            f"Mode: {mode}\n"
            f"Session: {self._session_id or 'UNSET'}\n"
            f"Timeframe: {self.timeframe}\n"
            f"Max Trades: {max_trades}\n"
            f"Capital Limit: {balance_ratio:.0%}\n"
            "State: engine online\n"
            "Status Feed: reset on engine restart\n"
            f"Note: {runtime_note}\n"
        )

    def _handle_telegram_command(self, text: str, current_time: datetime) -> Optional[str]:
        command = text.strip().split()[0].lower()
        aliases = {
            "/daily": "/status",
            "/weekly": "/status",
            "/monthly": "/status",
            "/performance": "/profit",
            "/stats": "/profit",
            "/count": "/positions",
            "/show_conf": "/show_config",
        }
        command = aliases.get(command, command)

        if command == "/start":
            return self._build_start_snapshot()
        if command == "/help":
            return self._build_help_snapshot()
        if command == "/status":
            return self._build_telegram_snapshot(current_time)
        if command == "/balance":
            return self._build_balance_snapshot()
        if command == "/profit":
            return self._build_profit_snapshot()
        if command == "/positions":
            return self._build_positions_snapshot()
        if command == "/logs":
            return self._tail_recent_logs()
        if command == "/health":
            return self._build_health_snapshot()
        if command == "/show_config":
            return self._build_config_snapshot()
        return None

    def _prime_telegram_update_offset(self) -> None:
        response = self._telegram_api_request(
            "getUpdates",
            {
                "timeout": 0,
                "limit": 20,
                "allowed_updates": ["message"],
            },
        )
        results = response.get("result", []) if isinstance(response, dict) else []
        if results:
            self._telegram_update_offset = int(results[-1]["update_id"]) + 1
        else:
            self._telegram_update_offset = 0

    def _poll_telegram_commands(self, current_time: datetime) -> None:
        current_time = self._as_utc(current_time)
        settings = self._get_honeytrade_settings()["telegram"]
        interval_seconds = int(settings.get("command_poll_interval_seconds", 15) or 15)
        if (
            self._last_telegram_command_poll_at is not None
            and (current_time - self._last_telegram_command_poll_at).total_seconds() < interval_seconds
        ):
            return

        self._last_telegram_command_poll_at = current_time

        if self._telegram_update_offset is None:
            self._prime_telegram_update_offset()
            return

        target = self._get_telegram_target()
        if not target:
            return
        _, expected_chat_id = target

        response = self._telegram_api_request(
            "getUpdates",
            {
                "timeout": 0,
                "limit": 10,
                "offset": self._telegram_update_offset,
                "allowed_updates": ["message"],
            },
        )
        if not isinstance(response, dict):
            return

        for update in response.get("result", []):
            update_id = int(update.get("update_id", 0))
            self._telegram_update_offset = max(self._telegram_update_offset, update_id + 1)
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if chat_id != expected_chat_id:
                continue

            text = str(message.get("text", "")).strip()
            if not text.startswith("/"):
                continue

            reply = self._handle_telegram_command(text, current_time)
            if reply:
                self._send_telegram_message(
                    reply,
                    include_keyboard=text.lower().startswith(("/start", "/help")),
                    html=text.lower().startswith("/start"),
                )

    def bot_start(self, **kwargs) -> None:
        self._session_id = self._utc_now().strftime("%Y%m%dT%H%M%SZ")
        self._last_telegram_heartbeat_at = self._utc_now()
        self._last_telegram_command_poll_at = None
        self._telegram_update_offset = None
        self._send_telegram_message(self._build_start_snapshot(), include_keyboard=True, html=True)
        try:
            total, available = self._get_wallet_context()
            emit_equity_snapshot(total, available, total)
            emit_health(open_trades=0, event="bot_start")
            emit_important_log("info", "HoneyTrade core online", session_id=self._session_id)
        except Exception:
            pass

    def bot_loop_start(self, current_time: datetime, **kwargs) -> None:
        current_time = self._as_utc(current_time)
        self._poll_telegram_commands(current_time)

        heartbeat_interval_minutes = int(
            self._get_honeytrade_settings()["telegram"].get("heartbeat_interval_minutes", 60) or 60
        )
        should_send = (
            self._last_telegram_heartbeat_at is None
            or (current_time - self._last_telegram_heartbeat_at).total_seconds() >= heartbeat_interval_minutes * 60
        )
        if not should_send:
            return

        self._last_telegram_heartbeat_at = current_time
        self._send_telegram_message(self._build_telegram_snapshot(current_time))

    def calculate_quant_metrics(self, trades: Optional[List[Trade]] = None) -> Dict:
        trades = list(trades) if trades is not None else self._get_closed_trades()
        if not trades:
            return {}

        returns = [float(getattr(trade, "close_profit", 0) or 0) for trade in trades]
        df_returns = pd.Series(returns)
        win_rate = len(df_returns[df_returns > 0]) / len(df_returns)
        profit_factor = abs(df_returns[df_returns > 0].sum() / df_returns[df_returns < 0].sum()) if any(df_returns < 0) else 10.0
        expectancy = df_returns.mean()
        std_dev = df_returns.std()
        sharpe = (df_returns.mean() / std_dev) * np.sqrt(252) if std_dev > 0 else 0
        return {
            "Win Rate": f"{win_rate:.2%}",
            "Sharpe Ratio": f"{sharpe:.2f}",
            "Profit Factor": f"{profit_factor:.2f}",
            "Expectancy": f"{expectancy:.2%}",
        }

    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: Optional[float],
        max_stake: float,
        leverage: float,
        entry_tag: Optional[str],
        side: str,
        **kwargs,
    ) -> float:
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe.empty:
            return proposed_stake

        last_row = dataframe.iloc[-1]
        prediction = float(last_row.get("&-s_close_change", 0) or 0)
        atr = float(last_row.get("atr", 0) or 0)
        avg_atr = float(dataframe["atr"].rolling(window=100).mean().iloc[-1] or 0) if "atr" in dataframe else 0

        controls = self._get_honeytrade_settings()["risk"]
        total_stake, available_stake = self._get_wallet_context()
        equity_reference = total_stake if total_stake > 0 else max(max_stake, proposed_stake, 0)

        conviction_mult = max(
            float(controls.get("conviction_floor", 0.85)),
            min(float(controls.get("conviction_ceiling", 1.15)), 1 + (prediction / 0.02)),
        )

        vol_mult = (
            max(
                float(controls.get("volatility_floor", 0.88)),
                min(float(controls.get("volatility_ceiling", 1.02)), avg_atr / atr),
            )
            if atr > 0 and avg_atr > 0
            else 1.0
        )

        progressive_target = float(controls.get("base_position_size", 35.0)) + (
            equity_reference * float(controls.get("capital_growth_fraction", 0.05))
        )
        fractional_cap = equity_reference * float(controls.get("max_position_fraction", 0.14))
        hard_cap = float(controls.get("max_position_size", progressive_target))
        operational_cap = min(hard_cap, fractional_cap if fractional_cap > 0 else hard_cap)

        target_stake = min(progressive_target, operational_cap)
        if max_stake > 0:
            target_stake = min(target_stake, max_stake)
        if available_stake > 0:
            target_stake = min(target_stake, available_stake)

        final_stake = target_stake * conviction_mult * vol_mult
        if max_stake > 0:
            final_stake = min(final_stake, max_stake)
        if available_stake > 0:
            final_stake = min(final_stake, available_stake)

        if min_stake and final_stake < min_stake:
            return 0.0

        return max(0.0, final_stake)

    def order_filled(self, pair: str, trade: Trade, order: Order, current_time: datetime, **kwargs) -> None:
        side = getattr(order, "ft_order_side", None) or getattr(order, "side", "unknown")
        price = getattr(order, "price", None) or getattr(order, "average", None) or 0
        amount = getattr(order, "amount", None) or trade.amount or 0

        if not trade.is_open:
            pnl_abs = getattr(trade, "close_profit_abs", 0) or 0
            pnl_ratio = getattr(trade, "close_profit", 0) or 0
            message = (
                "HoneyTrade · exit\n"
                f"Pair: {pair}\n"
                f"Side: {side}\n"
                f"Size: {amount:.6f}\n"
                f"Price: {price:.6f}\n"
                f"P&L: {pnl_abs:.2f} ({pnl_ratio:.2%})\n"
                f"Time: {current_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
        else:
            message = (
                "HoneyTrade · entry\n"
                f"Pair: {pair}\n"
                f"Side: {side}\n"
                f"Size: {amount:.6f}\n"
                f"Price: {price:.6f}\n"
                f"Time: {current_time.strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )

        self._send_telegram_message(message)

        try:
            emit_trade_filled(trade, order, current_time)
        except Exception:
            pass

    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int, metadata: Dict, **kwargs) -> DataFrame:
        dataframe[f"%-rsi-period_{period}"] = ta.RSI(dataframe, timeperiod=period)
        dataframe[f"%-atr-period_{period}"] = ta.ATR(dataframe, timeperiod=period)
        dataframe[f"%-ema-period_{period}"] = ta.EMA(dataframe, timeperiod=period)
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: Dict, **kwargs) -> DataFrame:
        dataframe["&-s_close_change"] = (
            dataframe["close"].shift(-self.freqai_info["feature_parameters"]["label_period_candles"])
            / dataframe["close"]
            - 1
        )
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: Dict) -> DataFrame:
        dataframe = self.freqai.start(dataframe, metadata, self)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: Dict) -> DataFrame:
        if "&-s_close_change" not in dataframe.columns or "ema200" not in dataframe.columns:
            return dataframe

        predict_mask = dataframe["do_predict"].eq(1) if "do_predict" in dataframe.columns else pd.Series(True, index=dataframe.index)
        di_mask = dataframe["DI_values"].lt(0.9) if "DI_values" in dataframe.columns else pd.Series(True, index=dataframe.index)

        dataframe.loc[
            (
                (dataframe["&-s_close_change"] > 0.004)
                & (dataframe["close"] > dataframe["ema200"])
                & predict_mask
                & di_mask
            ),
            "enter_long",
        ] = 1
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: Dict) -> DataFrame:
        if "&-s_close_change" not in dataframe.columns:
            return dataframe

        dataframe.loc[(dataframe["&-s_close_change"] < -0.001), "exit_long"] = 1
        return dataframe
