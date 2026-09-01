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


def http_get(url: str, timeout: int = 25, headers: dict | None = None) -> bytes:
    hdrs = {"User-Agent": UA, "Accept": "text/html,*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
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
            html_text = decode(http_get(base, timeout=15))
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
        html_text = decode(http_get(url, timeout=10))
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


def ddg_search(query: str, n: int = 6) -> list[tuple[str, str, str]]:
    """DuckDuckGo HTML 兜底搜索（对 GitHub 海外运行器较友好）。"""
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        text = decode(http_get(url, timeout=10))
    except Exception as exc:  # noqa: BLE001
        log(f"[采集] DDG 请求失败: {exc}")
        return []
    out: list[tuple[str, str, str]] = []
    for m in re.finditer(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        u = m.group(1).strip()
        if u.startswith("//duckduckgo.com/l/?uddg="):
            u = urllib.parse.unquote(u.split("uddg=")[1].split("&")[0])
        t = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if u.startswith("http") and t:
            out.append((u, t, ""))
        if len(out) >= n:
            break
    return out


# ---- 可信来源白名单（形势检索与来源净化共用；按用户要求不含新浪）----
TRUSTED_HOSTS = re.compile(
    r"(tv\.cctv|api\.cntv|cctv\.com|cls\.cn|eastmoney|nbd\.com\.cn|stcn\.com|thepaper|"
    r"21jingji|yicai\.com|jiemian|zhitongcaijing|10jqka|wallstreetcn|cfi\.net\.cn|"
    r"gov\.cn|xinhuanet|people\.com\.cn|qstheory|iqilu|news\.qq\.com|sohu|央视官方)"
)
BLOCKED_HOSTS = re.compile(
    r"(baike\.baidu|zhihu|jingyan\.baidu|bilibili|hanyuguoxue|chagushici|zhidao|"
    r"douyin|youku|iqiyi|tiktok|reddit|bbc\.com|zhongwen/simp|xinjiangtrip|map\.baidu|"
    r"skillhub|osta\.org|youth\.cn)"
)


def is_trusted(url: str, title: str = "", snippet: str = "") -> bool:
    """白名单判定：命中黑名单直接拒绝；命中可信站点返回 True（宁可没有，不要垃圾）。"""
    u = (url or "").lower()
    if BLOCKED_HOSTS.search(u):
        return False
    return bool(TRUSTED_HOSTS.search(u))


def build_search_queries(title: str) -> list[str]:
    """条目标题 → 关键词化检索查询：去系列前缀/括号/虚词/时间词，保留核心名词短语。"""
    t = re.sub(r"【[^】]*】", "", title)
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    for junk in (
        "我国", "中国", "全国", "国家", "中央", "本市", "今年", "上半年", "下半年",
        "前7个月", "前七个月", "前7月", "到2030年", "将", "正在", "推出", "加快",
        "持续", "推进", "推动", "落实", "强化", "进一步", "力争", "预计", "有望",
        "首次", "突破", "达到", "超过", "实现", "助力", "满足", "健全", "完善",
        "提升", "加大", "正式", "全面",
    ):
        t = t.replace(junk, "")
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z%]+", "", t)
    if not t:  # 全被虚词/时间词滤掉时退回标题原文
        t = re.sub(r"[^\u4e00-\u9fff0-9a-zA-Z]+", "", title)
    if len(t) > 24:
        t = t[:24]
    year = str(datetime.now().year)
    return [f"{t} 最新", f"{t} 政策 {year}"]


def retrieve_context(item_titles: list[str], per_item: int = 3, cap: int = 40) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """结合当前形势检索：关键词查询 + 可信来源白名单过滤，收集 1-3 条相关当前信息。

    仅保留可信站点结果；某条目无可信信息则跳过（不硬凑）。返回 [(条目标题, [(url,标题,摘要),...]), ...]。
    """
    out: list[tuple[str, list[tuple[str, str, str]]]] = []
    total = 0
    for title in item_titles:
        if total >= cap:
            break
        hits: list[tuple[str, str, str]] = []
        for q in build_search_queries(title):
            res = bing_search(q, n=per_item + 3)
            if not any(is_trusted(u, t, s) for u, t, s in res):
                d = ddg_search(q, n=per_item + 3)
                if d:
                    res = d
            hits.extend(r for r in res if is_trusted(*r))
            if len(hits) >= per_item:
                break
            time.sleep(0.3)
        seen_u: set[str] = set()
        kept: list[tuple[str, str, str]] = []
        for r in hits:
            if r[0] not in seen_u:
                seen_u.add(r[0])
                kept.append(r)
        kept = kept[:per_item]
        if kept:
            out.append((title, kept))
            total += len(kept)
            log(f"[检索] {title[:24]} → {len(kept)} 条可信信息")
        else:
            log(f"[检索] {title[:24]} → 无可信来源信息（已过滤/检索失败）")
        time.sleep(0.3)
    log(f"[检索] 形势检索完成：覆盖 {len(out)} 个条目，共 {total} 条可信信息")
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


CCTV_COLUMN_API = (
    "https://api.cntv.cn/NewVideo/getVideoListByColumn"
    "?id=TOPC1451528971114112&n=30&sort=desc&p=1&mode=0&serviceId=tvcctv"
)


def acquire_cctv_api(report_date: str) -> tuple[str | None, str | None]:
    """央视官方《新闻联播》栏目 API：返回 (完整条目文本, 节目页URL)。

    权威、简体、不依赖搜索引擎、全球可访问；brief 字段即"本期节目主要内容"完整编号列表。
    """
    ymd = report_date.replace("-", "")
    last_exc: str = ""
    for attempt in range(2):
        try:
            j = json.loads(
                http_get(CCTV_COLUMN_API, timeout=20, headers={"Referer": "https://tv.cctv.com/lm/xwlb/"}).decode(
                    "utf-8", "replace"
                )
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = f"{type(exc).__name__}: {exc}"
            log(f"[采集] 央视 API 请求失败（第 {attempt + 1} 次）: {exc}")
            continue
        lst = ((j.get("data") or {}).get("list")) or []
        cands = [it for it in lst if ymd in (it.get("title") or "")]
        if cands:
            it = next((x for x in cands if "19:00" in x.get("title", "")), cands[0])
            brief = (it.get("brief") or "").strip()
            url = it.get("url") or ""
            log(f"[采集] 央视 API 命中：{it.get('title')}（brief {len(brief)} 字符）")
            return brief, url
        log(f"[采集] 央视 API 未找到 {report_date} 节目（返回 {len(lst)} 条）")
        return None, None
    log(f"[采集] 央视 API 两次尝试均失败: {last_exc}")
    return None, None


def acquire(report_date: str, date_cn: str) -> tuple[list[tuple[str, list[str]]], list[tuple[str, str, str]], str | None]:
    """采集当日条目。返回 (item_sets 按条数降序, sources)。"""
    ymd = report_date.replace("-", "")
    y, m, d = (int(x) for x in report_date.split("-"))
    pat = date_pattern(report_date, m, d)

    # 0) 主通道：央视官方 API（权威、不依赖搜索引擎）
    cctv_text, cctv_url = acquire_cctv_api(report_date)

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
    ddg_disabled = False
    if not cctv_text:  # 已拿到央视官方完整清单时，跳过搜索引擎扫描（云端 Bing 常被降级）
        for q in queries:
            log(f"[采集] Bing 检索: {q}")
            res = bing_search(q)
            # Bing 返回门户首页等垃圾结果时，换 DDG 兜底（失败一次后本批不再重试，避免超时堆叠）
            if not any(("新闻联播" in (t + s)) or has_date_evidence(u, t, s, pat) for u, t, s in res):
                if ddg_disabled:
                    log("[采集]   Bing 结果非目标内容（DDG 先前失败，跳过）")
                else:
                    log("[采集]   Bing 结果非目标内容，尝试 DDG")
                    d = ddg_search(q)
                    if d:
                        res = d
                    else:
                        ddg_disabled = True
            results.extend(res)
            if not res:
                log("[采集]   尝试 Sogou")
                results.extend(sogou_search(q))
            time.sleep(0.5)
    else:
        log("[采集] 已获央视官方完整清单，跳过搜索引擎扫描")

    seen: set[str] = set()
    candidates: list[tuple[str, str, str]] = []
    if cctv_url:
        candidates.append((cctv_url, "央视官方《新闻联播》节目页", (cctv_text or "")[:120]))
        seen.add(cctv_url.split("?")[0])
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

    # 日期过滤：优先保留 URL/标题/摘要含目标日期的来源；
    # 若无日期命中，放宽到"标题/摘要含『新闻联播』主题词"的候选（避免门户首页垃圾）
    dated = [c for c in candidates if has_date_evidence(*c, pat)]
    if len(dated) < 2:
        kw = re.compile(r"新闻联播")
        extra = [c for c in candidates if c not in dated and kw.search(c[1] + " " + c[2])]
        dated = (dated + extra)[:12]
        log(f"[采集] 候选来源 {len(candidates)} 个（日期命中 {sum(1 for c in candidates if has_date_evidence(*c, pat))}，含主题词放宽 {len(extra)}）")
    else:
        log(f"[采集] 候选来源 {len(candidates)} 个（命中目标日期 {len(dated)} 个）")
    undated = [c for c in candidates if c not in dated]
    ordered = (dated + undated)[:14]
    for u, t, s in ordered[:12]:
        log(f"[采集]   {t[:40] or u[:60]} | {u[:90]}")

    # 抓取页面（仅保留含目标日期的页面正文；失败用摘要兜底；超时收紧避免挂起）
    page_texts: list[tuple[str, str]] = []
    for url, title, snippet in ordered[:8]:
        try:
            text = strip_html(decode(http_get(url, timeout=12)))
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
    if cctv_text:
        items = [it for it in extract_items(cctv_text) if not boilerplate.search(it)]
        item_sets.append(("央视官方口径(完整清单)", items))
    for url, text in page_texts:
        items = [it for it in extract_items(text) if not boilerplate.search(it)]
        if len(items) >= 8:
            item_sets.append((url, items))
    # 可信来源过滤：仅保留央视/财联社/东财/澎湃/齐鲁/智通/腾讯/搜狐/政府网等来源的条目集，
    # 避免 Bing 垃圾结果（BBC/Reddit/门户首页）生成假日报
    item_sets = [s for s in item_sets if is_trusted(s[0])]
    # 若页面不理想，用命中日期的摘要补条目（同样仅限可信来源）
    if len(item_sets) < 2:
        for url, title, snippet in dated[:12]:
            items = [it for it in extract_items(snippet) if not boilerplate.search(it)]
            if len(items) >= 8:
                item_sets.append((f"{url} (摘要)", items))
    item_sets.sort(key=lambda x: -len(x[1]))
    log(f"[采集] 可用条目集 {len(item_sets)} 个（最大 {len(item_sets[0][1]) if item_sets else 0} 条）")
    sources = [s for s in ordered[:12] if is_trusted(s[0])]
    return item_sets, sources


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

SYSTEM_PROMPT = """你是严谨的新闻联播政策分析与推演助手，产出有逻辑、有边界、可证伪、有预期差对照的结构化日报。

【红线】
1. 分析任何条目前先做【可分析性检查】：是否涉及"国家可分配财政资源、可执行行政指令、可量化产业数据"？未通过则只做一句话概况，归入日报末尾【程序性信息备忘】，标注"纯信息备案，无分析价值"，禁止深度拆解与臆测。
2. 禁入类：纯礼仪性外交（除非涉重大贸易协定/技术解禁）、领导人形象宣传、未公开军事细节、无产业变量社会新闻、形式化党内会议程序。
3. 禁止"暴涨""龙头""重大利好"等非理性词汇；禁止从政策直接跳跃到股价。
4. 预测必须可证伪（时间窗口/若则条件/3个跟踪指标/证伪底线）。
5. 【分数不外露】内部按五维评分定档，但输出严禁出现任何分数或分项数值。

【统计口径字典（速览表必须标注口径）】
- PMI：官方制造业PMI（季调后）；社零：社会消费品零售总额（含税，注明口径范围）；固投：固定资产投资（不含农户）；CPI/PPI：同比；进出口：海关口径（美元计价）。
- 数据口径不明时注明"（口径待核）"；推算值标注"（推算）"。

【官方用词热力词典】
确保(90) / 切实加快(80) / 大力推动(70) / 推动(60) / 关注(40) / 提及(30)。
- 🔴条目的"关键信号"须给出用词强度，如"用词强度：确保(90)，最高动员级"；
- 单条核心用词权重<50 的条目倾向归入"程序性/应付式"（在备忘中注明"（用词强度低，程序性倾向）"）。

【内容规范】
- 🔴条目：内容摘要 / 关键信号（含用词强度）/ 📈数据与预期（本条量化数据：边际增量=当月/季新增 vs 上期、三年复合增速2026vs2023【基数不足标"（基数不足）"】、市场一致预期【检索到给出并注来源，否则写"无共识预期"】）/ 估值与持仓约束（受益板块PE/PB近5年分位、公募超配、北向趋势——尽力检索，获取不到写"（数据源受限）"；分位>80%提示"利好边际效应递减"）/ 📊深度分析（政策意图、传导路径、受益画像）/ 🔎形势检索与推演 / 反身性拐点检查（警示：若该利好已被市场充分认知且相关板块近一月涨幅>15%，公布日可能为利好出尽拐点）/ 悲观情景压力测试（证伪条件后补：若证伪，则对以下X条预测产生二阶冲击：…）/ 🔮规范预测。
- 🟡条目：内容摘要 / 💡简析 / 检索要点与判断 / 📌观察哨。
- ⚪只列标题。
- 【每个条目只能出现在一个分档中，严禁跨档重复】。
- 【来源纪律】数据来源仅引用用户提供的可信URL；检索无效时明确标注，严禁编造来源或混入无关站点。

【舆情温度解读（🔴首条或官媒重点必做）】
逆向分析：发布时点（月末/周末/重大会议前）、版面排位（头条/非头条）、是否配发评论员文章；
判断为"预期管理"（稳定市场）或"动员令"（倒逼地方执行），一句话结论附在该条目"关键信号"后。

【形势检索与推演（🔴必做、🟡简做）】
使用用户提供的"形势检索信息"（可信来源，含URL）：对**每一条**做【计算推演】（结合数字估算量级/路径/节奏，可复核），
再给【判断】（方向/强度/置信度/风险点）；引用URL。未提供或标注"无有效检索信息"时写
"未获取有效检索信息，推演基于公开常识并明确标注假设"，严禁硬凑。

【贝叶斯联动更新】
用户会提供【历史状态】；若其中显示相关预测已证伪（如PMI≤49.5），则本条相关预测（如以旧换新全年销售额）按规则自动调整
（上调15-20%），并在预测中注明"联动调整（因PMI证伪）：…"；反之（PMI验证）则下调并注明。

【输出模板】（只输出主报告，不含【连续追踪板/数据口径与预期/政策博弈/市场流动性/待确认追踪】等增强板块——由第二段生成）
# 📺 新闻联播日报 {YYYY年MM月DD日}

## 📊 今日概览
- 总条数：XX条
- 重点关注：XX条
- 一般关注：XX条
- 常规报道：XX条
- 程序性信息备忘：XX条
- 核心产业领域：XXX、XXX
- 多空因子净得分：+X（偏多/偏空/对冲震荡）【明细见⚖️板块】

## 📋 今日关键数据速览
| 指标 | 数值 | 单位/口径 | 边际环比变动% | 三年复合增速 | 市场一致预期 | 出处条目 |
|---|---|---|---|---|---|---|
| … | … | … | … | … | … | … |
（5-8行；从当日条目提取最关键的量化指标；边际增量=当月/季新增vs上期；推算标"（推算）"）
- 口径说明：PMI=官方季调；社零=含税；……

## 🔴 重点关注
### 1. 【新闻标题】
- **内容摘要**：…
- **关键信号**：（含用词强度；舆情温度解读）
- **📈 数据与预期**：边际增量 / 三年CAGR / 市场一致预期
- **估值与持仓约束**：…
- **📊 深度分析**：政策意图 / 传导路径 / 受益画像
- **🔎 形势检索与推演**：检索信息①（URL）→ 推演 → 判断；……
- **反身性拐点检查**：…
- **悲观情景压力测试**：…
- **🔮 规范预测**：时间窗口 / 核心条件（若…则…）/ 跟踪指标①②③ / 证伪条件

## 🟡 一般关注
### 1. 【新闻标题】
- **内容摘要**：…
- **💡 简析**：…
- **检索要点与判断**：简短要点 → 判断
- **📌 观察哨**：…

## ⚪ 常规报道
- 标题1；标题2；……

## 📎 程序性信息备忘
- 一句话概况：……（"会谈/访问/签署/启动"类标注"（待成果确认）"）

## 👀 下一步关注
- 汇总各条观察哨 + 全局 2-3 个最值得跟踪的节点（时间+事件+影响）
- 财报披露窗口期：每月下旬标注"当前处于财报披露窗口期"及业绩兑现度约束（若已知）
- 非线性突变预警线：若以旧换新月均增速连续2个月下滑>5%，则触发"政策疲劳"情景、重设全年预期框架（作为规则说明）

## 📎 数据来源与免责声明
- 来源：[URL1]、[URL2]、……（仅引用用户提供的可信来源URL，严禁编造或混入无关站点）
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
    retrieval: list | None = None,
    state_text: str = "",
) -> str:
    lines = [
        f"请生成 {date_cn}《新闻联播》自动化日报。",
        "",
        "【数据来源】",
    ]
    for i, (u, t, s) in enumerate(sources, 1):
        lines.append(f"{i}. {t or u} | {u}" + (f" | 摘要：{s[:100]}" if s else ""))
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
    else:
        lines.append("【形势检索信息】无（网络/搜索引擎受限，未能获取）；如需推演，请基于公开常识并明确标注假设。")
        lines.append("")
    if state_text:
        lines.append("【历史状态（跨日）】")
        lines.append(state_text)
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

# ================================================================ 跨日状态与增强板块

STATE_FILE = DEFAULT_REPORTS / "state.json"


def load_state() -> dict:
    if STATE_FILE.is_file():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {
        "version": 2,
        "lastUpdate": None,
        "predictions": [],       # [{date,title,cond,falsify,window}]
        "pending": [],           # [{date,item,deadline,status}]
        "dataSeries": {},        # {指标: [{"date","value"}]}
        "multifactor": {},       # {date, score, direction}
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log(f"[状态] 保存失败: {exc}")


def state_summary_text(state: dict) -> str:
    """把跨日状态整理为给 LLM 的上下文文本（历史预测/数据序列/待确认池/多空得分）。"""
    lines = [f"（最近更新：{state.get('lastUpdate') or '无'}）"]
    preds = state.get("predictions") or []
    if preds:
        lines.append("【历史预测（近14天，供贝叶斯联动/连续追踪）】")
        for p in preds[-14:]:
            lines.append(f"- [{p.get('date')}] {p.get('title','')[:22]} | 证伪:{p.get('falsify','')[:40]}")
    ds = state.get("dataSeries") or {}
    if ds:
        lines.append("【核心数据序列（近3周，供连续追踪板/边际对比）】")
        for k, pts in list(ds.items())[:8]:
            vals = "；".join(f"{p.get('date')}={p.get('value')}" for p in pts[-3:])
            lines.append(f"- {k}: {vals}")
    pend = state.get("pending") or []
    if pend:
        lines.append("【待确认事项（跨日追踪池）】")
        for p in pend[-8:]:
            lines.append(f"- [{p.get('date')}] {p.get('item','')[:28]}（状态：{p.get('status','待确认')}）")
    mf = state.get("multifactor") or {}
    if mf:
        lines.append(f"【上日多空因子】净得分 {mf.get('score')}，方向 {mf.get('direction')}")
    return "\n".join(lines)


MARKET_QUERIES = [
    "VIX 恐慌指数 最新", "美债10年期收益率 最新", "离岸人民币 CNH 汇率 今日",
    "富时中国A50期指 夜盘", "韩国KOSPI 收盘 今日", "地方专项债 发行进度 2026",
    "波罗的海干散货指数 BDI 最新", "全国港口 集装箱吞吐量 最新",
]


def retrieve_market_context(per_query: int = 3, cap: int = 20) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """检索市场/宏观数据（VIX/美债/CNH/A50/KOSPI/专项债/BDI/吞吐量），白名单过滤。"""
    out: list[tuple[str, list[tuple[str, str, str]]]] = []
    total = 0
    for q in MARKET_QUERIES:
        if total >= cap:
            break
        res = bing_search(q, n=per_query + 2)
        if not any(is_trusted(u, t, s) for u, t, s in res):
            d = ddg_search(q, n=per_query + 2)
            if d:
                res = d
        hits = [r for r in res if is_trusted(*r)][:per_query]
        if hits:
            out.append((q, hits))
            total += len(hits)
        time.sleep(0.3)
    log(f"[检索] 市场/宏观检索完成：{len(out)} 组，{total} 条可信信息")
    return out


SYSTEM_PROMPT_EXTRA = """你是新闻联播日报的"增强分析模块"。给定当日主报告、跨日历史状态与市场/宏观检索信息，
生成以下 5 个增强板块（每个以标题开头，板块间空行分隔；不要输出其他内容；不要输出任何分数）：

## 📋 连续追踪板
- 列出 PMI、以旧换新销售额、发电耗煤、社零等核心指标的近3次数据轨迹（用历史状态+检索信息；缺失标注"—"）；
- 一句话判断各指标"加速/减速/持平"。

## 📈 数据口径与预期
- **口径字典**：PMI=官方季调；社零=含税；固投=不含农户；CPI/PPI=同比；进出口=海关美元计价（固定口径说明，含"口径待核"项）；
- **市场一致预期对照**：对当日关键数据给出 Wind/彭博/路透 调查中值（检索到则标注来源；否则"无共识预期"）；
- **高频微观交叉验证**：从当日联播快讯提取 BDI/集装箱运价/港口吞吐量/钢材价格等高频词，作为核心预测的辅助验证；
- **配套资金到位率**：结合专项债发行进度/财政存款估算中央+地方补贴实际拨付比例（数据不足写"待月末数据"）。

## ⚖️ 政策博弈与风险
- **政策矛盾矩阵**：若同日存在"内生修复（如PMI改善）"与"外生托底（强刺激延续）"并存，强制分析"刺激退坡概率"并给权重判断；否则写"无显著矛盾"；
- **多空因子净得分**：将当日主要利好/利空因子按影响力赋分（±1~5），列出明细并汇总净得分，判断当日风险偏好方向（偏多/偏空/对冲震荡）；
- **官方用词热力汇总**：列出当日用词强度最高的 3 条及权重；
- **悲观情景连锁**：若任一🔴预测证伪，列出对哪些预测产生二阶冲击及大致下修幅度；
- **贝叶斯联动更新**：基于历史状态中的证伪信息，说明本次对哪些预测做了联动调整。

## 📌 待确认事项追踪
- 列出历史"待成果确认"事项（会谈/访问/签署/启动）的最新状态：超过24小时未确认 → 标注"无实质进展（逾期未确认）"；
- 新增当日"会谈/访问/签署/启动"类事项入池，标注"待成果确认（+24h检查）"。

## 🌐 市场与流动性（隔夜观测）
- **宏观流动性风险评级**：依据美日国债利差、离岸CNH隐含波动率、VIX 给出"宽松/中性/紧缩"三档评级，并说明对当日利好结论的折扣系数（紧缩档×0.7）；
- **隔夜观测窗口**：列出次日开盘前需检查的 5 大指标：A50期指（涨跌幅）、离岸CNH（点位）、美债10Y收益率、原油/黄金波动率、韩国KOSPI开盘（亚太情绪风向标）。
"""


def assemble_report(md_main: str, extra: str) -> str:
    """把增强板块按标题插回主报告的固定位置。"""
    boards: dict[str, str] = {}
    for h in (
        "## 📋 连续追踪板",
        "## 📈 数据口径与预期",
        "## ⚖️ 政策博弈与风险",
        "## 📌 待确认事项追踪",
        "## 🌐 市场与流动性",
    ):
        idx = extra.find(h)
        if idx >= 0:
            nxt = extra.find("\n## ", idx + len(h))
            boards[h] = extra[idx : nxt if nxt >= 0 else len(extra)].rstrip()

    def insert_before(md: str, anchor: str, block: str) -> str:
        if not block:
            return md
        i = md.find(anchor)
        return md[:i] + block + "\n\n" + md[i:] if i >= 0 else md

    md = insert_before(md_main, "## 📋 今日关键数据速览", boards.get("## 📋 连续追踪板", ""))
    md = insert_before(
        md, "## 🔴 重点关注",
        "\n\n".join(x for x in (boards.get("## 📈 数据口径与预期", ""), boards.get("## ⚖️ 政策博弈与风险", "")) if x),
    )
    md = insert_before(md, "## 👀 下一步关注", boards.get("## 📌 待确认事项追踪", ""))
    md = insert_before(md, "## 📎 数据来源与免责声明", boards.get("## 🌐 市场与流动性", ""))
    return md


def extract_and_update_state(md: str, report_date: str, state: dict) -> None:
    """从生成的主报告中抽取预测/待确认/数据序列/多空得分，合并进跨日状态。"""
    preds = state.setdefault("predictions", [])
    pend = state.setdefault("pending", [])
    ds = state.setdefault("dataSeries", {})

    for b in re.split(r"\n### ", "\n" + md)[1:]:
        title = b.split("\n", 1)[0].strip("【】 \t")
        cond = re.search(r"核心条件[:：]\s*(.+)", b)
        fals = re.search(r"证伪条件[:：]\s*(.+)", b)
        win = re.search(r"时间窗口[:：]\s*(.+)", b)
        if fals or cond:
            preds.append({
                "date": report_date,
                "title": title[:50],
                "cond": (cond.group(1).strip()[:80] if cond else ""),
                "falsify": (fals.group(1).strip()[:120] if fals else ""),
                "window": (win.group(1).strip()[:40] if win else ""),
            })
    preds[:] = preds[-40:]

    mi = md.find("## 📎 程序性信息备忘")
    if mi >= 0:
        seg = md[mi:md.find("\n## ", mi + 3)]
        for m in re.finditer(r"([^：\n]{2,40}(?:会谈|访问|签署|启动)[^：\n]*)", seg):
            item = m.group(1).strip().strip("：:")
            if item and not any(p.get("item") == item and p.get("date") == report_date for p in pend):
                pend.append({"date": report_date, "item": item[:60], "deadline": "+24h", "status": "待确认"})
    today = datetime.strptime(report_date, "%Y-%m-%d").date()
    for p in pend:
        if p.get("status") == "待确认" and p.get("date"):
            try:
                d0 = datetime.strptime(p["date"], "%Y-%m-%d").date()
                if (today - d0).days >= 1:
                    p["status"] = "无实质进展（逾期未确认）"
            except ValueError:
                pass
    pend[:] = pend[-30:]

    ti = md.find("## 📋 今日关键数据速览")
    if ti >= 0:
        seg = md[ti:md.find("\n## ", ti + 3)]
        for row in re.finditer(r"^\|\s*([^|]{2,24})\s*\|\s*([^|]{1,24})\s*\|", seg, re.M):
            k = row.group(1).strip()
            v = row.group(2).strip()
            if k and v and k != "指标":
                seq = ds.setdefault(k[:12], [])
                seq.append({"date": report_date, "value": v[:30]})
                seq[:] = seq[-6:]

    mm = re.search(r"多空因子净得分[:：]\s*([+-]?\d+)\s*（(.+?)）", md)
    if mm:
        state["multifactor"] = {"date": report_date, "score": mm.group(1), "direction": mm.group(2).strip()}

    state["lastUpdate"] = report_date
    save_state(state)
    log(f"[状态] 已更新（预测{len(preds)}条/待确认{len(pend)}项/序列{len(ds)}组）")


def verify_report_sources(md: str) -> str:
    """4.4 来源链接有效性核验：GET 检查文末来源区 URL；4xx/5xx 失效剔除，网络受限则标注未确认。"""
    mi = md.find("## 📎 数据来源与免责声明")
    if mi < 0:
        return md
    head, tail = md[:mi], md[mi:]
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s）)」\]》]+", tail)))[:10]
    bad: set[str] = set()
    unsure: set[str] = set()
    for u in urls:
        uu = u.rstrip("。，；,;")
        try:
            with urllib.request.urlopen(
                urllib.request.Request(uu, headers={"User-Agent": UA}, method="GET"),
                timeout=6,
                context=ssl.create_default_context(),
            ) as r:
                if r.status >= 400:
                    bad.add(uu)
        except urllib.error.HTTPError as exc:
            if exc.code >= 400:
                bad.add(uu)
            else:
                unsure.add(uu)
        except Exception:  # noqa: BLE001
            unsure.add(uu)
    if bad or unsure:
        log(f"[核验] 失效 {len(bad)} 个、未确认 {len(unsure)} 个")
        for u in bad:
            tail = tail.replace(u, f"{u}（链接失效，已剔除）")
        for u in unsure:
            tail = tail.replace(u, f"{u}（核验受限，未确认）")
    return head + tail


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
    p.add_argument("--skip-extra", action="store_true", help="跳过增强板块（第二段LLM，调试用）")
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

    # 0.5) 跨日状态
    state = load_state()
    state_text = state_summary_text(state)

    # 1) 采集
    item_sets, sources = acquire(report_date, date_cn)
    if args.fetch_only:
        log(f"[采集] 完成，来源 {len(sources)}，条目集 {len(item_sets)}。fetch-only 模式退出。")
        return 0

    # 2) 组装内容（主报告 → 状态抽取 → 增强板块 → 装配）
    defer_send = False  # 数据缺失且未到末班(23:30)时延迟发送，等待后续定时重试
    if item_sets and item_sets[0][1]:
        api_key = resolve_api_key(args.api_key)
        if not api_key:
            log("[失败] 未找到 DEEPSEEK_API_KEY（--api-key / 环境变量 / ~/.dsh/.credentials.yaml）")
            return 3
        retrieval: list = []
        if not args.skip_retrieval:
            titles = [t for t in item_sets[0][1]][:15]
            retrieval = retrieve_context(titles, per_item=3, cap=40)
        user_msg = build_user_message(report_date, date_cn, item_sets, sources, retrieval, state_text)
        md_main = llm_report(api_key, SYSTEM_PROMPT, user_msg, args.model)
        log(f"[生成] 主报告完成（{len(md_main)} 字符）")

        # 状态抽取（主报告基础上），并刷新供增强板块使用
        extract_and_update_state(md_main, report_date, state)
        state_text = state_summary_text(state)

        if args.skip_extra:
            md = md_main
        else:
            try:
                market = retrieve_market_context() if not args.skip_retrieval else []
                mk_lines = []
                for q, hits in market:
                    mk_lines.append(f"◆ {q}")
                    for u, t, s in hits:
                        mk_lines.append(f"   - {t[:50]} | {u}" + (f" | {s[:80]}" if s else ""))
                extra_user = (
                    f"当日主报告：\n\n{md_main}\n\n"
                    f"【跨日历史状态】\n{state_text}\n\n"
                    f"【市场/宏观检索信息】\n" + ("\n".join(mk_lines) if mk_lines else "（未获取可信市场信息）")
                )
                extra = llm_report(api_key, SYSTEM_PROMPT_EXTRA, extra_user, args.model)
                md = assemble_report(md_main, extra)
                log("[生成] 增强板块完成，已装配")
            except Exception as exc:  # noqa: BLE001
                log(f"[警告] 增强板块生成失败，仅输出主报告: {exc}")
                md = md_main
    else:
        reason = "搜索/抓取未命中任何含条目的来源"
        now_bj = datetime.now().strftime("%Y-%m-%d %H:%M")
        if now_bj[:10] == report_date and int(now_bj[11:13]) < 19:
            reason += (
                f"（当前北京时间 {now_bj[11:16]}，当日节目 19:00 才播出，属正常无数据；"
                "若需补跑请用 --date 指定有数据的日期）"
            )
        else:
            reason += "（央视官方通道未命中：请检查网络可达性，或该日确无播出）"
        md = make_missing_report(report_date, date_cn, sources, reason)
        log(f"[生成] 数据缺失，生成说明日报（{len(md)} 字符）。原因：{reason}")
        # 多时段重试策略：21:30-23:30 窗口内，未到末班(23:30)则不发送、不写标记，等待后续定时重试
        hm = now_bj[11:16]  # HH:MM
        if hm >= "23:30":
            log("[缺失] 已达末班（≥23:30），本次发送缺失说明")
            defer_send = False
        else:
            defer_send = True
            log(f"[缺失] 当前 {hm}，未到末班（23:30），延迟不发送，等待后续定时重试")

    # 3) 来源核验 + 去分
    md = verify_report_sources(md)
    md = strip_scores(md)

    # 4) 落盘
    out_path = Path(args.out) if args.out else (DEFAULT_REPORTS / f"{report_date}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    log(f"[落盘] {out_path}（{len(md) / 1024:.1f} KB）")

    # 5) 推送（去重由 do_send 的 marker 处理；缺失未到末班则延迟）
    if defer_send:
        write_log(f"=== 结束 {report_date} deferred(缺失，等待后续定时重试) ===")
        return 0
    cfg = load_config(Path(args.config))
    code = do_send(report_date, cfg, report_path=out_path, dry_run=args.dry_run, force=args.force)
    write_log(f"=== 结束 {report_date} exit={code}（dry_run={args.dry_run}）===")
    return code


if __name__ == "__main__":
    sys.exit(main())
