# A 股单票量化分析器 (a-stock-quant-analyzer)

> 把公开 A 股数据源（腾讯、同花顺、baostock、东财）封装成一个 **10 因子 / 100 分**的量化打分引擎，输出**人话版 HTML 报告**（单文件、离线可看、微信可发）。

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Data Sources](https://img.shields.io/badge/data%20sources-4-orange)

## 一句话介绍

```bash
python quant_analyzer_v2.py 001696 宗申动力
# → ./reports/2026-09-03/001696-宗申动力-1422.html
# → 打开看: 能不能买 / 买多少 / 什么价格止盈止损
```

## 安装

```bash
git clone https://github.com/Swartea/a-stock-quant-analyzer.git
cd a-stock-quant-analyzer

pip install -r requirements.txt

# (可选) 设置报告输出目录
export A_STOCK_OUT_DIR="./reports"   # 默认就是当前目录下的 reports/
```

**Python ≥ 3.9** （系统自带；不需要 conda；不需要 Docker）

## 使用

### 1. 单票分析（推荐）

```bash
# 方式一: 命令行
python quant_analyzer_v2.py 001696 宗申动力

# 方式二: 代码调用
from quant_analyzer_v2 import analyze_single
result = analyze_single("001696", "宗申动力")
print(result["advice"])  # "可逢调整建仓，仓位30-50%，设好止损"
```

输出两个文件：
- `reports/YYYY-MM-DD/CODE-NAME-HHMM.md` — 人话 Markdown
- `reports/YYYY-MM-DD/CODE-NAME-HHMM.html` — 带图表 HTML（**单文件 < 50 KB**）

### 2. 批量分析

直接编辑脚本末尾的 `analyze_batch([...])` 调用，把你想看的票填进去：

```python
analyze_batch([
    ("688017", "绿的谐波", "机器人"),
    ("001696", "宗申动力", "低空经济"),
    ("600519", "贵州茅台", "白酒蓝筹"),
])
```

### 3. 仅输出 HTML 报告（去掉 Markdown）

```python
analyze_single("001696", output_md=False)
```

## 报告长啥样

打开生成的 `.html` 文件，你会看到：

### 顶部 — 一句话结论
- 综合评分（10 因子加权 / 100 分）
- ✅/⚠️/❌ 进场建议
- 仓位建议（30-50% / 10-20% / 不进场）
- 持仓周期（中线 3-6 个月 / 短线 1-2 周）

### 第二屏 — 一分钟结论（人话版）
| 项目 | 我的建议 |
|------|----------|
| **判断** | ✅ 可以建仓（可建仓 30-50%）|
| **分批进场价** | 16.53 ~ 17.04 元 |
| **止损位** | 14.94 元（跌 12.3% 必走）|
| **第一止盈 (+10%)** | 18.74 元 |
| **第二止盈 (+25%)** | 21.30 元 |
| **第三止盈 (+50%)** | 25.56 元 |
| **建议持仓周期** | 中线 3-6 个月 |

### 第三屏 — 6 个 KPI 卡片
PE / PB / 前向 PE / 3 年分位 / 获利盘 / 换手量比 — 数字 + 人话

### 第四屏 — 👍 看好这票的理由 / 👎 要小心的信号
彩色 chip 列表，每条都有数据来源

### 第五屏 — 4 个图表（纯 SVG，零 JS）
- 📈 近 60 日 K 线（红涨绿跌）
- 🎰 筹码分布（红套牢/绿获利 + 筹码峰标记）
- 📊 近 6 月 PE 走势（3 年分位标记）
- 🔬 10 因子雷达

### 第六屏 — 🎲 三种情景应对
- 涨到第一止盈 → 卖 1/2 仓锁利
- 横盘不动 → 怎么等
- 跌到止损位 → 必走，不留恋

## 10 因子打分体系（100 分）

| 因子 | 满分 | 数据来源 |
|------|------|----------|
| 趋势 | 12 | 腾讯实时行情 |
| 估值 | 15 | 腾讯 + 同花顺一致预期 EPS |
| 估值分位 | 8 | baostock 3 年历史 PE/PB |
| 资金 | 15 | 东财 push2 当日分钟级 |
| 动量 | 8 | 腾讯实时 |
| 情绪 | 8 | 同花顺北向 + 当日强势股 |
| 风险 | 10 | 腾讯 + 东财解禁日历 |
| 筹码 | 8 | baostock 前复权 K 线 + 本地算法 |
| 申万稳定 | 6 | 申万行业分类历史 |
| 龙虎榜 | 10 | 东财龙虎榜近 30 日 |

## 数据源（全部零鉴权）

| 源 | 用途 | 风控 |
|----|------|------|
| 腾讯财经 `qt.gtimg.cn` | 实时行情 / PE / PB / 换手 | 不封 IP |
| 同花顺 `basic.10jqka.com.cn` | 一致预期 EPS / 北向 / 强势股 | 偶发风控，加 UA + Referer |
| baostock (TCP) | 估值历史 / 筹码 K 线 | 国内 IP 才稳；不支持北交所 |
| 东财 `push2.eastmoney.com` | 概念板块 / 资金流 / 解禁 / 龙虎榜 | 内置串行限流（≥1.2s）+ 会话复用；部分大陆住宅 IP 间歇风控 |

> ⚠️ **不要把本工具当投资建议使用**。所有决策由你独立判断。本项目仅做数据获取和量化展示。

## 已知限制

- **北交所 (4/8/92/920 号段)**：baostock 不支持，建议改用腾讯快照
- **ETF / 指数**：腾讯接口支持，但 PE/PB 字段含义与个股不同
- **沙盒 / 海外网络**：腾讯/同花顺通常能通；baostock TCP 需要国内 IP；东财敏感网络会被拒（HTTP 000/空）
- **东财接口**：每天可能有 1-2 个失效，每次发版会跟进

## 文件结构

```
a-stock-quant-analyzer/
├── quant_analyzer_v2.py   # 主程序：CLI + Python API
├── html_report.py         # HTML 报告生成器（4 个 SVG 图）
├── requirements.txt
├── LICENSE
└── README.md
```

## 开发

```bash
# 跑测试
python quant_analyzer_v2.py 001696 宗申动力

# 看代码细节
# - 10 因子打分: quant_analyzer_v2.py::compute_quant_score_v2
# - 人话解读:    quant_analyzer_v2.py::_interpret_pe / _interpret_pctile / _interpret_chips
# - 交易计划:    quant_analyzer_v2.py::_make_trading_plan
# - SVG 图表:    html_report.py::_svg_kline / _svg_chip_histogram / _svg_pe_history / _svg_radar
```

## License

MIT — 自由使用，注明出处即可。

## 致谢

数据来源：
- 腾讯财经 — https://stockapp.finance.qq.com
- 同花顺 — https://www.10jqka.com.cn
- baostock — http://baostock.com
- 东财 — https://eastmoney.com

> 本项目仅做数据获取和量化展示，**不构成任何投资建议**。股市有风险，投资需谨慎。