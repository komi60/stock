"""모듈 5: 마스터 / 컨트롤 모듈.

모듈 1,2,3 데이터 취합 → 최종 투자 종목 선정 →
장중 모듈 4(트레이딩 엔진)와 결합 → 한국투자증권 API로 실제 주문 실행.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from broker.kis_api import KISClient
from core.base_module import BaseModule, PluginRegistry
from core.data_models import (
    Order, OrderSide, OrderStatus, OrderType,
    Position, SignalStrength, StockCandidate,
)
from core.database import get_db
from core.events import Event, EventBus, EventTypes
from core.logger import trade_logger
from modules.trading.trading_engine import TradingEngine


class MasterModule(BaseModule):
    """마스터 컨트롤 모듈.

    - 장 시작 전: 모듈 1,2,3 데이터 취합 → 최종 투자 종목 선정
    - 장 중: 모듈 4와 결합하여 실제 주문 실행
    - 리스크 관리: 포지션 한도, 일일 손실 한도 관리
    """

    def __init__(
        self,
        config: dict[str, Any],
        registry: PluginRegistry,
        kis_client: KISClient,
        trading_engine: TradingEngine,
    ):
        super().__init__("master", config)
        self._registry = registry
        self._kis = kis_client
        self._trading = trading_engine
        self._event_bus = EventBus()

        # 투자 상태
        self._candidates: list[StockCandidate] = []
        self._positions: list[Position] = []
        self._pending_orders: list[Order] = []
        self._daily_pnl: float = 0.0

        # 설정
        master_cfg = config.get("master", {})
        self._max_positions = master_cfg.get("max_positions", 10)
        self._max_single_pct = master_cfg.get("max_single_position_pct", 15.0)
        self._daily_loss_limit = master_cfg.get("daily_loss_limit_pct", 3.0)
        self._paper_trading = master_cfg.get("use_paper_trading", True)

    async def initialize(self) -> None:
        # 현재 보유 종목 로드
        try:
            self._positions = await self._kis.get_balance()
            logger.info(f"현재 보유 종목: {len(self._positions)}개")
        except Exception as e:
            logger.error(f"잔고 조회 실패: {e}")
        logger.info("마스터 모듈 초기화 완료")

    async def execute(self) -> None:
        """메인 실행 루프 (스케줄러에 의해 호출)."""
        pass

    async def shutdown(self) -> None:
        logger.info("마스터 모듈 종료")

    # ─── 장전 로직 ─────────────────────────────────────────

    async def pre_market_routine(self) -> list[StockCandidate]:
        """장 시작 전 루틴: 모듈 1,2,3 데이터 취합 → 최종 종목 선정."""
        logger.info("=== 장전 분석 시작 ===")

        # 1. 모든 DataProvider 모듈에서 후보 수집
        all_candidates: dict[str, StockCandidate] = {}

        for provider in self._registry.get_data_providers():
            try:
                candidates = await provider.get_candidates()
                for c in candidates:
                    if c.ticker in all_candidates:
                        # 기존 후보에 점수 합산
                        existing = all_candidates[c.ticker]
                        existing.news_score = max(existing.news_score, c.news_score)
                        existing.sentiment_score = max(existing.sentiment_score, c.sentiment_score)
                        existing.policy_score = max(existing.policy_score, c.policy_score)
                        existing.reasons.extend(c.reasons)
                    else:
                        all_candidates[c.ticker] = c.model_copy()
            except Exception as e:
                logger.error(f"[{provider.name}] 후보 수집 오류: {e}")

        # 2. 종합 점수 계산
        for ticker, candidate in all_candidates.items():
            candidate.score = self._calculate_composite_score(candidate)

        # 3. 점수순 정렬 및 상위 N개 선정
        sorted_candidates = sorted(
            all_candidates.values(), key=lambda x: x.score, reverse=True
        )
        self._candidates = sorted_candidates[:self._max_positions]

        # 4. DB 저장
        await self._save_candidates(self._candidates)

        # 5. 이벤트 발행
        await self._event_bus.publish(Event(
            event_type=EventTypes.CANDIDATES_UPDATED,
            data={"candidates": [c.model_dump() for c in self._candidates]},
            source=self.name,
        ))

        logger.info(f"최종 투자 후보: {len(self._candidates)}종목")
        for c in self._candidates[:5]:
            logger.info(f"  {c.ticker} | 점수: {c.score:.1f} | 뉴스:{c.news_score:.1f} 센티:{c.sentiment_score:.1f} 정책:{c.policy_score:.1f}")

        return self._candidates

    def _calculate_composite_score(self, candidate: StockCandidate) -> float:
        """뉴스/센티먼트/정책 점수 종합. 가중치 기반."""
        weights = {
            "news": 0.35,
            "sentiment": 0.25,
            "policy": 0.40,
        }
        score = (
            candidate.news_score * weights["news"]
            + candidate.sentiment_score * weights["sentiment"]
            + candidate.policy_score * weights["policy"]
        )
        return round(score, 2)

    # ─── 장중 로직 ─────────────────────────────────────────

    async def market_hours_routine(self) -> None:
        """장중 루틴: 후보 종목에 대해 기술적 분석 → 주문 실행."""
        # 리스크 체크
        if self._is_daily_loss_exceeded():
            logger.warning("일일 손실 한도 초과. 신규 매수 중단.")
            return

        # 잔고 업데이트
        try:
            self._positions = await self._kis.get_balance()
        except Exception as e:
            logger.error(f"잔고 업데이트 실패: {e}")

        # 1. 보유 종목 매도 체크
        await self._check_sell_signals()

        # 2. 후보 종목 매수 체크
        await self._check_buy_signals()

    async def _check_buy_signals(self) -> None:
        """후보 종목의 기술적 분석 기반 매수 판단."""
        if len(self._positions) >= self._max_positions:
            return

        # 이미 보유 중인 종목 제외
        held_tickers = {p.ticker for p in self._positions}

        for candidate in self._candidates:
            if candidate.ticker in held_tickers:
                continue
            if len(self._positions) >= self._max_positions:
                break

            try:
                signal = await self._trading.get_signal(candidate.ticker)

                if signal.signal in (SignalStrength.STRONG_BUY, SignalStrength.BUY):
                    # 기술적 점수 반영
                    candidate.technical_score = signal.confidence * 100
                    final_score = self._calculate_composite_score(candidate)
                    final_score = (final_score + candidate.technical_score * 0.3) / 1.3

                    if final_score >= 30:  # 최소 진입 점수
                        await self._execute_buy(candidate, signal)

            except Exception as e:
                logger.error(f"[{candidate.ticker}] 매수 분석 오류: {e}")

    async def _check_sell_signals(self) -> None:
        """보유 종목 매도 조건 체크."""
        for position in self._positions:
            try:
                signal = await self._trading.get_signal(position.ticker)

                # 매도 조건
                should_sell = False
                reason = ""

                # 1. 강한 매도 시그널
                if signal.signal == SignalStrength.STRONG_SELL:
                    should_sell = True
                    reason = "기술적 강력 매도 시그널"

                # 2. 손절 (-3%)
                elif position.unrealized_pnl_pct <= -3.0:
                    should_sell = True
                    reason = f"손절 (수익률: {position.unrealized_pnl_pct:.1f}%)"

                # 3. 목표 수익 달성 (+5%)
                elif position.unrealized_pnl_pct >= 5.0:
                    if signal.signal in (SignalStrength.SELL, SignalStrength.STRONG_SELL):
                        should_sell = True
                        reason = f"목표 수익 달성 + 매도 시그널 (수익률: {position.unrealized_pnl_pct:.1f}%)"

                if should_sell:
                    await self._execute_sell(position, reason)

            except Exception as e:
                logger.error(f"[{position.ticker}] 매도 분석 오류: {e}")

    # ─── 주문 실행 ─────────────────────────────────────────

    async def _execute_buy(
        self, candidate: StockCandidate, signal: TechnicalSignal
    ) -> Optional[str]:
        """매수 주문 실행."""
        try:
            # 가용 현금 조회
            cash = await self._kis.get_available_cash()

            # 포지션 사이즈 계산 (총 자산의 max_single_pct% 이내)
            max_amount = cash * (self._max_single_pct / 100)
            price = signal.entry_price or 0
            if price <= 0:
                return None

            quantity = int(max_amount / price)
            if quantity <= 0:
                logger.info(f"[{candidate.ticker}] 매수 금액 부족")
                return None

            order = Order(
                ticker=candidate.ticker,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=quantity,
                price=price,
            )

            trade_logger.info(
                f"매수 주문: {candidate.ticker} | {quantity}주 @ {price:,}원 | "
                f"종합점수: {candidate.score:.1f} | 사유: {', '.join(candidate.reasons[:3])}"
            )

            order_id = await self._kis.place_order(order)
            order.order_id = order_id
            order.status = OrderStatus.SUBMITTED
            self._pending_orders.append(order)

            await self._event_bus.publish(Event(
                event_type=EventTypes.ORDER_SUBMITTED,
                data=order.model_dump(),
                source=self.name,
            ))

            return order_id

        except Exception as e:
            logger.error(f"[{candidate.ticker}] 매수 주문 실패: {e}")
            return None

    async def _execute_sell(self, position: Position, reason: str) -> Optional[str]:
        """매도 주문 실행."""
        try:
            # 현재가 조회
            price_info = await self._kis.get_current_price(position.ticker)
            current_price = price_info.get("price", 0)

            order = Order(
                ticker=position.ticker,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=position.quantity,
                price=current_price,
            )

            trade_logger.info(
                f"매도 주문: {position.ticker} ({position.name}) | "
                f"{position.quantity}주 @ {current_price:,}원 | "
                f"평균단가: {position.avg_price:,}원 | "
                f"수익률: {position.unrealized_pnl_pct:.1f}% | 사유: {reason}"
            )

            order_id = await self._kis.place_order(order)
            order.order_id = order_id
            order.status = OrderStatus.SUBMITTED

            await self._event_bus.publish(Event(
                event_type=EventTypes.ORDER_SUBMITTED,
                data=order.model_dump(),
                source=self.name,
            ))

            return order_id

        except Exception as e:
            logger.error(f"[{position.ticker}] 매도 주문 실패: {e}")
            return None

    # ─── 리스크 관리 ───────────────────────────────────────

    def _is_daily_loss_exceeded(self) -> bool:
        """일일 손실 한도 초과 여부."""
        total_value = sum(p.current_price * p.quantity for p in self._positions)
        if total_value == 0:
            return False
        loss_pct = (self._daily_pnl / total_value) * 100
        return loss_pct <= -self._daily_loss_limit

    # ─── 상태 조회 ─────────────────────────────────────────

    def get_portfolio_summary(self) -> dict:
        """포트폴리오 요약."""
        total_value = sum(p.current_price * p.quantity for p in self._positions)
        total_cost = sum(p.avg_price * p.quantity for p in self._positions)
        total_pnl = total_value - total_cost

        return {
            "positions": [p.model_dump() for p in self._positions],
            "total_positions": len(self._positions),
            "total_value": total_value,
            "total_cost": total_cost,
            "total_pnl": total_pnl,
            "total_pnl_pct": (total_pnl / total_cost * 100) if total_cost > 0 else 0,
            "candidates": [c.model_dump() for c in self._candidates],
            "pending_orders": [o.model_dump() for o in self._pending_orders],
            "daily_pnl": self._daily_pnl,
        }

    # ─── DB ────────────────────────────────────────────────

    async def _save_candidates(self, candidates: list[StockCandidate]) -> None:
        try:
            db = await get_db()
            today = datetime.now().strftime("%Y-%m-%d")
            for c in candidates:
                await db.execute(
                    """INSERT INTO stock_candidates
                       (date, ticker, name, total_score, news_score, sentiment_score,
                        policy_score, technical_score, signal, reasons)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (today, c.ticker, c.name, c.score, c.news_score,
                     c.sentiment_score, c.policy_score, c.technical_score,
                     c.signal.value if c.signal else "hold",
                     "|".join(c.reasons)),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"후보 종목 DB 저장 오류: {e}")
