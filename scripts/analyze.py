"""
A股财报排雷分析工具 v3.3
用法:
  python analyze.py 600519          # 按股票代码
  python analyze.py 贵州茅台         # 按公司名称
  
输出: 5年多期评分报告 + 风险等级 (含行业/质押/股东/关联交易等非财务信号)
"""
import sys, os, json, subprocess, tempfile, argparse, re
from datetime import datetime

# ============ 配置 ============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(BASE_DIR, 'tmp_cache')
NEODATA_SCRIPT = os.path.join(
    os.path.expanduser('~'),
    '.workbuddy/plugins/marketplaces/cb_teams_marketplace/plugins/finance-data/skills/neodata-financial-search/scripts/query.py'
)
os.makedirs(CACHE_DIR, exist_ok=True)

# ============ 工具函数 ============
def safe(v, default=0.0):
    import pandas as pd
    if pd.isna(v) or v is None: return default
    try: return float(v)
    except: return default

def yoy(val_curr, val_prev):
    if abs(val_prev) < 1e-6: return 0.0
    return (val_curr - val_prev) / abs(val_prev) * 100

# ============ 非财务数据获取（行业/股东/质押） ============
def fetch_profile(symbol):
    """获取行业分类"""
    import akshare as ak
    code = symbol.replace('SH','').replace('SZ','')
    try:
        df = ak.stock_profile_cninfo(symbol=code)
        if not df.empty:
            return {'industry': str(df.iloc[0].get('所属行业',''))}
    except: pass
    return {}

def fetch_pledge(symbol):
    """获取股权质押数据"""
    import akshare as ak
    code = symbol.replace('SH','').replace('SZ','')
    try:
        df = ak.stock_gpzy_individual_pledge_ratio_detail_em(symbol=code)
        if not df.empty:
            active = df[df['状态'] == '未解押']
            total_pledge_pct = active['占总股本比例'].sum() if not active.empty else 0
            # 大股东最高的质押比例
            top_pledge = active.groupby('股东名称')['占所持股份比例'].max() if not active.empty else {}
            max_holder_pct = top_pledge.max() if not top_pledge.empty else 0
            return {
                'pledge_total_pct': float(total_pledge_pct),
                'pledge_max_holder_pct': float(max_holder_pct),
                'pledge_active_count': len(active)
            }
    except: pass
    return {}

def fetch_holder(symbol):
    """获取主要股东数据"""
    import akshare as ak
    code = symbol.replace('SH','').replace('SZ','')
    try:
        df = ak.stock_main_stock_holder(stock=code)
        if not df.empty:
            top1 = float(df.iloc[0].get('持股比例',0) or 0) if len(df) > 0 else 0
            top5 = sum(float(df.iloc[i].get('持股比例',0) or 0) for i in range(min(5, len(df))))
            total_holders = float(df.iloc[0].get('股东总数',0)) if '股东总数' in df.columns else 0
            return {
                'holder_top1_pct': top1,
                'holder_top5_pct': top5,
                'holder_total': total_holders,
                'holder_count': len(df)
            }
    except: pass
    return {}

def fetch_dsh_trade(symbol):
    """查询董监高最近交易记录（减持监控）"""
    import akshare as ak
    code = symbol.replace('SH','').replace('SZ','')
    try:
        holder_df = ak.stock_main_stock_holder(stock=code)
        if not holder_df.empty:
            top_name = str(holder_df.iloc[0].get('股东名称',''))
            if top_name:
                df = ak.stock_hold_management_person_em(symbol=code, name=top_name)
                if not df.empty:
                    sell = df[df['变动股数'] < 0]
                    return {
                        'top_holder_name': top_name,
                        'recent_sell_count': len(sell.head(10)),
                        'total_sell_shares': int(abs(sell['变动股数'].sum())) if not sell.empty else 0,
                        'has_sell': len(sell) > 0
                    }
    except: pass
    return {}

def fetch_pdf_analysis(symbol, years_dir='PDF年报'):
    """尝试从PDF提取年报数据"""
    import os
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), years_dir)
    if not os.path.exists(base):
        base = os.path.join(os.getcwd(), years_dir)
    if not os.path.exists(base):
        return {}
    code = symbol.replace('SH','').replace('SZ','')
    for f in os.listdir(base):
        if code in f and f.endswith('.pdf'):
            from analyze_pdf import analyze_pdf
            return analyze_pdf(os.path.join(base, f))
    return {}

def fetch_industry_benchmark(symbol):
    """获取申万行业基准（PE/PB/股息率）"""
    import akshare as ak
    code = symbol.replace('SH','').replace('SZ','')
    try:
        profile = fetch_profile(symbol)
        ind_name = profile.get('industry', '')
        if not ind_name: return {}
        sw = ak.sw_index_first_info()
        industry_row = sw[sw['行业名称'].str.contains(ind_name[:2], na=False)]
        if not industry_row.empty:
            row = industry_row.iloc[0]
            return {
                'sw_industry': str(row.get('行业名称', '')),
                'industry_pe': float(row.get('静态市盈率', 0) or 0),
                'industry_pb': float(row.get('市净率', 0) or 0),
                'industry_dividend': float(row.get('静态股息率', 0) or 0),
                'industry_count': int(row.get('成份个数', 0) or 0),
            }
    except: pass
    return {}

# ============ Step 1: 解析输入 ============
def resolve_stock(input_str):
    """解析股票代码或名称，返回 (name, symbol)"""
    input_str = str(input_str).strip()
    
    # 尝试直接作为代码（纯数字或带后缀）
    code = input_str.replace('.SH','').replace('.SZ','').replace('SH','').replace('SZ','').strip()
    if code.isdigit():
        if input_str.startswith('6') or input_str.upper().startswith('SH'):
            return None, f"SH{code}"
        else:
            return None, f"SZ{code}"
    
    # 名称 → 代码 (用AkShare查)
    import akshare as ak
    try:
        df = ak.stock_info_a_code_name()
        match = df[df['name'].str.contains(input_str, na=False)]
        if not match.empty:
            row = match.iloc[0]
            return row['name'], str(row['code']).strip()
    except:
        pass
    
    return None, None

# ============ Step 2: 获取数据 ============
def fetch_data(name, symbol):
    """获取5年三表数据，返回缓存entry"""
    import akshare as ak
    
    print(f"获取 {symbol} 数据...", flush=True)
    
    try:
        bs = ak.stock_balance_sheet_by_report_em(symbol=symbol)
        pl = ak.stock_profit_sheet_by_report_em(symbol=symbol)
        cf = ak.stock_cash_flow_sheet_by_report_em(symbol=symbol)
    except Exception as e:
        print(f"❌ 数据获取失败: {e}")
        return None
    
    # 取公司名称
    if not name:
        try: name = bs.iloc[0]['SECURITY_NAME_ABBR']
        except: name = symbol
    
    skip_keys = {'REPORT_DATE','SECUCODE','SECURITY_CODE','SECURITY_NAME_ABBR','ORG_CODE','ORG_TYPE',
                 'REPORT_TYPE','REPORT_DATE_NAME','SECURITY_TYPE_CODE','NOTICE_DATE','UPDATE_DATE','CURRENCY',
                 'OPINION_TYPE','OSOPINION_TYPE'}
    
    result = {'name': str(name), 'symbol': symbol, 'is_bomb': False, 'years': {}}
    
    for y in range(2021, 2026):
        ds = f"{y}-12-31 00:00:00"
        bsr = bs[bs['REPORT_DATE'] == ds]
        plr = pl[pl['REPORT_DATE'] == ds]
        cfr = cf[cf['REPORT_DATE'] == ds]
        if bsr.empty or plr.empty or cfr.empty:
            continue
        
        def to_dict(row, skip):
            return {k: (safe(v) if k not in skip else str(v)) for k, v in row.to_dict().items()}
        
        result['years'][str(y)] = {
            'bs': to_dict(bsr.iloc[0], skip_keys),
            'pl': to_dict(plr.iloc[0], skip_keys),
            'cf': to_dict(cfr.iloc[0], skip_keys),
        }
    
    if not result['years']:
        print("❌ 无年度数据")
        return None
    
    print(f"  {len(result['years'])}年: {', '.join(sorted(result['years'].keys()))}", flush=True)
    return result

# ============ Step 3: Layer 0 监管处罚 ============
def check_regulatory(symbol):
    """通过NeoData查询监管处罚"""
    ts_code = symbol.replace('SH', '.SH').replace('SZ', '.SZ')
    
    if not os.path.exists(NEODATA_SCRIPT):
        print("  ⚠️ NeoData脚本不存在，跳过监管查询")
        return False
    
    try:
        result = subprocess.run(
            [sys.executable, NEODATA_SCRIPT, '--query', f'{ts_code} 风险事件 立案调查 处罚', '--data-type', 'api'],
            capture_output=True, text=True, timeout=30,
            cwd=os.path.dirname(NEODATA_SCRIPT)
        )
        if not result.stdout.strip():
            return False, None
        data = json.loads(result.stdout)
        
        # 检查是否有"风险与监管事件提醒"类型
        api_recalls = data.get('data', {}).get('apiData', {}).get('apiRecall', [])
        for item in api_recalls:
            if item.get('type') == '风险与监管事件提醒':
                content = item.get('content', '')
                if '立案' in content or '处罚' in content or '风险' in content:
                    return True, content[:200]
        return False, None
    except Exception as e:
        print(f"  ⚠️ 监管查询异常: {e}")
        return False, None

# ============ Step 4: 提取字段 ============
def extract_fields(entry):
    years = sorted(entry['years'].keys(), key=int)
    data = {}
    for y in years:
        bs = entry['years'][y]['bs']; pl = entry['years'][y]['pl']; cf = entry['years'][y]['cf']
        f = {}
        for k in ['TOTAL_ASSETS','MONETARYFUNDS','SHORT_LOAN','LONG_LOAN','BOND_PAYABLE',
                  'NONCURRENT_LIAB_1YEAR','ACCOUNTS_RECE','INVENTORY','CIP','GOODWILL',
                  'FIXED_ASSET','TOTAL_EQUITY','TOTAL_LIABILITIES','STAFF_SALARY_PAYABLE',
                  'ACCOUNTS_PAYABLE','TOTAL_OTHER_RECE','OTHER_RECE','NOTE_RECE','PREPAYMENT',
                  'INTANGIBLE_ASSET']:
            f[k] = safe(bs.get(k, 0))
        if f['TOTAL_OTHER_RECE'] > 0: f['OTHER_RECE'] = f['TOTAL_OTHER_RECE']
        for k in ['OPERATE_INCOME','NETPROFIT','ASSET_IMPAIRMENT_LOSS','CREDIT_IMPAIRMENT_LOSS',
                  'SALE_EXPENSE','MANAGE_EXPENSE','RESEARCH_EXPENSE','FINANCE_EXPENSE',
                  'FE_INTEREST_EXPENSE','NONBUSINESS_INCOME','NONBUSINESS_EXPENSE',
                  'OPERATE_PROFIT','PARENT_NETPROFIT','DEDUCT_PARENT_NETPROFIT',
                  'OPERATE_COST','INVEST_INCOME','OTHER_INCOME','OTHER_COMPRE_INCOME']:
            f[k] = safe(pl.get(k, 0))
        for k in ['NETCASH_OPERATE','SALES_SERVICES','NETCASH_INVEST','NETCASH_FINANCE',
                  'CONSTRUCT_LONG_ASSET']:
            f[k] = safe(cf.get(k, 0))
        data[int(y)] = f
    return {k: data[k] for k in sorted(data.keys())}

# ============ Step 5: 规则计算(同v3) ============
def compute_rules(d):
    years = sorted(d.keys())
    if len(years) < 2: return {}, {}
    last = d[years[-1]]; prev = d[years[-2]] if len(years) >= 2 else {}
    alerts = {}; details = {}
    def set_alert(rid, level, msg):
        alerts[rid] = level; details[rid] = msg
    
    ta = last.get('TOTAL_ASSETS', 1) or 1
    st_debt = last['SHORT_LOAN'] + last['NONCURRENT_LIAB_1YEAR']
    ib_debt = st_debt + last['LONG_LOAN'] + last['BOND_PAYABLE']
    liab_r = last['TOTAL_LIABILITIES'] / ta
    cash_r = last['MONETARYFUNDS'] / ta
    debt_r = ib_debt / ta
    mf_cvr = last['MONETARYFUNDS'] / st_debt if st_debt > 0 else 999
    orr = last['OTHER_RECE'] / ta
    cip_r = last['CIP'] / ta
    gw_r = last['GOODWILL'] / max(last['TOTAL_EQUITY'], 1)
    pq = last['NETCASH_OPERATE'] / last['NETPROFIT'] if last['NETPROFIT'] > 0 else 0
    sr = last['SALES_SERVICES'] / (last['OPERATE_INCOME'] * 1.13) if last['OPERATE_INCOME'] > 0 else 0
    imp_t = last['ASSET_IMPAIRMENT_LOSS'] + last['CREDIT_IMPAIRMENT_LOSS']
    imp_fa = imp_t / max(last['FIXED_ASSET'], 1)
    imp_rev = imp_t / max(last['OPERATE_INCOME'], 1)
    exp_r = (last['SALE_EXPENSE'] + last['MANAGE_EXPENSE'] + last['RESEARCH_EXPENSE']) / max(last['OPERATE_INCOME'], 1)
    non_op = (last['NONBUSINESS_INCOME'] - last['NONBUSINESS_EXPENSE']) / max(abs(last['NETPROFIT']), 1)
    dd_r = last['DEDUCT_PARENT_NETPROFIT'] / max(last['PARENT_NETPROFIT'], 1)
    ier = last['FE_INTEREST_EXPENSE'] / max(ib_debt, 1)
    ior = abs(last['NETCASH_INVEST']) / ta
    rec_r = last['ACCOUNTS_RECE'] / ta
    
    nco_neg_count = sum(1 for y in years if d[y]['NETCASH_OPERATE'] < 0)
    
    def get_yoy(field):
        if len(years) < 2: return 0.0
        return yoy(last[field], prev[field])
    rev_yoy = get_yoy('OPERATE_INCOME')
    np_yoy = get_yoy('NETPROFIT')
    parent_np_yoy = get_yoy('PARENT_NETPROFIT')
    mgmt_yoy = get_yoy('MANAGE_EXPENSE')
    op_yoy = get_yoy('OPERATE_PROFIT')
    nco_yoy = get_yoy('NETCASH_OPERATE')
    rece = last['ACCOUNTS_RECE']
    rece_yoy = yoy(rece, prev.get('ACCOUNTS_RECE', 0))
    ta_prev = prev.get('TOTAL_ASSETS', 1) or 1
    ta_yoy = yoy(ta, ta_prev)
    
    # R-A01
    r01 = 1 if (cash_r > 0.2 and debt_r > 0.3) else 0
    if r01 == 1 and len(years) >= 2:
        pc = prev['MONETARYFUNDS']/max(prev['TOTAL_ASSETS'],1); pd_ = (prev['SHORT_LOAN']+prev['LONG_LOAN']+prev['BOND_PAYABLE']+prev['NONCURRENT_LIAB_1YEAR'])/max(prev['TOTAL_ASSETS'],1)
        if pc > 0.2 and pd_ > 0.3: r01 = 2
    set_alert('R-A01.存贷双高', r01, f"货币占比={cash_r:.1%}, 有息负债={debt_r:.1%}")
    
    # R-A03
    r03 = 1 if (st_debt > 0 and last['MONETARYFUNDS'] < st_debt * 0.8) else 0
    if len(years) >= 2 and r03 == 1:
        ps = prev['SHORT_LOAN']+prev['NONCURRENT_LIAB_1YEAR']
        if ps > 0 and prev['MONETARYFUNDS'] < ps * 0.8: r03 = 2
    set_alert('R-A03.货币资金不足偿债', r03, f"覆盖={mf_cvr:.2f}x")
    
    # R-A04
    r04 = 1 if orr > 0.05 else (2 if orr > 0.10 else 0)
    set_alert('R-A04.其他应收款过高', r04, f"占比={orr:.4%}")
    
    # R-A05
    cc = (last['NETCASH_OPERATE'] > 0 and last['CIP'] > 0 and last['NETCASH_OPERATE']/last['CIP'] > 1)
    r05 = 0 if cip_r <= 0.1 else (0 if cc else (1 if cip_r <= 0.2 else 2))
    set_alert('R-A05.在建工程过高', r05, f"占比={cip_r:.4%}")
    
    # R-A06
    r06 = 1 if gw_r > 0.3 else (2 if gw_r > 0.5 else 0)
    set_alert('R-A06.商誉过高', r06, f"商誉/净资产={gw_r:.4%}")
    
    # R-A07
    if last['NETPROFIT'] > 0: r07 = 1 if pq < 0.5 else 0
    else: r07 = 1 if last['NETCASH_OPERATE'] < 0 else 0
    if r07 == 1 and len(years) >= 2:
        pp = prev['NETCASH_OPERATE']/prev['NETPROFIT'] if prev['NETPROFIT'] > 0 else 0
        pt = (prev['NETPROFIT'] > 0 and pp < 0.5) or (prev['NETPROFIT'] <= 0 and prev['NETCASH_OPERATE'] < 0)
        if pt: r07 = 2
    set_alert('R-A07.经营现金流不足', r07, f"经现/净利={pq:.2f}x")
    
    # R-A08
    r08 = 1 if sr < 0.85 else (2 if sr < 0.70 else 0)
    set_alert('R-A08.销售回款率不足', r08, f"回款率={sr:.4%}")
    
    # R-A14
    r14 = 1 if imp_fa > 0.05 else (2 if imp_fa > 0.10 else 0)
    set_alert('R-A14.大额资产减值', r14, f"减值/固定资产={imp_fa:.4%}")
    
    # R-REV1 营收大幅下滑 [A级: W=5/F=8]
    # 营收YOY大幅下滑 — 营收是公司的命脉，下降超20%需高度警惕
    # 例外：周期性行业下行周期，需结合行业判断
    r_rev1 = 0
    if rev_yoy < -20.0:
        r_rev1 = 1 if rev_yoy >= -40.0 else 2
    set_alert('R-REV1.营收大幅下滑', r_rev1, f"营收YOY={rev_yoy:.1f}%")
    
    # R-NP1 归母净利大幅下滑 [A级: W=6/F=10]
    # 归母净利润YOY大幅下滑 — 盈利是公司的根本
    # 使用PARENT_NETPROFIT(归母净利润)而非NETPROFIT(含少数股东)
    r_np1 = 0
    if parent_np_yoy < -30.0:
        r_np1 = 1 if parent_np_yoy >= -50.0 else 2
    set_alert('R-NP1.归母净利大幅下滑', r_np1, f"归母净利YOY={parent_np_yoy:.1f}%")
    
    # R-P02
    rp02 = 1 if exp_r > 0.80 else 0
    set_alert('R-P02.费用率过高', rp02, f"三费/营收={exp_r:.4%}")
    
    # R-P03
    mgmt_r = last['MANAGE_EXPENSE'] / max(last['OPERATE_INCOME'], 1)
    rp03 = 1 if (mgmt_r > 0.05 and mgmt_yoy > 0 and rev_yoy > 0 and mgmt_yoy > rev_yoy * 1.2 and mgmt_yoy > 10.0) else 0
    set_alert('R-P03.费用增幅异常', rp03, f"管理YOY={mgmt_yoy:.1f}%")
    
    # R-P04
    rp04 = 1 if imp_rev > 0.045 else (2 if imp_rev > 0.08 else 0)
    set_alert('R-P04.减值损失过大', rp04, f"减值/营收={imp_rev:.4%}")
    
    # R-P05
    rp05 = 1 if abs(non_op) > 0.25 else (2 if abs(non_op) > 0.50 else 0)
    set_alert('R-P05.营业外收支占比过高', rp05, f"|营业外|/|净利润|={abs(non_op):.4%}")
    
    # R-P07
    rp07 = 1 if (op_yoy < 0 and abs(op_yoy) > 10.0) else 0
    if rp07 == 1 and len(years) >= 3:
        op2 = d[years[-3]]['OPERATE_PROFIT']
        if yoy(prev['OPERATE_PROFIT'], op2) < -10: rp07 = 2
    set_alert('R-P07.营业利润率下降', rp07, f"营业利润YOY={op_yoy:.1f}%")
    
    # R-P08
    rp08 = 1 if (last['PARENT_NETPROFIT'] > 0 and dd_r < 0.50) else 0
    set_alert('R-P08.扣非占比过低', rp08, f"扣非/归母={dd_r:.4%}")
    
    # R-CF1
    op_pos = last['NETCASH_OPERATE'] > 0; iv_pos = last['NETCASH_INVEST'] > 0; fn_pos = last['NETCASH_FINANCE'] > 0
    k = (1 if op_pos else 0, 1 if iv_pos else 0, 1 if fn_pos else 0)
    pats = {(0,0,0):"造假型",(0,0,1):"挣扎型",(0,1,0):"异常型",(0,1,1):"赌徒型",
            (1,0,0):"奶牛型",(1,0,1):"成长型",(1,1,0):"保守型",(1,1,1):"妖精型"}
    is_hg = (ta_yoy > 30.0 and last['NETCASH_OPERATE'] > 0)
    if is_hg and k == (1,1,1): rcf1 = 0
    else: rcf1 = 2 if k in [(0,0,0),(0,1,1),(1,1,1)] else (1 if k in [(0,0,1),(0,1,0)] else 0)
    set_alert('R-CF1.现金流肖像异常', rcf1, f"经{'+'if op_pos else'-'}投{'+'if iv_pos else'-'}筹{'+'if fn_pos else'-'}→{pats.get(k,'?')}")
    
    # R-CF2
    rcf2 = 2 if nco_neg_count >= 3 else (1 if nco_neg_count >= 1 else 0)
    set_alert('R-CF2.经营现金流为负', rcf2, f"5年{nco_neg_count}年为负")
    
    # R-CF3
    rcf3 = 1 if (ib_debt / ta > 0.01 and ier > 0.06) else (2 if (ib_debt / ta > 0.01 and ier > 0.10) else 0)
    set_alert('R-CF3.利息支出过高', rcf3, f"利息/有息负债={ier:.4%}")
    
    # R-CF4
    rcf4 = 1 if (last['NETCASH_OPERATE'] > 0 and last['NETCASH_INVEST'] < 0 and ior > 0.2) else 0
    set_alert('R-CF4.投资经营现金流背离', rcf4, f"投资流出/总资产={ior:.4%}")
    
    # R-CA2
    rca2 = 1 if liab_r > 0.70 else (2 if liab_r > 0.90 else 0)
    set_alert('R-CA2.资产负债率过高', rca2, f"负债率={liab_r:.4%}")
    
    # ============ B类规则 (9条新增) ============
    
    # R-P01 毛利率过低 [C级: W=2/F=3]
    gm = (last['OPERATE_INCOME'] - last['OPERATE_COST']) / max(last['OPERATE_INCOME'], 1)
    rp01 = 1 if gm < 0.20 else (2 if gm < 0.10 else 0)
    set_alert('R-P01.毛利率过低', rp01, f"毛利率={gm:.4%}")
    
    # R-P09 研发费用过高 [C级: W=1/F=2]
    rd_r = last['RESEARCH_EXPENSE'] / max(last['OPERATE_INCOME'], 1)
    rp09 = 1 if rd_r > 0.15 else (2 if rd_r > 0.30 else 0)
    set_alert('R-P09.研发费用过高', rp09, f"研发/营收={rd_r:.4%}")
    
    # R-P10 投资收益占比过高 [B级: W=3/F=5]
    inv_ratio = last['INVEST_INCOME'] / max(abs(last['OPERATE_PROFIT']), 1)
    rp10 = 1 if inv_ratio > 0.50 else (2 if inv_ratio > 0.80 else 0)
    set_alert('R-P10.投资收益占比过高', rp10, f"投资收益/营业利润={inv_ratio:.4%}")
    
    # R-B03 存货增速>营收增速×1.5 [C级: W=2/F=3]
    inv_yoy = yoy(last['INVENTORY'], prev.get('INVENTORY', 0))
    rb03 = 1 if (inv_yoy > 0 and rev_yoy > 0 and inv_yoy > rev_yoy * 1.5 and inv_yoy > 10.0) else 0
    set_alert('R-B03.存货增速异常', rb03, f"存货YOY={inv_yoy:.1f}%, 营收YOY={rev_yoy:.1f}%")
    
    # R-B05 预付款项/营收过高 [C级: W=2/F=3]
    prep_r = last['PREPAYMENT'] / max(last['OPERATE_INCOME'], 1)
    rb05 = 1 if prep_r > 0.10 else (2 if prep_r > 0.20 else 0)
    set_alert('R-B05.预付款项过高', rb05, f"预付款/营收={prep_r:.4%}")
    
    # R-B06 存货/营收比 [C级: W=1/F=2]
    ir_r = last['INVENTORY'] / max(last['OPERATE_INCOME'], 1)
    rb06 = 1 if ir_r > 0.50 else (2 if ir_r > 1.00 else 0)
    set_alert('R-B06.存货营收比过高', rb06, f"存货/营收={ir_r:.4%}")
    
    # R-B08 存货周转率异常 [B级: W=2/F=4]
    it = last['OPERATE_COST'] / max(last['INVENTORY'], 1)
    # 低于1x为慢，低于0.5x为很慢
    rb08 = 1 if it < 1.0 else (2 if it < 0.5 else 0)
    set_alert('R-B08.存货周转率过低', rb08, f"周转率={it:.2f}x")
    
    # R-B16 有息负债率持续攀升 [B级: W=3/F=5]
    ib_ratio = ib_debt / ta
    if len(years) >= 2:
        prev_ib = (prev['SHORT_LOAN']+prev['LONG_LOAN']+prev['BOND_PAYABLE']+prev['NONCURRENT_LIAB_1YEAR']) / max(prev['TOTAL_ASSETS'], 1)
        rb16 = 1 if (ib_ratio > prev_ib + 0.05) else 0
        if ib_ratio > 0.60: rb16 = 2
        if len(years) >= 3:
            y3 = d[years[-3]]
            ib3 = (y3['SHORT_LOAN']+y3['LONG_LOAN']+y3['BOND_PAYABLE']+y3['NONCURRENT_LIAB_1YEAR']) / max(y3['TOTAL_ASSETS'], 1)
            if ib_ratio > ib3 + 0.10: rb16 = 2
    else: rb16 = 0
    set_alert('R-B16.有息负债率攀升', rb16, f"有息负债率={ib_ratio:.4%}")
    
    # R-B01 应收增速>营收增速×2.0 [C级: W=2/F=3] (轻量版，R-X02用1.5x)
    rb01 = 1 if (rec_r > 0.05 and rece_yoy > 0 and rev_yoy > 0 and rece_yoy > rev_yoy * 2.0 and rece_yoy > 10.0) else 0
    set_alert('R-B01.应收增速异常', rb01, f"应收YOY={rece_yoy:.1f}%, 营收YOY={rev_yoy:.1f}%")
    
    # ============ Batch 1 新增规则 (6条) ============
    
    # R-B12 无形资产余额巨大 [B级: W=3/F=5]
    intan_r = last['INTANGIBLE_ASSET'] / ta
    if len(years) >= 2:
        prev_intan_r = prev['INTANGIBLE_ASSET'] / max(prev['TOTAL_ASSETS'], 1)
        rb12 = 1 if (intan_r > 0.15 and intan_r > prev_intan_r) else (2 if (intan_r > 0.30 and intan_r > prev_intan_r) else 0)
        if len(years) >= 3:
            y3_intan_r = d[years[-3]]['INTANGIBLE_ASSET'] / max(d[years[-3]]['TOTAL_ASSETS'], 1)
            if intan_r > y3_intan_r and intan_r > 0.15: rb12 = max(rb12, 1)
            if intan_r > 0.30 and intan_r > y3_intan_r: rb12 = 2
    else: rb12 = 0
    set_alert('R-B12.无形资产占比过高', rb12, f"无形资产/总资产={intan_r:.4%}")
    
    # R-B13 收购溢价极高 [B级: W=3/F=5] (商誉/净资产>50%)
    rb13 = 1 if gw_r > 0.50 else (2 if gw_r > 0.80 else 0)
    set_alert('R-B13.收购溢价极高', rb13, f"商誉/净资产={gw_r:.4%}")
    
    # R-B17 其他综合收益过大 [B级: W=2/F=4]
    oci = abs(last['OTHER_COMPRE_INCOME'])
    np_abs = abs(last['NETPROFIT'])
    oci_r = oci / np_abs if np_abs > 1e6 else 0
    rb17 = 1 if oci_r > 0.30 else (2 if oci_r > 0.60 else 0)
    set_alert('R-B17.其他综合收益过大', rb17, f"其他综合收益/净利润={oci_r:.4%}")
    
    # R-CA1 净资产收益率过低 [C级: W=1/F=2]
    roe = last['NETPROFIT'] / max(last['TOTAL_EQUITY'], 1)
    if len(years) >= 3:
        y2_roe = d[years[-2]]['NETPROFIT'] / max(d[years[-2]]['TOTAL_EQUITY'], 1)
        y3_roe = d[years[-3]]['NETPROFIT'] / max(d[years[-3]]['TOTAL_EQUITY'], 1)
        rca1 = 1 if (roe < 0.10 and y2_roe < 0.10) else (2 if (roe < 0.10 and y2_roe < 0.10 and y3_roe < 0.10) else 0)
    else: rca1 = 1 if roe < 0.10 else 0
    set_alert('R-CA1.净资产收益率过低', rca1, f"ROE={roe:.4%}")
    
    # R-C01 收入造假组合信号 [C级: W=2/F=3]
    # 触发条件: (B01应收异常) AND (B03存货增速异常 OR B06存货营收比过高)
    b01_trig = alerts.get('R-B01.应收增速异常', 0) in [1,2]
    b03_trig = alerts.get('R-B03.存货增速异常', 0) in [1,2]
    b06_trig = alerts.get('R-B06.存货营收比过高', 0) in [1,2]
    rc01 = 2 if (b01_trig and (b03_trig or b06_trig)) else (1 if (b01_trig or b03_trig or b06_trig) else 0)
    set_alert('R-C01.收入造假组合信号', rc01, f"应收异常={b01_trig}, 存货异常={b03_trig or b06_trig}")
    
    # R-C02 大股东占用资金组合信号 [C级: W=2/F=3]
    # 触发条件: (B05预付款过高) AND (A04其他应收款过高)
    b05_trig = alerts.get('R-B05.预付款项过高', 0) in [1,2]
    a04_trig = alerts.get('R-A04.其他应收款过高', 0) in [1,2]
    rc02 = 2 if (b05_trig and a04_trig) else (1 if (b05_trig or a04_trig) else 0)
    set_alert('R-C02.大股东占用资金', rc02, f"预付款异常={b05_trig}, 其他应收高={a04_trig}")
    
    # ============ Batch 2 新增规则 (3条) ============
    
    # R-X04 毛利率异常高 [B级: W=3/F=5]
    # 高毛利率本身不是问题, 但营收停滞+毛利率异常高 → 配合收入造假
    # 例外1：高速成长公司高毛利率合理（医药/软件）
    # 例外2：连续3年毛利>80%为行业特性（白酒），跳过
    r_x04 = 0
    if gm > 0.80:
        hg_rate = ta_yoy > 30.0 or rev_yoy > 20.0
        persistent_high_margin = False
        if len(years) >= 3:
            gm_all = [(d[y]['OPERATE_INCOME']-d[y]['OPERATE_COST'])/max(d[y]['OPERATE_INCOME'],1) for y in years[-3:]]
            persistent_high_margin = all(g > 0.80 for g in gm_all)
        if not hg_rate and not persistent_high_margin:
            r_x04 = 1 if rev_yoy < 5.0 else 0
            if gm > 0.90 and rev_yoy < 5.0: r_x04 = 2
    set_alert('R-X04.毛利率异常高', r_x04, f"毛利率={gm:.4%}, 营收YOY={rev_yoy:.1f}%")
    
    # R-B08D 存货周转天数持续上升 [B级: W=3/F=5]
    # 例外：高速成长公司存货自然扩张
    r_b08d = 0; days_chg = 0
    if len(years) >= 3:
        hg = ta_yoy > 20.0
        if not hg:
            it3 = [d[y]['OPERATE_COST']/max(d[y]['INVENTORY'],1) for y in years[-3:]]
            days3 = [365/max(v,0.01) for v in it3]
            days_up = days3[2] > days3[1] and days3[1] > days3[0]
            days_chg = (days3[2] - days3[0]) / max(days3[0], 1)
            r_b08d = 1 if (days_up and days_chg > 0.30) else (2 if (days_up and days_chg > 0.50) else 0)
    set_alert('R-B08D.存货周转天数上升', r_b08d, f"天数变化={days_chg:.1%}")
    
    # R-X05 现金流利润缺口扩大 [A级: W=4/F=7]
    # 净利润与经营现金流的差额逐年扩大 → 利润"含金量"越来越低
    # 例外：营收下降导致缺口扩大（经营问题，非造假信号）
    r_x05 = 0; gap_ratio = 0; gap_to_np = 0
    if len(years) >= 2 and last['NETPROFIT'] > 0 and prev['NETPROFIT'] > 0 and last['OPERATE_INCOME'] > prev['OPERATE_INCOME']:
        gap_curr = last['NETPROFIT'] - last['NETCASH_OPERATE']
        gap_prev = prev['NETPROFIT'] - prev['NETCASH_OPERATE']
        gap_ratio = gap_curr / max(gap_prev, 1e6) if abs(gap_prev) > 1e6 else 0
        gap_to_np = gap_curr / max(last['NETPROFIT'], 1e6)
        r_x05 = 1 if (gap_ratio > 1.5 and gap_to_np > 0.20) else (2 if (gap_ratio > 3.0 and gap_to_np > 0.30) else 0)
    set_alert('R-X05.现金流利润缺口扩大', r_x05, f"缺口扩大{gap_ratio:.1f}x, 占净利{gap_to_np:.1%}")
    
    # FR2
    if len(years) >= 3:
        gm_vals = [(d[y]['OPERATE_INCOME']-d[y]['OPERATE_COST'])/max(d[y]['OPERATE_INCOME'],1) for y in years[-3:]]
        it_vals = [d[y]['OPERATE_COST']/max(d[y]['INVENTORY'],1) for y in years[-3:]]
        hg = ta_yoy > 20.0
        if gm_vals[2] > gm_vals[1] > gm_vals[0] and it_vals[2] < it_vals[1] < it_vals[0] and not hg:
            rfr2 = 2
        elif abs(gm_vals[2]-gm_vals[0]) > 0.15 and not hg:
            rfr2 = 1
        else: rfr2 = 0
    else: rfr2 = 'N/A'
    set_alert('R-FR2.存货周转与毛利率背离', rfr2, "")
    
    # FR3
    if len(years) >= 3:
        pv = [d[y]['STAFF_SALARY_PAYABLE'] for y in years[-3:]]
        vol = (max(pv)-min(pv))/max((max(pv)+min(pv))/2,1) if max(pv)>0 else 0
        rfr3 = 2 if vol > 0.50 else (1 if vol > 0.30 else 0)
    else: rfr3 = 'N/A'
    set_alert('R-FR3.应付职工薪酬异常', rfr3, f"波动率={vol:.2%}" if len(years)>=3 else "")
    
    # R-X01
    rx01 = 1 if (np_yoy > 20.0 and nco_yoy < 0) else 0
    set_alert('R-X01.利润增长与现金流不同步', rx01, f"净利润YOY={np_yoy:.1f}%, 经现YOY={nco_yoy:.1f}%")
    
    # R-X02
    rx02 = 1 if (rec_r > 0.05 and rece_yoy > 0 and rev_yoy > 0 and rece_yoy > rev_yoy * 1.5 and rece_yoy > 10.0) else 0
    set_alert('R-X02.营收增长应收更快', rx02, f"应收YOY={rece_yoy:.1f}%, 营收YOY={rev_yoy:.1f}%")
    
    # R-X03
    rx03 = 1 if (np_yoy > 30.0 and ta_yoy > 20.0 and liab_r > 0.5 and pq < 1.0) else 0
    set_alert('R-X03.利润操纵资产端印证', rx03, f"净利润YOY={np_yoy:.1f}%, 经现/净利={pq:.2f}x")
    
    return alerts, details

# ============ 额外规则：行业/股东/质押/诉讼 ============
def compute_extra_rules(alerts, details, profile, pledge, holder, dsh_trade=None, pdf=None, bench=None):
    """添加非财务规则到alerts"""
    
    # R-IND1 高风险行业标记 [B级: W=2/F=0]
    high_risk = ['农林牧渔', '渔业']
    ind = profile.get('industry', '')
    if any(h in ind for h in high_risk):
        alerts['R-IND1.高风险行业'] = 1
        details['R-IND1.高风险行业'] = f"所属行业: {ind}"
    
    # R-PLG1 大股东质押比例过高 [B级: W=3/F=5]
    h_pct = pledge.get('pledge_max_holder_pct', 0)
    if h_pct > 0:
        r_plg1 = 2 if h_pct > 80 else (1 if h_pct > 50 else 0)
        alerts['R-PLG1.大股东质押过高'] = r_plg1
        details['R-PLG1.大股东质押过高'] = f"大股东质押占比={h_pct:.1f}%"
    
    # R-PLG2 总质押比例过高 [B级: W=2/F=4]
    t_pct = pledge.get('pledge_total_pct', 0)
    if t_pct > 0:
        r_plg2 = 2 if t_pct > 50 else (1 if t_pct > 30 else 0)
        alerts['R-PLG2.总质押比例过高'] = r_plg2
        details['R-PLG2.总质押比例过高'] = f"总质押占总股本={t_pct:.1f}%"
    
    # R-HLD1 一股独大 [C级: W=1/F=2]
    top1 = holder.get('holder_top1_pct', 0)
    if top1 > 0:
        r_hld1 = 2 if top1 > 70 else (1 if top1 > 50 else 0)
        alerts['R-HLD1.一股独大'] = r_hld1
        details['R-HLD1.一股独大'] = f"第一大股东持股={top1:.1f}%"
    
    # R-DSH1 董监高减持 [B级: W=3/F=0]
    if dsh_trade and dsh_trade.get('has_sell', False):
        alerts['R-DSH1.董监高减持'] = 1
        details['R-DSH1.董监高减持'] = f"{dsh_trade['top_holder_name']}近期减持{dsh_trade['total_sell_shares']}股"
    
    # PDF提取规则
    if pdf:
        subs = pdf.get('subsidiaries', {})
        if subs.get('has_overseas', False):
            alerts['R-SEG1.境外子公司'] = 1
            details['R-SEG1.境外子公司'] = f"含{subs.get('overseas_count',0)}家境外子公司"
        rpt = pdf.get('related_party', {})
        if rpt and not rpt.get('has_data', True):
            alerts['R-RPT1.关联交易未披露'] = 2
            details['R-RPT1.关联交易未披露'] = "关联购销表格为空，可能隐瞒关联交易"
        grt = pdf.get('guarantee', {})
        if grt.get('has_guarantee', False):
            alerts['R-GRT1.子公司大额担保'] = 1
            details['R-GRT1.子公司大额担保'] = f"对子公司担保{grt.get('hk_subsid_guarantee_wan',0):.0f}万元"
        # R-SEG2 境外收入占比 [C级: W=2/F=0]
        seg = pdf.get('segment', {})
        if seg.get('has_segment_data', False):
            alerts['R-SEG2.境外业务收入'] = 1
            details['R-SEG2.境外业务收入'] = f"境外收入{seg['overseas_revenue']/1e8:.2f}亿"
        # R-DSH2 董监高薪酬 [C级: W=1/F=2]
        dsh_pay = pdf.get('dsh_compensation', 0)
        if dsh_pay > 10000000:  # 超过1000万
            alerts['R-DSH2.董监高薪酬过高'] = 1
            details['R-DSH2.董监高薪酬过高'] = f"董监高薪酬{dsh_pay/1e4:.0f}万元"
        # R-CNT1 未执行合同 [B级: W=3/F=0]
        ct = pdf.get('contracts', {})
        if ct.get('count', 0) > 0:
            alerts['R-CNT1.重大未执行合同'] = 1
            details['R-CNT1.重大未执行合同'] = f"有{ct['count']}项未执行合同"
        # R-MDA1 MD&A风险提示过高 [C级: W=2/F=0]
        mda = pdf.get('mda_risk', {})
        if mda.get('rate_per_10k', 0) > 5.0:
            alerts['R-MDA1.风险提示过高'] = 1
            details['R-MDA1.风险提示过高'] = f"负面词频{mda['rate_per_10k']}/万字"
        # R-CMT1 承诺事项有风险 [C级: W=1/F=0]
        cmt = pdf.get('commitments', {})
        if cmt.get('has_litigation', False) or cmt.get('has_guarantee_commit', False):
            alerts['R-CMT1.承诺风险事项'] = 1
            details['R-CMT1.承诺风险事项'] = f"有诉讼/担保承诺事项"
    
    # 行业均值对标
    if bench and bench.get('industry_pe', 0) > 0:
        pass  # 预留：与行业PE/PB对比
    
    return alerts, details

# ============ 评分 ============
SCORE_MAP = {
    'R-A01.存贷双高':(6,10),'R-A03.货币资金不足偿债':(2,4),'R-A04.其他应收款过高':(4,7),
    'R-A05.在建工程过高':(3,5),'R-A06.商誉过高':(3,5),'R-A07.经营现金流不足':(4,7),
    'R-A08.销售回款率不足':(3,5),'R-A14.大额资产减值':(3,5),
    'R-P02.费用率过高':(2,4),'R-P03.费用增幅异常':(2,4),'R-P04.减值损失过大':(3,5),
    'R-P05.营业外收支占比过高':(3,5),'R-P07.营业利润率下降':(3,5),'R-P08.扣非占比过低':(1,2),
    'R-CF1.现金流肖像异常':(2,4),'R-CF2.经营现金流为负':(4,7),'R-CF3.利息支出过高':(3,5),
    'R-CF4.投资经营现金流背离':(3,5),'R-CA2.资产负债率过高':(1,2),
    'R-FR2.存货周转与毛利率背离':(4,7),'R-FR3.应付职工薪酬异常':(2,4),
    'R-X01.利润增长与现金流不同步':(4,8),'R-X02.营收增长应收更快':(4,7),'R-X03.利润操纵资产端印证':(4,8),
    # B类新增
    'R-P01.毛利率过低':(2,3),'R-P09.研发费用过高':(1,2),'R-P10.投资收益占比过高':(3,5),
    'R-B01.应收增速异常':(2,3),'R-B03.存货增速异常':(2,3),'R-B05.预付款项过高':(2,3),
    'R-B06.存货营收比过高':(1,2),'R-B08.存货周转率过低':(2,4),'R-B16.有息负债率攀升':(3,5),
    # Batch 1 新增
    'R-B12.无形资产占比过高':(3,5),'R-B13.收购溢价极高':(3,5),'R-B17.其他综合收益过大':(2,4),
    'R-CA1.净资产收益率过低':(1,2),'R-C01.收入造假组合信号':(2,3),'R-C02.大股东占用资金':(2,3),
    # Batch 2 新增
    'R-X04.毛利率异常高':(3,5),'R-B08D.存货周转天数上升':(3,5),'R-X05.现金流利润缺口扩大':(4,7),
    # Batch 5 营收/净利大幅下滑
    'R-REV1.营收大幅下滑':(5,8),'R-NP1.归母净利大幅下滑':(6,10),
    # 非财务信号 (行业/股东/质押)
    'R-IND1.高风险行业':(2,2),'R-PLG1.大股东质押过高':(3,5),'R-PLG2.总质押比例过高':(2,4),
    'R-HLD1.一股独大':(1,2),'R-DSH1.董监高减持':(3,3),'R-SEG1.境外子公司':(2,2),
    'R-RPT1.关联交易未披露':(3,5),'R-GRT1.子公司大额担保':(3,3),'R-SEG2.境外业务收入':(2,2),
    'R-DSH2.董监高薪酬过高':(1,2),'R-CNT1.重大未执行合同':(3,3),
    'R-MDA1.风险提示过高':(2,2),'R-CMT1.承诺风险事项':(1,1),
    # Layer 0
    '🚫 审计意见非标':(0,999),'⚠️ 审计意见带强调事项段':(4,7),'⚠️ 年报延期披露':(3,5),
    '🚫 监管处罚/立案调查':(0,999),
}

COMBO_RULES = [
    ("造假三角验证", lambda a: any(a.get(r,0) in [1,2] for r in ['R-A01.存贷双高','R-A04.其他应收款过高','R-A05.在建工程过高'])
     and any(a.get(r,0) in [1,2] for r in ['R-A07.经营现金流不足','R-CF2.经营现金流为负'])
     and any(a.get(r,0) in [1,2] for r in ['R-X01.利润增长与现金流不同步','R-X02.营收增长应收更快','R-X03.利润操纵资产端印证']), 6),
    ("现金危机", lambda a: sum(1 for r in ['R-A03.货币资金不足偿债','R-CF2.经营现金流为负','R-CF3.利息支出过高'] if a.get(r,0) in [1,2]) >= 2, 4),
    ("收入质量崩溃", lambda a: sum(1 for r in ['R-A08.销售回款率不足','R-X02.营收增长应收更快','R-A07.经营现金流不足'] if a.get(r,0) in [1,2]) >= 2, 5),
    ("资产疑云", lambda a: sum(1 for r in ['R-A04.其他应收款过高','R-A05.在建工程过高','R-A14.大额资产减值'] if a.get(r,0) in [1,2]) >= 2, 4),
]

RISK_LEVELS = [(0,8,"✅ 低风险","几乎无异常"),(8,18,"⚠️ 中风险","需关注异常项"),(18,30,"🔶 高风险","多项异常，建议避开"),(30,999,"🔴 极高风险","强烈暗示操纵，坚决排除")]

def compute_score(alerts):
    total = 0; excluded = False
    for rid, level in alerts.items():
        if isinstance(level, str) or level == 0: continue
        w, f = SCORE_MAP.get(rid, (1,2))
        if f >= 999: excluded = True; total += 999
        else: total += f if level == 2 else w
    combo_pts = 0; combo_detail = []
    for name, cond, bonus in COMBO_RULES:
        if cond(alerts): combo_pts += bonus; combo_detail.append(f"{name}+{bonus}")
    total += combo_pts
    if excluded: return total, "🚫 已排除", "审计意见非标或立案调查，直接排除"
    for lo, hi, name, desc in RISK_LEVELS:
        if lo <= total < hi: return total, name, desc
    return total, "未知", ""

# ============ 规则元数据 ============
RULE_META = {
    # === S级 ===
    'R-A01.存贷双高': {'level':'S','formula':'货币资金/总资产>20% AND 有息负债/总资产>30%','source':'资产负债表: MONETARYFUNDS, SHORT_LOAN, LONG_LOAN, BOND_PAYABLE','th_warn':'>20%+>30%','th_fail':'连续2年>20%+>30%','desc':'存贷双高=账面有大量现金却借大量有息负债，几乎无法合理解释','ref':'金融行业天然存贷双高，需跳过'},
    # === A级 资产负债表 ===
    'R-A04.其他应收款过高': {'level':'A','formula':'其他应收款/总资产','source':'资产负债表: OTHER_RECE, TOTAL_ASSETS','th_warn':'>5%','th_fail':'>10%','desc':'其他应收是"垃圾筐"，隐藏股东/关联方占款','ref':'正常公司通常<3%'},
    'R-A07.经营现金流不足': {'level':'A','formula':'经营现金流/净利润(经现/净利)','source':'利润表+现金流量表: NETPROFIT, NETCASH_OPERATE','th_warn':'经现/净利润<0.5','th_fail':'连续2年<0.5','desc':'利润"含金量"低，可能是应收挂账或虚构收入','ref':'茅台常年>1.0'},
    # === A级 跨表 ===
    'R-X01.利润增长与现金流不同步': {'level':'A','formula':'净利润YOY>20% 同时 经营现金流YOY<0','source':'利润表+现金流量表: NETPROFIT, NETCASH_OPERATE','th_warn':'净利YOY>20% 且 经现YOY<0','th_fail':'—','desc':'利润在增长但现金不跟进，收入含金量下降','ref':'高成长公司偶发，需结合毛利率趋势判断'},
    'R-X02.营收增长应收更快': {'level':'A','formula':'应收账款YOY/营收YOY > 1.5','source':'资产负债表+利润表: ACCOUNTS_RECE, OPERATE_INCOME','th_warn':'应收YOY/营收YOY>1.5','th_fail':'—','desc':'放宽赊销换收入，可能是虚构收入的信号','ref':'需应收>总资产5%才触发'},
    'R-X03.利润操纵资产端印证': {'level':'A','formula':'净利润YOY>30% AND 总资产YOY>20% AND 负债率>50% AND 经现/净利<1','source':'三表交叉验证','th_warn':'净利YOY>30%+资产YOY>20%+负债>50%+经现<净利','th_fail':'—','desc':'利润高增长的同时资产也在膨胀且现金流跟不上，利润操纵的高度信号','ref':'条件严格，极少触发'},
    # === A级 欺诈 ===
    'R-FR2.存货周转与毛利率背离': {'level':'A','formula':'毛利连续3年上升 同时 存货周转率连续3年下降','source':'利润表+资产负债表: GM_RATE, INVENTORY_TURNOVER','th_warn':'毛利↑+周转↓趋势','th_fail':'连续3年背离','desc':'存货周转越慢(产品积压)但毛利率越高(产品更赚钱)=数据矛盾','ref':'康美药业经典造假模式'},
    # === A级 业绩下滑 ===
    'R-REV1.营收大幅下滑': {'level':'A','formula':'营收YOY(同比)','source':'利润表: OPERATE_INCOME','th_warn':'营收YOY<-20%','th_fail':'营收YOY<-40%','desc':'营收同比大幅下滑，可能是产品竞争力下降或行业寒冬','ref':'周期性行业需结合判断'},
    'R-NP1.归母净利大幅下滑': {'level':'A','formula':'归母净利润YOY(同比)','source':'利润表: PARENT_NETPROFIT','th_warn':'归母净利YOY<-30%','th_fail':'归母净利YOY<-50%','desc':'归母净利润大幅下滑，盈利质量严重恶化','ref':'使用归母净利润(扣非前)判断'},
    # === B级 资产负债表 ===
    'R-A03.货币资金不足偿债': {'level':'B','formula':'货币资金/短期有息负债(覆盖倍数)','source':'资产负债表: MONETARYFUNDS, SHORT_LOAN, NONCURRENT_LIAB_1YEAR','th_warn':'覆盖<0.8x','th_fail':'连续2年<0.8x','desc':'现金连短期借款都还不上，可能是资金链紧张或货币资金受限','ref':'覆盖>1.5x为安全'},
    'R-A05.在建工程过高': {'level':'B','formula':'在建工程/总资产','source':'资产负债表: CIP, TOTAL_ASSETS','th_warn':'>10%且经现<在建','th_fail':'>20%且经现<在建','desc':'在建工程是造假资金出口，大股东占用资金的常见通道','ref':'需考虑基建行业特性'},
    'R-A06.商誉过高': {'level':'B','formula':'商誉/净资产','source':'资产负债表: GOODWILL, TOTAL_EQUITY','th_warn':'>30%','th_fail':'>50%','desc':'商誉过高意味着收购溢价极高，减值风险大','ref':'创业板公司商誉普遍偏高'},
    'R-A08.销售回款率不足': {'level':'B','formula':'销售商品收到现金/(营业收入×1.13)','source':'现金流量表+利润表: SALES_SERVICES, OPERATE_INCOME','th_warn':'<85%','th_fail':'<70%','desc':'回款率低说明收入没有对应现金流入，可能是赊销或虚构收入','ref':'正常公司>90%'},
    'R-A14.大额资产减值': {'level':'B','formula':'减值损失/固定资产','source':'利润表+资产负债表: ASSET_IMPAIRMENT_LOSS, CREDIT_IMPAIRMENT_LOSS, FIXED_ASSET','th_warn':'>5%','th_fail':'>10%','desc':'突然大额减值可能是在"洗大澡"，也可能是前期虚增资产的暴露','ref':'正常公司<3%'},
    # === B级 利润表 ===
    'R-P04.减值损失过大': {'level':'B','formula':'减值损失/营业收入','source':'利润表: ASSET_IMPAIRMENT_LOSS, CREDIT_IMPAIRMENT_LOSS, OPERATE_INCOME','th_warn':'>4.5%','th_fail':'>8%','desc':'减值占营收比重过大说明资产质量差或前期虚增','ref':'正常公司<3%'},
    'R-P05.营业外收支占比过高': {'level':'B','formula':'(营业外收入-营业外支出)/|净利润|','source':'利润表: NONBUSINESS_INCOME, NONBUSINESS_EXPENSE, NETPROFIT','th_warn':'>25%','th_fail':'>50%','desc':'利润靠非主营业务支撑，主业盈利能力差','ref':'正常公司<10%'},
    'R-P07.营业利润率下降': {'level':'B','formula':'营业利润YOY <-10%','source':'利润表: OPERATE_PROFIT','th_warn':'<-10%','th_fail':'连续2年<-10%','desc':'主营盈利能力持续下滑','ref':'周期性行业需结合行业周期判断'},
    # === B级 现金流量表 ===
    'R-CF1.现金流肖像异常': {'level':'B','formula':'经营/投资/筹资现金流方向组合','source':'现金流量表: NETCASH_OPERATE, NETCASH_INVEST, NETCASH_FINANCE','th_warn':'挣扎型/异常型','th_fail':'造假型/赌徒型/妖精型','desc':'正常的健康公司应该是正+负+负(奶牛型)或正+负+正(成长型)','ref':'高成长公司跳过妖精型(+++)'},
    'R-CF3.利息支出过高': {'level':'B','formula':'利息支出/有息负债','source':'利润表+资产负债表: FE_INTEREST_EXPENSE, SHORT_LOAN, LONG_LOAN, BOND_PAYABLE','th_warn':'>6%','th_fail':'>10%','desc':'融资成本过高意味着信用状况差','ref':'需有息负债>总资产1%才触发'},
    'R-CF4.投资经营现金流背离': {'level':'B','formula':'投资流出/总资产','source':'现金流量表+资产负债表: NETCASH_INVEST, TOTAL_ASSETS','th_warn':'>20%','th_fail':'—','desc':'经营现金流入但大量投资流出，正常扩张可接受','ref':'需经营CF>0才判断'},
    # === B级 综合分析 ===
    'R-CA2.资产负债率过高': {'level':'B','formula':'总负债/总资产','source':'资产负债表: TOTAL_LIABILITIES, TOTAL_ASSETS','th_warn':'>70%','th_fail':'>90%','desc':'负债率过高财务风险大','ref':'房地产行业天然高负债，需跳过'},
    # === B级 欺诈 ===
    'R-FR3.应付职工薪酬异常': {'level':'B','formula':'3年应付职工薪酬波动率>30%','source':'资产负债表: STAFF_SALARY_PAYABLE','th_warn':'>30%','th_fail':'>50%','desc':'薪酬波动异常可能意味着人工成本调节利润','ref':'需3年以上数据'},
    # === C级 ===
    'R-P02.费用率过高': {'level':'C','formula':'(销售+管理+研发+财务)/营收','source':'利润表: SALE_EXPENSE, MANAGE_EXPENSE, RESEARCH_EXPENSE, OPERATE_INCOME','th_warn':'>80%','th_fail':'—','desc':'费用吞噬大部分营收','ref':'轻资产公司通常费用率更高'},
    'R-P03.费用增幅异常': {'level':'C','formula':'管理费用YOY/营收YOY > 1.2','source':'利润表: MANAGE_EXPENSE, OPERATE_INCOME','th_warn':'管理YOY>营收YOY×1.2','th_fail':'—','desc':'管理费用增长远超营收，可能管理失控','ref':'需管理费>营收5%才判断'},
    'R-P08.扣非占比过低': {'level':'C','formula':'扣非净利润/归母净利润','source':'利润表: DEDUCT_PARENT_NETPROFIT, PARENT_NETPROFIT','th_warn':'<50%','th_fail':'—','desc':'利润靠非经常性损益支撑，主业盈利弱','ref':'亏损公司不判断'},
    # === Batch 1 新增 ===
    'R-B12.无形资产占比过高': {'level':'B','formula':'无形资产/总资产 连续2年增长','source':'资产负债表: INTANGIBLE_ASSET, TOTAL_ASSETS','th_warn':'>15%且增长','th_fail':'>30%且增长','desc':'无形资产水分大，可能是研发资本化过度积累','ref':'科技公司天然高无形资产'},
    'R-B13.收购溢价极高': {'level':'B','formula':'商誉/净资产','source':'资产负债表: GOODWILL, TOTAL_EQUITY','th_warn':'>50%','th_fail':'>80%','desc':'收购溢价极高=利益输送风险','ref':'基本同R-A06但阈值更高'},
    'R-B17.其他综合收益过大': {'level':'B','formula':'|其他综合收益|/净利润','source':'利润表: OTHER_COMPRE_INCOME, NETPROFIT','th_warn':'>30%','th_fail':'>60%','desc':'未实现损益占比过高，利润质量差','ref':'需净利润>0'},
    'R-CA1.净资产收益率过低': {'level':'C','formula':'净利润/净资产(ROE)','source':'利润表+资产负债表: NETPROFIT, TOTAL_EQUITY','th_warn':'ROE<10%连续2年','th_fail':'ROE<10%连续3年','desc':'ROE是巴菲特最看重指标，持续<10%说明资本回报差','ref':'白酒>20%, 制造10-15%'},
    # === Batch 2 新增 ===
    'R-X04.毛利率异常高': {'level':'B','formula':'毛利率>80%且营收增速<5%且非连续高毛利','source':'利润表: OPERATE_INCOME, OPERATE_COST','th_warn':'>80%+营收<5%','th_fail':'>90%+营收<5%','desc':'营收停滞但毛利率异常高可能是配合造假','ref':'白酒/医药天然高毛利。连续3年>80%跳过'},
    'R-B08D.存货周转天数上升': {'level':'B','formula':'周转天数连续2年上升且增幅>30%','source':'资产负债表+利润表: INVENTORY, OPERATE_COST','th_warn':'上升>30%','th_fail':'上升>50%','desc':'存货周转越来越慢，产品可能滞销或虚增存货','ref':'高速成长公司跳过'},
    'R-X05.现金流利润缺口扩大': {'level':'A','formula':'净利润-经营现金流的缺口同比扩大1.5倍','source':'利润表+现金流量表: NETPROFIT, NETCASH_OPERATE','th_warn':'缺口>1.5x且占净利>20%','th_fail':'缺口>3x且占净利>30%','desc':'利润"含金量"逐年下降，现金跟不上利润增长','ref':'需营收YOY>0才判断'},
    # === B类非财务 ===
    'R-PLG1.大股东质押过高': {'level':'B','formula':'大股东质押其所持股份比例','source':'AkShare: stock_gpzy_individual_pledge_ratio_detail_em','th_warn':'>50%','th_fail':'>80%','desc':'大股东高比例质押可能意味着资金链紧张或套现','ref':'须看未解押状态的质押'},
    'R-PLG2.总质押比例过高': {'level':'B','formula':'质押总股本占总股本比例','source':'AkShare: stock_gpzy_individual_pledge_ratio_detail_em','th_warn':'>30%','th_fail':'>50%','desc':'总股本大量被质押，崩盘风险大','ref':'银行/质押机构可辅助判断'},
    'R-HLD1.一股独大': {'level':'C','formula':'第一大股东持股比例','source':'AkShare: stock_main_stock_holder','th_warn':'>50%','th_fail':'>70%','desc':'一股独大=大股东控制权过高，中小股东权益保障差','ref':'国企普遍高持股，需区别对待'},
    'R-P01.毛利率过低': {'level':'C','formula':'毛利率 = (营收-成本)/营收','source':'利润表: OPERATE_INCOME, OPERATE_COST','th_warn':'<20%','th_fail':'<10%','desc':'毛利率过低说明产品竞争力弱','ref':'不同行业差异极大'},
    'R-P09.研发费用过高': {'level':'C','formula':'研发费用/营收','source':'利润表: RESEARCH_EXPENSE, OPERATE_INCOME','th_warn':'>15%','th_fail':'>30%','desc':'研发投入过高短期侵蚀利润','ref':'科技公司高研发费用正常'},
    'R-P10.投资收益占比过高': {'level':'B','formula':'投资收益/营业利润','source':'利润表: INVEST_INCOME, OPERATE_PROFIT','th_warn':'>50%','th_fail':'>80%','desc':'靠投资收益撑利润，主业不行','ref':'投资型公司例外'},
    'R-B01.应收增速异常': {'level':'C','formula':'应收YOY/营收YOY > 2','source':'资产负债表+利润表: ACCOUNTS_RECE, OPERATE_INCOME','th_warn':'>2x','th_fail':'—','desc':'应收账款增速远超营收增速=放宽赊销换收入','ref':'需应收>营收5%才触发'},
    'R-B03.存货增速异常': {'level':'C','formula':'存货YOY/营收YOY > 1.5','source':'资产负债表+利润表: INVENTORY, OPERATE_INCOME','th_warn':'>1.5x','th_fail':'—','desc':'存货增速远超营收增速=产品积压或虚增存货','ref':'高速成长公司偶发'},
    'R-B05.预付款项过高': {'level':'C','formula':'预付款/营收','source':'资产负债表+利润表: PREPAYMENT, OPERATE_INCOME','th_warn':'>10%','th_fail':'>20%','desc':'预付款过高可能是资金被大股东占用','ref':'大型设备定制行业例外'},
    'R-B06.存货营收比过高': {'level':'C','formula':'存货/营收','source':'资产负债表+利润表: INVENTORY, OPERATE_INCOME','th_warn':'>50%','th_fail':'>100%','desc':'存货占营收比重过高，周转慢','ref':'白酒(陈酿)天然高'},
    'R-B08.存货周转率过低': {'level':'B','formula':'营业成本/存货(周转率)','source':'资产负债表+利润表: OPERATE_COST, INVENTORY','th_warn':'<1x','th_fail':'<0.5x','desc':'周转率低=存货卖不动','ref':'白酒(陈酿)天然低，需跳过'},
    'R-B16.有息负债率攀升': {'level':'B','formula':'有息负债/总资产 年增>5pp','source':'资产负债表: SHORT_LOAN, LONG_LOAN, BOND_PAYABLE, NONCURRENT_LIAB_1YEAR, TOTAL_ASSETS','th_warn':'年增>5pp','th_fail':'>60%或年增>10pp','desc':'借钱越来越多，财务杠杆加大','ref':'需连续趋势判断'},
    'R-CF2.经营现金流为负': {'level':'A','formula':'经营现金流<0的年数','source':'现金流量表: NETCASH_OPERATE','th_warn':'1年为负','th_fail':'3年以上为负','desc':'经营本身不产生现金=在吃老本或造假','ref':'初创公司例外'},
    'R-C01.收入造假组合信号': {'level':'C','formula':'应收异常(B01) AND/OR 存货异常(B03/B06)','source':'组合判断','th_warn':'任一触发','th_fail':'应收+存货双触发','desc':'收入造假时，要么挂应收、要么虚增存货来消化利润','ref':'需B01/B03/B06配合判断'},
    'R-C02.大股东占用资金': {'level':'C','formula':'预付款高(B05) AND 其他应收高(A04)','source':'组合判断','th_warn':'任一触发','th_fail':'双触发','desc':'预付款+其他应收双高=大股东占用资金的强烈信号','ref':'需预付款+其他应收双高才FAIL'},
    # === PDF/非财务 ===
    'R-DSH1.董监高减持': {'level':'B','formula':'大股东近期有减持行为','source':'AkShare: stock_hold_management_person_em','th_warn':'有减持','th_fail':'—','desc':'大股东/董监高减持=对公司前景没信心','ref':'需查询最近交易记录'},
    'R-DSH2.董监高薪酬过高': {'level':'C','formula':'董监高薪酬总额>1000万','source':'PDF附注: 关键管理人员报酬','th_warn':'>1000万','th_fail':'—','desc':'董监高薪酬过高侵蚀股东利益','ref':'需PDF可提取时才有效'},
    'R-SEG1.境外子公司': {'level':'C','formula':'有境外子公司','source':'PDF附注: 子公司清单','th_warn':'有境外子公司','th_fail':'—','desc':'境外子公司=资金出境通道，不易审计','ref':'需PDF可提取时才有效'},
    'R-SEG2.境外业务收入': {'level':'C','formula':'境外收入>0','source':'PDF分部报告','th_warn':'有境外收入','th_fail':'—','desc':'境外收入占比过高=审计困难','ref':'需PDF可提取时才有效'},
    'R-RPT1.关联交易未披露': {'level':'B','formula':'关联购销表格为空','source':'PDF附注: 关联交易','th_warn':'—','th_fail':'表格为空但有子公司','desc':'有大量子公司但关联交易表为空=隐瞒关联交易','ref':'需PDF可提取时才有效'},
    'R-GRT1.子公司大额担保': {'level':'B','formula':'对子公司有担保','source':'PDF重要事项: 担保','th_warn':'有担保','th_fail':'—','desc':'对子公司提供大额担保=子公司可能独立融资困难或资金被占用','ref':'需PDF可提取时才有效'},
    'R-CNT1.重大未执行合同': {'level':'B','formula':'有标注"尚未执行"的合同','source':'PDF重要事项: 重大合同','th_warn':'有未执行合同','th_fail':'—','desc':'重大合同长期未执行=可能是虚假合同','ref':'需PDF可提取时才有效'},
    'R-MDA1.风险提示过高': {'level':'C','formula':'MD&A中负面关键词频次','source':'PDF: 经营讨论章节NLP分析','th_warn':'>5次/万字','th_fail':'—','desc':'管理层在年报中用了大量负面词汇=经营压力大','ref':'需PDF可提取时才有效'},
    'R-CMT1.承诺风险事项': {'level':'C','formula':'有诉讼/担保等承诺事项','source':'PDF附注: 承诺及或有事项','th_warn':'有风险事项','th_fail':'—','desc':'存在诉讼/担保等承诺事项需关注','ref':'需PDF可提取时才有效'},
    'R-IND1.高风险行业': {'level':'B','formula':'所属行业', 'source':'AkShare: stock_profile_cninfo','th_warn':'\'农林牧渔\'等行业','th_fail':'—','desc':'某些行业天然造假高发','ref':'农林牧渔、房地产等'},
}

def print_rule_summary(alerts, details, d, name, symbol, total, level, level_desc):
    """输出完整规则清单：含公式、数据源、计算结果、说明"""
    print(f"\n{'='*60}")
    print(f"  {name} ({symbol}) 排雷报告")
    print(f"  总分: {total} | {level} ({level_desc})")
    print(f"{'='*60}\n")
    
    # 按级别分组显示
    for lv_name in ['S','A','B','C']:
        lv_rules = {k:v for k,v in RULE_META.items() if v.get('level') == lv_name}
        # 也获取compute_rules里的触发层级
        triggered = {rid: lv for rid, lv in alerts.items() if lv in [1,2]}
        
        group_items = []
        for rid, meta in lv_rules.items():
            lv = alerts.get(rid, 0)
            det = details.get(rid, '')
            w, f = SCORE_MAP.get(rid, (1,2))
            pts = f if lv == 2 else (w if lv == 1 else 0)
            
            status = ''
            if lv == 2: status = f'❌ FAIL +{pts}'
            elif lv == 1: status = f'⚠️ WARN +{pts}'
            else: status = '✅ 正常'
            
            group_items.append((rid, meta, lv, det, status))
        
        if group_items:
            trig_count = sum(1 for _, _, lv, _, _ in group_items if lv > 0)
            print(f"\n  [{lv_name}级] ({trig_count}/{len(group_items)}条触发)")
            for rid, meta, lv, det, status in group_items:
                print(f"  ├─ {rid.split('.')[0]} {meta.get('name','')} [{status}]")
                if lv > 0:
                    print(f"  │  公式: {meta.get('formula','')}")
                    print(f"  │  数据: {meta.get('source','')}")
                    print(f"  │  详情: {det}")
                    print(f"  │  说明: {meta.get('desc','')}")
                    # 对于未触发但有数据的，显示关键数值
                else:
                    # 尝试提取关键数值
                    for k, v in details.items():
                        if k == rid:
                            print(f"  │  数据: {v[:60]}")
                            break
                print()
    
    # Layer 0
    l0_rules = ['🚫 审计意见非标', '⚠️ 审计意见带强调事项段', '⚠️ 年报延期披露', '🚫 监管处罚/立案调查']
    l0_found = {r:alerts.get(r,0) for r in l0_rules if alerts.get(r,0) > 0}
    if l0_found:
        print(f"\n  [Layer 0 一票否决]")
        for rid, lv in l0_found.items():
            print(f"  ├─ {rid}: {details.get(rid,'')}")

    # 组合加分
    combo_pts = 0; combo_det = []
    for name, cond, bonus in COMBO_RULES:
        if cond(alerts):
            combo_pts += bonus
            combo_det.append(f"{name}+{bonus}")
    if combo_det:
        print(f"\n  组合加分: {', '.join(combo_det)} = +{combo_pts}")
    
    print()
def analyze(input_str):
    print(f"\n{'='*50}")
    print(f"财报排雷分析 v3.0")
    print(f"{'='*50}\n")
    
    # Step 1: 解析
    name, symbol = resolve_stock(input_str)
    if not symbol:
        print(f"❌ 无法识别: {input_str}")
        return
    
    print(f"股票: {name or '?'} ({symbol})")
    
    # Step 2: 获取数据
    entry = fetch_data(name, symbol)
    if not entry: return
    
    years = sorted(entry['years'].keys(), key=int)
    name = entry['name']
    
    # Step 3: Layer 0
    alerts, details = {}, {}
    last_year = str(years[-1])
    bs_raw = entry['years'][last_year]['bs']
    opinion = bs_raw.get('OPINION_TYPE', '')
    notice_date = bs_raw.get('NOTICE_DATE', '')
    op_str = str(opinion)
    
    layer0_excluded = False
    if '标准无保留' in op_str and '带强调' not in op_str: pass
    elif '带强调' in op_str:
        alerts['⚠️ 审计意见带强调事项段'] = 1
        details['⚠️ 审计意见带强调事项段'] = f"审计意见: {op_str}"
    elif '保留意见' in op_str or '否定意见' in op_str or '无法表示意见' in op_str:
        alerts['🚫 审计意见非标'] = 2
        details['🚫 审计意见非标'] = f"审计意见: {op_str}"
        layer0_excluded = True
    
    # Step 3b: 监管处罚查询
    print("查询监管处罚记录...", flush=True)
    reg_found, reg_detail = check_regulatory(symbol)
    if reg_found:
        alerts['🚫 监管处罚/立案调查'] = 2
        details['🚫 监管处罚/立案调查'] = f"发现监管风险: {reg_detail[:100]}"
        layer0_excluded = True
    
    # 年报延期披露
    if notice_date and len(str(notice_date)) >= 10:
        try:
            if str(notice_date)[5:10] > '04-30':
                alerts['⚠️ 年报延期披露'] = 1
                details['⚠️ 年报延期披露'] = f"披露日: {notice_date[:10]}"
        except: pass
    
    if layer0_excluded:
        print(f"\n🚫 {name}: 一票否决，直接排除")
        if '🚫 审计意见非标' in details:
            print(f"  原因: {details['🚫 审计意见非标']}")
        if '🚫 监管处罚/立案调查' in details:
            print(f"  原因: {details['🚫 监管处罚/立案调查']}")
        return
    
    # Step 4: 跑规则
    d = extract_fields(entry)
    rule_alerts, rule_details = compute_rules(d)
    alerts.update(rule_alerts); details.update(rule_details)
    
    # Step 4b: 非财务规则（行业/股东/质押/董监高/PDF）
    print("获取非财务数据...", flush=True)
    profile = fetch_profile(symbol)
    pledge = fetch_pledge(symbol)
    holder = fetch_holder(symbol)
    dsh_trade = fetch_dsh_trade(symbol)
    pdf = fetch_pdf_analysis(symbol)
    bench = fetch_industry_benchmark(symbol)
    extra_alerts, extra_details = compute_extra_rules({}, {}, profile, pledge, holder, dsh_trade, pdf, bench)
    alerts.update(extra_alerts); details.update(extra_details)
    pdf_info = f" PDF:员工{pdf.get('employees','?')}人" if pdf else ""
    print(f"  行业: {profile.get('industry','?')}  质押: {pledge.get('pledge_total_pct',0):.1f}%  大股东: {holder.get('holder_top1_pct',0):.1f}%{pdf_info}")
    
    total, level, level_desc = compute_score(alerts)
    
    # Step 5: 输出报告
    print_rule_summary(alerts, details, d, name, symbol, total, level, level_desc)
    
    # 财务概览
    print("5年财务概览(亿元):")
    print(f"{'年份':>4} {'营收':>7} {'净利':>7} {'毛利率':>6} {'经营CF':>7} {'总资产':>8} {'存货':>6} {'研发率':>6}")
    for y in years:
        bs = entry['years'][y]['bs']; pl = entry['years'][y]['pl']; cf = entry['years'][y]['cf']
        r = float(pl.get('OPERATE_INCOME',0) or 0)
        np = float(pl.get('NETPROFIT',0) or 0)
        nco = float(cf.get('NETCASH_OPERATE',0) or 0)
        ta = float(bs.get('TOTAL_ASSETS',0) or 0)
        inv = float(bs.get('INVENTORY',0) or 0)
        rd = float(pl.get('RESEARCH_EXPENSE',0) or 0)
        cost = float(pl.get('OPERATE_COST',0) or 0)
        gm = (r-cost)/r*100 if r>0 else 0
        print(f"{y:>4} {r/1e8:>7.2f} {np/1e8:>7.2f} {gm:>5.1f}% {nco/1e8:>7.2f} {ta/1e8:>8.1f} {inv/1e8:>6.2f} {rd/r*100 if r>0 else 0:>6.1f}%")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法: python analyze.py <股票代码或名称>")
        print("示例: python analyze.py 600519")
        print("      python analyze.py 贵州茅台")
        print("      python analyze.py SH688041")
        sys.exit(1)
    analyze(sys.argv[1])
