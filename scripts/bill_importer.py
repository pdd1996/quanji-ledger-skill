# -*- coding: utf-8 -*-
"""
账单导入引擎 —— 基于微信 / 支付宝「真实导出文件」实测结构实现。

实测结构（微信/支付宝官方导出；实测年月已隐去）：
  支付宝：ZipCrypto zip -> CSV(GBK) -> 23 行 preamble -> 13 列表头 -> 数百行
  微信  ：ZipCrypto zip -> XLSX       -> 17 行 preamble -> 11 列表头 -> 数十行

设计要点（全部由真实数据驱动）：
  1. 先解密（ZipCrypto，口令由用户提供；口令不落盘）
  2. 识别平台 -> 跳过 preamble -> 定位表头 -> 字段清洗（GBK / strip TAB）
  3. 解析 preamble 里的官方统计，作为【对账校验基准】
  4. 四向分流：(收/支 x 交易状态)
       支出/收入 + 成功  -> 主账本
       交易关闭          -> 剔除（未完成的交易，官方统计也不计）
       不计收支          -> 资金流水区（默认折叠）
       退款成功          -> 与原始交易配对冲销
  5. 聚合：同一计划的重复小额（如每日定投 10.00 x N）折叠成一条
  6. 分类：支付宝用自带「交易分类」做映射；微信无分类 -> 商户知识库 + 待确认
  7. 隐私：preamble 中的姓名/账户/昵称一律不进入输出
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


def to_grid(name: str, raw: bytes):
    """把 CSV / XLSX 统一成二维列表。"""
    low = name.lower()
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
# 2. 平台识别 / 表头定位 / preamble 统计
# ============================================================================

PLATFORM_SIGS = [
    ("alipay", {"交易分类", "收/付款方式", "交易订单号"}),
    ("wechat", {"交易类型", "支付方式", "交易单号"}),
]

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

def partition(txns: list[Txn]) -> dict:
    ledger, neutral, closed, zero = [], [], [], []
    for t in txns:
        if t.status in CLOSED_STATES:
            t.bucket = "discarded"
            closed.append(t)
        elif t.amount == 0.0:
            t.bucket = "zero_amount"
            zero.append(t)
        elif t.direction == "neutral":
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


def classify_all(txns: list[Txn]) -> None:
    for t in txns:
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
                  "商品说明", "支付方式", "支付性质", "状态", "交易单号", "需确认"]


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
                        "Y" if t.needs_review else ""])
    return path