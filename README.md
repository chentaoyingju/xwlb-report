# 📺 新闻联播自动化日报系统

每日自动获取《新闻联播》文字稿，按「政策信号 / 产业影响 / 量化指标 / 时效落地 / 新提法」五维评分**内部排序分档**，
对重点内容做**有逻辑、有边界、可证伪**的深度分析与规范预测，生成结构化 Markdown 日报并推送至指定 QQ 邮箱。

- 双执行路径：DSH 看板定时（在线） + 独立离线脚本（DSH 服务器离线也可执行）
- 评分原则：分数仅用于内部分档（🔴/🟡/⚪），**日报文件中不显示任何分数**
- 工作目录：`D:\CTYJ\DeepSeek\Harness\News`

---

## 1. 架构与流程

```
① DSH 在线路径（主）                   ② 离线路径（兜底，不依赖 DSH）
DSH 看板定时 21:30 触发                 Windows 计划任务 21:35 触发
  └─ agent 会话：web_search 采集            └─ scripts/offline_daily.cmd
      → 边界检查 → 内部分档 → 分析            → scripts/standalone_report.py
      → 生成 reports/YYYY-MM-DD.md              → 新浪7x24 API + Bing 采集
      → SMTP 推送                                → DeepSeek API（DEEPSEEK_API_KEY）分析
                                                  → 生成 reports/YYYY-MM-DD.md
                                                  → SMTP 推送
        └────────────── 去重 ──────────────┘
      reports/.sent-YYYY-MM-DD.marker：任一路径发送成功后写入，另一路径自动跳过，绝不双发
```

**核心链路**（两条路径共用）：
```
采集（多源交叉核对）→ 可分析性检查（准入/禁入）→ 内部五维评分分档（🔴🟡⚪，分数不外露）
→ 深度分析 + 规范预测（时间窗口/若则条件/3跟踪指标/证伪底线）→ 生成 reports/YYYY-MM-DD.md → SMTP 推送
```

## 2. 目录结构

```
News/
├── README.md                     # 本文档
├── config/
│   └── smtp_config.json          # SMTP 凭据（smtp.qq.com:465，含授权码，请勿外传）
├── scripts/
│   ├── send_report.py            # SMTP 发信（含 marker 去重 / dry-run / --force）
│   ├── standalone_report.py      # 独立离线全流程（采集→DeepSeek API→生成→推送）
│   ├── fetch_pages.py            # urllib 页面抓取助手（补充核对用）
│   └── offline_daily.cmd         # Windows 计划任务启动器（每天 21:35）
├── prompts/
│   └── daily_report.md           # 定时任务执行提示词源稿（任务 prompt 的持久化副本）
├── reports/
│   ├── YYYY-MM-DD.md             # 每日日报（保留历史，不含分数）
│   └── .sent-YYYY-MM-DD.marker   # 已发送标记（自动生成，去重用）
└── logs/
    └── scheduler.log             # 离线路径运行日志（自动追加）
```

## 3. 配置说明

`config/smtp_config.json`：`smtp.host/port/ssl`（smtp.qq.com:465）、`sender`（发件 QQ 邮箱）、
`authcode`（SMTP 授权码，16 位，非登录密码）、`recipient`（收件邮箱列表）。

独立离线脚本的 DeepSeek API Key 解析顺序：`--api-key` 参数 → 环境变量 `DEEPSEEK_API_KEY` →
`C:\Users\CTYJ\.dsh\.credentials.yaml`（DSH 本地凭据，无需单独配置）。

> 🔒 安全：`smtp_config.json` 与 `.credentials.yaml` 含敏感凭据，仅限本机使用，请勿外传/提交公开仓库。

## 4. 执行路径说明

### 路径① DSH 看板定时（在线）
看板任务「每日《新闻联播》日报生成与推送」（`t-mtehbl9v-joo4jb`，cron `30 21 * * *`，isolation=none）：
- 每次触发启动全新执行会话，按 `prompts/daily_report.md`（已同步写入任务 prompt）执行；
- 完成后提交 execution_report → 移入 in_review → 由用户在看板验收（done 只能由用户确认）；
- 发送前检查 marker：若离线路径已完成当日推送则跳过，避免双发。

### 路径② 独立离线脚本（服务器离线兜底）
`scripts/standalone_report.py` 完全不依赖 DSH 运行时，由 **Windows 计划任务 `NewsReportDaily-Offline`**
（每天 21:35，注册于 2026-08-29）调用 `offline_daily.cmd` 执行：
- 采集：新浪 7x24 直播流 API 翻页定位当日《新闻联播》主要内容帖 → 抓取 wap 文章页完整清单（主通道，
  当日 19:47 后即可用）；Bing site 限定检索补充（央视官网节目页、新浪 7x24、东方财富、齐鲁网等，
  按年份锚定日期过滤，排除地方台《XX新闻联播》）；
- 分析：调用 DeepSeek API（默认模型 `deepseek-v4-flash`，推理输出预算耗尽时自动加大 `max_tokens`，
  失败按 `deepseek-chat` / `deepseek-reasoner` 降级）；
- 生成：按无分数模板输出日报，`生成时间`由脚本强制取当前时间；
- 推送：复用 `send_report.py`（marker 去重）；采集失败时生成「数据缺失说明」日报并照常推送；
- 当日若已由路径①发送（marker 存在）→ 直接跳过退出，不重复不覆盖。

### 路径③ 云端执行（可选，电脑关机也能准时送达）
把 `scripts/standalone_report.py` 部署到云端定时执行（GitHub Actions 或 腾讯云函数等），
凭据全部走平台加密 Secrets/环境变量（`DEEPSEEK_API_KEY`、`SMTP_SENDER`、`SMTP_AUTHCODE`、`SMTP_RECIPIENT`），
**不写入仓库/代码**（`.gitignore` 已排除本地凭据文件）。详见 `cloud/部署指南.md`。
云端生效后请停用本机两条定时路径（Windows 计划任务 `schtasks /change /tn "NewsReportDaily-Offline" /disable`；
看板任务取消或改手动），避免双发。

### 手动运行
```powershell
# 离线全流程（生成+推送；--dry-run 不发送；--force 忽略 marker）
python scripts/standalone_report.py --date YYYY-MM-DD
python scripts/standalone_report.py --date YYYY-MM-DD --dry-run
# 仅采集调试
python scripts/standalone_report.py --date YYYY-MM-DD --fetch-only
# 单独推送某日日报（marker 去重）
python scripts/send_report.py --date YYYY-MM-DD
```

## 5. 评分与分档（内部规则，分数不外露）

| 维度 | 分值 | 判断标准 |
| :--- | :---: | :--- |
| A 政策信号强度 | 0-30 | 最高领导人出席/定调 25-30；部委发布 15-24；地方常规 0-14 |
| B 产业与经济影响 | 0-25 | AI/量子/生物制造等未来产业 20-25；传统升级 10-19；一般数据 0-9 |
| C 量化指标明确度 | 0-20 | 具体数字目标 15-20；仅定性 5-14；无指标 0-4 |
| D 时效与落地性 | 0-15 | 有时间表（"十五五""2027年前"）10-15；仅方向 0-9 |
| E 新提法溢价 | 0-10 | 首次战略新概念 8-10；已有概念新表述 4-7；常规 0-3 |

分档：🔴 重点关注（总评高，通常仅少数几条）/ 🟡 一般关注（中）/ ⚪ 常规报道（低，只列标题）。
**分数仅用于内部排序与分档，日报 .md 与邮件中均不显示任何分数。**

## 6. 可分析性边界（分析前强制检查）

- **准入**（任一即可分析）：可量化经济影响（投资额/预算/税率/进出口/渗透率）；可追踪行政资源（部委主导/地方试点）；
  明确产业传导链（政策→资金→订单→财报）；制度性改革（准入规则/行业标准/法规修订）。
- **禁入**（仅一句话备案，归入【程序性信息备忘】）：纯礼仪外交、领导人形象宣传、未公开军事细节、
  无产业变量社会新闻、形式化党内会议程序。

## 7. 维护建议

- **修改模板/规则**：编辑 `prompts/daily_report.md` 后，同步用 `taskboard_update` 更新看板任务 prompt；
  独立脚本的系统提示词在 `standalone_report.py` 的 `SYSTEM_PROMPT` 中（两处保持一致）。
- **执行记录**：2026-08-29 已完整试运行（DSH 路径：22 条采集核对 → 分档 → 生成 → 邮件送达；
  离线路径：standalone_report.py 干跑验证采集→API→生成全链路，Windows 计划任务已注册）。
- **机器关机**：本机两条定时路径在关机时都无法发送（计划任务为"仅登录用户运行"，关机错过不补跑）；
  若需要**关机也准时送达**，请部署云端路径③（见 `cloud/部署指南.md`，凭据走平台加密 Secrets）。

## 8. 免责声明

本系统输出基于公开政策信息推演，所有预测均附证伪条件（到期未发生即自动失效），不构成直接投资建议；
严禁对未通过「可分析性检查」的内容进行主观臆测。
