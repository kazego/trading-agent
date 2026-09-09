# Phase 6C — completed DEVELOPMENT evidence

## Scope

Exactly three registered experiments and three completed attempts: EMA 9/21 at 15m, 1h and 4h signal timeframes. Zero retries. Evaluation: **[2024-01-01T00:00:00Z, 2025-01-01T00:00:00Z)**, DEVELOPMENT only. Execution resolution: 15m. Frozen completed-candle semantics and pandas `ewm(adjust=False)` remain unchanged.

BTCUSDT / ETHUSDT / ADAUSDT; LONG + SHORT; BTC → ETH → ADA priority; maximum one concurrent position total. Each run starts flat at 1000 USDT, risk 1%, maximum leverage 3x, SL 1.5%, TP 3%. Modeled fee 0.0005/fill and adverse slippage 0.0002/fill. Funding and liquidation excluded. Static current `research-rules-v1` applied retrospectively.

## Canonical results

Values below are copied from stored scorecard fields without rounding or recalculating financial results. Return and drawdowns are percentages. Monetary values are USDT.

Financial results

| Field | 15m | 1h | 4h |
|---|---|---|---|
| Final equity | 523.686859506117 | 810.774227135196 | 1039.539766095443 |
| Return % | -47.6313140493883 | -18.9225772864804 | 3.9539766095443 |
| Gross P&L | -202.452916058 | 29.144032996 | 145.143092102 |
| Net P&L | -476.313140493883 | -189.225772864804 | 39.539766095443 |
| Trades | 541 | 375 | 174 |
| Win rate % | 31.977818853974121996303142329020332717190388170055 | 34.133333333333333333333333333333333333333333333333 | 37.356321839080459770114942528735632183908045977011 |
| Gross expectancy | -0.37421980787060998151571164510166358595194085027726 | 0.077717421322666666666666666666666666666666666666667 | 0.8341557017356321839080459770114942528735632183908 |
| Net expectancy | -0.8804309436116136783733826247689463955637707948244 | -0.50460206097281066666666666666666666666666666666667 | 0.22724003503128160919540229885057471264367816091954 |
| Gross PF | 0.92842568297648166141108290995629292839911009884184 | 1.0133088145560137870081113186116289259110694316594 | 1.1435251207871650112486928062674616525420129277044 |
| Net PF | 0.84197420482000224976115210253187953618938396257797 | 0.91892399450840374062761737091920601831120653108317 | 1.0366910259865499768396867483920909192149692634294 |
| Max MTM DD % | 52.439800616894335851823692380001632498014253562025 | 26.307178267512395972881076561189051440343264107436 | 14.018266433560507256644408715563595382858252032623 |
| Max realized DD % | 52.208592172180628520418424026489427892776492268457 | 25.660510384957815647408711062917050276824822455367 | 13.649519614075448057199198484294740800258582646327 |
| Fees | 273.860224435883 | 218.369805860804 | 105.603326006557 |
| Recorded slippage | 109.544086058 | 87.347927004 | 42.241337898 |
| Positive quarters | 0 | 1 | 2 |
| Positive months (count from stored monthly signs) | 3 | 5 | 6 |
| Positive-month fraction (stored) | 0.25 | 0.41666666666666666666666666666666666666666666666667 | 0.5 |
| Evidence status | INELIGIBLE | INELIGIBLE | INELIGIBLE |

## Main attribution

| Field | 15m | 1h | 4h |
|---|---|---|---|
| BTCUSDT net P&L | -110.54601431935 | -124.31654421306 | -45.41895576379 |
| ETHUSDT net P&L | -261.990185226103 | -69.182746922464 | -95.038387194977 |
| ADAUSDT net P&L | -103.77694094843 | 4.27351827072 | 179.99710905421 |
| LONG net P&L | -217.941518841936 | -149.877280409472 | -46.863573898966 |
| SHORT net P&L | -258.371621651947 | -39.348492455332 | 86.403339994409 |
| accepted_entries | 541 | 375 | 174 |
| BLOCKED_OPEN_POSITION | 4319 | 749 | 75 |
| BLOCKED_PRIORITY | 86 | 56 | 15 |

4h ADA SHORT: net `172.46507486225`, gross `189.93465862`, trades `29`.
4h best month: `2024-10`, net `145.01502949545`. Worst month: `2024-12`, net `-101.555262263615`.

## Eligibility

- 15m: `NONPOSITIVE_NET_EXPECTANCY`, `NET_PF_NOT_ABOVE_ONE`, `FEWER_THAN_THREE_POSITIVE_QUARTERS`, `MTM_DRAWDOWN_CAP`.
- 1h: `NONPOSITIVE_NET_EXPECTANCY`, `NET_PF_NOT_ABOVE_ONE`, `FEWER_THAN_THREE_POSITIVE_QUARTERS`, `MTM_DRAWDOWN_CAP`.
- 4h: `FEWER_THAN_THREE_POSITIVE_QUARTERS`.

No run was eligible. All exceeded the 100-trade evidence target, which remains a target rather than a hard rejection threshold. No candidate was nominated; no validation occurred. The policy was not reinterpreted after observing results.

## Interpretation

On the declared Pareto axes (higher return, higher net expectancy, lower MTM drawdown), 4h dominates 1h and 15m; 1h dominates 15m. Slower signal timeframes reduced turnover and modeled friction. 4h became net-positive on 2024 DEVELOPMENT, but its improvement was not broad: BTC and ETH remained net-negative, LONG overall remained net-negative, and ADA SHORT supplied the majority of positive contribution, offset by losses elsewhere. October 4h net profit exceeded full-year 4h net profit. Only two 4h quarters were positive, so the frozen eligibility policy rejects it.

These correlated observations from the same already observed DEVELOPMENT period do not establish robustness or future profitability. No scalar ranking or automatic promotion was used.

## Immutable identities

### 15m

- experiment_fingerprint: `94effde761681b2133e4164e25886b6463af735ca51407044c3c88e93f071ee7`
- run_fingerprint: `8dff359cd66a9234687eb047046b4e8bf3c4913802a3b51a3b662241a9013972`
- strategy_fingerprint: `9aa938ddb1f0e5d911278cce2262e3ab8fe9299f329de483b6cb41ffe0acfd76`
- rules_fingerprint: `e382b5da9ad38a6836e24d488fb8306e61b8849906000ec015d3784486802872`
- portfolio_result_fingerprint: `2350cb7d159601eab8c83d3daf977ee0da4069dbe06d8cd043c4c4c091ca91f3`
- scorecard_fingerprint: `e9e14db8d14b9a9a5eb25685d083f46bdc92ab6890ebf0857a12dc1a1f25c9fd`
- manifest_fingerprint: `16f158c13c456d6e0fc05b452fa6690881928053b27e58c366fc4712afa31074`
- dataset_fingerprints ADAUSDT: `a7d2d582fa6171e5ee6ca5cc285c1f08dabf5c9b2543c49e1b49fa4f59cfea24`
- dataset_fingerprints BTCUSDT: `566c94bf3dd152fc4636392edfdbf322277d0c34fefd3d6ca6b7171c8b0f1980`
- dataset_fingerprints ETHUSDT: `a3a854c9d19bb4970faf235f9dff76d455e7af1376ff79cf58357e0c8097a756`
- view_fingerprints ADAUSDT: `d0eadcc099dfb3f6ed81b6d9929b2e49877ed1a3e8a1c2eace8a461433de498f`
- view_fingerprints BTCUSDT: `8b3915cc694963d02051011fabd895b98e968d5e5a6c4d374951a1ce999ce01d`
- view_fingerprints ETHUSDT: `5ef078e845bdbba3cc7452d1b234612a6c5881627f203af5eb96131b55b1dfbb`

Artifact directory: `artifacts/research/94effde761681b2133e4164e25886b6463af735ca51407044c3c88e93f071ee7/development/`

Existing manifest file checksums (SHA-256):

- `approved-inputs.json`: `04a001f0aac93771003c629a832aaad1d85bc4d6a7130e2c71a972b51eab3d42`
- `first-result.json`: `4eeab5f15b4239b4dd221b80acf8fcb9b66a3bc1791bf6d9003c3ee57fbb7cd7`
- `report.txt`: `caddef5e9e24d46c9f06908dbf359d7ef29248dc9280a51bf805c07e926886f9`
- `scorecard.json`: `820f8596ee7e47c623ff69e9ef4e91b005cedabef1d1a2d1033be49fa616bae5`
### 1h

- experiment_fingerprint: `41dd3eef1b985af4cca0f885a68997b721d60357a63b9f0b01f2f160d96f8288`
- run_fingerprint: `d0ab6f02849f3ab1c79cb84b4b4c4302dddf07b481956a7f9d77cfbd34f56ed7`
- strategy_fingerprint: `9aa938ddb1f0e5d911278cce2262e3ab8fe9299f329de483b6cb41ffe0acfd76`
- rules_fingerprint: `e382b5da9ad38a6836e24d488fb8306e61b8849906000ec015d3784486802872`
- portfolio_result_fingerprint: `02f8f37c17819cc81d3e583f80e06a37fe59e59259e09777d0eaa40692f89d34`
- scorecard_fingerprint: `c8cda900556eb4668d820da62436e833ff391d796afefccb0014cd4611f72a89`
- manifest_fingerprint: `21f1792f8c803c04fb82427e76709a6b1668b7b705a7d9edf600c260a17c2741`
- dataset_fingerprints ADAUSDT: `a7d2d582fa6171e5ee6ca5cc285c1f08dabf5c9b2543c49e1b49fa4f59cfea24`
- dataset_fingerprints BTCUSDT: `566c94bf3dd152fc4636392edfdbf322277d0c34fefd3d6ca6b7171c8b0f1980`
- dataset_fingerprints ETHUSDT: `a3a854c9d19bb4970faf235f9dff76d455e7af1376ff79cf58357e0c8097a756`
- view_fingerprints ADAUSDT: `2d9f8824172802eb4622cb227d7d1e0fd102f39d8178b6f911fe4697947bda13`
- view_fingerprints BTCUSDT: `c11a43e316ca4ca2ce42b764115c0ee20040d5d4b07b05525449c27cda99ae2f`
- view_fingerprints ETHUSDT: `b22a4c53b0264d141dd811e2adcd5adcfaa1e7fc87993d133c02f3cbe50b8503`

Artifact directory: `artifacts/research/41dd3eef1b985af4cca0f885a68997b721d60357a63b9f0b01f2f160d96f8288/development/`

Existing manifest file checksums (SHA-256):

- `approved-inputs.json`: `8f97812ce13f11043360275de21ab6b88a3bea3e26447e339c8cb1cc0acf4dfc`
- `first-result.json`: `65b70a4730a0c5eb6bbfb3fb55334d5e46a6f388c7d5fd92003cfea163d5567c`
- `report.txt`: `9fd56915ca0827dc6c2fceed06ced4003bc78feb3499580267c41a6ddc9b35c5`
- `scorecard.json`: `cd725edc25eaac973ca2eb808b7f027bc2067492bcdd503af69093bb316485ec`
### 4h

- experiment_fingerprint: `0c8d02a7bb3a2e9bb02c9e8b9c36e6c248581d7c95b2b3e3f91572faa7dd5851`
- run_fingerprint: `37de98cc08420c945dece486ec0295b144dc3824b44fad1705367d08f4d096b7`
- strategy_fingerprint: `9aa938ddb1f0e5d911278cce2262e3ab8fe9299f329de483b6cb41ffe0acfd76`
- rules_fingerprint: `e382b5da9ad38a6836e24d488fb8306e61b8849906000ec015d3784486802872`
- portfolio_result_fingerprint: `726a515e0fbb95a9b3281afe5d179c55f0d89e6dd9c5d20d77f1796342de6632`
- scorecard_fingerprint: `17f726344e551ef8137f63529c61af9c0fdbb6f84eced0e94a63104cf9a72603`
- manifest_fingerprint: `d78fbaa2af3b120ac3e2d567779f7ba7e862b4ae3406471dcc3a0b7abfa101b6`
- dataset_fingerprints ADAUSDT: `a7d2d582fa6171e5ee6ca5cc285c1f08dabf5c9b2543c49e1b49fa4f59cfea24`
- dataset_fingerprints BTCUSDT: `566c94bf3dd152fc4636392edfdbf322277d0c34fefd3d6ca6b7171c8b0f1980`
- dataset_fingerprints ETHUSDT: `a3a854c9d19bb4970faf235f9dff76d455e7af1376ff79cf58357e0c8097a756`
- view_fingerprints ADAUSDT: `9ef5a0dd24016d1c418a4357be7384c68e96a4c5a872926dcda6f1abb8ec840a`
- view_fingerprints BTCUSDT: `1002b59b7699ce1a5b6c29beaaf3f15412f51742ebceee25ce77da5b8fe60630`
- view_fingerprints ETHUSDT: `95b9852180e7b913bd20d27b80d0de9598b7bb0653340faa9c3f8d82d15a8b3e`

Artifact directory: `artifacts/research/0c8d02a7bb3a2e9bb02c9e8b9c36e6c248581d7c95b2b3e3f91572faa7dd5851/development/`

Existing manifest file checksums (SHA-256):

- `approved-inputs.json`: `89fe39a425a51e303bec22d5364286b65e8d97cfa7499971cde267d68505e232`
- `first-result.json`: `99458eddfd474f3c48d826bb9c10da1613ff706d651b28f962682b97ccd89c42`
- `report.txt`: `dcec0258ed1ba1b7d8e0a4fa390d56ce849470743dd6e58c51e84e8fd6851221`
- `scorecard.json`: `83b64d6ac22fe1a94f5db33e69e50ff982b61795925bfc980ac0c8123b575b0a`

Shared evidence fingerprints:

- Comparison: `de040b15dd421e091c7561df0d2d7d34adf882f2518927383f8e3735e369a2d7`
- Frozen batch: `09fe85c80ac0eeb47c73ff5d2230b446089afe1eabeaf0726deafb1dd4f27d48`
- baseline-001 report: `61b496687c440a505349635255e58986b18bca48c9607f0def04d383688177be`
- Phase 6A diagnostic: `4ac76029e950c93c7e5f5228bd985eee60fabcf65b70f43a3313988269044e0a`
- Research policy: `2642052de313ece07b300791ab1663ff60fc8368d9d7d0059934cde75d4dac99`

## Artifact integrity and evidence sources

Verified the registry hash chain and three completed outcomes, each scorecard/manifest fingerprint, all file checksums already recorded in manifests (including the preserved first result), and the comparison fingerprint. Comparison rows match the three scorecards exactly. Identity references match `phase6c-results.md`; its presentation-rounded values are not used in the canonical table above. Baseline/Phase 6A report fingerprints were also verified. No simulations or financial analytics were rerun.

Sources (repository-relative canonical local paths):

- `artifacts/research/phase6c-approved-batch.json`
- `artifacts/research/phase6c-results.md`
- `artifacts/research/phase6c-comparison/comparison.json`
- `artifacts/research/registry/events.jsonl`
- Each experiment directory above: `scorecard.json`, `run-manifest.json`, and files covered by its recorded checksums.
- `artifacts/baseline-001/financial/report.json`
- `artifacts/baseline-001/diagnostics/report.json`
- Unchanged `research/spec.py` policy, matched to the scorecard policy.

Artifacts remain local and ignored by Git; only this evidence document is version-controlled. The original 370-test preflight passed; this documentation closeout used lightweight integrity checks only.

## Limitations and access boundaries

- Experiments are correlated and share the same observed DEVELOPMENT period.
- Static current exchange rules were used retrospectively, not asserted to be historically identical.
- Fee/slippage values are research assumptions, not actual operator fees or exact historical costs.
- Funding and liquidation modeling are excluded.
- Candle-close MTM does not capture worst intrabar exposure; it is price-only and excludes hypothetical exit fees, with open-position entry fees deferred until closure.
- 2025 remains `LOCKED_PREVIOUSLY_OBSERVED_VALIDATION`; it was previously observed through baseline-001/Phase 6A and is not genuinely unseen. No 2025 evaluation occurred here.
- 2026 remains untouched `TRUE_HOLDOUT`; no 2026 data was accessed.
- No nomination, promotion, validation, new configuration, framework change, baseline rerun or Phase 6D work occurred.
