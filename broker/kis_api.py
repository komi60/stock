"""한국투자증권 Open API 클라이언트.

공식 문서: https://apiportal.koreainvestment.com
- 인증 토큰 자동 갱신
- 주문 (매수/매도/정정/취소)
- 잔고 조회, 현재가 조회
- 모의투자/실전투자 자동 분기
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from loguru import logger

from core.config import KISConfig
from core.data_models import Order, OrderSide, OrderType, OrderStatus, Position


class KISAuth:
    """한국투자증권 OAuth 토큰 관리."""

    def __init__(self, config: KISConfig):
        self.config = config
        self._access_token: str = ""
        self._token_expires: datetime = datetime.min
        self._lock = asyncio.Lock()

    @property
    def is_token_valid(self) -> bool:
        return bool(self._access_token) and datetime.now() < self._token_expires

    async def get_token(self, client: httpx.AsyncClient) -> str:
        """액세스 토큰 반환. 만료 시 자동 갱신."""
        async with self._lock:
            if self.is_token_valid:
                return self._access_token
            return await self._refresh_token(client)

    async def _refresh_token(self, client: httpx.AsyncClient) -> str:
        """토큰 신규 발급."""
        url = f"{self.config.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
        }
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        # 토큰 유효기간: 보통 24시간, 안전하게 23시간으로 설정
        self._token_expires = datetime.now() + timedelta(hours=23)
        logger.info("KIS 액세스 토큰 갱신 완료")
        return self._access_token


class KISClient:
    """한국투자증권 API 클라이언트."""

    def __init__(self, config: KISConfig):
        self.config = config
        self._auth = KISAuth(config)
        self._client: Optional[httpx.AsyncClient] = None

    async def connect(self) -> None:
        """HTTP 클라이언트 초기화."""
        self._client = httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=30.0,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        # 초기 토큰 발급
        await self._auth.get_token(self._client)
        logger.info(f"KIS API 연결 완료 (모의투자: {self.config.is_paper})")

    async def close(self) -> None:
        """HTTP 클라이언트 종료."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _headers(self, tr_id: str) -> dict[str, str]:
        """API 요청 헤더 구성."""
        token = await self._auth.get_token(self._client)
        return {
            "authorization": f"Bearer {token}",
            "appkey": self.config.app_key,
            "appsecret": self.config.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }

    async def _get(self, path: str, tr_id: str, params: dict | None = None) -> dict:
        """GET 요청."""
        headers = await self._headers(tr_id)
        resp = await self._client.get(path, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise KISAPIError(data.get("msg1", "Unknown error"), data.get("msg_cd"))
        return data

    async def _post(self, path: str, tr_id: str, body: dict) -> dict:
        """POST 요청."""
        headers = await self._headers(tr_id)
        resp = await self._client.post(path, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        if data.get("rt_cd") != "0":
            raise KISAPIError(data.get("msg1", "Unknown error"), data.get("msg_cd"))
        return data

    # ─── 시세 조회 ─────────────────────────────────────────

    async def get_current_price(self, ticker: str) -> dict[str, Any]:
        """현재가 조회.

        Returns:
            {price, change, change_pct, volume, high, low, open, ...}
        """
        tr_id = "FHKST01010100"
        params = {
            "fid_cond_mrkt_div_code": "J",  # 주식
            "fid_input_iscd": ticker,
        }
        data = await self._get("/uapi/domestic-stock/v1/quotations/inquire-price", tr_id, params)
        output = data.get("output", {})
        return {
            "ticker": ticker,
            "price": int(output.get("stck_prpr", 0)),
            "change": int(output.get("prdy_vrss", 0)),
            "change_pct": float(output.get("prdy_ctrt", 0)),
            "volume": int(output.get("acml_vol", 0)),
            "high": int(output.get("stck_hgpr", 0)),
            "low": int(output.get("stck_lwpr", 0)),
            "open": int(output.get("stck_oprc", 0)),
        }

    async def get_daily_chart(
        self, ticker: str, period: str = "D", count: int = 100
    ) -> list[dict]:
        """일봉/주봉/월봉 데이터 조회.

        Args:
            period: D(일), W(주), M(월)
        """
        tr_id = "FHKST01010400" if not self.config.is_paper else "FHKST01010400"
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=count * 2)).strftime("%Y%m%d")

        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": ticker,
            "fid_input_date_1": start_date,
            "fid_input_date_2": end_date,
            "fid_period_div_code": period,
            "fid_org_adj_prc": "0",  # 수정주가
        }
        data = await self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price", tr_id, params
        )
        candles = []
        for item in data.get("output", []):
            candles.append({
                "date": item.get("stck_bsop_date", ""),
                "open": int(item.get("stck_oprc", 0)),
                "high": int(item.get("stck_hgpr", 0)),
                "low": int(item.get("stck_lwpr", 0)),
                "close": int(item.get("stck_clpr", 0)),
                "volume": int(item.get("acml_vol", 0)),
            })
        return candles

    # ─── 주문 ──────────────────────────────────────────────

    async def place_order(self, order: Order) -> str:
        """주문 실행 (매수/매도).

        Returns:
            주문번호 (order_id)
        """
        if order.side == OrderSide.BUY:
            tr_id = "VTTC0802U" if self.config.is_paper else "TTTC0802U"
        else:
            tr_id = "VTTC0801U" if self.config.is_paper else "TTTC0801U"

        # 주문 유형
        if order.order_type == OrderType.MARKET:
            ord_dvsn = "01"  # 시장가
            ord_unpr = "0"
        else:
            ord_dvsn = "00"  # 지정가
            ord_unpr = str(int(order.price))

        body = {
            "CANO": self.config.account_no[:8],
            "ACNT_PRDT_CD": self.config.account_no[8:] or self.config.account_prod_code,
            "PDNO": order.ticker,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(order.quantity),
            "ORD_UNPR": ord_unpr,
        }

        data = await self._post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, body)
        output = data.get("output", {})
        order_id = output.get("ODNO", "")
        logger.info(f"주문 체결: {order.side.value} {order.ticker} x{order.quantity} @ {order.price} -> {order_id}")
        return order_id

    async def cancel_order(self, order_id: str, ticker: str, quantity: int) -> dict:
        """주문 취소."""
        tr_id = "VTTC0803U" if self.config.is_paper else "TTTC0803U"
        body = {
            "CANO": self.config.account_no[:8],
            "ACNT_PRDT_CD": self.config.account_no[8:] or self.config.account_prod_code,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 취소
            "ORD_QTY": str(quantity),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
        }
        return await self._post("/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, body)

    async def modify_order(
        self, order_id: str, ticker: str, quantity: int, new_price: int
    ) -> dict:
        """주문 정정."""
        tr_id = "VTTC0803U" if self.config.is_paper else "TTTC0803U"
        body = {
            "CANO": self.config.account_no[:8],
            "ACNT_PRDT_CD": self.config.account_no[8:] or self.config.account_prod_code,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_id,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "01",  # 정정
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(new_price),
            "QTY_ALL_ORD_YN": "N",
        }
        return await self._post("/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, body)

    # ─── 잔고 조회 ─────────────────────────────────────────

    async def get_balance(self) -> list[Position]:
        """보유 잔고 조회."""
        tr_id = "VTTC8434R" if self.config.is_paper else "TTTC8434R"
        params = {
            "CANO": self.config.account_no[:8],
            "ACNT_PRDT_CD": self.config.account_no[8:] or self.config.account_prod_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "01",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        data = await self._get("/uapi/domestic-stock/v1/trading/inquire-balance", tr_id, params)

        positions = []
        for item in data.get("output1", []):
            qty = int(item.get("hldg_qty", 0))
            if qty <= 0:
                continue
            avg_price = float(item.get("pchs_avg_pric", 0))
            cur_price = float(item.get("prpr", 0))
            positions.append(Position(
                ticker=item.get("pdno", ""),
                name=item.get("prdt_name", ""),
                quantity=qty,
                avg_price=avg_price,
                current_price=cur_price,
                unrealized_pnl=float(item.get("evlu_pfls_amt", 0)),
                unrealized_pnl_pct=float(item.get("evlu_pfls_rt", 0)),
            ))
        return positions

    async def get_available_cash(self) -> int:
        """주문 가능 현금 조회."""
        tr_id = "VTTC8908R" if self.config.is_paper else "TTTC8908R"
        params = {
            "CANO": self.config.account_no[:8],
            "ACNT_PRDT_CD": self.config.account_no[8:] or self.config.account_prod_code,
            "PDNO": "005930",  # 삼성전자 (종목 무관, 가능액 조회용)
            "ORD_UNPR": "0",
            "ORD_DVSN": "01",
            "CMA_EVLU_AMT_ICLD_YN": "Y",
            "OVRS_ICLD_YN": "N",
        }
        data = await self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order", tr_id, params
        )
        output = data.get("output", {})
        return int(output.get("ord_psbl_cash", 0))


class KISAPIError(Exception):
    """한국투자증권 API 에러."""

    def __init__(self, message: str, code: str = ""):
        self.code = code
        super().__init__(f"[{code}] {message}")
