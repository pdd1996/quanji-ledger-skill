# -*- coding: utf-8 -*-
"""
账单导入引擎 —— 基于微信 / 支付宝 / 工商银行「真实导出文件」实测结构实现。

实测结构（官方导出；实测年月已隐去）：
  支付宝：ZipCrypto zip -> CSV(GBK) -> 23 行 preamble -> 13 列表头 -> 数百行
  微信  ：ZipCrypto zip -> XLSX       -> 17 行 preamble -> 11 列表头 -> 数十行
  工商银行：加密 PDF（打开口令，非解压口令）-> 14 页横排表格 -> 逐行 11 列

设计要点（全部由真实数据驱动）：
  1. 先解密（zip 用 ZipCrypto 口令、工行 PDF 用打开口令；口令由用户提供，不落盘）
  2. 识别平台 -> 跳过 preamble / 按列坐标切表 -> 字段清洗（GBK / strip TAB）
  3. 解析官方自报的统计（支付宝/微信 preamble、工行逐页脚注），作为【对账校验基准】
  4. 四向分流：(收/支 x 交易状态)
       支出/收入 + 成功  -> 主账本
       交易关闭          -> 剔除（未完成的交易，官方统计也不计）
       不计收支/中性     -> 资金流水区（默认折叠）
       退款成功          -> 与原始交易配对冲销
  5. 聚合：同一计划的重复小额（如每日定投 10.00 x N）折叠成一条
  6. 分类：支付宝用自带「交易分类」做映射；微信/工行无分类 -> 商户知识库 + 待确认
  7. 隐私：姓名/卡号/账号/昵称一律不进入输出（工行只保留卡号后 4 位）
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

WORK = os.environ.get("BILL_WORK") or os.path.join(os.path.expanduser("~"), ".bill-lens")

# ============================================================================
# 1. 解密
# ============================================================================

def open_bill(zip_path: str, password: Optional[str] = None):
    """解密账单压缩包，返回 [(内部文件名, 字节内容)]。口令仅在内存中使用。"""
    pwd = password.encode() if password else None
    out = []
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            out.append((info.filename, z.read(info, pwd=pwd)))
    return out


def sniff_encoding(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except Exception:
            continue
    return "gbk"


def to_grid(name: str, raw: bytes, pdf_path: Optional[str] = None,
            pdf_password: Optional[str] = None) -> tuple:
    """把 CSV / XLSX / PDF 统一成后续可解析的形式。

    CSV / XLSX 返回 (二维列表, 类型)；工行 PDF 返回 (解析结果 dict, "pdf")，
    由 normalize 走 icbc 分支。PDF 走「路径」而不是字节，因为解密库要自己读文件。
    """
    low = name.lower()
    if low.endswith(".pdf"):
        if not pdf_path:
            raise ValueError("解析 PDF 需要文件路径（pdf_path 为空）")
        return icbc_extract(pdf_path, pdf_password), "pdf"
    if low.endswith(".csv"):
        text = raw.decode(sniff_encoding(raw), errors="replace")
        return [r for r in csv.reader(io.StringIO(text))], "csv"
    if low.endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        ws = wb.worksheets[0]
        grid = [[ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
                for r in range(1, ws.max_row + 1)]
        return grid, "xlsx"
    raise ValueError(f"不支持的文件类型: {name}")


# ============================================================================
# 1.5 工商银行历史明细 PDF（唯一的 PDF 来源）
# ============================================================================
#
# 实测结构（工行 App / 网银「历史明细下载」）—— 这个格式的几何关系必须照抄，
# 因为它是**整页横排**的，靠「按行读文本」是读不出来的：
#
#   * 文件本身加密，需要【打开口令】—— 导出时由用户自行设置，不是解压口令。
#   * `get_text("words")` 的 (x0, y0) 在这份 PDF 里的含义是反的：
#       x0 = 沿表格「行」方向的位置（越大越靠下）
#       y0 = 沿表格「列」方向的位置（越大越靠表格左侧）
#     所以 **x0 才是行号，y0 才是列**。
#   * 行距（同一列里相邻两行的 x0 差）：7.0（或 7.05），个别行 8.0。
#   * 行内偏移（相对该行日期格的 x0）：9 个数据显示列在 +3.5，时间在 +3.5 ~ +7.0。
#     ➜ 行内偏移(≤7) 和行距(7) 同量级，**任何"按 x 聚簇"的做法都会裂行或串行**。
#       正确做法是：以「日期格」当行锚点，用中位行距切行带，再按内容认列。
#   * 列头：交易日期 | 账号 | 储种 | 序号 | 币种 | 钞汇 | 摘要 | 地区 | 收入/支出金额 | 余额 | 渠道
#   * 「摘要」列在实表里可能是两个物理格（如「绩效」+「7月工资及5月」），
#     必须并成一格，否则整行列位会右移 —— 这是本格式最容易出错的地方。
#   * 金额列自带正负号（+ 收入 / - 支出），**只能靠符号判方向**。
#   * 每页页脚有「本页支出算术合计 / 本页收入算术合计 / 本页交易笔数」，
#     是工行自报的口径，可逐页对账。
#   * 「余额」列构成一条链：本行余额 = 上行余额 + 本行金额。这是最强的校验不变量。
#   * 水印文字（「中国工商银行 …」「请扫描二维码」等）会散布在表格的**行间**，
#     必须靠「带符号金额」把它们挡掉，否则会多出假的明细行。
#
# 隐私：卡号 / 户名 / 账号只在内存里参与解析，绝不进输出；对外只保留卡号后 4 位。

ICBC_COLS = ["交易日期", "账号", "储种", "序号", "币种", "钞汇",
             "摘要", "地区", "收入/支出金额", "余额", "渠道"]

# 行距的实测区间：7.0 ~ 8.0，取 5.0/12.0 作为「同带」判定上下界
ICBC_ROW_MIN = 5.0
ICBC_ROW_MAX = 12.0
# 行带宽度 = 中位行距 × 该系数（行内偏移最大 7.0，需要留够）
ICBC_BAND_FACTOR = 1.35

# 列带：由列头 y0 的中点推导（列头本身写在列带顶端，不能直接当边界）。
#
# ⚠️ 方向坑（本格式最坑的一点，实测验证过）：
#   y0 **越小越靠表格右侧**。所以按 y0 升序排列时，列序是**反的**：
#       ICBC_COLS      = 交易日期 … 渠道        （左 -> 右）
#       ICBC_BANDS     = 渠道 … 交易日期        （y0 小 -> 大）
#   bounds 与 ICBC_BANDS 一一对应：bounds[0]=-inf 配「渠道」，bounds[11]=+inf 配「交易日期」。
#   把 bounds 和 ICBC_COLS 直接配对会把每一列都错位一格，且金额/余额会互换 —— 表现是
#   明细全被丢掉（因为取不到「收入/支出金额」）。
ICBC_BANDS = list(reversed(ICBC_COLS))

# 下面这份是「文件里找不到列头」时的兜底边界（实测值，按 y0 升序，配 ICBC_BANDS）。
ICBC_BOUNDARY_FALLBACK = [-1e9, 142.83, 230.09, 322.68, 391.10, 434.35,
                          472.51, 510.67, 551.37, 606.32, 677.09, 1e9]

_ICBC_AMOUNT_RE = re.compile(r"^[+-][\d,]+\.\d{2}$")
_ICBC_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ICBC_TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")

# 水印 token：会落在「摘要」列带里的那些（收单编号 / 下单日期 / 银行名等）。
# 它们不是摘要内容，必须剔掉，否则类目会被污染成「2026-10-13」这种噪声。
_ICBC_NOISE_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{4,}|[0-9A-F]{8,}|中国工商银行)$")


def _icbc_clean_summary(vals: list) -> str:
    """摘要可能被拆成多个格（「绩效」+「7月工资及5月」），需合并；
    同时剔除混进来的水印 token。"""
    keep = [t for t in vals if t.strip() and not _ICBC_NOISE_RE.match(t.strip())]
    return " ".join(keep).strip()


class PdfPasswordNeeded(RuntimeError):
    """PDF 需要打开口令，但调用方没有提供（或提供的不对）。"""


def _pymupdf():
    """按需导入 PDF 库；没装时给出明确的安装提示，不静默失败。"""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        pass
    try:
        import fitz
        return fitz
    except ImportError:
        raise RuntimeError("解析工行 PDF 需要 PyMuPDF：pip install pymupdf")


def open_pdf(path: str, password: Optional[str] = None):
    """打开（可能加密的）PDF。口令仅用于本次调用，不落盘。"""
    mupdf = _pymupdf()
    try:
        doc = mupdf.open(path)
    except Exception as e:
        raise RuntimeError(f"无法打开 PDF：{e}") from e
    if doc.needs_pass:
        ok = False
        for cand in (password, ""):
            if cand is None:
                continue
            try:
                ok = bool(doc.authenticate(cand))
            except Exception:
                ok = False
            if ok:
                break
        if not ok:
            doc.close()
            raise PdfPasswordNeeded(
                "该 PDF 需要打开口令（工行导出时由你设置的 6 位密码）。"
                "用 --pdfpwd <口令> 提供；它只在本机使用，不落盘。")
    return doc


def icbc_page_words(doc, pno: int) -> list:
    """把一页的 words 归一成 [(行位置 x0, 列位置 y0, 文本)]，行内按列序（y 从大到小）排好。"""
    try:
        raw = doc[pno].get_text("words")
    except Exception:
        return []
    return [(w[0], w[1], w[4]) for w in raw if str(w[4]).strip()]


def icbc_is_icbc(doc) -> bool:
    """是否工行历史明细：标题或列头命中即算。"""
    try:
        t = doc[0].get_text()
    except Exception:
        return False
    if "中国工商银行" in t and "历史明细" in t:
        return True
    return all(c in t for c in ("交易日期", "收入/支出金额", "余额", "渠道"))


def icbc_privacy_findings(doc) -> list:
    """返回文件里出现的身份字段名（仅用于提示，不返回值本身）。"""
    try:
        t = doc[0].get_text()
    except Exception:
        return []
    return [k for k in ("卡号", "户名", "账号") if k in t]


def icbc_row_anchors(ws: list) -> list:
    """取行锚点：**带符号金额格**的 x0。

    只有明细行才有「收入/支出金额」格，所以它每行恰好一个、且水印里不会出现
    （水印是收单编号、机构号、「中国工商银行」这类不含正负号的短串）。
    实测这一列所有行的 x0 完全对齐（比日期格更整齐），是最可靠的行标志。

    反例（不要用）：日期格。水印里也含「2026-10-13」这种日期戳，
    用它当锚点会掺进噪声，把中位行距算成 7.5 而不是真实的 18，导致整行被切碎。
    """
    xs = sorted({round(x0, 2) for x0, _, t in ws if _ICBC_AMOUNT_RE.match(t)})
    if len(xs) <= 1:
        return xs
    # 去掉水印造成的密集簇：只保留与左边邻居间距 >= ICBC_ROW_MIN 的
    keep = [xs[0]]
    for x in xs[1:]:
        if x - keep[-1] >= ICBC_ROW_MIN:
            keep.append(x)
    return keep


def icbc_row_pitch(anchors: list) -> float:
    """行距 = 相邻锚点间距的中位数（水印已被 icbc_row_anchors 滤掉）。"""
    gaps = sorted(round(b - a, 2) for a, b in zip(anchors, anchors[1:]))
    if not gaps:
        return 7.0
    return gaps[len(gaps) // 2]


def icbc_row_bands(anchors: list, pitch: float) -> list:
    """把行锚点切成互不重叠的行带。

    行带 = [前一锚点与本锚点的中点, 本锚点与后一锚点的中点)，
    但宽度不超过 pitch × ICBC_BAND_FACTOR，避免行距异常时把隔壁行吃进来。
    """
    bands = []
    for i, a in enumerate(anchors):
        prev = anchors[i - 1] if i else a - pitch
        nxt = anchors[i + 1] if i + 1 < len(anchors) else a + pitch
        width = pitch * ICBC_BAND_FACTOR
        lo = max((prev + a) / 2.0, a - width / 2.0)
        hi = min((a + nxt) / 2.0, a + width / 2.0)
        bands.append((a, lo, hi))
    return bands


def icbc_header_row(doc) -> tuple:
    """在一页里找到列头行，返回 (行位置 x0, {列名: y0})。

    注意：水印里也会出现「交易日期」这种字样（它是明细的起止日期戳），
    所以**不能见到「交易日期」就认**，要比一比哪个行位置命中的列名最多。
    """
    best_x, best_hits = None, {}
    for i in range(min(doc.page_count, 3)):
        try:
            ws = doc[i].get_text("words")
        except Exception:
            continue
        by_x = defaultdict(dict)
        for w in ws:
            if w[4] in ICBC_COLS:
                by_x[round(w[0], 1)][w[4]] = w[1]
        for x, hits in by_x.items():
            if len(hits) == len(ICBC_COLS):
                return x, hits
            if len(hits) > len(best_hits):
                best_x, best_hits = x, hits
    return best_x, best_hits


def icbc_col_bounds(doc) -> list:
    """由列头 y0 推导各列带的上下界（n 列 -> n+1 个边界）。

    列头文字写在列带顶端，所以**中间**边界取相邻列头 y0 的中点；
    但最外两条必须是无穷 —— 若拿首/末列头的 y0 当边界，
    第一列（交易日期，y0≈713）和最后一列（渠道，y0≈96）会被整列切掉。
    """
    _, hits = icbc_header_row(doc)
    if len(hits) != len(ICBC_COLS):
        return list(ICBC_BOUNDARY_FALLBACK)
    avg = dict(hits)
    # ICBC_BANDS 已按 y0 升序（渠道 -> 交易日期），相邻两者取中点即为边界。
    mids = [round((avg[ICBC_BANDS[k - 1]] + avg[ICBC_BANDS[k]]) / 2.0, 2)
            for k in range(1, len(ICBC_BANDS))]
    return [-1e9] + mids + [1e9]


def icbc_date_split(doc, bounds: list) -> float:
    """「交易日期」列里含两个格：日期（y0≈709.77）和时间（y0≈713.27）。

    「交易日期」是 y0 最大的那一列，即 (bounds[-2], bounds[-1]]。
    按 y0 一维聚成两簇，取两簇中心的中点当分界。找不到两簇就用兜底值。
    """
    lo_b, hi_b = bounds[-2], bounds[-1]
    vals = []
    for i in range(min(doc.page_count, 3)):
        try:
            ws = doc[i].get_text("words")
        except Exception:
            continue
        for w in ws:
            if lo_b < w[1] <= hi_b and (_ICBC_DATE_RE.match(str(w[4]))
                                        or _ICBC_TIME_RE.match(str(w[4]))):
                vals.append(w[1])
    if not vals:
        return 711.5
    vals.sort()
    gaps = [(vals[i + 1] - vals[i], i) for i in range(len(vals) - 1)]
    if not gaps:
        return 711.5
    _, cut = max(gaps)
    if cut + 1 >= len(vals):
        return 711.5
    return round((vals[cut] + vals[cut + 1]) / 2.0, 2)


def icbc_row_cells(cells: list, bounds: list, date_split: float) -> dict:
    """把一行的格按列带切进 11 列，并按内容挑出真正的值。

    cells: 该行带内的 [(列位置 y0, 文本)]，顺序无所谓。

    **注意方向**：列带是左闭右开的 (bounds[k], bounds[k+1]]，其中
    bounds[0] = -inf、bounds[11] = +inf，中间是相邻列头 y0 的中点。
    也就是「y0 越小越靠表格右侧」（交易日期在 +inf 那头，渠道在 -inf 那头）。

    **必须按内容过滤**：工行的水印（申请单号尾段、收单编号、银行名这类短串）
    会落在列带之内，只按 y0 归位会把水印当成金额/余额/钞汇。
    """
    buckets = defaultdict(list)
    for y0, t in cells:
        t = t.strip()
        if not t:
            continue
        for k, name in enumerate(ICBC_BANDS):      # 注意：ICBC_BANDS，不是 ICBC_COLS
            if bounds[k] < y0 <= bounds[k + 1]:
                buckets[name].append((y0, t))
                break

    def pick(name, pattern=None):
        got = buckets.get(name, [])
        if pattern is not None:
            got = [(y, t) for y, t in got if pattern.match(t)]
        got.sort(reverse=True)                     # y0 大 = 靠左
        return got[0][1] if got else ""

    def pick_all(name):
        """同列多格：按 y0 从大（靠左）到小（靠右）返回原序。"""
        return [t for _, t in sorted(buckets.get(name, []), reverse=True)]

    # 日期 / 时间同处「交易日期」列带：时间在该格的左侧，所以 y0 更大
    d_dates = [(y, t) for y, t in buckets.get("交易日期", [])
               if y <= date_split and _ICBC_DATE_RE.match(t)]
    d_times = [(y, t) for y, t in buckets.get("交易日期", [])
               if y > date_split and _ICBC_TIME_RE.match(t)]
    row = {
        "date": sorted(d_dates)[0][1] if d_dates else "",
        "time": sorted(d_times)[-1][1] if d_times else "00:00:00",
        "账号": pick("账号"),
        "储种": pick("储种"),
        "序号": pick("序号"),
        "币种": pick("币种"),
        "钞汇": pick("钞汇", re.compile(r"^钞(?:/汇)?$|^汇$")),
        "摘要": _icbc_clean_summary(pick_all("摘要")),
        "地区": pick("地区"),
        "收入/支出金额": pick("收入/支出金额", _ICBC_AMOUNT_RE),
        "余额": pick("余额", re.compile(r"^[\d,]+\.\d{2}$")),
        "渠道": pick("渠道"),
    }
    return row


def _icbc_footer(joined: str) -> dict:
    """从页脚文字里抽官方自报统计。"""
    n = re.search(r"交易笔数[：:]\s*(\d+)", joined)
    e = re.search(r"支出算术合计[：:]\s*([\d,]+\.\d{2})", joined)
    i = re.search(r"收入算术合计[：:]\s*([\d,]+\.\d{2})", joined)
    if not (n or e or i):
        return {}
    return {"count": int(n.group(1)) if n else None,
            "expense": float(e.group(1).replace(",", "")) if e else None,
            "income": float(i.group(1).replace(",", "")) if i else None}


def icbc_extract(path: str, password: Optional[str] = None) -> dict:
    """解析工行历史明细 PDF。

    返回 {"rows": [...], "pages": [{page, rows:[...], footer}], "meta": {...}}
    每行：{date, time, occurred_at, direction, amount, summary, region, channel,
           account, currency, cash_flag, balance, ext_id, seq}
    """
    doc = open_pdf(path, password)
    try:
        if not icbc_is_icbc(doc):
            raise ValueError("不是工商银行历史明细 PDF（标题/列头不匹配）")

        # ---- 第一遍：逐页取文本 + 建列带（列带全文件共用一份）----
        bounds = icbc_col_bounds(doc)
        date_split = icbc_date_split(doc, bounds)
        pages = [icbc_page_words(doc, i) for i in range(doc.page_count)]

        # ---- 第二遍：以「金额格」为行锚点逐行切 ----
        # 行距(18) 与 行内偏移(最大 7.0) 量级不同但都很小 —— 不能按 x 自由聚簇。
        # 每个明细行有且只有一个带符号金额格，用它当锚点最稳（水印里没有金额格）。
        all_rows, page_info = [], []
        for i, ws in enumerate(pages):
            if not ws:
                continue
            cells = sorted(((x0, y0, t) for x0, y0, t in ws if t.strip()),
                           key=lambda c: (c[0], -c[1]))
            anchors = icbc_row_anchors(ws)
            footer_parts = [t for _, _, t in ws if t.startswith("本页")]
            if not anchors:
                page_info.append({"page": i + 1, "rows": [],
                                  "footer": _icbc_footer(" ".join(footer_parts))})
                continue
            bands = icbc_row_bands(anchors, icbc_row_pitch(anchors))
            rows_here = []
            for a, lo, hi in bands:
                inband = [(y0, t) for x0, y0, t in cells if lo <= x0 < hi]
                row = icbc_row_cells(inband, bounds, date_split)
                if not row["date"] or not row["收入/支出金额"]:
                    continue                      # 水印 / 页眉 / 页脚行
                amt = parse_amount(row["收入/支出金额"])
                if amt is None:
                    continue
                rows_here.append({
                    "date": row["date"],
                    "time": row["time"],
                    "amount": abs(amt),
                    "direction": "income" if amt > 0 else "expense",
                    "summary": row["摘要"],
                    "region": row["地区"],
                    "channel": row["渠道"],
                    "account": row["账号"],
                    "currency": row["币种"],
                    "cash_flag": row["钞汇"],
                    "deposit_type": row["储种"],
                    "seq": row["序号"],
                    "balance": parse_amount(row["余额"]),
                })
            page_info.append({"page": i + 1, "rows": rows_here,
                              "footer": _icbc_footer(" ".join(footer_parts))})
            all_rows += rows_here

        # ---- 归一化输出 ----
        for r in all_rows:
            r["occurred_at"] = f'{r["date"]} {r["time"]}'
            # 工行明细没有交易单号；(账号, 时间, 金额, 余额) 在同一账户内唯一
            tail = r["account"][-6:] if r["account"] else ""
            r["ext_id"] = f'{r["occurred_at"]}|{tail}|{r["amount"]:.2f}|'\
                          f'{(r["balance"] if r["balance"] is not None else 0):.2f}'
        card = ""
        try:
            t0 = doc[0].get_text()
            mc = re.search(r"卡号\s*(\d{6,})", t0)
            card = mc.group(1) if mc else ""
        except Exception:
            pass
        meta = {"pages": doc.page_count,
                "card_tail": card[-4:] if card else "",
                "has_identity": bool(icbc_privacy_findings(doc))}
        return {"rows": all_rows, "pages": page_info, "meta": meta}
    finally:
        try:
            doc.close()
        except Exception:
            pass


def icbc_reconcile(extracted: dict) -> dict:
    """工行口径对账：逐页脚注 vs 明细重算，外加余额链（最强的内部不变量）。

    判定标准与微信/支付宝一致：**逐项差额为 0**。
    """
    res = {"per_page": [], "total": {}, "balance_chain": {}}
    ts = ins = 0.0
    n = 0
    for p in extracted["pages"]:
        f = p["footer"] or {}
        inc = round(sum(r["amount"] for r in p["rows"] if r["direction"] == "income"), 2)
        exp = round(sum(r["amount"] for r in p["rows"] if r["direction"] == "expense"), 2)
        res["per_page"].append({
            "page": p["page"], "n": len(p["rows"]),
            "n_official": f.get("count"),
            "income": inc, "income_official": f.get("income"),
            "expense": exp, "expense_official": f.get("expense"),
            "diff_n": None if f.get("count") is None else len(p["rows"]) - f["count"],
            "diff_income": None if f.get("income") is None else round(inc - f["income"], 2),
            "diff_expense": None if f.get("expense") is None else round(exp - f["expense"], 2),
        })
        ts += round(inc, 2)
        ins += round(exp, 2)
        n += len(p["rows"])
    res["total"] = {"n": n, "income": round(ts, 2), "expense": round(ins, 2),
                    "net": round(ts - ins, 2)}
    # 余额链：按时间排序后 余额[i] - 余额[i-1] == ±金额[i]
    rows = sorted(extracted["rows"], key=lambda r: r["occurred_at"])
    bad, checked = [], 0
    prev = None
    for r in rows:
        if r["balance"] is None:
            continue
        if prev is not None:
            delta = round(r["balance"] - prev, 2)
            want = r["amount"] if r["direction"] == "income" else -r["amount"]
            checked += 1
            if abs(delta - want) > 0.005:
                bad.append({"at": r["occurred_at"], "delta": delta, "want": want})
        prev = r["balance"]
    res["balance_chain"] = {"checked": checked, "mismatch": len(bad), "samples": bad[:5]}
    return res


# ============================================================================
# 2. 平台识别 / 表头定位 / preamble 统计
# ============================================================================

PLATFORM_SIGS = [
    ("alipay", {"交易分类", "收/付款方式", "交易订单号"}),
    ("wechat", {"交易类型", "支付方式", "交易单号"}),
]


def detect_pdf_platform(path: str, password: Optional[str] = None) -> str:
    """PDF 只支持工行历史明细这一种；不是则明确报错，不猜。"""
    doc = open_pdf(path, password)
    try:
        return "icbc" if icbc_is_icbc(doc) else "unknown"
    finally:
        doc.close()

def _cells(row) -> list[str]:
    return ["" if c is None else str(c).strip() for c in row]


def find_header(grid) -> int:
    """定位真正的表头行：必须同时含『交易时间』和『金额』。"""
    for i, row in enumerate(grid):
        cells = _cells(row)
        if "交易时间" in cells and any(c.startswith("金额") for c in cells):
            return i
    raise ValueError("未找到表头行")


def detect_platform(header: list[str]) -> str:
    hset = set(header)
    for name, sig in PLATFORM_SIGS:
        if sig <= hset:
            return name
    return "unknown"


SUM_PATTERNS = [
    ("expense", re.compile(r"支出[：:]\s*(\d+)\s*笔\s*([\d,]+\.?\d*)\s*元")),
    ("income",  re.compile(r"收入[：:]\s*(\d+)\s*笔\s*([\d,]+\.?\d*)\s*元")),
    ("neutral", re.compile(r"(?:不计收支|中性交易)[：:]\s*(\d+)\s*笔\s*([\d,]+\.?\d*)\s*元")),
    ("total",   re.compile(r"共\s*(\d+)\s*笔记录")),
]

def parse_official_summary(grid, header_idx: int) -> dict:
    """从 preamble 里读出官方自报的统计，作为对账基准。"""
    out = {}
    for row in grid[:header_idx]:
        line = "".join(_cells(row))
        for key, pat in SUM_PATTERNS:
            m = pat.search(line)
            if m:
                if key == "total":
                    out["total"] = int(m.group(1))
                else:
                    out[key] = (int(m.group(1)), round(float(m.group(2).replace(",", "")), 2))
    return out


# ============================================================================
# 3. 归一化
# ============================================================================

@dataclass
class Txn:
    platform: str
    occurred_at: str
    direction: str            # expense | income | neutral
    amount: float
    counterparty: str
    description: str
    txn_type: str = ""
    category: str = "未分类"
    category_source: str = ""  # platform | kb | rule | none
    confidence: float = 0.0
    status: str = ""
    pay_method: str = ""
    pay_kind: str = "other"    # cash | credit | balance | other
    external_id: str = ""
    merchant_order_id: str = ""
    note: str = ""
    bucket: str = "ledger"     # ledger | neutral_flow | discarded | zero_amount
    refund_paired: str = ""
    needs_review: bool = False
    merchant_id: str = ""

    @property
    def dt(self):
        try:
            return datetime.strptime(self.occurred_at[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None


COL_ALIAS = {
    "alipay": {
        "time": "交易时间", "category_raw": "交易分类", "txn_type": "交易分类",
        "counterparty": "交易对方",
        "description": "商品说明", "direction": "收/支", "amount": "金额",
        "pay_method": "收/付款方式", "status": "交易状态",
        "external_id": "交易订单号", "merchant_order_id": "商家订单号", "note": "备注",
    },
    "wechat": {
        "time": "交易时间", "txn_type": "交易类型", "counterparty": "交易对方",
        "description": "商品", "direction": "收/支", "amount": "金额(元)",
        "pay_method": "支付方式", "status": "当前状态",
        "external_id": "交易单号", "merchant_order_id": "商户单号", "note": "备注",
    },
}

CREDIT_KW = ("花呗", "信用购", "信用卡", "白条", "分期")
BALANCE_KW = ("余额宝", "零钱", "零钱通", "账户余额")
CASH_KW = ("银行", "储蓄卡", "借记卡")


def pay_kind_of(m: str) -> str:
    if any(k in m for k in CREDIT_KW):
        return "credit"
    if any(k in m for k in BALANCE_KW):
        return "balance"
    if any(k in m for k in CASH_KW):
        return "cash"
    return "other"


def _icbc_txn(r: dict) -> Optional[Txn]:
    """工行明细行 -> 统一事件模型。

    工行是**借记账户**，所以支付性质一律 cash：这里是真实的现金流出，
    不存在花呗那类「权责发生制」的问题 —— 工行账本本身就是现金流视角。
    """
    time_s = r.get("time") or "00:00:00"
    occurred = f'{r["date"]} {time_s}'
    if not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", occurred):
        return None
    summary = (r.get("summary") or "").strip()
    region = (r.get("region") or "").strip()
    desc = f"{summary}-{region}" if region else summary
    return Txn(
        platform="icbc",
        occurred_at=occurred,
        direction=r["direction"],
        amount=round(float(r["amount"]), 2),
        counterparty=summary or "工商银行",
        description=desc,
        txn_type="",                       # 工行没有分类字段，只有摘要（形态）
        status=summary,                    # 摘要即状态（消费/还款/转账/退款…）
        pay_method="",                     # 借记账户：无需暴露卡号
        pay_kind="cash",
        external_id=r.get("ext_id", ""),
        note=r.get("channel", ""),
    )


def parse_amount(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 2)
    s = str(v).strip().replace(",", "").replace("¥", "").replace("￥", "")
    if not s or s == "/":
        return None
    try:
        return round(float(s), 2)
    except Exception:
        return None


def normalize(platform: str, grid, header_idx: int) -> list[Txn]:
    if platform == "icbc":
        return [t for t in (_icbc_txn(r) for r in grid["rows"]) if t is not None]
    header = _cells(grid[header_idx])
    alias = COL_ALIAS[platform]
    pos = {k: (header.index(v) if v in header else None) for k, v in alias.items()}
    txns = []
    for row in grid[header_idx + 1:]:
        cells = _cells(row)
        if not any(cells):
            continue
        def get(key):
            i = pos.get(key)
            return cells[i] if i is not None and i < len(cells) else ""
        amt = parse_amount(get("amount"))
        time_s = get("time")
        if amt is None or not re.match(r"\d{4}-\d{2}-\d{2}", time_s):
            continue   # 跳过 footer / 说明行
        raw_dir = get("direction")
        direction = {"支出": "expense", "收入": "income"}.get(raw_dir, "neutral")
        txns.append(Txn(
            platform=platform,
            occurred_at=time_s,
            direction=direction,
            amount=amt,
            txn_type=get("txn_type"),
            counterparty=get("counterparty"),
            description=get("description"),
            status=get("status"),
            pay_method=get("pay_method"),
            pay_kind=pay_kind_of(get("pay_method")),
            external_id=get("external_id"),
            merchant_order_id=get("merchant_order_id"),
            note=get("note"),
            category=get("category_raw") or "未分类",
            category_source="platform" if get("category_raw") else "",
            confidence=0.9 if get("category_raw") else 0.0,
        ))
    return txns


# ============================================================================
# 4. 分流 / 剔除 / 退款配对
# ============================================================================

CLOSED_STATES = ("交易关闭", "已关闭", "已撤销")

# 类目 -> 口径。只有"消费"才真正花掉钱；转账/投资/还款/退款都只是钱换位置。
CLS = {"转账·人际": "transfer", "资金调拨": "transfer",
       "投资": "invest", "投资·收益": "invest",
       "信用·还款": "debt", "退款": "refund", "收入": "income"}


def partition(txns: list[Txn]) -> dict:
    """四向分流：主账本 / 资金流水区 / 剔除 / 零金额。

    判定顺序很重要：
      1. 关闭态、零金额 先剔除
      2. 已经是中性的（微信 `/`、支付宝「不计收支」）进资金流水区
      3. **剩下的按类目判**：转账/投资/还款/**退款** 也不计收支 ——
         否则「理财买入」「信用卡还款」「退款」会被算成当月消费/支出。
         这一步必须在 classify_all 之后跑（见 ingest 流程）。
    """
    ledger, neutral, closed, zero = [], [], [], []
    for t in txns:
        if t.status in CLOSED_STATES:
            t.bucket = "discarded"
            closed.append(t)
        elif t.amount == 0.0:
            t.bucket = "zero_amount"
            zero.append(t)
        elif t.direction == "neutral" or CLS.get(t.category) in ("transfer", "invest", "debt", "refund"):
            t.bucket = "neutral_flow"
            neutral.append(t)
        else:
            t.bucket = "ledger"
            ledger.append(t)
    return {"ledger": ledger, "neutral": neutral, "closed": closed, "zero": zero}


def pair_refunds(all_txns: list[Txn]) -> list[dict]:
    """退款与原始交易配对：金额相同 + 商品说明互为『退款-X』/『X』+ 时间邻近。"""
    refunds = [t for t in all_txns if "退款" in t.status or "退款" in t.description]
    origs = [t for t in all_txns if t not in refunds]
    pairs = []
    used = set()
    for r in refunds:
        base = r.description.replace("退款-", "").replace("退款", "").strip()
        best = None
        for o in origs:
            if id(o) in used or abs(o.amount - r.amount) > 0.005:
                continue
            ob = o.description.replace("退款-", "").strip()
            if base and ob and (base[:6] in ob or ob[:6] in base):
                best = o
                break
        if best:
            used.add(id(best))
            r.refund_paired = best.external_id
            best.refund_paired = r.external_id
            pairs.append({"refund": r, "original": best})
    return pairs


# ============================================================================
# 5. 聚合：同一计划的重复小额
# ============================================================================

PLAN_NOISE = re.compile(r"\d{4}[-.]\d{1,2}[-.]\d{1,2}")

def aggregate_plans(neutral: list[Txn], min_count: int = 5) -> list[dict]:
    """把重复小额折叠：如『<基金平台>-<基金名称>-买入』10.00 x N。"""
    groups = defaultdict(list)
    for t in neutral:
        key = (t.counterparty, PLAN_NOISE.sub("", t.description))
        groups[key].append(t)
    plans = []
    for (cp, desc), items in groups.items():
        if len(items) < min_count:
            continue
        amounts = [i.amount for i in items]
        days = sorted(i.occurred_at[:10] for i in items)
        plans.append({
            "counterparty": cp,
            "plan": desc,
            "count": len(items),
            "total": round(sum(amounts), 2),
            "single": Counter(amounts).most_common(1)[0][0],
            "same_amount_ratio": round(Counter(amounts).most_common(1)[0][1] / len(items), 3),
            "from": days[0], "to": days[-1],
        })
    plans.sort(key=lambda x: -x["total"])
    return plans


# ============================================================================
# 6. 分类
# ============================================================================

ALIPAY_CATEGORY_MAP = {
    "餐饮美食": "餐饮", "交通出行": "交通", "日用百货": "购物·日用",
    "充值缴费": "生活缴费", "投资理财": "投资", "信用借还": "信用·还款",
    "转账红包": "转账·人际", "爱车养车": "汽车", "医疗健康": "医疗",
    "数码电器": "购物·数码", "商业服务": "服务", "生活服务": "生活服务",
    "运动户外": "运动", "家居家装": "家居", "宠物": "宠物",
    "教育培训": "教育", "收入": "收入", "退款": "退款", "其他": "其他",
}

# 商户知识库：关键词 -> (商户ID, 类目, 置信度)
# 微信侧全部依赖它（微信导出无分类）；支付宝侧用于纠错。
#
# 分两层，避免把某个人的账单特征写进代码：
#   1) MERCHANT_KB_GENERIC —— 随技能分发的通用层，只放连锁品牌与公开机构；
#      刻意不含任何地方性机构、基金公司、平台马甲公司名。
#   2) merchant_kb.local.json —— 用户本地的补充层（BILL_WORK/merchant_kb.local.json），
#      {"关键词": ["类目", 置信度]}；把个人账单里出现的机构写这里，不要改进本文件。
MERCHANT_KB_GENERIC = [
    # 交通
    ("老乡鸡", "laoxiangji", "餐饮", 0.98), ("蜜雪冰城", "mixue", "餐饮", 0.98),
    ("好想来", "haoxianglai", "餐饮·零食", 0.95), ("瑞幸", "luckin", "餐饮·咖啡", 0.98),
    ("星巴克", "starbucks", "餐饮·咖啡", 0.98), ("肯德基", "kfc", "餐饮·快餐", 0.98),
    ("麦当劳", "mcdonalds", "餐饮·快餐", 0.98), ("高德打车", "amap_taxi", "交通·网约车", 0.97),
    ("滴滴", "didi", "交通·网约车", 0.97), ("铁路12306", "rail12306", "交通·火车", 0.99),
    # 微信账单里的商户名常是平台马甲公司，字面完全看不出业务。
    # 以下为示例占位符：换成你自己账单里出现过的名字（或写进 merchant_kb.local.json）。
    ("<共享单车马甲公司A>", "bike_share_a", "交通·共享单车", 0.90),
    ("<共享单车马甲公司B>", "bike_share_b", "交通·共享单车", 0.90),
    ("<智慧停车公司>", "parking_company", "交通·停车", 0.92),
    ("<某停车服务商>", "parking_generic", "交通·停车", 0.85),
    ("<城市一卡通>", "city_card", "交通·公交", 0.95),
    ("公共交通一码通", "bus_qr", "交通·公交", 0.95),
    ("luckin", "luckin", "餐饮·咖啡", 0.97), ("瑞幸", "luckin", "餐饮·咖啡", 0.98),
    ("MiniMax", "minimax", "订阅·AI服务", 0.90), ("ChatGPT", "openai", "订阅·AI服务", 0.95),
    ("Gemini", "google", "订阅·AI服务", 0.90), ("喜茶", "heytea", "餐饮·茶饮", 0.97), ("奈雪", "nayuki", "餐饮·茶饮", 0.97),
    ("古茗", "guming", "餐饮·茶饮", 0.95), ("茶百道", "chabaidao", "餐饮·茶饮", 0.95),
    # 购物 / 生活
    ("拼多多", "pdd", "购物·电商", 0.97), ("小红书", "xhs", "购物·电商", 0.90),
    # 基金销售平台：换成你自己在用的平台名，否则会落入待确认队列
    ("<基金销售平台>", "fund_platform", "投资", 0.99),
    ("余额宝", "yuebao", "投资·收益", 0.99), ("花呗", "huabei", "信用·还款", 0.99),
    ("信用购", "xinyonggou", "信用·还款", 0.99),
    # 医疗类：不要写具体医院名（会暴露常住地与就医记录）；用「医院」兜底规则即可
    # 订阅 / 公益
    ("钉钉", "dingtalk", "订阅·办公", 0.95),
    ("慈善基金会", "charity", "公益捐赠", 0.95), ("红十字会", "redcross", "公益捐赠", 0.95),
]

# 本地补充层（默认空文件，放在工作目录，不进版本库）
LOCAL_KB_FILE = "merchant_kb.local.json"


def _load_local_kb() -> list:
    """读取用户本地商户库：{"关键词": ["类目", 置信度], ...}。缺失或损坏时返回空表。"""
    path = os.path.join(WORK, LOCAL_KB_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    out = []
    for kw, v in raw.items():
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            out.append((str(kw), "", str(v[0]), float(v[1])))
        elif isinstance(v, str):
            out.append((str(kw), "", v, 0.90))
    return out


MERCHANT_KB = MERCHANT_KB_GENERIC + _load_local_kb()

def classify_merchant(counterparty: str, description: str) -> tuple[str, str, float]:
    """返回 (类目, 商户ID, 置信度)。命中优先 -> 长关键词优先。"""
    hay = f"{counterparty} {description}"
    hit = None
    for kw, mid, cat, conf in MERCHANT_KB:
        if kw in hay:
            if hit is None or len(kw) > len(hit[0]):
                hit = (kw, mid, cat, conf)
    if hit:
        return hit[2], hit[1], hit[3]
    # 兜底规则
    for kw, cat in (("茶饮", "餐饮·茶饮"), ("奶茶", "餐饮·茶饮"), ("咖啡", "餐饮·咖啡"),
                    ("聚餐", "餐饮"), ("餐饮", "餐饮"), ("餐", "餐饮"),
                    ("打车", "交通·网约车"), ("停车", "交通·停车"), ("单车", "交通·共享单车"),
                    ("基金", "投资"), ("收益", "投资·收益"), ("还款", "信用·还款"),
                    ("医院", "医疗"), ("药房", "医疗"), ("药店", "医疗"),
                    ("超市", "购物·超市"), ("便利店", "购物·便利"), ("零食", "餐饮·零食")):
        if kw in hay:
            return cat, "", 0.6
    return "未分类", "", 0.0


# 微信「交易类型」表达的是形态，可据此直接定类，无需 AI
WECHAT_TRANSFER_TYPES = ("转账", "微信红包", "微信红包（群红包）", "微信红包(群红包)", "群收款")
WECHAT_INVEST_TYPES = ("转入零钱通-来自零钱", "零钱通转出-到零钱", "充值", "提现")

# 工行「摘要」同样是形态字段（不是类目），但语义明确，可直接定类。
#
# ⚠️ 两个关键判断（都会显著改变"这个月花了多少"）：
#   1. 「还款」＝ 还自己的信用卡 → 和花呗还款一样**不计收支**。
#      当成消费会把负债归还算成当月支出，所有月份的数字都被抬高。
#   2. 「理财」「金融付款」＝ 账户内部的**资金调拨**（买理财/基金、缴费型划扣），
#      钱只是换了位置、没花掉 → 归「资金调拨」，**不计收支**。
#      实测：这两个摘要占全部支出笔数的约 89%，当成消费会把支出严重虚增。
#   3. 「他行汇入」「转账」「跨境汇款」＝ 转账·人际（计入收支，但默认从"消费"剔除）。
ICBC_SUMMARY_MAP = {
    "转账": "转账·人际", "他行汇入": "转账·人际", "跨境汇款": "转账·人际",
    "理财": "资金调拨", "金融付款": "资金调拨", "利息": "投资·收益",
    "还款": "信用·还款", "退款": "退款", "消费": "消费",
    "费": "服务·手续费", "工资": "收入", "绩效": "收入",
}


def _icbc_category(summary: str) -> Optional[tuple]:
    """查工行摘要映射：命中返回 (类目, 置信度)，未命中返回 None（交回知识库）。

    摘要可能是两格拼起来的（「绩效」+「7月工资及5月」），所以先按空格取首段再查，
    否则这类工资/绩效行会掉进待确认队列。
    """
    s = (summary or "").strip()
    if not s:
        return None
    head = s.split()[0]
    if head in ICBC_SUMMARY_MAP:
        return ICBC_SUMMARY_MAP[head], 0.90
    if s in ICBC_SUMMARY_MAP:
        return ICBC_SUMMARY_MAP[s], 0.90
    # 绩效类摘要常带月份前缀（「4月绩效」），按后缀再兜一次
    for kw, cat in (("绩效", "收入"), ("工资", "收入")):
        if head.endswith(kw) or s.endswith(kw):
            return cat, 0.90
    return None


def classify_all(txns: list[Txn]) -> None:
    for t in txns:
        # 工行：先看「摘要」这类形态字段（还款/转账/理财…），命中即定类
        if t.platform == "icbc":
            hit = _icbc_category(t.status)
            if hit:
                t.category, t.confidence, t.category_source = hit[0], hit[1], "rule"
                t.needs_review = False
                continue
            cat, mid, conf = classify_merchant(t.counterparty, t.description)
            t.category, t.merchant_id, t.confidence = cat, mid, conf
            t.category_source = "kb" if conf else "none"
            t.needs_review = conf < 0.7
            continue
        # 微信红包 / 转账 / 群收款：形态即类目
        if t.platform == "wechat" and t.txn_type in WECHAT_TRANSFER_TYPES:
            t.category, t.confidence, t.category_source = "转账·人际", 0.90, "rule"
            t.needs_review = False
            continue
        if t.platform == "wechat" and t.txn_type in WECHAT_INVEST_TYPES:
            t.category, t.confidence, t.category_source = "资金调拨", 0.85, "rule"
            t.needs_review = False
            continue
        # 微信：无平台分类 -> 走知识库
        if t.category_source == "platform" and t.platform == "alipay":
            mapped = ALIPAY_CATEGORY_MAP.get(t.category, "其他")
            kb_cat, mid, conf = classify_merchant(t.counterparty, t.description)
            # 支付宝分类是"高起点"但会错（实测：收钱码收款被归为交通出行）
            if kb_cat != "未分类" and kb_cat.split("·")[0] != mapped.split("·")[0]:
                t.category, t.merchant_id, t.confidence = kb_cat, mid, min(0.85, conf)
                t.category_source = "kb_override"
                t.needs_review = True
            else:
                t.category, t.confidence = mapped, 0.9
                t.merchant_id = mid
        else:
            cat, mid, conf = classify_merchant(t.counterparty, t.description)
            t.category, t.merchant_id, t.confidence = cat, mid, conf
            t.category_source = "kb" if conf else "none"
            t.needs_review = conf < 0.7

# ============================================================================
# 7. 落盘：ledger.csv（下游 insights.py / build_app.py 的输入）
# ============================================================================

LEDGER_COLUMNS = ["平台", "时间", "收支", "金额", "类目", "类目来源", "置信度", "商户",
                  "商品说明", "支付方式", "支付性质", "状态", "交易单号", "需确认", "口径桶"]

# 口径桶：下游（insights / App）必须按它决定"算不算收支"。
# 为什么不能只看「收支」列：微信/支付宝的中性行「收支」是 `/` / `不计收支`，
# 但**银行流水的资金调拨本身就带正负号**（理财买入 -10.00、他行汇入 +500.00），
# 只看符号会把大批内部划转算成消费，把支出虚增一个量级。
BUCKET_LABEL = {"ledger": "计入", "neutral_flow": "不计收支",
                "zero_amount": "零金额", "discarded": "已剔除"}
BUCKET_CODE = {v: k for k, v in BUCKET_LABEL.items()}

# 平台标识 -> ledger.csv 里用的短名（merge 时要能认出来）
PLATFORM_TAG = {"wechat": "wechat", "alipay": "alipay", "icbc": "icbc"}


def write_ledger_csv(txns, path: str) -> str:
    """把归一化后的账目写成 ledger.csv。金额统一两位小数，不改动任何口径。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(LEDGER_COLUMNS)
        for t in sorted(txns, key=lambda x: x.occurred_at, reverse=True):
            w.writerow([t.platform, t.occurred_at, t.direction, f"{t.amount:.2f}", t.category,
                        t.category_source, f"{t.confidence:.2f}", t.counterparty, t.description,
                        t.pay_method, t.pay_kind, t.status, t.external_id,
                        "Y" if t.needs_review else "",
                        BUCKET_LABEL.get(t.bucket, t.bucket)])
    return path


def load_ledger_csv(path: str) -> list[Txn]:
    """把已写出的 ledger.csv 读回 Txn（用于跨平台合并）。

    读回的意义：合并必须发生在**统一事件模型**上 —— 否则跨平台的同一个商户
    没法聚合、退款也没法跨平台配对。老账本缺「交易单号」「口径桶」也能读。
    """
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("时间") or not r.get("金额"):
                continue
            amt = parse_amount(r["金额"])
            if amt is None:
                continue
            bucket = BUCKET_CODE.get((r.get("口径桶") or "").strip())
            if bucket is None:                     # 老账本：按「收支」回退
                bucket = "ledger" if r.get("收支") in ("expense", "income") else "neutral_flow"
            t = Txn(
                platform=(r.get("平台") or "unknown").strip(),
                occurred_at=r["时间"].strip(),
                direction=(r.get("收支") or "neutral").strip(),
                amount=amt,
                counterparty=r.get("商户", ""),
                description=r.get("商品说明", ""),
                category=r.get("类目", "未分类") or "未分类",
                category_source=r.get("类目来源", ""),
                confidence=float(r.get("置信度") or 0),
                status=r.get("状态", ""),
                pay_method=r.get("支付方式", ""),
                pay_kind=r.get("支付性质", "other") or "other",
                external_id=r.get("交易单号", ""),
                bucket=bucket,
                needs_review=(r.get("需确认") or "").strip() == "Y",
            )
            out.append(t)
    return out


def merge_ledgers(paths: list, out_path: str, dedupe: bool = True) -> dict:
    """把多份账本合并成一本。

    合并规则（缺一不可）：
      1. **同一文件重复导入要去重**：有「交易单号」就按 (平台, 交易单号)；
         没有就按 (平台, 时间, 金额, 商户, 商品说明)。
         ⚠️ 跨平台**不能**用金额+时间当唯一键 —— 同一天同金额的两笔不同交易会被误合。
      2. **退款配对在合并后重跑**：退款可能记在另一个平台/另一个文件里。
      3. 合并只合并"事实"，不合并"口径"：各行的「口径桶」原样保留，
         所以第 4 步聚合出来的消费仍然是各平台口径之和，不会被冲掉。
    """
    all_txns, per_file, dropped = [], [], 0
    seen = set()
    for p in paths:
        rows = load_ledger_csv(p)
        kept = 0
        for t in rows:
            if dedupe:
                key = ((t.platform, t.external_id) if t.external_id else
                       (t.platform, t.occurred_at, f"{t.amount:.2f}", t.counterparty, t.description))
                if key in seen:
                    dropped += 1
                    continue
                seen.add(key)
            all_txns.append(t)
            kept += 1
        per_file.append({"file": p, "rows": len(rows), "kept": kept})

    pair_refunds(all_txns)                        # 跨文件重跑退款配对
    write_ledger_csv(all_txns, out_path)

    from collections import Counter
    return {
        "out": out_path,
        "total": len(all_txns),
        "dropped": dropped,
        "per_file": per_file,
        "by_platform": dict(Counter(t.platform for t in all_txns)),
        "by_bucket": dict(Counter(BUCKET_LABEL.get(t.bucket, t.bucket) for t in all_txns)),
        "n_ledger": sum(1 for t in all_txns if t.bucket == "ledger"),
    }