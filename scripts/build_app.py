# -*- coding: utf-8 -*-
"""
把清洗后的账本渲染成单文件可交互 App 原型（记账App.html）。

用法：
    python build_app.py --ledger <ledger.csv> [--out <记账App.html>] [--title "AI 记账"]

模板资源位于 <skill>/assets/app_src/{style.css,shell.html,app.js}。
"""
from __future__ import annotations
import argparse, json, os, sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from insights import load, build, LEDGER as LEDGER_DEFAULT

ASSETS = os.path.join(SKILL_ROOT, "assets", "app_src")

CLS = {"转账·人际": "transfer", "资金调拨": "transfer", "投资": "invest", "投资·收益": "invest",
       "信用·还款": "debt", "退款": "refund"}
ICON = {
    "餐饮": "🍜", "餐饮·快餐": "🍔", "餐饮·咖啡": "☕", "餐饮·茶饮": "🧋", "餐饮·零食": "🍿",
    "交通": "🚕", "交通·网约车": "🚕", "交通·公交": "🚌", "交通·停车": "🅿️",
    "交通·共享单车": "🚲", "交通·火车": "🚄",
    "购物·日用": "🛒", "购物·数码": "💻", "购物·电商": "📦", "购物·超市": "🏪", "购物·便利": "🏪",
    "生活缴费": "💡", "医疗": "🏥", "汽车": "🚗", "其他": "⚪",
    "订阅·健康": "⭐", "订阅·内容": "⭐", "订阅·办公": "⭐", "订阅·AI服务": "🤖",
    "转账·人际": "💸", "投资": "📈", "投资·收益": "📈", "信用·还款": "💳", "公益捐赠": "❤️",
    "服务": "🔧", "生活服务": "🧾", "运动": "🏃", "家居": "🛋️", "宠物": "🐾", "教育": "📚",
    "资金调拨": "🔄", "收入": "💰", "退款": "↩️", "未分类": "❓",
}


def make_data(rows, R):
    tx, review = [], []
    for i, r in enumerate(rows):
        tx.append([r["时间"][:7], r["时间"][:16], r["类目"], CLS.get(r["类目"], "consume"),
                   round(r["金额"], 2), {"expense": "e", "income": "i"}.get(r["收支"], "n"),
                   r["商户"][:18], r["商品说明"][:26], r["支付性质"],
                   "微信" if r["平台"] == "wechat" else "支付宝", r["状态"][:12]])
        if r["需确认"] == "Y":
            review.append(i)
    months = sorted({t[0] for t in tx})
    eco = Counter(t[0] for t in tx if t[5] == "e")
    plans, incomes = R["plans"]
    return dict(
        months=months, default=max(months, key=lambda m: eco.get(m, 0)), tx=tx, icons=ICON,
        allcats=sorted(set(t[2] for t in tx)),
        insight=dict(
            plans=list(plans), incomes=list(incomes), subs=R["subs"],
            anomalies=[dict(a.__dict__) for a in R["anomalies"]],
            refunds={k: v for k, v in R["refunds"].items() if k != "items"},
            refund_items=[dict(t=r["时间"][:10], a=r["金额"], d=r["商品说明"][:40])
                          for r in R["refunds"]["items"]],
        ), review=review)


def main():
    ap = argparse.ArgumentParser(description="生成单文件记账 App 原型")
    ap.add_argument("--ledger", default=LEDGER_DEFAULT)
    ap.add_argument("--out", default=None)
    ap.add_argument("--title", default="AI 记账 · 原型")
    a = ap.parse_args()

    for need in ("style.css", "shell.html", "app.js"):
        p = os.path.join(ASSETS, need)
        if not os.path.exists(p):
            sys.exit(f"缺少模板资源: {p}")
    if not os.path.exists(a.ledger):
        sys.exit(f"账本不存在: {a.ledger}（先跑 bill_importer / qqmail_bills run 生成）")

    rows = load(a.ledger)
    R = build(rows)
    DATA = make_data(rows, R)
    J = json.dumps(DATA, ensure_ascii=False, default=str).replace("<", "\\u003c")

    css = open(os.path.join(ASSETS, "style.css"), encoding="utf-8").read()
    js = open(os.path.join(ASSETS, "app.js"), encoding="utf-8").read()
    shell = open(os.path.join(ASSETS, "shell.html"), encoding="utf-8").read()
    html = (shell.replace("__CSS__", css).replace("__JS__", js)
                 .replace("__DATA__", J).replace("<title>AI 记账 · 原型</title>",
                                                 f"<title>{a.title}</title>"))
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.ledger)), "记账App.html")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    open(out, "w", encoding="utf-8").write(html)

    o = DATA
    print(f"写出: {out}  ({os.path.getsize(out):,} bytes)")
    print(f"  账本 {len(DATA['tx'])} 笔 · 月份 {DATA['months']} · 默认 {DATA['default']}")
    print(f"  待确认 {len(DATA['review'])} 笔 · 定投 {len(R['plans'][0])} · 订阅 {len(R['subs'])} · 异常 {len(R['anomalies'])}")


if __name__ == "__main__":
    main()