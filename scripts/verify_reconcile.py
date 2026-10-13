# -*- coding: utf-8 -*-
"""
解析器对账校验（回归测试合同）—— 判定标准是**逐项差异为 0**，不是等于某个历史数字。

为什么必须这样判：写死绝对金额会在换账本后静默失效（金额随账单变化），
所以这里只写**不变量**：解析结果必须能还原官方自报的口径。

各平台要还原的口径：
  微信    支出 / 收入 / 中性交易
  支付宝  总笔数 / 支出金额 / 收入 / 不计收支
  工商银行 逐页「本页交易笔数 / 本页收入算术合计 / 本页支出算术合计」
           ＋ 余额链（本行余额 = 上行余额 ± 本行金额）

用法：
    # 工行 PDF（口令走环境变量，避免进 shell 历史）
    set ICBC_PDF_PWD=******  &&  python verify_reconcile.py --icbc "明细.pdf"
    python verify_reconcile.py --icbc "明细.pdf" --pdfpwd ******

    # 微信 / 支付宝 zip
    python verify_reconcile.py --zip "账单.zip" --pwds "111111"

退出码：全部通过 0；有任何一项差额非 0 则 1。
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from bill_importer import (  # noqa: E402
    icbc_extract, icbc_reconcile, open_bill, to_grid, find_header,
    detect_platform, parse_official_summary, normalize, partition, classify_all, _cells,
    CLOSED_STATES,
)

OK, BAD = "✅", "❌"


class Report:
    def __init__(self):
        self.items = []          # (名称, 还原值, 官方值, 差额)

    def add(self, name, got, want, diff=None):
        if diff is None and got is not None and want is not None:
            diff = round(float(got) - float(want), 2)
        self.items.append((name, got, want, diff))

    @property
    def failed(self):
        return [i for i in self.items if i[3] not in (None, 0, 0.0)]

    def render(self, title):
        print(f"\n■ {title}")
        for name, got, want, diff in self.items:
            if want is None:
                print(f"   {name}: 解析 {got}（官方未自报，跳过比对）")
                continue
            mark = OK if diff in (0, 0.0) else BAD
            print(f"   {name}: 解析 {got} vs 官方 {want}  差额 {diff}  {mark}")


def verify_icbc(path, password) -> int:
    print("=" * 74)
    print("工商银行历史明细 PDF —— 还原官方口径")
    print("=" * 74)
    ex = icbc_extract(path, password)
    rc = icbc_reconcile(ex)
    rows, pages = ex["rows"], ex["pages"]

    print(f"共 {rc['total']['n']} 笔，{ex['meta']['pages']} 页"
          f"（卡号后 4 位 {ex['meta']['card_tail']} —— 其余身份字段不进入输出）")

    n_total = len(rows)
    foot_n = sum(p["footer"].get("count") or 0 for p in pages)
    foot_income = round(sum(p["footer"].get("income") or 0.0 for p in pages), 2)
    foot_expense = round(sum(p["footer"].get("expense") or 0.0 for p in pages), 2)
    inc = round(sum(r["amount"] for r in rows if r["direction"] == "income"), 2)
    exp = round(sum(r["amount"] for r in rows if r["direction"] == "expense"), 2)

    R = Report()
    R.add("① 总笔数", n_total, foot_n)
    R.add("② 支出金额", exp, foot_expense)
    R.add("③ 收入金额", inc, foot_income)
    R.render("逐项还原（工行自报口径）")

    # 逐页：任何一页差额非 0 都算失败
    print("\n■ 逐页对账")
    page_bad = 0
    for p in rc["per_page"]:
        d = (p["diff_n"], p["diff_income"], p["diff_expense"])
        mark = OK if d == (0, 0.0, 0.0) else BAD
        page_bad += mark == BAD
        print(f"   p{p['page']:2d} 笔数 {p['n']:3d}/{p['n_official']} (d={p['diff_n']})"
              f" 收入 {p['income']:>10.2f}/{p['income_official']} (d={p['diff_income']})"
              f" 支出 {p['expense']:>10.2f}/{p['expense_official']} (d={p['diff_expense']}) {mark}")

    bc = rc["balance_chain"]
    print(f"\n■ 余额链（本行余额 = 上行余额 ± 本行金额）")
    print(f"   校验 {bc['checked']} 条，不一致 {bc['mismatch']} 条 "
          f"{OK if bc['mismatch'] == 0 else BAD}")
    for s in bc["samples"]:
        print(f"     !! {s['at']} 实际 {s['delta']:+.2f} 期望 {s['want']:+.2f}")

    # 入账分流不变量：主账本 + 不计收支 + 零金额 + 剔除 == 全集
    all_t = normalize("icbc", ex, -1)
    classify_all(all_t)                    # 分类必须先跑：分流要按类目判口径
    parts = partition(all_t)
    ssum = sum(len(parts[k]) for k in ("ledger", "neutral", "zero", "closed"))
    R2 = Report()
    R2.add("④ 分流守恒 (ledger+neutral+zero+closed)", ssum, n_total)
    R2.render("分流守恒")
    print(f"   主账本 {len(parts['ledger'])} · 不计收支 {len(parts['neutral'])}"
          f" · 零金额 {len(parts['zero'])} · 剔除 {len(parts['closed'])}")

    failed = R.failed + R2.failed + ([1] if page_bad else []) + ([1] if bc["mismatch"] else [])
    print("\n" + "=" * 74)
    print(f"{OK} 工行：全部差额为 0" if not failed else f"{BAD} 工行：存在差异，不要入账")
    return 1 if failed else 0


def verify_zip(path, pwds) -> int:
    """还原微信 / 支付宝官方口径。

    ⚠️ 支付宝的官方统计**自相矛盾**，所以不能直接拿"支出=全部支出行"去比，
    必须按官方口径还原（见 references/pitfalls.md 坑 1、坑 2）：
      · 支出金额 = 全部支出行 − **交易关闭**金额（笔数含关闭、金额不含）
      · 不计收支 = 全部不计收支行 − 桶内**基金买入退款**（在桶内冲减，不是跨桶抵消）
    这两条是"还原方法"，不是给解析器放水 —— 差额仍必须为 0。
    """
    print("=" * 74)
    print("微信 / 支付宝 zip —— 还原官方口径")
    print("=" * 74)
    files = open_bill(path, pwds[0] if pwds else None)
    rc_all = 0
    for name, raw in files:
        if not name.lower().endswith((".csv", ".xlsx")):
            continue
        grid, _ = to_grid(name, raw)
        hi = find_header(grid)
        plat = detect_platform(_cells(grid[hi]))
        txns = normalize(plat, grid, hi)
        off = parse_official_summary(grid, hi)

        R = Report()
        if plat == "wechat":
            # 微信账单干净：没有交易关闭、没有零金额，直接逐项相等
            R.add("支出金额", round(sum(t.amount for t in txns if t.direction == "expense"), 2),
                  (off.get("expense") or (None, None))[1])
            R.add("收入金额", round(sum(t.amount for t in txns if t.direction == "income"), 2),
                  (off.get("income") or (None, None))[1])
            R.add("中性交易", round(sum(t.amount for t in txns if t.direction == "neutral"), 2),
                  (off.get("neutral") or (None, None))[1])
        elif plat == "alipay":
            # ⚠️ 官方只把「支出桶」里的交易关闭扣掉；收入桶里的关闭行（如「收入/交易关闭」）
            #    官方**没有**从支出里扣。这个不对称是实测出来的，别自作聪明去改。
            closed_exp = [t for t in txns
                          if t.status in CLOSED_STATES and t.direction == "expense"]
            closed_any = [t for t in txns if t.status in CLOSED_STATES]
            fund_ref = [t for t in txns
                        if t.direction == "neutral" and "买入退款" in t.description]
            exp_amt = round(sum(t.amount for t in txns if t.direction == "expense")
                            - sum(t.amount for t in closed_exp), 2)
            neu_amt = round(sum(t.amount for t in txns if t.direction == "neutral")
                            - sum(t.amount for t in fund_ref), 2)
            R.add("总笔数", len(txns), off.get("total"))
            R.add("支出金额（− 支出桶的交易关闭）", exp_amt,
                  (off.get("expense") or (None, None))[1])
            R.add("收入金额", round(sum(t.amount for t in txns if t.direction == "income"), 2),
                  (off.get("income") or (None, None))[1])
            R.add("不计收支（− 桶内买入退款）", neu_amt, (off.get("neutral") or (None, None))[1])
            skipped = [t for t in closed_any if t not in closed_exp]
            print(f"   还原口径：支出桶关闭 {len(closed_exp)} 笔 "
                  f"¥{sum(t.amount for t in closed_exp):.2f}"
                  f" · 基金买入退款 {len(fund_ref)} 笔 ¥{sum(t.amount for t in fund_ref):.2f}")
            if skipped:
                print(f"   注意：另有 {len(skipped)} 笔「交易关闭」不在支出桶"
                      f"（合计 ¥{sum(t.amount for t in skipped):.2f}），官方未从支出扣除")
        else:
            print(f"  ⏭ {name}: 平台识别为 {plat}，跳过")
            continue
        R.render(f"{name} [{plat}]")
        rc_all |= 1 if R.failed else 0
    print("\n" + "=" * 74)
    print(f"{OK} 全部差额为 0" if not rc_all else f"{BAD} 存在差异")
    return rc_all


def main():
    ap = argparse.ArgumentParser(description="对账校验：还原官方口径，判定逐项差额为 0")
    ap.add_argument("--icbc", help="工商银行历史明细 PDF 路径")
    ap.add_argument("--pdfpwd", default=os.environ.get("ICBC_PDF_PWD"),
                    help="PDF 打开口令（建议用环境变量 ICBC_PDF_PWD，别进 shell 历史）")
    ap.add_argument("--zip", help="微信 / 支付宝 账单 zip 路径")
    ap.add_argument("--pwds", default="", help="zip 候选口令，逗号分隔")
    a = ap.parse_args()

    if not a.icbc and not a.zip:
        ap.error("至少给一个 --icbc 或 --zip")

    rc = 0
    if a.icbc:
        rc |= verify_icbc(a.icbc, a.pdfpwd)
    if a.zip:
        pwds = [p for p in (a.pwds or "").split(",") if p]
        rc |= verify_zip(a.zip, pwds)
    sys.exit(rc)


if __name__ == "__main__":
    main()
