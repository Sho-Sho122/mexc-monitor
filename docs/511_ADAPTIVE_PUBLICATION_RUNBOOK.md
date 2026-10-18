# Adaptive 公開・保存・Shakedown 手順

この文書は運用手順です。承認済みRECONCILED仕様・研究条件・Frozen Modelを変更しません。
コード公開、Actions実行、`ADAPTIVE_511_ENABLED=true`、正式Forwardの復旧は別の承認を必要とします。
現在はローカル検証段階です。

## 保存と競合

1. `python -B -m model511_adaptive.persistence prepare --backup "$RECOVERY"`
   はcheckout外の新しいrun専用ディレクトリを要求します。ローカル入力がHEADと不一致なら停止します。
   origin/mainをfetchし、同一commitのResearch・511・Adaptiveファイルを計算前に取得します。
   code/spec変更、既存入力の削除、既存Adaptiveの破損・projection不一致では準備完了にしません。
   HEAD/indexは変更しません。途中失敗時はbaseline完了マーカーを作らず、計算を開始しません。
2. init/tickにはbaseline.jsonの`base`を`--commit`として渡します。Frozen Modelや過去データは変更しません。
3. `archive --backup "$RECOVERY"`で出力を先に保全します。tar、全ファイルSHA-256/Git blob、
   baseline、source SHA、Save Auditを残します。計算失敗時の不完全出力も証跡として保全します。
4. Recovery Artifactのupload成功後だけ`save --backup "$RECOVERY"`を許可します。
   正式stateと全CSV/audit projectionの一致を検証し、Adaptive全ファイルをbaselineと比較します。
   他のResearch/511保存だけが進んだ場合は最新treeにAdaptiveだけを載せて通常pushを再試行します。
   Adaptive変更は`ADAPTIVE_REMOTE_CONFLICT`で停止し、自動マージ・再計算・旧state上書きをしません。
   push応答消失時、remoteが保存予定bytesと完全一致ならALREADY_SAVEDとして二重commitしません。
5. 成否にかかわらず最終Save Auditのuploadを試みます。別workflowの既存Research保存は待機・取消しません。

Recovery/Auditの保持要求は90日です。GitHubの保持上限・実際のexpires_atを確認し、必要なら期限前に保管庫へ保全します。
runner消失、ディスク故障、GitHub Artifactサービス停止中のuploadまでは保証できません。
Recovery upload失敗時は保存を行いません。生出力も同じArtifactへ含め、archive失敗の調査に使います。
新runは最新stateから開始しますが、失敗runのForward候補を無条件に正式証拠へ取り込みません。

## 隔離Recovery

検証済みArtifactから元のbackupディレクトリ構造を取り出し、次を使用します。

```sh
python -B -m model511_adaptive.persistence recover --backup "$RECOVERY" --destination "$QUARANTINE"
```

QUARANTINEはcheckout外・Git repository外の新規ディレクトリです。tarを直接extractせず、
許可されたファイル名・hashを検証してbytesを復元します。既存先は完全一致の場合だけ再実行可能です。
`QUARANTINE.json`には`formal_forward=false`と検証結果を記録します。破損tarは拒否し、
有効なtar内の不完全projectionは調査用として復元できますがpublishable_output=falseになります。
この操作は正式stateの復旧、Forward再計算、時計リセットを行いません。
正式復旧が必要なら、remoteとの差分・欠損・時間的可用性を個別審査し、別途承認を得ます。

## 正式入力と完全性

Discoveryは4戦略CSVとstate、Exit trades/results（archive variantを含む）、β1 stateを読みます。
Forwardは正式β1 stateのEvent historyを参照し、Freeze後の対象Entryだけを評価します。
時刻不正・必要source不足・衝突するExit行・state/projection不一致を利益に応じて除外しません。
`BLOCK_RESEARCH`、population completeness、immutable feature/model hash、availability制約は維持します。
BACKFILLをForwardへコピーせず、Final holdoutをDiscoveryへ再投入しません。

再監査したpoint-in-time sourceは正式保存commit
`a4eda44fdf6e4e34b0e659682eac3ad71dc6fb26`（cutoff 2026-10-18 02:38 JST）です。
保全済み29ファイルをこのcommitのGit blobと照合した隔離コピーで検証しました。最新main全期間の健全性を保証するものではありません。
有効OPEN 18,704、Unique Event 16,060、β1 Event 6,853、結果6,838。
β1 state欠損はこのsnapshotではありませんが、INVALID_SCAN_TIME 1,531件でBLOCK_RESEARCHです。
Net評価可能件数は0です。過去エラー行や既存Forwardを変更してBlockを解除してはいけません。
有効化前には最新固定SHAで同じ確認を繰り返し、解消方法が未承認なら研究を進めません。

## Netに必要な情報

| 項目 | 現在の経路 | 未解決事項 |
|---|---|---|
| Entry quote | β1 Eventの実bid/ask、ticker時刻、decision時刻 | 欠損は補完不可 |
| Exit quote | PublicMarketDataのcontract/ticker。判定時に利用可能なbid/askを使用 | 過去quote履歴を後から捏造しない。遅延と欠損率をShakedownで測定 |
| Fee | `set-cost-config`のfee_bps_entry/exit | 適用口座・銘柄・API経路・maker/taker・有効日を裏付ける正式値と承認が必要 |
| Slippage | 同設定のslippage_bps_entry/exit | 対象数量/流動性と整合する事前の根拠・承認が未確定。tickerだけでは約定slippageは分からない |

MEXC公式の[Fee Details](https://www.mexc.com/api-docs/futures/account-and-trading-endpoints/get-fee-details)
には口座のrealMakerFee/realTakerFee、銘柄指定、割引情報があります。認証付き取得は今回追加・実行していません。
[API fee告知](https://www.mexc.com/announcements/article/updates-to-api-futures-trading-fees-jun-1-2026-17827791535742)
はWeb/Appの表示料率とAPIの扱いが異なることを説明しています。一般告知やサンプル値を本口座の設定として採用しません。
[Ticker仕様](https://www.mexc.com/api-docs/futures/market-endpoints/get-ticker-contract-market-data)
のbid1/ask1は最良気配、timestampの説明はTrade timeです。個別bid/ask更新時刻とは保証されていないため、
現実のquote freshness精度には限界があります。既存のtimestamp/data age/exit delayの意味を変更せず明示します。

設定は`source`、`approved_by`、4つのbps値を必要とします。率からbpsへの単位確認も必要です。
現在の設定は共通スカラーです。銘柄や口座で異なる料率を正しく表せない場合、勝手に代表値を選びません。
設定はEntryごとに保存され、将来の変更は過去の欠損Netを埋めません。ゼロにも証拠が必要です。
Gross − Spread − Fee − Slippageを一度だけ計算し、必要成分不足ではNet=NULLのままです。
ここでSpreadは参照価格からquoteまでの符号付きExecution impactで、遅延中の市場変動も含みます。
Practice/Finalには既定の完全性条件が必要です。費用を未設定のまま開始しません。

## 承認後の公開手順（今回は実行しない）

1. 最新mainをfetchし、今回のAdaptiveコード/docs/testsだけを比較する。既存stateと保存修正を上書きしない。
2. 研究意味論hash・spec hashの不変、全テスト、diff check、秘密情報・生成物不在を再確認する。
3. 対象ファイルだけをcommit。通常fast-forwardで公開できなければ停止し、最新mainへコードだけを再統合・再テストする。
4. 承認されたpush後、既存β1/保存CIとAdaptive CIの結果を確認する。有効化変数はfalseのままにする。
5. 別承認でShakedownを始める。最新source完全性と正式費用の根拠を確認し、Recovery保全先を準備する。
   入力Blockが残る場合、認定を進めず診断だけとし、Block解除に新仕様が必要なら先に承認を得る。
6. 許可された1回のrunでprepareのcheckout/base SHA、先行Recovery upload、save結果、remote全blob、auditを確認する。
7. 再実行でID・anchor・Frozen hash・解決済み結果の不変を確認する。自然な同時Research更新時にも両保存が残ることを確認する。
   本番で意図的に競合データや失敗を注入しない。注入試験は一時local Gitで行う。
8. 異常時は追加runを止め、Recovery/auditを保全する。reset/force pushや過去Forwardへの復元をしない。

## 将来の研究（今回は未実装）

既存AdaptiveはRegime/Direction等の診断集計、Challenger凍結、未使用Forward、Net coverage、
人間承認Gateに対応します。次は粗い少数の市場状態と通貨の性質の診断を検討します。
LONG/SHORT/NO TRADEの独立したShadow判定は追加機能であり、現実装のEntryフィルタと同一視しません。
OPPOSITE内部の細分化、成績に合わせた閾値探索、Asset whitelistは行いません。
過去データは全試行を記録した仮説発見に限定し、新しい意味論は新hashのChallengerとして事前凍結し、
未使用Forwardで検証します。既存モデルの条件や過去Netを変更しません。
