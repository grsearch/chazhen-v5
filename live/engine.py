"""
主引擎
- 管理所有 Bot 的生命周期
- 涨幅榜自动扫描（可关闭）
- 所有 Bot 共用一个 asyncio event loop（单独线程）
- 供 Flask server.py 调用
"""
import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from binance import AsyncClient, SyncClient
from bot import Bot
from config import (
    load_cfg, save_cfg, cfg_safe,
    load_state, save_state,
    load_trades,
    read_logs, write_log,
    SCAN_INTERVAL, MAX_SYMBOLS, BL_EXACT, BL_KW,
)

logger = logging.getLogger(__name__)


class Engine:
    def __init__(self):
        self.cfg   = load_cfg()
        self.state = load_state()

        # 异步客户端（所有 Bot 共用）
        self._aclient: Optional[AsyncClient] = self._mk_aclient()
        # 同步客户端（扫描用）
        self._sclient: Optional[SyncClient]  = self._mk_sclient()

        # 单一 event loop，运行在独立线程
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread]   = None

        # Bot 字典
        self._bots:  dict[str, Bot] = {}
        self._lock   = threading.Lock()

        # 扫描线程
        self._scan_stop   = threading.Event()
        self._scan_thread: Optional[threading.Thread] = None

        # 内存日志
        self._mem_logs: list[dict] = read_logs(200)

        # PnL 聚合（从 state 恢复）
        self.pnl_total = self.state.get("pnl_total", 0.0)
        self.pnl_log   = self.state.get("pnl_log", [])

    # ── 客户端构造 ────────────────────────────────

    def _mk_aclient(self) -> AsyncClient:
        # paper模式不需要Key，但仍需client访问公开接口（精度查询）
        k = self.cfg.get("api_key", "")
        s = self.cfg.get("api_secret", "")
        return AsyncClient(k, s)

    def _mk_sclient(self) -> Optional[SyncClient]:
        k, s = self.cfg.get("api_key",""), self.cfg.get("api_secret","")
        return SyncClient(k, s)

    # ── Event Loop 管理 ───────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop and self._loop.is_running():
            return self._loop
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True, name="bot-loop"
        )
        self._loop_thread.start()
        return self._loop

    # ── 公开 API（供 Flask 调用）──────────────────

    def get_status(self) -> dict:
        trades = load_trades()
        closed = [t for t in trades if t.get("status") == "closed"]
        wins   = [t for t in closed if t.get("pnl_pct", 0) > 0]
        wr     = round(len(wins) / len(closed) * 100, 1) if closed else 0

        positions, orders = {}, {}
        bot_states = {}
        for sym, bot in self._bots.items():
            st = bot.get_status()
            bot_states[sym] = st["running"]
            if st["position"]:
                positions[sym] = st["position"]
            if st["order"]:
                orders[sym] = st["order"]

        return {
            "running":       self.state["running"],
            "mode":          self.cfg.get("mode", "paper"),
            "symbols":       list(self.state["symbols"]),
            "positions":     positions,
            "orders":        orders,
            "pnl_total":     round(self.pnl_total, 4),
            "pnl_log":       self.pnl_log[-80:],
            "trade_count":   len(closed),
            "win_rate":      wr,
            "logs":          self._mem_logs[:120],
            "recent_trades": trades[:50],
            "cfg_size":      self.cfg.get("position_size_usdt", 100),
            "auto_scan":     self.cfg.get("auto_scan", True),
        }

    def get_trades(self) -> list:
        return load_trades()

    def get_config(self) -> dict:
        return cfg_safe(self.cfg)

    def update_config(self, data: dict) -> dict:
        with self._lock:
            if data.get("api_secret") == "***":
                data["api_secret"] = self.cfg.get("api_secret", "")
            self.cfg.update(data)
            save_cfg(self.cfg)
            self._aclient = self._mk_aclient()
            self._sclient = self._mk_sclient()
        self._log("SYS", "配置已更新")
        return {"ok": True}

    def start(self) -> dict:
        with self._lock:
            if self.state["running"]:
                return {"ok": False, "msg": "已在运行"}
            self.state["running"] = True
            save_state(self.state)

        loop = self._ensure_loop()

        # 启动所有已有币种的 Bot
        for sym in list(self.state["symbols"]):
            self._start_bot(sym, loop)

        # 启动扫描线程
        self._scan_stop.clear()
        self._scan_thread = threading.Thread(
            target=self._scan_loop, daemon=True, name="scanner"
        )
        self._scan_thread.start()

        # 立即扫一次
        threading.Thread(target=self._do_scan, daemon=True).start()

        self._log("SYS", f"启动 模式={self.cfg.get('mode','paper')}")
        return {"ok": True}

    def stop(self) -> dict:
        self._scan_stop.set()
        with self._lock:
            for bot in list(self._bots.values()):
                bot.stop()
            self._bots.clear()
            self.state["running"] = False
            save_state(self.state)
        self._log("SYS", "已停止")
        return {"ok": True}

    def reset(self) -> dict:
        if self.cfg.get("mode") == "live":
            return {"ok": False, "msg": "实盘模式不允许重置"}
        self.stop()
        with self._lock:
            self.state = {
                "running": False, "symbols": [],
                "pnl_total": 0.0, "pnl_log": [],
            }
            save_state(self.state)
            from config import save_trades
            save_trades([])
        self.pnl_total = 0.0
        self.pnl_log   = []
        self._mem_logs = []
        self._log("SYS", "已重置")
        return {"ok": True}

    def add_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        if not symbol.endswith("USDT"):
            return {"ok": False, "msg": "目前只支持 USDT 交易对"}
        with self._lock:
            if symbol not in self.state["symbols"]:
                self.state["symbols"].append(symbol)
                save_state(self.state)
        if self.state["running"]:
            self._start_bot(symbol, self._ensure_loop())
        self._log("SYS", f"添加监控: {symbol}")
        return {"ok": True}

    def remove_symbol(self, symbol: str) -> dict:
        symbol = symbol.upper()
        with self._lock:
            bot = self._bots.pop(symbol, None)
            if bot:
                bot.stop()
            if symbol in self.state["symbols"]:
                self.state["symbols"].remove(symbol)
            save_state(self.state)
        self._log("SYS", f"移除监控: {symbol}")
        return {"ok": True}

    def manual_scan(self) -> dict:
        threading.Thread(target=self._do_scan, daemon=True).start()
        return {"ok": True}

    # ── 涨幅榜扫描 ────────────────────────────────

    def _scan_loop(self) -> None:
        while not self._scan_stop.wait(SCAN_INTERVAL):
            if not self.state.get("running"):
                break
            if not self.cfg.get("auto_scan", True):
                self._log("SYS", "自动扫描已关闭，跳过")
                continue
            self._do_scan()

    def _do_scan(self) -> None:
        try:
            data = self._sclient.ticker_24h() if self._sclient else []
            cfg  = self.cfg
            gainers = []
            for t in data:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):        continue
                if sym in BL_EXACT:                 continue
                if any(k in sym[:-4] for k in BL_KW): continue
                try:
                    gain = float(t["priceChangePercent"])
                    vol  = float(t["quoteVolume"])
                except Exception:
                    continue
                if gain >= cfg.get("min_gain_24h", 30.0) and \
                   vol  >= cfg.get("min_volume_usdt", 500_000.0):
                    gainers.append((sym, gain))

            gainers.sort(key=lambda x: x[1], reverse=True)
            new_syms = [s for s, _ in gainers[:MAX_SYMBOLS]]

            loop = self._ensure_loop() if self.state.get("running") else None

            with self._lock:
                cur     = list(self.state["symbols"])
                added   = [s for s in new_syms if s not in cur]
                removed = [
                    s for s in cur
                    if s not in new_syms
                    and s not in self._bots  # 有 Bot 运行中的不自动移除
                ]
                for s in removed:
                    self.state["symbols"].remove(s)
                for s in added:
                    self.state["symbols"].append(s)
                save_state(self.state)

            for s in removed:
                self._log("SYS", f"榜单移除: {s}")
            for s in added:
                self._log("SYS", f"榜单新增: {s}")
                if loop:
                    self._start_bot(s, loop)

            # 补漏：symbols 里有但没有运行中 Bot 的，也补启动
            if loop:
                for s in list(self.state["symbols"]):
                    if s not in self._bots or not self._bots[s].running:
                        self._log("SYS", f"补启动 Bot: {s}")
                        self._start_bot(s, loop)

            self._log("SYS",
                f"扫描完成 +{len(added)} -{len(removed)} "
                f"当前{len(self.state['symbols'])}个")
        except Exception as e:
            self._log("SYS", f"扫描失败: {e}")

    # ── Bot 启停 ──────────────────────────────────

    def _count_positions(self) -> int:
        """返回当前所有Bot中有持仓的数量"""
        return sum(1 for b in self._bots.values() if b._pos is not None)

    def _start_bot(self, symbol: str,
                   loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            if symbol in self._bots and self._bots[symbol].running:
                return
            bot = Bot(
                symbol             = symbol,
                cfg                = self.cfg,
                client             = self._aclient,
                on_trade           = self._on_trade,
                get_pos_count = self._count_positions,
            )
            self._bots[symbol] = bot
        # bot.start 内部使用 run_coroutine_threadsafe，线程安全
        bot.start(loop)
        self._log(symbol, "Bot 已启动")

    # ── 成交回调 ─────────────────────────────────

    def _on_trade(self, trade: dict) -> None:
        pnl = trade.get("pnl_usdt", 0.0)
        self.pnl_total += pnl
        self.pnl_log.append(round(trade.get("pnl_pct", 0.0), 4))
        if len(self.pnl_log) > 200:
            self.pnl_log = self.pnl_log[-200:]
        with self._lock:
            self.state["pnl_total"] = round(self.pnl_total, 4)
            self.state["pnl_log"]   = self.pnl_log
            save_state(self.state)

    # ── 日志 ─────────────────────────────────────

    def _log(self, symbol: str, msg: str) -> None:
        write_log(symbol, msg)
        entry = {
            "time":   datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "symbol": symbol,
            "msg":    msg,
        }
        self._mem_logs.insert(0, entry)
        if len(self._mem_logs) > 200:
            self._mem_logs = self._mem_logs[:200]


# ── 单例 ─────────────────────────────────────────────
_engine: Optional[Engine] = None

def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine
