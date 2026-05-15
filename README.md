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
