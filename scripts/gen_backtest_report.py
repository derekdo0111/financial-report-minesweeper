"""Generate v3.1 backtest report"""
import json, sys, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import extract_fields, compute_rules, compute_score, SCORE_MAP, COMBO_RULES
from analyze import fetch_profile, fetch_pledge, fetch_holder, compute_extra_rules

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'cache_5y.json')
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '回测报告_v3.1_全量回测.md')

with open(CACHE, 'r') as f:
    data = json.load(f)

BOMB_NAMES = ['康美药业','獐子岛','尔康制药','*ST索菱','皇台酒业']
LAYER0_RULES = ['🚫 审计意见非标', '⚠️ 审计意见带强调事项段', '⚠️ 年报延期披露', '🚫 监管处罚/立案调查']
ALL_RULES = [r for r in sorted(SCORE_MAP.keys()) if r not in LAYER0_RULES]

results = []
for entry in data:
    d = extract_fields(entry)
    if len(d) < 2:
        continue
    alerts, details = compute_rules(d)
    # 非财务规则
    profile = fetch_profile(entry['symbol'])
    pledge = fetch_pledge(entry['symbol'])
    holder = fetch_holder(entry['symbol'])
    extra_alerts, extra_details = compute_extra_rules({}, {}, profile, pledge, holder, {}, {})
    alerts.update(extra_alerts); details.update(extra_details)
    total, level, _ = compute_score(alerts)
    triggered = [(r, lv, details.get(r,'')) for r, lv in alerts.items() if lv in [1,2] and r not in LAYER0_RULES]
    combo_strs = []
    for name, cond, bonus in COMBO_RULES:
        if cond(alerts):
            combo_strs.append(f"{name}+{bonus}")
    results.append({
        'name': entry['name'], 'symbol': entry['symbol'],
        'total': total, 'level': level, 'triggered': triggered,
        'combo': combo_strs, 'n_alerts': len(triggered),
        'years': sorted(d.keys(), key=int),
        'is_bomb': entry['name'] in BOMB_NAMES
    })

now = datetime.now().strftime('%Y-%m-%d %H:%M')
bomb_covered = sum(1 for r in results if r['is_bomb'] and r['total'] >= 18)
health_false_high = sum(1 for r in results if not r['is_bomb'] and r['total'] >= 18)
bomb_missed = sum(1 for r in results if r['is_bomb'] and r['total'] < 8)

rule_stats = {}
for r in results:
    for rid, lv, _ in r['triggered']:
        rule_stats.setdefault(rid, {'bomb':0,'healthy':0,'total':0})
        rule_stats[rid]['total'] += 1
        if r['is_bomb']:
            rule_stats[rid]['bomb'] += 1
        else:
            rule_stats[rid]['healthy'] += 1

lines = [
    f"# 财报排雷 v3.1 全量回测报告\n",
    f"> **生成时间**：{now}",
    f"> **测试范围**：10家公司 × 5年（2021~2025）",
    f"> **规则总数**：{len(ALL_RULES)}条（不含Layer 0）",
    f"> **版本**：v3.1\n",
    "---\n",
    "## 一、总体结果\n",
    "| 指标 | 结果 |",
    "|:---|:---:|",
    f"| 暴雷公司识别为高风险(≥18) | **{bomb_covered}/5** {'✅' if bomb_covered==5 else '❌'} |",
    f"| 健康公司误判为高风险(≥18) | **{health_false_high}/5** {'✅' if health_false_high==0 else '❌'} |",
    f"| 暴雷公司漏报(<8分) | **{bomb_missed}/5** {'✅' if bomb_missed==0 else '⚠️'} |",
    "",
    "### 暴雷公司详情",
    "",
    "| 公司 | 总分 | 等级 | 告警数 | 组合加分 |",
    "|:---|:---:|:---|:---:|:---|",
]

for r in results:
    if r['is_bomb']:
        cs = ", ".join(r['combo']) if r['combo'] else "—"
        lines.append(f"| {r['name']} | **{r['total']}** | {r['level']} | {r['n_alerts']} | {cs} |")

lines += [
    "",
    "### 健康公司详情",
    "",
    "| 公司 | 总分 | 等级 | 告警数 |",
    "|:---|:---:|:---|:---:|",
]

for r in results:
    if not r['is_bomb']:
        lines.append(f"| {r['name']} | **{r['total']}** | {r['level']} | {r['n_alerts']} |")

lines += ["", "---", "", "## 二、各公司告警明细", ""]

for r in results:
    lines.append(f"### {r['name']} ({r['symbol']}) — 总分 {r['total']} {r['level']}")
    lines.append("")
    if r['triggered']:
        lines.append("| 级别 | 规则名称 | 得分 | 详情 |")
        lines.append("|:---:|:---|:---:|:---|")
        for rid, lv, det in r['triggered']:
            w, f = SCORE_MAP.get(rid, (1,2))
            pts = f if lv == 2 else w
            lvl = "FAIL" if lv == 2 else "WARN"
            lines.append(f"| {lvl} | {rid} | +{pts} | {det[:70]} |")
    else:
        lines.append("✅ 无告警")
    lines.append("")

lines += ["---", "", "## 三、规则触发统计", ""]
lines.append("| 规则名称 | 标记 | WARN/FAIL | 暴雷触发 | 健康触发 | 合计 |")
lines.append("|:---|:---:|:---:|:---:|:---:|:---:|")

sorted_rules = sorted(rule_stats.items(), key=lambda x: x[1]['total'], reverse=True)
for rid, stats in sorted_rules:
    w, f = SCORE_MAP.get(rid, (1,2))
    if stats['healthy'] == 0 and stats['bomb'] > 0:
        sym = "✅"
    elif stats['healthy'] > 0:
        sym = "⚠️"
    else:
        sym = "⚪"
    lines.append(f"| {rid} | {sym} | {w}/{f} | {stats['bomb']} | {stats['healthy']} | {stats['total']} |")

untouched = [r for r in ALL_RULES if r not in rule_stats]
if untouched:
    lines.append("")
    lines.append(f"**未触发规则（{len(untouched)}条）**：")
    lines.append("")
    for r in untouched:
        w, f = SCORE_MAP.get(r, (1,2))
        lines.append(f"- {r}  (W={w}, F={f})")
    lines.append("")

lines += ["", "---", "", "## 四、数据完整性", ""]
lines.append("| 公司 | 数据年份 | 数据完整性 |")
lines.append("|:---|:---|:---:|")
for r in results:
    yrs = ", ".join(str(y) for y in r['years'])
    comp = "✅ 5年完整" if len(r['years']) == 5 else f"⚠️ {len(r['years'])}年"
    lines.append(f"| {r['name']} | {yrs} | {comp} |")

lines.append("")
lines.append("---")
lines.append(f"*报告由 analyze.py v3.1 生成 | 规则数：{len(ALL_RULES)}条 | 覆盖：10家公司 × 5年*")

with open(OUTPUT, 'w', encoding='utf-8') as f:
    f.write("\n".join(lines))

print(f"✅ 报告已生成: {OUTPUT}")
print(f"   暴雷覆盖: {bomb_covered}/5, 健康误报高风险: {health_false_high}/5")
print(f"   未触发规则: {len(untouched)}条")
