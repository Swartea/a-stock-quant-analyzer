"""
HTML 报告生成模块 (V2.2) — 供 quant_analyzer_v2.py 调用

提供:
  - _svg_kline / _svg_chip_histogram / _svg_pe_history / _svg_radar: SVG 图
  - write_html_report(r): 生成单文件 HTML 报告 (离线可看、微信可发)

4 个 SVG 图都是纯手写, 不依赖任何 CDN/JS 库, 单文件 50KB 内.
"""
import os
import math
import datetime
from datetime import datetime

# 报告输出目录 (与 quant_analyzer_v2.py 保持一致)
OUT_DIR = os.environ.get("A_STOCK_OUT_DIR", os.path.join(os.getcwd(), "reports"))


def _svg_kline(klines, width=720, height=240, current_price=None):
    """K 线图 SVG (开高低收 + 当前价虚线)"""
    if not klines or len(klines) < 2:
        msg = "无 K 线数据" if not klines else "K 线不足"
        return '<svg width="{w}" height="{h}"><text x="50%" y="50%" text-anchor="middle" fill="#999" font-size="14">{m}</text></svg>'.format(w=width, h=height, m=msg)

    # 取最近 60 个交易日
    klines = klines[-60:]
    n = len(klines)
    margin_l, margin_r, margin_t, margin_b = 30, 60, 20, 30
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    all_high = max(k["high"] for k in klines)
    all_low = min(k["low"] for k in klines)
    pad = (all_high - all_low) * 0.05 or 0.1
    y_max = all_high + pad
    y_min = all_low - pad
    candle_w = max(2, plot_w // n * 0.7)

    parts = ['<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" style="background:#fff;border-radius:8px">'.format(w=width, h=height)]

    # 网格 + 左侧价格标签
    for i in range(5):
        y = margin_t + plot_h * i / 4
        parts.append('<line x1="{x1}" y1="{y1:.1f}" x2="{x2}" y2="{y2:.1f}" stroke="#eee"/>'.format(
            x1=margin_l, y1=y, x2=margin_l+plot_w, y2=y))
        price_at_y = y_max - (y_max - y_min) * i / 4
        parts.append('<text x="{x}" y="{y:.1f}" font-size="10" fill="#999">{p:.2f}</text>'.format(
            x=margin_l+plot_w+5, y=y+4, p=price_at_y))

    # K 线
    for i, k in enumerate(klines):
        x = margin_l + i * (plot_w / n)
        y_high = margin_t + (y_max - k["high"]) / (y_max - y_min) * plot_h
        y_low = margin_t + (y_max - k["low"]) / (y_max - y_min) * plot_h
        y_open = margin_t + (y_max - k["open"]) / (y_max - y_min) * plot_h
        y_close = margin_t + (y_max - k["close"]) / (y_max - y_min) * plot_h
        is_up = k["close"] >= k["open"]
        color = "#ef232a" if is_up else "#14b143"  # 红涨绿跌 (A股惯例)
        parts.append('<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}" stroke="{c}" stroke-width="1"/>'.format(
            x=x+candle_w/2, y1=y_high, y2=y_low, c=color))
        body_top = min(y_open, y_close)
        body_h = max(1, abs(y_close - y_open))
        parts.append('<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{c}"/>'.format(
            x=x, y=body_top, w=candle_w, h=body_h, c=color))

    # 当前价虚线
    if current_price is not None:
        y_now = margin_t + (y_max - current_price) / (y_max - y_min) * plot_h
        parts.append('<line x1="{x1}" y1="{y1:.1f}" x2="{x2}" y2="{y2:.1f}" stroke="#ff9800" stroke-width="1" stroke-dasharray="3,3"/>'.format(
            x1=margin_l, y1=y_now, x2=margin_l+plot_w, y2=y_now))
        parts.append('<text x="{x}" y="{y:.1f}" font-size="10" fill="#ff9800">{p:.2f}</text>'.format(
            x=margin_l+plot_w+5, y=y_now+4, p=current_price))

    parts.append('</svg>')
    return '\n'.join(parts)


def _svg_chip_histogram(cd, width=720, height=200):
    """筹码分布柱状图 (简化版: 三角分布近似)
    红柱=现价之上(套牢), 绿柱=现价之下(获利)
    """
    if not cd or "error" in cd:
        return ""

    peak = cd.get("peak_price", 0)
    price = cd.get("price", 0)
    if peak <= 0 or price <= 0:
        return ""

    lo, hi = cd["cost_90"][0], cd["cost_90"][1]
    if hi <= lo:
        return ""

    n_bins = 30
    margin_l, margin_r, margin_t, margin_b = 50, 60, 30, 30
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b
    bar_w = plot_w / n_bins * 0.85

    parts = ['<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" style="background:#fff;border-radius:8px">'.format(w=width, h=height)]
    parts.append('<text x="{x}" y="16" font-size="11" fill="#666">筹码分布 (红=套牢 绿=获利)</text>'.format(x=margin_l))

    # 三角分布
    bins = []
    for i in range(n_bins):
        p = lo + (hi - lo) * i / (n_bins - 1)
        h_norm = max(0, 1 - abs(p - peak) / max(0.1, (hi - lo) / 2))
        bins.append((p, h_norm))

    for i, (p, h_norm) in enumerate(bins):
        x = margin_l + i * (plot_w / n_bins)
        bh = h_norm * plot_h
        color = "#ef232a" if p > price else "#14b143"
        parts.append('<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{c}" opacity="0.75"/>'.format(
            x=x, y=margin_t + plot_h - bh, w=bar_w, h=bh, c=color))
        if i % 5 == 0:
            parts.append('<text x="{x:.1f}" y="{y:.1f}" font-size="9" fill="#999" text-anchor="middle">{p:.1f}</text>'.format(
                x=x+bar_w/2, y=margin_t+plot_h+12, p=p))

    # 当前价线
    x_now = margin_l + (price - lo) / max(0.1, hi - lo) * plot_w
    parts.append('<line x1="{x:.1f}" y1="{y1}" x2="{x:.1f}" y2="{y2}" stroke="#ff9800" stroke-width="2" stroke-dasharray="3,3"/>'.format(
        x=x_now, y1=margin_t, y2=margin_t+plot_h))
    parts.append('<text x="{x:.1f}" y="{y:.1f}" font-size="11" fill="#ff9800" font-weight="bold">现 {p:.2f}</text>'.format(
        x=x_now+5, y=margin_t+12, p=price))

    # 筹码峰线
    x_peak = margin_l + (peak - lo) / max(0.1, hi - lo) * plot_w
    parts.append('<line x1="{x:.1f}" y1="{y1}" x2="{x:.1f}" y2="{y2}" stroke="#2962ff" stroke-width="2" stroke-dasharray="2,2"/>'.format(
        x=x_peak, y1=margin_t, y2=margin_t+plot_h))
    parts.append('<text x="{x:.1f}" y="{y:.1f}" font-size="11" fill="#2962ff" font-weight="bold">峰 {p:.2f}</text>'.format(
        x=x_peak+5, y=margin_t+26, p=peak))

    parts.append('</svg>')
    return '\n'.join(parts)


def _svg_pe_history(pe_series, current_pct, width=720, height=200):
    """PE 历史折线图 (近 6 个月)"""
    if not pe_series:
        return ""

    n = len(pe_series)
    margin_l, margin_r, margin_t, margin_b = 50, 60, 30, 30
    plot_w = width - margin_l - margin_r
    plot_h = height - margin_t - margin_b

    vals = [s["peTTM"] for s in pe_series if s.get("peTTM") is not None]
    if not vals:
        return ""

    v_max = max(vals)
    v_min = min(vals)
    pad = (v_max - v_min) * 0.1 or 1
    v_max += pad
    v_min -= pad

    parts = ['<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" style="background:#fff;border-radius:8px">'.format(w=width, h=height)]
    parts.append('<text x="{x}" y="16" font-size="11" fill="#666">近 6 个月 PE (TTM) 走势 · 3 年分位 {p}%</text>'.format(
        x=margin_l, p=current_pct if current_pct else 0))

    # 网格
    for i in range(5):
        y = margin_t + plot_h * i / 4
        parts.append('<line x1="{x1}" y1="{y1:.1f}" x2="{x2}" y2="{y2:.1f}" stroke="#eee"/>'.format(
            x1=margin_l, y1=y, x2=margin_l+plot_w, y2=y))
        v_at_y = v_max - (v_max - v_min) * i / 4
        parts.append('<text x="{x}" y="{y:.1f}" font-size="10" fill="#999" text-anchor="end">{v:.0f}</text>'.format(
            x=margin_l-5, y=y+4, v=v_at_y))

    # 折线
    pts = []
    for i, s in enumerate(pe_series):
        if s.get("peTTM") is None:
            continue
        x = margin_l + i * (plot_w / n)
        y = margin_t + (v_max - s["peTTM"]) / (v_max - v_min) * plot_h
        pts.append("{x:.1f},{y:.1f}".format(x=x, y=y))

    parts.append('<polyline points="{p}" fill="none" stroke="#2962ff" stroke-width="2"/>'.format(p=' '.join(pts)))

    # 起点终点标记
    if pts:
        first_x, first_y = pts[0].split(",")
        last_x, last_y = pts[-1].split(",")
        parts.append('<circle cx="{x}" cy="{y}" r="3" fill="#14b143"/>'.format(x=first_x, y=first_y))
        parts.append('<circle cx="{x}" cy="{y}" r="4" fill="#ff9800" stroke="#fff" stroke-width="2"/>'.format(x=last_x, y=last_y))

    parts.append('</svg>')
    return '\n'.join(parts)


def _svg_radar(score_dict, width=380, height=340):
    """10 因子雷达图"""
    labels = [
        ("趋势", "trend", 12), ("估值", "valuation", 15), ("分位", "valuation_pctile", 8),
        ("资金", "capital", 15), ("动量", "momentum", 8), ("情绪", "sentiment", 8),
        ("风险", "risk", 10), ("筹码", "chip", 8), ("申万", "sw_stability", 6), ("龙虎榜", "dragon", 10),
    ]
    cx = width / 2
    cy = height / 2
    R = min(width, height) / 2 - 50
    n_axes = len(labels)

    parts = ['<svg width="{w}" height="{h}" xmlns="http://www.w3.org/2000/svg" style="background:#fff;border-radius:8px">'.format(w=width, h=height)]

    # 同心圆
    for i in range(1, 6):
        r = R * i / 5
        pts = []
        for j in range(n_axes):
            ang = -math.pi / 2 + 2 * math.pi * j / n_axes
            pts.append("{x:.1f},{y:.1f}".format(x=cx + r*math.cos(ang), y=cy + r*math.sin(ang)))
        parts.append('<polygon points="{p}" fill="none" stroke="#eee"/>'.format(p=' '.join(pts)))

    # 轴线 + 标签
    for j, (name, key, mx) in enumerate(labels):
        ang = -math.pi / 2 + 2 * math.pi * j / n_axes
        parts.append('<line x1="{x1}" y1="{y1}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#eee"/>'.format(
            x1=cx, y1=cy, x2=cx + R*math.cos(ang), y2=cy + R*math.sin(ang)))
        lx = cx + (R + 22) * math.cos(ang)
        ly = cy + (R + 22) * math.sin(ang)
        parts.append('<text x="{x:.1f}" y="{y:.1f}" font-size="12" fill="#666" text-anchor="middle">{n}</text>'.format(
            x=lx, y=ly+4, n=name))

    # 数据多边形
    data_pts = []
    for j, (name, key, mx) in enumerate(labels):
        ang = -math.pi / 2 + 2 * math.pi * j / n_axes
        v = score_dict.get(key, 0) / mx
        r = R * v
        data_pts.append("{x:.1f},{y:.1f}".format(x=cx + r*math.cos(ang), y=cy + r*math.sin(ang)))

    parts.append('<polygon points="{p}" fill="#2962ff" fill-opacity="0.35" stroke="#2962ff" stroke-width="2"/>'.format(p=' '.join(data_pts)))

    # 顶点圆 + 分数
    for j, (name, key, mx) in enumerate(labels):
        ang = -math.pi / 2 + 2 * math.pi * j / n_axes
        v = score_dict.get(key, 0) / mx
        r = R * v
        px = cx + r * math.cos(ang)
        py = cy + r * math.sin(ang)
        parts.append('<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#2962ff"/>'.format(x=px, y=py))
        parts.append('<text x="{x:.1f}" y="{y:.1f}" font-size="9" fill="#2962ff" text-anchor="middle">{s}</text>'.format(
            x=px, y=py-7, s=score_dict.get(key, 0)))

    parts.append('</svg>')
    return '\n'.join(parts)


# ============================================================
# 主函数: 生成完整 HTML 报告
# ============================================================
def write_html_report(r, interpret_pe_fn, interpret_pctile_fn, interpret_chips_fn,
                     make_trading_plan_fn, make_signal_list_fn):
    """生成单文件 HTML 报告 (依赖外层传入 5 个解读函数)

    r: analyze_single 返回的 result dict
    5 个 fn: 来自 quant_analyzer_v2.py 内部的辅助函数
    """
    code = r["code"]
    name = r["name"]
    q = r["quote"]
    v = r["valuation"]
    s = r["score"]
    score_total = s["total"]
    emoji = r["emoji"]
    advice = r["advice"]
    cd = r.get("chip_data")
    vh = r.get("valuation_hist")

    outdir = os.path.join(OUT_DIR, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    path_html = "{d}/{c}-{n}-{t}.html".format(
        d=outdir, c=code, n=name, t=datetime.now().strftime("%H%M"))

    # 准备数据
    price = q.get("price", 0)
    change_pct = s["change_pct"]
    plan = make_trading_plan_fn(q, v, cd, score_total)
    good_signals, bad_signals = make_signal_list_fn(s, s["factors"])
    pe_talk = interpret_pe_fn(q.get("pe_ttm", 0))
    pe_pctile_talk = interpret_pctile_fn(vh.get("pe_percentile_3y")) if vh and "error" not in vh else "无历史数据"

    # 颜色: 按评分定
    if score_total >= 65:
        verdict_color = "#14b143"
        verdict_bg = "#e8f5e9"
    elif score_total >= 55:
        verdict_color = "#ff9800"
        verdict_bg = "#fff3e0"
    elif score_total >= 45:
        verdict_color = "#ff5722"
        verdict_bg = "#fbe9e7"
    else:
        verdict_color = "#d32f2f"
        verdict_bg = "#ffebee"

    # 涨色: A股 红涨绿跌
    change_color = "#ef232a" if change_pct >= 0 else "#14b143"

    # 生成 4 个 SVG 图
    klines = cd.get("kline", []) if cd and "error" not in cd else []
    svg_kline = _svg_kline(klines, current_price=price)
    svg_chip = _svg_chip_histogram(cd)
    pe_series = vh.get("pe_series", []) if vh and "error" not in vh else []
    pe_pct = vh.get("pe_percentile_3y", 0) if vh and "error" not in vh else 0
    svg_pe = _svg_pe_history(pe_series, pe_pct)
    svg_radar = _svg_radar(s)

    # 时间戳
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # HTML 拼接 (用 % 格式, 避免 f-string 转义混乱)
    action_text = "✅ 可以建仓" if score_total >= 65 else ("⚠️ 轻仓试探" if score_total >= 55 else "❌ 不建议进场")

    # 头部
    html = '<!DOCTYPE html>\n'
    html += '<html lang="zh-CN"><head>\n'
    html += '<meta charset="UTF-8">\n'
    html += '<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">\n'
    html += '<title>%s (%s) %s</title>\n' % (name, code, now_str)
    html += '<style>\n'
    html += '*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}\n'
    html += 'body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f5f6fa;color:#222;padding:0;margin:0;font-size:14px;line-height:1.5}\n'
    html += '.container{max-width:760px;margin:0 auto;padding:16px;padding-bottom:env(safe-area-inset-bottom)}\n'
    html += '.header{background:linear-gradient(135deg,#2962ff,#1e88e5);color:#fff;padding:20px;border-radius:12px;margin-bottom:16px;box-shadow:0 2px 8px rgba(41,98,255,.15)}\n'
    html += '.header h1{margin:0 0 4px 0;font-size:24px}\n'
    html += '.header .sub{opacity:.85;font-size:13px}\n'
    html += '.header .price{font-size:36px;font-weight:700;margin:8px 0}\n'
    html += '.verdict{background:%s;color:%s;padding:16px;border-radius:12px;border-left:4px solid %s;margin-bottom:16px}\n' % (verdict_bg, verdict_color, verdict_color)
    html += '.verdict h2{margin:0 0 8px 0;font-size:18px}\n'
    html += '.verdict .big{font-size:26px;font-weight:700}\n'
    html += '.verdict .detail{margin-top:8px;font-size:13px;opacity:.9}\n'
    html += '.kpi-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}\n'
    html += '.kpi{background:#fff;padding:12px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.05)}\n'
    html += '.kpi .label{color:#888;font-size:11px;text-transform:uppercase}\n'
    html += '.kpi .value{font-size:18px;font-weight:600;margin-top:4px}\n'
    html += '.kpi .delta{font-size:11px;margin-top:2px;color:#666}\n'
    html += '.section{background:#fff;padding:16px;border-radius:12px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.05)}\n'
    html += '.section h2{margin:0 0 12px 0;font-size:16px;color:#2962ff;display:flex;align-items:center}\n'
    html += '.section h2::before{content:"";display:inline-block;width:3px;height:14px;background:#2962ff;margin-right:8px;border-radius:2px}\n'
    html += '.signals{display:flex;flex-wrap:wrap;gap:6px}\n'
    html += '.signal{padding:6px 10px;border-radius:6px;font-size:12px}\n'
    html += '.good{background:#e8f5e9;color:#1b5e20}\n'
    html += '.bad{background:#ffebee;color:#b71c1c}\n'
    html += '.neutral{background:#f5f5f5;color:#616161}\n'
    html += '.chart{text-align:center;margin:8px 0;overflow-x:auto}\n'
    html += 'table{width:100%%;border-collapse:collapse;font-size:13px}\n'
    html += 'th,td{padding:8px 6px;text-align:left;border-bottom:1px solid #f0f0f0}\n'
    html += 'th{color:#888;font-weight:500;font-size:11px;text-transform:uppercase}\n'
    html += '.footer{text-align:center;color:#999;font-size:11px;padding:16px;padding-bottom:env(safe-area-inset-bottom)}\n'
    html += '.up{color:%s}.down{color:#14b143}\n' % change_color
    html += 'details summary{cursor:pointer;padding:8px 0;color:#2962ff;font-size:13px;font-weight:500}\n'
    html += 'details[open] summary{margin-bottom:8px}\n'
    html += '.op-tips{margin-top:12px;padding:10px;background:rgba(255,255,255,.5);border-radius:6px;font-size:13px;line-height:1.6}\n'
    html += '.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}\n'
    html += '@media (max-width:480px){.kpi-grid{grid-template-columns:1fr 1fr}.grid-2{grid-template-columns:1fr}.container{padding:12px}}\n'
    html += '</style></head><body><div class="container">\n'

    # 头部卡片
    html += '<div class="header">\n'
    html += '<h1>%s <span style="opacity:.7;font-size:14px">(%s)</span></h1>\n' % (name, code)
    html += '<div class="sub">%s 生成 · 数据时点 %s</div>\n' % (now_str, datetime.now().strftime("%H:%M"))
    html += '<div class="price" style="color:#fff">%.2f <span style="font-size:18px" class="%s">%+.2f%%</span></div>\n' % (
        price, "up" if change_pct >= 0 else "down", change_pct)
    html += '<div class="sub">📊 综合 %d/100 分 · %s %s</div>\n' % (score_total, emoji, advice)
    html += '</div>\n'

    # 一分钟结论
    if plan:
        html += '<div class="verdict">\n'
        html += '<h2>🎯 一分钟结论</h2>\n'
        html += '<div class="big">%s</div>\n' % action_text
        html += '<div class="detail">%s · 持仓周期 %s</div>\n' % (plan['position'], plan['period'])
        html += '<table style="margin-top:12px">\n'
        html += '<tr><td><b>分批进场价</b></td><td>%.2f ~ %.2f 元</td></tr>\n' % (plan['entry_low'], plan['entry_high'])
        html += '<tr><td><b>止损位</b></td><td style="color:#d32f2f"><b>%.2f 元</b> (跌 %.1f%% 必走)</td></tr>\n' % (plan['stop_loss'], plan['stop_loss_pct'])
        html += '<tr><td><b>第一止盈 (+10%%)</b></td><td style="color:#14b143">%.2f 元</td></tr>\n' % plan['tp1']
        html += '<tr><td><b>第二止盈 (+25%%)</b></td><td style="color:#14b143">%.2f 元</td></tr>\n' % plan['tp2']
        html += '<tr><td><b>第三止盈 (+50%%)</b></td><td style="color:#14b143">%.2f 元</td></tr>\n' % plan['tp3']
        html += '</table>\n'
        html += '<div class="op-tips">💡 <b>操作口诀</b>: 现价附近分两批进(如各 1/2 仓),跌到 %.2f 必走,涨到 %.2f 先卖一半锁利,剩下一半等 %.2f 或 %.2f。</div>\n' % (
            plan['stop_loss'], plan['tp1'], plan['tp2'], plan['tp3'])
        html += '</div>\n'

    # KPI 卡片 6 个
    pb = q.get("pb", 0)
    mcap = q.get("float_mcap", 0)
    pe_fwd_v = v.get("pe_fwd") if v.get("pe_fwd") else None
    peg_v = v.get("peg")
    profit_pct = (cd["profit_ratio"]*100 if cd and "error" not in cd else None)
    peak_p = (cd["peak_price"] if cd and "error" not in cd else None)
    turnover = q.get("turnover_rate", 0)
    vol_r = q.get("vol_ratio", 0)
    amp = q.get("amplitude", 0)
    pe_pct_val = (vh.get("pe_percentile_3y") if vh and "error" not in vh else None)

    html += '<div class="kpi-grid">\n'
    html += '<div class="kpi"><div class="label">PE (TTM)</div><div class="value">%.1f</div><div class="delta">%s</div></div>\n' % (q.get("pe_ttm", 0), pe_talk)
    html += '<div class="kpi"><div class="label">PB</div><div class="value">%.2f</div><div class="delta">市值 %.0f 亿</div></div>\n' % (pb, mcap)
    html += '<div class="kpi"><div class="label">前向 PE</div><div class="value">%s</div><div class="delta">%s</div></div>\n' % (
        pe_fwd_v if pe_fwd_v else "N/A",
        ("PEG " + str(peg_v)) if peg_v else "无一致预期")
    html += '<div class="kpi"><div class="label">PE 3年分位</div><div class="value">%s%%</div><div class="delta">%s</div></div>\n' % (
        str(pe_pct_val) if pe_pct_val is not None else "?",
        pe_pctile_talk)
    html += '<div class="kpi"><div class="label">获利盘</div><div class="value">%s%%</div><div class="delta">%s</div></div>\n' % (
        ("%.0f" % profit_pct) if profit_pct is not None else "?",
        ("筹码峰 %.2f元" % peak_p) if peak_p is not None else "无")
    html += '<div class="kpi"><div class="label">换手 / 量比</div><div class="value">%.1f%% / %.1f</div><div class="delta">振幅 %.1f%%</div></div>\n' % (turnover, vol_r, amp)
    html += '</div>\n'

    # 信号清单
    html += '<div class="section"><h2>👍 看好这票的理由</h2><div class="signals">'
    if good_signals:
        for g in good_signals:
            html += '<span class="signal good">%s</span>' % g
    else:
        html += '<span class="signal neutral">暂无明确正面信号</span>'
    html += '</div></div>\n'

    html += '<div class="section"><h2>👎 要小心的信号</h2><div class="signals">'
    if bad_signals:
        for b in bad_signals:
            html += '<span class="signal bad">%s</span>' % b
    else:
        html += '<span class="signal good">✅ 没有明显负面信号</span>'
    html += '</div></div>\n'

    # 估值人话 + PE 历史并排（手机上下排）
    html += '<div class="section"><h2>💰 估值贵不贵</h2>\n'
    html += '<ul style="list-style:none;padding:0;margin:0">\n'
    html += '<li style="padding:6px 0">▸ <b>PE %.1f</b> — %s</li>\n' % (q.get("pe_ttm", 0), pe_talk)
    if pb > 0:
        pb_talk = "PB 偏高" if pb > 10 else ("PB 偏高" if pb > 5 else "PB 偏低")
        html += '<li style="padding:6px 0">▸ <b>PB %.2f</b> — %s</li>\n' % (pb, pb_talk)
    if v.get("peg") and v["peg"] != float("inf"):
        peg_v = v["peg"]
        peg_talk = "PEG < 1，便宜区" if peg_v < 1 else ("PEG 1~1.5，合理" if peg_v < 1.5 else "PEG > 1.5，偏贵")
        html += '<li style="padding:6px 0">▸ <b>PEG %s</b> — %s</li>\n' % (peg_v, peg_talk)
    if v.get("analyst_count"):
        cnt = v["analyst_count"]
        cov_talk = "覆盖足够" if cnt >= 5 else "覆盖较少，预期可能不准"
        html += '<li style="padding:6px 0">▸ <b>机构覆盖 %d 家</b> — %s</li>\n' % (cnt, cov_talk)
    if vh and "error" not in vh and vh.get("pe_percentile_3y") is not None:
        html += '<li style="padding:6px 0">▸ <b>过去 3 年分位</b> — %s</li>\n' % pe_pctile_talk
    html += '</ul></div>\n'

    # K 线图 + 筹码图: 并排显示（移动端上下排）
    if svg_kline and svg_chip:
        html += '<div class="grid-2">\n'
        html += '<div class="section"><h2>📈 K 线</h2><div class="chart">%s</div></div>\n' % svg_kline
        html += '<div class="section"><h2>🎰 筹码分布</h2><div class="chart">%s</div></div>\n' % svg_chip
        html += '</div>\n'
    elif svg_kline:
        html += '<div class="section"><h2>📈 近 60 日 K 线</h2><div class="chart">%s</div></div>\n' % svg_kline
    elif svg_chip:
        html += '<div class="section"><h2>🎰 筹码分布</h2><div class="chart">%s</div></div>\n' % svg_chip

    # PE 历史
    if svg_pe:
        html += '<div class="section"><h2>📊 近 6 个月 PE 走势</h2><div class="chart">%s</div></div>\n' % svg_pe

    # 10 因子雷达
    html += '<div class="section"><h2>🔬 10 因子雷达</h2><div class="chart">%s</div></div>\n' % svg_radar

    # 三种情景
    if plan:
        html += '<div class="section"><h2>🎲 如果接下来…</h2><table>'
        html += '<tr><td style="color:#14b143"><b>…涨到 %.2f (+10%%)</b></td><td>卖 1/2 仓锁利,留 1/2 看 %.2f</td></tr>' % (plan['tp1'], plan['tp2'])
        html += '<tr><td><b>…横盘不动</b></td><td>观察一周,如一直横在 %.2f~%.2f 不破 %.2f 就继续持有</td></tr>' % (plan['entry_low'], price, plan['stop_loss'])
        html += '<tr><td style="color:#d32f2f"><b>…跌到 %.2f</b></td><td><b>必走,不留恋</b></td></tr>' % plan['stop_loss']
        html += '</table></div>\n'

    # 详细数据表 (折叠)
    html += '<div class="section"><details><summary>📋 查看详细数据表</summary>\n'
    html += '<h3>实时行情</h3><table>'
    html += '<tr><td>涨跌停价</td><td>%.2f / %.2f</td></tr>' % (q.get('limit_up', 0), q.get('limit_down', 0))
    html += '<tr><td>振幅</td><td>%.2f%%</td></tr>' % q.get('amplitude', 0)
    html += '<tr><td>换手率</td><td>%.2f%%</td></tr>' % q.get('turnover_rate', 0)
    html += '<tr><td>量比</td><td>%.2f</td></tr>' % q.get('vol_ratio', 0)
    html += '</table>'

    if v.get("pe_fwd") or v.get("eps_cur"):
        html += '<h3 style="margin-top:16px">机构一致预期</h3><table>'
        for k, label in [("analyst_count","覆盖机构数"),("eps_cur","当年EPS"),("eps_next","次年EPS"),
                         ("cagr_pct","预期增速%"),("pe_fwd","前向PE"),("peg","PEG")]:
            v_str = v.get(k, "N/A")
            html += '<tr><td>%s</td><td>%s</td></tr>' % (label, v_str)
        html += '</table>'

    if vh and "error" not in vh:
        html += '<h3 style="margin-top:16px">估值历史</h3><table>'
        html += '<tr><td>数据范围</td><td>%s → %s</td></tr>' % (vh.get("data_start",""), vh.get("data_end",""))
        html += '<tr><td>PE 当前/中位/区间</td><td>%s / %s / %s~%s</td></tr>' % (
            vh.get("current_pe","?"), vh.get("pe_median","?"), vh.get("pe_min","?"), vh.get("pe_max","?"))
        html += '<tr><td>PB 当前</td><td>%s</td></tr>' % vh.get("current_pb","?")
        html += '<tr><td>ST 占比</td><td>%s%%</td></tr>' % vh.get("is_st_ratio","?")
        html += '</table>'

    if cd and "error" not in cd:
        html += '<h3 style="margin-top:16px">筹码分布明细</h3><table>'
        html += '<tr><td>获利比例</td><td>%.2f%%</td></tr>' % (cd['profit_ratio']*100)
        html += '<tr><td>平均成本</td><td>%.2f 元</td></tr>' % cd['avg_cost']
        html += '<tr><td>90%% 成本区间</td><td>%.2f ~ %.2f</td></tr>' % (cd['cost_90'][0], cd['cost_90'][1])
        html += '<tr><td>70%% 成本区间</td><td>%.2f ~ %.2f</td></tr>' % (cd['cost_70'][0], cd['cost_70'][1])
        html += '<tr><td>筹码峰</td><td>%.2f 元</td></tr>' % cd['peak_price']
        html += '<tr><td>90%% 集中度</td><td>%.2f%%</td></tr>' % (cd['concentration_90']*100)
        html += '<tr><td>窗口累计换手</td><td>%s%%</td></tr>' % cd.get("total_turnover_pct", "?")
        html += '</table>'

    html += '</details></div>\n'

    # Footer
    html += '<div class="footer">'
    html += '⚠️ 本报告基于公开数据的多因子量化模型生成,不构成投资建议。<br>'
    html += '数据时点 %s · 市场随时变化,请独立判断。<br>' % now_str
    html += '<span style="opacity:.5">Generated by a-stock-data V2.2 HTML</span>'
    html += '</div>'

    html += '</div></body></html>'

    with open(path_html, "w", encoding="utf-8") as f:
        f.write(html)
    return path_html