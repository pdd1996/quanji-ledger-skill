# -*- coding: utf-8 -*-
"""
检查账单 zip 的加密方式 —— 只读，不解密，不上传任何数据。

用途：判断微信/支付宝账单压缩包用的是哪种加密，从而决定技术路线：
  - 未加密          -> 可直接解析，无需密码
  - ZipCrypto 传统加密 -> 存在已知明文攻击弱点，bkcrack 路线值得验证
  - WinZip AES      -> 无已知明文弱点，必须拿到密码（走剪贴板/通知捕获）

用法：
    python 检查账单zip加密方式.py "C:\\path\\to\\账单.zip"
    python 检查账单zip加密方式.py            # 无参数则检查当前目录下所有 zip
"""

import os
import sys
import glob
import zipfile

# 保证在任意终端/管道下输出中文不乱码、不崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CT_NAMES = {
    0: "STORE (未压缩)",
    1: "SHRINK",
    8: "DEFLATE",
    9: "DEFLATE64",
    12: "BZIP2",
    14: "LZMA",
    93: "ZSTD",
    95: "XZ",
    99: "AES (WinZip AE-x) 或其他",
}

# 账单 CSV 可能的固定表头（已知明文候选，用于评估 bkcrack 可行性）
KNOWN_PLAINTEXT_HINTS = [
    b"\xef\xbb\xbf" + "交易时间".encode("utf-8"),
    "交易时间".encode("gbk"),
    "微信支付账单明细".encode("gbk"),
    "支付宝交易记录明细查询".encode("gbk"),
    "支付宝账务明细查询".encode("gbk"),
]

SEP = "-" * 68


def sniff_encoding(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            raw.decode(enc)
            return enc
        except Exception:
            continue
    return "unknown"


def check_one(path: str) -> None:
    print(SEP)
    print(f"文件: {path}")
    size = os.path.getsize(path)
    print(f"大小: {size:,} bytes")

    with open(path, "rb") as f:
        magic = f.read(8)

    if magic[:2] != b"PK":
        print("!! 不是 ZIP 文件（可能平台已改为直接发送 CSV/PDF，或这是 RAR/7z）")
        if magic[:4] == b"Rar!":
            print("   -> 检测到 RAR 格式")
        elif magic[:6] == b"7z\xbc\xaf'\x1c":
            print("   -> 检测到 7z 格式")
        print("   提示：请把解压后的文件或正确的账单压缩包提供给我")
        return

    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        print(f"!! 无法作为 ZIP 解析: {e}")
        return

    verdicts = []
    for info in zf.infolist():
        encrypted = bool(info.flag_bits & 0x1)
        strong_enc = bool(info.flag_bits & 0x40)      # PKWARE 强加密标记
        is_aes = (info.compress_type == 99)
        ct = CT_NAMES.get(info.compress_type, str(info.compress_type))

        print(SEP)
        print(f"条目: {info.filename}")
        print(f"  原始大小  : {info.file_size:,} bytes")
        print(f"  压缩后大小: {info.compress_size:,} bytes")
        print(f"  压缩方式  : {ct}  (compress_type={info.compress_type})")
        print(f"  flag_bits : 0x{info.flag_bits:04X}")
        print(f"  加密      : {'是' if encrypted else '否'}")
        if strong_enc:
            print("  强加密标记: 是 (PKWARE strong encryption, bit 0x40)")

        if not encrypted:
            print("  => 判定: 【未加密】 可直接解析，无需密码 ✅")
            verdicts.append("none")
            # 顺便展示文件头，确认是不是账单 CSV
            try:
                with zf.open(info) as fp:
                    head = fp.read(400)
                enc = sniff_encoding(head)
                print(f"  编码嗅探  : {enc}")
                print("  文件头预览:")
                text = head.decode(enc, errors="replace") if enc != "unknown" else repr(head)
                for line in text.splitlines()[:4]:
                    print("      " + line[:110])
            except Exception as e:
                print(f"  (无法预览内容: {e})")
        elif is_aes or strong_enc:
            print("  => 判定: 【WinZip AES / 强加密】")
            print("     bkcrack 已知明文攻击【不可行】（AES 无此弱点）")
            print("     必须拿到密码 -> 走 剪贴板捕获 / Android通知捕获 / 手动输入")
            verdicts.append("aes")
        else:
            print("  => 判定: 【ZipCrypto 传统加密】")
            print("     bkcrack 已知明文攻击【理论可行】，值得验证")
            print("     前提: 需要 >=12 字节已知明文（账单CSV固定表头通常满足）")
            print("     注意: 若为 DEFLATE 压缩，需先用相同算法复现压缩流")
            verdicts.append("zipcrypto")

        # 额外：探测是否含 AES 扩展字段 (0x9901)
        for extra_hex, extra_desc in ((0x9901, "AES extra field"),):
            try:
                if info.extra and extra_hex in [
                    int.from_bytes(info.extra[i + 2: i + 4], "little")
                    for i in range(0, len(info.extra), 1)
                    if i + 4 <= len(info.extra)
                ]:
                    print(f"  检测到额外字段: {extra_desc} (0x{extra_hex:04X})")
            except Exception:
                pass

    print(SEP)
    if "zipcrypto" in verdicts:
        print(">>> 总结论: ZipCrypto —— 可以尝试「零密码」技术路线，先跑一次 bkcrack 验证")
    elif "aes" in verdicts:
        print(">>> 总结论: AES 强加密 —— 必须拿到密码，重点做「剪贴板/通知捕获」体验")
    elif "none" in verdicts:
        print(">>> 总结论: 未加密 —— 最省事，直接解析即可")
    else:
        print(">>> 总结论: 无法判定")


def main():
    args = sys.argv[1:]
    if args:
        targets = args
    else:
        targets = sorted(glob.glob("*.zip"))
        if not targets:
            print("用法: python 检查账单zip加密方式.py \"账单.zip\"")
            print("（也可把 zip 放在当前目录后直接运行本脚本）")
            return
    for t in targets:
        if os.path.isfile(t):
            check_one(t)
        else:
            print(f"!! 文件不存在: {t}")


if __name__ == "__main__":
    main()