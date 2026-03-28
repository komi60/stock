"""업비트 Open API 클라이언트.

공식 문서: https://docs.upbit.com
- JWT 인증 (Access Key + Secret Key)
- 전체 KRW 마켓 조회
- 현재가, 캔들 데이터 조회
- 주문 (매수/매도/취소)
- 잔고 조회
- Rate limit 준수: 시세 10 req/sec, 주문 8 req/sec
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any, Optional
from urllib.parse import urlencode

import httpx
import jwt
from asyncio_throttle import Throttler
from loguru import logger


UPBIT_BASE_URL = "https://api.upbit.com/v1"


class UpbitAuth:
    """업비트 JWT 인증 토큰 생성."""

    def __init__(self, access_key: str, secret_key: str):
        self.access_key = access_key
        self.secret_key = secret_key

    def make_token(self, query: dict | None = None) -> str:
        """JWT 액세스 토큰 생성.

        쿼리 파라미터가 있는 경우 query_hash 포함.
        """
        payload: dict[str, Any] = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }

        if query:
            query_string = urlencode(query).encode()
            m = hashlib.sha512()
            m.update(query_string)
            query_hash = m.hexdigest()
            payload["query_hash"] = query_hash
            payload["query_hash_alg"] = "SHA512"

        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    def make_body_token(self, body: dict) -> str:
        """POST body용 JWT 토큰 생성."""
        query_string = urlencode(body).encode()
        m = hashlib.sha512()
        m.update(query_string)
        query_hash = m.hexdigest()

        payload = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
            "query_hash": query_hash,
            "query_hash_alg": "SHA512",
        }
        return jwt.encode(payload, self.secret_key, algorithm="HS256")


class UpbitClient:
    """업비트 REST API 클라이언트."""

    def __init__(self, access_key: str = "", secret_key: str = "", is_paper: bool = True):
        self.access_key = access_key
        self.secret_key = secret_key
        self.is_paper = is_paper
        self._auth = UpbitAuth(access_key, secret_key) if access_key else None
        self._client: Optional[httpx.AsyncClient] = None
        # Rate limiters
        self._ticker_throttler = Throttler(rate_limit=10, period=1)  # 시세: 10 req/sec
        self._order_throttler = Throttler(rate_limit=8, period=1)    # 주문: 8 req/sec

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def has_credentials(self) -> bool:
        return bool(self.access_key and self.secret_key)

    async def connect(self) -> None:
        """HTTP 클라이언트 초기화."""
        self._client = httpx.AsyncClient(
            base_url=UPBIT_BASE_URL,
            timeout=30.0,
        )
        mode = "페이퍼" if self.is_paper else "실거래"
        cred_status = "인증키 있음" if self.has_credentials else "인증키 없음 (시세만 가능)"
        logger.info(f"업비트 API 연결 완료 (모드: {mode}, {cred_status})")

    async def close(self) -> None:
        """HTTP 클라이언트 종료."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _auth_header(self, query: dict | None = None) -> dict[str, str]:
        """인증 헤더 생성."""
        if not self._auth:
            return {}
        token = self._auth.make_token(query)
        return {"Authorization": f"Bearer {token}"}

    def _auth_body_header(self, body: dict) -> dict[str, str]:
        """POST body용 인증 헤더 생성."""
        if not self._auth:
            return {}
        token = self._auth.make_body_token(body)
        return {"Authorization": f"Bearer {token}"}

    async def _get(self, path: str, params: dict | None = None, auth: bool = False) -> Any:
        """GET 요청."""
        async with self._ticker_throttler:
            headers = self._auth_header(params) if auth else {}
            resp = await self._client.get(path, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, body: dict) -> Any:
        """POST 요청 (인증 필수)."""
        async with self._order_throttler:
            headers = self._auth_body_header(body)
            resp = await self._client.post(path, data=body, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str, params: dict) -> Any:
        """DELETE 요청 (주문 취소)."""
        async with self._order_throttler:
            headers = self._auth_header(params)
            resp = await self._client.delete(path, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json()

    # ─── 마켓 정보 ─────────────────────────────────────────

    async def get_markets(self, krw_only: bool = True) -> list[dict]:
        """전체 마켓 코드 조회.

        Args:
            krw_only: True이면 KRW 마켓만 반환

        Returns:
            [{"market": "KRW-BTC", "korean_name": "비트코인", "english_name": "Bitcoin"}, ...]
        """
        data = await self._get("/market/all", params={"isDetails": "false"})
        if krw_only:
            return [m for m in data if m["market"].startswith("KRW-")]
        return data

    # ─── 시세 조회 ─────────────────────────────────────────

    async def get_ticker(self, markets: list[str]) -> list[dict]:
        """현재가 조회 (최대 100개).

        Returns:
            [{"market": "KRW-BTC", "trade_price": 50000000, "change_rate": 0.01, ...}, ...]
        """
        markets_str = ",".join(markets)
        data = await self._get("/ticker", params={"markets": markets_str})
        return data

    async def get_candles_days(
        self, market: str, count: int = 200, to: str | None = None
    ) -> list[dict]:
        """일봉 캔들 조회 (최대 200개).

        Returns:
            [{"candle_date_time_utc": ..., "opening_price": ..., "high_price": ...,
              "low_price": ..., "trade_price": ..., "candle_acc_trade_volume": ...}, ...]
        """
        params: dict[str, Any] = {"market": market, "count": min(count, 200)}
        if to:
            params["to"] = to
        data = await self._get("/candles/days", params=params)
        return data

    async def get_candles_minutes(
        self, market: str, unit: int = 60, count: int = 200
    ) -> list[dict]:
        """분봉 캔들 조회.

        Args:
            unit: 1, 3, 5, 10, 15, 30, 60, 240 (분)
        """
        params: dict[str, Any] = {"market": market, "count": min(count, 200)}
        data = await self._get(f"/candles/minutes/{unit}", params=params)
        return data

    async def get_orderbook(self, markets: list[str]) -> list[dict]:
        """호가 정보 조회."""
        markets_str = ",".join(markets)
        return await self._get("/orderbook", params={"markets": markets_str})

    # ─── 계정 / 잔고 ───────────────────────────────────────

    async def get_balance(self) -> list[dict]:
        """전체 계좌 잔고 조회.

        Returns:
            [{"currency": "KRW", "balance": "1000000.0", ...},
             {"currency": "BTC", "balance": "0.001", "avg_buy_price": "50000000", ...}]
        """
        if not self.has_credentials:
            logger.warning("업비트 인증키 없음 - 잔고 조회 불가")
            return []
        return await self._get("/accounts", auth=True)

    async def get_krw_balance(self) -> float:
        """KRW 잔고만 반환."""
        balances = await self.get_balance()
        for item in balances:
            if item.get("currency") == "KRW":
                return float(item.get("balance", 0))
        return 0.0

    # ─── 주문 ──────────────────────────────────────────────

    async def place_order(
        self,
        market: str,
        side: str,
        volume: float | None = None,
        price: float | None = None,
        ord_type: str = "limit",
    ) -> dict:
        """주문 실행.

        Args:
            market: 마켓 코드 (예: "KRW-BTC")
            side: "bid" (매수) / "ask" (매도)
            volume: 주문 수량 (코인 개수). 시장가 매수 시 None
            price: 주문 가격. 시장가 매도 시 None, 시장가 매수 시 KRW 금액
            ord_type: "limit" (지정가) / "price" (시장가 매수) / "market" (시장가 매도)

        Returns:
            주문 정보 dict (uuid 포함)
        """
        if self.is_paper:
            # 페이퍼 트레이딩: 실제 주문 없이 로그만
            mock_uuid = str(uuid.uuid4())
            logger.info(
                f"[페이퍼] {side.upper()} {market} "
                f"volume={volume} price={price} type={ord_type} -> uuid={mock_uuid}"
            )
            return {
                "uuid": mock_uuid,
                "side": side,
                "ord_type": ord_type,
                "price": str(price) if price else None,
                "state": "done",
                "market": market,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "volume": str(volume) if volume else None,
                "is_paper": True,
            }

        if not self.has_credentials:
            raise UpbitAPIError("업비트 인증키가 설정되지 않았습니다.")

        body: dict[str, Any] = {
            "market": market,
            "side": side,
            "ord_type": ord_type,
        }
        if volume is not None:
            body["volume"] = str(volume)
        if price is not None:
            body["price"] = str(price)

        data = await self._post("/orders", body)
        logger.info(
            f"주문 실행: {side.upper()} {market} "
            f"volume={volume} price={price} -> uuid={data.get('uuid')}"
        )
        return data

    async def cancel_order(self, order_uuid: str) -> dict:
        """주문 취소.

        Args:
            order_uuid: 취소할 주문의 UUID

        Returns:
            취소된 주문 정보
        """
        if self.is_paper:
            logger.info(f"[페이퍼] 주문 취소: uuid={order_uuid}")
            return {"uuid": order_uuid, "state": "cancel", "is_paper": True}

        if not self.has_credentials:
            raise UpbitAPIError("업비트 인증키가 설정되지 않았습니다.")

        data = await self._delete("/order", params={"uuid": order_uuid})
        logger.info(f"주문 취소 완료: uuid={order_uuid}")
        return data

    async def get_orders(
        self, market: str | None = None, state: str = "wait"
    ) -> list[dict]:
        """주문 목록 조회.

        Args:
            market: 마켓 코드 (None이면 전체)
            state: "wait" (미체결) / "done" (완료) / "cancel" (취소)
        """
        if not self.has_credentials:
            return []

        params: dict[str, Any] = {"state": state}
        if market:
            params["market"] = market

        return await self._get("/orders", params=params, auth=True)

    async def get_order(self, order_uuid: str) -> dict:
        """특정 주문 조회."""
        if not self.has_credentials:
            return {}
        return await self._get("/order", params={"uuid": order_uuid}, auth=True)


class UpbitAPIError(Exception):
    """업비트 API 에러."""

    def __init__(self, message: str, code: str = ""):
        self.code = code
        super().__init__(f"[{code}] {message}" if code else message)
