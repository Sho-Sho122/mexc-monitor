# 1. 511 Adaptive Research / Model League v1.1 FINAL 実装仕様書

**Project:** MEXC 511 Adaptive Research / Model League<br>
**Specification Version:** 1.1 FINAL<br>
**Specification Date:** 2026-09-26 JST<br>
**Status:** FINAL / Specification reconciled — implementation not yet authorized<br>
**Parent Specification:** `511_BETA1_SPEC_v1.0.md`<br>
**Target Repository:** `Sho-Sho122/mexc-monitor`<br>
**Revision Note:** 2026-09-26 利用者指定6項目の整合改訂。実装承認とは別。

親仕様のファイル名 `511_BETA1_SPEC_v1.0.md` は既存β1の正式名称であり、Adaptiveの版番号とは区別して維持する。β1の仕様version・Research Logicを変更しない。

---

## 0. 位置づけ

本仕様は既存の511 β1を置き換えない。既存β1の上位に、4戦略・Exit Research・511 Forwardを横断して研究する**Adaptive Research / Model League**を追加する。

目的は、研究速度を維持しながら、過学習・多重比較・後付け最適化・生存者バイアスを可能な限り抑え、未使用Forwardデータで再現したモデルだけを次世代βまたは実践候補へ進めることである。

本仕様では実注文を行わない。実弾移行は `PRACTICE_CANDIDATE_READY` 認定後のMicro Liveを別仕様で扱う。

---

# 2. 最上位原則

このシステムの目的は「過去データで最も利益の大きいモデル」を作ることではない。目的は、未知の未来データでも再現する可能性のあるEdge候補を発見し、その証拠を段階的に強くすることにある。

1. Discoveryに使ったデータを、そのモデル自身のForward成績に含めない。
2. 成績を見た後で条件を変更した場合、それは新しいモデルとする。
3. β番号は研究世代番号であり、性能順位ではない。
4. 5日ごとに必ず仮説を作らない。15日ごとに必ずβを作らない。
5. `NO_NEW_HYPOTHESIS` と `NO_CHANGE` を正常な結果として扱う。
6. 勝った仮説だけでなく、失敗・棄却・重複も保存する。
7. 単一の最高EVセルより、時間・銘柄・方向・パラメータ近傍の頑健性を優先する。
8. Grossだけで実践候補にしない。
9. 年内実弾などの期限を理由に認定基準を緩めない。
10. 「将来も勝てる」と断定しない。
11. 最終通知は `PRACTICE_CANDIDATE_READY` とする。
12. Adaptive Researchの失敗が既存4戦略、Exit Research、511 β1の保存を妨げてはならない。

---

# 3. 既存511 β1との境界

既存β1の次の仕様は変更しない。

## 3.1 Unique Signal Event

```text
symbol × direction × JST 5-minute bucket
```

LONGとSHORTは別Event。

## 3.2 Core β1

```text
has_pullback == True
AND
crowding_state == OPPOSITE
```

## 3.3 Reference Exit β1

```text
SL = 0.50%
TP = 0.50%
RR = 1.0
Max Holding = 3h
```

## 3.4 維持する原則

- Crowdingは現在bucketを含めない。
- Persistenceは過去6完成bucket。
- 同一5分足TP/SL同時到達はUNKNOWN。
- deadline boundaryが判別不能ならUNKNOWN。
- late-arriving dataで確定済みForward membershipを書き換えない。
- BACKFILL / FORWARDを分離する。
- Research outcomeとExecution outcomeを分離する。
- fee / slippageを推測しない。
- 必要コストが欠ける場合 `net_R=NULL`。
- `511_state.json` をauthoritative sourceとする。
- CSVは再生成可能projection。
- 実注文は行わない。

---

# 4. 全体アーキテクチャ

```text
MEXC Market
    ↓
Existing Scanner
    ↓
4 Strategies
    ├─ PULLBACK
    ├─ TREND_VOLUME
    ├─ OI_FUNDING
    └─ HIGH_EDGE
    ↓
Existing Exit Research
    ↓
511 β1
    ├─ Unique Event
    ├─ Regime
    ├─ Model Membership
    ├─ Shadow Trade
    ├─ Reference Result
    └─ Execution
    ↓
===========================================
511 Adaptive Research / Model League
===========================================
    ↓
5-Day Research Scan
    ↓
Hypothesis Registry
    ↓
Frozen Challenger
    ↓
Forward Evidence Accumulation
    ↓
15-Day Generation Gate
    ↓
β Generation / NO_CHANGE
    ↓
Longer Forward Validation
    ↓
Final Confirmation
    ↓
PRACTICE_CANDIDATE_READY
    ↓
Micro Live（別仕様）
```

---

# 5. 3つの時計

## 5.1 Data Clock — 5分

既存MEXC Researchの周期。Scanner、4戦略、Exit Research、511 Forward、Executionを更新する。Adaptive Researchの探索は行わない。

## 5.2 Research Clock — 5日

5日ごとにResearch Scanを1回実施する。役割は、データ品質確認、4戦略分析、戦略間分析、Regime分析、Exit robustness分析、511分析、Execution品質分析、Edge劣化監視、新仮説登録である。

ここで得た成績は**Discovery evidence**であり、Forward evidenceではない。

## 5.3 Generation Clock — 15日

3回のResearch ScanごとにGeneration Gateを実施する。ここでは、すでに登録・凍結されているChallengerのみ審査する。

結果は次のいずれか。

```text
ELIGIBLE_FOR_PROMOTION_REVIEW
PROMOTE_TO_BETA
CONTINUE_TESTING
REJECT
INSUFFICIENT_FORWARD
NO_CHANGE
INVALID_DATA
```

15日Gate当日に新しく考えた仮説を、その場で過去15日の成績を使って昇格させてはならない。

システムの判定上限は `ELIGIBLE_FOR_PROMOTION_REVIEW`。`PROMOTE_TO_BETA` はEligible候補に対する人間の承認記録がある場合だけ成立する（§30・34・35）。

---

# 6. スケジュール固定

Adaptive Research有効化時に `adaptive_anchor_jst` を保存する。

以後は次の固定周期。

```text
anchor + 5d
anchor + 10d
anchor + 15d
anchor + 20d
...
```

成績を理由に周期を変更しない。例外は実装バグ、データ破損、lookahead、timestamp不整合、state破損、Workflow障害のみ。例外時は監査ログへ理由を保存する。

---

# 7. Research Scan ID

各Scanに一意なIDを付与する。

```text
SCAN_YYYYMMDD_NNNN
```

最低保存項目：

```text
scan_id
scheduled_at_jst
started_at_jst
completed_at_jst
source_cutoff_jst
git_commit_hash
adaptive_spec_version
feature_schema_version
data_quality_status
new_hypothesis_count
candidate_count_examined_total
status
```

---

# 8. Research ScanのLayer構造

```text
Layer 0: Data Integrity
Layer 1: 4 Strategy Analysis
Layer 2: Cross-Strategy Structure
Layer 3: Regime Analysis
Layer 4: Exit Research
Layer 5: 511 Model Analysis
Layer 6: Execution / Net Analysis
Layer 7: Hypothesis Registration
```

Layer 0が明示的に固定された条件により `BLOCK_RESEARCH` なら、後段を計算しても新Hypothesis/Challenger作成は禁止。

---

# 9. Layer 0 — Data Integrity

最低限チェックする。

- strategy CSV欠損
- strategy state異常
- 511 state欠損
- CSV/state不一致
- duplicate event_id / trade_id
- timestamp parse error
- timezone mismatch
- backward time
- late data件数
- Forward start reset
- model hash mismatch
- source cutoff mismatch
- missing entry / result / quote
- impossible bid/ask
- net coverage
- stale quote
- UNKNOWN/ambiguous outcome率
- Exit Research pending異常
- 511と4戦略input整合

状態：

```text
PASS
WARN
BLOCK_RESEARCH
```

`BLOCK_RESEARCH` の場合、新しいモデル研究へ進めない。

列挙したチェック項目をすべて無条件BLOCKとはしない。閾値が未定義のstale quote・UNKNOWN率・Execution品質・pending件数等は、数値と欠損理由を診断として表示する。BLOCKには根拠となる事前固定規則と適用段階を保存する。未定義の「重大」「異常」から実装者が失格閾値を作らない。

---

# 10. Layer 1 — 4戦略分析

対象：

```text
PULLBACK
TREND_VOLUME
OI_FUNDING
HIGH_EDGE
```

既存4戦略はResearch Sourceであり、その場で書き換えない。

各戦略について最低限：

```text
Raw OPEN N
Unique Event N
Resolved N
WIN
LOSS
UNKNOWN
TIME
Gross R
Gross EV
Net R
Net EV
Net Coverage
Win Rate
Max Drawdown R
Max Loss Streak
5m Cluster EV
30m Cluster EV
Active Days
```

診断slice：

```text
LONG / SHORT
Asset Class
Crowding
Persistence
BTC context
ATR regime
Volume regime
OI direction
Funding sign
Strategy-only / overlap
Holding time
MFE / MAE
```

Raw Signal数を独立サンプル数として扱わない。

---

# 11. Layer 2 — Cross-Strategy Structure

最低限：

```text
strategy_set
strategy_count
PULLBACK only
TREND_VOLUME only
OI_FUNDING only
HIGH_EDGE only
PULLBACK + OI
PULLBACK + TREND
PULLBACK + HIGH_EDGE
3 strategy overlap
4 strategy overlap
```

`strategy_countが多い = 強い` とは仮定しない。Strategy VoteはControlとして検証する。

---

# 12. Layer 3 — Regime Analysis

At-Time-of-Eventで利用可能な特徴量を分析する。

```text
crowding_state
prior_long_count
prior_short_count
majority_ratio
direction_imbalance
persistence_score
persistence_category
asset_class
BTC returns / RSI / EMA / MACD / Volume / ATR
symbol RSI / EMA distance / MACD / Volume / ATR
funding_rate
oi
oi_change_pct
spread
concurrent signal counts
```

Research Scanでは新Regime仮説を作れるが、細かいthreshold optimizationは禁止。

---

# 13. Threshold Policy

連続値の境界は以下の順で選ぶ。

1. 既存仕様のcategory
2. 事前定義された粗いbin
3. 市場構造上意味のある境界
4. Discovery時点で登録した固定境界

理由のないFine tuningは禁止。

禁止例：

```text
RSI > 53.27
ATR < 0.4173
majority_ratio > 67.41%
funding > 0.00173
```

---

# 14. Layer 4 — Exit Research

既存 `research_exit.py` をResearch Sourceとして利用する。

現行Exit候補：

```text
SL: 0.25 / 0.35 / 0.50 / 0.75 / 1.00 %
RR: 1.0 / 1.5 / 2.0
Max Holding: 3 / 6 / 12 / 24 h
```

合計60 variants。

同一trade_idの60 variantsは60 independent tradesではない。

---

# 15. Exit Plateau Policy

Best Exit Cellをそのまま採用しない。

新Exit仮説では、周辺SL・RR・Holdingでも同方向の結果が得られるかを見る。期間、銘柄、方向にも頑健であることを重視する。

単独1セルだけ極端に良い場合は `SPIKE_CELL` とし、原則Challenger候補にしない。

新Exitを選ぶ場合は、安定領域の中央付近を優先し、最大EVセルを選ばない。

---

# 16. Layer 5 — 511 Model Analysis

対象：

```text
Current Core
Existing Challengers
Controls
Frozen Challengers
Current β
Historical β generations
```

最低限：

```text
Resolved Unique N
30m Cluster N
Active Days
Gross EV
Net EV
Net Coverage
Max DD
Loss Streak
Time Concentration
Symbol Concentration
Direction Dependence
Regime Dependence
Execution Delay
Unknown Rate
```

---

# 17. Layer 6 — Execution / Net Analysis

`511_execution.csv` を利用して最低限：

```text
entry bid / ask
exit bid / ask
quote timestamp
decision timestamp
data age
quote delay
gross_R
spread_R
fee_R
slippage_R
net_R
cost_status
missing_cost_inputs
```

Practice Candidate判断ではNetを重視する。`net_R=NULL` を0扱いしない。

---

# 18. Hypothesis Registry

有望な構造を見つけた場合、`HYP-000001` のようなIDを発行する。

保存：

```text
hypothesis_id
created_at_jst
scan_id
discovery_start_jst
discovery_end_jst
source_cutoff_jst
parent_model_id
hypothesis_family
hypothesis_description
economic_or_market_rationale
features_used
parameter_values
candidate_count_examined
discovery_unique_n
discovery_cluster_n
discovery_gross_ev
discovery_net_ev
multiple_testing_family_id
status
rejection_reason
duplicate_of
```

---

# 19. Hypothesis Status

```text
DISCOVERED
REGISTERED
CHALLENGER_CREATED
FORWARD_TESTING
REJECTED
PROMOTED
DUPLICATE
EXPIRED
INVALID_DATA
```

`REJECTED` を削除しない。

---

# 20. Hypothesis Graveyard

失敗した仮説を永久保存する。

目的：

- 同じ仮説の再探索抑制
- 名前だけ変えた再探索抑制
- 生存者バイアス防止
- 試行回数監査

類似仮説には `possible_duplicate_of` を保存する。

---

# 21. Trial Ledger

研究で試したすべての候補を保存する。

```text
trial_id
scan_id
hypothesis_family
features
parameters
dataset_start_jst
dataset_end_jst
candidate_family_size
candidate_index
result_summary
selected_for_registration
selection_reason
```

200候補を比較して1つを採用した場合、`candidate_count_examined = 200` とする。

---

# 22. AI探索回数

ChatGPT/Codexが比較した候補も探索回数に含める。完全な総数を取得できない場合は `candidate_count_quality=UNKNOWN` とし、過小評価しない。

---

# 23. 探索空間制限

Adaptive v1.1 FINALでは、1 Challengerにつき**親モデルから1つの主要概念変更**を原則とする。

許可例：

```text
Core + Persistence
Core + Asset Class
Core + Exit Family
Core + Regime Feature
```

最初から多数条件を重ねたモデルは禁止。多変数interactionは、構成要素が個別に根拠を得た後に別Hypothesisとして登録する。

---

# 24. 銘柄・時間帯最適化

銘柄・時間帯sliceは診断に使用してよい。

ただし「負けている銘柄を削る」「悪い時間帯を削る」を同一モデルの修正として扱わない。除外版は新Challengerとして、作成後の未来データだけで評価する。

---

# 25. Challenger

Hypothesisを未来検証へ進める場合、`CH-000001` のようなIDを発行する。

保存：

```text
challenger_id
hypothesis_id
parent_beta_id
created_at_jst
forward_start_jst
model_spec_json
model_spec_hash
feature_schema_version
adaptive_spec_version
git_commit_hash
status
```

---

# 26. Challenger Freeze

Challenger作成時に判定条件を完全固定する。

以下の変更は新IDが必要：

- Entry条件
- Regime条件
- Universe
- Direction restriction
- Exit
- Threshold
- Asset filter
- Time filter
- Symbol filter
- Feature definition

---

# 27. Challenger Forward Start

作成後の最初の完全な5分bucketを `forward_start_jst` とする。

それ以前はDiscovery。過去へ遡って正式Forward成績を作らない。

---

# 28. 5日ScanでChallenger作成可能

研究速度確保のため、5日ScanでChallengerを作成してよい。

```text
Day 5: HYP-A → CH-A Freeze → Day 5以降Forward
Day10: HYP-B → CH-B Freeze → Day10以降Forward
```

---

# 29. Generation Gate最低Forward条件

Adaptive v1.1 FINAL初期値：

```text
forward_age_days >= 10
resolved_unique_events >= 150
cluster_30m_n >= 60
active_days >= 7
```

不足なら `INSUFFICIENT_FORWARD`。

これはPractice Candidate条件ではない。

---

# 30. Generation Gate評価項目と人間承認

v1.1 FINALのGeneration Gateは完全自動Promotionではない。
システムは§29の最低Forward条件、Net品質、Robustness、多重比較結果、過去Gate履歴、明示的BLOCK条件を自動計算・保存する。

最低限の出力：

```text
Net EV / Gross EV / Net Coverage
30m Time Cluster Bootstrap / Day Cluster Bootstrap
Parent Difference / Control Difference
Max DD / Loss Streak
Symbol / Time Concentration
Direction / Regime Dependence
Execution Quality
Multiple Testing Adjustment
Gate Evaluation History
Explicit Block Reasons
```

事前固定された当該段階の最低条件を満たし、明示的BLOCKがない候補のシステム判定は
`ELIGIBLE_FOR_PROMOTION_REVIEW` とする。これは昇格・有意性・実践認定を意味しない。
Net、Robustness、多重比較の未定義の閾値や主観的スコアを追加しない。
未定義の診断項目は§42〜48に従い表示し、人間の審査資料とする。

最終 `PROMOTE_TO_BETA` は人間の承認が必須。
承認者、承認時刻、候補ID、Gate ID、model hash、参照データcutoff、判断理由を保存する。
承認によって明示的BLOCK、最低Evidence条件、最大1モデル制約を解除できない。
複数Eligible候補をEV順で自動選択しない。人間が選ばなければ `NO_CHANGE`。
人間が条件を変更した候補をそのまま承認することも禁止し、変更版は新ID・新hashで再Freezeする。

---

# 31. 反復Gate / Optional Stopping管理

同一Challengerを15日ごとに再審査すること自体は許可するが、「有意になるまで待つ」ことをPromotion根拠にしてはならない。

各Challengerについて保存：

```text
evaluation_count
gate_ids
evaluation_timestamps
decision_history
```

`CONTINUE_TESTING` から次回Gateへ持ち越した場合も、過去の審査履歴を消さない。

Generation Gateは統計的有意性の一発判定ではなく、

- 事前固定最低N
- Effective N
- Net品質
- Robustness
- Concentration
- Multiple-testing status
- 過去Gate履歴

を人間が確認するための**世代管理Gate**とする。システムは§30のEligible判定までを担当し、総合自動スコアリングは行わない。

最終的な独立確認はFinal Confirmationに委ねる。

# 32. 多重比較管理

最低限：

```text
family_id
family_candidate_count
global_candidate_count
```

を保存する。

統計手法の優先順位：

1. 30m-cluster bootstrapによるmax-statistic / step-down
2. 実装困難ならHolm補正
3. 補正不能なら `MULTIPLE_TESTING_UNRESOLVED`

補正不能なら `MULTIPLE_TESTING_UNRESOLVED` を保持し、§70の明示的BLOCKとして審査する。補正不能を有意性の証拠とせず、人間承認でこのBLOCKを解除しない。自動Promotionは常に禁止する。

---

# 33. Random CV禁止

正式検証では random train/test split、random k-fold、shuffle splitを使用しない。

時間順のみ。

```text
Past → Discovery → Frozen Model → Future Forward → Later Future Confirmation
```

---

# 34. β Generation

`ELIGIBLE_FOR_PROMOTION_REVIEW` 候補のうち、人間が承認した最大1モデルだけを次世代βへ昇格させる。
β番号は性能順位ではない。人間承認前にβを生成しない。
同一 `model_spec_hash` の昇格はモデル条件変更ではなく、累積Forwardの開始時刻をリセットしない。

---

# 35. 1 Gate最大1 β

1回のGeneration Gateで新βは最大1。例外として自動で2モデル以上を昇格させない。
複数Eligible候補のEVランキングによる自動選択は禁止。
人間の選択がなければ `NO_CHANGE`。その他の候補は凍結条件のままChallengerとして継続可能。
同一Gateの再実行・再承認でもβを二重生成しない。

---

# 36. NO_CHANGE

新βを作る十分な根拠がない場合、またはEligible候補があっても人間が選択・承認しない場合は `NO_CHANGE`。

正常な結果とする。

---

# 37. β Registry

保存：

```text
beta_id
parent_beta_id
source_challenger_id
promoted_at_jst
model_frozen_at_jst
cumulative_forward_start_jst
beta_only_start_jst
forward_start_jst
model_spec_hash
git_commit_hash
status
evidence_summary
```

状態：

```text
ACTIVE
SHADOW
REPLICATED
REJECTED
RETIRED
FINAL_CONFIRMATION
PRACTICE_CANDIDATE
```

---

# 38. 旧β保存

新β作成後も旧βを削除しない。可能な範囲でShadow評価を継続する。

目的はEdge decay、新旧比較、最新β偏重防止。

---

# 39. β順位付け禁止

`EVが最高 = 最良β`、`最新β = 最良β` と扱わない。

各βは同一model hashのChallenger Freeze後、最初の完全bucketからの累積Forward証拠で評価する。加えてβ昇格後専用成績を別表示する。昇格前の同一モデルの正当なForwardをDiscoveryとして捨てたり、別モデルのDiscoveryを引き継いだりしない。

---

# 40. Practice Candidateへの独立ステージ

```text
DISCOVERY
→ CHALLENGER
→ BETA
→ REPLICATED
→ FINAL_CONFIRMATION
→ PRACTICE_CANDIDATE
```

β昇格だけでは実弾候補ではない。

---

# 41. Practice Candidate最低条件

v1.1 FINALの固定初期基準：

```text
model_frozen_age >= 28 days
resolved_unique_events >= 400
time_cluster_30m_n >= 150
active_days >= 20
formal_net_coverage == 100%
net_ev > 0
```

`model_frozen_age` の起算点は **Challenger Freeze時点** とする。

Challengerからβへ昇格しても `model_spec_hash` が完全に同一なら、Freeze以降の未使用Forward evidenceを継続して数える。β昇格日から28日を数え直さない。

集計は次の2系統を必ず区別する。

- `CUMULATIVE_FORWARD`: 同一hashのChallenger Freeze後、§27のforward_start以降のEntryを累積する。
- `POST_BETA_PROMOTION`: 同一hashであっても `entry_time >= promoted_at_jst` のEntryだけを対象とする。

昇格前にEntryして昇格後に決済したTradeは累積Forwardにのみ含め、β昇格後専用集計へ移さない。
2集計に同じEventが含まれても足し合わせて独立サンプル数を増やさない。
Freezeからforward_startまでの未完成bucket、Discovery、他hashの証拠は累積にも含めない。

Entry、Exit、Universe、Threshold、Direction、Symbol/Time filter、Feature定義等を変更して `model_spec_hash` が変わる場合は新モデルとし、Forward clockを0から開始する。

Net Coverageの正式区分：

```text
100%                 = FORMAL_NET_COMPLETE
95% <= coverage <100 = NET_COVERAGE_INCOMPLETE
coverage <95%        = NET_INSUFFICIENT
```

欠損 `net_R` を0として補完してはならない。

`NET_COVERAGE_INCOMPLETE` は研究・診断には利用できるが、Practice Candidateの正式認定には利用しない。

現在のForward運用でFee/Slippage設定が不足しNet評価可能件数が0である場合、これはPractice/Finalへ進む前に解消すべき **EXECUTION_COST_CONFIG_BLOCKER** とする。実測・正式設定なしにFee/Slippageを推測して埋めてはならない。

Fee/Slippageの正式設定作業はAdaptive実装と分離する。`MISSING_FEE_CONFIG` または必要Slippage設定不足によるNet評価0件は、Practice予備認定・Final開始前に解消必須。
このBlockerは基盤実装・欠損を明示した診断レポートの作成を禁止する意味ではない。
既存の確定済みβ1結果のNULLを後から推測値で埋めない。費用設定後も過去の欠損を母集団から隠して100%と報告しない。
設定を解消しても、正式評価対象のNet completenessが100%にならなければPractice/Finalへ進めない。

上記は最低サンプル条件であり、400件そのものを統計的有意性の保証とはみなさない。後述のbootstrap、robustness、Final Confirmationを別途要求する。

# 42. Time Robustness

複数の非重複時間blockで評価する。基本は7日単位の非重複block。

全利益が1 blockに集中する場合は `TIME_CONCENTRATED`。

全blockプラスを必須にしない。利益集中はblock別の数値・除外時集計として保存・表示する。未定義の「大勝」「重大」を自動失格条件として使わず、v1.1 FINALでは診断情報とする。

---

# 43. Symbol Robustness

計算：

```text
top1_symbol_profit_share
top3_symbol_profit_share
leave_one_symbol_out_EV
remove_top3_symbols_EV
symbol_positive_count
symbol_negative_count
```

1銘柄除外時のEV変化を含めて保存・表示し、`SYMBOL_CONCENTRATED` は診断情報として扱う。未定義の「Edge消失」「重大な集中」を自動失格閾値にしない。

その銘柄を後から削除して同一モデルのForwardを再計算しない。除外版は新Challenger。

---

# 44. Direction Robustness

```text
LONG N / EV
SHORT N / EV
```

片方向だけプラスでも即失格にしないが `DIRECTION_DEPENDENT` として記録する。方向限定版は新Challenger。

---

# 45. Regime Robustness

最低限診断：

```text
Crowding
Persistence
BTC Direction
BTC Volatility
Asset Class
ATR
Volume
OI
Funding
```

すべてのRegimeがプラスである必要はない。1つの極端な市場状態だけで全利益が生じていないかを確認する。

---

# 46. Effective N / 30分Cluster定義

必ず併記：

```text
Unique Event N
5m Time Cluster N
30m Time Cluster N
30m × Direction Diagnostic Cluster N
Active Day N
```

**正式なbootstrap / resampling unitは方向を統合した30分の時間bucket** とする。

同じ30分時間bucket内に存在するLONG、SHORT、複数銘柄のEventは、統計的再標本化では同一clusterとして一緒にresampleする。

理由は、同一市場時間帯のLONGとSHORTを別clusterに分けても独立性が保証されないためである。

既存β1の診断・IDとしての「30分×方向cluster」は変更せず保持してよい。ただしAdaptive Researchの正式な `cluster_30m_n`、bootstrap、Practice/Final判定では **方向統合30分Time Cluster** を使用する。

同じ30分に20銘柄Signalが出ても、20完全独立サンプルとはみなさない。

# 47. Drawdown / Loss Streak

保存：

```text
max_drawdown_R
max_event_loss_streak
max_negative_5m_cluster_streak
max_negative_30m_cluster_streak
```

Practice Candidate通知時に必ず含める。

---

# 48. Bootstrap

最低限：

```text
30m time-cluster bootstrap 95% CI
day-cluster bootstrap 95% CI
```

30m bootstrapでは、同一30分時間bucket内の全Event（LONG/SHORTを含む）を同一resampling clusterとして扱う。

Practice Candidate予備認定では原則、

```text
30m_time_cluster_ci_lower > 0
```

を要求する。

day-cluster CIは数値を保存・表示する。「大きく反証」の定量境界は未定義なので、これを自動失格条件にしない。

Time/Symbol/Direction/Regime、day-cluster、UNKNOWN率、Execution品質等は、明示的に固定されたBLOCK規則を除いて診断情報とする。
診断値の算式・母集団・欠損・算出不能理由を保存し、未定義のラベルから独自の失格閾値を追加しない。
将来これらを自動判定条件にする場合は、新仕様versionで事前固定し、その導入後の対象モデルに適用する。現モデルのForward結果を見て後付け適用しない。

同じモデルをGateごとに繰り返し検定して「有意になった回だけ採用」することを避けるため、各Challengerについて以下を保存する。

```text
evaluation_count
first_evaluation_at_jst
gate_evaluation_history
```

Generation Gateでは、単一回のp値やCIが閾値を越えたことだけをPromotion理由にしてはならない。Gateは事前固定最低Evidenceの適格性判定と、人間によるRobustness審査の段階とし、最終的な未使用データによる認定はFinal Confirmationへ寄せる。

将来、反復Gateを正式な逐次統計検定として扱う場合は、alpha spending等を含む別仕様を作成する。v1.1 FINALでは「通るまで待つ」optional stoppingをPromotion根拠にしない。

# 49. Final Confirmation — 固定日次評価・一度の正式判定

Practice Candidate予備条件とExecution Cost Blocker解消を満たしたモデルだけを `FINAL_CONFIRMATION` へ進める。
Final開始時にmodel hash、Final start、JST日次評価時刻、cutoff規則を記録し固定する。

```text
final_start_jst
final_daily_evaluation_time_jst  # HH:MM:SS、Final開始前の必須設定
scheduled_evaluation_at_jst
evaluation_cutoff_jst
source_cutoff_jst
evaluated_event_ids
pending_reference_event_ids
final_decision_count
final_decision
```

固定JST時刻に1日1回だけ評価する。時刻未設定ならFinalを開始しない。
日次のevaluation cutoffは事前固定の予定評価日時とし、ジョブ遅延や成績を理由に動かさない。
対象母集団は各日次評価について以下で固定し、対象ID・cutoffを保存する。

```text
Final start <= entry_time < evaluation cutoff
```

Final開始前のEntryが開始後に決済しても対象外。cutoff以後のEntry・価格・quote等をそのcutoffの判定へ混入させない。
Reference結果がcutoff時点で未確定の対象Tradeを除外してPASSさせない。
未確定Tradeがあれば正式判定を保留し、決済または適用deadline規則による確定を待ち、翌日の固定時刻に評価する。
最大保有時間が経過したことだけで価格・結果を捏造しない。β1の価格欠損・UNKNOWN・deadline境界規則を維持する。

deadlineには各モデルの凍結済み最大保有時間を使用する。β1は3時間、Adaptiveの6/12/24時間Exitモデルはそれぞれ凍結した6/12/24時間を維持する。
Finalのために保有時間を一律3時間へ変更しない。最大保有時間とdeadline境界規則はmodel_spec_hashの対象であり、Final開始後に変更しない。

最低データ条件はFinal期間だけで判定する。

```text
elapsed_calendar_days >= 14
resolved_unique_events >= 150
time_cluster_30m_n >= 60
active_days >= 10
formal_net_coverage == 100%
pending_reference_event_count == 0
```

未達なら `FINAL_INSUFFICIENT_DATA`。モデル条件を変えず、翌日の固定評価時刻まで延長する。
延長は経過日数・件数・未確定Trade・Net完全性等のデータ準備条件だけで決め、Net EVやCIの良否によって延長しない。
UNKNOWNは既存の定義どおり確定したUNKNOWN結果として記録し、resolved Grossの件数・EV分母から除外する。未確定Tradeを便宜的にUNKNOWNへ変更しない。

最低データ条件と明示的BLOCK解消を初めて満たした日次評価で、正式判定を一度だけ行う。

```text
PASS条件:
final_net_ev > 0
final_30m_time_cluster_bootstrap_95_ci_lower > 0
Data Integrity / Execution Cost / Model Hash / Net completenessに明示的BLOCKなし
```

上記を満たさなければ正式FAIL。正式PASS/FAIL後は同じFinalを再判定・期間延長して合格へ変更しない。
判定記録・モデルhash・対象Event ID・参照snapshot・CI設定を固定保存する。
再実行は同じ判定の読出しに限定し、final_decision_countは最大1。
準備条件未達の間は正式合否判定を繰り返さず、Final損益をモデル選択・条件変更へ利用しない。

---

# 50. Final中の禁止事項

Final期間中：

- Entry変更禁止
- Exit変更禁止
- Universe変更禁止
- Threshold変更禁止
- Direction変更禁止
- Symbol除外禁止
- Time filter変更禁止
- Feature定義変更禁止

変更した場合、そのFinalは失効し、新しいmodel_spec_hashを持つ別モデルとして扱う。

# 51. Finalデータ再利用禁止

β4がFinalで失敗した場合、その期間をDiscovery材料としてβ5を作ること自体は可能。

ただしβ5は、

> β5 Freeze後のさらに新しい未来期間

をFinalとして使用する。

同じFinal期間を別モデルの最終試験として使わない。

原則としてFinal Confirmationは、モデル選択のために何度も覗くデータではなく、事前固定モデルに対する一度の独立確認として扱う。

# 52. PRACTICE_CANDIDATE_READY

すべての事前固定条件とFinal Confirmationを通過した場合のみ発行する。

通知内容：

```text
Model
Frozen At
Forward Days
Unique N
30m Cluster N
Active Days
Gross EV
Net EV
Net Coverage
30m Cluster CI
Day Cluster CI
Max DD
Max 30m Loss Streak
Top1 Symbol Share
Remove Top3 EV
Forward Block Summary
Final Confirmation
Hypotheses Examined
Challengers Tested
Adaptive Spec Version
Model Hash
Git Commit
```

---

# 53. 禁止する通知表現

```text
勝てるモデルを発見しました
永久に勝てます
利益が保証されます
```

正式表現：

> 事前固定した511 Practice Candidate認定基準を、未使用Forwardデータ上で通過したモデルが存在します。

---

# 54. Micro Live

Practice Candidate認定後も通常資金運用へ直行しない。

まず `MICRO_LIVE` へ進み、real fill、fee、slippage、latency、rejected order、partial fill、quote qualityを測定する。

Micro Liveは利益最大化フェーズではない。

---

# 55. Live研究との分離

Micro Live結果を見てモデル条件を変更した場合、別モデルとする。実弾モデルをその場で最適化しない。

---

# 56. Edge Decay Monitoring

実践開始後もShadow評価を継続する。

状態：

```text
HEALTHY
WATCH
DEGRADING
INSUFFICIENT
```

劣化条件はLive開始前に固定する。

---

# 57. Authoritative State

Adaptive Researchも既存511と同じ思想を採用する。

```text
511_adaptive/adaptive_state.json
```

をauthoritative sourceとし、CSVは再生成可能projectionとする。

---

# 58. 推奨保存構造

```text
511_adaptive/
    adaptive_state.json
    research_scan.csv
    hypothesis_registry.csv
    trial_ledger.csv
    challenger_registry.csv
    beta_registry.csv
    gate_decisions.csv
    qualification_history.csv
    final_confirmation.csv
    adaptive_audit.log
```

ファイル数自体を研究量の指標にしない。

---

# 59. adaptive_state.json 推奨トップレベル

```json
{
  "spec_version": "1.1 FINAL",
  "anchor_jst": null,
  "feature_schema_version": "1",
  "last_scan_jst": null,
  "last_gate_jst": null,
  "scans": [],
  "hypotheses": [],
  "trials": [],
  "challengers": [],
  "betas": [],
  "gate_decisions": [],
  "qualifications": [],
  "final_confirmations": [],
  "audit": []
}
```

---

# 60. Feature Schema Version

新しい保存特徴量を追加した場合、

```text
feature_schema_version
feature_available_from_jst
```

を保存する。

過去に存在しなかった観測値を、当時取得済みだったかのようにBackfillしない。

再計算可能な派生特徴量と、当時観測が必要なExecution特徴量を区別する。

---

# 61. Data Provenance

すべてのResearch結果について、可能な限り次を追跡する。

```text
source_file
source_commit
source_cutoff_jst
source_schema_version
```

---

# 62. ChatGPTの役割

ChatGPTは次を担当可能。

- GitHub上の最新研究データ取得
- 5日Scanの解釈
- 仮説形成
- 過去仮説との重複確認
- Robustness検討
- 研究レポート
- Codex用実装指示案作成

ChatGPTは承認者を代行しない。システムは保存された客観条件でEligibleまでを判定し、最終Promotionは人間の明示承認を必要とする。

---

# 63. Codexの役割

Codexは次を担当する。

- Adaptive Researchコード実装
- state管理
- CSV projection
- CLI
- GitHub Actions統合
- tests
- migration
- report generation

禁止：

```text
一番勝てる閾値を探して自動的にProductionへ書き込む
```

---

# 64. GitHubの役割

GitHubを以下の監査基盤とする。

```text
Code History
Spec History
Model History
Research State
Audit Trail
Test History
```

各モデルに、

```text
spec_version
spec_hash
model_spec_hash
git_commit_hash
```

を紐付ける。

---

# 65. main自動変更禁止

Research結果だけを理由にProduction/Mainロジックを無審査で書き換えない。

基本フロー：

```text
Research Result
→ Hypothesis
→ Challenger Spec
→ Code
→ Tests
→ Shadow Forward
→ Gate
```

---

# 66. CLI要件

最低限：

```bash
python -m model511_adaptive scan
python -m model511_adaptive gate
python -m model511_adaptive report
python -m model511_adaptive validate-state
```

`--as-of` はBACKFILL/再現テスト専用とし、正式Forward Scanで任意の過去時刻を選んで研究期間を都合よく変えない。

---

# 67. GitHub Actions統合

既存 `MEXC Research` の5分Workflowを壊さない。

Adaptive Researchは、5分Workflow内でDue判定しても、別Scheduled Workflowでもよい。

必須条件：

> Adaptive失敗で既存Research保存を失敗させない。

---

# 68. Research Scan Report

各Scanで最低限：

```text
Scan Metadata
Data Quality
4 Strategy Summary
Cross-Strategy Summary
Regime Summary
Exit Robustness
511 Core Summary
Current β Summary
Current Challengers
Execution / Net Summary
New Hypotheses
Rejected / Duplicate Hypotheses
Candidate Count
Multiple Testing Status
Warnings
```

新仮説0件でも成功とする。

---

# 69. Generation Gate Report

最低限：

```text
Gate ID
Current β
Eligible Challengers
Forward Age
Unique N
30m Cluster N
Active Days
Gross EV
Net EV
Net Coverage
Bootstrap CI
DD
Symbol Robustness
Time Robustness
Direction Dependence
Regime Dependence
Multiple Testing Adjustment
Decision
Reason
```

---

# 70. 明示的BLOCKとPromotion権限

v1.1 FINALでは完全自動Promotionを常に禁止する。
人間承認があっても、以下の明示的な適用条件・BLOCKを無視して昇格や正式認定を行わない。

```text
事前固定規則に基づくData Integrity BLOCK
当該段階に事前固定されたNet completeness条件の不足
Multiple testing unresolved
当該段階のForward N / Time Cluster N / Active Days / 経過日数不足
Model hash mismatch
データ時刻・provenanceから確認されたLookahead / Feature availability不整合
Final data contamination
Practice/Final開始時のEXECUTION_COST_CONFIG_BLOCKER
Final正式判定時の未確定Reference Trade
```

Gate最低条件は§29、Practice最低条件は§41、Final最低条件は§49に従い、別段階の閾値を暗黙に流用しない。
根拠未確定の疑いは監査と人間への確認事項とし、独自閾値で自動失格判定しない。
Time/Symbol concentrationの「severe」、day-clusterの「大きく反証」、UNKNOWN率、Execution品質等に未定義の自動BLOCKを設けない。
これらは診断情報。将来の自動条件化は新仕様versionで事前固定し、現モデルへ後付けしない。

---

# 71. Anti-Overfitting Constitution

Adaptive Research v1.1 FINALの最重要ルール。

1. DiscoveryとValidationを時間分離する。
2. 仮説作成以前のデータをその仮説のForwardに含めない。
3. モデル変更は新ID。
4. 全探索をTrial Ledgerへ残す。
5. 失敗仮説を削除しない。
6. Fine threshold tuningを禁止する。
7. Random CVを禁止する。
8. Best Exit Cellを禁止する。
9. 単一銘柄依存を診断する。
10. 単一時間帯依存を診断する。
11. 多重比較を管理する。
12. βを必ず作る義務を設けない。
13. 最新βを自動的に優位とみなさない。
14. Final Holdoutを繰り返し覗かない。
15. Final失敗期間を別モデルのFinalへ再利用しない。
16. GrossだけでPractice Candidateにしない。
17. deadlineを理由に基準を緩めない。
18. 変更理由・データ範囲・commit・hashを監査可能にする。

---

# 72. Adaptive v1.1 FINALで禁止する研究手法

```text
Genetic Algorithm
Bayesian Optimization
Massive Grid Search
Automatic Feature Selection over hundreds of features
Black-box AutoML
Symbol-by-symbol optimization
Time-of-day fine tuning
Thousands of thresholds
Best historical Exit auto-selection
Auto Live promotion
```

必要になれば別仕様とする。

---

# 73. 推奨初期スケジュール

```text
Day 0
Adaptive v1.1 FINAL開始

Day 5
Scan #1
→ HYP-001
→ CH-001 Freeze

Day 10
Scan #2
→ HYP-002
→ CH-002 Freeze

Day 15
Scan #3 + Generation Gate #1

CH-001:
10日Forward
→ 条件を満たせば審査

CH-002:
5日Forward
→ INSUFFICIENT_FORWARD（Forward収集を継続）

根拠不足:
→ NO_CHANGE
```

以後Day20、25、30…と継続する。

---

# 74. 年内Micro Live目標の扱い

運用目標：

> 2026年内にPractice Candidateが出ればMicro Liveへ進める。

ただし `2026-12-31` はPromotion条件ではない。

年末時点で基準未達なら `NO PRACTICE CANDIDATE` が正しい結果。

---

# 75. テスト要件

## Time Separation

- Discovery期間がForwardに混入しない。
- Challenger作成前データをForwardに含めない。
- 同一model hashならChallenger Freeze後の正当なForwardをβ昇格後も累積する。
- β昇格後専用集計には、Entryがβ昇格前のEventを含めない。
- 累積Forwardとβ昇格後専用集計を合算してサンプル数を増やさない。
- hash変更時は新モデルとしてForward時計をリセットする。
- Final開始前データをFinalに含めない。

## Model Identity

- 条件変更でmodel_spec_hashが変わる。
- 条件変更時に同一challenger_idを使えない。
- Git commit / spec hash不整合を検出する。

## Trial Accounting

- candidate_countを保存する。
- REJECTED Hypothesisが消えない。
- duplicate hypothesisを検出する。
- Trial Ledger再生成一致。

## Exit

- 60 variantsを60 independent samplesと数えない。
- Best Exitを自動採用しない。
- same-bar TP/SL UNKNOWNを保持する。

## Gate

- insufficient NでPROMOTEしない。
- unresolved multiple testingでPROMOTEしない。
- Data Integrity BLOCKでPROMOTEしない。
- システムはELIGIBLE_FOR_PROMOTION_REVIEWまでしか判定しない。
- 人間承認なしにPROMOTE_TO_BETAを記録しない。
- 複数Eligible候補をEV順で自動選択しない。
- 人間が選ばなければNO_CHANGE。
- 1 Gate最大1 β。
- NO_CHANGE可能。

## Practice Candidate

- Grossだけで認定しない。
- insufficient net coverageで認定しない。
- Final未実施で認定しない。
- Final contaminationで認定しない。

## Final日次・診断境界

- 固定JST時刻未設定ではFinal開始不可。
- 同じJST日の再実行で日次評価・正式判定を重複させない。
- Final start <= entry_time < evaluation cutoff の両境界を検証する。
- 対象の未確定Reference Tradeを除外してPASSしない。
- β1の3時間とAdaptiveの6/12/24時間について、各モデルの凍結済み最大保有時間を使用する。Final開始でExitを短縮しない。
- deadline到達だけで価格・結果を捏造しない。
- データ不足のみ翌日へ延長し、Net EV/CIの不合格を理由に延長しない。
- 正式判定は最初の準備完了時点で一度だけ。PASS/FAIL後の再判定禁止。
- UNKNOWNの確定と未確定Tradeを区別する。
- 未定義のRobustness/Execution閾値を自動BLOCKに使用しない。
- 推測Costによる過去Net補完を禁止する。

## State

- atomic write
- restart idempotency
- duplicate prevention
- state / CSV projection一致

---

# 76. 実装推奨モジュール

```text
model511_adaptive/
    __init__.py
    __main__.py
    config.py
    scheduler.py
    source_reader.py
    data_quality.py
    strategy_analysis.py
    cross_strategy.py
    regime_analysis.py
    exit_analysis.py
    execution_analysis.py
    hypothesis_registry.py
    trial_ledger.py
    challenger.py
    beta_registry.py
    gate.py
    qualification.py
    final_confirmation.py
    statistics.py
    storage.py
    reporting.py
    workflow_save.py
```

既存 `model511` を破壊的変更せず、必要に応じて共有utilityのみ切り出す。

---

# 77. Adaptive v1.1 FINAL初期判定値

実装前に最終承認し、承認後はForward成績を見て変更しない。

```text
Research Scan: 5日
Generation Gate: 15日
Gate Forward Age: 10日
Gate Unique N: 150
Gate 30m Cluster N: 60
Gate Active Days: 7
Practice Frozen Age: 28日
Practice Unique N: 400
Practice 30m Cluster N: 150
Practice Active Days: 20
Practice Formal Net Coverage: 100%
Final Evaluation: 事前固定JST日次時刻、1日1回
Final Confirmation Base Period: 14日
Final Resolved Unique N: 150
Final 30m Time Cluster N: 60
Final Active Days: 10
Final Formal Net Coverage: 100%
Final Pending Reference Trades: 0
Final Formal Decision Count: 最大1
Promotion: Eligible候補への人間承認必須、1 Gate最大1モデル
```

変更する場合はAdaptive Spec Versionを上げる。

---

# 78. 最終思想

このシステムは「過学習を完全に防ぐシステム」ではない。

Discoveryでは過学習が一定程度発生することを前提とする。

目的は、

```text
広く探索
↓
全探索履歴を保存
↓
少数だけChallengerとして凍結
↓
未知の未来データへ晒す
↓
弱いものを落とす
↓
Generation Gate
↓
さらに長いForward
↓
Final Confirmation
↓
それでも残ったものだけ通知
```

という構造を作ること。

最重要思想：

> **研究速度は速く、昇格速度は遅く、実弾認定はさらに厳しくする。**

---

# 79. FINAL Freeze Declaration

本仕様 `511 Adaptive Research / Model League v1.1 FINAL` は実装基準版とする。

実装開始後に、Forward成績を見たことを理由として以下を変更してはならない。

- 5日Research Scan
- 15日Generation Gate
- Gate最低N
- Practice最低N
- Net completeness条件
- Final最低N
- Final PASS条件
- Final日次評価時刻・対象Entry母集団・一度の正式判定
- 累積Forward／β昇格後専用集計の境界
- 人間承認必須のPromotion権限
- bootstrap cluster定義
- 28日起算点

重大な統計設計上の欠陥、lookahead、データリーク、実装不能要件が判明した場合のみ仕様改訂を許可する。その場合はversionを上げ、改訂日時と理由をAuditへ保存する。成績改善だけを理由とする仕様改訂は禁止する。

# 80. Done Definition

Adaptive Research v1.1 FINAL実装完了条件：

- 既存511 β1が変更されていない。
- 4戦略をResearch Sourceとして読める。
- Exit ResearchをResearch Sourceとして読める。
- 5日Research Scanが再現可能。
- 15日Generation Gateが再現可能。
- Hypothesis Registryが永続化。
- Trial Ledgerが永続化。
- Frozen Challengerを作成可能。
- Challenger Forward Startが固定。
- β Registryが永続化。
- NO_CHANGEを扱える。
- ELIGIBLE_FOR_PROMOTION_REVIEWと人間承認を区別できる。
- 同一hashの累積Forwardとβ昇格後専用成績を別保存できる。
- multiple testing statusを保存。
- Practice Candidate評価を実装。
- Final Confirmationを固定JST時刻・母集団・一度の判定で管理可能。
- 未定義の診断閾値を自動失格にしない。
- Execution Cost BlockerをPractice/Final開始前に検知できる。
- `PRACTICE_CANDIDATE_READY` 通知情報を生成可能。
- Adaptive失敗で既存Research保存が止まらない。
- stateがatomic。
- restart/idempotency tests PASS。
- anti-lookahead tests PASS。
- anti-backfill-contamination tests PASS。
- 全自動テストPASS。

---

# 81. Codexへの最重要指示

Codexは本仕様を実装する際、過去成績を改善するために仕様外の条件を追加してはならない。

特に禁止：

- 過去データを見てthresholdを微調整
- 勝っている銘柄だけを残す
- 負けている時間帯を削除
- Best Exitを採用
- LONG/SHORTの片方を過去成績で削除
- Candidate数を記録せずWinnerだけ保存
- DiscoveryデータをForwardとして再利用
- Final期間を何度もモデル選択に利用
- Practice Candidateを自動Live化
- 既存511 β1の判定条件を変更

Adaptive Layerは、

> **「勝者を作る機械」ではなく、「偶然の勝者を落とし続ける研究機械」**

として実装すること。

---

# 82. 今回の整合改訂の確認状態

利用者指定6項目を反映。β1の研究条件、固定Exit、UNKNOWN規則、確定済みForward、既存stateは変更しない。
同一hashのForward引継ぎ矛盾は解消。Gateは人間承認必須。未定義のRobustness失格は診断へ変更。
Finalは日次のデータ準備確認と一度の正式合否判定を分離し、成績による延長を禁止。
Discovery/別hash/Final開始前のEntryを対象証拠へ混入させない。
親β1のv1.0ファイル名だけは正しい参照として保持する。
§49のdeadlineは利用者確認により各モデルの凍結済み最大保有時間と確定（β1は3時間）。今回の6項目に関する文書上の矛盾は解消した。
Execution Cost設定不足は未解消の運用BLOCKERとして維持する。JST日次評価時刻はFinal開始前に事前固定する必須設定であり、この文書で任意の時刻を補完しない。
これは仕様文書の整合確認であり、未知の市場データで統計的リスクがゼロになる保証や、実装テスト完了を意味しない。
実装は利用者の別途承認まで開始しない。
