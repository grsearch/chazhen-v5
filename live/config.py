"""
全局配置
"""
import os, json, threading
from typing import Any

BASE_DIR = os.path.dirname(__file__)
# 数据目录放在项目根目录下，与 live/ 平级
# 这样更新代码（覆盖 live/）不会误删历史数据
DATA_DIR = os.path.join(os.path.dirname(BASE_DIR), "data")
os.makedirs(DATA_DIR, exist_ok=True)

CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
STATE_FILE  = os.path.join(DATA_DIR, "state.json")
TRADES_FILE = os.path.join(DATA_DIR, "trades.json")
LOG_FILE    = os.path.join(DATA_DIR, "bot.log")

# ── 策略默认值（用户可在 dashboard 调整）──────────
ORDER_PCT      = 2.0    # 挂单深度默认值
HOLD_MAX_S     = 10     # 最多持仓秒数默认值
STOP_LOSS_PCT  = 1.0    # 止损 % 默认值
MIN_PROFIT_PCT = 0.25   # 最小盈利阈值 % 默认值
FEE_RATE       = 0.001  # 单边手续费（固定）

def ORDER_PCT_CFG(cfg: dict) -> float:
    return float(cfg.get("order_pct", ORDER_PCT))

def HOLD_MAX_CFG(cfg: dict) -> int:
    return int(cfg.get("hold_max_s", HOLD_MAX_S))

def STOP_LOSS_CFG(cfg: dict) -> float:
    return float(cfg.get("stop_loss_pct", STOP_LOSS_PCT))

def MIN_PROFIT_CFG(cfg: dict) -> float:
    return float(cfg.get("min_profit_pct", MIN_PROFIT_PCT))

# 涨幅榜
SCAN_INTERVAL  = 15 * 60   # 15 分钟扫一次
MAX_SYMBOLS    = 15        # 榜单最多取前 N 名
BL_EXACT = {"USDCUSDT","BUSDUSDT","TUSDUSDT","FDUSDUSDT","DAIUSDT"}
BL_KW    = ("UP","DOWN","BULL","BEAR","3L","3S")

# ── 默认用户配置 ──────────────────────────────────
DEFAULTS: dict = {
    "mode":               "paper",   # paper | live
    "api_key":            "",
    "api_secret":         "",
    "position_size_usdt": 100.0,     # 每笔仓位 USDT
    "max_positions":      3,         # 同时最多持仓数
    "min_gain_24h":       30.0,      # 涨幅榜筛选：最低 24h 涨幅 %
    "min_volume_usdt":    500_000.0, # 涨幅榜筛选：最低 24h 成交量
    "auto_scan":          True,      # 是否自动扫描涨幅榜
    # 策略参数（可在 dashboard 调整）
    "order_pct":          2.0,       # 挂单深度 %（收盘价 -X%）
    "hold_max_s":         10,        # 最多持仓秒数
    "stop_loss_pct":      1.0,       # 止损 %
    "min_profit_pct":     1.0,       # 最小盈利阈值 %
}

_lock = threading.Lock()


def _read(path: str, default: Any) -> Any:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _write(path: str, data: Any) -> None:
    with _lock:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)


# ── Config ───────────────────────────────────────
def load_cfg() -> dict:
    stored = _read(CONFIG_FILE, {})
    return {**DEFAULTS, **stored}

def save_cfg(cfg: dict) -> None:
    _write(CONFIG_FILE, cfg)

def cfg_safe(cfg: dict) -> dict:
    c = dict(cfg)
    if c.get("api_secret"):
        c["api_secret"] = "***"
    return c


# ── State ────────────────────────────────────────
DEFAULT_STATE = {
    "running":   False,
    "symbols":   [],
    "pnl_total": 0.0,
    "pnl_log":   [],
}

def load_state() -> dict:
    s = _read(STATE_FILE, {})
    return {**DEFAULT_STATE, **s}

def save_state(state: dict) -> None:
    _write(STATE_FILE, state)


# ── Trades ───────────────────────────────────────
def load_trades() -> list:
    return _read(TRADES_FILE, [])

def save_trades(trades: list) -> None:
    _write(TRADES_FILE, trades)

def append_trade(t: dict) -> None:
    trades = load_trades()
    trades.insert(0, t)
    save_trades(trades[:500])


# ── Log ──────────────────────────────────────────
def write_log(symbol: str, msg: str) -> None:
    from datetime import datetime, timezone
    ts   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{symbol}] {msg}\n"
    with _lock:
        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 10*1024*1024:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                with open(LOG_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines)//2:])
        except Exception:
            pass
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)

def read_logs(n: int = 200) -> list:
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        result = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                ts_end  = line.index("]")
                ts      = line[1:ts_end]
                rest    = line[ts_end+3:]
                sym_end = rest.index("]")
                sym     = rest[1:sym_end]
                msg     = rest[sym_end+2:]
                result.append({"time": ts[-8:], "symbol": sym, "msg": msg})
            except Exception:
                result.append({"time": "--:--:--", "symbol": "SYS", "msg": line})
        return list(reversed(result))
    except Exception:
        return []
