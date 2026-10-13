---
name: quanji-ledger-skill
agent_created: true
description: 解析与分析中国大陆的微信支付 / 支付宝 / 工商银行账单，可从 QQ 邮箱自动取件。当用户提到微信账单、支付宝账单、工行历史明细、银行流水、账单导入、记账、收支分析、花呗、账单洞察、QQ邮箱取账单，或提供账单压缩包 / CSV / XLSX / PDF / .eml 文件时使用。不适用于企业财务、发票报销与报税。
metadata:
  short-description: 全迹记账：微信/支付宝/工行账单导入与洞察
---

# 全迹记账（quanji-ledger-skill）

把微信支付 / 支付宝 / 工商银行的官方账单，变成一本**口径正确**的账和一页洞察。

## 先建立的三个事实（否则会做错方向）

1. **没有个人账单 API。** 微信/支付宝的账单接口只对签约商户开放。唯一官方出口是「导出账单文件」。
2. **导出必须本人手动。** 这一步没有任何自动化空间，不要承诺"完全自动同步"。
3. **口令有两个来源、两种用途，别混：**
   - 微信/支付宝：**解压口令**，每次导出都变，由平台经另一条通道下发
     （微信 → 微信支付公众号；支付宝 → 申请页面）。邮件里**没有**口令。
   - 工商银行：**PDF 打开口令**，导出时由你自己设置。它加密的是文件本身，不是压缩包。

因此本技能的定位是：**让"导出之后的所有事"自动化**，而不是假装能跳过导出。

## 硬边界（违反即事故，不要越线）

- ❌ 不抓包、不模拟登录、不爬虫、不用无障碍服务读取微信/支付宝界面
- ❌ **服务端不接触账单解压口令**；口令只在用户设备上使用
- ❌ 不改动、不删除用户邮件；IMAP 一律 `readonly=True`，只搜白名单发件人
- ❌ **默认不主动穷举口令**（即使技术上可行，见 `references/privacy.md`）
- ❌ 输出中剥离姓名、账户、昵称等身份字段

对外话术用「**账单导入 + 自动取件 + AI 归类**」，**不要**说"打通了微信支付宝"。

## 工作流

### 场景 A：用户已经有账单文件

先判断是「加密压缩包」还是「已解压的 CSV/XLSX」，还是「加密 PDF」：

```bash
# 不确定加密方式时先探测（只读，不修改）
python scripts/check_zip_encryption.py "<文件.zip>"

# 加密包 → 需要用户提供口令；然后解密 + 解析 + 落账本
python scripts/qqmail_bills.py decrypt --file "<文件.zip>" --pwds "<口令1>,<口令2>"

# 已解压的 CSV/XLSX → 直接入库（把文件放进工作目录后）
BILL_WORK=<工作目录> python scripts/qqmail_bills.py run --pwds ""

# 工商银行历史明细 PDF（要的是【打开口令】，不是解压口令）
python scripts/qqmail_bills.py parsepdf --file "<明细.pdf>" --pdfpwd "<打开口令>"
```

> 工行 PDF 的口令建议用环境变量传：`ICBC_PDF_PWD`，避免留在 shell 历史里。

### 场景 B：从邮箱取件（推荐）

```bash
# 0) 测连通 + 登录（授权码，不是邮箱密码）
python scripts/qqmail_bills.py check --user <邮箱>

# 1) 列出候选账单邮件 —— 必做，用来确认白名单没有漏
python scripts/qqmail_bills.py list --since <YYYY-MM-DD>

# 2) 一条龙：下载 → 解密 → 解析入库
python scripts/qqmail_bills.py run --since <YYYY-MM-DD> --pwds "<口令1>,<口令2>"
```

**取件机制三个平台不同，必须都支持：**

| 平台 | 取件方式 |
|---|---|
| 微信支付 | 邮件**没有附件**，只有「点击下载」链接（**7 天过期**） |
| 支付宝 | 邮件**有 `.zip` 附件**（正文也有链接） |
| 工商银行 | 邮件**有 `.pdf` 附件**（历史明细，带打开口令）；App 里也能直接下载 |

> 微信用的是下载链接 —— 只做"下载附件"会拿不到微信账单。
> 链接 7 天过期意味着「用户导出后忘了下载」是真实损失，**及时取件比省点击更重要**。

发件人白名单：`service@mail.alipay.com` / `wechatpay@tencent.com` /
`webmaster@icbc.com.cn`（域 `@icbc.com.cn`）。
`webmaster@` 是工行**综合发信口**（营销、安全提醒也走它），
所以白名单只决定"是否候选"，真正取件还要按附件类型过滤。

### 场景 C：出结果（洞察 / 可视化）

```bash
# 文字版洞察：订阅、定投、异常、退款、双视角对账
python scripts/insights.py --ledger <ledger.csv>

# 单文件可交互 App 原型（5 个 Tab + 内部数据）
python scripts/build_app.py --ledger <ledger.csv> --out <记账App.html>
```

### 合并：三个平台合成一本账（默认行为，不是可选项）

**统一账本只有一个文件**：`<工作目录>/ledger.csv`。
`run`（微信/支付宝）和 `parsepdf`（工行）都默认**并入**它，而不是各写一份。

```bash
# 工行并入统一账本（不带 --out 时）
python scripts/qqmail_bills.py parsepdf --file "<明细.pdf>" --pdfpwd "<口令>"

# 手动合并已有账本
python scripts/qqmail_bills.py merge --files a.csv b.csv --out ledger.csv
python scripts/qqmail_bills.py merge --dir <目录>          # 合并目录下所有 CSV
```

合并规则（**缺一不可**）：

1. **按「交易单号」跨文件去重** —— 同一文件重复导入不会翻倍。
   没有单号时退回 `(平台, 时间, 金额, 商户, 商品说明)`。
   ⚠️ **跨平台绝不能用「金额 + 时间」当唯一键**，同一天同金额的两笔不同交易会被误合。
2. **退款配对在合并后重跑** —— 退款可能记在另一个平台/另一个文件里。
3. **合并只合并"事实"，不合并"口径"** —— 每行的「口径桶」原样保留，
   所以合并后的消费 = 各平台口径之和，不会被内部划转冲掉。

> **为什么必须合并**：工行是借记账户，**永远看不到花呗**。
> 只看工行的双视角对账，「信用消费」恒为 0；合上支付宝之后才能看到
> "这个月花了多少 / 其中多少是借来的"。这是这套账本最有用的一组数。

## 口径规则（本技能的核心价值，务必遵守）

**把每一笔拆成四类**：消费 / 转账·人际 / 投资 / 信用还款。

> 实测：某 3 个月账单里「转账·人际」占总支出 **45%**。不拆分的话，单月"消费"会**虚报 65%**。

**信用消费按权责发生制**：花呗消费计入**发生日**，还款不计收支。
实测信用消费占真实消费 **38.1%** —— 按现金流记账的话，每个月的数字都是错的。

**银行账本没有这个问题，但有自己的坑**：工行是**借记账户**，本身已是现金流视角，
所以每笔一律 `pay_kind=cash`，双视角对账里"信用消费"天然为 0 —— 不要因此以为解析漏了数据。
但工行明细里的「还款」摘要是**还自己的信用卡**，和花呗还款一样**不计收支**；
当成消费会把负债归还算成当月支出。

**不要拿官方 preamble 里的统计算校验真值。** 支付宝的统计口径自相矛盾
（笔数含"交易关闭"、金额不含；基金买入退款在桶内冲减），它自己在说明里也写了
"请以实际交易金额为准"。**以明细为准**。工行的逐页脚注则可以直接当校验基准（实测差额为 0）。

细节见 `references/ledger-model.md` 与 `references/pitfalls.md`。

## 验收标准

解析器改动后，必须能**还原官方口径** —— 这是唯一的正确性保证：

```bash
# 0) 口径与分流回归测试（合成数据，不需要真实账单）
python scripts/test_ledger_rules.py

# 1) 对账校验：还原官方口径，逐项差额必须为 0
#    工行（口令走环境变量，避免进 shell 历史）
set ICBC_PDF_PWD=******  &&  python scripts/verify_reconcile.py --icbc "<明细.pdf>"
#    微信 / 支付宝
python scripts/verify_reconcile.py --zip "<账单.zip>" --pwds "<口令>"
```

| 平台 | 校验项 | 期望 |
|---|---|---|
| 支付宝 | 总笔数 / 支出金额 / 收入 / 不计收支 | 逐项等于按官方口径还原的结果，**差额为 0** |
| 微信 | 支出 / 收入 / 中性交易 | 逐项等于按官方口径还原的结果，**差额为 0** |
| 工商银行 | 逐页 笔数 / 收入 / 支出 ＋ 余额链 | 逐项**差额为 0**，余额链 0 处不一致 |

（**判定标准是"逐项差异为 0"，不是"等于某个历史数字"。**
绝对金额会随账单变化，写死数值会让校验在换账本后静默失效。
还原方法见 `references/ledger-model.md` 第六节。）

## 环境

```bash
pip install openpyxl               # 读微信账单的 XLSX
pip install pymupdf                # 读工商银行的明细 PDF
```

zip 解密只用标准库 `zipfile`（ZipCrypto 够用），**不需要 pyzipper**。
若平台改用 AES 强加密（`compress_type == 99`），标准库会失败 —— 此时明确告知用户
"当前不支持 AES 加密的账单包"，不要假装能解。

路径约定：工作目录默认 `~/.bill-lens`，可用环境变量覆盖
（`BILL_WORK` / `BILL_LEDGER` / `QQMAIL_DIR`）。所有脚本都可用 `--help` 查看参数。

## references 索引（按需读取）

| 文件 | 内容 |
|---|---|
| `references/sources.md` | 三个平台的真实邮件、容器、列结构、值域枚举、发件人白名单；工行 PDF 的几何关系 |
| `references/ledger-model.md` | 统一事件模型、四类口径、分流算法、指标公式、`口径桶` 契约、对账合同 |
| `references/pitfalls.md` | 官方统计口径 4 个坑、口径混淆、花呗、工行 PDF 的 5 个几何坑 |
| `references/privacy.md` | 合规红线、口令处理、商店审查、对外话术 |

## 脚本索引

| 脚本 | 用途 |
|---|---|
| `scripts/bill_importer.py` | 解析引擎：zip/CSV/XLSX/工行 PDF → 统一事件模型 → `ledger.csv` |
| `scripts/qqmail_bills.py` | 邮箱取件；`decrypt` / `run` / **`parsepdf`**（工行明细直接出账本 + 对账） |
| `scripts/insights.py` | 文字版洞察（按 `口径桶` 聚合） |
| `scripts/build_app.py` | 单文件可交互 App（按 `口径桶` 定方向） |
| `scripts/verify_reconcile.py` | **对账校验**：还原官方口径，判定逐项差额为 0 |
| `scripts/test_ledger_rules.py` | **口径回归测试**：用合成数据锁住分流契约 |
| `scripts/check_zip_encryption.py` | 探测 zip 加密方式（ZipCrypto / AES） |