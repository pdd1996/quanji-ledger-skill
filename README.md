# 全迹记账（quanji-ledger-skill）

> 把微信支付 / 支付宝 / 工商银行的官方账单，变成一本**口径正确**的账，和一页能看的洞察。

一个给 AI Agent（DSH / Claude Code 等）用的技能包：处理**账单导出之后的所有事** —— 从 QQ 邮箱自动取件、解密、解析入库，到按真实口径分类、出洞察、生成单文件可视化 App。

## 能做什么

| 场景 | 能力 |
|---|---|
| 已有账单文件 | 探测加密方式 → 解密 → 解析 → 落账本（CSV / XLSX / 加密 zip / **工行加密 PDF** / .eml 都吃） |
| 从邮箱取件 | IMAP 只读搜白名单发件人，自动下载 + 解密 + 入库 |
| 工行明细 | 加密 PDF 直接解析（含重建横排表格、逐页脚注对账、余额链校验） |
| **三平台合并** | 微信 + 支付宝 + 工行合成**一本账**（按交易单号去重，退款跨平台配对，口径不串味） |
| 出结果 | 文字洞察（订阅、定投、异常、退款、双视角对账）+ 单文件可交互 App 原型 |
| 口径还原 | 按官方口径还原笔数与金额，**逐项差异为 0** 才算通过 |

## 先建立的三个事实

1. **没有个人账单 API。** 微信 / 支付宝的账单接口只对签约商户开放，唯一官方出口是「导出账单文件」。
2. **导出必须本人手动。** 这一步没有任何自动化空间，本技能不承诺"完全自动同步"。
3. **口令有两种，别混：** 微信 / 支付宝是**解压口令**，每次导出都变，由平台经另一条通道下发（微信 → 微信支付公众号；支付宝 → 申请页面），邮件里**没有**口令；工商银行是 **PDF 打开口令**，导出时自己设置。

## 快速开始

```bash
pip install openpyxl pymupdf              # 微信 XLSX / 工行 PDF 各需其一

# 不确定加密方式时先探测（只读，不改文件）
python scripts/check_zip_encryption.py "<文件.zip>"

# 加密包 → 提供口令后解密 + 解析 + 落账本
python scripts/qqmail_bills.py decrypt --file "<文件.zip>" --pwds "<口令1>,<口令2>"

# 已解压的 CSV/XLSX → 直接入库
BILL_WORK=<工作目录> python scripts/qqmail_bills.py run --pwds ""

# 工商银行历史明细 PDF（要的是【打开口令】）→ 账本 + 逐页对账
python scripts/qqmail_bills.py parsepdf --file "<明细.pdf>" --pdfpwd "<打开口令>"

# 从邮箱一条龙取件（授权码，不是邮箱密码）
python scripts/qqmail_bills.py check --user <邮箱>
python scripts/qqmail_bills.py list  --since <YYYY-MM-DD>
python scripts/qqmail_bills.py run   --since <YYYY-MM-DD> --pwds "<口令>"

# 三个平台合成一本账（run / parsepdf 默认就已合并进 ledger.csv）
python scripts/qqmail_bills.py merge --files a.csv b.csv --out ledger.csv
python scripts/qqmail_bills.py merge --dir <目录>

# 洞察 / 可视化
python scripts/insights.py --ledger <ledger.csv>
python scripts/build_app.py --ledger <ledger.csv> --out <记账App.html>

# 回归测试与对账校验
python scripts/test_ledger_rules.py
python scripts/verify_reconcile.py --icbc "<明细.pdf>"      # 口令走 ICBC_PDF_PWD 环境变量
python scripts/verify_reconcile.py --zip "<账单.zip>" --pwds "<口令>"
```

所有脚本都支持 `--help`。路径约定：工作目录默认 `~/.bill-lens`，可用 `BILL_WORK` / `BILL_LEDGER` / `QQMAIL_DIR` 覆盖。

**取件机制两个平台不同，必须都支持：**

| 平台 | 取件方式 |
|---|---|
| 微信支付 | 邮件**没有附件**，只有「点击下载」链接，**7 天过期** |
| 支付宝 | 邮件**有 `.zip` 附件**，正文也有链接 |
| 工商银行 | 不走邮箱：用户在 App / 网银直接拿到加密 PDF |

## 口径规则（本技能的核心价值）

把每一笔拆成四类：**消费 / 转账·人际 / 投资·信用还款**。
不拆分的话，单月"消费"会严重虚报（转账与信用还款被算进消费）。

**信用消费按权责发生制**：花呗消费计入**发生日**，还款不计收支 —— 按现金流记账，每个月的数字都是错的。
（银行账本不存在账期，工行一律 `pay_kind=cash`，其"信用消费"为 0 是**正确结果**。）

**银行流水最容易踩的坑**：工行的「理财」「金融付款」是**账户内部资金调拨**，占支出笔数的大多数，
但它们**带正负号**（理财买入 `-10.00`），只看符号会把支出虚增约 **15 倍**。
所以账本多输出一列 **`口径桶`**，下游一律按它聚合。

**不要拿官方 preamble 里的统计算校验真值**（它自己的口径就自相矛盾，说明里也写了"请以实际交易金额为准"）。
**以明细为准**；工行的逐页脚注则干净可用。验收判定标准是"逐项差异为 0"，不是"等于某个历史数字"。

**统一账本只有一个文件**：`<工作目录>/ledger.csv`。三个平台的入口都**并入**它，不各写一份。
合并只合并"事实"、不合并"口径"：按交易单号去重、退款跨平台重配，每行的「口径桶」原样保留。
这一步不能省 —— 工行是借记账户，**永远看不到花呗**，只有合起来才知道"花了多少、其中多少是借来的"。

细节见 [`references/ledger-model.md`](references/ledger-model.md) 与 [`references/pitfalls.md`](references/pitfalls.md)。

## 硬边界

- ❌ 不抓包、不模拟登录、不爬虫、不用无障碍服务读取微信 / 支付宝界面
- ❌ **服务端不接触账单解压口令**；口令只在用户设备上使用
- ❌ 不改动、不删除用户邮件；IMAP 一律 `readonly=True`，只搜白名单发件人
- ❌ **默认不主动穷举口令**（见 [`references/privacy.md`](references/privacy.md)）
- ❌ 输出中剥离姓名、账户、昵称等身份字段

## 目录结构

```
SKILL.md                     技能定义与工作流（Agent 入口）
REDACTION.md                 脱敏与修正说明 —— 分享前请读
assets/app_src/              单文件 App 原型源码（app.js / style.css / shell.html）
references/sources.md        三个平台的邮件、容器、列结构、值域；工行 PDF 几何关系
references/ledger-model.md   统一事件模型、四类口径、分流算法、口径桶契约、对账合同
references/pitfalls.md       官方统计口径的坑、银行内部划转、花呗、技术坑
references/privacy.md        合规红线、口令处理、商店审查、对外话术
scripts/qqmail_bills.py      IMAP 取件 + 解密 + 入库 + parsepdf（主入口）
scripts/bill_importer.py     解析微信 / 支付宝 / 工行 PDF → 统一账本
scripts/insights.py          文字洞察：订阅、定投、异常、退款、对账
scripts/build_app.py         账本 → 单文件可交互 App
scripts/verify_reconcile.py  对账校验：还原官方口径，判定逐项差额为 0
scripts/test_ledger_rules.py 口径回归测试（合成数据，防分流口径回归）
scripts/check_zip_encryption.py  压缩包加密方式探测（只读）
```

## 安装到 DSH

放到用户级技能根目录即可（所有会话可用；只给单个项目用则放 `<项目>/.dsh/skills/`）：

```
~/.dsh/skills/quanji-ledger-skill/
```

DSH 扫描深度为 1，必须是 `<根>/<技能名>/SKILL.md` 这个形状，且**目录名要和 `SKILL.md` 里 frontmatter 的 `name:` 完全一致**，否则不加载。

## 依赖

- Python 3：`openpyxl`（读微信 XLSX）、`pymupdf`（读工行 PDF；`import pymupdf` / `import fitz` 两种名都认）
- zip 解密只用标准库 `zipfile`（ZipCrypto 够用），**不需要 pyzipper**
- 若平台改用 AES 强加密（`compress_type == 99`），标准库会失败 —— 此时明确告知用户"不支持 AES 加密的账单包"，不要假装能解
- 工行 PDF 是**打开口令**加密（`permissions == 0`），没有"空口令"路径；拿不到口令只能重新导出

## 隐私

本仓库是**脱敏副本**：真实商户名、金额、年月、硬编码口令均已移除，改为占位符与自洽虚构值。
自己账单里的机构名请写进本地补充层 `merchant_kb.local.json`（已在 `.gitignore` 中），**不要改回代码**。

工行 PDF 里含**户名与完整卡号**：解析器只把它们留在内存里，
输出（账本 CSV / 洞察 JSON / 单文件 App）**只保留卡号后 4 位**，不含户名、不含完整卡号、不含账号。
内部的 `交易单号` 列用「账号后 6 位 + 时间 + 金额 + 余额」做唯一键，属于弱标识但用于对账去重；
如需彻底去除，自行改写 `icbc_extract` 里的 `ext_id`。**PDF 打开口令同样不落盘。**

`build_app.py` 生成的单文件 HTML 会**内联完整账本**，分享前请确认对象。

详见 [`REDACTION.md`](REDACTION.md)。
