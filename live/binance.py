"""
币安客户端
- AsyncClient : asyncio + aiohttp，用于下单/撤单（速度优先）
- SyncClient  : 同步 REST，用于涨幅榜扫描等低频接口
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
import urllib.error
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)
REST = "https://api.binance.com"


# ── 精度工具 ─────────────────────────────────────────

def fmt_price(price: float, tick: float) -> str:
    tick_d  = Decimal(str(tick))
    price_d = Decimal(str(price))
    result  = (price_d / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
    s = str(tick_d.normalize())
    decimals = len(s.split(".")[-1]) if "." in s else 0
    return f"{float(result):.{decimals}f}"

def fmt_qty(qty: float, step: float, min_qty: float) -> str:
    step_d = Decimal(str(step))
    qty_d  = Decimal(str(qty))
    result = (qty_d / step_d).to_integral_value(rounding=ROUND_DOWN) * step_d
    if float(result) < min_qty:
        raise ValueError(f"qty {float(result):.8f} < min_qty {min_qty}")
    s = str(step_d.normalize())
    decimals = len(s.split(".")[-1]) if "." in s else 0
    return f"{float(result):.{decimals}f}"


# ══════════════════════════════════════════════════════
#  异步客户端（下单核心）
# ══════════════════════════════════════════════════════

class AsyncClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key    = api_key
        self.api_secret = api_secret
        self._session:  Optional[aiohttp.ClientSession] = None
        self._filters:  dict = {}   # 精度缓存

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            conn = aiohttp.TCPConnector(
                limit=30, ttl_dns_cache=300,
                force_close=False, enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=conn,
                headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "chazhen/4.0"},
                timeout=aiohttp.ClientTimeout(total=5),
            )
        return self._session

    def _sign(self, params: dict) -> str:
        return hmac.new(
            self.api_secret.encode(),
            urllib.parse.urlencode(params).encode(),
            hashlib.sha256,
        ).hexdigest()

    async def _post(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.post(
            REST + path,
            data=urllib.parse.urlencode(params),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise Exception(f"[{body.get('code')}] {body.get('msg', body)}")
            return body

    async def _delete(self, path: str, params: dict) -> dict:
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.delete(
            REST + path + "?" + urllib.parse.urlencode(params)
        ) as r:
            return await r.json(content_type=None)

    async def _get(self, path: str, params: dict = None, signed: bool = False) -> Any:
        params = params or {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        qs = "?" + urllib.parse.urlencode(params) if params else ""
        s  = await self._sess()
        async with s.get(REST + path + qs) as r:
            return await r.json(content_type=None)

    # ── 账户 ───────────────────────────────────────

    async def usdt_balance(self) -> float:
        data = await self._get("/api/v3/account", signed=True)
        for b in data.get("balances", []):
            if b["asset"] == "USDT":
                return float(b["free"])
        return 0.0

    async def asset_balance(self, asset: str) -> float:
        data = await self._get("/api/v3/account", signed=True)
        for b in data.get("balances", []):
            if b["asset"] == asset:
                return float(b["free"])
        return 0.0

    # ── 精度 ───────────────────────────────────────

    async def get_filters(self, symbol: str) -> dict:
        if symbol in self._filters:
            return self._filters[symbol]
        data = await self._get("/api/v3/exchangeInfo", {"symbol": symbol})
        fs   = {f["filterType"]: f for f in data["symbols"][0]["filters"]}
        info = {
            "tick":     float(fs["PRICE_FILTER"]["tickSize"]),
            "step":     float(fs["LOT_SIZE"]["stepSize"]),
            "min_qty":  float(fs["LOT_SIZE"]["minQty"]),
            "min_notional": float(fs.get("NOTIONAL", {}).get("minNotional", 5)),
        }
        self._filters[symbol] = info
        return info

    # ── 下单 / 撤单 ────────────────────────────────

    async def limit_buy(self, symbol: str, price: str,
                        qty: str, cid: str) -> dict:
        return await self._post("/api/v3/order", {
            "symbol": symbol, "side": "BUY", "type": "LIMIT",
            "timeInForce": "GTC", "price": price, "quantity": qty,
            "newClientOrderId": cid,
        })

    async def market_sell(self, symbol: str, qty: str) -> dict:
        return await self._post("/api/v3/order", {
            "symbol": symbol, "side": "SELL",
            "type": "MARKET", "quantity": qty,
        })

    async def cancel_order(self, symbol: str, oid: int) -> dict:
        return await self._delete("/api/v3/order",
                                  {"symbol": symbol, "orderId": oid})

    async def cancel_all(self, symbol: str) -> dict:
        return await self._delete("/api/v3/openOrders", {"symbol": symbol})

    # ── User Data Stream ────────────────────────────

    async def get_listen_key(self) -> str:
        data = await self._post("/api/v3/userDataStream", {})
        return data["listenKey"]

    async def keepalive_key(self, key: str) -> None:
        params = {"listenKey": key,
                  "timestamp": int(time.time() * 1000)}
        params["signature"] = self._sign(params)
        s = await self._sess()
        async with s.put(
            REST + "/api/v3/userDataStream?" + urllib.parse.urlencode(params)
        ):
            pass

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ══════════════════════════════════════════════════════
#  同步客户端（扫描 / 低频用）
# ══════════════════════════════════════════════════════

class SyncClient:
    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key    = api_key
        self.api_secret = api_secret

    def _get(self, path: str, params: dict = None) -> Any:
        params = params or {}
        url = REST + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            url, headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "chazhen/4.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def ticker_24h(self) -> list:
        return self._get("/api/v3/ticker/24hr")


# 补 Any 导入
