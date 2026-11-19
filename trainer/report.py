"""把本地账本渲染成一份 HTML 报告 + 控制台表格。"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from . import bank as bank_mod
from .config import QBASE_TYPES


def console_table(store: bank_mod.Bank, limit: int = 20) -> str:
    rows = store.recent_papers(limit)
    if not rows:
        return "（还没有刷题记录）"
    head = f"{'时间':<17}{'题型':<7}{'状态':<6}{'得分':<10}{'备注'}"
    lines = [head, "-" * 62]
    state_name = {0: "未完成", 1: "已完成", 2: "计分中", 3: "跳过", 4: "以后做", 5: "计分失败"}
    for r in rows:
        when = (r.get("created_at") or "")[5:16]
        qt = QBASE_TYPES.get(r.get("qbase_type"), "?")
        st = state_name.get(r.get("state"), r.get("state"))
        score = f"{r.get('score')}/{r.get('total')}" if r.get("score") is not None else "-"
        lines.append(f"{when:<17}{qt:<7}{str(st):<6}{score:<10}{r.get('note') or ''}")
    return "\n".join(lines)


def build_html(store: bank_mod.Bank, out: Path | str, account: str = "") -> Path:
    rows = store.recent_papers(200)
    stats = store.stats()
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    done = [r for r in rows if r.get("score") is not None]
    scores = [float(r["score"]) for r in done][:30][::-1]
    labels = [(r.get("created_at") or "")[5:16] for r in done][:30][::-1]

    # 简单柱状图（SVG）
    chart = ""
    if scores:
        w, h, pad = 660, 180, 28
        mx = max(scores + [1])
        bw = (w - pad * 2) / max(len(scores), 1)
        bars = []
        for i, s in enumerate(scores):
            bh = (h - pad * 2) * (s / mx)
            x = pad + i * bw + bw * 0.15
            y = h - pad - bh
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw*0.7:.1f}" height="{bh:.1f}" '
                f'rx="3" fill="#0f766e" opacity="0.85"/>'
                f'<text x="{x+bw*0.35:.1f}" y="{y-5:.1f}" font-size="10" fill="#475569" '
                f'text-anchor="middle">{s:g}</text>'
            )
        chart = (
            f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">'
            f'<line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{h-pad}" stroke="#cbd5e1"/>'
            + "".join(bars)
            + "</svg>"
        )

    trs = []
    state_name = {0: "未完成", 1: "已完成", 2: "计分中", 3: "跳过", 4: "以后做", 5: "计分失败"}
    for r in rows:
        score = f"{r.get('score')}/{r.get('total')}" if r.get("score") is not None else "—"
        trs.append(
            "<tr>"
            f"<td>{html.escape(str(r.get('created_at') or ''))}</td>"
            f"<td>{html.escape(QBASE_TYPES.get(r.get('qbase_type'), '?'))}</td>"
            f"<td>{html.escape(str(state_name.get(r.get('state'), r.get('state'))))}</td>"
            f"<td class='num'>{html.escape(score)}</td>"
            f"<td class='muted'>{html.escape(str(r.get('note') or ''))}</td>"
            "</tr>"
        )

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>刷题报告</title>
<style>
 :root{{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f8fafc;--card:#fff;--brand:#0f766e}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--ink);
   font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif;line-height:1.6}}
 .wrap{{max-width:900px;margin:0 auto;padding:36px 22px 60px}}
 h1{{font-size:24px;margin:0 0 4px}}
 .sub{{color:var(--muted);font-size:13px;margin-bottom:24px}}
 .cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:26px}}
 .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px}}
 .card .k{{font-size:12px;color:var(--muted)}}
 .card .v{{font-size:26px;font-weight:600;margin-top:2px}}
 section{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:22px}}
 h2{{font-size:15px;margin:0 0 14px;color:var(--brand);letter-spacing:.02em}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th{{text-align:left;font-weight:600;color:var(--muted);border-bottom:1px solid var(--line);padding:8px 6px}}
 td{{border-bottom:1px solid #f1f5f9;padding:8px 6px}}
 td.num{{font-variant-numeric:tabular-nums;font-weight:600}}
 td.muted{{color:var(--muted)}}
 .empty{{color:var(--muted);font-size:13px}}
</style></head><body><div class="wrap">
<h1>自动刷题报告</h1>
<div class="sub">账号 {html.escape(account or '—')} · 生成于 {datetime.now():%Y-%m-%d %H:%M}</div>
<div class="cards">
  <div class="card"><div class="k">累计卷数</div><div class="v">{stats['papers']}</div></div>
  <div class="card"><div class="k">已完成</div><div class="v">{stats['done']}</div></div>
  <div class="card"><div class="k">平均分</div><div class="v">{stats['avg_score'] if stats['avg_score'] is not None else '—'}</div></div>
  <div class="card"><div class="k">本地题库</div><div class="v">{stats['cached_answers']}</div></div>
</div>
<section><h2>最近得分走势</h2>{chart or '<div class="empty">还没有已计分的卷子</div>'}</section>
<section><h2>明细</h2>
<table><thead><tr><th>开卷时间</th><th>题型</th><th>状态</th><th>得分</th><th>备注</th></tr></thead>
<tbody>{''.join(trs) or '<tr><td colspan="5" class="empty">暂无记录</td></tr>'}</tbody></table>
</section>
</div></body></html>"""
    out.write_text(doc, encoding="utf-8")
    return out
