# HoneyTrade Protected Core

Private HoneyTrade quant runtime package.

This repository contains the proprietary execution, telemetry, and strategy logic that must not be distributed as plain source through the customer-facing `trade` repository.

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

Key: Use safe wrappers (try/finally), propagate HONEYTRADE_CORRELATION_ID, emit from _build_operating_snapshot and trade fill points.

This enables the full seamless experience: real payment -> running pod -> live dashboard + Telegram without "no data" shock.
