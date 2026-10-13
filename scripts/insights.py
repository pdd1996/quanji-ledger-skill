# -*- coding: utf-8 -*-
"""
洞察引擎 —— 把清洗后的账本，翻译成用户真正想问的那几句话。
四类洞察：双视角对账 / 订阅与定投 / 异常检测 / 退款追踪
"""
from __future__ import annotations
import csv, os, sys, json, statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DEFAULT_WORK = os.environ.get("BILL_WORK") or os.path.join(os.path.expanduser("~"), ".bill-lens")
LEDGER = os.environ.get("BILL_LEDGER") or os.path.join(DEFAULT_WORK, "ledger.csv")

SUB_KW = ("订阅", "连续订阅", "自动续费", "会员", "包月", "包年", "续费")

@dataclass
class Insight:
    kind: str
    severity: str          # info | warn | alert
    title: str
    detail: str
    amount: float = 0.0
    evidence: list = field(default_factory=list)

def load(path=LEDGER):
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            r["金额"] = float(r["金额"])
            r["_dt"] = datetime.strptime(r["时间"][:19], "%Y-%m-%d %H:%M:%S")
            rows.append(r)
    return rows

# ---------------------------------------------------------------- 1. 双视角
def dual_perspective(rows):
    exp = [r for r in rows if r["收支"] == "expense"]
    total = sum(r["金额"] for r in exp)
    credit = [r for r in exp if r["支付性质"] == "credit"]
    cred_sum = sum(r["金额"] for r in credit)
    balance = [r for r in exp if r["支付性质"] == "balance"]
    repay = [r for r in rows if "还款" in r["类目"] and r["收支"] == "neutral"]
    repay_sum = sum(r["金额"] for r in repay)
    later = sorted(repay, key=lambda r: r["_dt"])[-1] if repay else None
    return dict(total=total, credit=cred_sum, credit_n=len(credit),
                cash=total - cred_sum, credit_ratio=(cred_sum / total if total else 0),
                balance_sum=sum(r["金额"] for r in balance),
                repay_sum=repay_sum, repay_n=len(repay),
                net_debt=cred_sum - repay_sum, last_repay=later)

# ---------------------------------------------------------------- 2. 订阅
def detect_subscriptions(rows):
    exp = [r for r in rows if r["收支"] == "expense"]
    groups = defaultdict(list)
    for r in exp:
        key = r["类目"]
        if any(k in r["商品说明"] or k in r["商户"] for k in SUB_KW) and "还款" not in key:
            groups[("kw:" + r["商户"],)].append(r)
        else:
            groups[(r["商户"],)].append(r)
    found = []
    for key, items in groups.items():
        items.sort(key=lambda r: r["_dt"])
        if len(items) < 2:
            continue
        amts = [i["金额"] for i in items]
        if statistics.pstdev(amts) > max(1.0, 0.1 * statistics.mean(amts)):
            continue                                     # 金额不稳定
        gaps = [(items[i + 1]["_dt"] - items[i]["_dt"]).days for i in range(len(items) - 1)]
        med = statistics.median(gaps) if gaps else 0
        cycle = None
        if 25 <= med <= 35: cycle = "每月"
        elif 6 <= med <= 8: cycle = "每周"
        elif 350 <= med <= 380: cycle = "每年"
        if not cycle:
            continue
        found.append(dict(merchant=items[0]["商户"], cycle=cycle, amount=statistics.mean(amts),
                          count=len(items), category=items[0]["类目"],
                          first=items[0]["时间"][:10], last=items[-1]["时间"][:10],
                          monthly=round(statistics.mean(amts) * (30 / med), 2)))
    # 关键词单次命中（如「1个月会员连续订阅」只出现一次）
    for key, items in groups.items():
        if len(items) == 1 and str(key[0]).startswith("kw:"):
            r = items[0]
            found.append(dict(merchant=r["商户"], cycle="疑似（仅 1 期）", amount=r["金额"],
                              count=1, category=r["类目"], first=r["时间"][:10],
                              last=r["时间"][:10], monthly=r["金额"]))
    return sorted(found, key=lambda x: -x["monthly"])

# ---------------------------------------------------------------- 3. 定投
def detect_invest_plans(rows):
    neu = [r for r in rows if r["收支"] == "neutral"]
    g = defaultdict(list)
    for r in neu:
        g[(r["商户"], r["商品说明"])].append(r)
    plans, incomes = [], []
    for (cp, desc), items in g.items():
        if len(items) < 5:
            continue
        amts = [i["金额"] for i in items]
        days = sorted(i["_dt"] for i in items)
        rec = dict(merchant=cp, plan=desc, count=len(items), total=round(sum(amts), 2),
                   single=Counter(amts).most_common(1)[0][0],
                   first=days[0].strftime("%Y-%m-%d"), last=days[-1].strftime("%Y-%m-%d"),
                   span=(days[-1] - days[0]).days + 1)
        (incomes if "收益" in desc or "发放" in desc else plans).append(rec)
    return (sorted(plans, key=lambda x: -x["total"]),
            sorted(incomes, key=lambda x: -x["total"]))

# ---------------------------------------------------------------- 4. 异常
def detect_anomalies(rows, months=(None, None)):
    exp = [r for r in rows if r["收支"] == "expense"]
    out = []
    bycat = defaultdict(list)
    for r in exp:
        bycat[r["类目"]].append(r)
    for cat, items in bycat.items():
        if len(items) < 5:
            continue
        med = statistics.median(i["金额"] for i in items)
        for r in items:
            if r["金额"] > max(150.0, med * 4) and r["金额"] > 300:
                out.append(Insight("anomaly_single", "warn",
                    f"单笔偏高：{r['商户']}",
                    f"{r['时间'][:10]} 支出 ¥{r['金额']:.2f}，是「{cat}」类目单笔中位数 ¥{med:.2f} 的 "
                    f"{r['金额']/med:.1f} 倍", r["金额"],
                    [f"{r['商户']} | {r['商品说明'][:40]} | {r['状态']}"]))
    # 重复扣款：同商户同金额 72h 内（排除定投小额）
    pair = defaultdict(list)
    for r in exp:
        pair[(r["商户"], r["金额"])].append(r)
    for (cp, amt), items in pair.items():
        if amt <= 10:
            continue
        items.sort(key=lambda r: r["_dt"])
        for i in range(len(items) - 1):
            if (items[i + 1]["_dt"] - items[i]["_dt"]) <= timedelta(hours=72):
                out.append(Insight("anomaly_dup", "warn",
                    f"疑似重复扣款：{cp}",
                    f"{items[i]['时间'][:16]} 与 {items[i+1]['时间'][:16]} 各扣 ¥{amt:.2f}", amt * 2,
                    [f"{items[i]['商品说明'][:30]}", f"{items[i+1]['商品说明'][:30]}"]))
    # 类目环比：比较"最近两个完整自然月"（由账本自身推断，不写死月份）
    def cat_sum(ym):
        c = defaultdict(float); n = defaultdict(int)
        for r in exp:
            if r["时间"][:7] == ym:
                c[r["类目"]] += r["金额"]; n[r["类目"]] += 1
        return c, n
    m1, m2 = months
    if m1 and m2:
        a, na = cat_sum(m1); b, nb = cat_sum(m2)
        for cat, v2 in b.items():
            v1 = a.get(cat, 0.0)
            # 基期只要求 >= 1 笔：单笔消费暴涨是最常见的环比异常，
            # 若要求 >= 2 笔，稀疏类目（如某月只去过一次餐厅）会被静默漏掉。
            if v1 >= 50 and v2 > v1 * 1.5 and na.get(cat, 0) >= 1:
                out.append(Insight("anomaly_mom", "info",
                    f"类目环比上涨：{cat}",
                    f"{m2} ¥{v2:.2f} vs {m1} ¥{v1:.2f}，上涨 {(v2/v1-1)*100:.0f}%", v2 - v1,
                    [f"{m1} {na.get(cat,0)} 笔 -> {m2} {nb.get(cat,0)} 笔"]))
    return out


def complete_months(rows, n=2):
    """"最近 n 个完整自然月"，由账本自身推断。

    规则：账本覆盖区间去掉首月与末月（都可能是残缺月），从剩余月份里取最后 n 个；
    剩余不足 n 个时退回账本实际覆盖的月份。返回 (较早, 较晚)，不足两月时为 (None, None)。
    """
    ms = sorted({r["时间"][:7] for r in rows})
    if len(ms) > 2:
        ms = ms[1:-1]
    tail = ms[-n:]
    if len(tail) < 2:
        return None, None
    return tail[0], tail[-1]

# ---------------------------------------------------------------- 5. 退款
def detect_refunds(rows):
    ref = [r for r in rows if "退款" in r["状态"] or "退款" in r["商品说明"]]
    done = sum(r["金额"] for r in ref)
    pend = [r for r in rows if "等待确认收货" in r["状态"] or "确认中" in r["状态"]]
    return dict(n=len(ref), total=done, pending=len(pend),
                pending_amt=sum(r["金额"] for r in pend),
                items=sorted(ref, key=lambda r: -r["金额"])[:8])

# ---------------------------------------------------------------- 6. 完整度
def coverage(rows):
    days = sorted({r["时间"][:10] for r in rows})
    d0, d1 = days[0], days[-1]
    have = set(days)
    d = datetime.strptime(d0, "%Y-%m-%d")
    end = datetime.strptime(d1, "%Y-%m-%d")
    miss = []
    while d <= end:
        s = d.strftime("%Y-%m-%d")
        if s not in have:
            miss.append(s)
        d += timedelta(days=1)
    return dict(first=d0, last=d1, days=len(days), missing=len(miss))


def build(rows):
    return dict(
        overview=dict(
            n=len(rows),
            expense=sum(r["金额"] for r in rows if r["收支"] == "expense"),
            income=sum(r["金额"] for r in rows if r["收支"] == "income"),
            review=sum(1 for r in rows if r["需确认"] == "Y"),
            platforms=dict(Counter(r["平台"] for r in rows)),
            categories=Counter(r["类目"] for r in rows if r["收支"] == "expense").most_common(),
        ),
        dual=dual_perspective(rows),
        subs=detect_subscriptions(rows),
        plans=detect_invest_plans(rows),
        anomalies=detect_anomalies(rows, months=complete_months(rows)),
        refunds=detect_refunds(rows),
        coverage=coverage(rows),
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="账单洞察引擎")
    ap.add_argument("--ledger", default=LEDGER, help="ledger.csv 路径")
    _a = ap.parse_args()
    LEDGER = _a.ledger
    rows = load(LEDGER)
    R = build(rows)
    o, d = R["overview"], R["dual"]
    print("=" * 76)
    print("■ 概览")
    print(f"  账本条数 {o['n']}  平台 {o['platforms']}")
    print(f"  消费(权责) ¥{o['expense']:.2f}   收入 ¥{o['income']:.2f}   待确认分类 {o['review']} 笔")
    print(f"  覆盖 {R['coverage']['first']} ~ {R['coverage']['last']}，共 {R['coverage']['days']} 天有记录，"
          f"无记录 {R['coverage']['missing']} 天")
    print()
    print("■ ① 双视角对账（权责 vs 现金流）")
    print(f"  消费发生额      ¥{d['total']:.2f}")
    print(f"  其中信用消费    ¥{d['credit']:.2f}  ({d['credit_n']} 笔, 占 {d['credit_ratio']*100:.1f}%)")
    print(f"  余额类支付      ¥{d['balance_sum']:.2f}")
    print(f"  推算现金流出    ¥{d['cash']:.2f}")
    print(f"  已还信用        ¥{d['repay_sum']:.2f}  ({d['repay_n']} 期)")
    print(f"  期间净负债变化  ¥{-d['net_debt']:+.2f}   (正=负债减少)")
    if d["last_repay"]:
        r = d["last_repay"]
        print(f"  最近一期还款    {r['时间'][:10]}  ¥{r['金额']:.2f}  {r['商品说明'][:30]}")
    print()
    print("■ ② 订阅识别")
    print(f"  共 {len(R['subs'])} 项")
    for s in R["subs"][:10]:
        print(f"    · {s['merchant'][:16]:16s} {s['cycle']:12s} ¥{s['amount']:>7.2f}  "
              f"({s['count']} 期, {s['first']}~{s['last']})  折合月均 ¥{s['monthly']:.2f}  [{s['category']}]")
    print()
    print("■ ③ 定投 / 收益")
    _plans, _incomes = R["plans"]
    for p in _plans[:8]:
        print(f"    定投 {p['plan'][:38]:38s} {p['count']:3d}笔 × {p['single']:>7.2f} = {p['total']:>9.2f}  ({p['span']}天)")
    for p in _incomes[:5]:
        print(f"    收益 {p['plan'][:38]:38s} {p['count']:3d}笔          合计 {p['total']:>9.2f}  ({p['span']}天)")
    print()
    print("■ ④ 异常检测")
    for a in R["anomalies"]:
        print(f"    [{a.severity}] {a.title}")
        print(f"          {a.detail}")
    print()
    print("■ ⑤ 退款追踪")
    rf = R["refunds"]
    print(f"  退款 {rf['n']} 笔 合计 ¥{rf['total']:.2f}；进行中 {rf['pending']} 笔 ¥{rf['pending_amt']:.2f}")
    for r in rf["items"][:6]:
        print(f"    {r['时间'][:10]}  ¥{r['金额']:>8.2f}  {r['状态']:8s} {r['商品说明'][:38]}")
    dst = os.path.join(os.path.dirname(os.path.abspath(LEDGER)), "insights.json")
    json.dump(R, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=2, default=str)
    print("\n写出", dst)