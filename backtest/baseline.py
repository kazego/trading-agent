"""Explicit Phase 5G baseline-001 orchestration; no live trading imports."""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import pickle
import urllib.request

from backtest.data import SYMBOLS, Source, DataValidationError, canonical_decimal, ingest, _encoded, _hash
from backtest.simulation import CostMode, SimulationConfig, SymbolRules

ROOT = Path("artifacts/baseline-001")
URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
VERSION = "research-rules-v1"
REVISION = "1cdfa50654846cb2c8b6069a16b79bf5e65fac12"
COST_NOTICE = "RESEARCH ASSUMPTIONS: fee 0.0005 and adverse slippage 0.0002 per fill; not actual operator fees, guaranteed Binance fees, or exact historical transaction costs. Approximately 14 bps nominal round-trip friction; fees depend on fill notional."
RULE_NOTICE = "Current public rules are a STATIC RESEARCH ASSUMPTION for 2024-2025, not proof of historical rule identity. Only Phase 5D tick, market quantity and minimum-notional constraints are modeled; no complete exchange matching-engine emulation."
EXPECTED = dict(zip(SYMBOLS, (
    "4fc343f86d7f170c934de4881bbe7aeb2cfcea3a9835de4409156b825344d075",
    "841a2a44ca567cb648a105a56728b43efe47d292675100a18e3e3e19e49ae154",
    "a10e8fe6b13ce5c368174f82a4d29c331e95c166d14681e3c879c53860e2b0a1")))


def config():
    return SimulationConfig(cost_mode=CostMode.FEES_AND_SLIPPAGE,
                            entry_fee_rate="0.0005", exit_fee_rate="0.0005", slippage_bps="2")


def normalize_rules(payload):
    """Fail closed on missing/ambiguous filters; use intersecting market/lot limits."""
    selected, normalized = {}, {}
    for item in payload["symbols"]:
        symbol = item["symbol"]
        if symbol not in SYMBOLS:
            continue
        if symbol in selected or any(item[k] != v for k, v in
                (("contractType", "PERPETUAL"), ("quoteAsset", "USDT"), ("marginAsset", "USDT"), ("status", "TRADING"))):
            raise DataValidationError("Unexpected or duplicate symbol contract")
        filters = {}
        for f in item["filters"]:
            kind = f["filterType"]
            if kind in ("PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL"):
                if kind in filters:
                    raise DataValidationError("Duplicate filter")
                filters[kind] = f
        if set(filters) != {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL"}:
            raise DataValidationError("Missing or unsupported notional/quantity contract")
        def decimal(f, key):
            return Decimal(canonical_decimal(filters[f][key]))
        lot_step, market_step = (decimal(f, "stepSize") for f in ("LOT_SIZE", "MARKET_LOT_SIZE"))
        if lot_step != market_step or lot_step <= 0:
            raise DataValidationError("Different quantity grids require explicit review")
        minimum = max(decimal(f, "minQty") for f in ("LOT_SIZE", "MARKET_LOT_SIZE"))
        maximum = min(decimal(f, "maxQty") for f in ("LOT_SIZE", "MARKET_LOT_SIZE"))
        rule = SymbolRules(symbol, VERSION, decimal("PRICE_FILTER", "tickSize"),
                           lot_step, minimum, maximum, decimal("MIN_NOTIONAL", "notional"))
        selected[symbol] = filters
        normalized[symbol] = {k: canonical_decimal(v) if isinstance(v, Decimal) else v
                              for k, v in asdict(rule).items()}
    if set(selected) != set(SYMBOLS):
        raise DataValidationError("Missing configured symbol")
    result = dict(version=VERSION, source=URL, raw_filters=selected, normalized_rules=normalized,
                  mapping="PRICE_FILTER tick; shared LOT_SIZE/MARKET_LOT_SIZE step; intersected quantity bounds; MIN_NOTIONAL notional",
                  assumption=RULE_NOTICE)
    result["rules_fingerprint"] = _hash(result)
    return result


def write_json(path, value):
    with Path(path).open("xb") as stream:
        stream.write(_encoded(value) + b"\n")


def capture_rules(root=ROOT):
    output = root / "research-rules-v1.json"
    if output.exists():
        raise FileExistsError("Frozen rules already exist; no refresh allowed")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise DataValidationError("Redirect refused")
    with urllib.request.build_opener(NoRedirect).open(URL, timeout=30) as response:
        raw = response.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024:
        raise DataValidationError("Oversized exchangeInfo")
    rules = normalize_rules(json.loads(raw))
    rules["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
    write_json(output, rules)
    print("Frozen rules: " + rules["rules_fingerprint"], flush=True)


def load_inputs(root=ROOT):
    snapshot = json.loads((root / "research-rules-v1.json").read_text())
    deterministic = {k: v for k, v in snapshot.items() if k not in ("retrieved_at_utc", "rules_fingerprint")}
    if _hash(deterministic) != snapshot["rules_fingerprint"] or snapshot["source"] != URL:
        raise DataValidationError("Rules provenance mismatch")
    # Rebuild normalized values from their recorded public filters, not a second request.
    reconstructed = normalize_rules({"symbols": [dict(symbol=s, contractType="PERPETUAL", quoteAsset="USDT",
        marginAsset="USDT", status="TRADING", filters=list(snapshot["raw_filters"][s].values())) for s in SYMBOLS]})
    if reconstructed["rules_fingerprint"] != snapshot["rules_fingerprint"]:
        raise DataValidationError("Rules normalization mismatch")
    rules = {s: SymbolRules(**snapshot["normalized_rules"][s]) for s in SYMBOLS}
    history = root / "historical"
    acquisition = json.loads((history / "acquisition-manifest.json").read_text())
    if _hash({k: v for k, v in acquisition.items() if k != "fingerprint"}) != acquisition["fingerprint"]:
        raise DataValidationError("Acquisition manifest integrity failure")
    datasets = {}
    for symbol in SYMBOLS:
        sources = [Source(history / "normalized" / symbol / f"{symbol}-15m-{a['month']}.csv",
                          symbol, a["url"], a["normalized_csv_sha256"])
                   for a in acquisition["archives"] if a["symbol"] == symbol]
        dataset = ingest(sources, symbols=(symbol,), start_ms=1704067200000, end_ms=1767225600000,
                         as_of_ms=1767225600000, timestamp_unit="ms", warmup_candles=99)
        if (dataset.provenance["normalized_candle_fingerprint"] != EXPECTED[symbol]
                or dataset.fingerprint != acquisition["datasets"][symbol]["provenance"]["dataset_fingerprint"]):
            raise DataValidationError("Approved dataset fingerprint mismatch")
        datasets[symbol] = dataset
        print("Verified local dataset " + symbol, flush=True)
    return datasets, rules, snapshot, acquisition


def run(root=ROOT):
    # The exclusive directory is claimed BEFORE simulation: interruption never silently reruns it.
    output = root / "financial"
    if output.exists():
        raise FileExistsError("baseline-001 already started; preserve first result and inspect manually")
    datasets, rules, snapshot, acquisition = load_inputs(root)
    output.mkdir()
    from backtest.portfolio import simulate_portfolio, _canonical
    from backtest.report import build_report
    manifest = dict(run_id="baseline-001", configuration=_canonical(asdict(config())),
                    cost_assumption=COST_NOTICE, rule_assumption=RULE_NOTICE,
                    rules_fingerprint=snapshot["rules_fingerprint"],
                    acquisition_fingerprint=acquisition["fingerprint"],
                    normalized_candle_fingerprints=EXPECTED,
                    code_revision=REVISION, code_revision_note="Approved Phase 5F HEAD plus uncommitted Phase 5G orchestration",
                    source_sha256={str(p).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted([*Path('backtest').glob('*.py'), Path('strategy.py'), Path('requirements.txt')])})
    write_json(output / "approved-inputs.json", manifest)
    print("Starting the ONE approved baseline-001 simulation", flush=True)
    result = simulate_portfolio(datasets, rules, config())
    # Local trusted checkpoint preserves the first completed result if reporting is interrupted.
    with (output / "first-result.pickle").open("xb") as stream:
        pickle.dump(result, stream, protocol=5)
    print("Simulation completed and checkpoint preserved; building Phase 5F report", flush=True)
    report = build_report(result, datasets, code_revision=REVISION)
    report.write(output / "report.json")
    with (output / "report.txt").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("baseline-001\n" + COST_NOTICE + "\n" + RULE_NOTICE + "\n" + report.text())
    manifest["phase5f_manifest"] = json.loads(report.canonical_json)["manifest"]
    manifest["report_fingerprint"] = report.fingerprint
    manifest["manifest_fingerprint"] = _hash(manifest)
    write_json(output / "reproducibility-manifest.json", manifest)
    with (output / "report-fingerprint.txt").open("x", encoding="ascii") as stream:
        stream.write(report.fingerprint + "\n")
    print(report.text(), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--capture-public-rules", action="store_true")
    group.add_argument("--run-approved-baseline-001", action="store_true")
    args = parser.parse_args()
    capture_rules() if args.capture_public_rules else run()


if __name__ == "__main__":
    main()
