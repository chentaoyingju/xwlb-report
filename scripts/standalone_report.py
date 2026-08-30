#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻联播日报 · 独立离线执行脚本（不依赖 DSH 运行时 / Web 服务器）

流程:
    1) HTTP 搜索采集当日《新闻联播》条目（Bing site 限定搜索 + 页面直抓，多源交叉核对）
    2) 调用 DeepSeek API（DEEPSEEK_API_KEY）按规范做边界检查、内部评分分档（分数不外露）、
       深度分析与规范预测，生成不含分数的 Markdown 日报
    3) 落盘 reports/YYYY-MM-DD.md
    4) 通过 send_report.py 的 do_send 推送 SMTP（含 .sent-*.marker 去重，防与 DSH 定时任务双发）

用法:
    python scripts/standalone_report.py --date 2026-08-29
    python scripts/standalone_report.py --date 2026-08-29 --dry-run      # 不发送邮件
    python scripts/standalone_report.py --date 2026-08-29 --fetch-only   # 仅采集，不调 API
    python scripts/standalone_report.py --date 2026-08-29 --force        # 忽略 marker 强制重发

API Key 解析顺序: --api-key 参数 > 环境变量 DEEPSEEK_API_KEY > ~/.dsh/.credentials.yaml
退出码: 0 = 成功/按设计跳过  1 = 发送失败  2 = 采集或生成失败  3 = 配置缺失/非法
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
from send_report import DEFAULT_REPORTS, do_send, load_config, log  # noqa: E402

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEEPSEEK_API = "https://api.deepseek.com/chat/completions"
MODEL_FALLBACK = ["deepseek-chat", "deepseek-v4-flash", "deepseek-reasoner"]
LOG_FILE = ROOT / "logs" / "standalone.log"

# ---------------------------------------------------------------- 基础工具

def write_log(line: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except Exception:  # noqa: BLE001
        pass


def http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as resp:
        return resp.read()


def decode(data: bytes) -> str:
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text


# ---------------------------------------------------------------- 搜索与采集

def bing_search(query: str, n: int = 8) -> list[tuple[str, str, str]]:
    """Bing 搜索 HTML 解析 -> [(url, title, snippet)]。失败/被反爬返回空列表。

    指定中文市场参数（mkt=zh-CN）提高命中；检测反爬页并重试一次。
    """
    base = (
        "https://www.bing.com/search?q=" + urllib.parse.quote(query)
        + "&setlang=zh-hans&mkt=zh-CN&count=" + str(n * 2)
    )
    for attempt in range(2):
        try:
            html_text = decode(http_get(base, timeout=25))
        except Exception as exc:  # noqa: BLE001
            log(f"[采集] Bing 请求失败（第 {attempt + 1} 次）: {exc}")
            time.sleep(2)
            continue
        low = html_text.lower()
        if "b_algo" not in html_text:
            if "captcha" in low or "robot" in low or "verify" in low:
                log(f"[采集] Bing 触发反爬（captcha），第 {attempt + 1} 次放弃")
            else:
                log(f"[采集] Bing 未返回结果块（第 {attempt + 1} 次）")
            time.sleep(2)
            continue
        out: list[tuple[str, str, str]] = []
        for block in re.findall(r'<li class="b_algo".*?</li>', html_text, re.S)[:n]:
            m = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not m:
                continue
            url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            sn = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            snippet = re.sub(r"<[^>]+>", "", sn.group(1)).strip() if sn else ""
            if url.startswith("http"):
                out.append((url, title, snippet))
        if out:
            return out
        time.sleep(2)
    return []


def sogou_search(query: str, n: int = 5) -> list[tuple[str, str, str]]:
    """Sogou 兜底搜索。"""
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
    try:
        html_text = decode(http_get(url, timeout=25))
    except Exception as exc:  # noqa: BLE001
        log(f"[采集] Sogou 请求失败: {exc}")
        return []
    out: list[tuple[str, str, str]] = []
    for block in re.findall(r'<h3[^>]*>.*?</h3>', html_text, re.S)[:n]:
        m = re.search(r'href="([^"]+)"', block)
        t = re.sub(r"<[^>]+>", "", block).strip()
        if m and t:
            out.append((m.group(1), t, ""))
    return out


def retrieve_context(item_titles: list[str], per_item: int = 3, cap: int = 40) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """结合当前形势检索：对关键条目标题做 Bing 检索，收集 1-3 条相关当前信息。

    返回 [(条目标题, [(url, 标题, 摘要), ...]), ...]，供 LLM 逐条计算推演并给出判断。
    """
    out: list[tuple[str, list[tuple[str, str, str]]]] = []
    total = 0
    for title in item_titles:
        if total >= cap:
            break
        q = re.sub(r"[\s【】\[\]（）()：:]+", " ", title).strip()[:40]
        if not q:
            continue
        log(f"[检索] 形势检索：{q[:36]}")
        res = bing_search(q + " 最新", n=per_item + 2)
        if not res:
            res = bing_search(q, n=per_item + 2)
        if res:
            out.append((title, res[:per_item]))
            total += len(res[:per_item])
        time.sleep(0.3)
    log(f"[检索] 形势检索完成：覆盖 {len(out)} 个条目，共 {total} 条信息")
    return out


def extract_items(text: str) -> list[str]:
    """从文本中提取编号条目（如 1.【标题】；2、…；（3）…），按序去重返回。

    防误伤：数字后必须紧跟 、.．)） 分隔符，且下一字符为中文/括号（避免 1.5亿、2026.08.29 等）。
    """
    items: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"(?:^|[；;：:，,\s（(])\s*(\d{1,2})\s*[、.．)）]\s*(?=[\u4e00-\u9fff0-9【\[\（(])([^\n；;]{4,160})"
    )
    for m in pattern.finditer(text):
        body = m.group(2).strip()
        key = re.sub(r"[\s【】\[\]（）()]", "", body)[:40]
        if key and key not in seen and re.search(r"[\u4e00-\u9fff]", body):
            seen.add(key)
            items.append(body)
    return items


def date_pattern(report_date: str, m: int, d: int) -> re.Pattern:
    """仅匹配锚定年份的目标日期（避免命中往年同日旧闻）。"""
    y = report_date[:4]
    return re.compile(
        rf"({y}{m:02d}{d:02d}|{y}-0?{m}-0?{d}|{y}年0?{m}月0?{d}日|/{y}/0?{m}/0?{d}/)"
    )


def has_date_evidence(url: str, title: str, snippet: str, pat: re.Pattern) -> bool:
    return bool(pat.search(url + " " + title + " " + snippet))


def acquire_sina_7x24(report_date: str) -> tuple[str | None, str | None]:
    """新浪 7x24 直播流（zhibo_id=152）检索当日《新闻联播》主要内容，返回 (完整条目文本, wap文章URL)。

    步骤：翻页找到当日《新闻联播》主要内容帖 id → 抓取 wap 文章页完整正文（rich_text 会被截断）。
    找不到返回 (None, None)。
    """
    target_id: int | None = None
    for page in range(1, 13):
        url = (
            "https://zhibo.sina.com.cn/api/zhibo/feed"
            f"?page={page}&page_size=20&zhibo_id=152&tag_id=0&dire=f&dpc=1"
        )
        try:
            j = json.loads(http_get(url, timeout=20).decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            log(f"[采集] 新浪 7x24 第 {page} 页失败: {exc}")
            time.sleep(1)
            continue
        items = ((j.get("result") or {}).get("data") or {}).get("feed") or {}
        lst = items.get("list") or []
        if not lst:
            break
        for it in lst:
            rt = it.get("rich_text") or ""
            created = (it.get("create_time") or "")[:10]
            if created == report_date and "《新闻联播》" in rt and "主要内容" in rt:
                target_id = it.get("id")
                break
        if target_id:
            break
        time.sleep(0.4)
    if not target_id:
        log(
            "[采集] 新浪 7x24 未命中（已翻页 1-12；可能原因：接口不可达 / 当日内容未上线 / "
            "条目已滚出窗口 / 帖子文本不含《新闻联播》主要内容）"
        )
        return None, None

    # 抓取 wap 文章页完整正文（rich_text 常被截断为 ~660 字符）
    wap_url = f"https://wap.cj.sina.cn/pc/7x24/{target_id}"
    for attempt in range(2):
        try:
            text = strip_html(decode(http_get(wap_url, timeout=25)))
        except Exception as exc:  # noqa: BLE001
            log(f"[采集] 新浪 wap 文章页抓取失败（第 {attempt + 1} 次）: {exc}")
            time.sleep(2)
            continue
        idx = text.find("《新闻联播》主要内容")
        if idx < 0:
            idx = text.find("主要内容")
        seg = text[idx : idx + 3000] if idx >= 0 else text
        if len(extract_items(seg)) >= 8:
            log(f"[采集] 新浪 7x24 完整清单（item {target_id}，{len(seg)} 字符）")
            return seg, wap_url
    return None, None


def acquire(report_date: str, date_cn: str) -> tuple[list[tuple[str, list[str]]], list[tuple[str, str, str]], str | None]:
    """采集当日条目。返回 (item_sets 按条数降序, sources, sina_authoritative_text)。"""
    ymd = report_date.replace("-", "")
    y, m, d = (int(x) for x in report_date.split("-"))
    pat = date_pattern(report_date, m, d)

    # 0) 主通道：新浪 7x24 完整清单
    sina_text, sina_url = acquire_sina_7x24(report_date)

    queries = [
        f"site:tv.cctv.com 新闻联播 {ymd}",
        f"新闻联播 {ymd} 要闻",
        f"新闻联播 {date_cn} 主要内容",
        f"新闻联播 {date_cn} 要闻",
        f"新闻联播 {date_cn} 速览",
        f"新闻联播 {date_cn} 要闻22条",
        f"央视新闻联播 {ymd} 主要内容",
        f"site:thepaper.cn 新闻联播 {date_cn}",
        f"site:cls.cn 新闻联播 {ymd}",
        f"site:zhitongcaijing.com 新闻联播 {date_cn}",
        f"site:news.qq.com 新闻联播 速览 {date_cn}",
        f"新闻联播 {ymd} 文字稿",
    ]
    results: list[tuple[str, str, str]] = []
    for q in queries:
        log(f"[采集] Bing 检索: {q}")
        results.extend(bing_search(q))
        if not results:
            results.extend(sogou_search(q))
        time.sleep(0.5)

    seen: set[str] = set()
    candidates: list[tuple[str, str, str]] = []
    if sina_url:
        candidates.append((sina_url, "新浪7x24《新闻联播》主要内容", (sina_text or "")[:120]))
        seen.add(sina_url.split("?")[0])
    local_tv = re.compile(
        r"(北京|上海|天津|重庆|河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|"
        r"河南|湖北|湖南|广东|海南|四川|贵州|云南|陕西|甘肃|青海|内蒙古|广西|西藏|宁夏|新疆|兵团|深圳)新闻联播"
    )
    for u, t, s in results:
        u2 = u.split("?")[0]
        if u2 in seen or local_tv.search(t) or local_tv.search(u):
            continue
        seen.add(u2)
        candidates.append((u, t, s))

    # 日期过滤：优先保留 URL/标题/摘要含目标日期的来源
    dated = [c for c in candidates if has_date_evidence(*c, pat)]
    undated = [c for c in candidates if not has_date_evidence(*c, pat)]
    log(f"[采集] 候选来源 {len(candidates)} 个（命中目标日期 {len(dated)} 个）")
    ordered = (dated + undated)[:14]
    for u, t, s in ordered[:12]:
        log(f"[采集]   {t[:40] or u[:60]} | {u[:90]}")

    # 抓取页面（仅保留含目标日期的页面正文；失败用摘要兜底）
    page_texts: list[tuple[str, str]] = []
    for url, title, snippet in ordered[:12]:
        try:
            text = strip_html(decode(http_get(url, timeout=25)))
        except Exception as exc:  # noqa: BLE001
            log(f"[采集]   抓取失败 {exc} <- {url[:80]}（改用摘要）")
            text = f"（抓取失败，搜索引擎摘要）{snippet}"
        if not pat.search(text) and not has_date_evidence(url, title, snippet, pat):
            continue  # 无日期证据，跳过，避免串日
        page_texts.append((url, text[:4000]))
        log(f"[采集]   采用 {len(text)} 字符 <- {url[:80]}")
        time.sleep(0.4)

    # 条目集：每个来源一份（过滤页脚/版权等噪音）
    boilerplate = re.compile(r"版权|本网|免责|非法使用|联系本网|移除")
    item_sets: list[tuple[str, list[str]]] = []
    if sina_text:
        items = [it for it in extract_items(sina_text) if not boilerplate.search(it)]
        item_sets.append(("新浪7x24官方口径(完整清单)", items))
    for url, text in page_texts:
        items = [it for it in extract_items(text) if not boilerplate.search(it)]
        if len(items) >= 8:
            item_sets.append((url, items))
    # 若页面不理想，用命中日期的摘要补条目
    if len(item_sets) < 2:
        for url, title, snippet in dated[:12]:
            items = [it for it in extract_items(snippet) if not boilerplate.search(it)]
            if len(items) >= 8:
                item_sets.append((f"{url} (摘要)", items))
    item_sets.sort(key=lambda x: -len(x[1]))
    log(f"[采集] 可用条目集 {len(item_sets)} 个（最大 {len(item_sets[0][1]) if item_sets else 0} 条）")
    return item_sets, ordered[:12], sina_text


# ---------------------------------------------------------------- DeepSeek API

def resolve_api_key(cli_key: str | None) -> str | None:
    if cli_key:
        return cli_key
    env = os.environ.get("DEEPSEEK_API_KEY")
    if env:
        return env
    cred = Path.home() / ".dsh" / ".credentials.yaml"
    if cred.is_file():
        m = re.search(r"DEEPSEEK_API_KEY\s*:\s*[\"']?([^\"'\s#]+)", cred.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return None


def chat_completion(api_key: str, model: str, system: str, user: str, max_tokens: int = 8000) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        DEEPSEEK_API,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    if not content:
        finish = data["choices"][0].get("finish_reason")
        raise RuntimeError(f"模型 {model} 返回空内容（finish_reason={finish}，请稍后重试）")
    return content


def llm_report(api_key: str, system: str, user: str, preferred: str) -> str:
    models = [preferred] + [m for m in MODEL_FALLBACK if m != preferred]
    last_err: Exception | None = None
    for model in models:
        for attempt in range(2):
            try:
                log(f"[分析] 调用 DeepSeek API（模型 {model}，第 {attempt + 1} 次）")
                return chat_completion(api_key, model, system, user)
            except urllib.error.HTTPError as exc:
                last_err = exc
                code = exc.code
                body = exc.read().decode("utf-8", "replace")[:200]
                if code == 401:
                    raise RuntimeError(f"API Key 无效（401）: {body}") from exc
                if code == 429:
                    log(f"[分析] 限流(429)，{10 * (attempt + 1)} 秒后重试")
                    time.sleep(10 * (attempt + 1))
                    continue
                if code in (400, 404):
                    log(f"[分析] 模型 {model} 不可用({code}): {body}，尝试下一个模型")
                    break  # 换模型
                log(f"[分析] HTTP {code}: {body}")
                time.sleep(5)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if "空内容" in str(exc) and attempt == 0:
                    # 推理模型把输出预算耗在 reasoning 上：加大 max_tokens 再试一次
                    log("[分析] 推理输出预算耗尽，加大 max_tokens 重试")
                    try:
                        return chat_completion(api_key, model, system, user, max_tokens=16000)
                    except Exception as exc2:  # noqa: BLE001
                        last_err = exc2
                        log(f"[分析] 加大 token 仍失败: {exc2}")
                else:
                    log(f"[分析] 请求异常: {exc}，重试")
                time.sleep(5)
    raise RuntimeError(f"LLM 调用全部失败: {last_err}")


# ---------------------------------------------------------------- 报告生成

SYSTEM_PROMPT = """你是严谨的新闻联播政策分析与推演助手，产出有逻辑、有边界、可证伪的结构化日报。

【红线】
1. 分析任何条目前先做【可分析性检查】：是否涉及"国家可分配财政资源、可执行行政指令、可量化产业数据"？未通过则只做一句话概况，归入日报末尾【程序性信息备忘】，标注"纯信息备案，无分析价值"，禁止深度拆解与臆测。
2. 禁入类：纯礼仪性外交（除非涉重大贸易协定/技术解禁）、领导人形象宣传、未公开军事细节、无产业变量社会新闻、形式化党内会议程序。
3. 禁止"暴涨""龙头""重大利好"等非理性词汇；禁止从政策直接跳跃到股价。
4. 预测必须可证伪（时间窗口/若则条件/3个跟踪指标/证伪底线）。

【分档】内部按五维评分（政策信号0-30/产业影响0-25/量化指标0-20/时效落地0-15/新提法0-10，总分100）确定等级，但【严禁在输出中出现任何分数或分项数值】：🔴重点关注=总评高、🟡一般关注=中、⚪常规报道=低（只列标题）。只输出等级标签，不输出分数。

【内容规范】
- 🔴条目：内容摘要 / 关键信号 / 📊深度分析（政策意图、传导路径：政策→资金→订单→财报、受益画像）/ 🔎形势检索与推演 / 🔮规范预测（时间窗口【短期/中期/长期+明确年月】、核心条件"若…则…"、3个可观测跟踪指标、证伪底线"若X未在Y前发生则预测失效"）。
- 🟡条目：内容摘要 / 💡简析（短期情绪或细分领域压力）/ 检索要点与判断（简短）/ 📌观察哨（出现何种信号则升级）。
- ⚪只列标题。
- 【每个条目只能出现在一个分档中，严禁跨档重复】；同一条新闻去重合并后只归入最高分档。

【形势检索与推演（🔴必做、🟡简做）】
使用用户提供的"形势检索信息"（每条含来源 URL），对**每一条检索信息**做【计算推演】：
结合具体数字估算影响量级、推演传导路径与时间节奏（如"若X增速为Y%，则Z环节订单/价格影响约W%"），
必须有数字支撑或明确假设、可复核；然后给出【判断】：影响方向（利好/中性/承压）、强度（强/中/弱）、
置信度（高/中/低）、需警惕的风险点。推演必须基于检索信息与公开数据，禁止无依据臆测；引用检索来源 URL。

【输出模板】严格按以下结构输出完整 Markdown（不要输出其他内容，不要输出分数）：
# 📺 新闻联播日报 {YYYY年MM月DD日}

## 📊 今日概览
- 总条数：XX条
- 重点关注：XX条
- 一般关注：XX条
- 常规报道：XX条
- 程序性信息备忘：XX条
- 核心产业领域：XXX、XXX

## 🔴 重点关注
### 1. 【新闻标题】
- **内容摘要**：…
- **关键信号**：…
- **📊 深度分析**：政策意图 / 传导路径 / 受益画像
- **🔎 形势检索与推演**：检索信息①（来源URL）→ 计算推演 → 判断；检索信息② → 推演 → 判断
- **🔮 规范预测**：时间窗口 / 核心条件 / 跟踪指标①②③ / 证伪条件

## 🟡 一般关注
### 1. 【新闻标题】
- **内容摘要**：…
- **💡 简析**：…
- **检索要点与判断**：简短要点 → 判断
- **📌 观察哨**：…

## ⚪ 常规报道
- 标题1；标题2；……

## 📎 程序性信息备忘
- 一句话概况：……

## 📎 数据来源与免责声明
- 来源：[URL1]、[URL2]、……（使用用户提供的来源URL）
- 生成时间：YYYY-MM-DD HH:MM
- 重要声明：本报告基于公开政策信息推演，所有预测均附有证伪条件，不构成直接投资建议。
"""


def strip_scores(md: str) -> str:
    """安全兜底：删除任何可能出现的分数行/分项数值；生成时间以实际当前时间为准。"""
    md = re.sub(r"^.*总分[:：].*$", "", md, flags=re.M)
    md = re.sub(r"（总分[≥<]?\s*\d+\s*分?）", "", md)
    md = re.sub(r"^.*\d+\s*分\s*（\s*A\s*[:：].*$", "", md, flags=re.M)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    md = re.sub(r"^-\s*\*?\*?生成时间\*?\*?[:：].*$", f"- 生成时间：{now}", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"


def build_user_message(
    report_date: str,
    date_cn: str,
    item_sets: list,
    sources: list,
    sina_text: str | None,
    retrieval: list | None = None,
) -> str:
    lines = [
        f"请生成 {date_cn}《新闻联播》自动化日报。",
        "",
        "【数据来源】",
    ]
    for i, (u, t, s) in enumerate(sources, 1):
        lines.append(f"{i}. {t or u} | {u}" + (f" | 摘要：{s[:100]}" if s else ""))
    lines.append("")
    if sina_text:
        lines.append("【当日官方口径完整清单（新浪7x24，权威，以此为准）】")
        lines.append(sina_text[:3000])
        lines.append("")
    lines.append("【各来源抓取的编号条目（供你合并核对，可能有重复/缺漏/繁体，请按官方口径整理为简体）】")
    for url, items in item_sets[:3]:
        lines.append(f"--- 来源: {url[:80]}（{len(items)} 条）---")
        lines.append("；".join(items[:40]))
    lines.append("")
    if retrieval:
        lines.append("【形势检索信息（供逐条计算推演与判断；对每一条都要推演并给出判断，引用其URL）】")
        for title, hits in retrieval:
            lines.append(f"◆ 条目：{title[:50]}")
            for j, (u, t, s) in enumerate(hits, 1):
                lines.append(f"   {j}. {t[:60]} | {u}" + (f" | 摘要：{s[:120]}" if s else ""))
        lines.append("")
    lines.append(
        "要求：按系统提示词规范完成可分析性检查、分档（只标🔴🟡⚪不标分数）、"
        "深度分析与规范预测，对🔴/🟡条目结合【形势检索信息】逐条计算推演并给出判断（融入现有板块）；"
        "程序性备忘条目一句话概况；来源必须引用上面给出的 URL；概览总条数需与官方口径一致（含联播快讯子项，通常20-25条）；"
        "生成时间用当前时间。"
    )
    return "\n".join(lines)


def make_missing_report(report_date: str, date_cn: str, sources: list, reason: str) -> str:
    lines = [
        f"# 📺 新闻联播日报 {date_cn}",
        "",
        "## ⚠️ 数据缺失说明",
        f"- 日期：{report_date}",
        f"- 状态：未能采集到当日《新闻联播》文字稿",
        f"- 原因：{reason}",
        f"- 已尝试来源：{'；'.join(u for u, _, _ in sources[:8]) or '无'}",
        "",
        "## 📎 数据来源与免责声明",
        "- 生成时间：" + datetime.now().strftime("%Y-%m-%d %H:%M"),
        "- 重要声明：本报告基于公开政策信息推演，不构成直接投资建议。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    p = argparse.ArgumentParser(description="新闻联播日报 · 独立离线执行")
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    p.add_argument("--config", default=str(ROOT / "config" / "smtp_config.json"), help="SMTP 配置路径")
    p.add_argument("--out", default=None, help="日报输出路径（默认 reports/日期.md）")
    p.add_argument("--dry-run", action="store_true", help="生成日报但不发送邮件")
    p.add_argument("--force", action="store_true", help="忽略已发送 marker 强制重发")
    p.add_argument("--fetch-only", action="store_true", help="仅采集并打印，不调用 API/不写文件/不发信")
    p.add_argument("--api-key", default=None, help="DeepSeek API Key（默认读环境变量或 ~/.dsh/.credentials.yaml）")
    p.add_argument("--model", default="deepseek-chat", help="首选模型（长任务推荐 deepseek-chat）")
    p.add_argument("--skip-retrieval", action="store_true", help="跳过形势检索（调试用）")
    args = p.parse_args()

    report_date = args.date or date.today().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        log(f"[失败] 日期格式非法: {report_date}")
        return 3
    y, m, d = report_date.split("-")
    date_cn = f"{int(y)}年{int(m)}月{int(d)}日"
    write_log(f"=== 开始 {report_date} ===")

    # 0) 去重预检：当日已发送（DSH 定时路径等已生成并推送）则直接退出，避免双发/覆盖
    marker = DEFAULT_REPORTS / f".sent-{report_date}.marker"
    if marker.exists() and not args.force:
        log(f"[跳过] 当日日报已发送（{marker.name} 存在），由其他执行路径完成，本次退出（如需重跑加 --force）。")
        write_log(f"=== 结束 {report_date} skip(marker) ===")
        return 0

    # 1) 采集
    item_sets, sources, sina_text = acquire(report_date, date_cn)
    if args.fetch_only:
        log(f"[采集] 完成，来源 {len(sources)}，条目集 {len(item_sets)}，新浪完整清单={'有' if sina_text else '无'}。fetch-only 模式退出。")
        return 0

    # 2) 组装内容（含形势检索 → 逐条推演 → 判断）
    if item_sets and item_sets[0][1]:
        api_key = resolve_api_key(args.api_key)
        if not api_key:
            log("[失败] 未找到 DEEPSEEK_API_KEY（--api-key / 环境变量 / ~/.dsh/.credentials.yaml）")
            return 3
        retrieval: list = []
        if not args.skip_retrieval:
            titles = [t for t in item_sets[0][1]][:15]
            retrieval = retrieve_context(titles, per_item=3, cap=40)
        user_msg = build_user_message(report_date, date_cn, item_sets, sources, sina_text, retrieval)
        md = llm_report(api_key, SYSTEM_PROMPT, user_msg, args.model)
        md = strip_scores(md)
        log(f"[生成] LLM 日报完成（{len(md)} 字符）")
    else:
        reason = "搜索/抓取未命中任何含条目的来源"
        now_bj = datetime.now().strftime("%Y-%m-%d %H:%M")
        if now_bj[:10] == report_date and int(now_bj[11:13]) < 19:
            reason += (
                f"（当前北京时间 {now_bj[11:16]}，当日节目 19:00 才播出，属正常无数据；"
                "若需补跑请用 --date 指定有数据的日期）"
            )
        elif not sina_text:
            reason += "（新浪 7x24 通道未命中：请检查网络/接口可达性，或该日确无播出）"
        md = make_missing_report(report_date, date_cn, sources, reason)
        log(f"[生成] 数据缺失，生成说明日报（{len(md)} 字符）。原因：{reason}")

    # 3) 落盘
    out_path = Path(args.out) if args.out else (DEFAULT_REPORTS / f"{report_date}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    log(f"[落盘] {out_path}（{len(md) / 1024:.1f} KB）")

    # 4) 推送（去重由 do_send 的 marker 处理）
    cfg = load_config(Path(args.config))
    code = do_send(report_date, cfg, report_path=out_path, dry_run=args.dry_run, force=args.force)
    write_log(f"=== 结束 {report_date} exit={code}（dry_run={args.dry_run}）===")
    return code


if __name__ == "__main__":
    sys.exit(main())
