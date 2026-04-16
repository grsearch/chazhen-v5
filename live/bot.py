"""
单币种 1秒K线策略 Bot

策略逻辑（回测验证）：
  1. 订阅 WebSocket <symbol>@kline_1s
  2. 每根K线收盘 → 以收盘价挂 -2% 限价买单
  3. 有持仓时每秒检查：
       盈利 >= MIN_PROFIT_PCT → 市价卖出
       亏损 >= STOP_LOSS_PCT  → 市价止损
       持仓满 HOLD_MAX_S 秒  → 强制市价卖出
  4. 下一根K线收盘时撤销未成交挂单，挂新单

速度保证：
  - 行情：WebSocket 毫秒级推送
  - 下单/撤单：asyncio + aiohttp 长连接，来回 <50ms（东京机房）
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
    """单币种 1秒K线策略执行器，运行在独立 asyncio event loop 线程中"""

    def __init__(self, symbol: str, cfg: dict,
                 client: AsyncClient,
                 on_trade: Callable):
        self.symbol    = symbol.upper()
        self._sym_lo   = symbol.lower()
        self._cfg      = cfg
        self._client   = client
        self._on_trade = on_trade

        # 运行状态
        self.running   = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task] = None

        # 挂单（最多1笔）
        self._order: Optional[dict] = None   # {order_id, price, qty, client_id, filled, fill_price}
        # 持仓
        self._pos:   Optional[dict] = None   # {entry_price, qty, entry_time, hold_s}
        # 精度
        self._filt:  Optional[dict] = None
        # 串行锁（防止K线回调重入）
        self._busy = False

        # 统计
        self.pnl_total   = 0.0
        self.trade_count = 0

    # ── 生命周期 ──────────────────────────────────

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop   = loop
        self.running = True
        # 用 run_coroutine_threadsafe 保证线程安全（可从任意线程调用）
        future = asyncio.run_coroutine_threadsafe(self._start_task(), loop)
        # 不阻塞等待，让 loop 异步执行
        _ = future

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
        self._log(f"启动，连接 {url}")

        while self.running:
            try:
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=10, max_size=2**20
                ) as ws:
                    self._log("WS 已连接")
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
                    self._log(f"WS 断连({e})，{delay}s 后重连")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)

        # 退出时撤销挂单
        if self._order and self._order.get("order_id") and \
                self._cfg.get("mode") == "live":
            try:
                await self._client.cancel_order(
                    self.symbol, self._order["order_id"]
                )
            except Exception:
                pass
        self._log("Bot 已停止")

    # ── K线消息 ───────────────────────────────────

    async def _on_msg(self, msg: dict) -> None:
        k = msg.get("k", {})
        if not k or not k.get("x"):   # 只处理已收盘的K线
            return
        if self._busy:
            return
        self._busy = True
        try:
            close_p = float(k["c"])
            ts      = datetime.fromtimestamp(
                k["T"] / 1000, tz=timezone.utc
            ).strftime("%H:%M:%S.%f")[:-3]
            await self._on_close(close_p, ts)
        finally:
            self._busy = False

    # ── K线收盘核心逻辑 ──────────────────────────

    async def _on_close(self, close_p: float, ts: str) -> None:
        mode = self._cfg.get("mode", "paper")
        t0   = time.monotonic()

        # 1. 检查持仓出场
        if self._pos:
            await self._check_exit(close_p, ts, mode)

        # 2. 处理上一轮挂单
        if self._order:
            filled = self._check_fill(close_p, mode)
            if not filled:
                # 撤销未成交
                await self._cancel_order(mode)
            else:
                # 开仓（若已有持仓则不重复开）
                if not self._pos:
                    o = self._order
                    self._pos = {
                        "entry_price": o["fill_price"],
                        "qty":         o["qty"],
                        "entry_time":  ts,
                        "hold_s":      0,
                    }
                    sl = o["fill_price"] * (1 - STOP_LOSS_PCT / 100)
                    self._log(
                        f"✅ 买入 {o['fill_price']:.6f} qty={o['qty']:.6f} "
                        f"止损={o['fill_price']*(1-STOP_LOSS_CFG(self._cfg)/100):.6f} "
                        f"最多{HOLD_MAX_CFG(self._cfg)}s"
                    )
            self._order = None

        # 3. 没有持仓才挂新单
        if not self._pos:
            await self._place_order(close_p, mode)

        ms = (time.monotonic() - t0) * 1000
        if ms > 100:
            self._log(f"⚠ 收盘处理耗时 {ms:.0f}ms")

    def _check_fill(self, close_p: float, mode: str) -> bool:
        """paper 模式：收盘价 <= 挂单价视为成交"""
        o = self._order
        if not o:
            return False
        if mode == "paper":
            if close_p <= o["price"]:
                o["filled"]     = True
                o["fill_price"] = o["price"]
                return True
            return False
        else:
            # 实盘：由 User Data Stream 回调更新 filled 状态
            return o.get("filled", False)

    # ── 持仓出场 ─────────────────────────────────

    async def _check_exit(self, close_p: float, ts: str, mode: str) -> None:
        pos    = self._pos
        hold_s = pos.get("hold_s", 0) + 1
        pos["hold_s"] = hold_s
        entry  = pos["entry_price"]
        pnl    = (close_p - entry) / entry * 100

        sl  = STOP_LOSS_CFG(self._cfg)
        tp  = MIN_PROFIT_CFG(self._cfg)
        hms = HOLD_MAX_CFG(self._cfg)
        reason = None
        if pnl >= tp:
            reason = f"止盈 +{pnl:.3f}%"
        elif pnl <= -sl:
            reason = f"止损 {pnl:.3f}%"
        elif hold_s >= hms:
            reason = f"超时{hms}s ({pnl:+.3f}%)"

        if reason:
            await self._sell(close_p, ts, mode, reason)

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
                self._log("❌ 实盘模式需要填写 API Key")
                self._pos = pos
                return
            try:
                q_str  = fmt_qty(qty, self._filt["step"], self._filt["min_qty"])
                t0     = time.monotonic()
                resp   = await self._client.market_sell(self.symbol, q_str)
                lat    = (time.monotonic() - t0) * 1000
                fills  = resp.get("fills", [])
                if fills:
                    tv    = sum(float(f["price"]) * float(f["qty"]) for f in fills)
                    tq    = sum(float(f["qty"]) for f in fills)
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

        # 实盘检查余额
        if mode == "live":
            try:
                bal = await self._client.usdt_balance()
                if bal < total * 0.5:
                    self._log(f"余额不足 {bal:.2f}U < {total*0.5:.1f}U，跳过挂单")
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
            "price":     float(p_str),
            "qty":       float(q_str),
            "order_id":  None,
            "client_id": cid,
            "filled":    False,
            "fill_price": 0.0,
        }

        if mode == "live":
            if not self._client.api_key:
                self._log("❌ 实盘模式需要填写 API Key")
                return
            try:
                t0   = time.monotonic()
                resp = await self._client.limit_buy(
                    self.symbol, p_str, q_str, cid
                )
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
                f"(-{ORDER_PCT_CFG(self._cfg)}%)"
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
        self._log(f"撤单 {o['price']:.6f}")

    # ── User Data Stream 回调 ─────────────────────

    def on_order_filled(self, client_id: str,
                        fill_price: float, fill_qty: float) -> None:
        """实盘：User Data Stream 推送成交时调用"""
        if self._order and self._order.get("client_id") == client_id:
            self._order["filled"]     = True
            self._order["fill_price"] = fill_price
            self._order["qty"]        = fill_qty
            self._log(
                f"⚡ UDS 成交推送 {fill_price:.6f} qty={fill_qty:.6f}"
            )

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
