---
name: stock-analyzer
description: "A股财报排雷分析：输入股票代码/名称，自动获取5年三表数据，执行40+条风控规则，输出完整排雷报告（含行业/质押/股东/PDF附注/NLP分析）。用于深度基本面分析中的财报排雷环节。"
agent_created: true
---

# stock-analyzer — A股财报排雷分析

## Overview

输入股票代码或公司名称，执行全自动A股财报排雷分析：
1. 通过 AkShare 获取5年三张主报表
2. 执行45+条自动化风控规则（含多期趋势判断+组合加分）
3. 补充非财务数据（行业分类、股权质押、股东结构、董监高减持）
4. 如有PDF年报则提取附注数据（关联交易、担保、子公司等）
5. 含NLP风险提示分析
6. 输出完整排雷报告（含公式、数据源、计算过程、说明）

## When to Use

- 用户要求分析某只A股的风险
- 用户想了解某公司的财务健康状况
- 需要深度基本面分析中的财报排雷环节
- 输入格式：股票代码（600519）或公司名称（贵州茅台）

## Usage

### 基本用法

```bash
cd <workspace>/financial-risk-analyzer/回测
python analyze.py <股票代码或名称>
```

示例：
```bash
python analyze.py 600519
python analyze.py 贵州茅台
python analyze.py SZ002920
python analyze.py 300267
```

### 数据说明

- **实时数据**：首次运行自动从AkShare拉取5年三表数据（约60-90秒）
- **缓存**：数据缓存到 `tmp_cache/` 目录，下次运行秒级响应
- **PDF提取**：如有上市公司的年报PDF在 `PDF年报/` 目录下，自动提取附注数据
- **依赖**：需已安装 akshare、pypdf 库

### 输出说明

报告按S/A/B/C四级显示所有规则检查结果：
- **S级**：不可能模式（存贷双高）
- **A级**：造假倾向（经营现金流不足、利润现金流不同步等）
- **B级**：风险信号（质押过高、商誉过大等）
- **C级**：财务指标（毛利率、负债率等）
- 每条规则显示：公式、数据源、计算结果、说明

### 评分体系

| 总分 | 等级 | 含义 |
|:---:|:---|:---|
| 0~8 | ✅ 低风险 | 几乎无异常 |
| 8~18 | ⚠️ 中风险 | 需关注异常项 |
| 18~30 | 🔶 高风险 | 多项异常，建议避开 |
| 30+ | 🔴 极高风险 | 强烈暗示操纵，坚决排除 |

注：R-REV1（营收大幅下滑，5/8分）和 R-NP1（归母净利大幅下滑，6/10分）为A级业绩下滑检测规则，使用OPERATE_INCOME/PARENT_NETPROFIT字段计算。

### 一票否决（Layer 0）

- 审计意见非标准无保留 → 直接🚫
- 证监会立案调查/处罚 → 直接🚫
- 审计带强调事项段 → ⚠️ WARN

## Scripts

### `scripts/analyze.py`

主分析脚本。核心功能：

1. `resolve_stock(input_str)` — 解析股票代码或名称
2. `fetch_data(name, symbol)` — 获取5年三表数据
3. `check_regulatory(symbol)` — NeoData监管处罚查询
4. `extract_fields(entry)` — 提取关键财务字段
5. `compute_rules(d)` — 执行40+条财务规则
6. `compute_extra_rules(...)` — 非财务+PDF+NLP规则
7. `compute_score(alerts)` — 评分计算+组合加分
8. `print_rule_summary(...)` — 格式化输出报告

包含：SCORE_MAP（分数映射）、RULE_META（规则元数据）、COMBO_RULES（组合加分）

### `scripts/analyze_pdf.py`

PDF年报数据提取模块（可选），提取：
- 对外担保数据
- 子公司清单（境外子公司检测）
- 关联交易表格检测
- 董监高薪酬总额
- 重大合同/未执行合同
- 分部报告（境外收入）
- NLP: MD&A风险提示分析
- 承诺及或有事项

## References

### `references/rule_list.md`

完整规则列表（含公式、阈值、说明）。脚本运行时会自动加载RULE_META，此处为人工查阅用。
