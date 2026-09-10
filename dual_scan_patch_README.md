
# DualScan Bug 修复应用说明

## 需要在 VM 上做的事情

### 1. 定位 dual_scan.py 源码位置
```bash
find ~ -name "dual_scan.py" -o -name "dual_scan*.py"
```

### 2. 读取 dual_scan_patch.py 的 6 个修复点

修复点 1: `build_candidates()` - 构造候选时带上完整 Stage1 数据
修复点 2: `enrich_recursive_summary()` - 填 trend_score / segment_score
修复点 3: `fix_forming_contradictions()` - 同层 FORMING 矛盾修复
修复点 4: `compute_alignment()` - TREND/SEG/interval 对齐检测
修复点 5: `compute_gate()` - 多因子 gate (新增 OBSERVE 观察池)
修复点 6: `compute_dlp()` - DL_P 日线计算 (需要 kline_cache)

### 3. 主流程集成
```python
# 在原有 dual_scan 主循环里, 每次生成 record 前调用:
enrich_recursive_summary(summary)
fix_forming_contradictions(summary)
alignment = compute_alignment(summary, interval, direction)
gate = compute_gate(direction, summary, interval)
```

### 4. 需要的额外数据源
- price_map: {code: current_price} 实时行情 (akshare/tushare)
- kline_cache: {code: DataFrame} 日线 K 线 (已有 incremental_kline_sync)
- stock_name_map: {code: name} 股票名称 (akshare.stock_info_a_code_name)

## 本地已修复的 JSON

- dualscan_20260909_094550_fixed.json (2141 字段修复)
  - ✅ score 从全 0 → 真实比例
  - ✅ alignment 从全 unverified → 6 种状态
  - ✅ ratio / dlp proxy 值填充
  - ✅ FORMING 矛盾修复
  - ⚠️ price 仍为 null (egress 被挡无法拉行情)
  - ⚠️ dlp 是 proxy 不是真实 DL_P (没有 kline_cache)
