"""크립토 마스터 모듈.

크립토 뉴스 + 기술적 분석 + Fear&Greed Index를 통합하여
업비트 KRW 마켓 전체에서 매수/매도 코인을 자동 선정 및 주문 실행.
24/7 운영 (5분 간격).
페이퍼 트레이딩 지원.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from loguru import logger

from broker.upbit_api import UpbitClient
from core.base_module import BaseModule
from core.database import get_db
from modules.crypto_news.crypto_news_module import CryptoNewsModule
from modules.crypto_trading.crypto_trading_module import (
    CryptoTradingModule,
    SIGNAL_BUY,
    SIGNAL_STRONG_BUY,
    SIGNAL_SELL,
    SIGNAL_STRONG_SELL,
    SIGNAL_HOLD,
)


class CryptoMasterModule(BaseModule):
    """암호화폐 자동매매 마스터 모듈.

    5분마다 실행:
    1. KRW 전체 마켓 스캔
    2. 기술적 분석 (crypto_trading)
    3. 뉴스 감성 점수 (crypto_news)
    4. Fear & Greed Index
    5. 통합 점수 계산 → 매수 후보 선정
    6. 보유 포지션 매도 체크 (스탑로스/익절)
    7. 주문 실행 (페이퍼 or 실거래)
    """

    def __init__(
        self,
        config: dict[str, Any],
        upbit: UpbitClient,
        crypto_news: CryptoNewsModule,
        crypto_trading: CryptoTradingModule,
    ):
        super().__init__("crypto_master", config)
        self._upbit = upbit
        self._news = crypto_news
        self._trading = crypto_trading

        # 설정
        risk = config.get("risk", {})
        self._max_positions = risk.get("max_positions", 5)
        self._max_single_pct = risk.get("max_single_position_pct", 20.0)
        self._daily_loss_limit_pct = risk.get("daily_loss_limit_pct", 5.0)
        self._stop_loss_pct = risk.get("stop_loss_pct", 8.0)
        self._take_profit_pct = risk.get("take_profit_pct", 15.0)
        self._min_confidence = risk.get("min_confidence", 0.65)
        self._min_order_krw = risk.get("min_order_krw", 5000)
        self._paper_trading = config.get("use_paper_trading", True)

        # 신호 통합 가중치
        signal_weights = config.get("modules", {}).get("crypto_trading", {}).get(
            "signal_weights", {}
        )
        self._w_technical = signal_weights.get("technical", 0.5)
        self._w_news = signal_weights.get("news", 0.3)
        self._w_fear_greed = signal_weights.get("fear_greed", 0.2)

        # 상태
        self._positions: dict[str, dict] = {}  # {market: position_dict}
        self._paper_balance: float = 1_000_000.0  # 페이퍼 트레이딩 초기 잔고 (100만원)
        self._daily_start_value: float = 0.0
        self._markets_cache: list[str] = []

    async def initialize(self) -> None:
        """모듈 초기화: 잔고 로드, 마켓 목록 캐시."""
        try:
            markets_data = await self._upbit.get_markets(krw_only=True)
            self._markets_cache = [m["market"] for m in markets_data]
            logger.info(f"KRW 마켓 목록 로드: {len(self._markets_cache)}개")
        except Exception as e:
            logger.error(f"마켓 목록 로드 실패: {e}")

        if not self._paper_trading:
            await self._sync_real_positions()

        logger.info(
            f"크립토 마스터 초기화 완료 (모드: {'페이퍼' if self._paper_trading else '실거래'})"
        )

    async def execute(self) -> dict:
        """5분마다 실행되는 메인 루틴."""
        if not self._markets_cache:
            try:
                markets_data = await self._upbit.get_markets(krw_only=True)
                self._markets_cache = [m["market"] for m in markets_data]
            except Exception as e:
                logger.error(f"마켓 목록 재로드 실패: {e}")
                return {}

        result = {
            "checked_markets": 0,
            "buy_orders": 0,
            "sell_orders": 0,
            "positions": len(self._positions),
        }

        # 1. 일일 손실 한도 체크
        if await self._is_daily_loss_exceeded():
            logger.warning("크립토 일일 손실 한도 초과. 신규 매수 중단.")
            await self._check_stop_losses()
            return result

        # 2. 현재 포지션 가격 업데이트
        await self._update_position_prices()

        # 3. 보유 코인 매도 체크 (스탑로스 / 익절)
        sell_count = await self._check_sell_conditions()
        result["sell_orders"] = sell_count

        # 4. 매수 후보 스캔 (최대 포지션 미만일 때만)
        if len(self._positions) < self._max_positions:
            buy_count, checked = await self._scan_buy_opportunities()
            result["buy_orders"] = buy_count
            result["checked_markets"] = checked

        result["positions"] = len(self._positions)
        logger.info(
            f"크립토 마스터 실행 완료: 스캔={result['checked_markets']}, "
            f"매수={result['buy_orders']}, 매도={result['sell_orders']}, "
            f"보유={result['positions']}"
        )
        return result

    async def shutdown(self) -> None:
        logger.info("크립토 마스터 모듈 종료")

    # ─── 매수 스캔 (AI 종목선정 기반) ───────────────────────

    async def _scan_buy_opportunities(self) -> tuple[int, int]:
        """AI 선정 종목 우선 분석 → 통합 점수 계산 → 매수 실행.

        흐름:
        1. CryptoNewsModule에서 AI 선정 종목 가져오기 (최대 8~10개)
        2. AI 선정 없으면 거래량 상위 10개로 폴백 (50→10 축소)
        3. 선정된 종목에만 기술적 분석 실행
        4. 통합 점수: AI신뢰도(40%) + 기술적(40%) + Fear&Greed(20%)
        5. min_confidence 이상이면 매수

        Returns:
            (buy_count, checked_count)
        """
        held_markets = set(self._positions.keys())
        candidate_markets = [m for m in self._markets_cache if m not in held_markets]

        # ① AI 선정 종목 조회
        ai_picks = self._news.get_ai_selected_coins()
        ai_picks_map: dict[str, float] = {}  # {market: confidence}

        if ai_picks:
            for pick in ai_picks:
                mkt = pick.get("market", "")
                conf = pick.get("confidence", 0.5)
                if mkt in self._markets_cache and mkt not in held_markets:
                    ai_picks_map[mkt] = conf
            scan_markets = list(ai_picks_map.keys())
            logger.info(f"AI 선정 종목 {len(scan_markets)}개 기술적 분석 시작")
        else:
            # ② 폴백: 거래량 상위 10개
            fallback_n = self.config.get("modules", {}).get(
                "crypto_master", {}
            ).get("scan_top_n_fallback", 10)
            scan_markets = await self._get_top_volume_markets(
                candidate_markets, top_n=fallback_n
            )
            logger.info(f"AI 선정 없음. 거래량 상위 {len(scan_markets)}개 분석 (폴백)")

        buy_count = 0
        checked = 0
        scored_markets = []

        # ③ 기술적 분석
        for market in scan_markets:
            try:
                tech_signal = await self._trading.analyze(market)
                signal = tech_signal.get("signal", SIGNAL_HOLD)
                checked += 1

                # 매수 신호 아니면 스킵 (단, AI가 선정했으면 낮은 임계값 적용)
                ai_conf = ai_picks_map.get(market, 0.0)
                if signal not in (SIGNAL_BUY, SIGNAL_STRONG_BUY):
                    if ai_conf < 0.75:  # AI 신뢰도 75% 미만이면 기술 신호 없을 때 스킵
                        continue

                total_score = self._calc_total_score_v2(market, tech_signal, ai_conf)

                if total_score >= self._min_confidence:
                    scored_markets.append((market, total_score, tech_signal, ai_conf))

            except Exception as e:
                logger.debug(f"[{market}] 분석 오류: {e}")

        # ④ 점수 내림차순 정렬 → 상위부터 매수
        scored_markets.sort(key=lambda x: x[1], reverse=True)

        for market, score, tech_signal, ai_conf in scored_markets:
            if len(self._positions) >= self._max_positions:
                break
            try:
                success = await self._execute_buy(market, score, tech_signal)
                if success:
                    buy_count += 1
                    # AI 선정 종목이면 선정 당시 가격 DB 업데이트
                    if ai_conf > 0:
                        await self._update_ai_selection_price(
                            market, tech_signal.get("current_price", 0)
                        )
            except Exception as e:
                logger.error(f"[{market}] 매수 실행 오류: {e}")

        return buy_count, checked

    def _calc_total_score_v2(
        self, market: str, tech_signal: dict, ai_confidence: float = 0.0
    ) -> float:
        """AI신뢰도 + 기술적 분석 + Fear&Greed 통합 점수 (0~1).

        가중치:
        - AI 종목선정 신뢰도: 40%
        - 기술적 분석:       40%
        - Fear & Greed:      20%
        """
        # AI 신뢰도 점수 (없으면 뉴스 감성 점수로 대체)
        if ai_confidence > 0:
            ai_score = ai_confidence * 0.4
        else:
            news_score = self._news.get_coin_news_score(market)
            ai_score = news_score * 0.4

        # 기술적 점수
        tech_confidence = tech_signal.get("confidence", 0.0)
        signal = tech_signal.get("signal", SIGNAL_HOLD)
        if signal in (SIGNAL_SELL, SIGNAL_STRONG_SELL):
            tech_confidence = 0.0  # 매도 신호면 0점
        tech_score = tech_confidence * 0.4

        # Fear & Greed 점수 (0~100 → 0~1)
        fg_index = self._news.get_fear_greed_index()
        fg_score = (fg_index / 100.0) * 0.2

        total = ai_score + tech_score + fg_score
        return round(min(total, 1.0), 3)

    # 구버전 호환 (dashboard 등에서 참조할 수 있음)
    def _calc_total_score(self, market: str, tech_signal: dict) -> float:
        return self._calc_total_score_v2(market, tech_signal, ai_confidence=0.0)

    async def _update_ai_selection_price(
        self, market: str, price: float
    ) -> None:
        """AI 선정 종목 매수 시 선정 당시 가격을 DB에 업데이트."""
        try:
            db = await get_db()
            await db.execute(
                """UPDATE ai_selections
                   SET price_at_selection = ?
                   WHERE id = (
                       SELECT id FROM ai_selections
                       WHERE market = ?
                         AND price_at_selection = 0
                       ORDER BY selected_at DESC
                       LIMIT 1
                   )""",
                (price, market),
            )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.debug(f"AI 선정 가격 업데이트 오류: {e}")

    async def _get_top_volume_markets(
        self, markets: list[str], top_n: int = 50
    ) -> list[str]:
        """거래량 상위 N개 마켓 반환."""
        try:
            # 100개씩 나눠서 현재가 조회
            all_tickers = []
            for i in range(0, len(markets), 100):
                batch = markets[i : i + 100]
                tickers = await self._upbit.get_ticker(batch)
                all_tickers.extend(tickers)

            # 거래대금 기준 정렬
            all_tickers.sort(
                key=lambda x: float(x.get("acc_trade_price_24h", 0)), reverse=True
            )
            return [t["market"] for t in all_tickers[:top_n]]
        except Exception as e:
            logger.error(f"거래량 상위 마켓 조회 실패: {e}")
            return markets[:top_n]

    # ─── 매수 실행 ────────────────────────────────────────

    async def _execute_buy(
        self, market: str, total_score: float, tech_signal: dict
    ) -> bool:
        """매수 주문 실행.

        Returns:
            True if 주문 성공
        """
        current_price = tech_signal.get("current_price", 0)
        if current_price <= 0:
            return False

        # 가용 KRW 조회
        if self._paper_trading:
            available_krw = self._paper_balance
        else:
            try:
                available_krw = await self._upbit.get_krw_balance()
            except Exception as e:
                logger.error(f"KRW 잔고 조회 실패: {e}")
                return False

        # 포지션 사이즈 계산
        total_portfolio = available_krw + sum(
            pos.get("current_value", 0) for pos in self._positions.values()
        )
        order_krw = min(
            available_krw,
            total_portfolio * (self._max_single_pct / 100),
        )

        if order_krw < self._min_order_krw:
            logger.info(f"[{market}] 가용 KRW 부족 ({order_krw:.0f}원)")
            return False

        # 수량 계산
        volume = order_krw / current_price

        stop_loss = tech_signal.get("stop_loss_price", current_price * (1 - self._stop_loss_pct / 100))
        take_profit = tech_signal.get("take_profit_price", current_price * (1 + self._take_profit_pct / 100))

        logger.info(
            f"[크립토 매수] {market} | 점수: {total_score:.3f} | "
            f"가격: {current_price:,.0f} KRW | 금액: {order_krw:,.0f} KRW | "
            f"스탑: {stop_loss:,.0f} | 익절: {take_profit:,.0f}"
        )

        order_result = await self._upbit.place_order(
            market=market,
            side="bid",
            price=order_krw,
            ord_type="price",  # 시장가 매수
        )

        if order_result:
            # 포지션 등록
            self._positions[market] = {
                "market": market,
                "volume": volume,
                "avg_price": current_price,
                "current_price": current_price,
                "current_value": order_krw,
                "pnl_pct": 0.0,
                "stop_loss_price": stop_loss,
                "take_profit_price": take_profit,
                "total_score": total_score,
                "opened_at": datetime.now().isoformat(),
                "is_paper": self._paper_trading,
            }

            if self._paper_trading:
                self._paper_balance -= order_krw

            # DB 저장
            await self._save_order(market, "bid", volume, current_price, order_result)
            await self._save_position(market)
            return True

        return False

    # ─── 매도 체크 ────────────────────────────────────────

    async def _check_sell_conditions(self) -> int:
        """보유 코인 매도 조건 체크 (스탑로스 / 익절 / 기술적 매도 신호)."""
        sell_count = 0
        markets_to_sell = []

        for market, pos in self._positions.items():
            current_price = pos.get("current_price", 0)
            avg_price = pos.get("avg_price", 0)
            if avg_price <= 0:
                continue

            pnl_pct = (current_price / avg_price - 1) * 100
            pos["pnl_pct"] = pnl_pct

            stop_loss = pos.get("stop_loss_price", avg_price * (1 - self._stop_loss_pct / 100))
            take_profit = pos.get("take_profit_price", avg_price * (1 + self._take_profit_pct / 100))

            should_sell = False
            reason = ""

            # 스탑로스
            if current_price <= stop_loss:
                should_sell = True
                reason = f"스탑로스 ({pnl_pct:.1f}%)"

            # 익절
            elif current_price >= take_profit:
                should_sell = True
                reason = f"익절 목표 달성 ({pnl_pct:.1f}%)"

            # 기술적 강력 매도 신호
            elif pnl_pct > -2:  # 큰 손실 상태가 아닐 때만 신호 체크
                try:
                    tech = self._trading.get_signal(market)
                    if tech and tech.get("signal") == SIGNAL_STRONG_SELL:
                        should_sell = True
                        reason = f"기술적 강력 매도 신호 ({pnl_pct:.1f}%)"
                except Exception:
                    pass

            if should_sell:
                markets_to_sell.append((market, reason))

        for market, reason in markets_to_sell:
            try:
                success = await self._execute_sell(market, reason)
                if success:
                    sell_count += 1
            except Exception as e:
                logger.error(f"[{market}] 매도 실행 오류: {e}")

        return sell_count

    async def _check_stop_losses(self) -> None:
        """일일 손실 한도 초과 시 긴급 스탑로스 점검."""
        for market, pos in list(self._positions.items()):
            pnl_pct = pos.get("pnl_pct", 0)
            if pnl_pct <= -self._stop_loss_pct:
                await self._execute_sell(market, f"긴급 손절 (일손실 한도 초과, {pnl_pct:.1f}%)")

    async def _execute_sell(self, market: str, reason: str) -> bool:
        """매도 주문 실행."""
        pos = self._positions.get(market)
        if not pos:
            return False

        volume = pos.get("volume", 0)
        current_price = pos.get("current_price", 0)
        pnl_pct = pos.get("pnl_pct", 0)

        logger.info(
            f"[크립토 매도] {market} | 사유: {reason} | "
            f"수익률: {pnl_pct:.1f}% | 가격: {current_price:,.0f} KRW"
        )

        order_result = await self._upbit.place_order(
            market=market,
            side="ask",
            volume=volume,
            ord_type="market",  # 시장가 매도
        )

        if order_result:
            # 포지션 제거
            sell_value = volume * current_price
            if self._paper_trading:
                self._paper_balance += sell_value

            await self._save_order(market, "ask", volume, current_price, order_result)

            # DB 포지션 업데이트
            try:
                db = await get_db()
                await db.execute(
                    "UPDATE crypto_positions SET updated_at = ? WHERE market = ?",
                    (datetime.now().isoformat(), market),
                )
                await db.commit()
                await db.close()
            except Exception as e:
                logger.error(f"포지션 DB 업데이트 오류: {e}")

            del self._positions[market]
            return True

        return False

    # ─── 포지션 관리 ──────────────────────────────────────

    async def _update_position_prices(self) -> None:
        """보유 포지션 현재가 업데이트."""
        if not self._positions:
            return
        try:
            # 상장폐지/미지원 마켓 필터링
            valid_markets = [
                m for m in self._positions
                if not self._markets_cache or m in self._markets_cache
            ]
            invalid = set(self._positions) - set(valid_markets)
            if invalid:
                logger.warning(f"가격 조회 불가 마켓 건너뜀: {invalid}")
            if not valid_markets:
                return

            tickers = await self._upbit.get_ticker(valid_markets)
            price_map = {t["market"]: float(t.get("trade_price", 0)) for t in tickers}

            for market, pos in self._positions.items():
                price = price_map.get(market, pos.get("current_price", 0))
                pos["current_price"] = price
                pos["current_value"] = pos.get("volume", 0) * price
                avg = pos.get("avg_price", 0)
                pos["pnl_pct"] = (price / avg - 1) * 100 if avg > 0 else 0
        except Exception as e:
            logger.error(f"포지션 가격 업데이트 오류: {e}")

    async def _sync_real_positions(self) -> None:
        """실거래 모드: 업비트 실계정 잔고와 포지션 동기화."""
        try:
            balances = await self._upbit.get_balance()
            for item in balances:
                currency = item.get("currency", "")
                if currency == "KRW":
                    continue
                balance = float(item.get("balance", 0))
                avg_price = float(item.get("avg_buy_price", 0))
                if balance <= 0:
                    continue
                market = f"KRW-{currency}"
                # 유효한 KRW 마켓인지 확인 (상장폐지/미지원 코인 제외)
                if self._markets_cache and market not in self._markets_cache:
                    logger.warning(f"유효하지 않은 마켓 건너뜀: {market}")
                    continue
                self._positions[market] = {
                    "market": market,
                    "volume": balance,
                    "avg_price": avg_price,
                    "current_price": avg_price,
                    "current_value": balance * avg_price,
                    "pnl_pct": 0.0,
                    "stop_loss_price": avg_price * (1 - self._stop_loss_pct / 100),
                    "take_profit_price": avg_price * (1 + self._take_profit_pct / 100),
                    "opened_at": datetime.now().isoformat(),
                    "is_paper": False,
                }
            logger.info(f"실계정 포지션 동기화: {len(self._positions)}개")
        except Exception as e:
            logger.error(f"실계정 포지션 동기화 실패: {e}")

    async def _is_daily_loss_exceeded(self) -> bool:
        """일일 손실 한도 초과 여부."""
        if not self._positions:
            return False

        total_value = sum(pos.get("current_value", 0) for pos in self._positions.values())
        total_cost = sum(
            pos.get("avg_price", 0) * pos.get("volume", 0)
            for pos in self._positions.values()
        )
        if total_cost == 0:
            return False

        loss_pct = (total_value / total_cost - 1) * 100
        return loss_pct <= -self._daily_loss_limit_pct

    # ─── 포트폴리오 요약 ──────────────────────────────────

    def get_portfolio_summary(self) -> dict:
        """현재 크립토 포트폴리오 요약."""
        total_value = sum(pos.get("current_value", 0) for pos in self._positions.values())
        total_cost = sum(
            pos.get("avg_price", 0) * pos.get("volume", 0)
            for pos in self._positions.values()
        )
        total_pnl_pct = (total_value / total_cost - 1) * 100 if total_cost > 0 else 0

        return {
            "positions": list(self._positions.values()),
            "total_positions": len(self._positions),
            "total_value": round(total_value, 0),
            "total_cost": round(total_cost, 0),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "paper_krw_balance": self._paper_balance if self._paper_trading else None,
            "fear_greed_index": self._news.get_fear_greed_index(),
            "is_paper": self._paper_trading,
        }

    # ─── DB 저장 ───────────────────────────────────────────

    async def _save_order(
        self,
        market: str,
        side: str,
        volume: float,
        price: float,
        order_result: dict,
    ) -> None:
        try:
            db = await get_db()
            await db.execute(
                """INSERT INTO crypto_orders
                   (market, side, volume, price, ord_type, status, uuid, is_paper)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market,
                    side,
                    volume,
                    price,
                    order_result.get("ord_type", "market"),
                    order_result.get("state", "done"),
                    order_result.get("uuid", ""),
                    1 if self._paper_trading else 0,
                ),
            )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"주문 DB 저장 오류: {e}")

    async def _save_position(self, market: str) -> None:
        pos = self._positions.get(market)
        if not pos:
            return
        try:
            db = await get_db()
            await db.execute(
                """INSERT OR REPLACE INTO crypto_positions
                   (market, volume, avg_price, current_price, pnl_pct,
                    stop_loss_price, take_profit_price, is_paper)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    market,
                    pos.get("volume", 0),
                    pos.get("avg_price", 0),
                    pos.get("current_price", 0),
                    pos.get("pnl_pct", 0),
                    pos.get("stop_loss_price", 0),
                    pos.get("take_profit_price", 0),
                    1 if self._paper_trading else 0,
                ),
            )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"포지션 DB 저장 오류: {e}")
