# -*- coding: utf-8 -*-
"""
口径与分流回归测试 —— 不需要真实账单，用合成数据锁住容易回归的几条契约。

为什么需要它：真实账单入不了版本库（含个人流水），
但"分流口径"恰恰是最容易在改动中悄悄错掉的地方（把钱换位置算成花钱）。
这里用最小的合成样本把契约钉死。

    python test_ledger_rules.py        # 退出码 0 = 全过
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bill_importer import (  # noqa: E402
    partition, classify_all, normalize, BUCKET_LABEL, write_ledger_csv, CLS,
    ICBC_SUMMARY_MAP,
)

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got}" + ("" if ok else f"  (期望 {want})"))
    if not ok:
        FAILS.append(name)


def mk(platform, direction, amount, category, status="", txn_type=""):
    """直接构造一条已归类的 Txn（跳过分类器，专测分流）。"""
    from bill_importer import Txn
    t = Txn(platform=platform, occurred_at="2026-01-01 12:00:00", direction=direction,
            amount=amount, counterparty=category, description=category,
            status=status, txn_type=txn_type)
    t.category = category
    t.category_source = "rule"
    return t


def icbc_row(summary, amount, direction="expense", date="2026-01-01", time="12:00:00"):
    """构造一条工行解析结果，走**真实的** normalize + classify_all 路径。"""
    return {
        "date": date, "time": time, "amount": abs(amount), "direction": direction,
        "summary": summary, "region": "1202", "channel": "快捷支付",
        "account": "0000000000000000000", "currency": "人民币", "cash_flag": "钞",
        "deposit_type": "活期", "seq": "00000", "balance": 1000.0,
        "occurred_at": f"{date} {time}", "ext_id": f"{date} {time}|{summary}|{amount}",
    }


def classify_icbc(rows):
    """工行完整解析路径：normalize -> classify_all -> partition。"""
    ex = {"rows": rows, "pages": [], "meta": {}}
    txns = normalize("icbc", ex, -1)
    classify_all(txns)
    return txns, partition(txns)


def test_cls_contract():
    """四类口径映射：只有「消费」算花钱，其余都是钱换位置。"""
    print("\n■ 口径映射 CLS")
    for cat in ("转账·人际", "资金调拨", "投资", "投资·收益", "信用·还款", "退款"):
        check(f"{cat} 不计收支", CLS.get(cat) in ("transfer", "invest", "debt", "refund"), True)
    for cat in ("消费", "餐饮", "交通·网约车", "购物·电商", "服务·手续费", "未分类"):
        check(f"{cat} 计入主账本", CLS.get(cat), None)


def test_partition_buckets():
    """分流：银行流水的资金调拨带正负号，也**不能**进主账本。

    这里走的是**真实的**工行路径（摘要 -> 类目 -> 分流），不是手填类目，
    因为口径出错的根源就在"摘要到类目"这一步的映射。
    """
    print("\n■ 分流（重点：带符号的资金调拨，走真实工行路径）")
    rows = [
        icbc_row("理财", -10.00),          # 理财买入：钱换位置
        icbc_row("他行汇入", 500.00, "income"),
        icbc_row("还款", -2605.95),        # 还自己的信用卡
        icbc_row("消费", -48.00),          # 真实消费
        icbc_row("利息", 5.03, "income"),  # 存款利息
        icbc_row("工资", 8000.00, "income"),
    ]
    txns, p = classify_icbc(rows)
    cats = {t.status: (t.category, t.bucket) for t in txns}
    check("理财 -> 资金调拨/不计收支", cats["理财"], ("资金调拨", "neutral_flow"))
    check("还款 -> 信用·还款/不计收支", cats["还款"], ("信用·还款", "neutral_flow"))
    check("他行汇入 -> 转账·人际/不计收支", cats["他行汇入"], ("转账·人际", "neutral_flow"))
    check("利息 -> 投资·收益/不计收支", cats["利息"], ("投资·收益", "neutral_flow"))
    check("工资 -> 收入/计入", cats["工资"], ("收入", "ledger"))
    check("消费 -> 消费/计入", cats["消费"], ("消费", "ledger"))
    check("主账本只有消费+工资", sorted(t.amount for t in p["ledger"]), [48.00, 8000.00])
    check("不计收支 4 笔", len(p["neutral"]), 4)
    check("分流守恒", sum(len(p[k]) for k in p), len(txns))


def test_icbc_internal_transfers_are_not_spending():
    """核心口径：内部划转再多，也不该抬高「这个月花了多少」。"""
    print("\n■ 核心口径：内部划转不算消费")
    rows = [icbc_row("理财", -10.00, date=f"2026-01-{d:02d}") for d in range(1, 21)]
    rows += [icbc_row("消费", -48.00, date="2026-01-21")]
    txns, p = classify_icbc(rows)
    spent = sum(t.amount for t in p["ledger"] if t.direction == "expense")
    check("21 笔里真实消费只有 48", spent, 48.00)
    check("主账本笔数", len(p["ledger"]), 1)
    check("不计收支笔数", len(p["neutral"]), 20)


def test_zero_and_closed():
    """零金额单列、关闭态剔除。"""
    print("\n■ 零金额 / 关闭态")
    txns = [mk("icbc", "expense", 0.00, "消费"),
            mk("wechat", "expense", 20.00, "消费", status="交易关闭"),
            mk("icbc", "expense", 48.00, "消费")]
    p = partition(txns)
    check("零金额单列", len(p["zero"]), 1)
    check("关闭态剔除", len(p["closed"]), 1)
    check("主账本 1 笔", len(p["ledger"]), 1)
    check("分流守恒", sum(len(p[k]) for k in p), len(txns))


def test_neutral_never_in_ledger():
    """任意组合下：主账本里不允许出现 转账/投资/还款/退款 类目。"""
    print("\n■ 不变量：主账本不含非消费类目")
    cats = list(CLS.keys()) + ["消费", "餐饮", "交通·网约车", "医疗"]
    txns = [mk("icbc", "expense", 10.00, c) for c in cats]
    p = partition(txns)
    bad = [t.category for t in p["ledger"]
           if CLS.get(t.category) in ("transfer", "invest", "debt", "refund")]
    check("主账本里的非消费类目", bad, [])


def test_csv_bucket_column():
    """ledger.csv 必须带「口径桶」列 —— 下游只能靠它判断算不算收支。"""
    print("\n■ ledger.csv 输出契约")
    rows = [icbc_row("理财", -10.00), icbc_row("消费", -48.00)]
    txns, p = classify_icbc(rows)
    out = os.path.join(HERE, ".tmp_ledger_contract.csv")
    try:
        write_ledger_csv(p["ledger"] + p["neutral"], out)
        head = open(out, encoding="utf-8-sig").readline().strip()
        check("表头含「口径桶」", "口径桶" in head, True)
        body = open(out, encoding="utf-8-sig").read().splitlines()[1:]
        buckets = sorted(ln.split(",")[-1] for ln in body)
        check("桶标签", buckets, sorted([BUCKET_LABEL["ledger"], BUCKET_LABEL["neutral_flow"]]))
    finally:
        if os.path.exists(out):
            os.remove(out)


def test_insights_respects_bucket():
    """insights 的「消费」不能把资金调拨算进去。"""
    print("\n■ insights 按口径桶聚合")
    import insights
    rows = [
        {"平台": "icbc", "时间": "2026-01-01 12:00:00", "收支": "expense", "金额": 10.0,
         "类目": "资金调拨", "口径桶": "不计收支", "支付性质": "cash", "需确认": "",
         "商户": "理财", "商品说明": "理财", "状态": "理财"},
        {"平台": "icbc", "时间": "2026-01-02 12:00:00", "收支": "expense", "金额": 48.0,
         "类目": "消费", "口径桶": "计入", "支付性质": "cash", "需确认": "",
         "商户": "消费", "商品说明": "消费", "状态": "消费"},
    ]
    from datetime import datetime
    for r in rows:
        r["_dt"] = datetime.strptime(r["时间"], "%Y-%m-%d %H:%M:%S")
    check("消费金额只算 48", insights._expense(rows) and sum(
        r["金额"] for r in insights._expense(rows)), 48.0)


def test_wechat_transfer_rule():
    """微信「交易类型」可直接定类（无平台分类字段时的兜底路径）。"""
    print("\n■ 微信形态即类目")
    txns = [mk("wechat", "expense", 100.00, "未分类", txn_type="微信红包"),
            mk("wechat", "expense", 30.00, "未分类", txn_type="商户消费")]
    classify_all(txns)
    check("红包 -> 转账·人际", txns[0].category, "转账·人际")
    p = partition(txns)
    check("红包不进主账本", [t.amount for t in p["ledger"]], [30.00])


def test_sender_matching():
    """发件人白名单与域名匹配（防仿冒）。

    这里锁的是一个真实漏洞：用 `addr.endswith("icbc.com.cn")` 判断域名，
    会把 `phish@icbc.com.cn.evil.com` 判成可信 —— 域名层级是从右往左的，
    只有「完全相同」或「真子域」才算命中。
    """
    print("\n■ 发件人白名单与域名匹配")
    sys.path.insert(0, HERE)
    from qqmail_bills import match_reason, load_senders
    senders = load_senders()
    cases = [
        # (From, Subject, 是否应命中)
        ("中国工商银行 <webmaster@icbc.com.cn>", "中国工商银行历史明细（申请单号：X）", True),
        ("ICBC <noreply@icbc.com.cn>", "工行历史明细", True),
        ("工行 <x@mail.icbc.com.cn>", "交易明细", True),
        ("service@mail.alipay.com", "支付宝交易明细", True),
        ("wechatpay@tencent.com", "微信支付账单流水文件", True),
        ("phish@icbc.com.cn.evil.com", "历史明细", False),
        ("a@noticbc.com.cn.xx.com", "对账单", False),
        ("attacker@fakealipay.com", "支付宝账单", False),
        ("random@gmail.com", "我的账单", False),
    ]
    for frm, subj, want in cases:
        got = bool(match_reason({"frm": frm, "subj": subj}, senders))
        check(f"{frm[:38]}", got, want)


def test_icbc_summary_map_complete():
    """工行摘要映射必须覆盖实测出现的全部摘要（含带后缀的绩效类）。

    漏掉一个摘要的后果：那笔会掉进待确认队列，或者被误当成消费。
    """
    print("\n■ 工行摘要映射覆盖")
    seen = ["理财", "金融付款", "消费", "还款", "转账", "退款", "跨境汇款",
            "他行汇入", "工资", "绩效", "4月绩效", "利息", "费"]
    for s in seen:
        rows = [icbc_row(s, -10.00)]
        txns, _ = classify_icbc(rows)
        check(f"「{s}」已归类", txns[0].category != "未分类", True)
    check("「费」-> 服务·手续费", ICBC_SUMMARY_MAP.get("费"), "服务·手续费")
    # 摘要可能是两格拼起来的（「绩效」+「7月工资及5月」），也要能认出来
    txns, _ = classify_icbc([icbc_row("绩效 7月工资及5月", 3571.47, "income")])
    check("「绩效 7月工资及5月」-> 收入", txns[0].category, "收入")


if __name__ == "__main__":
    print("=" * 70)
    print("口径与分流回归测试（合成数据，无需真实账单）")
    print("=" * 70)
    test_cls_contract()
    test_partition_buckets()
    test_icbc_internal_transfers_are_not_spending()
    test_zero_and_closed()
    test_neutral_never_in_ledger()
    test_csv_bucket_column()
    test_insights_respects_bucket()
    test_wechat_transfer_rule()
    test_sender_matching()
    test_icbc_summary_map_complete()
    print("\n" + "=" * 70)
    if FAILS:
        print(f"❌ {len(FAILS)} 项未通过: {FAILS}")
        sys.exit(1)
    print("✅ 全部通过")
