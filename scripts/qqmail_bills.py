# -*- coding: utf-8 -*-
"""
从 QQ 邮箱自动获取微信/支付宝账单附件，并接入既有导入管线。

它省掉的是「打开邮件 → 下载附件 → 找到文件 → 拖进应用」这几步；
它省不掉的是「在 App 内点导出」这一步 —— 那是平台侧的人工动作，没有 API。

用法：
    # 0) 只测连通与登录（不下载任何东西）
    python qqmail_bills.py check --user <你的邮箱> 

    # 1) 列出候选账单邮件（强烈建议先跑这个，确认发件人）
    python qqmail_bills.py list --since 2025-10-01

    # 2) 下载账单文件（附件 / 下载链接都会处理）
    python qqmail_bills.py fetch --since 2025-10-01

    # 3) 解密（口令每次导出都不同，由用户提供，可给多个候选）
    python qqmail_bills.py decrypt --file "...zip" --pwds "111111,222222"

    # 4) 一条龙：下载 -> 解密 -> 解析入库
    python qqmail_bills.py run --since 2025-10-01 --pwds "111111,222222"

    # 5) 工商银行历史明细 PDF（下载/解析）
    python qqmail_bills.py fetchpdf --file "...pdf"
    python qqmail_bills.py parsepdf --file "...pdf" --out 工行明细.csv

凭据来源（按优先级）：
    命令行 --user/--authcode  >  环境变量 QQMAIL_USER / QQMAIL_AUTHCODE  >  work/.qqmail.json

授权码获取：QQ邮箱 → 设置 → 账户 → POP3/IMAP/SMTP服务 → 开启 IMAP/SMTP → 生成授权码（16 位）
"""
from __future__ import annotations
import argparse, datetime, email, getpass, imaplib, json, os, re, sys, zipfile, urllib.parse, urllib.request
from email.header import decode_header, make_header
from email.utils import parseaddr

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HOST = "imap.qq.com"
HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("BILL_WORK") or os.path.join(os.path.expanduser("~"), ".bill-lens")
MAILDIR = os.environ.get("QQMAIL_DIR") or os.path.join(WORK, "mail_bills")
CREDFILE = os.path.join(WORK, ".qqmail.json")

# ── 发件人白名单（实测确认）────────────────────────────────────────
#   支付宝账单    : service@mail.alipay.com
#   微信支付账单  : wechatpay@tencent.com
#   工商银行明细  : webmaster@icbc.com.cn（历史明细 PDF 也是邮件下发的）
# 注意：工行的 webmaster@ 是**综合发信口**，营销、安全提醒也走它。
# 所以白名单只决定"是否候选"，真正取件还要按附件类型过滤（见 harvest / ATT_EXT）。
SENDERS_DEFAULT = {
    "service@mail.alipay.com": "支付宝账单",
    "wechatpay@tencent.com":   "微信支付账单",
    "webmaster@icbc.com.cn":   "工商银行",
    "@icbc.com.cn":            "工商银行",
}
SENDER_FILE = os.path.join(WORK, "mail_senders.json")

# 兜底（可 --no-fallback 关闭）：发件人不在白名单时，要求「主题命中 + 发件域可信」
KW_SUBJECT = ("账单", "交易明细", "对账单", "账单流水", "交易流水",
              "历史明细", "明细", "流水")
KW_TRUSTED_DOMAIN = ("alipay.com", "tencent.com", "wechatpay",
                     "icbc.com.cn", "ccb.com", "abchina.com", "boc.cn", "bankcomm.com")
ATT_EXT = (".zip", ".csv", ".xlsx", ".xls", ".pdf")


def load_senders():
    d = dict(SENDERS_DEFAULT)
    if os.path.exists(SENDER_FILE):
        try:
            d.update(json.load(open(SENDER_FILE, encoding="utf-8")))
        except Exception:
            pass
    return d


def _domain_of(addr: str) -> str:
    """取邮箱域名（小写、去掉 <> 与显示名）。"""
    a = (parseaddr(addr)[1] or addr or "").strip().lower()
    return a.rsplit("@", 1)[-1] if "@" in a else ""


def _domain_matches(dom: str, pat: str) -> bool:
    """域名匹配：必须是**完全相同**或**真子域**。

    ⚠️ 不能用子串判断：`addr.endswith("icbc.com.cn")` 这种写法会把
    `phish@icbc.com.cn.evil.com` 判成命中 —— 域名的层级关系是从右往左的，
    只有 `dom == pat` 或 `dom.endswith("." + pat)` 才是真的属于该域。
    """
    dom = (dom or "").lower().lstrip(".")
    pat = (pat or "").lower().lstrip(".")
    return bool(dom) and bool(pat) and (dom == pat or dom.endswith("." + pat))


def match_reason(h, senders, fallback=True):
    """命中返回原因字符串；未命中返回 None。"""
    addr = (parseaddr(h.get("frm", ""))[1] or "").lower()
    if addr in senders:
        return f"✅ 白名单发件人 → {senders[addr]}"
    dom = _domain_of(h.get("frm", ""))
    for s, tag in senders.items():
        if s.startswith("@") and _domain_matches(dom, s[1:]):
            return f"✅ 白名单域 {s} → {tag}"
    if fallback:
        subj = h.get("subj", "")
        if any(k in subj for k in KW_SUBJECT) and any(
                _domain_matches(dom, d) for d in KW_TRUSTED_DOMAIN):
            return "⚠️ 主题关键词 + 可信域名（待确认，建议加白名单）"
    return None


def is_candidate(h, senders=None, fallback=True):
    return match_reason(h, senders or load_senders(), fallback) is not None


# ---------------------------------------------------------------- 工具
def dec(s) -> str:
    if not s:
        return ""
    try:
        return str(make_header(decode_header(s)))
    except Exception:
        return str(s)


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fix_filename(fn: str) -> str:
    """修复附件名编码。支付宝用的是非标准的 RFC2047(GB18030) 形式，
    微信用的是标准 RFC2231；还有 GBK 被误当 latin-1 的情况。"""
    if not fn:
        return fn
    if "=?" in fn:                                   # RFC2047 encoded-word（支付宝用 GB18030）
        # 注意：相邻 encoded-word 之间必须有空白，否则会被粘成 `?==?` 破坏解析
        for cand in (fn, re.sub(r"\s+", " ", fn).strip()):
            try:
                out = str(make_header(decode_header(cand)))
                if "=?" not in out:
                    return out
            except Exception:
                pass
    try:
        return fn.encode("latin-1").decode("utf-8")
    except Exception:
        try:
            return fn.encode("latin-1").decode("gbk")
        except Exception:
            return fn


def sanitize_filename(fn, fallback="bill.zip"):
    """去掉 Windows 非法字符，避免编码解不出时文件名不可用。"""
    if not fn:
        return fallback
    fn = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", fn).strip(" .")
    return fn[:180] or fallback


def body_urls(msg):
    out = []
    for part in msg.walk():
        if part.get_content_type() in ("text/html", "text/plain"):
            try:
                t = part.get_payload(decode=True).decode("utf-8", errors="replace")
            except Exception:
                continue
            out += re.findall(r'https?://[^\s"\'<>\)\]]+', t)
    return [u.replace("&amp;", "&") for u in out]


def http_get(url, timeout=60):
    """下载一个 URL，返回 (文件名, 字节)。等价于用户点「点击下载」。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://mail.qq.com/", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        cd = r.headers.get("Content-Disposition") or ""
        ct = (r.headers.get("Content-Type") or "").lower()
    fn = None
    mm = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.I)
    if mm:
        fn = urllib.parse.unquote(mm.group(1))
    if not fn:
        fn = "download" + (".zip" if "zip" in ct else "")
    return sanitize_filename(fix_filename(fn)), data


def harvest(msg, follow_links=True):
    """取邮件里的账单文件：优先附件；微信是「点击下载」链接，需要跟进。
    返回 [(文件名, 字节, 来源)]"""
    out = [(fn, data, "附件") for fn, data in attachments_of(msg)]
    if out or not follow_links:
        return out
    for u in body_urls(msg):
        if not any(k in u.lower() for k in ("download", "bill", "file", "attachment")):
            continue
        try:
            fn, data = http_get(u)
        except Exception as e:
            print(f"       ⚠️ 链接下载失败 {type(e).__name__}: {str(e)[:70]}")
            continue
        if data[:2] == b"PK" or fn.lower().endswith(ATT_EXT):
            out.append((fn, data, "下载链接"))
    return out


def creds(args, prompt=True):
    """按 命令行 > 环境变量 > work/.qqmail.json > 交互输入 的顺序取凭据。"""
    user = getattr(args, "user", None) or os.environ.get("QQMAIL_USER")
    code = getattr(args, "authcode", None) or os.environ.get("QQMAIL_AUTHCODE")
    if (not user or not code) and os.path.exists(CREDFILE):
        try:
            c = json.load(open(CREDFILE, encoding="utf-8"))
            user = user or c.get("user")
            code = code or c.get("authcode")
        except Exception:
            pass
    # 交互补全：仅在真正的终端里提示，绝不阻塞管道/脚本执行
    interactive = prompt and bool(getattr(sys.stdin, "isatty", lambda: False)())
    if interactive and not user:
        try:
            user = input("QQ邮箱地址（形如 <你的QQ号>@qq.com）: ").strip()
        except Exception:
            return None, None
    if interactive and user and not code:
        try:
            code = getpass.getpass(f"{user} 的 IMAP 授权码（16 位，输入不回显）: ").strip()
        except Exception:
            pass
    if code and getattr(args, "save_cred", False):
        os.makedirs(WORK, exist_ok=True)
        json.dump({"user": user, "authcode": code}, open(CREDFILE, "w", encoding="utf-8"))
        try:
            os.chmod(CREDFILE, 0o600)
        except Exception:
            pass
        print(f"（凭据已保存到 {CREDFILE}，明文存储，请自行权衡）")
    return user, code


def connect(user, code):
    M = imaplib.IMAP4_SSL(HOST, 993, timeout=40)
    M.login(user, code)
    return M


# ---------------------------------------------------------------- IMAP 操作
def search_uids(M, since, folder="INBOX"):
    M.select(folder, readonly=True)
    typ, data = M.uid("SEARCH", None, f"(SINCE {since})")
    if typ != "OK":
        raise RuntimeError(f"SEARCH 失败: {typ}")
    return data[0].split()


def fetch_headers(M, uids):
    """批量取 FROM/SUBJECT/DATE，返回 [{uid, frm, subj, date}]"""
    out = []
    for i in range(0, len(uids), 40):
        chunk = b",".join(uids[i:i + 40])
        typ, d = M.uid("FETCH", chunk,
                       "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if typ != "OK":
            continue
        for item in d:
            if not isinstance(item, tuple):
                continue
            meta = item[0].decode(errors="replace")
            # 注意：必须解析出真正的 UID；item[0] 开头是「序号」而不是 UID，
            # 拿序号当 UID 去 UID FETCH 会静默取不到任何数据。
            mu = re.search(r"UID\s+(\d+)", meta)
            uid = mu.group(1) if mu else meta.split()[0]
            m = email.message_from_bytes(item[1])
            out.append(dict(uid=uid, frm=dec(m.get("From")),
                            subj=dec(m.get("Subject")), date=m.get("Date", "")))
    return out


# (旧的 is_candidate 已移除 —— 它引用了已废弃的 KW_SENDER，且会覆盖白名单版本)


def fetch_message(M, uid):
    typ, d = M.uid("FETCH", uid, "(RFC822)")
    if typ != "OK":
        return None
    for item in d:
        if isinstance(item, tuple):
            return email.message_from_bytes(item[1])
    return None


def attachments_of(msg):
    """返回 [(文件名, 字节内容)]"""
    out = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fn = fix_filename(part.get_filename())
        data = part.get_payload(decode=True)
        if not data:
            continue
        if fn:
            out.append((sanitize_filename(fn), data))
        elif any(data[:4].startswith(sig) for sig in (b"PK\x03\x04", b"PK\x05\x06")):
            out.append((f"unnamed_{abs(hash(data))%100000}.zip", data))
    return out


# ---------------------------------------------------------------- 解密
def decrypt_zip(path, pwd):
    """本地解密（ZipCrypto / AES / 无密码均可）到同目录同名文件，返回解出的路径。"""
    outs = []
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            data = z.read(info, pwd=pwd.encode() if pwd else None)
            dst = os.path.join(os.path.dirname(path), os.path.basename(info.filename))
            with open(dst, "wb") as f:
                f.write(data)
            outs.append(dst)
    return outs


def try_decrypt(path, pwds):
    """在候选口令里逐个试，返回 (成功口令, 解出文件列表)。"""
    for p in pwds:
        try:
            return p, decrypt_zip(path, p)
        except Exception:
            continue
    return None, []


# ---------------------------------------------------------------- 命令
def cmd_check(a):
    print(f"1) 连接 {HOST}:993 …")
    M = imaplib.IMAP4_SSL(HOST, 993, timeout=30)
    print("   OK  TLS 建立")
    user, code = creds(a)
    if not user or not code:
        print("2) 跳过登录：未提供账号/授权码")
        print("   请设置环境变量 QQMAIL_USER / QQMAIL_AUTHCODE，或用 --user/--authcode")
        return
    try:
        M.login(user, code)
        print(f"2) 登录 OK  {user}")
        typ, d = M.list()
        boxes = [dec(x.decode(errors="replace")) for x in d]
        print(f"   邮箱文件夹 {len(boxes)} 个，示例：")
        for b in boxes[:8]:
            print("     ", b[-60:])
        M.logout()
        print("   ✅ 凭据有效，可以执行 list / fetch / run")
    except imaplib.IMAP4.error as e:
        print("   ❌ 登录失败:", e)
        print("      常见原因：用的是登录密码而不是「授权码」；或未开启 IMAP 服务")


def cmd_list(a):
    user, code = creds(a)
    if not user or not code:
        print("需要账号与授权码（--user/--authcode、环境变量，或直接在终端里输入）"); return
    M = connect(user, code)
    uids = search_uids(M, a.since, a.folder)
    print(f"{a.folder} 自 {a.since} 起共 {len(uids)} 封")
    heads = fetch_headers(M, uids)
    senders = load_senders()
    scored = [(h, match_reason(h, senders, a.fallback)) for h in heads]
    cand = [(h, r) for h, r in scored if r]
    print(f"{a.folder} 共 {len(heads)} 封，命中 {len(cand)} 封（白名单 {len(senders)} 条）\n")
    for h, reason in sorted(cand, key=lambda x: x[0]["date"]):
        print(f"  [{h['uid']}] {reason}")
        print(f"       Date   : {h['date'][:31]}")
        print(f"       From   : {h['frm'][:80]}")
        print(f"       Subject: {h['subj'][:80]}")
        msg = fetch_message(M, h["uid"])
        if msg:
            urls = [u for u in body_urls(msg)
                    if any(k in u.lower() for k in ("download", "bill", "file", "attachment"))]
            if urls:
                print(f"       🔗 下载链接 {len(urls)} 个（无附件，需跟链接取件）")
            for fn, data in attachments_of(msg):
                print(f"       📎 {fn}  ({len(data):,} bytes)")
        print()
    hit = sorted({parseaddr(h["frm"])[1] for h, _ in cand})
    miss = sorted({parseaddr(h["frm"])[1] for h, r in scored if not r})[:12]
    print("命中邮件的发件人：")
    for s in hit:
        print("   ✅", s)
    if miss:
        print("未命中邮件的发件人（抽样，确认没有漏掉账单）：")
        for s in miss:
            print("   ⬜", s)
    M.logout()
    print("\n如需扩充白名单：把域名加进 work/mail_senders.json，例如 {\"@xx.com\": \"某平台\"}")


def cmd_fetch(a):
    user, code = creds(a)
    os.makedirs(MAILDIR, exist_ok=True)
    M = connect(user, code)
    uids = search_uids(M, a.since, a.folder)
    heads = [h for h in fetch_headers(M, uids) if is_candidate(h)]
    n = 0
    for h in heads:
        msg = fetch_message(M, h["uid"])
        if not msg:
            continue
        for fn, data, src in harvest(msg):
            if not fn.lower().endswith(ATT_EXT):
                continue
            dst = os.path.join(MAILDIR, fn)
            if os.path.exists(dst) and os.path.getsize(dst) == len(data):
                print(f"  = {fn}  (已存在)")
                continue
            open(dst, "wb").write(data)
            print(f"  ↓ [{src}] {fn}  ({len(data):,} bytes)")
            n += 1
    M.logout()
    print(f"\n下载 {n} 个附件 -> {MAILDIR}")


def cmd_decrypt(a):
    pwds = [p for p in ([a.pwd] + (a.pwds or "").split(",")) if p]
    path = a.file
    p, outs = try_decrypt(path, pwds)
    if not p:
        print("❌ 所有候选口令都失败")
        return
    print(f"✅ 口令 {p} 解密成功，解出 {len(outs)} 个文件：")
    for o in outs:
        print("   ", o, f"({os.path.getsize(o):,} bytes)")


def ingest_file(path, fn, pwds, pdfpwd=None):
    """把一个账单文件（zip / csv / xlsx / 工行 pdf）解析成可入账交易。

    返回 dict(txns=[...], note=str)。口令错误 / 加密方式不支持时抛异常，由调用方决定是否跳过。
    """
    sys.path.insert(0, HERE)
    from bill_importer import (open_bill, to_grid, find_header, detect_platform,
                               normalize, partition, classify_all, pair_refunds,
                               icbc_extract, PdfPasswordNeeded)

    outs = []
    low = fn.lower()
    if low.endswith(".zip"):
        p, files = try_decrypt(path, pwds)
        if not p:
            raise RuntimeError("口令未知")
        for o in files:
            if o.lower().endswith((".csv", ".xlsx", ".pdf")):
                outs.append((os.path.basename(o), open(o, "rb").read(), o))
    else:
        outs.append((fn, open(path, "rb").read(), path))

    collected, notes = [], []
    for name, raw, src_path in outs:
        if name.lower().endswith(".pdf"):
            ext = icbc_extract(src_path, pdfpwd)      # 口令不对会抛 PdfPasswordNeeded
            plat, hi, kind = "icbc", -1, "pdf"
            grid = ext
        else:
            grid, kind = to_grid(name, raw)
            hi = find_header(grid)
            plat = detect_platform([("" if c is None else str(c).strip()) for c in grid[hi]])
        txns = normalize(plat, grid, hi)
        classify_all(txns)                 # 分类必须先跑：分流要按类目判口径
        parts = partition(txns)
        pair_refunds(txns)
        collected += parts["ledger"] + parts["neutral"]
        notes.append(f"{plat:7s} {kind:5s} 解析 {len(txns):4d} 笔 -> 主账本 {len(parts['ledger'])} 笔"
                     f" · 不计收支 {len(parts['neutral'])} · 关闭剔除 {len(parts['closed'])}"
                     f" · 待确认 {sum(1 for t in parts['ledger'] + parts['neutral'] if t.needs_review)}")
    return {"txns": collected, "note": "\n".join(notes)}


def cmd_run(a):
    """下载 -> 解密 -> 入库"""
    user, code = creds(a, prompt=False)   # run 不主动提示，避免污染管道输出
    pwds = [p for p in ([a.pwd] + (a.pwds or "").split(",")) if p]
    os.makedirs(MAILDIR, exist_ok=True)
    if user and code:
        M = connect(user, code)
        uids = search_uids(M, a.since, a.folder)
        heads = [h for h in fetch_headers(M, uids) if is_candidate(h)]
        print(f"候选账单邮件 {len(heads)} 封")
        for h in heads:
            msg = fetch_message(M, h["uid"])
            if not msg:
                continue
            for fn, data, src in harvest(msg):     # 附件 + 下载链接（微信走链接）
                if fn.lower().endswith(ATT_EXT):
                    dst = os.path.join(MAILDIR, fn)
                    if os.path.exists(dst) and os.path.getsize(dst) == len(data):
                        print(f"  = {fn}  (已存在，跳过)")
                        continue
                    open(dst, "wb").write(data)
                    print(f"  ↓ [{src}] {fn}")
        M.logout()
    else:
        print("（未提供邮箱凭据，跳过错箱，直接用本地已有附件）")

    sys.path.insert(0, HERE)
    from bill_importer import merge_ledgers, write_ledger_csv, PdfPasswordNeeded
    pdfpwd = getattr(a, "pdfpwd", None)
    total, collected = 0, []
    for fn in sorted(os.listdir(MAILDIR)):
        if not fn.lower().endswith((".zip", ".csv", ".xlsx", ".pdf")):
            continue
        path = os.path.join(MAILDIR, fn)
        try:
            res = ingest_file(path, fn, pwds, pdfpwd)
        except PdfPasswordNeeded as e:
            print(f"  ⏭ {fn} —— {e}")
            continue
        except RuntimeError as e:
            print(f"  ⏭ {fn} —— {e}（把它发给我，或告诉我口令）")
            continue
        except Exception as e:
            print(f"  ⏭ {fn} —— 解析失败 {type(e).__name__}: {str(e)[:80]}")
            continue
        print(f"  ✅ {fn}")
        for line in res["note"].splitlines():
            print(f"      {line}")
        collected += res["txns"]
        total += sum(1 for t in res["txns"] if t.bucket == "ledger")
    print(f"\n本次解析可入账 {total} 笔")
    if collected:
        dst = os.path.join(WORK, "ledger.csv")
        # 先落本次结果，再与已有账本按「交易单号」合并 —— 直接覆盖会把上次导入的
        # 其他平台（微信/支付宝/工行）抹掉，重复跑一次还可能翻倍。
        tmp = dst + ".incoming.csv"
        write_ledger_csv(collected, tmp)
        m = merge_ledgers([tmp, dst] if os.path.exists(dst) else [tmp], dst)
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"账本已写出: {dst}  ({m['total']} 行"
              f"{'，去重丢弃 ' + str(m['dropped']) + ' 行' if m['dropped'] else ''})")
        print(f"  平台分布 {m['by_platform']}   口径桶 {m['by_bucket']}")
        print(f"下一步:\n  python insights.py --ledger \"{dst}\"\n  python build_app.py --ledger \"{dst}\"")
    else:
        print("没有可入账的账目（是否口令不对？用 --pwds / --pdfpwd 给出正确口令）")


def cmd_parsepdf(a):
    """离线解析工行历史明细 PDF：出统一账本 CSV + 逐页对账报告。

    对账判定标准与微信/支付宝一致：**逐项差额为 0**。
    """
    sys.path.insert(0, HERE)
    from bill_importer import (icbc_extract, icbc_reconcile, normalize, partition,
                               classify_all, pair_refunds, write_ledger_csv, PdfPasswordNeeded)
    pw = [p for p in ([a.pdfpwd] + (a.pdfpwds or "").split(",")) if p]
    ex = None
    for cand in pw or [None]:
        try:
            ex = icbc_extract(a.file, cand)
            print(f"✅ 打开成功（{'口令 ' + cand if cand else '无口令'}）")
            break
        except PdfPasswordNeeded as e:
            if cand is None:
                print(f"❌ {e}")
                return
            continue
    if ex is None:
        print("❌ 所有候选口令都打不开这份 PDF")
        return

    rc = icbc_reconcile(ex)
    print(f"\n■ 解析结果  共 {rc['total']['n']} 笔（{ex['meta']['pages']} 页）"
          f"  卡号后 4 位 {ex['meta']['card_tail']}")
    print(f"  收入 ¥{rc['total']['income']:.2f}   支出 ¥{rc['total']['expense']:.2f}"
          f"   净额 ¥{rc['total']['net']:+.2f}")
    print("\n■ 逐页对账（工行自报脚注 vs 明细重算，要求差额为 0）")
    bad = 0
    for p in rc["per_page"]:
        flag = "OK" if (p["diff_n"] == 0 and p["diff_income"] == 0 and p["diff_expense"] == 0) else "!!"
        bad += flag == "!!"
        print(f"  p{p['page']:2d} 笔数 {p['n']:3d} vs {p['n_official']} (dN={p['diff_n']})"
              f" | 收入 {p['income']:>10.2f} vs {p['income_official']} (d={p['diff_income']})"
              f" | 支出 {p['expense']:>10.2f} vs {p['expense_official']} (d={p['diff_expense']})  {flag}")
    bc = rc["balance_chain"]
    print(f"\n■ 余额链校验（本行余额 = 上行余额 ± 本行金额）")
    print(f"  校验 {bc['checked']} 条，不一致 {bc['mismatch']} 条")
    for s in bc["samples"]:
        print(f"    !! {s['at']} 实际 {s['delta']:+.2f} 期望 {s['want']:+.2f}")
    print(f"\n对账结论：{'全部差额为 0 ✅' if bad == 0 and bc['mismatch'] == 0 else '存在差异，请勿直接入账 ❌'}")

    txns = normalize("icbc", ex, -1)
    classify_all(txns)                     # 分类必须先跑：分流要按类目判口径
    parts = partition(txns)
    pair_refunds(txns)
    keep = parts["ledger"] + parts["neutral"]
    # 默认写进**统一账本**（与微信/支付宝同一个 ledger.csv），而不是另存一份 ——
    # 否则三个平台各一个文件，永远合不起来。
    out = a.out or os.path.join(WORK, "ledger.csv")
    if a.out:
        write_ledger_csv(keep, out)
        print(f"\n账本已写出: {out}  ({len(keep)} 行"
              f"：主账本 {len(parts['ledger'])} / 不计收支 {len(parts['neutral'])}"
              f" / 零金额 {len(parts['zero'])} / 剔除 {len(parts['closed'])}）")
    else:
        from bill_importer import merge_ledgers
        tmp = out + ".incoming.csv"
        write_ledger_csv(keep, tmp)
        m = merge_ledgers([tmp, out] if os.path.exists(out) else [tmp], out)
        if os.path.exists(tmp):
            os.remove(tmp)
        print(f"\n已并入统一账本: {out}  共 {m['total']} 行"
              f"（本份 {len(keep)} 行"
              f"{'，跨文件去重丢弃 ' + str(m['dropped']) + ' 行' if m['dropped'] else ''}）")
        print(f"  平台分布 {m['by_platform']}   口径桶 {m['by_bucket']}")
        print(f"  本份分流：主账本 {len(parts['ledger'])} / 不计收支 {len(parts['neutral'])}"
              f" / 零金额 {len(parts['zero'])} / 剔除 {len(parts['closed'])}")
        print(f"下一步:\n  python insights.py --ledger \"{out}\"\n  python build_app.py --ledger \"{out}\"")


def cmd_merge(a):
    """把多份账本合并成一本（微信 + 支付宝 + 工行 → 一个 ledger.csv）。

    合并只合并"事实"，不合并"口径"：
      · 按「交易单号」跨文件去重（同一文件重复导入不会翻倍）
      · 退款配对在合并后重跑（退款可能记在另一个平台）
      · 各行的「口径桶」原样保留，所以消费仍然是各平台口径之和
    """
    sys.path.insert(0, HERE)
    from bill_importer import merge_ledgers
    files = [f for f in (a.files or []) if f]
    if a.dir:
        files += [os.path.join(a.dir, f) for f in sorted(os.listdir(a.dir))
                  if f.lower().endswith(".csv") and not f.endswith(".incoming.csv")]
    files = [f for f in files if os.path.exists(f)]
    if not files:
        print("❌ 没有可合并的账本（用 --files 给文件，或 --dir 给目录）")
        return
    out = a.out or os.path.join(WORK, "ledger.csv")
    print(f"合并 {len(files)} 份账本 -> {out}")
    for f in files:
        print(f"   · {f}")
    m = merge_ledgers(files, out)
    print()
    for pf in m["per_file"]:
        print(f"   {os.path.basename(pf['file']):<32s} {pf['rows']:>5d} 行 → 保留 {pf['kept']:>5d}")
    if m["dropped"]:
        print(f"   （跨文件重复丢弃 {m['dropped']} 行）")
    print(f"\n合并结果：{m['total']} 行，其中计入收支 {m['n_ledger']} 行")
    print(f"  平台分布 {m['by_platform']}")
    print(f"  口径桶   {m['by_bucket']}")
    print(f"下一步:\n  python insights.py --ledger \"{out}\"\n  python build_app.py --ledger \"{out}\"")


def cmd_eml(a):
    """离线处理本地 .eml 文件（无需邮箱凭据，也用于自测）"""
    os.makedirs(MAILDIR, exist_ok=True)
    msg = email.message_from_bytes(open(a.eml, "rb").read())
    print("From   :", dec(msg.get("From")))
    print("Subject:", dec(msg.get("Subject")))
    print("Date   :", msg.get("Date"))
    n = 0
    for fn, data, src in harvest(msg):
        dst = os.path.join(MAILDIR, fn)
        open(dst, "wb").write(data)
        print(f"  📎 [{src}] {fn}  ({len(data):,} bytes) -> {dst}")
        n += 1
    print(f"\n提取 {n} 个附件")


def main():
    ap = argparse.ArgumentParser(description="从 QQ 邮箱获取账单并入库")
    ap.add_argument("cmd", choices=["check", "list", "fetch", "decrypt", "run", "eml", "parsepdf", "merge"])
    ap.add_argument("--user"); ap.add_argument("--authcode")
    ap.add_argument("--since", default=(datetime.date.today() - datetime.timedelta(days=400)).isoformat(),
                    help="只处理该日期之后的邮件（默认约 13 个月前）")
    ap.add_argument("--folder", default="INBOX")
    ap.add_argument("--pwd", help="解压口令（6 位数字）")
    ap.add_argument("--pwds", help="多个候选口令，逗号分隔")
    ap.add_argument("--file"); ap.add_argument("--eml")
    ap.add_argument("--pdfpwd", help="工行 PDF 的【打开口令】（导出时自己设的，不是解压口令）")
    ap.add_argument("--pdfpwds", help="多个候选 PDF 打开口令，逗号分隔（parsepdf 用）")
    ap.add_argument("--out", help="输出 CSV 路径（parsepdf 不指定时会并入统一账本）")
    ap.add_argument("--files", nargs="*", help="merge：要合并的账本 CSV 列表")
    ap.add_argument("--dir", help="merge：合并该目录下所有 CSV")
    ap.add_argument("--no-fallback", dest="fallback", action="store_false", default=True,
                    help="只用精确白名单，不用主题关键词兜底")
    ap.add_argument("--save-cred", action="store_true", help="把账号/授权码存到 work/.qqmail.json")
    a = ap.parse_args()
    {"check": cmd_check, "list": cmd_list, "fetch": cmd_fetch,
     "decrypt": cmd_decrypt, "run": cmd_run, "eml": cmd_eml,
     "parsepdf": cmd_parsepdf, "merge": cmd_merge}[a.cmd](a)


if __name__ == "__main__":
    main()