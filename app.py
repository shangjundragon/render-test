"""
美股期权权利金收益率计算器（多股票对比版）
使用 yfinance 获取期权链数据，计算自定义指标
"""
import json
import time
import random
import warnings
import os
from datetime import datetime, date

# 在导入其他库之前抑制所有警告
os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

# 抑制 yfinance/urllib3 日志
import logging
logging.getLogger("yfinance").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("peewee").setLevel(logging.ERROR)

import yfinance as yf

# 自定义 JSON Encoder，处理 NaN/Infinity
import math as _math
from flask.json.provider import DefaultJSONProvider

class SafeJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)

    @staticmethod
    def _clean(obj):
        """递归清理 NaN/Infinity 值"""
        if isinstance(obj, float):
            if _math.isnan(obj) or _math.isinf(obj):
                return 0.0
            return obj
        elif isinstance(obj, dict):
            return {k: SafeJSONProvider._clean(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return [SafeJSONProvider._clean(v) for v in obj]
        return obj

app = Flask(__name__, static_folder="static")
app.json = SafeJSONProvider(app)
CORS(app)

# 全局配置
MAX_RETRIES = 3          # 每个期权链最大重试次数
RETRY_DELAY = 3          # 重试等待秒数
FETCH_DELAY = 1.5        # 每个到期日之间间隔秒数
STOCK_DELAY = 3          # 每只股票之间间隔秒数


def safe_float(val, default=0.0):
    """安全转换浮点数，处理 NaN/Infinity/None 等"""
    import math
    if val is None:
        return default
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (ValueError, TypeError):
        return default


def fetch_option_chain_with_retry(ticker, exp_str, option_type):
    """带重试的期权链获取"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            opt_chain = ticker.option_chain(exp_str)
            chain = opt_chain.calls if option_type == "calls" else opt_chain.puts
            return chain, None
        except Exception as e:
            last_error = str(e)
            err_lower = last_error.lower()
            is_rate_limit = any(kw in err_lower for kw in [
                "rate limit", "too many requests", "429", "jsondecode",
                "timeout", "connection", "not found"
            ])
            if is_rate_limit and attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (attempt + 1) + random.uniform(0.5, 1.5)
                print(f"[yfinance] {ticker.ticker} {exp_str} 限流/网络错误，{wait:.1f}s 后重试 (第{attempt+1}次)...")
                time.sleep(wait)
            else:
                print(f"[yfinance] {ticker.ticker} {exp_str}: {last_error}")
                break
    return None, last_error


def fetch_single_stock(symbol, risk_free_rate, days_min, days_max, option_type, selected_exp=None):
    """获取单只股票的期权数据"""
    symbol = symbol.strip().upper()
    
    # 尝试创建 Ticker 对象（带重试）
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            ticker = yf.Ticker(symbol)
            stock_info = ticker.info
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY + random.uniform(0.5, 1.5)
                print(f"[yfinance] {symbol} info 获取失败，{wait:.1f}s 后重试...")
                time.sleep(wait)
            else:
                raise Exception(f"无法获取 {symbol} 股票信息: {last_error}")

    current_price = safe_float(stock_info.get("currentPrice")
                               or stock_info.get("regularMarketPrice")
                               or stock_info.get("regularMarketPreviousClose")
                               or stock_info.get("previousClose"), 0)

    company_name = stock_info.get("shortName") or stock_info.get("longName") or symbol

    # 获取期权到期日列表（带重试）
    expiration_dates = None
    for attempt in range(MAX_RETRIES):
        try:
            expiration_dates = ticker.options
            break
        except Exception as e:
            last_error = str(e)
            if attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY + random.uniform(0.5, 1.5)
                print(f"[yfinance] {symbol} options 获取失败，{wait:.1f}s 后重试...")
                time.sleep(wait)
            else:
                return {
                    "symbol": symbol,
                    "company_name": company_name,
                    "current_price": round(current_price, 2),
                    "error": f"获取 {symbol} 期权列表失败: {last_error}",
                    "total_records": 0,
                    "results": [],
                }

    if not expiration_dates:
        return {
            "symbol": symbol,
            "company_name": company_name,
            "current_price": round(current_price, 2),
            "error": f"未找到 {symbol} 的期权数据",
            "total_records": 0,
            "results": [],
        }

    today = date.today()
    results = []
    fetch_count = 0  # 实际请求计数

    # 如果指定了到期日，只查那一天；否则按天数范围过滤
    if selected_exp:
        target_dates = [selected_exp]
    else:
        target_dates = expiration_dates

    for exp_str in target_dates:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        days_to_expiry = (exp_date - today).days

        if days_to_expiry <= 0:
            continue

        # 未指定到期日时，按天数范围过滤
        if not selected_exp:
            if days_to_expiry < days_min or days_to_expiry > days_max:
                continue

        # 请求前等待（除第一个外）
        if fetch_count > 0:
            time.sleep(FETCH_DELAY)
        fetch_count += 1

        chain, err = fetch_option_chain_with_retry(ticker, exp_str, option_type)
        if chain is None:
            continue

        for _, row in chain.iterrows():
            strike = safe_float(row.get("strike"), 0)
            if strike <= 0:
                continue

            premium = safe_float(row.get("lastPrice"), 0)
            if premium <= 0:
                bid = safe_float(row.get("bid"), 0)
                ask = safe_float(row.get("ask"), 0)
                if bid > 0 and ask > 0:
                    premium = (bid + ask) / 2
                elif bid > 0:
                    premium = bid
                elif ask > 0:
                    premium = ask
                else:
                    continue

            denominator = strike - premium
            if denominator <= 0:
                continue

            # 只收集数据，后续筛选"现价下方最近档位"
            results.append({
                "symbol": symbol,
                "expiration_date": exp_str,
                "days_to_expiry": days_to_expiry,
                "strike": round(strike, 2),
                "premium": round(premium, 4),
                "last_price": round(safe_float(row.get("lastPrice"), 0), 4),
                "bid": round(safe_float(row.get("bid"), 0), 4),
                "ask": round(safe_float(row.get("ask"), 0), 4),
                "volume": safe_float(row.get("volume"), 0),
                "open_interest": safe_float(row.get("openInterest"), 0),
                "implied_volatility": round(safe_float(row.get("impliedVolatility"), 0), 4),
            })

    # 筛选：每个到期日取最接近现价的行权价
    # 规则：先找下方最近的行权价（OTM），如果差距在 2% 以内就用下方的；
    # 如果差距超过 2%，改取上方最近的行权价（ITM），避免像 PDD 差太大
    GAP_THRESHOLD = 0.02  # 2%
    results.sort(key=lambda x: (x["expiration_date"], x["strike"]))  # 按日期升序、行权价升序
    filtered = {}
    for r in results:
        exp = r["expiration_date"]
        if exp in filtered:
            # 行权价升序，不断更新直到超过现价为止
            if r["strike"] <= current_price:
                filtered[exp] = r  # 更新为最接近现价的下方档位
            elif r["strike"] > current_price:
                # 出现了第一个高于现价的档位
                below = filtered.get(exp)  # 当前已记录的下方最近档位
                if below is not None:
                    below_strike = below["strike"]
                    above_strike = r["strike"]
                    gap_below = (current_price - below_strike) / current_price
                    # 如果下方档位差距 <= 2%，用下方；否则用上方
                    if gap_below <= GAP_THRESHOLD:
                        filtered[exp] = below  # 保持下方
                    else:
                        filtered[exp] = r  # 改用上方（第一个高于现价的）
                else:
                    filtered[exp] = r  # 没有下方档位，直接用上方第一个
                # 记录后不再更新该到期日
                filtered[exp + "__locked"] = True
        elif r["strike"] <= current_price:
            filtered[exp] = r  # 初始化：记录遇到的第一个下方档位

    # 清理 lock 标记
    filtered = {k: v for k, v in filtered.items() if not k.endswith("__locked")}
    filtered_results = list(filtered.values())
    filtered_results.sort(key=lambda x: x["expiration_date"])

    # 计算指标
    risk_free_rate = 0.038  # 国债利率固定 3.8%
    for r in filtered_results:
        strike = r["strike"]
        premium = r["premium"]
        days = r["days_to_expiry"]
        denominator = strike - premium
        if denominator > 0:
            base_return = premium / denominator
            # 单笔收益 = 基础回报 + 按天折算的国债利率
            single_return = base_return + risk_free_rate * (days / 365.0)
            # 年化收益 = 基础回报年化 + 国债利率
            annualized = base_return * (365.0 / days) + risk_free_rate
        else:
            single_return = 0
            annualized = 0
        r["single_return"] = round(single_return, 4)       # 单笔收益（含国债）
        r["annualized_rate"] = round(annualized, 4)         # 年化收益（含国债）

    total_records = len(filtered_results)
    if filtered_results:
        avg_indicator = sum(r["annualized_rate"] for r in filtered_results) / total_records
        max_item = max(filtered_results, key=lambda x: x["annualized_rate"])
        min_item = min(filtered_results, key=lambda x: x["annualized_rate"])
    else:
        avg_indicator = max_item = min_item = None

    # 如果指定日期无结果，扫描所有到期日，找出哪些有下方档位
    available_dates = None
    if selected_exp and not filtered_results:
        available_dates = []
        for exp_str in expiration_dates:
            exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            days = (exp_date - today).days
            if days <= 0:
                continue
            # 检查该到期日是否有低于现价的档位（在 results 中查找）
            has_below = any(
                r["expiration_date"] == exp_str and r["strike"] < current_price
                for r in results
            )
            if has_below:
                # 找到最近下方档位
                below_strikes = sorted([
                    r["strike"] for r in results
                    if r["expiration_date"] == exp_str and r["strike"] < current_price
                ], reverse=True)
                if below_strikes:
                    available_dates.append({
                        "date": exp_str,
                        "days": days,
                        "label": f"{exp_str}（{days}天后）",
                        "best_strike": round(below_strikes[0], 2)
                    })

    error_msg = None
    if selected_exp and not filtered_results:
        if available_dates:
            error_msg = f"该日期 ({selected_exp}) 无符合条件的期权（现价下方无档位），但以下日期有："
        else:
            error_msg = f"该日期 ({selected_exp}) 无符合条件的期权，且其他日期也暂未找到下方档位"

    return {
        "symbol": symbol,
        "company_name": company_name,
        "current_price": round(current_price, 2),
        "risk_free_rate": risk_free_rate,
        "option_type": option_type,
        "days_range": f"{days_min} - {days_max}",
        "days_min": days_min,
        "days_max": days_max,
        "total_records": total_records,
        "avg_indicator": round(avg_indicator, 4) if avg_indicator is not None else 0,
        "max_indicator": max_item["annualized_rate"] if max_item else 0,
        "max_indicator_item": max_item,
        "min_indicator": min_item["annualized_rate"] if min_item else 0,
        "min_indicator_item": min_item,
        "results": filtered_results,
        "available_dates": available_dates,
        "error": error_msg,
    }


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/expirations", methods=["GET"])
def get_expirations():
    """获取某只股票的期权到期日列表"""
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "请提供股票代码"}), 400

    for attempt in range(MAX_RETRIES):
        try:
            ticker = yf.Ticker(symbol)
            expirations = ticker.options
            if not expirations:
                return jsonify({"error": f"未找到 {symbol} 的期权数据", "expirations": []})
            today = date.today()
            # 只返回未来的到期日，并附带距离天数
            result = []
            for exp_str in expirations:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                days = (exp_date - today).days
                if days > 0:
                    result.append({
                        "date": exp_str,
                        "days": days,
                        "label": f"{exp_str}（{days}天后）"
                    })
            return jsonify({"symbol": symbol, "expirations": result})
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
            else:
                return jsonify({"error": f"获取失败: {str(e)}", "expirations": []})
    return jsonify({"error": "获取失败", "expirations": []})


@app.route("/api/fetch-options", methods=["POST"])
def fetch_options():
    """
    批量获取多只股票的期权数据
    请求参数:
        stocks: [
            { symbol, days_min?, days_max?, option_type? },
            ...
        ]
        risk_free_rate: 全局国债利率（%）
    """
    data = request.get_json() or {}
    stocks = data.get("stocks", [])
    risk_free_rate = safe_float(data.get("risk_free_rate"), 4.5)

    if not stocks:
        # 兼容旧版单股票请求
        symbol = (data.get("symbol") or "").strip().upper()
        if symbol:
            stocks = [{"symbol": symbol}]
        else:
            return jsonify({"error": "请至少添加一只股票"}), 400

    results_list = []
    for i, stock in enumerate(stocks):
        symbol = (stock.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        days_min = int(stock.get("days_min", 0))
        days_max = int(stock.get("days_max", 1000))
        option_type = stock.get("option_type", "puts")  # 默认看跌期权

        if days_min < 0:
            days_min = 0
        if days_max < days_min:
            days_max = days_min + 1

        try:
            selected_exp = stock.get("selected_exp")  # 用户选择的到期日
            result = fetch_single_stock(symbol, risk_free_rate, days_min, days_max, option_type, selected_exp)
            results_list.append(result)
        except Exception as e:
            err_msg = str(e)
            if "Rate limited" in err_msg or "Too Many Requests" in err_msg:
                results_list.append({
                    "symbol": symbol,
                    "company_name": symbol,
                    "current_price": 0,
                    "error": "Yahoo Finance 限流，请稍后重试",
                    "total_records": 0,
                    "results": [],
                })
            else:
                results_list.append({
                    "symbol": symbol,
                    "company_name": symbol,
                    "current_price": 0,
                    "error": f"获取失败: {err_msg}",
                    "total_records": 0,
                    "results": [],
                })

        # 多只股票之间间隔
        if i < len(stocks) - 1:
            time.sleep(STOCK_DELAY)

    # 汇总所有股票的期权结果（用于统一对比表）
    all_results = []
    for r in results_list:
        all_results.extend(r["results"])

    all_results.sort(key=lambda x: (x["symbol"], x["expiration_date"], x["strike"]))

    return jsonify({
        "risk_free_rate": risk_free_rate,
        "stock_count": len(stocks),
        "stocks": results_list,
        "total_records": len(all_results),
        "all_results": all_results,
    })


@app.route("/api/search-symbol", methods=["GET"])
def search_symbol():
    query = (request.args.get("q") or "").strip()
    if not query or len(query) < 1:
        return jsonify([])

    # 全量美股列表（含中文名称），覆盖主要交易品种
    common_stocks = {
        # ===== 科技七巨头 =====
        "AAPL":  ["Apple Inc.", "苹果"],
        "MSFT":  ["Microsoft Corporation", "微软"],
        "GOOGL": ["Alphabet Inc. (Google)", "谷歌"],
        "AMZN":  ["Amazon.com, Inc.", "亚马逊"],
        "META":  ["Meta Platforms, Inc.", "脸书"],
        "TSLA":  ["Tesla, Inc.", "特斯拉"],
        "NVDA":  ["NVIDIA Corporation", "英伟达"],

        # ===== 热门中概股 =====
        "BABA":  ["Alibaba Group", "阿里巴巴"],
        "JD":    ["JD.com", "京东"],
        "PDD":   ["PDD Holdings", "拼多多"],
        "BIDU":  ["Baidu Inc.", "百度"],
        "NIO":   ["NIO Inc.", "蔚来"],
        "XPEV":  ["XPeng Inc.", "小鹏汽车"],
        "LI":    ["Li Auto", "理想汽车"],
        "BILI":  ["Bilibili Inc.", "哔哩哔哩"],
        "TME":   ["Tencent Music", "腾讯音乐"],
        "TCOM":  ["Trip.com Group", "携程"],
        "NTES":  ["NetEase Inc.", "网易"],
        "ZTO":   ["ZTO Express", "中通快递"],
        "BEKE":  ["KE Holdings", "贝壳找房"],
        "YUMC":  ["Yum China", "百胜中国"],
        "FUTU":  ["Futu Holdings", "富途"],
        "TIGR":  ["UP Fintech", "老虎证券"],
        "IQ":    ["iQIYI Inc.", "爱奇艺"],
        "WB":    ["Weibo Corporation", "微博"],
        "ATHM":  ["Autohome Inc.", "汽车之家"],
        "ZH":    ["Zhihu Inc.", "知乎"],
        "DADA":  ["Dada Nexus", "达达"],
        "GDS":   ["GDS Holdings", "万国数据"],
        "VIPS":  ["Vipshop Holdings", "唯品会"],
        "RLX":   ["RLX Technology", "雾芯科技"],
        "MNSO":  ["MINISO Group", "名创优品"],
        "DQ":    ["Daqo New Energy", "大全新能源"],
        "JKS":   ["JinkoSolar", "晶科能源"],
        "HTHT":  ["H World Group", "华住"],
        "YY":    ["JOYY Inc.", "欢聚"],
        "MOMO":  ["Hello Group", "挚文集团"],
        "FINV":  ["FinVolution Group", "信也科技"],

        # ===== 半导体/AI 芯片 =====
        "AMD":   ["Advanced Micro Devices", "超威"],
        "INTC":  ["Intel Corporation", "英特尔"],
        "QCOM":  ["Qualcomm Inc.", "高通"],
        "AVGO":  ["Broadcom Inc.", "博通"],
        "MRVL":  ["Marvell Technology", "迈威尔"],
        "MU":    ["Micron Technology", "美光"],
        "ARM":   ["Arm Holdings", "安谋"],
        "TXN":   ["Texas Instruments", "德州仪器"],
        "ADI":   ["Analog Devices", "亚德诺"],
        "AMAT":  ["Applied Materials", "应用材料"],
        "LRCX":  ["Lam Research", "拉姆研究"],
        "KLAC":  ["KLA Corporation", "科磊"],
        "ASML":  ["ASML Holding", "阿斯麦"],
        "TSM":   ["Taiwan Semiconductor", "台积电"],

        # ===== 软件/云计算/SaaS =====
        "ADBE":  ["Adobe Inc.", "奥多比"],
        "CRM":   ["Salesforce Inc.", "赛富时"],
        "ORCL":  ["Oracle Corporation", "甲骨文"],
        "SNOW":  ["Snowflake Inc.", "雪花"],
        "NOW":   ["ServiceNow Inc.", "现在服务"],
        "WDAY":  ["Workday Inc.", "工作日"],
        "TEAM":  ["Atlassian Corp.", "亚特兰蒂"],
        "DDOG":  ["Datadog Inc.", "数据狗"],
        "CRWD":  ["CrowdStrike Holdings", "群体打击"],
        "ZS":    ["Zscaler Inc.", "零信任安全"],
        "NET":   ["Cloudflare Inc.", "云盾"],
        "MDB":   ["MongoDB Inc.", "蒙戈数据库"],
        "PLTR":  ["Palantir Technologies", "帕兰提尔"],
        "U":     ["Unity Software", "团结引擎"],
        "RBLX":  ["Roblox Corporation", "罗布乐思"],

        # ===== 互联网/社交媒体 =====
        "NFLX":  ["Netflix, Inc.", "奈飞"],
        "SPOT":  ["Spotify Technology", "声田"],
        "SNAP":  ["Snap Inc.", "色拉布"],
        "PINS":  ["Pinterest Inc.", "品趣"],
        "RDDT":  ["Reddit Inc.", "红迪"],
        "MTCH":  ["Match Group", "火柴集团"],

        # ===== 金融科技/支付 =====
        "V":     ["Visa Inc.", "维萨"],
        "MA":    ["Mastercard Inc.", "万事达"],
        "PYPL":  ["PayPal Holdings", "贝宝"],
        "SQ":    ["Block Inc.", "方块"],
        "COIN":  ["Coinbase Global", "比特币基地"],
        "SOFI":  ["SoFi Technologies", "索菲"],
        "AFRM":  ["Affirm Holdings", "先买后付"],
        "HOOD":  ["Robinhood Markets", "罗宾汉"],

        # ===== 金融/银行 =====
        "JPM":   ["JPMorgan Chase & Co.", "摩根大通"],
        "BAC":   ["Bank of America", "美国银行"],
        "WFC":   ["Wells Fargo", "富国银行"],
        "C":     ["Citigroup Inc.", "花旗银行"],
        "GS":    ["Goldman Sachs", "高盛"],
        "MS":    ["Morgan Stanley", "摩根士丹利"],
        "BLK":   ["BlackRock Inc.", "贝莱德"],
        "SCHW":  ["Charles Schwab", "嘉信理财"],

        # ===== 电商/零售 =====
        "WMT":   ["Walmart Inc.", "沃尔玛"],
        "COST":  ["Costco Wholesale", "好市多"],
        "HD":    ["Home Depot", "家得宝"],
        "LOW":   ["Lowe's Companies", "劳氏"],
        "TGT":   ["Target Corporation", "塔吉特"],
        "SHOP":  ["Shopify Inc.", "电商平台"],
        "AMZN":  ["Amazon.com, Inc.", "亚马逊"],
        "MELI":  ["MercadoLibre", "拉美电商"],
        "ETSY":  ["Etsy Inc.", "手工电商"],
        "CPNG":  ["Coupang Inc.", "韩国电商"],

        # ===== 出行/共享经济 =====
        "UBER":  ["Uber Technologies", "优步"],
        "LYFT":  ["Lyft Inc.", "来福车"],
        "DASH":  ["DoorDash Inc.", "外卖送餐"],
        "ABNB":  ["Airbnb Inc.", "爱彼迎"],

        # ===== 新能源车 =====
        "RIVN":  ["Rivian Automotive", "里维安"],
        "LCID":  ["Lucid Group", "路西德"],
        "F":     ["Ford Motor", "福特"],
        "GM":    ["General Motors", "通用汽车"],
        "TM":    ["Toyota Motor", "丰田"],
        "HMC":   ["Honda Motor", "本田"],
        "STLA":  ["Stellantis N.V.", "斯特兰蒂斯"],
        "FERRARI": ["Ferrari N.V.", "法拉利"],

        # ===== 生物医药 =====
        "PFE":   ["Pfizer Inc.", "辉瑞"],
        "MRNA":  ["Moderna Inc.", "莫德纳"],
        "LLY":   ["Eli Lilly", "礼来"],
        "UNH":   ["UnitedHealth Group", "联合健康"],
        "JNJ":   ["Johnson & Johnson", "强生"],
        "ABBV":  ["AbbVie Inc.", "艾伯维"],
        "MRK":   ["Merck & Co.", "默沙东"],
        "BMY":   ["Bristol-Myers Squibb", "百时美施贵宝"],
        "GILD":  ["Gilead Sciences", "吉利德"],
        "AMGN":  ["Amgen Inc.", "安进"],
        "REGN":  ["Regeneron Pharmaceuticals", "再生元"],
        "BIIB":  ["Biogen Inc.", "渤健"],
        "VRTX":  ["Vertex Pharmaceuticals", "顶点制药"],
        "NVO":   ["Novo Nordisk", "诺和诺德"],

        # ===== 消费/餐饮 =====
        "PG":    ["Procter & Gamble", "宝洁"],
        "KO":    ["Coca-Cola", "可口可乐"],
        "PEP":   ["PepsiCo", "百事"],
        "MCD":   ["McDonald's", "麦当劳"],
        "SBUX":  ["Starbucks", "星巴克"],
        "NKE":   ["Nike Inc.", "耐克"],
        "DIS":   ["The Walt Disney Company", "迪士尼"],
        "CMG":   ["Chipotle Mexican Grill", "奇波雷"],
        "YUM":   ["Yum! Brands", "百胜餐饮"],
        "DPZ":   ["Domino's Pizza", "达美乐"],

        # ===== 能源 =====
        "XOM":   ["Exxon Mobil", "埃克森美孚"],
        "CVX":   ["Chevron Corporation", "雪佛龙"],
        "OXY":   ["Occidental Petroleum", "西方石油"],
        "COP":   ["ConocoPhillips", "康菲石油"],
        "BP":    ["BP p.l.c.", "英国石油"],
        "SHEL":  ["Shell plc", "壳牌"],
        "SLB":   ["Schlumberger Limited", "斯伦贝谢"],
        "HAL":   ["Halliburton Company", "哈里伯顿"],

        # ===== 航空/国防/工业 =====
        "BA":    ["Boeing Company", "波音"],
        "CAT":   ["Caterpillar Inc.", "卡特彼勒"],
        "GE":    ["General Electric", "通用电气"],
        "RTX":   ["RTX Corporation", "雷神"],
        "LMT":   ["Lockheed Martin", "洛克希德马丁"],
        "DE":    ["Deere & Company", "约翰迪尔"],
        "UPS":   ["United Parcel Service", "联合包裹"],
        "FDX":   ["FedEx Corporation", "联邦快递"],

        # ===== 量子计算 =====
        "IONQ":  ["IonQ Inc.", "离子量子计算"],
        "RGTI":  ["Rigetti Computing", "里盖蒂量子计算"],
        "QBTS":  ["D-Wave Quantum", "D波量子"],
        "QUBT":  ["Quantum Computing Inc.", "量子计算"],

        # ===== 航天 =====
        "ASTS":  ["AST SpaceMobile", "太空移动"],
        "LUNR":  ["Intuitive Machines", "直觉机器"],
        "RKLB":  ["Rocket Lab USA", "火箭实验室"],

        # ===== 游戏 =====
        "GME":   ["GameStop Corp.", "游戏驿站"],
        "EA":    ["Electronic Arts", "艺电"],
        "TTWO":  ["Take-Two Interactive", "双互动"],
        "ATVI":  ["Activision Blizzard", "动视暴雪"],

        # ===== 加密货币相关 =====
        "MSTR":  ["MicroStrategy", "微策略"],
        "MARA":  ["Marathon Digital", "马拉松数字"],
        "RIOT":  ["Riot Platforms", "暴乱平台"],
        "CLSK":  ["CleanSpark Inc.", "清洁能源挖矿"],

        # ===== 主要指数 ETF =====
        "SPY":   ["SPDR S&P 500 ETF", "标普500指数基金"],
        "QQQ":   ["Invesco QQQ Trust", "纳斯达克100指数基金"],
        "IWM":   ["iShares Russell 2000 ETF", "罗素2000指数基金"],
        "DIA":   ["SPDR Dow Jones ETF", "道琼斯指数基金"],
        "VOO":   ["Vanguard S&P 500 ETF", "先锋标普500"],
        "VTI":   ["Vanguard Total Stock Market", "先锋全市场"],
        "TLT":   ["iShares 20+ Year Treasury Bond", "长期国债ETF"],
        "GLD":   ["SPDR Gold Trust", "黄金ETF"],
        "SLV":   ["iShares Silver Trust", "白银ETF"],
        "USO":   ["United States Oil Fund", "原油ETF"],
        "UNG":   ["United States Natural Gas", "天然气ETF"],
        "VIXY":  ["ProShares VIX Short-Term", "恐慌指数ETF"],
        "SOXX":  ["iShares Semiconductor ETF", "半导体ETF"],
        "SMH":   ["VanEck Semiconductor ETF", "芯片ETF"],
        "ARKK":  ["ARK Innovation ETF", "方舟创新ETF"],
        "XLF":   ["Financial Select Sector SPDR", "金融行业ETF"],
        "XLE":   ["Energy Select Sector SPDR", "能源行业ETF"],
        "XLK":   ["Technology Select Sector SPDR", "科技行业ETF"],
        "XLV":   ["Health Care Select Sector SPDR", "医疗行业ETF"],
        "KRE":   ["SPDR S&P Regional Banking ETF", "区域银行ETF"],

        # ===== 其他热门个股 =====
        "SMCI":  ["Super Micro Computer", "超微电脑"],
        "CVNA":  ["Carvana Co.", "二手车电商"],
        "DKNG":  ["DraftKings Inc.", "体育博彩"],
        "CELH":  ["Celsius Holdings", "能量饮料"],
        "APP":   ["AppLovin Corporation", "广告科技"],
        "TOST":  ["Toast Inc.", "餐饮科技"],
        "NU":    ["Nu Holdings", "巴西金融科技"],
        "DELL":  ["Dell Technologies", "戴尔科技"],
        "HPQ":   ["HP Inc.", "惠普"],
        "IBM":   ["IBM Corporation", "国际商业机器"],

        # ===== 综合金融/控股 =====
        "BRK-B": ["Berkshire Hathaway Inc. (Class B)", "伯克希尔哈撒韦B"],
        "BRK-A": ["Berkshire Hathaway Inc. (Class A)", "伯克希尔哈撒韦A"],
        "KKR":   ["KKR & Co. Inc.", "KKR"],
        "BX":    ["Blackstone Inc.", "黑石"],
        "APO":   ["Apollo Global Management", "阿波罗资管"],

        # ===== 工业/制造 =====
        "MMM":   ["3M Company", "3M"],
        "HON":   ["Honeywell International", "霍尼韦尔"],
        "UNP":   ["Union Pacific", "联合太平洋"],
        "NSC":   ["Norfolk Southern", "诺福克南方"],
    }

    results = []
    seen_symbols = set()
    query_lower = query.lower()

    # 第一步：搜本地字典（带中文名）
    for code, names in common_stocks.items():
        en_name = names[0].lower()
        cn_name = names[1].lower()

        if (query_lower in code.lower()
            or query_lower in en_name
            or query_lower in cn_name):
            results.append({
                "symbol": code,
                "name": names[0],
                "cn_name": names[1],
                "display": f"{code} - {names[1]} ({names[0]})"
            })
            seen_symbols.add(code)

    # 按匹配度排序
    def sort_key(item):
        code_lower = item["symbol"].lower()
        if code_lower == query_lower:
            return 0
        if code_lower.startswith(query_lower):
            return 1
        return 2

    results.sort(key=sort_key)

    # 第二步：用 yfinance 在线搜索补充（限时5秒，防止卡顿）
    try:
        import threading
        online_results = []

        def search_yf():
            try:
                search = yf.Search(query)
                quotes = search.quotes if search.quotes else []
                for q in quotes[:20]:  # 最多取20个
                    sym = q.get("symbol", "")
                    if not sym or sym in seen_symbols:
                        continue
                    # 只保留美股（不含期货、指数等复杂品种）
                    quote_type = q.get("quoteType", "").upper()
                    exchange = q.get("exchange", "").upper()
                    if quote_type not in ("EQUITY", "ETF", ""):
                        continue
                    # 过滤掉粉单、OTC
                    if exchange in ("PNK", "OTC"):
                        continue
                    long_name = q.get("longname") or q.get("shortname") or sym
                    online_results.append({
                        "symbol": sym,
                        "name": long_name,
                        "cn_name": "",
                        "display": f"{sym} - {long_name}",
                        "from_yf": True
                    })
                    seen_symbols.add(sym)
            except Exception:
                pass

        t = threading.Thread(target=search_yf)
        t.start()
        t.join(timeout=5.0)  # 最多等5秒

        if online_results:
            # yfinance 结果排后面
            results.extend(online_results)
    except Exception:
        pass

    return jsonify(results)



if __name__ == "__main__":
    app.run(debug=True, port=5001, host="0.0.0.0")
