# ci-chan — 缠论严格实现 · 回测/信号生成（CI 版）

这个仓库只做一件事：把 `chan_strict` 的结构计算放进 GitHub Actions 并行跑，
用来做多票扩样本、口径对照这类"吃 CPU 但不吃机器"的任务。

## 目录

- `chan_strict/`：严格缠论实现（分型/笔/线段/中枢/背驰/买卖点），来自本机策略工程
- `signal_gen.py`：逐 bar 扫描结构 → 信号表（只出信号，不碰资金）
- `portfolio_bt.py`：组合回测执行层（T+1、佣金万 2.5、印花税 0.05%、滑点 0.1%、风险预算定仓位）
- `chan_data.py`：数据层（CI 里由 `ci/patch_data.py` 换成读 `data/min30/*.csv.gz`）
- `data/min30/`：20 只票的 30 分钟前复权数据（2020-01-01~2026-10-29，gzip CSV，约 6.3MB）
- `ci/`：CI 专用脚本
- `.github/workflows/backfill.yml`：手动触发的并行回填

## 用法

本地（在仓库根目录）：

```bash
pip install -r requirements.txt
python ci/run_stock.py --code 600519 --variant ep1      # 单只票，约 3~5 分钟
python ci/merge_and_bt.py --variant ep1                 # 合并 + 回测
```

GitHub Actions：Actions → backfill → Run workflow，可选 `variant`（ep0/ep1）与 `codes`（留空=全部 20 只）。
20 只票并行约 5~8 分钟，产出 `result-<variant>` 工件（stats json / equity / trades / roundtrips）。

## 口径

- `ep0`：只含 2026-10-30 的"笔断流"修复
- `ep1`：ep0 + `require_end_peak`（笔端点必须是区间极值，属工程增补，不是原文硬性要求）

回测统一用"终审那版"配置：`trailing_stop=False`、`panzheng_exit=False`、`regime_ma=0`，
本金 100 万、最多 3 仓、单笔风险 1%。

## 注意

- 本仓库**不含**实盘代码、券商接口、账户信息。
- 数据是从本机 baostock 前复权缓存导出的，不随仓库更新；要换区间请重新导出。
