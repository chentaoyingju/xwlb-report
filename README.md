# 📺 新闻联播自动化日报系统

每日 **21:30–23:30（北京时间）多时段自动重试** 由**云端（GitHub Actions）**自动执行：获取《新闻联播》文字稿 →
提取关键信息 → **结合当前形势检索信息、对每条信息计算推演并给出判断** →
按现有格式生成结构化日报 → 推送至 QQ 邮箱。**电脑关机也能准时送达。**

- 执行：GitHub Actions 定时（21:30/22:00/22:30/23:00/23:30 北京多班重试；央视 API 当日条目滞后约 2-4 小时，首班未拿到数据则延迟不发送，末班 23:30 仍缺才发缺失说明；发送成功写 marker 当天自动停止），仓库：`chentaoyingju/xwlb-report`（私有）
- 凭据：全部走 GitHub Secrets（`DEEPSEEK_API_KEY`、`SMTP_SENDER`、`SMTP_AUTHCODE`、`SMTP_RECIPIENT`），**不写入代码/仓库**
- 评分原则：分数仅用于内部分档（🔴/🟡/⚪），**日报文件中不显示任何分数**
- 本机路径状态：DSH 看板定时任务与 Windows 计划任务**已停用**（2026-08-30），避免与云端双发
- 工作目录（开发/手动补跑）：`D:\CTYJ\DeepSeek\Harness\News`

---

## 1. 架构与流程（云端单路径）

```
GitHub Actions 定时触发（北京 21:30/22:00/22:30/23:00/23:30 多班重试）
   │
   ▼ ① 采集（多源交叉核对）
   │   央视官方 API（api.cntv.cn《新闻联播》栏目，当日完整清单，主通道，不依赖搜索引擎）
   │   + Bing site 限定检索（央视官网节目页/财联社/东方财富等，年份锚定、排除地方台）
   ▼ ② 可分析性检查（准入/禁入）→ 内部五维评分分档（🔴🟡⚪，分数不外露）
   ▼ ③ 形势检索（🔴/🟡 关键条目标题 → Bing 检索 1-3 条相关当前信息，含 URL）
   ▼ ④ DeepSeek API 分析：
   │   提取关键信息 → 结合形势检索信息【逐条计算推演】（数字量级/路径/节奏）
   │   →【最终判断】（方向/强度/置信度/风险点）→ 深度分析与规范预测
   ▼ ⑤ 生成 reports/YYYY-MM-DD.md（现有格式不变）
   ▼ ⑥ SMTP 推送至 QQ 邮箱（smtp.qq.com:465，marker 去重）
```

**核心链路**：
```
采集 → 可分析性检查 → 内部分档 → 提取关键信息 → 形势检索 → 逐条计算推演 → 判断
→ 深度分析 + 规范预测 → 生成 reports/YYYY-MM-DD.md → SMTP 推送
```

## 2. 目录结构

```
News/                            # 开发工作区（也是 git 仓库根）
├── README.md                    # 本文档
├── .gitignore                   # 排除 config/smtp_config.json、.credentials.yaml、reports/、logs/
├── .github/workflows/
│   └── daily_report.yml         # GitHub Actions 工作流（21:30-23:30 北京多班重试）
├── cloud/
│   └── 部署指南.md              # 云端部署与安全指南（Secrets、双发处理、排障）
├── config/
│   └── smtp_config.json         # 本地 SMTP 凭据（仅本机手动补跑用，勿提交/外传）
├── scripts/
│   ├── standalone_report.py     # 云端主脚本（采集→形势检索→API分析→生成→推送）
│   └── send_report.py           # SMTP 发信（环境变量优先；marker 去重；dry-run/--force）
├── reports/
│   ├── YYYY-MM-DD.md            # 每日日报（保留历史，不含分数）
│   ├── .sent-YYYY-MM-DD.marker  # 已发送标记（去重用）
│   └── state.json               # 跨日状态（预测/待确认池/数据序列/多空得分，自动维护）
└── logs/
    └── standalone.log           # 运行日志（云端会随 artifact 上传）
```

## 3. 配置说明

- **云端**：凭据 = GitHub Secrets（`DEEPSEEK_API_KEY`、`SMTP_SENDER`、`SMTP_AUTHCODE`、`SMTP_RECIPIENT`），
  由工作流以环境变量注入；`SMTP_HOST=smtp.qq.com / SMTP_PORT=465 / SMTP_SSL=true` 在工作流中固定。
- **本地手动补跑**：`config/smtp_config.json`（smtp.qq.com:465、sender、authcode、recipient）；
  DeepSeek Key 解析顺序 = `--api-key` > 环境变量 `DEEPSEEK_API_KEY` > `C:\Users\CTYJ\.dsh\.credentials.yaml`。

> 🔒 安全：`smtp_config.json` 与 `.credentials.yaml` 含敏感凭据，已被 `.gitignore` 排除，请勿外传/提交公开仓库。

## 4. 手动运行（本机补跑/调试）

```powershell
# 完整流程（生成+推送；--dry-run 不发送；--force 忽略 marker；--skip-retrieval 跳过形势检索）
python scripts/standalone_report.py --date YYYY-MM-DD
python scripts/standalone_report.py --date YYYY-MM-DD --dry-run
# 仅采集调试
python scripts/standalone_report.py --date YYYY-MM-DD --fetch-only
# 单独推送某日日报（marker 去重）
python scripts/send_report.py --date YYYY-MM-DD
```

## 5. 分析规范（standalone_report.py 的 SYSTEM_PROMPT）

- **可分析性检查**：准入（可量化经济影响 / 可追踪行政资源 / 明确产业传导链 / 制度性改革）；
  禁入（纯礼仪外交、形象宣传、未公开军事细节、无产业变量社会新闻、形式化会议程序）→ 程序性信息备忘。
- **内部分档**：五维评分（政策信号 0-30 / 产业影响 0-25 / 量化指标 0-20 / 时效落地 0-15 / 新提法 0-10）；
  🔴 重点关注 / 🟡 一般关注 / ⚪ 常规报道（只列标题）；**分数不外露**。
- **形势检索与推演（🔴必做、🟡简做）**：对每条检索信息做【计算推演】（结合数字估算影响量级、传导路径、时间节奏，
  必须有数字支撑或明确假设），随后给出【判断】（方向/强度/置信度/风险点），并引用检索来源 URL。
- **规范预测四要素**：时间窗口 / 核心条件（若…则…）/ 3 个跟踪指标 / 证伪底线。
- **红线**：禁止"暴涨/龙头"等非理性词汇；禁止从政策直接跳跃到股价；预测必须可证伪；发送结果如实汇报。

## 6. 云端部署状态与操作

- 工作流：`.github/workflows/daily_report.yml`（21:30-23:30 北京多班重试、缺失延迟发送、含手动补跑 date 输入、20 分钟超时、上传日报+日志）。
- 推送更新：在 `D:\CTYJ\DeepSeek\Harness\News` 执行 `git push origin main`。
- 手动验证：Actions → `daily-xinwenlianbo-report` → Run workflow → date 填 `YYYY-MM-DD`（如 `2026-08-29`）。
- 排障：查看运行日志（工作流日志或 artifact 内 `logs/standalone.log`）；凭据问题核对 Secrets 名称。

## 7. 维护建议

- **修改规范**：编辑 `standalone_report.py` 的 `SYSTEM_PROMPT`（主报告）与 `SYSTEM_PROMPT_EXTRA`（增强板块）即可，无外部源稿需同步。
- **执行记录**：2026-08-29 本地全链路试运行通过；2026-08-30 云端手动触发曾出现"凌晨无数据 + Secrets 缺失"，
  已修复（date 输入、Secrets 提示、Bing 中文市场参数/反爬重试、采集诊断、重复函数定义清理）。
- **双发防护**：云端发送后写 marker（仅云端自身）；本机路径（DSH 看板定时、Windows 计划任务）已删除，无需跨端去重。

## 8. 免责声明

本系统输出基于公开政策信息推演，所有预测均附证伪条件（到期未发生即自动失效），不构成直接投资建议；
严禁对未通过「可分析性检查」的内容进行主观臆测。
