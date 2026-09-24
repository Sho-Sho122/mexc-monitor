# 511 β1 実装仕様書 v1.0

**Project:** MEXC 511 Model League  
**Specification Version:** 1.0  
**Freeze Date:** 2026-09-25 JST  
**目的:** 過去データへの追加最適化を行わず、これまでの研究から事前に固定した仮説をForward環境で検証するShadow Trading Systemを構築する。

---

## 1. Codexへの最重要指示

このプロジェクトの目的は、**過去データに最も適合する売買モデルを作ることではない。**

既に研究済みの仮説を固定した状態で、

**Regime → Entry → Exit → Risk → Execution → Forward Evaluation**

を再現可能に記録するシステムを実装することが目的である。

Codexは、過去成績を改善する目的で以下を行ってはならない。

- 新しい閾値を探索する
- RSI、MACD、Funding等から勝率の高い条件を追加する
- 特定銘柄を成績を理由に除外する
- 特定時間帯を成績を理由に除外する
- 最も利益が高かったExitをβ1として採用する
- LONGまたはSHORTを成績を理由に除外する
- PULLBACK条件そのものを変更する
- 30分Crowdingの時間幅を最適化する
- Persistenceの区切りを最適化する
- 過去データを使ってβ1のルールを書き換える
- HYBRID等の既存戦略を削除・破壊する
- 実注文を送信する

**511 β1はShadow専用であり、MEXCへの注文機能を実装してはならない。**

---

## 2. 既存Repository

現在Repositoryには少なくとも以下が存在する。

### Main / Scanner

```text
mexc_scanner.py
mexc_scanner_research.py
mexc_system.py
mexc_system_state.json
```

### Existing Hybrid

```text
mexc_hybrid_system.py
mexc_hybrid_state.json
mexc_hybrid_trades.csv

hybrid_lab_engine.py
hybrid_lab_workflow.py
hybrid_lab_models_template.py
hybrid_lab_models.json
```

### Strategy

```text
strategy_pullback.py
strategy_pullback.csv
strategy_pullback_state.json

strategy_trend_volume.py
strategy_trend_volume.csv
strategy_trend_volume_state.json

strategy_oi_funding.py
strategy_oi_funding.csv
strategy_oi_funding_state.json

strategy_high_edge.py
strategy_high_edge.csv
strategy_high_edge_state.json
```

### Exit Research

```text
research_exit.py
research_exit_state.json
research_exit_trades*.csv
research_exit_results*.csv
```

### Other

```text
requirements.txt
.github/workflows/
```

Codexは実装前にRepository全体を読み、実際の依存関係、workflow、CSV schema、state管理方法を把握すること。

**ファイル名だけを根拠に既存コードの役割を推測して変更してはならない。**

---

## 3. 基本実装方針

既存の4戦略およびExit Researchは、原則としてそのまま維持する。

511は、

```text
Existing Scanner
       ↓
Existing 4 Strategies
       ↓
strategy_*.csv
       ↓
Unique Signal Engine
       ↓
Regime Engine
       ↓
Model League
       ↓
Shadow Engine
       ↓
Exit / Execution / Evaluation
```

という**上位レイヤーとして追加する。**

既存戦略を511内部へコピーして再実装しないこと。

既存戦略CSVを511の入力データとして利用することを基本とする。

---

## 4. 511の基本思想

511 β1では、

> Strategy Vote > Market Regime

ではなく、

> **Market Regime → Entry Strategy → Exit → Risk → Execution**

の順に判断する。

複数戦略が同時にSignalを出したこと自体を「強いSignal」とみなしてはならない。

---

## 5. 時刻

システム上の基準時刻は、

```text
Asia/Tokyo
JST
```

とする。

判断に使用する時刻は原則、

```text
scan_time_jst
```

とする。

ticker取得時刻などと混同しない。

---

## 6. 5分Bucket

Signalは5分単位に正規化する。

例：

```text
10:00:00 ～ 10:04:59 → 10:00 bucket
10:05:00 ～ 10:09:59 → 10:05 bucket
```

定義：

```python
bucket_5m = floor(scan_time_jst, "5min")
```

---

## 7. Unique Signal Event

511の独立した市場Signal単位を以下とする。

```text
symbol
×
direction
×
5-minute bucket
```

同じ `symbol / direction / bucket_5m` に複数Strategy Signalが存在しても、**1 Unique Signal Event**として扱う。

例えば、

```text
10:00 SOL LONG PULLBACK
10:00 SOL LONG OI_FUNDING
10:00 SOL LONG HIGH_EDGE
```

は3取引ではなく、

```text
SOL LONG 10:00
```

という1 Event。

### 7.1 LONGとSHORT

LONGとSHORTは絶対に統合しない。

```text
SOL LONG 10:00
SOL SHORT 10:00
```

は別Event。

---

## 8. strategy_set

Unique Eventには以下を保存する。

```text
strategy_set = PULLBACK|OI_FUNDING
strategy_count = 2
```

booleanも保存する。

```text
has_pullback
has_trend_volume
has_oi_funding
has_high_edge
```

---

## 9. event_id

再実行しても同じEventが同じIDになるよう、**deterministic ID**を使用する。

```text
event_id =
511_
YYYYMMDDHHMM_
SYMBOL_
DIRECTION
```

例：

```text
511_202609251030_SOLUSDT_LONG
```

同じInputを再処理して新しいEventを増殖させてはならない。

---

## 10. PULLBACK entry

Core SignalにPULLBACKが含まれる場合、**PULLBACK StrategyがSignal発生時に記録したentryをReference Entryとして使用する。**

他Strategyのentryで置き換えない。

複数PULLBACK行が異常に存在する場合は、同一symbol・同一direction・同一5m bucketについて最も早い`scan_time_jst`を採用し、重複としてログに残す。

---

## 11. 30分Crowding

511 β1における主要Regime変数。

現在Eventのbucketを `T` とする。

Crowding計算には、`T-30min ～ T直前` の**過去6個の完成済み5分Bucketだけ**を使用する。

現在Bucket Tを絶対に含めない。

例：現在が10:30 bucketなら、使用できるのは10:00, 10:05, 10:10, 10:15, 10:20, 10:25まで。

---

## 12. Crowdingの入力

Raw Strategy Signalではなく、**Unique Signal Event**を使用する。

過去30分のLONG / SHORT Unique Eventsを数える。

```text
prior_long_count
prior_short_count
```

---

## 13. majority_direction

```text
prior_long_count > prior_short_count → LONG
prior_short_count > prior_long_count → SHORT
同数 → NEUTRAL
どちらも0 → NEUTRAL
```

---

## 14. MATCH / OPPOSITE / NEUTRAL

現在Signal方向と`majority_direction`を比較する。

```text
majority = LONG, current = LONG → MATCH
majority = LONG, current = SHORT → OPPOSITE
majority = NEUTRAL → NEUTRAL
```

保存列：

```text
crowding_state
```

値：

```text
MATCH
OPPOSITE
NEUTRAL
```

---

## 15. Crowding補助値

以下も保存する。

```text
prior_long_count
prior_short_count
prior_total_count
majority_direction
majority_count
minority_count
majority_ratio
direction_imbalance
```

```text
direction_imbalance =
(prior_long_count - prior_short_count) / prior_total_count
```

0件の場合はNULLまたは0とし、仕様上統一する。

ただし、これらをβ1のTrade Filterとして使用しない。

---

## 16. Persistence

Crowdingが**一時的な偏り・Exhaustion**なのか、**持続Trend**なのかを測定するRegime変数。

過去6個の5分Bucketについて、それぞれのBucket内のUnique Signal方向を集計する。

```text
LONG > SHORT → LONG bucket
SHORT > LONG → SHORT bucket
同数またはSignalなし → NEUTRAL bucket
```

30分majority_directionと同じ方向になったBucket数を数える。

```text
persistence_score
```

範囲：

```text
0 ～ 6
```

---

## 17. Persistence category

β1では以下に固定する。

```text
LOW   = 0～2
MID   = 3～4
HIGH  = 5～6
```

**Persistence categoryはβ1 CoreのTrade Filterではない。**

Challenger ModelおよびRegime解析用とする。

---

## 18. Asset Class

保存可能な分類：

```text
CRYPTO_NATIVE
COMMODITY_LINKED
EQUITY_ETF_LINKED
INDEX_LINKED
UNKNOWN
```

Codexが銘柄名から勝手にAsset Classを推測しないこと。

既存研究で使用しているAsset MappingがRepository内に存在する場合、それを使用する。

存在しない場合は`UNKNOWN`とするか、version管理された`asset_class_map.json`を作成する。

---

## 19. BTC Market Context

既存Strategy CSVにある場合、以下を511 Eventへ保持する。

```text
btc_ret_1m_pct
btc_ret_5m_pct
btc_ret_15m_pct
btc_ret_1h_pct
btc_rsi*
btc_ema*
btc_macd*
btc_volume_ratio*
btc_atr*
```

β1 CoreのTrade Filterには使用しない。

---

## 20. OI / Funding / Volume / ATR

利用可能なら保存する。

```text
funding_rate
oi
oi_change_pct
volume_ratio1
volume_ratio5
volume_ratio15
atr1_pct
atr5_pct
atr15_pct
```

ただし、β1ではTrade Filterにしない。

---

## 21. 511 β1 CORE

β1のCore Modelは以下のみ。

```text
has_pullback == True
AND
crowding_state == OPPOSITE
```

Model ID：

```text
CORE_B1_PULLBACK_OPPOSITE
```

これ以外の後付け条件を加えてはならない。

---

## 22. Coreでは使用禁止の条件

以下はCore条件にしない。

```text
BTC direction
BTC volatility
Funding
OI direction
Volume Ratio
ATR
LONG / SHORT
symbol
time of day
strategy_count
RSI threshold
MACD threshold
majority_ratio threshold
majority age
Spread threshold
Asset class
Persistence
```

---

## 23. Model League

1 Eventに複数Model Membershipを付与可能とする。

Modelが重複していても、新しいMarket Eventを生成してはならない。

---

## 24. Challenger 1 — Persistence

```text
CORE_B1
AND
persistence_category == LOW
```

ID：

```text
CH_B1_PERSISTENCE_LOW
```

---

## 25. Challenger 2 — Asset

```text
CORE_B1
AND
asset_class IN (
CRYPTO_NATIVE,
COMMODITY_LINKED
)
```

ID：

```text
CH_B1_PRIMARY_ASSET
```

---

## 26. Challenger 3 — Persistence × Asset

```text
CORE_B1
AND
persistence_category == LOW
AND
asset_class IN (
CRYPTO_NATIVE,
COMMODITY_LINKED
)
```

ID：

```text
CH_B1_PERSISTENCE_ASSET
```

---

## 27. Control Models

少なくとも以下を同時記録する。

```text
CTRL_PULLBACK_ANY
CTRL_OPPOSITE_ANY
CTRL_OPPOSITE_NON_PULLBACK
CTRL_PULLBACK_MATCH
CTRL_TREND_VOLUME
CTRL_OI_FUNDING
CTRL_HIGH_EDGE
```

定義：

```text
CTRL_PULLBACK_ANY:
has_pullback == True

CTRL_OPPOSITE_ANY:
crowding_state == OPPOSITE

CTRL_OPPOSITE_NON_PULLBACK:
crowding_state == OPPOSITE
AND has_pullback == False

CTRL_PULLBACK_MATCH:
has_pullback == True
AND crowding_state == MATCH
```

Strategy Controlsは、それぞれ対応Strategyを含むUnique Event。

---

## 28. Controlの目的

ControlはTrade対象を増やすためではない。

Coreの成績が良かった場合に、

```text
PULLBACK自体が良かっただけなのか
OPPOSITE自体が良かったのか
交差条件に意味があるのか
```

を区別するために使う。

---

## 29. Exit β1 Reference

β1評価用Exitは固定する。

```text
Stop Loss = 0.50%
Reward/Risk = 1.0
Take Profit = 0.50%
Maximum Holding Time = 3 hours
```

---

## 30. LONG Exit

```text
SL = E × (1 - 0.005)
TP = E × (1 + 0.005)
```

---

## 31. SHORT Exit

```text
SL = E × (1 + 0.005)
TP = E × (1 - 0.005)
```

---

## 32. 5分足内 TP/SL同時Hit

同じ5分足でTPとSLの両方が到達した場合、

```text
UNKNOWN
```

とする。

保存：

```text
exit_result = UNKNOWN
ambiguous_intrabar = 1
```

Primary EVから除外するが、件数・割合は必ず表示する。

---

## 33. Time Exit

Entryから3時間経過してもTP/SLに到達していない場合、最初に利用可能な3時間経過時点の市場価格で終了する。

使用価格・足の定義は既存`research_exit.py`との整合性を優先する。

Time ExitのR：

```text
LONG:
(exit_price - entry_price) / (entry_price × 0.005)

SHORT:
(entry_price - exit_price) / (entry_price × 0.005)
```

---

## 34. Existing 60 Exit Research

既存の`research_exit.py`は維持する。

1 Signalについて60 Exit Variantが存在しても、60 tradesとは扱わない。

同じEntry Eventに対する60 exit simulationsとして扱う。

β1の正式成績は固定Reference Exitで評価する。

---

## 35. Best Exit禁止

60 Exitの中からForward期間終了後に「一番良かったExit」をβ1の結果として採用してはならない。

---

## 36. Shadow Only

β1では実注文・Live Order・API Tradingを一切行わない。

---

## 37. Risk Engine β1

```text
1 Signal = 1R risk unit
```

として評価する。

実際の資金割合は最適化しない。

---

## 38. 同時Signal

以下を可能な限り記録する。

```text
concurrent_core_count
concurrent_long_count
concurrent_short_count
concurrent_crypto_count
```

---

## 39. Correlation Cluster

最低限以下を作れるようにする。

```text
5m × direction cluster
30m × direction cluster
```

Asset Classを加えたCluster分析も可能にする。

---

## 40. Cluster ID

```text
cluster_5m_id
cluster_30m_id
```

を保存。

---

## 41. Execution Logger

Entry Event時点で可能なら以下を保存。

```text
signal_price
bid
ask
spread_pct
spread_bps
source_scan_time
decision_time
ticker_time
data_age_ms
```

---

## 42. expected_fill

Shadow上でMarket Orderを想定する場合、

```text
LONG → ask
SHORT → bid
```

をexpected_fillとして保存する。

存在しないデータを捏造しない。

---

## 43. Fee

Feeを推測してハードコードしない。

設定値：

```text
fee_bps_entry
fee_bps_exit
```

未設定時：

```text
net_R = NULL
cost_status = MISSING_FEE_CONFIG
```

Gross EVをNet EVとして扱わない。

---

## 44. Slippage

```text
slippage_bps_entry
slippage_bps_exit
```

を設定可能にする。

実測がない場合、勝手な数値を入力しない。

---

## 45. Gross / Net

別々に保存する。

```text
gross_R
fee_R
slippage_R
spread_R
net_R
```

---

## 46. Forward時のNo Lookahead

後から追加されたSignalで過去のTrade判定を書き換えてはならない。

```text
decision_created_at_jst
source_cutoff_jst
```

を保存する。

---

## 47. Late-arriving data

後から過去BucketのSignalがCSVへ追加されても、既にForwardで確定した`crowding_state`と`model_membership`を書き換えてはならない。

必要ならAudit用列を別に持つ。

---

## 48. BackfillとForwardを分離

```text
evaluation_mode
```

値：

```text
BACKFILL
FORWARD
```

---

## 49. β1 Freeze

2026-09-25までに判明している研究結果はDEVELOPMENT扱い。

実際のForward開始は、**この仕様を実装したGit commitが確定し、511 Shadowが初めて稼働した後の最初の完全な5分Bucket**とする。

保存：

```text
spec_version = 1.0
forward_start_jst
git_commit_hash
```

---

## 50. Sep25–Sep30 Shakedown

実装後から2026-09-30 23:59 JSTまではSHAKEDOWN期間としてよい。

目的：

- 実装ミス
- CSV破損
- timezone error
- duplicate error
- restart error
- lookahead error

の検出。

---

## 51. Shakedown中に許される修正

仕様書と実装が一致していないソフトウェアバグの修正のみ。

---

## 52. Shakedown中でも禁止

成績を見て条件を変更しない。

例：

```text
Persistence LOWだけにする
SHORTを消す
LINKを除外する
SLを0.35%にする
```

---

## 53. Main Forward

```text
2026-10-01 00:00 JST
～
2026-10-14 23:59 JST
```

β1 Core条件を変更しない。

---

## 54. Forward終了後も自動でLive化しない

良い結果が出てもProductionへ自動昇格させない。

---

## 55. 最低限のForward評価

```text
N
WIN
LOSS
UNKNOWN
TIME
gross_R total
gross_EV
net_R total
net_EV
win_rate
max_drawdown_R
max_loss_streak
5m_cluster_EV
30m_cluster_EV
```

---

## 56. Slice Evaluation

```text
date
direction
symbol
asset_class
crowding_state
persistence_category
BTC context
strategy_count
```

で分解可能にする。

---

## 57. Walk-forward blocks

Report groupingとして期間分割可能にする。

---

## 58. Bootstrap

可能なら、

```text
day cluster
30m market cluster
```

による95% CIを計算可能にする。

---

## 59. Forward Qualification

出力状態：

```text
INSUFFICIENT_DATA
NEGATIVE
INCONCLUSIVE
FORWARD_SUPPORTED
```

### INSUFFICIENT_DATA

```text
resolved Core events < 100
```

または

```text
30m clusters < 30
```

### NEGATIVE

十分なSampleがあり、net_EV < 0 の場合。

Netが計算不能ならGrossのみでNEGATIVE判定しない。

### FORWARD_SUPPORTED

```text
resolved Core events >= 100
30m clusters >= 30
net_EV > 0
cluster-bootstrap 95% CI lower bound > 0
```

ただしLive昇格を意味しない。

### INCONCLUSIVE

上記以外。

---

## 60. Control comparison

同じForward期間で以下を比較する。

```text
CORE_B1
CTRL_PULLBACK_ANY
CTRL_OPPOSITE_ANY
CTRL_OPPOSITE_NON_PULLBACK
CTRL_PULLBACK_MATCH
```

---

## 61. Challenger comparison

```text
CH_B1_PERSISTENCE_LOW
CH_B1_PRIMARY_ASSET
CH_B1_PERSISTENCE_ASSET
```

も同時にShadow評価する。

---

## 62. 推奨Output Files

```text
511_unique_events.csv
511_model_membership.csv
511_shadow_trades.csv
511_shadow_results.csv
511_execution.csv
511_state.json
511_audit.log
```

名称は既存Repository構造に合わせて調整可。

---

## 63. 511_unique_events.csv

1行 = 1 Unique Signal Event。

主要列：

```text
event_id
scan_bucket_jst
symbol
direction
strategy_set
strategy_count
has_pullback
has_trend_volume
has_oi_funding
has_high_edge
entry_price
prior_long_count
prior_short_count
majority_direction
majority_ratio
direction_imbalance
crowding_state
persistence_score
persistence_category
asset_class
BTC context
OI
Funding
Volume
ATR
evaluation_mode
spec_version
```

---

## 64. 511_model_membership.csv

1 Event × 1 Model Membership。

```text
event_id
model_id
eligible
reason
spec_version
decision_created_at_jst
```

---

## 65. Shadow Trade

同じEventがCoreとChallenger両方に属していても、市場価格Pathは共通として扱う。

---

## 66. State file

```text
schema_version
spec_version
last_processed_bucket
processed_event_ids
open_shadow_positions
forward_start_jst
git_commit_hash
```

等を保持。

---

## 67. Atomic write

State JSONや重要CSVは安全なAtomic Writeを優先する。

---

## 68. Restart Safety

再実行・Restartで同じEventやTradeを二重生成しない。

---

## 69. CSV Rotation

Exit Researchは約2週間単位のRotationを維持可能にする。

---

## 70. trade_id

Rotationを跨いでもglobally uniqueとする。

---

## 71. Rotation boundary

Rotationを理由に未決済Tradeを強制終了しないことを原則とする。

やむを得ない場合は`rotation_forced_exit = 1`等で識別する。

---

## 72. Existing HYBRID

既存HYBRID系は削除しない。

---

## 73. Existing Strategies

```text
strategy_pullback.py
strategy_trend_volume.py
strategy_oi_funding.py
strategy_high_edge.py
```

のStrategy Logicを511のために変更しない。

---

## 74. Scanner

Scanner変更が必要な場合は責任範囲を調査し、既存収集を壊さない。

---

## 75. GitHub Actions

`.github/workflows`を確認し、Strategy更新完了前に511が不完全Snapshotを読む問題を避ける。

---

## 76. Source Snapshot

可能なら以下をAudit用に保存。

```text
source_file
source_last_timestamp
source_row_count
source_hash
```

---

## 77. Data validation

最低限、

```text
scan_time_jst parseable
direction ∈ LONG, SHORT
symbol non-null
strategy known
entry valid
duplicate handling
```

を確認する。

---

## 78. Bad Row

異常Rowは黙って削除せず、理由付きで記録する。

---

## 79. Missing data

BTCやOI等の補助Context欠損だけでCoreから除外しない。

---

## 80. Essential Missing Data

以下欠損時はCore Entryを作らない。

```text
symbol
direction
scan_time
PULLBACK membership
entry price
crowding calculation data
```

---

## 81. Test Requirement

最低限以下を自動Testする。

1. 同じsymbol/direction/bucketの4 Strategyが1 Unique Eventになる
2. LONGとSHORTは別Event
3. Crowdingに現在Bucketが入らない
4. prior 6 completed bucketsのみ使用
5. LONG多数時のLONG current = MATCH
6. LONG多数時のSHORT current = OPPOSITE
7. LONG=SHORT = NEUTRAL
8. Persistence scoreが0～6
9. 再実行でEvent数が増えない
10. Restart後もOpen Shadow Tradeが二重生成されない
11. 同一5m CandleでTP/SL両方Hitの場合UNKNOWN
12. β1固定Exit計算
13. Late Dataが来ても正式Membershipを書き換えない
14. BACKFILLとFORWARDが区別される
15. Missing Fee時にGrossをNetとして扱わない

---

## 82. Synthetic Test Data

本番成績ではなく、手計算できる人工データFixtureも用意する。

---

## 83. Historical Backfill

既存CSVによるBackfillはimplementation verification目的のみ許可する。

Parameter Searchは禁止。

---

## 84. Backfill sanity check

過去分析と大きく異なる場合は、Unique definition、timezone、dedup、30m window等を調査する。

---

## 85. 既知のSanity References

過去研究では大まかに、

```text
Unique全体の無条件EVはほぼゼロ付近
OPPOSITE単独のEdgeは最近弱まっている
PULLBACKは最近比較的強い
PULLBACK × OPPOSITEが主要Candidate
```

という構造が観測されている。

これらへ数字を合わせるためにコードを変更してはならない。

---

## 86. 重要な既存研究解釈

β1で検証したい中心仮説は、

> **Recent market crowdingと逆方向に発生したPULLBACK型SignalにEdgeが存在する可能性**

である。

---

## 87. Persistenceの役割

Crowding / Exhaustion と Persistent Trendを区別できる可能性を検証する。

β1 Coreには加えない。

---

## 88. Asset Classの役割

Asset Class差はChallengerとして評価し、β1 CoreからEquityを除外しない。

---

## 89. Strategy Count

複数Strategy一致をConfidenceとして扱わない。

---

## 90. β2 Candidate Log

Forward中に気になるPatternが発見されてもβ1へ追加しない。

別途β2候補として記録する。

---

## 91. β2 Example

以下のような発見があってもβ1には追加しない。

```text
RSI < 43
02:00～05:00
exclude SOL
Funding < -0.0023
ATR > X
```

---

## 92. Code structure

具体的な新規FilenameはRepository調査後に自然な構成を選択してよい。

候補：

```text
model511/
    __init__.py
    unique_engine.py
    regime_engine.py
    model_league.py
    shadow_engine.py
    evaluation.py
    config.py
```

---

## 93. Config

変更可能なSystem設定と、変更禁止のResearch Specを分離する。

Research Spec固定項目：

```text
30-minute crowding
5-minute bucket
PULLBACK × OPPOSITE
SL 0.5%
RR 1
3h
```

---

## 94. Spec fingerprint

可能ならβ1 Research Configから`spec_hash`を生成し、Forward Eventに記録する。

---

## 95. Documentation

READMEまたは専用Documentに以下を記載する。

```text
511とは何か
β1 Core
Crowding
Persistence
Exit
Forward dates
Output files
Run method
```

---

## 96. Codexが実装前に行うこと

コード変更前に以下を報告する。

1. Repository構造
2. 各Python fileの役割
3. Strategy Data Flow
4. Exit Research Data Flow
5. GitHub Actionsの実行順
6. 511をどこへ接続する予定か
7. 新規作成予定File
8. 変更予定File
9. 変更しないFile
10. リスクまたは不明点

---

## 97. Codexの実装方針

**Minimum invasive change**を原則とする。

既存の動いているSystemを大規模Rewriteしない。

---

## 98. Codexが不明点を発見した場合

Research Specを自己判断で変更しない。

コードから確定できない場合は`BLOCKER`として報告する。

---

## 99. Definition of Done

- 既存4 Strategyが正常稼働
- Existing Exit Research正常稼働
- HYBRIDが破壊されていない
- Unique Event生成
- 30m Crowding生成
- Persistence生成
- Core判定
- Challenger判定
- Control判定
- Shadow Tradeのみ
- β1 fixed Exit評価
- Execution log
- BACKFILL / FORWARD分離
- Late Data対策
- Idempotent
- Restart Safe
- Tests pass
- GitHub workflow正常
- Forward startが記録される
- `spec_version=1.0`
- real orders = 0

---

## 100. 最終禁止事項

Codexは、

> 「もっと勝てそうなので条件を変更しました」

という変更を絶対に行わない。

このプロジェクトでは、**収益最大化よりも、事前に固定した仮説を未来データで公平に検証できることを優先する。**

---

# Codexへ最初に送る指示文

```text
添付した「511 β1 実装仕様書 v1.0」に従って実装してください。

ただし、すぐにコード変更を開始しないでください。

最初にRepository全体、.github/workflows、既存Python、CSV schema、
state JSON、research_exit.py、4 Strategy、HYBRIDの構造を調査してください。

その後、

1. 現在のArchitecture
2. 511を接続する位置
3. 新規作成するファイル
4. 修正する既存ファイル
5. 修正しない既存ファイル
6. Lookahead / duplicate / state管理上のリスク
7. 仕様書と現行コードの矛盾
8. 実装計画

を提示してください。

私の確認を得るまでは、大規模なRewriteや既存Strategy条件の変更をしないでください。

511 β1のResearch Logicは凍結されています。
過去CSVを利用して新しい閾値を探索したり、
利益が最大になる条件を追加したりしないでください。

511 β1はShadow Onlyです。
MEXCへの実注文機能は実装しないでください。

実装では既存システムを壊さないことを最優先し、
可能な限り新しい511レイヤーとして追加してください。
```
