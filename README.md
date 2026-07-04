# HoneyTrade Protected Core

Private HoneyTrade quant runtime package.

This repository contains the proprietary execution, telemetry, and strategy logic that must not be distributed as plain source through the customer-facing `trade` repository.

**Browser auth (operators):** End users sign in on `web.trade` and the `trade` dashboard (same Supabase project; cross-app session sync). Pods use `HONEYTRADE_INGEST_TOKEN` / instance env — not browser cookies.

## SaaS telemetry (protected core)

`strategy.py` emits best-effort telemetry via optional `honeytrade_telemetry` (install from the `trade` repo `telemetry/` package in pod images). Hooks: `bot_start`, `_build_operating_snapshot`, `order_filled`.

Environment (injected by the K8s worker):

- `HONEYTRADE_TELEMETRY_INGEST_URL`
- `HONEYTRADE_INSTANCE_ID`
- `HONEYTRADE_ORGANIZATION_ID`
- `HONEYTRADE_INGEST_TOKEN`

Build wheel: `make build` or `./scripts/build-wheel.sh`.

Syntax check (no wheel build): `make verify`.

## Build

```bash
python3 -m pip install --upgrade build
python3 -m build
```

The wheel will be written to `dist/`.

## Delivery Workflow

1. Build a fresh wheel from this repository.
2. Copy the wheel into the delivery repository under `artifacts/`.
3. Build the customer engine image from the delivery repository.

The delivery repo imports `honeytrade_core` as an installed dependency and should only contain the thin Freqtrade adapter, licensing shell, Docker/runtime assets, and customer-safe documentation.

## SaaS Telemetry Integration (for sorunsuz customer experience)

For SaaS customers (via web.trade), the protected core must emit live trade/equity/log/health data to the trade repo's ingest-telemetry and trigger rich notifications.

See the authoritative integration guide and ready-to-apply patch in the trade repo:
- `telemetry/INTEGRATION_GUIDE_FOR_PROTECTED_CORE.md`
- `telemetry/PATCH_EXAMPLE_FOR_PROTECTED_CORE.md`
- `telemetry/telemetry_integration_example.py`

Key: Use safe wrappers (try/finally), propagate HONEYTRADE_CORRELATION_ID, emit from _build_operating_snapshot (for equity + health + metrics) and trade fill points (order_filled + Trade.get_trades_proxy).

**Sorunsuz Roadmap Complete (staff execution):** All internal scaffolding, automation, latency observability, evidence capture, 3-repo sync (MCP), docs, playbooks, and PRODUCTION_READINESS items for Phase 5/6 are done. .env and .env.example files are standardized across 3 repos with explanatory comments. Operator fills real Supabase secret key + .env values at the end, then runs the real Lemon test via prepare + runbook + capture for full evidence. 5.3 real pod validation ready with guides/PATCH + capture for proof.

**Latest validation (control plane):** MCP inspection of this repo (SHA 73fc438f...) confirms _build_operating_snapshot, order_filled, bot_loop, populate_indicators, _get_open_trades (Trade.get_trades_proxy) hooks are stable and match the guides exactly. No drift. First_live_data latency tracking now live in trade (ingest records seconds from created_at, notifier enriches Telegram/Email with "arrived in ~X min" + metadata). /status command now surfaces the latency value (Xs) for rich live status.

For 5.3 real pod validation: instrument a protected instance using the example wrapper + HONEYTRADE_INGEST_URL + token + CORRELATION_ID, trigger a real Lemon payment via web.trade, verify <10min live equity/trades/logs/health in dashboard (all SaaS warming states + pulse + realtime), rich first_trade in Telegram, /status (with latency) works with heartbeat. For the SUPABASE keys used in capture: use trade repo's standardized .env and .env.example (cp .env.example to .env and fill your real key VALUES at the very end; scripts auto-source .env if present). After emit: run `bash scripts/capture-sorunsuz-evidence.sh` (trade repo, with SUPABASE service role + org_id from PostCheckout + TELEGRAM_CHAT_ID) to auto-capture latency proof (7.1 [STRONG] <10min samples from metadata), telemetry counts, stale check, /status response, and pre-filled evidence template block. Fill evidence template. If any latency gaps in first_live_data or /status, use trade sorunsuz-incident-response.md latency triage subsection. This closes the sorunsuz E2E for real protected telemetry.

This enables the full seamless experience: real payment -> running pod -> live dashboard + Telegram without "no data" shock.
