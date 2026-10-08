"""Connectivity check used by `./run.sh check`."""
from __future__ import annotations

import logging
import sys

from backend.analytics.regime import classify_regime, compute_market_metrics
from backend.config import settings
from backend.logging_setup import setup_logging
from backend.market_data.indices import IndexHistoryProvider
from backend.market_data.longbridge import LongbridgeClient, LongbridgeError


def main() -> int:
    setup_logging(settings)
    log = logging.getLogger("backend.check")
    ok = True

    indices = IndexHistoryProvider(settings.data_dir, float(settings.get("data.index_cache_ttl_minutes", 30)))
    try:
        vix, skew = indices.vix(), indices.skew()
        metrics = compute_market_metrics(vix, skew, settings.section("thresholds"))
        regime = classify_regime(metrics)
        print(f"VIX  {metrics.vix.value:.2f} ({metrics.vix.date})  level={metrics.vix.level}  IV rank={metrics.vix.iv_rank:.0f} ({metrics.iv_rank_level})")
        print(f"SKEW {metrics.skew.value:.2f} ({metrics.skew.date})  level={metrics.skew.level}  percentile={metrics.skew.percentile:.0f}")
        print(f"Regime: {regime.title} (exact={regime.exact_match}, confidence={regime.confidence})")
        for note in regime.notes:
            print(f"  note: {note}")
    except Exception as exc:
        ok = False
        print(f"Index data FAILED: {exc}")

    if not settings.longbridge_credentials_present:
        ok = False
        print("Longbridge credentials missing in environment/.env (LONGPORT_APP_KEY, LONGPORT_APP_SECRET, LONGPORT_ACCESS_TOKEN)")
    else:
        client = LongbridgeClient(settings)
        try:
            info = client.ping()
            print(f"Longbridge OK: {info['symbol']} last={info['last']} at {info['timestamp']}")
        except LongbridgeError as exc:
            ok = False
            print(f"Longbridge FAILED: {exc}")
            log.error("Longbridge check failed: %s", exc)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
