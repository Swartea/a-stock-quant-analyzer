#!/usr/bin/env python3
"""
A 股多因子量化分析器 V2 (兼容 a-stock-data V3.7.2)
======================================================

v1 (V3.2.2 era):  6 因子，9 端点，单票/批量合并
v2 (V3.7.2):    10 因子，22 端点，CLI 单票/批量分离，Markdown 报告输出

因子体系 (100 分):
  趋势因子     12分  K线形态 + 涨跌 + 量能
  估值因子     15分  PE-TTM / PB / 一致预期 / PE消化 / PEG
  估值分位     8分   PE/PB 在过去 3 年的分位 (baostock 估值历史)
  资金因子     15分  主力/大单净流入 (当日 + 近 20 日 120日)
  动量因子     8分   换手/量比/振幅
  情绪因子     8分   概念热度 + 北向资金
  风险因子     10分  解禁 + 估值偏离 + 小盘溢价
  筹码因子     8分   筹码获利比例 / 集中度 (V3.7 新增)
  申万稳定性   6分   行业变迁次数 / 当前分类 (V3.7 新增)
  龙虎榜因子   10分  近 30 日上榜次数 / 机构动向

免责声明: 本工具仅供数据分析参考，不构成任何投资建议。
"""

import requests
import urllib.request
import json
import time
import random
import math
import os
import sys
import re
import io
from datetime import datetime, timedelta
from typing import Optional
import pandas as pd
import numpy as np
from html_report import write_html_report as _write_html_report

# ============================================================
# 配置 & 节流（来自 V3.7.2 SKILL.md）
# ============================================================
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# 报告输出目录 (用户可改)
import os as _os
OUT_DIR = _os.environ.get("A_STOCK_OUT_DIR", _os.path.join(_os.getcwd(), "reports"))
EM_MIN_INTERVAL = 1.2  # 东财串行限流（实测建议 1.2~1.5s，过快会被风控）
_em_last_call = [0.0]
EM_SESSION = requests.Session()
EM_SESSION.headers.update({"User-Agent": UA})

HSGT_HEADERS = {"User-Agent": UA, "Referer": "https://data.hexin.cn/"}

# 申万行业分类表 (V3.7 新增，XLS ~10MB，模块级懒加载缓存)
SW_URL = "https://www.swsresearch.com/swindex/pdf/SwClass2021/StockClassifyUse_stock.xls"
_SW_CACHE = {"df": None, "loaded_at": None}

# 筹码分布三角分布权重 (从 SKILL.md §6.5 直接搬)
def _triangular_weights(grid, low, high, avg):
    """当日筹码在价格网格上的三角分布权重（峰值在均价，面积归一）"""
    w = np.zeros_like(grid)
    if not np.isfinite([low, high, avg]).all() or high < low:
        return w
    if high - low < 1e-9:
        w[np.argmin(np.abs(grid - low))] = 1.0
        return w
    avg = min(max(avg, low), high)
    left = (grid >= low) & (grid <= avg)
    right = (grid > avg) & (grid <= high)
    if avg - low > 1e-9:
        w[left] = (grid[left] - low) / (avg - low)
    else:
        w[left] = 1.0
    if high - avg > 1e-9:
        w[right] = (high - grid[right]) / (high - avg)
    else:
        w[right] = 1.0
    total = w.sum()
    if total > 0:
        return w / total
    w[np.argmin(np.abs(grid - avg))] = 1.0
    return w

def em_get(url, params=None, headers=None, timeout=15, **kwargs):
    """东财统一入口：串行限流 + 会话复用。"""
    wait = EM_MIN_INTERVAL - (time.time() - _em_last_call[0])
    if wait > 0:
        time.sleep(wait + random.uniform(0.1, 0.5))
    try:
        return EM_SESSION.get(url, params=params, headers=headers, timeout=timeout, **kwargs)
    finally:
        _em_last_call[0] = time.time()

# ============================================================
# 辅助函数
# ============================================================
def normalize_code(code: str) -> str:
    """归一化为纯 6 位。"""
    return re.sub(r"[^0-9]", "", code)[-6:].zfill(6)

def get_prefix(code: str) -> str:
    """市场前缀：沪 / 深 / 北。"""
    if code.startswith(("92", "8")):
        return "bj"
    if code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"

def em_market_code(code: str) -> str:
    """东财 secid 前缀：沪=1, 深=0, 京=0。"""
    prefix = get_prefix(code)
    if prefix == "sh":
        return "1"
    if prefix == "bj":
        return "0"
    return "0"

# ============================================================
# 数据获取层 — 行情
# ============================================================
def fetch_tencent_quote(codes) -> dict:
    """腾讯实时行情（批量，支持 5x沪ETF/9x沪指数/北交所920号段）。"""
    if isinstance(codes, str):
        codes = [codes]
    SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}

    prefixed = []
    key_of = {}
    for c in codes:
        low = c.lower()
        if low.startswith(("sh", "sz", "bj")):
            p = low
        elif c.startswith("92"):
            p = f"bj{c}"
        elif c in SH_INDEX or c.startswith(("5", "6", "9")):
            p = f"sh{c}"
        elif c.startswith(("4", "8")):
            p = f"bj{c}"
        else:
            p = f"sz{c}"
        prefixed.append(p)
        key_of[p] = c

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = resp.content.decode("gbk")
    except Exception as e:
        return {k: {"error": str(e)} for k in codes}

    results = {}
    for line in data.strip().split(";"):
        if '="' not in line or '";"' in line and "=" not in line:
            continue
        # 提取 key 和 值
        m = re.match(r'v_([a-z]+\d{6})="([^"]*)"', line.strip())
        if not m:
            continue
        key, payload = m.group(1), m.group(2)
        original_code = key_of.get(key, key)
        if not payload:
            results[original_code] = {}
            continue
        parts = payload.split("~")
        if len(parts) < 50:
            results[original_code] = {"error": "字段不足"}
            continue
        def g(i, default=0):
            try:
                v = parts[i]
                return float(v) if v else default
            except (IndexError, ValueError):
                return default
        results[original_code] = {
            "name": parts[1],
            "price": g(3),
            "prev_close": g(4),
            "open": g(5),
            "high": g(33),
            "low": g(34),
            "volume_shou": g(36),       # 成交量(手)
            "turnover_wan": g(37),       # 成交额(万)
            "amplitude": g(43),          # ⚠️ 振幅，有些版本是 32
            "turnover_rate": g(38),      # 换手率%
            "pe_ttm": g(39),
            "pb": g(46),
            "float_mcap": g(45),         # 流通市值(亿) ← 注意是 45 不是 44
            "vol_ratio": g(49, 1),
            "limit_up": g(47),
            "limit_down": g(48),
        }
    return results


def fetch_full_valuation(code: str) -> dict:
    """一站式估值：腾讯实时 + 同花顺一致预期 + PE消化 + PEG。"""
    code = normalize_code(code)
    prefix = get_prefix(code)
    url = f"https://qt.gtimg.cn/q={prefix}{code}"
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=10)
        data = resp.content.decode("gbk")
    except Exception as e:
        return {"error": f"腾讯接口失败: {e}"}

    if '=""' in data or '"' not in data:
        return {"error": "腾讯接口返回空"}
    vals = data.split('"')[1].split("~")
    try:
        price = float(vals[3])
        mcap = float(vals[45])   # 总市值(亿)
        pe_ttm = float(vals[39]) if vals[39] else 0
        pb = float(vals[46]) if vals[46] else 0
        name = vals[1]
    except (IndexError, ValueError) as e:
        return {"error": f"腾讯字段解析失败: {e}"}

    # 同花顺一致预期 EPS
    eps_cur = eps_next = None
    analyst_count = 0
    try:
        ths_url = f"https://basic.10jqka.com.cn/new/{code}/worth.html"
        r = requests.get(ths_url, headers={"User-Agent": UA, "Referer": "https://basic.10jqka.com.cn/"},
                        timeout=10)
        r.encoding = "gbk"
        dfs = pd.read_html(io.StringIO(r.text))
        for df in dfs:
            cols = [str(c) for c in df.columns]
            if any("每股收益" in c or "均值" in c for c in cols):
                def _pick(row, name):
                    for c in df.columns:
                        if name in str(c):
                            return row.get(c)
                    return None
                r0 = df.iloc[0]
                v = _pick(r0, "均值")
                if v is not None and pd.notna(v):
                    eps_cur = float(v)
                cnt = _pick(r0, "预测机构数")
                if cnt is not None and pd.notna(cnt):
                    analyst_count = int(cnt)
                if len(df) >= 2:
                    vn = _pick(df.iloc[1], "均值")
                    if vn is not None and pd.notna(vn):
                        eps_next = float(vn)
                break
    except Exception as e:
        # 一致预期失败不算致命错误
        pass

    # 估值计算
    pe_fwd = price / eps_cur if (eps_cur and eps_cur > 0) else None
    cagr = ((eps_next / eps_cur - 1) if (eps_cur and eps_next and eps_cur > 0) else 0)
    peg = (pe_fwd / (cagr * 100)) if (pe_fwd and cagr > 0) else None
    digest_years = 0.0
    if pe_fwd and cagr > 0 and pe_fwd > 30:
        try:
            digest_years = math.log(pe_fwd / 30) / math.log(1 + cagr)
        except (ValueError, ZeroDivisionError):
            digest_years = float("inf")

    return {
        "name": name,
        "code": code,
        "price": price,
        "mcap_yi": mcap,
        "pe_ttm": pe_ttm,
        "pb": pb,
        "eps_cur": eps_cur,
        "eps_next": eps_next,
        "analyst_count": analyst_count,
        "pe_fwd": round(pe_fwd, 1) if pe_fwd else None,
        "cagr_pct": round(cagr * 100, 0) if cagr else None,
        "peg": round(peg, 2) if peg else None,
        "digest_years": round(digest_years, 1) if digest_years != float("inf") else None,
    }

# ============================================================
# 数据获取层 — 信号 / 资金 / 板块
# ============================================================
def fetch_eastmoney_concept_blocks(code: str) -> list:
    """东财 slist 概念板块归属（V3.2.2 替换百度 PAE）。"""
    secid_str = f"{em_market_code(code)}.{code}"
    params = {
        "spt": "3", "fltt": "2", "invt": "2",
        "fields": "f12,f14,f3,f128,f140,f207",
        "secid": secid_str,
    }
    try:
        r = em_get("https://push2.eastmoney.com/api/qt/slist/get",
                   params=params, headers={"User-Agent": UA,
                       "Referer": "https://quote.eastmoney.com/"}, timeout=10)
        d = r.json()
        blocks = (d.get("data") or {}).get("diff") or []
        return [{"name": it.get("f14", ""), "code": it.get("f12", ""),
                 "change_pct": it.get("f3", ""), "lead_stock": it.get("f128", ""),
                 "lead_code": it.get("f140", "")}
                for it in blocks]
    except Exception as e:
        return [{"error": str(e)}]

def fetch_fund_flow_minute(code: str) -> dict:
    """东财 push2 当日分钟级资金流。"""
    secid_str = f"{em_market_code(code)}.{code}"
    params = {"secid": secid_str, "klt": 1,
              "fields1": "f1,f2,f3,f7",
              "fields2": "f51,f52,f53,f54,f55,f56,f57"}
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
               "Origin": "https://quote.eastmoney.com"}
    try:
        r = em_get("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
                   params=params, headers=headers, timeout=10)
        d = r.json()
    except Exception as e:
        return {"error": str(e), "klines": []}

    rows = []
    for line in (d.get("data") or {}).get("klines") or []:
        p = line.split(",")
        if len(p) >= 6:
            rows.append({
                "time": p[0],
                "main_net": float(p[1]),       # 元
                "small_net": float(p[2]),
                "mid_net": float(p[3]),
                "large_net": float(p[4]),
                "super_net": float(p[5]),
            })
    if not rows:
        return {"klines": [], "summary": {}}

    # 汇总统计
    total_main = sum(r["main_net"] for r in rows) / 1e8      # 折亿
    recent_main = sum(r["main_net"] for r in rows[-30:]) / 1e8  # 最近 30 min
    # 趋势
    if len(rows) >= 60:
        mid = len(rows) // 2
        first = sum(r["main_net"] for r in rows[:mid])
        second = sum(r["main_net"] for r in rows[mid:])
        if second > first > 0:
            trend = "加速流入"
        elif second > first:
            trend = "减速流出"
        elif 0 < second < first:
            trend = "流入放缓"
        elif second < first < 0:
            trend = "加速流出"
        else:
            trend = "震荡"
    else:
        trend = "数据不足"

    return {
        "klines": rows,
        "summary": {
            "total_main_yi": round(total_main, 3),
            "recent_main_yi": round(recent_main, 3),
            "trend": trend,
            "n_points": len(rows),
        },
    }

def fetch_industry_ranking(top_n: int = 10) -> list:
    """东财行业板块 TOP 排名。"""
    params = {
        "pn": "1", "pz": str(top_n), "po": "1", "np": "1",
        "fltt": "2", "invt": "2", "fid": "f3",
        "fs": "m:90+t:2",
        "fields": "f2,f3,f4,f12,f14,f104,f105,f128",
    }
    try:
        r = em_get("https://push2.eastmoney.com/api/qt/clist/get",
                   params=params, headers={"User-Agent": UA,
                       "Referer": "https://quote.eastmoney.com/"}, timeout=10)
        d = r.json()
        diff = (d.get("data") or {}).get("diff") or []
        return [{"name": it.get("f14", ""), "change_pct": it.get("f3", ""),
                 "up_count": it.get("f104", ""), "down_count": it.get("f105", ""),
                 "lead_stock": it.get("f128", "")} for it in diff]
    except Exception:
        return []

def fetch_hsgt_realtime() -> dict:
    """同花顺北向资金当日累计。"""
    try:
        r = requests.get("https://data.hexin.cn/market/hsgtApi/method/dayChart/",
                        headers=HSGT_HEADERS, timeout=10)
        d = r.json()
        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])
        if not times:
            return {}
        return {
            "latest_hgt_yi": hgt[-1] if hgt else 0,
            "latest_sgt_yi": sgt[-1] if sgt else 0,
            "total_yi": (hgt[-1] + sgt[-1]) if hgt and sgt else 0,
            "data_points": len(times),
        }
    except Exception:
        return {}

def fetch_thx_hot_stocks() -> list:
    """同花顺当日强势股 + 题材归因（reason 字段做词频统计用）。"""
    try:
        r = requests.get("http://zx.10jqka.com.cn/event/api/getharden/",
                        headers={"User-Agent": UA, "Referer": "https://www.10jqka.com.cn/"},
                        timeout=10)
        d = r.json()
        if d.get("errocode", 0) != 0:
            return []
        return [{"code": row.get("code", ""), "name": row.get("name", ""),
                 "change": row.get("change", 0), "reason": row.get("reason", "")}
                for row in (d.get("data") or [])]
    except Exception:
        return []

# ============================================================
# 数据获取层 — V3.7 新增端点
# ============================================================
def fetch_valuation_history(code: str, lookback_days: int = 252 * 3) -> Optional[dict]:
    """baostock 估值历史 → 当前 PE/PB 分位数。
    ⚠️ baostock 不支持北交所（4/8/92/920 号段），不支持 ETF。"""
    code = normalize_code(code)
    if code.startswith(("8", "92")):
        return {"error": "baostock 不支持北交所/8 字头代码"}

    try:
        import baostock as bs
    except ImportError:
        return {"error": "baostock 未安装"}

    # 转换 code 到 baostock 格式
    prefix = get_prefix(code)
    bs_code = f"{prefix}.{code}" if prefix != "bj" else None
    if not bs_code:
        return {"error": "不支持北交所"}

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=int(lookback_days * 1.5))).strftime("%Y-%m-%d")

    try:
        bs.login()
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,code,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn,tradestatus,isST",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="3",
        )
        rows = []
        while (rs.error_code == "0") and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
    except Exception as e:
        return {"error": f"baostock 请求失败: {e}"}

    if not rows:
        return {"error": "无数据"}

    df = pd.DataFrame(rows, columns=rs.fields)
    for c in ("close", "peTTM", "pbMRQ", "psTTM", "pcfNcfTTM", "turn"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["peTTM", "pbMRQ"])

    if df.empty:
        return {"error": "数据为空"}

    pe_ttm_series = df["peTTM"].dropna()
    pb_series = df["pbMRQ"].dropna()

    if len(pe_ttm_series) < 30:
        return {"error": f"历史数据不足({len(pe_ttm_series)} 条)"}

    cur_pe = pe_ttm_series.iloc[-1]
    cur_pb = pb_series.iloc[-1]

    # 分位数（越小越便宜）
    pe_pct = (pe_ttm_series < cur_pe).sum() / len(pe_ttm_series) * 100
    pb_pct = (pb_series < cur_pb).sum() / len(pb_series) * 100

    # 画图用: 取最近 180 个交易日的 PE/PB 序列
    recent_n = min(180, len(df))
    recent = df.tail(recent_n)[["date", "peTTM", "pbMRQ", "close"]].copy()
    recent["date"] = recent["date"].astype(str)
    pe_series = recent[["date", "peTTM"]].dropna().to_dict("records")
    pb_series_list = recent[["date", "pbMRQ"]].dropna().to_dict("records")

    return {
        "n_days": len(df),
        "current_pe": round(float(cur_pe), 2),
        "current_pb": round(float(cur_pb), 2),
        "pe_percentile_3y": round(float(pe_pct), 1),
        "pb_percentile_3y": round(float(pb_pct), 1),
        "pe_min": round(float(pe_ttm_series.min()), 2),
        "pe_max": round(float(pe_ttm_series.max()), 2),
        "pe_median": round(float(pe_ttm_series.median()), 2),
        "is_st_ratio": round((df["isST"] == "1").sum() / len(df) * 100, 1),
        "data_start": str(df["date"].iloc[0]),
        "data_end": str(df["date"].iloc[-1]),
        "pe_series": pe_series,       # 给 HTML 画分位图
        "pb_series": pb_series_list,
        "close_series": recent[["date", "close"]].to_dict("records"),
    }


def _bs_code(code: str) -> str:
    """6位代码 → baostock 格式；北交所在登录前就拦掉"""
    code = str(code).zfill(6)
    if code[:2] in ("60", "68", "90"):
        return f"sh.{code}"
    if code[:2] in ("00", "30", "20"):
        return f"sz.{code}"
    raise ValueError(
        f"baostock 不支持该代码: {code}（北交所 4/8/92/920 号段被服务端拒绝，"
        f"报 10004011 股票代码未标识sh或sz）。北交所估值请改用腾讯当日快照。"
    )


def chip_distribution(df: pd.DataFrame, grid_size: int = 300, decay: float = 1.0) -> dict:
    """筹码分布 CYQ (V3.7) — df 需含 date/high/low/close/turn（turn 为百分数）
    decay: 换手衰减系数。1.0=按真实换手率；同花顺口径常用 1.5~2.0 加快历史筹码消散。
    算法来自 SKILL.md §6.5，作者调试过的版本，直接搬以保留所有边界处理。
    """
    need = {"date", "high", "low", "close", "turn"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"chip_distribution 缺少列: {sorted(missing)}")
    d = df.dropna(subset=["high", "low", "close", "turn"]).copy()
    d = d[d["high"] > 0]
    if d.empty:
        raise ValueError("chip_distribution: 有效行数为 0（检查是否全是停牌日）")
    d = d.sort_values("date").reset_index(drop=True)

    lo, hi = float(d["low"].min()), float(d["high"].max())
    pad = (hi - lo) * 0.02 or max(lo * 0.02, 0.01)
    grid = np.linspace(lo - pad, hi + pad, grid_size)

    chips = None
    for row in d.itertuples(index=False):
        t = float(row.turn) / 100.0 * decay
        t = min(max(t, 0.0), 1.0)
        avg = (float(row.high) + float(row.low) + float(row.close)) / 3.0
        w = _triangular_weights(grid, float(row.low), float(row.high), avg)
        if w.sum() <= 0:
            continue
        if chips is None:
            chips = w.copy()
            continue
        chips = chips * (1.0 - t) + w * t
    if chips is None:
        raise RuntimeError("chip_distribution: 所有交易日的价格区间都无效")
    total = chips.sum()
    if total <= 0:
        raise RuntimeError("chip_distribution: 筹码总量为 0")
    chips = chips / total

    price = float(d["close"].iloc[-1])
    cum = np.cumsum(chips)
    def price_at(q):
        return float(np.interp(q, cum, grid))
    p05, p15, p85, p95 = (price_at(q) for q in (0.05, 0.15, 0.85, 0.95))
    peak_i = int(np.argmax(chips))
    return {
        "price": price,
        "profit_ratio": float(chips[grid <= price].sum()),
        "avg_cost": float((grid * chips).sum()),
        "cost_90": (p05, p95),
        "cost_70": (p15, p85),
        "concentration_90": float((p95 - p05) / (p95 + p05)) if p95 + p05 else None,
        "concentration_70": float((p85 - p15) / (p85 + p15)) if p85 + p15 else None,
        "peak_price": float(grid[peak_i]),
    }


def fetch_chip_distribution(code: str, lookback_days: int = 250) -> dict:
    """拉 baostock 前复权 K 线 → 调 chip_distribution (V3.7 满血)。"""
    code = normalize_code(code)
    try:
        bs_code = _bs_code(code)   # 自动识别深沪 + 拦截北交所
    except ValueError as e:
        return {"error": str(e)}
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=int(lookback_days * 1.5))).strftime("%Y-%m-%d")
    try:
        import baostock as bs
        bs.login()
        rs = bs.query_history_k_data_plus(
            bs_code,
            "date,open,high,low,close,turn,tradestatus",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2",  # 2=前复权，筹码成本必须用复权价
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
    except Exception as e:
        return {"error": f"baostock 失败: {e}"}
    if not rows:
        return {"error": "无 K 线数据"}
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "turn", "tradestatus"])
    for c in ("open", "high", "low", "close", "turn"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df[df["tradestatus"] == "1"].copy()   # 停牌日不参与衰减
    if len(df) < 30:
        return {"error": f"有效交易日不足 {len(df)} 条"}

    try:
        r = chip_distribution(df, grid_size=300, decay=1.0)
        r["n_days"] = len(df)
        r["window_start"] = str(df["date"].iloc[0])
        r["window_end"] = str(df["date"].iloc[-1])
        r["total_turnover_pct"] = round(df["turn"].sum(), 1)
        # 保存 K 线数据 (HTML 画图用)
        kline = df[["date", "open", "high", "low", "close", "turn"]].copy()
        kline["date"] = kline["date"].astype(str)
        r["kline"] = kline.to_dict("records")
        return r
    except Exception as e:
        return {"error": f"chip_distribution 失败: {e}"}


def sw_industry_history() -> Optional[pd.DataFrame]:
    """申万行业归属变迁史 — 每只股票每次行业调整一行 (V3.7)"""
    if _SW_CACHE["df"] is not None:
        return _SW_CACHE["df"]
    try:
        r = requests.get(SW_URL, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        df = pd.read_excel(io.BytesIO(r.content))
    except requests.exceptions.SSLError as e:
        print("[WARN] sw_industry_history SSL 握手失败。")
        print("       这是本机 CA 包过旧导致，修复: pip install -U certifi")
        print(f"       原始: {e}")
        return None
    except Exception as e:
        print(f"[WARN] sw_industry_history 失败: {e}")
        return None
    df = df.rename(columns={"股票代码": "code", "计入日期": "start_date",
                            "行业代码": "industry_code", "更新日期": "update_date"})
    missing = {"code", "start_date", "industry_code"} - set(df.columns)
    if missing:
        print(f"[WARN] 申万表结构变了，缺列 {sorted(missing)}")
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["industry_code"] = df["industry_code"].astype(str).str.zfill(6)
    df["l1_code"] = df["industry_code"].str[:2] + "0000"
    df["l2_code"] = df["industry_code"].str[:4] + "00"
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df = df.sort_values(["code", "start_date"]).reset_index(drop=True)
    _SW_CACHE["df"] = df
    _SW_CACHE["loaded_at"] = datetime.now()
    print(f"  [sw] 申万表加载: {len(df)} 行 | {df['code'].nunique()} 只 | "
          f"{df['l1_code'].nunique()} 个一级行业")
    return df


def sw_industry_as_of(df: pd.DataFrame, code: str, as_of: str) -> Optional[dict]:
    """某只股票在 as_of 日所属的申万行业（取不晚于该日的最后一次调整）"""
    if df is None:
        return None
    code = str(code).zfill(6)
    sub = df[(df["code"] == code) & (df["start_date"] <= pd.Timestamp(as_of))]
    if sub.empty:
        return None
    row = sub.iloc[-1]
    return {
        "code": code, "as_of": as_of,
        "industry_code": row["industry_code"],
        "l1_code": row["l1_code"], "l2_code": row["l2_code"],
        "since": row["start_date"].strftime("%Y-%m-%d"),
    }


def fetch_sw_stability(code: str) -> dict:
    """申万行业变迁次数 → 稳定性得分 (V3.7 满血)。
    行业变更越频繁，前视偏差越大，得分越低。"""
    code = normalize_code(code)
    df = sw_industry_history()
    if df is None:
        return {"error": "申万表加载失败"}
    sub = df[df["code"] == code]
    if sub.empty:
        return {"error": "该股票无申万记录"}
    n_changes = len(sub)
    current = sw_industry_as_of(df, code, datetime.now().strftime("%Y-%m-%d"))
    # 行业活跃度：变更次数 vs 全市场中位数
    median_changes = df.groupby("code").size().median()
    is_churning = n_changes > median_changes
    return {
        "n_changes": n_changes,
        "median_changes": int(median_changes),
        "is_churning": is_churning,
        "current_l1": current["l1_code"] if current else None,
        "current_l2": current["l2_code"] if current else None,
        "since": current["since"] if current else None,
        "all_changes": sub[["start_date", "l1_code", "l2_code"]].to_dict("records"),
    }


def fetch_margin_trading(code: str) -> Optional[dict]:
    """融资融券最近一期（V3.7 用 eastmoney_datacenter）。"""
    from urllib.parse import quote
    # 走 em_datacenter 的 report_name=RPTA_WEB_RZRQ_GGMX
    try:
        params = {
            "reportName": "RPTA_WEB_RZRQ_GGMX",
            "columns": "ALL",
            "filter": f'(SCODE="{code}")',
            "pageNumber": "1",
            "pageSize": "5",
            "sortColumns": "DATE",
            "sortTypes": "-1",
        }
        r = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                   params=params, headers={"User-Agent": UA,
                       "Referer": "https://data.eastmoney.com/"}, timeout=15)
        d = r.json()
        result = d.get("result") or {}
        rows = result.get("data") or []
        if not rows:
            return {"error": "无融资融券数据"}
        latest = rows[0]
        return {
            "date": str(latest.get("DATE", ""))[:10],
            "rzye_yi": float(latest.get("RZYE", 0) or 0) / 1e8,
            "rzmre_yi": float(latest.get("RZMRE", 0) or 0) / 1e8,
            "rzche_yi": float(latest.get("RZCHE", 0) or 0) / 1e8,
            "rqye_yi": float(latest.get("RQYE", 0) or 0) / 1e8,
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_lockup_expiry(code: str, trade_date: str = None,
                       forward_days: int = 90) -> dict:
    """限售解禁：历史 + 未来 forward_days 天。"""
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    try:
        end_str = (datetime.strptime(trade_date, "%Y-%m-%d") + timedelta(days=forward_days)).strftime("%Y-%m-%d")
        params = {
            "reportName": "RPT_LIFT_STAGE",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")(FREE_DATE>=\'{trade_date}\')(FREE_DATE<=\'{end_str}\')',
            "pageNumber": "1",
            "pageSize": "20",
            "sortColumns": "FREE_DATE",
            "sortTypes": "1",
        }
        r = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                   params=params, headers={"User-Agent": UA,
                       "Referer": "https://data.eastmoney.com/"}, timeout=15)
        d = r.json()
        rows = (d.get("result") or {}).get("data") or []
        upcoming = []
        for row in rows:
            upcoming.append({
                "date": str(row.get("FREE_DATE", ""))[:10],
                "type": row.get("FREE_SHARES_TYPE", ""),
                "shares_wan": float(row.get("FREE_SHARES", 0) or 0),
                "ratio_pct": round(float(row.get("FREE_RATIO", 0) or 0) * 100, 2),
            })
        return {
            "as_of": trade_date,
            "upcoming": upcoming,
            "n_upcoming": len(upcoming),
            "max_ratio_pct": max([u["ratio_pct"] for u in upcoming], default=0),
        }
    except Exception as e:
        return {"error": str(e)}


def fetch_dragon_tiger(code: str, trade_date: str = None, look_back: int = 30) -> dict:
    """龙虎榜近 30 日上榜情况。"""
    trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
    start_str = (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)).strftime("%Y-%m-%d")
    try:
        params = {
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": "ALL",
            "filter": f'(TRADE_DATE>=\'{start_str}\')(TRADE_DATE<=\'{trade_date}\')(SECURITY_CODE="{code}")',
            "pageNumber": "1",
            "pageSize": "50",
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
        }
        r = em_get("https://datacenter-web.eastmoney.com/api/data/v1/get",
                   params=params, headers={"User-Agent": UA,
                       "Referer": "https://data.eastmoney.com/"}, timeout=15)
        d = r.json()
        rows = (d.get("result") or {}).get("data") or []
        records = []
        for row in rows:
            records.append({
                "date": str(row.get("TRADE_DATE", ""))[:10],
                "reason": row.get("EXPLANATION", ""),
                "net_buy_wan": round(float(row.get("BILLBOARD_NET_AMT", 0) or 0) / 1e4, 1),
                "turnover_pct": round(float(row.get("TURNOVERRATE", 0) or 0), 2),
            })
        return {"records": records, "n_records": len(records)}
    except Exception as e:
        return {"error": str(e)}


def fetch_macro_snapshot() -> dict:
    """宏观快照：当日北向 + 行业 TOP5 + 强势股题材归因词频。"""
    out = {
        "hsgt": fetch_hsgt_realtime(),
        "industries": fetch_industry_ranking(10),
        "hot_stocks": fetch_thx_hot_stocks(),
    }
    # 题材词频
    top_tags = []
    if out["hot_stocks"]:
        tag_count = {}
        for hs in out["hot_stocks"]:
            for r in hs.get("reason", "").split("+"):
                r = r.strip()
                if r:
                    tag_count[r] = tag_count.get(r, 0) + 1
        top_tags = sorted(tag_count.items(), key=lambda x: x[1], reverse=True)[:8]
    out["hot_tags"] = top_tags
    return out


# ============================================================
# 量化评分引擎 V2 — 10 因子，100 分
# ============================================================
def compute_quant_score_v2(quote: dict, valuation: dict, blocks: list,
                           fund: dict, valuation_hist: dict, lockup: dict,
                           dragon: dict, macro: dict,
                           chip_data: dict = None, sw_data: dict = None) -> dict:
    """10 因子加权打分。"""
    factors = []

    # ---- 1. 趋势因子 12分 ----
    trend = 6  # 基础分
    price = quote.get("price", 0)
    prev = quote.get("prev_close", 0)
    change_pct = round((price - prev) / prev * 100, 2) if prev else 0
    if change_pct > 5: trend += 5; factors.append(f"涨{change_pct:+.1f}% +5")
    elif change_pct > 2: trend += 3; factors.append(f"涨{change_pct:+.1f}% +3")
    elif change_pct > 0: trend += 1; factors.append(f"涨{change_pct:+.1f}% +1")
    elif change_pct < -5: trend -= 5; factors.append(f"跌{change_pct:+.1f}% -5")
    elif change_pct < -2: trend -= 3; factors.append(f"跌{change_pct:+.1f}% -3")
    elif change_pct < 0: trend -= 1; factors.append(f"跌{change_pct:+.1f}% -1")
    # 低开高走加分
    op = quote.get("open", 0)
    if op and price > op * 1.02:
        trend += 1; factors.append("低开高走 +1")
    trend = max(0, min(trend, 12))

    # ---- 2. 估值因子 15分 ----
    val_score = 7  # 基础
    pe_ttm = quote.get("pe_ttm", 0) or valuation.get("pe_ttm", 0) or 0
    pb = quote.get("pb", 0) or valuation.get("pb", 0) or 0
    if pe_ttm > 0:
        if pe_ttm < 15: val_score += 5; factors.append(f"PE={pe_ttm:.1f}极低 +5")
        elif pe_ttm < 25: val_score += 3; factors.append(f"PE={pe_ttm:.1f}偏低 +3")
        elif pe_ttm < 40: val_score += 1; factors.append(f"PE={pe_ttm:.1f}适中 +1")
        elif pe_ttm < 80: val_score -= 1; factors.append(f"PE={pe_ttm:.1f}偏高 -1")
        else: val_score -= 3; factors.append(f"PE={pe_ttm:.1f}极高 -3")

    if pb > 0:
        if pb < 2: val_score += 3; factors.append(f"PB={pb:.2f}极低 +3")
        elif pb < 5: val_score += 2; factors.append(f"PB={pb:.2f}正常 +2")
        elif pb < 10: val_score += 0; factors.append(f"PB={pb:.2f}偏高 ±0")
        else: val_score -= 1; factors.append(f"PB={pb:.2f}极高 -1")

    # 一致预期消化
    digest = valuation.get("digest_years")
    if digest is not None and digest != 0:
        if digest <= 2: val_score += 3; factors.append(f"PE消化{digest}年(快速) +3")
        elif digest <= 4: val_score += 1; factors.append(f"PE消化{digest}年(合理) +1")
        elif digest > 4: val_score -= 2; factors.append(f"PE消化{digest}年(慢) -2")

    # PEG 校验
    peg = valuation.get("peg")
    if peg and peg != float("inf"):
        if peg < 1: val_score += 3; factors.append(f"PEG={peg}便宜 +3")
        elif peg < 1.5: val_score += 1; factors.append(f"PEG={peg}合理 +1")
        elif peg > 2: val_score -= 2; factors.append(f"PEG={peg}贵 -2")

    val_score = max(0, min(val_score, 15))

    # ---- 3. 估值分位因子 8分 (V3.7 新增) ----
    pct_score = 4  # 基础
    if "error" not in valuation_hist:
        pe_pct = valuation_hist.get("pe_percentile_3y")
        if pe_pct is not None:
            if pe_pct < 20:
                pct_score += 4; factors.append(f"PE历史分位{pe_pct}%(低位) +4")
            elif pe_pct < 50:
                pct_score += 2; factors.append(f"PE历史分位{pe_pct}%(中低) +2")
            elif pe_pct > 80:
                pct_score -= 3; factors.append(f"PE历史分位{pe_pct}%(高位) -3")
            elif pe_pct > 60:
                pct_score -= 1; factors.append(f"PE历史分位{pe_pct}%(偏高) -1")
            else:
                factors.append(f"PE历史分位{pe_pct}%(中位) ±0")
    else:
        factors.append(f"估值分位:{valuation_hist.get('error','N/A')[:30]} 0")
    pct_score = max(0, min(pct_score, 8))

    # ---- 4. 资金因子 15分 ----
    cap_score = 7
    if fund and "summary" in fund and fund["summary"]:
        s = fund["summary"]
        recent = s.get("recent_main_yi", 0)
        trend_str = s.get("trend", "")
        if recent > 1: cap_score += 6; factors.append(f"近30min主力流入{recent:.2f}亿 +6")
        elif recent > 0.3: cap_score += 3; factors.append(f"近30min主力流入{recent:.2f}亿 +3")
        elif recent > 0: cap_score += 1; factors.append(f"近30min主力微流入{recent:.2f}亿 +1")
        elif recent < -1: cap_score -= 4; factors.append(f"近30min主力流出{recent:.2f}亿 -4")
        elif recent < -0.3: cap_score -= 2; factors.append(f"近30min主力流出{recent:.2f}亿 -2")
        if "加速流入" in trend_str: cap_score += 2; factors.append(f"资金{trend_str} +2")
        elif "加速流出" in trend_str: cap_score -= 2; factors.append(f"资金{trend_str} -2")
    else:
        factors.append("资金流数据不可用 0")
    cap_score = max(0, min(cap_score, 15))

    # ---- 5. 动量因子 8分 ----
    mom_score = 4
    turnover = quote.get("turnover_rate", 0)
    vol_ratio = quote.get("vol_ratio", 1)
    amp = quote.get("amplitude", 0)
    if 2 < turnover < 8: mom_score += 2; factors.append(f"换手{turnover:.1f}%活跃 +2")
    elif turnover >= 10: mom_score -= 1; factors.append(f"换手{turnover:.1f}%过高 -1")
    elif turnover < 0.5: mom_score -= 1; factors.append(f"换手{turnover:.1f}%低迷 -1")
    if 1.2 < vol_ratio < 3: mom_score += 1; factors.append(f"量比{vol_ratio:.1f}温和放量 +1")
    elif vol_ratio > 5: mom_score -= 2; factors.append(f"量比{vol_ratio:.1f}异常 -2")
    if amp > 8: mom_score -= 2; factors.append(f"振幅{amp:.1f}%剧烈 -2")
    elif amp > 5: mom_score -= 1; factors.append(f"振幅{amp:.1f}%较大 -1")
    mom_score = max(0, min(mom_score, 8))

    # ---- 6. 情绪因子 8分 ----
    sent_score = 4
    hot_count = 0
    if isinstance(blocks, list):
        for b in blocks:
            if isinstance(b, dict) and "error" not in b:
                try:
                    if float(b.get("change_pct", 0) or 0) > 0:
                        hot_count += 1
                except (ValueError, TypeError):
                    pass
    if hot_count >= 5: sent_score += 3; factors.append(f"覆盖{hot_count}个上涨概念 +3")
    elif hot_count >= 2: sent_score += 1; factors.append(f"覆盖{hot_count}个上涨概念 +1")
    elif hot_count == 0: sent_score -= 1; factors.append("无热门概念 -1")
    # 北向
    hsgt = macro.get("hsgt", {})
    if hsgt.get("total_yi", 0) > 5: sent_score += 1; factors.append(f"北向净流入{hsgt.get('total_yi',0):.1f}亿 +1")
    elif hsgt.get("total_yi", 0) < -10: sent_score -= 1; factors.append(f"北向净流出{-hsgt.get('total_yi',0):.1f}亿 -1")
    sent_score = max(0, min(sent_score, 8))

    # ---- 7. 风险因子 10分 ----
    risk_score = 8  # 基础（高分起步，扣分制）
    mcap = quote.get("float_mcap", 0)
    if mcap > 0:
        if mcap > 1000: risk_score += 1; factors.append(f"大盘股({mcap:.0f}亿) +1")
        elif mcap < 50: risk_score -= 3; factors.append(f"小盘股({mcap:.0f}亿) -3")
        elif mcap < 200: risk_score -= 1; factors.append(f"中盘股({mcap:.0f}亿) -1")
    if pe_ttm > 100: risk_score -= 3; factors.append(f"PE>{pe_ttm:.0f}极高估 -3")
    if price < 3: risk_score -= 2; factors.append(f"低价股({price:.2f}元) -2")
    # 解禁风险
    if isinstance(lockup, dict) and "error" not in lockup:
        if lockup.get("max_ratio_pct", 0) > 5:
            risk_score -= 2; factors.append(f"未来90天最大解禁{lockup.get('max_ratio_pct',0):.1f}% -2")
        elif lockup.get("n_upcoming", 0) > 0:
            factors.append(f"未来90天有{lockup.get('n_upcoming',0)}批解禁 ±0")
    risk_score = max(0, min(risk_score, 10))

    # ---- 8. 筹码因子 8分 (V3.7 满血) ----
    chip_score = 4  # 基础分（中位）
    if chip_data and "error" not in chip_data:
        profit = chip_data.get("profit_ratio", 0.5)
        conc_90 = chip_data.get("concentration_90") or 0
        avg_cost = chip_data.get("avg_cost", 0)
        price = quote.get("price", 0)
        # 获利比例
        if profit > 0.7:
            chip_score += 2; factors.append(f"获利盘{profit*100:.1f}% +2")
        elif profit > 0.4:
            chip_score += 1; factors.append(f"获利盘{profit*100:.1f}% +1")
        elif profit < 0.2:
            chip_score -= 2; factors.append(f"套牢盘{(1-profit)*100:.1f}% -2")
        else:
            factors.append(f"获利盘{profit*100:.1f}% ±0")
        # 集中度
        if conc_90 < 0.15:
            chip_score += 2; factors.append(f"90%集中度{conc_90*100:.1f}%(极集中) +2")
        elif conc_90 < 0.30:
            chip_score += 1; factors.append(f"90%集中度{conc_90*100:.1f}%(集中) +1")
        elif conc_90 > 0.50:
            chip_score -= 1; factors.append(f"90%集中度{conc_90*100:.1f}%(发散) -1")
        # 套牢 vs 现价
        if price > 0 and avg_cost > 0:
            pct_off = (price - avg_cost) / avg_cost * 100
            if pct_off > 30:
                chip_score += 1; factors.append(f"现价高于均成本{pct_off:+.1f}% +1")
            elif pct_off < -20:
                chip_score -= 1; factors.append(f"现价低于均成本{pct_off:+.1f}% -1")
    else:
        err = chip_data.get("error", "N/A") if chip_data else "未拉取"
        factors.append(f"筹码: {err[:40]} 0")
    chip_score = max(0, min(chip_score, 8))

    # ---- 9. 申万稳定性 6分 (V3.7 满血) ----
    sw_score = 5  # 基础（默认稳定）
    if sw_data and "error" not in sw_data:
        n = sw_data.get("n_changes", 0)
        median = sw_data.get("median_changes", 5)
        is_churning = sw_data.get("is_churning", False)
        # 行业变更次数 vs 中位数
        if n <= 2:
            sw_score += 1; factors.append(f"行业变更{n}次(稳定) +1")
        elif n >= 7:
            sw_score -= 2; factors.append(f"行业变更{n}次(剧烈) -2")
        elif n >= 5:
            sw_score -= 1; factors.append(f"行业变更{n}次(偏多) -1")
        else:
            factors.append(f"行业变更{n}次(中位) ±0")
        # 偏离全市场中位数
        if is_churning and n > median * 1.5:
            sw_score -= 1; factors.append(f"行业变更远超市中位数({median}) -1")
        if sw_data.get("current_l1"):
            factors.append(f"当前申万一级:{sw_data['current_l1']} L2:{sw_data['current_l2']}")
    else:
        err = sw_data.get("error", "N/A") if sw_data else "未拉取"
        factors.append(f"申万: {err[:40]} 0")
    sw_score = max(0, min(sw_score, 6))

    # ---- 10. 龙虎榜因子 10分 ----
    dt_score = 5
    if isinstance(dragon, dict) and "error" not in dragon:
        n = dragon.get("n_records", 0)
        if n == 0: dt_score -= 1; factors.append("近30日未上龙虎榜 -1")
        elif n >= 3: dt_score += 3; factors.append(f"近30日上榜{n}次 +3")
        else: dt_score += 1; factors.append(f"近30日上榜{n}次 +1")
        # 净买入
        net_buys = [r.get("net_buy_wan", 0) for r in dragon.get("records", [])]
        if net_buys:
            avg_net = sum(net_buys) / len(net_buys)
            if avg_net > 1000: dt_score += 2; factors.append(f"龙虎榜均净买{avg_net:.0f}万 +2")
            elif avg_net < -1000: dt_score -= 3; factors.append(f"龙虎榜均净卖{-avg_net:.0f}万 -3")
    else:
        factors.append(f"龙虎榜:{dragon.get('error','N/A')[:30]} ±0")
    dt_score = max(0, min(dt_score, 10))

    total = trend + val_score + pct_score + cap_score + mom_score + sent_score + risk_score + chip_score + sw_score + dt_score

    return {
        "total": round(total, 1),
        "trend": trend, "valuation": val_score, "valuation_pctile": pct_score,
        "capital": cap_score, "momentum": mom_score, "sentiment": sent_score,
        "risk": risk_score, "chip": chip_score, "sw_stability": sw_score,
        "dragon": dt_score,
        "factors": factors,
        "change_pct": change_pct,
    }


def get_advice_v2(score: float) -> tuple:
    """评分 → 建议 (5 档)。"""
    if score >= 75: return ("强烈看多", "🟢🟢", "可重仓介入，止损设20日均线-3%，目标60-80%仓位")
    elif score >= 65: return ("看多", "🟢", "可逢调整建仓，仓位30-50%，设好止损")
    elif score >= 55: return ("中性偏多", "🟡", "观望为主，轻仓试探")
    elif score >= 45: return ("中性偏空", "🟡", "减仓观望，已有持仓设紧止损")
    elif score >= 35: return ("看空", "🔴", "建议减仓至轻仓或清仓")
    else: return ("强烈看空", "🔴🔴", "清仓回避，等底部放量企稳")


# ============================================================
# 主程序 — 单票 / 批量
# ============================================================
def analyze_single(code: str, name: str = "", output_md: bool = True,
                   out_dir: Optional[str] = None) -> dict:
    """单票完整分析。

    Args:
        code: 6 位股票代码
        name: 股票名称（可选, 留空则自动从腾讯接口获取）
        output_md: 是否输出报告 (Markdown + HTML)
        out_dir: 报告输出目录, 默认用模块级 OUT_DIR
    """
    code = normalize_code(code)
    print(f"\n>>> 单票分析: {code} {name}")
    print("=" * 70)

    # 1. 行情 + 估值（一次性）
    print("[1/8] 拉取腾讯实时行情...")
    quotes = fetch_tencent_quote([code])
    quote = quotes.get(code, {})
    if not quote or "error" in quote:
        return {"error": f"行情获取失败: {quote.get('error','未知')}"}

    print("[2/8] 计算估值（一致预期/PE消化/PEG）...")
    valuation = fetch_full_valuation(code)

    # 3. 板块归属
    print("[3/8] 拉取概念板块归属...")
    blocks = fetch_eastmoney_concept_blocks(code)

    # 4. 当日资金流
    print("[4/8] 拉取当日资金流...")
    fund = fetch_fund_flow_minute(code)

    # 5. 估值历史分位
    print("[5/10] 拉取估值历史（baostock）...")
    val_hist = fetch_valuation_history(code)

    # 6. 解禁预警
    print("[6/10] 拉取解禁日历...")
    lockup = fetch_lockup_expiry(code)

    # 7. 龙虎榜
    print("[7/10] 拉取龙虎榜近 30 日...")
    dragon = fetch_dragon_tiger(code)

    # 8. 宏观底色
    print("[8/10] 拉取宏观环境（北向/行业/强势股）...")
    macro = fetch_macro_snapshot()

    # 9. 筹码分布 (V3.7 满血)
    print("[9/10] 计算筹码分布（baostock前复权K线）...")
    chip_data = fetch_chip_distribution(code, lookback_days=180)

    # 10. 申万行业变迁 (V3.7 满血)
    print("[10/10] 拉取申万行业分类表...")
    sw_data = fetch_sw_stability(code)

    # 打分
    score = compute_quant_score_v2(quote, valuation, blocks, fund, val_hist,
                                   lockup, dragon, macro,
                                   chip_data=chip_data, sw_data=sw_data)
    advice, emoji, detail = get_advice_v2(score["total"])

    # 控制台输出
    print(f"\n{'='*70}")
    print(f"【{code} {quote.get('name') or name}】综合 {score['total']}分 {emoji}{advice}")
    print(f"  价格: {quote.get('price',0):.2f}  涨跌: {score['change_pct']:+.2f}%")
    print(f"  PE(TTM)={quote.get('pe_ttm',0):.1f}  PB={quote.get('pb',0):.2f}  "
          f"市值={quote.get('float_mcap',0):.0f}亿")
    if valuation.get("pe_fwd"):
        print(f"  一致预期: 当年EPS={valuation.get('eps_cur',0):.2f} "
              f"次年EPS={valuation.get('eps_next',0):.2f} "
              f"覆盖{valuation.get('analyst_count',0)}家")
        print(f"  前向PE={valuation.get('pe_fwd',0):.1f}x  "
              f"PEG={valuation.get('peg',0)}  "
              f"消化到30x={valuation.get('digest_years',0)}年")
    if "error" not in val_hist:
        print(f"  估值分位(3年): PE {val_hist.get('pe_percentile_3y',0)}% / "
              f"PB {val_hist.get('pb_percentile_3y',0)}%")
    print(f"\n  因子明细:")
    print(f"    趋势={score['trend']:>4}  估值={score['valuation']:>4}  "
          f"分位={score['valuation_pctile']:>4}  资金={score['capital']:>4}")
    print(f"    动量={score['momentum']:>4}  情绪={score['sentiment']:>4}  "
          f"风险={score['risk']:>4}  筹码={score['chip']:>4}")
    print(f"    申万={score['sw_stability']:>4}  龙虎榜={score['dragon']:>4}")
    print(f"  → {detail}")

    result = {
        "code": code, "name": quote.get("name") or name,
        "quote": quote, "valuation": valuation, "blocks": blocks,
        "fund": fund, "valuation_hist": val_hist, "lockup": lockup,
        "dragon": dragon, "macro": macro,
        "chip_data": chip_data, "sw_data": sw_data,
        "score": score, "advice": advice, "emoji": emoji, "detail": detail,
    }

    if output_md:
        md_path = write_markdown_report(result)
        print(f"\n  📝 Markdown 报告已写入: {md_path}")
        try:
            html_path = _write_html_report(
                result,
                interpret_pe_fn=_interpret_pe,
                interpret_pctile_fn=_interpret_pctile,
                interpret_chips_fn=_interpret_chips,
                make_trading_plan_fn=_make_trading_plan,
                make_signal_list_fn=_make_signal_list,
            )
            print(f"  🌐 HTML 报告已写入:    {html_path}")
        except Exception as e:
            import traceback
            print(f"  ❌ HTML 报告生成失败: {e}")
            traceback.print_exc()

    return result


def _interpret_pe(pe):
    """PE → 人话"""
    if pe <= 0 or pe > 500: return "PE 无效（亏损或极端值）"
    if pe < 15: return f"PE {pe:.1f} 极低，便宜"
    if pe < 25: return f"PE {pe:.1f} 偏低，估值有吸引力"
    if pe < 40: return f"PE {pe:.1f} 合理，可接受"
    if pe < 80: return f"PE {pe:.1f} 偏贵，要谨慎"
    return f"PE {pe:.1f} 极高估，泡沫风险"

def _interpret_pctile(pct):
    """PE 分位 → 人话（数字越小越便宜）"""
    if pct is None: return "无历史数据"
    if pct < 10: return f"在历史 {pct}% 分位（接近 3 年最低，便宜区间）"
    if pct < 30: return f"在历史 {pct}% 分位（便宜区间，可考虑）"
    if pct < 60: return f"在历史 {pct}% 分位（合理区间，不贵不便宜）"
    if pct < 80: return f"在历史 {pct}% 分位（偏贵区间，谨慎）"
    return f"在历史 {pct}% 分位（接近 3 年最高，泡沫区间）"

def _interpret_chips(cd):
    """筹码分布 → 人话"""
    if not cd or "error" in cd: return None
    out = []
    profit = cd["profit_ratio"]
    if profit > 0.7: out.append(f"🟢 {profit*100:.0f}% 持仓赚钱，套牢盘轻")
    elif profit > 0.4: out.append(f"🟡 {profit*100:.0f}% 持仓赚钱，正常")
    else: out.append(f"🔴 仅 {profit*100:.0f}% 持仓赚钱，套牢盘重")

    peak = cd["peak_price"]; price = cd["price"]
    pct_off_peak = (price - peak) / peak * 100
    if -3 < pct_off_peak < 8:
        out.append(f"🟢 现价距筹码峰 {pct_off_peak:+.1f}%（主成本区附近，主力没动）")
    elif pct_off_peak > 20:
        out.append(f"🔴 现价距筹码峰 {pct_off_peak:+.1f}%（远离主成本，主力可能已在更高位出货）")
    elif pct_off_peak < -10:
        out.append(f"🟡 现价低于筹码峰 {pct_off_peak:+.1f}%（回到主力成本区，下方有支撑）")
    else:
        out.append(f"🟡 现价距筹码峰 {pct_off_peak:+.1f}%")

    conc90 = (cd.get("concentration_90") or 0) * 100
    if conc90 < 15: out.append(f"🟢 90% 集中度 {conc90:.1f}% 极集中（容易拉升）")
    elif conc90 < 30: out.append(f"🟡 90% 集中度 {conc90:.1f}% 集中")
    else: out.append(f"🟡 90% 集中度 {conc90:.1f}% 发散（拉升难度大）")

    avg = cd["avg_cost"]
    pct_off_cost = (price - avg) / avg * 100
    if pct_off_cost > 10:
        out.append(f"🟡 现价高于平均成本 {pct_off_cost:+.1f}%（安全垫 {pct_off_cost:.1f}%）")
    elif pct_off_cost < -10:
        out.append(f"🟡 现价低于平均成本 {pct_off_cost:+.1f}%（下方有支撑）")
    else:
        out.append(f"🟢 现价接近平均成本 {pct_off_cost:+.1f}%（成本博弈区）")
    return out

def _make_trading_plan(q, v, cd, score_total):
    """生成具体买卖点位 + 仓位（核心:报告开头那个 "能不能买" 框）"""
    price = q.get("price", 0)
    if price <= 0: return None

    # 进场价: 现价 -3% ~ 现价之间
    entry_low = round(price * 0.97, 2)
    entry_high = price

    # 止损: 从筹码峰和平均成本里取更保守的（高的那个）
    stop_loss = round(price * 0.93, 2)
    if cd and "error" not in cd:
        peak = cd["peak_price"]
        avg = cd["avg_cost"]
        # 筹码峰和均成本哪个更接近现价，取下方 2% 作为止损
        support_candidate = min(peak, avg)
        candidate = round(support_candidate * 0.97, 2)
        # 不能让止损比 entry_low 高（不合理）
        if candidate < stop_loss and candidate > price * 0.85:
            stop_loss = candidate

    # 止盈: 三档（保守/中性/激进），基于现价 + 距离筹码峰
    take_profit_1 = round(price * 1.10, 2)   # +10%
    take_profit_2 = round(price * 1.25, 2)   # +25%
    take_profit_3 = round(price * 1.50, 2)   # +50%

    # 仓位: 由评分决定（65+ 满仓 30-50%，55-65 轻仓 20-30%，<55 不建议）
    if score_total >= 75: position = "可重仓 60-80%"
    elif score_total >= 65: position = "可建仓 30-50%"
    elif score_total >= 55: position = "轻仓试探 10-20%"
    elif score_total >= 45: position = "不进场 / 已有持仓减仓"
    else: position = "清仓回避"

    # 持仓周期: 看趋势分
    # 用一个简单规则: 评分高 + 一致预期 CAGR 高 → 中线
    cagr = (v.get("cagr_pct") or 0)
    if cagr >= 20: period = "中线 3-6 个月"
    elif cagr >= 10: period = "中线 1-3 个月"
    else: period = "短线 1-2 周"

    return {
        "entry_low": entry_low, "entry_high": entry_high,
        "stop_loss": stop_loss,
        "tp1": take_profit_1, "tp2": take_profit_2, "tp3": take_profit_3,
        "position": position, "period": period,
        "stop_loss_pct": round((1 - stop_loss/price) * 100, 1),
    }

def _make_signal_list(score, factors):
    """把因子分翻译成 "好消息/坏消息" 列表"""
    good = []; bad = []
    for f in factors:
        s = f.strip()
        if not s: continue
        # 解析 +N / -N
        import re as _re
        m = _re.search(r'([+\-]\d+)\s*$', s)
        if not m: continue
        val = int(m.group(1))
        if val > 0:
            # 提取原因（去掉分数）
            reason = _re.sub(r'\s*[+\-]\d+\s*$', '', s).strip()
            good.append(f"✅ {reason}")
        elif val < 0:
            reason = _re.sub(r'\s*[+\-]\d+\s*$', '', s).strip()
            bad.append(f"⚠️ {reason}")
    return good, bad


def write_markdown_report(r: dict) -> str:
    """输出单票 Markdown 报告 — 人话版（V2.2）
    结构: 一分钟结论 → 好/坏信号 → 详细数据 → 操作计划 → 三种情景
    """
    code = r["code"]; name = r["name"]
    q = r["quote"]; v = r["valuation"]; s = r["score"]
    score_total = s["total"]
    emoji = r["emoji"]; advice = r["advice"]

    outdir = os.path.join(OUT_DIR, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    path = f"{outdir}/{code}-{name}-{datetime.now().strftime('%H%M')}.md"

    # ========== 准备解读数据 ==========
    pe = q.get("pe_ttm", 0)
    pb = q.get("pb", 0)
    mcap = q.get("float_mcap", 0)
    price = q.get("price", 0)
    change_pct = s["change_pct"]

    pe_talk = _interpret_pe(pe)
    cd = r.get("chip_data")
    chips_talks = _interpret_chips(cd)
    plan = _make_trading_plan(q, v, cd, score_total)
    good_signals, bad_signals = _make_signal_list(s, s["factors"])

    lines = []

    # ============================================================
    # 0. 标题 + 一分钟结论 (最重要，第一眼看到)
    # ============================================================
    lines += [
        f"# {name} ({code})",
        f"",
        f"**{emoji} {advice}**  |  综合 **{score_total}/100 分**  |  "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')}  "
        f"价 {price:.2f}元 ({change_pct:+.2f}%)",
        f"",
        f"## 🎯 一分钟结论",
        f"",
    ]

    if plan:
        # 进场 / 仓位 / 止损 / 三档止盈
        action = "✅ 可以建仓" if score_total >= 65 else ("⚠️ 轻仓试探" if score_total >= 55 else "❌ 不建议进场")
        lines += [
            f"| 项目 | 我的建议 |",
            f"|------|----------|",
            f"| **判断** | {action}（{plan['position']}）|",
            f"| **分批进场价** | {plan['entry_low']:.2f} ~ {plan['entry_high']:.2f} 元 |",
            f"| **止损位** | {plan['stop_loss']:.2f} 元（跌 {plan['stop_loss_pct']:.1f}% 必走）|",
            f"| **第一止盈 (+10%)** | {plan['tp1']:.2f} 元 |",
            f"| **第二止盈 (+25%)** | {plan['tp2']:.2f} 元 |",
            f"| **第三止盈 (+50%)** | {plan['tp3']:.2f} 元 |",
            f"| **建议持仓周期** | {plan['period']} |",
            f"",
            f"> 💡 **操作口诀**：现价附近分两批进（如各 1/2 仓），跌到 {plan['stop_loss']:.2f} 必走，"
            f"涨到 {plan['tp1']:.2f} 先卖一半锁利，剩下一半等 {plan['tp2']:.2f} 或 {plan['tp3']:.2f}。",
            f"",
        ]

    # ============================================================
    # 1. 好消息 vs 坏消息 (一眼看到该不该动)
    # ============================================================
    lines += [f"## 👍 看好这票的理由"]
    if good_signals:
        for g in good_signals: lines.append(g)
    else:
        lines.append("(暂时没有发现明确的正面信号)")
    lines.append("")

    lines += [f"## 👎 要小心的信号"]
    if bad_signals:
        for b in bad_signals: lines.append(b)
    else:
        lines.append("✅ 没有发现明显负面信号")
    lines.append("")

    # ============================================================
    # 2. 估值人话解读 (PE / PB / 历史分位)
    # ============================================================
    lines += [f"## 💰 估值贵不贵"]
    lines += [f"- **{pe_talk}**"]
    if pb > 0:
        pb_talk = "PB 偏高" if pb > 10 else ("PB 偏高" if pb > 5 else "PB 偏低")
        lines.append(f"- **PB {pb:.2f}** — {pb_talk}")
    if v.get("peg") and v["peg"] != float("inf"):
        peg = v["peg"]
        peg_talk = "PEG < 1，便宜区" if peg < 1 else ("PEG 1~1.5，合理" if peg < 1.5 else "PEG > 1.5，偏贵")
        lines.append(f"- **PEG {peg}** — {peg_talk}")
    if v.get("digest_years") and v["digest_years"] > 0:
        d = v["digest_years"]
        d_talk = "2 年内消化完（便宜）" if d < 2 else ("2~4 年（合理）" if d < 4 else "4 年以上（太贵）")
        lines.append(f"- **PE 消化年数 {d}** — {d_talk}")
    if v.get("analyst_count"):
        cnt = v["analyst_count"]
        cov_talk = "覆盖足够" if cnt >= 5 else "覆盖较少，预期可能不准"
        lines.append(f"- **机构覆盖 {cnt} 家** — {cov_talk}")
    vh = r.get("valuation_hist", {})
    if "error" not in vh:
        pe_pct = vh.get("pe_percentile_3y")
        if pe_pct is not None:
            lines.append(f"- **PE 在过去 3 年里** — {_interpret_pctile(pe_pct)}")
    lines.append("")

    # ============================================================
    # 3. 筹码人话解读
    # ============================================================
    if chips_talks:
        lines += [f"## 🎰 主力和散户的筹码状态"]
        for t in chips_talks:
            lines.append(f"- {t}")
        lines.append("")

    # ============================================================
    # 4. 实时行情 (作为佐证，简短)
    # ============================================================
    lines += [
        f"## 📊 实时行情",
        f"",
        f"| 字段 | 值 |",
        f"|------|-----|",
        f"| 价格 | {price:.2f} 元（{change_pct:+.2f}%）|",
        f"| 涨跌停价 | {q.get('limit_up',0):.2f} / {q.get('limit_down',0):.2f} |",
        f"| 振幅 | {q.get('amplitude',0):.2f}% |",
        f"| 换手 | {q.get('turnover_rate',0):.2f}% |",
        f"| 量比 | {q.get('vol_ratio',0):.2f} |",
        f"| 流通市值 | {mcap:.2f} 亿 |",
        f"",
    ]

    # ============================================================
    # 5. 一致预期 (中线必看)
    # ============================================================
    if v.get("pe_fwd") or v.get("eps_cur"):
        lines += [
            f"## 🔮 机构怎么看的",
            f"",
            f"| 指标 | 值 | 说明 |",
            f"|------|-----|------|",
            f"| 覆盖机构 | {v.get('analyst_count',0)} 家 | {'≥5 家才算靠谱' if v.get('analyst_count',0) >= 5 else '<5 家预期不太准'} |",
            f"| 当年 EPS 预期 | {v.get('eps_cur','N/A')} | 2026 年赚多少 |",
            f"| 次年 EPS 预期 | {v.get('eps_next','N/A')} | 2027 年赚多少 |",
            f"| 预期增速 | {v.get('cagr_pct','N/A')}% | {'≥30% 是高增长' if (v.get('cagr_pct') or 0) >= 30 else '<30% 是普通增长'} |",
            f"| 前向 PE | {v.get('pe_fwd','N/A')} | 用明年预期利润算 |",
            f"| PEG | {v.get('peg','N/A')} | 越低越便宜 |",
            f"",
        ]

    # ============================================================
    # 6. 估值历史分位 (人话+数据)
    # ============================================================
    if "error" not in vh:
        lines += [
            f"## 📈 过去 3 年估值在啥位置",
            f"",
            f"| 指标 | 当前 | 3 年分位 | 历史区间 | 解读 |",
            f"|------|------|---------|---------|------|",
            f"| PE(TTM) | {vh.get('current_pe','?')} | {vh.get('pe_percentile_3y','?')}% | "
            f"{vh.get('pe_min','?')} ~ {vh.get('pe_max','?')} | {_interpret_pctile(vh.get('pe_percentile_3y'))} |",
            f"| PB(MRQ) | {vh.get('current_pb','?')} | {vh.get('pb_percentile_3y','?')}% | - | - |",
            f"",
        ]

    # ============================================================
    # 7. 筹码分布数据 (详细)
    # ============================================================
    if cd and "error" not in cd:
        lines += [
            f"## 🎰 筹码分布明细 ({cd.get('n_days','?')} 个交易日)",
            f"",
            f"| 指标 | 值 | 解释 |",
            f"|------|-----|------|",
            f"| 获利比例 | {cd['profit_ratio']*100:.2f}% | 现价之下持仓占比 |",
            f"| 平均成本 | {cd['avg_cost']:.2f} | 所有人持仓的平均价格 |",
            f"| 90% 成本区间 | {cd['cost_90'][0]:.2f} ~ {cd['cost_90'][1]:.2f} | 5%~95% 分位的持仓价 |",
            f"| 70% 成本区间 | {cd['cost_70'][0]:.2f} ~ {cd['cost_70'][1]:.2f} | 15%~85% 分位的持仓价 |",
            f"| 90% 集中度 | {cd['concentration_90']*100:.2f}% | <20% 算很集中 |",
            f"| 70% 集中度 | {cd['concentration_70']*100:.2f}% | - |",
            f"| 筹码峰 | {cd['peak_price']:.2f} 元 | 最密集的持仓价位 |",
            f"| 窗口累计换手 | {cd.get('total_turnover_pct','?')}% | {'换手充分，筹码可信' if (cd.get('total_turnover_pct',0) or 0) > 200 else '换手不够，筹码数据精度有限'} |",
            f"",
        ]
    elif cd and "error" in cd:
        lines += [f"## 🎰 筹码分布", f"", f"> ⚠️ {cd['error']}", f""]

    # ============================================================
    # 8. 申万行业
    # ============================================================
    sw = r.get("sw_data")
    if sw and "error" not in sw:
        n = sw.get("n_changes", 0)
        median = sw.get("median_changes", 0)
        sw_talk = "很稳定" if n <= 2 else ("稳定" if n <= median else "行业换过几次")
        lines += [
            f"## 🏭 申万行业分类",
            f"",
            f"- 当前一级行业代码: **{sw.get('current_l1', '?')}** （自 {sw.get('since', '?')} 起）",
            f"- 历史变更 {n} 次（中位数 {median}） — {sw_talk}",
            f"- {'⚠️ 行业换得太多，历史数据可能有偏差' if sw.get('is_churning') else ''}",
            f"",
        ]

    # ============================================================
    # 9. 因子得分 (技术细节，给好奇心重的)
    # ============================================================
    lines += [
        f"## 🔬 10 因子打分明细",
        f"",
        f"| 因子 | 得分 | 满分 |",
        f"|------|------|------|",
        f"| 趋势 | {s['trend']} | 12 |",
        f"| 估值 | {s['valuation']} | 15 |",
        f"| 估值分位 | {s['valuation_pctile']} | 8 |",
        f"| 资金 | {s['capital']} | 15 |",
        f"| 动量 | {s['momentum']} | 8 |",
        f"| 情绪 | {s['sentiment']} | 8 |",
        f"| 风险 | {s['risk']} | 10 |",
        f"| 筹码 | {s['chip']} | 8 |",
        f"| 申万稳定 | {s['sw_stability']} | 6 |",
        f"| 龙虎榜 | {s['dragon']} | 10 |",
        f"| **综合** | **{s['total']}** | **100** |",
        f"",
    ]

    # ============================================================
    # 10. 三种情景应对
    # ============================================================
    if plan:
        lines += [
            f"## 🎲 如果接下来…",
            f"",
            f"**…涨到 {plan['tp1']:.2f} (+10%)**：卖 1/2 仓锁利，留 1/2 看 {plan['tp2']:.2f}",
            f"",
            f"**…横盘不动**：观察一周，如果一直横在 {plan['entry_low']:.2f}~{price:.2f} 区间不破 "
            f"{plan['stop_loss']:.2f} 就继续持有；跌破止损线必走",
            f"",
            f"**…跌到 {plan['stop_loss']:.2f}**：必走，不留恋。可能的原因: 大盘崩、个股出利空、行业被砍",
            f"",
        ]

    lines += [
        f"---",
        f"",
        f"⚠️ 免责声明: 本报告基于公开数据的多因子量化模型生成，不构成投资建议。"
        f" 数据时点 {datetime.now().strftime('%Y-%m-%d %H:%M')}，市场随时变化，请独立判断。",
        f"",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return path


def analyze_batch(codes: list):
    """批量分析（保留 V1 接口兼容性）。"""
    print("=" * 90)
    print(f"           A 股多因子量化交易分析系统 V2  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 90)
    print("\n>>> 大盘环境扫描...")
    macro = fetch_macro_snapshot()
    hsgt = macro["hsgt"]
    if hsgt:
        print(f"  北向资金: 沪股通{hsgt.get('latest_hgt_yi',0):+.1f}亿 | "
              f"深股通{hsgt.get('latest_sgt_yi',0):+.1f}亿")
    if macro["industries"]:
        top3 = [f"{i['name']}({i['change_pct']}%)" for i in macro["industries"][:3]]
        print(f"  领涨板块: {', '.join(top3)}")
    if macro["hot_tags"]:
        tags_str = " | ".join(f"{t}({c}次)" for t, c in macro["hot_tags"][:5])
        print(f"  热门题材: {tags_str}")

    print(f"\n{'='*90}")
    print(f"  {'代码':<10} {'名称':<8} {'价格':<10} {'涨跌':<8} {'PE':<8} {'PB':<6} {'综合':<6} {'建议'}")

    results = []
    for code, name, tag in codes:
        code = normalize_code(code)
        r = analyze_single(code, name, output_md=False)
        if "error" in r:
            print(f"  {code:<10} {name:<8} 数据获取失败: {r['error']}")
            continue
        sc = r["score"]
        print(f"  {code:<10} {r['name']:<8} {r['quote'].get('price',0):<10.2f} "
              f"{sc['change_pct']:>+.2f}%   "
              f"{r['quote'].get('pe_ttm',0):<8.1f} {r['quote'].get('pb',0):<6.2f} "
              f"{sc['total']:<6} {r['emoji']}{r['advice']}")
        results.append(r)

    if results:
        results.sort(key=lambda x: x["score"]["total"], reverse=True)
        print(f"\n{'='*90}")
        print(f"  最终排序:")
        for i, r in enumerate(results, 1):
            print(f"  {i}. {r['code']} {r['name']:<6} {r['score']['total']}分 {r['emoji']}")
    print(f"\n  ⚠️ 免责声明: 基于公开数据的多因子量化模型，不构成投资建议。")


# ============================================================
# CLI 入口
# ============================================================
if __name__ == "__main__":
    if len(sys.argv) >= 2:
        # CLI: python script.py 001696 [name]
        code = sys.argv[1]
        name = sys.argv[2] if len(sys.argv) >= 3 else ""
        analyze_single(code, name, output_md=True)
    else:
        # 原批量模式
        analyze_batch([
            ("688017", "绿的谐波", "机器人龙头"),
            ("001696", "宗申动力", "低空经济/通机"),
            ("600519", "贵州茅台", "大盘蓝筹基准"),
            ("000858", "五粮液", "白酒蓝筹"),
            ("002463", "沪电股份", "PCB龙头"),
        ])