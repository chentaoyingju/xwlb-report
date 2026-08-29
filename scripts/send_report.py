#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
新闻联播日报 SMTP 推送脚本（纯标准库，零第三方依赖）

用法:
    python scripts/send_report.py --date 2026-08-29
    python scripts/send_report.py --date 2026-08-29 --dry-run
    python scripts/send_report.py --date 2026-08-29 --force     # 忽略已发送 marker 强制重发
    python scripts/send_report.py --date 2026-08-29 --report 自定义路径.md

行为:
    1) 读取 config/smtp_config.json（相对脚本所在仓库根目录）;
    2) 若 sender / authcode / recipient 任一为空 -> 打印 [跳过] 配置缺失，未发送，退出码 0;
    3) 去重：reports/.sent-YYYY-MM-DD.marker 存在且未加 --force -> [跳过] 已发送过，退出码 0;
    4) 把 reports/YYYY-MM-DD.md 渲染为简洁 HTML 正文，并附上原始 .md 附件;
    5) 通过 QQ SMTP(465/SSL) 真实发送; 成功后写 marker; 失败打印 [失败] 并退出码 1。

退出码:
    0 = 成功 / 按设计跳过（配置缺失或已发送）  1 = 发送失败  2 = 日报文件缺失  3 = 配置缺失/非法
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):  # 保证 DSH/执行日志按 UTF-8 记录
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "smtp_config.json"
DEFAULT_REPORTS = ROOT / "reports"


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config(path: Path) -> dict:
    """读取 SMTP 配置。优先级：环境变量（SMTP_*，云端部署用） > config/smtp_config.json。

    环境变量：SMTP_HOST / SMTP_PORT / SMTP_SSL / SMTP_TIMEOUT /
              SMTP_SENDER / SMTP_AUTHCODE / SMTP_RECIPIENT（逗号分隔多个）。
    凭据只经环境变量注入（如 GitHub Actions Secrets），不写入代码/仓库。
    """
    cfg: dict = {}
    if path.is_file():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log(f"[失败] 配置文件解析错误: {exc}")
            sys.exit(3)
    elif not os.environ.get("SMTP_SENDER"):
        log(
            "[失败] 未找到发信配置：本地 config/smtp_config.json 不存在，且未提供 SMTP_SENDER 环境变量。\n"
            "       云端请检查仓库 Settings → Secrets and variables → Actions 是否已添加：\n"
            "       SMTP_SENDER / SMTP_AUTHCODE / SMTP_RECIPIENT（大小写一致）。"
        )
        sys.exit(3)

    def env_or(key: str, default):
        v = os.environ.get(key)
        return v if v not in (None, "") else default

    smtp = cfg.get("smtp") or {}
    recipients = cfg.get("recipient") or env_or("SMTP_RECIPIENT", [])
    if isinstance(recipients, str):
        recipients = [r.strip() for r in recipients.split(",") if r.strip()]

    ssl_flag = env_or("SMTP_SSL", smtp.get("ssl", True))
    if isinstance(ssl_flag, str):
        ssl_flag = ssl_flag.strip().lower() in ("1", "true", "yes", "on")

    return {
        "host": env_or("SMTP_HOST", smtp.get("host") or "smtp.qq.com"),
        "port": int(env_or("SMTP_PORT", smtp.get("port") or 465)),
        "ssl": bool(ssl_flag),
        "timeout": int(env_or("SMTP_TIMEOUT", smtp.get("timeout") or 30)),
        "sender": (env_or("SMTP_SENDER", cfg.get("sender") or "") or "").strip(),
        "authcode": (env_or("SMTP_AUTHCODE", cfg.get("authcode") or "") or "").strip(),
        "recipient": recipients,
    }


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def md_to_html(md: str) -> str:
    lines = md.splitlines()
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    bold = re.compile(r"\*\*(.+?)\*\*")
    code = re.compile(r"`(.+?)`")

    def inline(line: str) -> str:
        line = esc(line)
        line = bold.sub(r"<strong>\1</strong>", line)
        line = code.sub(r"<code>\1</code>", line)
        return line

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            continue
        if line.startswith("# "):
            close_list()
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("## "):
            close_list()
            out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("### "):
            close_list()
            out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("#### "):
            close_list()
            out.append(f"<h4>{inline(line[5:])}</h4>")
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
        elif re.fullmatch(r"-{3,}|\*{3,}", line.strip()):
            close_list()
            out.append("<hr>")
        else:
            close_list()
            out.append(f"<p>{inline(line)}</p>")
    close_list()
    return "\n".join(out)


def render_body(md: str) -> str:
    content = md_to_html(md)
    return (
        "<div style='font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;"
        "max-width:860px;margin:0 auto;padding:16px;color:#1f2328;line-height:1.7;'>"
        f"{content}"
        "<hr><p style='color:#888;font-size:12px;'>本邮件由新闻联播自动化日报系统自动生成，"
        "基于公开政策信息推演，所有预测均附证伪条件，不构成直接投资建议。</p></div>"
    )


def do_send(
    report_date: str,
    cfg: dict,
    report_path: Path | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> int:
    """组装并发送邮件。返回退出码（0/1/2/3）。成功后写去重 marker。"""
    # 1) 凭据检查：任一缺失则按设计跳过，绝不伪造成功
    if not cfg["sender"] or not cfg["authcode"] or not cfg["recipient"]:
        log("[跳过] 配置缺失（sender/authcode/recipient 有空值），未发送。")
        return 0

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", report_date):
        log(f"[失败] 日期格式非法: {report_date}")
        return 3

    report_path = report_path or (DEFAULT_REPORTS / f"{report_date}.md")
    if not report_path.is_file():
        log(f"[失败] 日报文件不存在: {report_path}")
        return 2
    md_text = report_path.read_text(encoding="utf-8")

    # 2) 去重 marker
    marker = DEFAULT_REPORTS / f".sent-{report_date}.marker"
    if not force and marker.exists():
        log(f"[跳过] 已发送过（{marker.name} 存在），如需重发请加 --force")
        return 0

    # 3) 组装邮件
    y, m, d = report_date.split("-")
    subject = f"【新闻联播日报】{int(y)}年{int(m)}月{int(d)}日"
    msg = MIMEMultipart("mixed")
    msg["From"] = formataddr(("新闻联播日报", cfg["sender"]))
    msg["To"] = ", ".join(cfg["recipient"])
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(render_body(md_text), "html", "utf-8"))

    att = MIMEText(md_text, "plain", "utf-8")
    fname = f"新闻联播日报_{report_date}.md"
    att.add_header("Content-Disposition", "attachment", filename=("utf-8", "", fname))
    msg.attach(att)

    if dry_run:
        log(
            "[干跑] 配置完整，将向 %s 发送《%s》（正文 HTML + 附件 %s，%.1f KB）"
            % (", ".join(cfg["recipient"]), subject, fname, len(md_text) / 1024)
        )
        return 0

    # 4) 真实发送
    try:
        context = ssl.create_default_context()
        if cfg["ssl"]:
            server = smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=context)
        else:
            server = smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"])
            server.starttls(context=context)
        with server:
            server.login(cfg["sender"], cfg["authcode"])
            server.send_message(msg)
        marker.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
        log(f"[成功] 已发送至 {', '.join(cfg['recipient'])}，主题：{subject}（marker 已写入）")
        return 0
    except smtplib.SMTPAuthenticationError as exc:
        log(f"[失败] SMTP 认证失败（请检查授权码）：{exc.smtp_error!r}")
        return 1
    except Exception as exc:  # noqa: BLE001
        log(f"[失败] 发送异常：{exc}")
        return 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="新闻联播日报 SMTP 推送")
    p.add_argument("--date", default=None, help="YYYY-MM-DD，默认今天")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="配置文件路径")
    p.add_argument("--report", default=None, help="日报 md 文件路径（默认 reports/日期.md）")
    p.add_argument("--dry-run", action="store_true", help="仅打印将发送的内容，不真正连接 SMTP")
    p.add_argument("--force", action="store_true", help="忽略已发送 marker，强制重发")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(Path(args.config))
    report_date = args.date or date.today().isoformat()
    return do_send(
        report_date,
        cfg,
        report_path=Path(args.report) if args.report else None,
        dry_run=args.dry_run,
        force=args.force,
    )


if __name__ == "__main__":
    sys.exit(main())
