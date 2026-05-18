# stock-analyzer — A股财报排雷分析工具

基于《手把手教你读财报》的 A 股财报风险排雷系统，45+ 条自动化规则。

## 用法

```bash
python scripts/analyze.py <股票代码或名称>
```

示例：
```bash
python scripts/analyze.py 600519
python scripts/analyze.py 000858
python scripts/analyze.py 兆易创新
```

首次运行自动从 AkShare 拉取 5 年三表数据（约 60-90 秒），数据缓存到 `scripts/tmp_cache/`。

## 部署到服务器（Hermes Agent / 任何 AI Agent）

### 1. 上传

把整个 `stock-analyzer-pack` 目录上传到云服务器任意位置。

### 2. 安装依赖

```bash
cd /path/to/stock-analyzer-pack
pip install -r requirements.txt
```

> Python >= 3.8 即可。建议用 `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` 加速。

### 3. Agent 调用

Hermes agent（或任何能执行 shell 的 AI Agent）直接运行：

```bash
python scripts/analyze.py 603986
```

### 4. 输出说明

脚本输出到 stdout，含：
- 5 年财务概览（营收/净利/毛利率/经营现金流/总资产/存货/研发率）
- S/A/B/C 四级共 57 条规则逐一检查结果
- 每条规则显示：公式、数据源、计算结果、说明
- 总分 + 风险等级

### 5. 规则体系

| 总分 | 等级 | 含义 |
|:---:|:---|:---|
| 0~8 | ✅ 低风险 | 几乎无异常 |
| 8~18 | ⚠️ 中风险 | 需关注异常项 |
| 18~30 | 🔶 高风险 | 多项异常，建议避开 |
| 30+ | 🔴 极高风险 | 强烈暗示操纵，坚决排除 |

### 6. 可选：PDF 年报分析

如有上市公司的年报 PDF 放在 `scripts/PDF年报/` 目录下，自动提取附注数据（关联交易、担保、子公司等）。

## 文件结构

```
stock-analyzer-pack/
├── README.md                  # 本文件
├── requirements.txt           # Python 依赖
├── SKILL.md                   # WorkBuddy skill 定义
├── scripts/
│   ├── analyze.py             # 主分析脚本（核心）
│   ├── analyze_pdf.py         # PDF 附注提取（可选）
│   └── gen_backtest_report.py # 批量回测（开发用）
└── references/
    └── rule_list.md           # 规则列表参考
```
