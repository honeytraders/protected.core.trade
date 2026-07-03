"""Optional license guard for protected core runtime (calls trade verify-license contract)."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Dict

logger = logging.getLogger(__name__)


def verify_runtime_license(dry_run: bool = True) -> Dict[str, Any]:
    if dry_run:
        return {"ok": True, "skipped": "dry_run"}

    key = os.getenv("HONEYTRADE_LICENSE_KEY", "").strip()
    url = os.getenv("HONEYTRADE_LICENSE_URL", "").strip()
    if not url:
        base = os.getenv("SUPABASE_URL", "").rstrip("/")
        url = f"{base}/functions/v1/verify-license" if base else ""

    if not key or not url:
        return {"ok": False, "reason": "missing_license_config"}

    body = json.dumps(
        {
            "license_key": key,
            "instance_id": os.getenv("HONEYTRADE_INSTANCE_ID"),
            "machine_fingerprint": os.getenv("HONEYTRADE_MACHINE_FINGERPRINT"),
        }
    ).encode()

    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST"),
            timeout=5,
        ) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        logger.warning("license verify HTTP %s", exc.code)
        return {"ok": False, "reason": f"http_{exc.code}"}
    except Exception as exc:
        logger.warning("license verify failed: %s", exc)
        return {"ok": False, "reason": "network_error"}


def assert_live_allowed(dry_run: bool) -> None:
    result = verify_runtime_license(dry_run=dry_run)
    if dry_run:
        return
    if not result.get("ok"):
        raise SystemExit(f"HoneyTrade license check failed: {result.get('reason', 'denied')}")
    if result.get("live_mode_enabled") is False:
        raise SystemExit("Plan does not allow live trading.")
