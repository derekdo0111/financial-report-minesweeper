"""PDF年报数据提取模块 v3 - 全量提取
含：担保、关联交易、子公司、员工、董监高薪酬、重大合同、
    业绩承诺、分部报告、子公司经营数据、承诺事项、日后事项、NLP风险提示
"""
import re
from pypdf import PdfReader

def _page_range(reader, start, end=None):
    """遍历指定页范围"""
    e = end if end else len(reader.pages)
    for i in range(start, min(e, len(reader.pages))):
        yield i, reader.pages[i].extract_text() or ''

def extract_guarantee(pdf_path):
    """对外担保"""
    reader = PdfReader(pdf_path)
    total = hk = 0
    for i, text in _page_range(reader, 0):
        if '对外担保' in text or '担保情况' in text:
            for line in text.split('\n'):
                if '香港' in line and ('担保' in line or '保证' in line or '质押' in line or '万' in line):
                    nums = [float(n.replace(',','')) for n in re.findall(r'[\d,]+\.?\d*', line) if 10 < float(n) < 100000]
                    if nums: hk = max(hk, max(nums))
                if '实际担保余额合计' in line:
                    nums = re.findall(r'[\d,]+\.?\d*', line)
                    if nums: total = max(float(n.replace(',','')) for n in nums)
    return {'total_wan': total, 'hk_wan': hk, 'has_any': total > 0 or hk > 0}

def extract_subsidiaries(pdf_path):
    """子公司清单"""
    reader = PdfReader(pdf_path)
    r = {'total': 0, 'overseas': [], 'has_overseas': False}
    for i, text in _page_range(reader, 0):
        m = re.search(r'(\d+)家子公司', text)
        if m: r['total'] = int(m.group(1))
        for kw in ['香港', '柬埔寨', '境外']:
            if kw in text:
                for line in text.split('\n'):
                    if kw in line and ('有限' in line or '公司' in line):
                        r['overseas'].append(line.strip()[:60])
                        r['has_overseas'] = True
    r['overseas_count'] = len(set(r['overseas']))
    return r

def extract_related_party(pdf_path):
    """关联交易检测"""
    reader = PdfReader(pdf_path)
    for i, text in _page_range(reader, 0):
        if '关联方' in text and ('关联交易' in text or '购销' in text):
            full = ''
            for off in [-1, 0, 1]:
                idx = i + off
                if 0 <= idx < len(reader.pages): full += reader.pages[idx].extract_text() or ''
            if '采购商品' in full or '出售商品' in full:
                secs = re.split(r'采购商品/接受劳务情况表|出售商品/提供劳务情况表', full)
                n = sum(1 for s in secs[1:] for n in re.findall(r'[\d,]+\.?\d*', s[:300])
                        if float(n.replace(',','')) > 10000)
                return {'has_data': n > 3, 'nums': n}
    return {'has_data': False, 'nums': 0}

def extract_employees(pdf_path):
    """员工人数"""
    reader = PdfReader(pdf_path)
    for i, text in _page_range(reader, 0):
        m = re.search(r'母公司在职员工.*?(\d+[,]?\d*)', text)
        if m: return int(m.group(1).replace(',',''))
    return 0

def extract_dsh_compensation(pdf_path):
    """董监高薪酬总额"""
    reader = PdfReader(pdf_path)
    for i, text in _page_range(reader, 0):
        m = re.search(r'关键管理人员报酬.*?(\d[\d,]*\.?\d*)', text)
        if m: return float(m.group(1).replace(',',''))
    return 0

def extract_major_contracts(pdf_path):
    """重大合同"""
    reader = PdfReader(pdf_path)
    found = set()
    for i, text in _page_range(reader, 0):
        if ('其他重大' in text or '重大合同' in text) and '尚未执行' in text:
            for line in text.split('\n'):
                if '尚未执行' in line:
                    found.add(line.strip()[:100])
        # 找未执行的大额合同
        if '尚未执行' in text:
            nums = re.findall(r'[\d,]+\.?\d*', text)
            big = [float(n.replace(',','')) for n in nums if float(n) > 10000]
            if big:
                for line in text.split('\n'):
                    if '尚未执行' in line:
                        found.add(line.strip()[:100])
    return {'list': list(found)[:5], 'count': len(found)}

def extract_performance_commitment(pdf_path):
    """业绩承诺"""
    reader = PdfReader(pdf_path)
    found = []
    for i, text in _page_range(reader, 0):
        if '业绩承诺' in text and ('减值' in text or '商誉' in text or '达标' in text):
            for line in text.split('\n'):
                if '业绩承诺' in line:
                    found.append(line.strip()[:100])
    return {'found': len(found) > 0, 'mentions': found[:5]}

def extract_segment_info(pdf_path):
    """分部报告"""
    reader = PdfReader(pdf_path)
    ov_rev = 0
    for i, text in _page_range(reader, 0):
        if '分部' in text and ('收入' in text or '报告' in text):
            if '境外' in text or '国外' in text or '出口' in text:
                nums = [float(n.replace(',','')) for n in re.findall(r'[\d,]+\.?\d*', text) if float(n.replace(',','')) > 1e6]
                if nums: ov_rev = max(ov_rev, max(nums))
    return {'overseas_revenue': ov_rev, 'has_data': ov_rev > 0}

def extract_subsidiary_performance(pdf_path):
    """提取主要子公司营收/净利润（在其他主体中的权益章节）"""
    reader = PdfReader(pdf_path)
    subs_data = []
    in_section = False
    for i, text in _page_range(reader, 0):
        if '在其他主体中的权益' in text or '主要子公司' in text:
            in_section = True
        if in_section:
            # 优先找含"营业收入"和"净利润"的表
            lines = text.split('\n')
            for j, line in enumerate(lines):
                if ('营业收入' in line or '净利润' in line) and ('万元' in line or '亿' in line):
                    nearby = ' '.join(lines[max(0,j-3):min(len(lines),j+3)])
                    nums = [float(n.replace(',','')) for n in re.findall(r'[\d,]+\.?\d*', nearby) if 10 < float(n) < 1e8]
                    if nums:
                        # 找到上下文中的子公司名称
                        for k in range(max(0,j-5), min(len(lines),j)):
                            if '有限' in lines[k] or '公司' in lines[k]:
                                sub_name = lines[k].strip()[:30]
                                subs_data.append({'name': sub_name, 'vals': nums[:2]})
                                break
            # 如果遇到下个章节标题就退出
            if in_section and ('十二' in text or '十三' in text or '十四' in text) and len(subs_data) > 0:
                break
    return {'subsidiaries': subs_data[:5], 'count': len(subs_data)}

def extract_commitments(pdf_path):
    """承诺及或有事项"""
    reader = PdfReader(pdf_path)
    found = {'guarantees': False, 'litigation': False, 'commitments': []}
    for i, text in _page_range(reader, 0):
        if '承诺' in text and ('或有事' in text or '担保' in text):
            for line in text.split('\n'):
                if any(kw in line for kw in ['诉讼', '担保', '承诺', '质押', '抵押']):
                    found['commitments'].append(line.strip()[:80])
                if '诉讼' in line: found['litigation'] = True
                if '担保' in line: found['guarantees'] = True
    return {'items': found['commitments'][:10], 'has_litigation': found['litigation'],
            'has_guarantee_commit': found['guarantees'], 'count': len(found['commitments'])}

def extract_mda_risk(pdf_path):
    """NLP: MD&A风险提示分析 - 统计负面关键词"""
    reader = PdfReader(pdf_path)
    negative_kw = ['风险', '下滑', '亏损', '下降', '困难', '不稳定', '不确定性', '不利', '恶化',
                   '压力', '挑战', '波动', '萎缩', '减缓', '放缓', '过剩', '低迷', '亏损', '减少']
    
    in_mda = False
    scores = {}
    total_chars = 0
    
    for i, text in _page_range(reader, 0):
        # 检查MD&A章节开始
        if not in_mda:
            if ('经营情况' in text and '讨论' in text) or ('管理层' in text and '讨论' in text):
                in_mda = True
            continue
        # 检查MD&A章节结束
        if i > 5 and ('重要事项' in text or '公司治理' in text or '股份变动' in text or '公司简介' in text):
            break
        
        for kw in negative_kw:
            c = text.count(kw)
            if c > 0: scores[kw] = scores.get(kw, 0) + c
        total_chars += len(text)
    
    # 如果完全没有找到MD&A, 尝试搜索"董事会报告"或全文
    if not scores and total_chars == 0:
        for i, text in _page_range(reader, 3, 50):  # 通常3-50页是MD&A
            for kw in negative_kw:
                c = text.count(kw)
                if c > 0: scores[kw] = scores.get(kw, 0) + c
            total_chars += len(text)
    
    neg_count = sum(scores.values())
    rate = neg_count / max(total_chars, 1) * 10000
    return {
        'negative_words': neg_count,
        'rate_per_10k': round(rate, 2),
        'top_keywords': sorted(scores.items(), key=lambda x: -x[1])[:10],
        'chars': total_chars
    }

def extract_post_balance(pdf_path):
    """资产负债表日后事项"""
    reader = PdfReader(pdf_path)
    for i, text in _page_range(reader, 0):
        if '资产负债表日后' in text or '日后事项' in text:
            lines = text.split('\n')
            items = [l.strip() for l in lines if len(l.strip()) > 10 and '日' in l and '元' not in l]
            return {'found': True, 'items': items[:5], 'has_content': len(items) > 2}
    return {'found': False, 'items': [], 'has_content': False}

def analyze_pdf(pdf_path):
    """一站式全量PDF提取"""
    return {
        'guarantee': extract_guarantee(pdf_path),
        'subsidiaries': extract_subsidiaries(pdf_path),
        'related_party': extract_related_party(pdf_path),
        'employees': extract_employees(pdf_path),
        'dsh_compensation': extract_dsh_compensation(pdf_path),
        'contracts': extract_major_contracts(pdf_path),
        'performance': extract_performance_commitment(pdf_path),
        'segment': extract_segment_info(pdf_path),
        'subsidiary_perf': extract_subsidiary_performance(pdf_path),
        'commitments': extract_commitments(pdf_path),
        'mda_risk': extract_mda_risk(pdf_path),
        'post_balance': extract_post_balance(pdf_path),
    }
