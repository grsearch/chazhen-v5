"""
单币种 1秒K线策略 Bot

策略逻辑（回测验证）：
  1. 订阅 WebSocket <symbol>@kline_1s
  2. 每根K线收盘 → 以收盘价挂 -ORDER_PCT% 限价买单
  3. 有持仓时每秒检查（用K线最低价/最高价/收盘价）：
       K线内最高价触及盈利线  → 以盈利价卖出（保证盈利）
       K线内最低价触及止损线  → 以止损价卖出（精确控亏）
       持仓满 HOLD_MAX_S 秒  → 以收盘价市价卖出
  4. 下一根K线收盘时撤销未成交挂单，挂新单
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from binance import AsyncClient, fmt_price, fmt_qty
from config import (
    ORDER_PCT_CFG, HOLD_MAX_CFG, STOP_LOSS_CFG, MIN_PROFIT_CFG,
    append_trade, write_log,
)

logger = logging.getLogger(__name__)
WS_BASE = "wss://stream.binance.com:9443/ws"


class Bot:
    def __init__(self, symbol: str, cfg: dict,
                 client: AsyncClient,
                 on_trade: Callable,
                 get_pos_count: Callable):
        self.symbol    = symbol.upper()
        self._sym_lo   = symbol.lower()
        self._cfg      = cfg
        self._client   = client
        self._on_trade = on_trade
        self._get_pos_count = get_pos_count

        self.running   = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

        self._order: Optional[dict] = None
        self._pos:   Optional[dict] = None
        self._filt:  Optional[dict] = None
        self._busy   = False

        self.pnl_total   = 0.0
        self.trade_count = 0

    # ── 生命周期 ──────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop   = loop
        self.running = True
        asyncio.run_coroutine_threadsafe(self._start_task(), loop)

    async def _start_task(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        self.running = False
        if self._task and not self._task.done():
            self._task.cancel()

    # ── 主协程 ───────────────────────────────────

    async def _run(self) -> None:
        try:
            self._filt = await self._client.get_filters(self.symbol)
        except Exception as e:
            self._log(f"获取精度失败: {e}")
            return

        import websockets
        url   = f"{WS_BASE}/{self._sym_lo}@kline_1s"
        delay = 1
        self._log(f"启动 WS={url}")

        while self.running:
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10, max_size=2**20
                ) as ws:
                    self._log("WS已连接")
                    delay = 1
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            await self._on_msg(json.loads(raw))
                        except Exception as e:
                            self._log(f"消息处理异常: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.running:
                    self._log(f"WS断连({e})，{delay}s后重连")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

        if self._order and self._order.get("order_id") and \
                self._cfg.get("mode") == "live":
            try:
                await self._client.cancel_order(self.symbol, self._order["order_id"])
            except Exception:
                pass
        self._log("Bot已停止")

    # ── K线消息 ───────────────────────────────────

    async def _on_msg(self, msg: dict) -> None:
        k = msg.get("k", {})
        if not k or not k.get("x"):
            return
        if self._busy:
            return
        self._busy = True
        try:
            close_p = float(k["c"])
            low_p   = float(k["l"])
            high_p  = float(k["h"])
            ts = datetime.fromtimestamp(
                k["T"] / 1000, tz=timezone.utc
            ).strftime("%H:%M:%S.%f")[:-3]
            await self._on_close(close_p, low_p, high_p, ts)
        finally:
            self._busy = False

    # ── K线收盘核心逻辑 ──────────────────────────

    async def _on_close(self, close_p: float, low_p: float,
                        high_p: float, ts: str) -> None:
        """
        执行顺序（严格串行）：
        1. 有持仓 → 用本K线 low/high/close 精确判断出场
        2. 处理上轮挂单（成交/撤单）
        3. 无持仓且未达最大持仓数 → 挂新单
        """
        mode = self._cfg.get("mode", "paper")
        t0   = time.monotonic()

        # ── 步骤1：持仓出场检查 ──────────────────
        if self._pos:
            await self._check_exit(close_p, low_p, high_p, ts, mode)

        # ── 步骤2：处理上轮挂单 ──────────────────
        if self._order:
            filled = self._check_fill(low_p, mode)

            if filled and not self._pos:
                o = self._order
                sl_price = round(o["fill_price"] * (1 - STOP_LOSS_CFG(self._cfg) / 100), 8)
                tp_price = round(o["fill_price"] * (1 + MIN_PROFIT_CFG(self._cfg) / 100), 8)
                self._pos = {
                    "entry_price": o["fill_price"],
                    "qty":         o["qty"],
                    "entry_time":  ts,
                    "hold_s":      0,
                    "sl_price":    sl_price,   # 预计算止损价
                    "tp_price":    tp_price,   # 预计算止盈价
                }
                self._log(
                    f"✅ 买入 {o['fill_price']:.6f} qty={o['qty']:.6f} "
                    f"止损={sl_price:.6f}(-{STOP_LOSS_CFG(self._cfg)}%) "
                    f"止盈={tp_price:.6f}(+{MIN_PROFIT_CFG(self._cfg)}%)"
                )
            elif not filled:
                await self._cancel_order(mode)

            self._order = None

        # ── 步骤3：无持仓才挂新单 ────────────────
        if not self._pos:
            max_pos = self._cfg.get("max_positions", 3)
            if self._get_pos_count() < max_pos:
                await self._place_order(close_p, mode)

        ms = (time.monotonic() - t0) * 1000
        if ms > 150:
            self._log(f"⚠ 收盘处理耗时 {ms:.0f}ms")

    def _check_fill(self, low_p: float, mode: str) -> bool:
        o = self._order
        if not o:
            return False
        if mode == "paper":
            if low_p <= o["price"]:
                o["filled"]     = True
                o["fill_price"] = o["price"]
                return True
            return False
        else:
            return o.get("filled", False)

    # ── 持仓出场（核心修复）─────────────────────

    async def _check_exit(self, close_p: float, low_p: float,
                          high_p: float, ts: str, mode: str) -> None:
        """
        用 K线内最高/最低价精确触发，用预计算价格卖出：
          - high_p >= tp_price → 以 tp_price 卖出（保证盈利）
          - low_p  <= sl_price → 以 sl_price 卖出（精确控亏，不超亏）
          - hold_s >= HOLD_MAX → 以 close_p  卖出（超时）
        """
        pos    = self._pos
        hold_s = pos.get("hold_s", 0) + 1
        pos["hold_s"] = hold_s

        sl_price = pos["sl_price"]
        tp_price = pos["tp_price"]
        hms      = HOLD_MAX_CFG(self._cfg)

        sell_price = None
        reason     = None

        # 优先判断止盈（用K线最高价触发）
        if high_p >= tp_price:
            sell_price = tp_price
            pnl_pct    = (tp_price - pos["entry_price"]) / pos["entry_price"] * 100
            reason     = f"止盈 +{pnl_pct:.3f}%"

        # 止损（用K线最低价触发）
        elif low_p <= sl_price:
            sell_price = sl_price
            pnl_pct    = (sl_price - pos["entry_price"]) / pos["entry_price"] * 100
            reason     = f"止损 {pnl_pct:.3f}%"

        # 超时（用收盘价）
        elif hold_s >= hms:
            sell_price = close_p
            pnl_pct    = (close_p - pos["entry_price"]) / pos["entry_price"] * 100
            reason     = f"超时{hms}s ({pnl_pct:+.3f}%)"

        if reason:
            self._log(f"📤 出场: {reason} 持仓{hold_s}s")
            await self._sell(sell_price, ts, mode, reason)

    async def _sell(self, price: float, ts: str,
                    mode: str, reason: str) -> None:
        pos       = self._pos
        self._pos = None
        qty       = pos["qty"]
        entry     = pos["entry_price"]
        sell_p    = price

        lat = 0
        if mode == "live":
            if not self._client.api_key:
                self._log("❌ 实盘需要 API Key")
                self._pos = pos
                return
            try:
                q_str  = fmt_qty(qty, self._filt["step"], self._filt["min_qty"])
                t0     = time.monotonic()
                resp   = await self._client.market_sell(self.symbol, q_str)
                lat    = (time.monotonic() - t0) * 1000
                fills  = resp.get("fills", [])
                if fills:
                    tv     = sum(float(f["price"]) * float(f["qty"]) for f in fills)
                    tq     = sum(float(f["qty"]) for f in fills)
                    sell_p = tv / tq if tq else price
            except Exception as e:
                self._log(f"❌ 卖出失败: {e}，恢复持仓")
                self._pos = pos
                return

        pnl_pct  = (sell_p - entry) / entry * 100
        pnl_usdt = (sell_p - entry) * qty
        self.pnl_total   += pnl_usdt
        self.trade_count += 1

        icon = "💰" if pnl_pct > 0 else "📉"
        self._log(
            f"{icon} {reason} | 买={entry:.6f} 卖={sell_p:.6f} "
            f"PnL={pnl_pct:+.3f}% ({pnl_usdt:+.4f}U) "
            f"持仓{pos['hold_s']}s 延迟{lat:.0f}ms"
        )

        trade = {
            "symbol":      self.symbol,
            "entry_price": round(entry, 8),
            "exit_price":  round(sell_p, 8),
            "qty":         round(qty, 6),
            "entry_time":  pos["entry_time"],
            "exit_time":   ts,
            "exit_reason": reason,
            "hold_s":      pos["hold_s"],
            "pnl_pct":     round(pnl_pct, 4),
            "pnl_usdt":    round(pnl_usdt, 4),
            "status":      "closed",
            "mode":        mode,
        }
        append_trade(trade)
        self._on_trade(trade)

    # ── 挂单 ─────────────────────────────────────

    async def _place_order(self, close_p: float, mode: str) -> None:
        cfg   = self._cfg
        total = cfg.get("position_size_usdt", 100.0)

        if mode == "live":
            if not self._client.api_key:
                return
            try:
                bal = await self._client.usdt_balance()
                if bal < total * 0.5:
                    self._log(f"余额不足 {bal:.2f}U，跳过挂单")
                    return
            except Exception as e:
                self._log(f"余额查询失败: {e}")
                return

        price = close_p * (1 - ORDER_PCT_CFG(self._cfg) / 100)
        try:
            p_str = fmt_price(price, self._filt["tick"])
            qty   = total / float(p_str)
            q_str = fmt_qty(qty, self._filt["step"], self._filt["min_qty"])
        except ValueError as e:
            self._log(f"下单参数错误: {e}")
            return

        cid = f"{self.symbol[:8]}_{int(time.time()*1000)}"[:36]
        rec = {
            "price":      float(p_str),
            "qty":        float(q_str),
            "order_id":   None,
            "client_id":  cid,
            "filled":     False,
            "fill_price": 0.0,
        }

        if mode == "live":
            try:
                t0   = time.monotonic()
                resp = await self._client.limit_buy(self.symbol, p_str, q_str, cid)
                lat  = (time.monotonic() - t0) * 1000
                rec["order_id"] = resp.get("orderId")
                self._log(
                    f"挂单 收盘={close_p:.6f} → {p_str}(-{ORDER_PCT_CFG(self._cfg)}%) "
                    f"qty={q_str} 延迟{lat:.0f}ms"
                )
            except Exception as e:
                self._log(f"下单失败: {e}")
                return
        else:
            self._log(
                f"[paper] 挂单 收盘={close_p:.6f} → {p_str}"
                f"(-{ORDER_PCT_CFG(self._cfg)}%) qty={q_str}"
            )

        self._order = rec

    async def _cancel_order(self, mode: str) -> None:
        o = self._order
        if not o:
            return
        if mode == "live" and o.get("order_id"):
            try:
                await self._client.cancel_order(self.symbol, o["order_id"])
            except Exception:
                pass

    # ── User Data Stream 回调 ─────────────────────

    def on_order_filled(self, client_id: str,
                        fill_price: float, fill_qty: float) -> None:
        if self._order and self._order.get("client_id") == client_id:
            self._order["filled"]     = True
            self._order["fill_price"] = fill_price
            self._order["qty"]        = fill_qty
            self._log(f"⚡ UDS成交 {fill_price:.6f} qty={fill_qty:.6f}")

    # ── 状态 ─────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "symbol":      self.symbol,
            "running":     self.running,
            "position":    self._pos,
            "order":       self._order,
            "pnl_total":   round(self.pnl_total, 4),
            "trade_count": self.trade_count,
        }

    def _log(self, msg: str) -> None:
        write_log(self.symbol, msg)
