"""모듈 간 이벤트 버스 (Pub/Sub 패턴)."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine

from loguru import logger


@dataclass
class Event:
    """이벤트 데이터."""
    event_type: str
    data: Any = None
    source: str = ""
    timestamp: datetime = field(default_factory=datetime.now)


# 이벤트 핸들러 타입
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """비동기 이벤트 버스. 모듈 간 느슨한 결합을 위한 Pub/Sub 시스템."""

    _instance: EventBus | None = None

    def __new__(cls) -> EventBus:
        """싱글턴."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._subscribers = defaultdict(list)
            cls._instance._history = []
        return cls._instance

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """이벤트 구독."""
        self._subscribers[event_type].append(handler)
        logger.debug(f"이벤트 구독: {event_type} -> {handler.__qualname__}")

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """이벤트 구독 해제."""
        if handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        """이벤트 발행. 모든 구독자에게 비동기로 전달."""
        self._history.append(event)
        # 최근 1000개만 유지
        if len(self._history) > 1000:
            self._history = self._history[-500:]

        handlers = self._subscribers.get(event.event_type, [])
        if not handlers:
            return

        tasks = []
        for handler in handlers:
            try:
                tasks.append(asyncio.create_task(handler(event)))
            except Exception as e:
                logger.error(f"이벤트 핸들러 생성 실패: {handler.__qualname__} - {e}")

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"이벤트 핸들러 오류: {handlers[i].__qualname__} - {result}")

    def get_history(self, event_type: str | None = None, limit: int = 50) -> list[Event]:
        """이벤트 히스토리 조회."""
        if event_type:
            filtered = [e for e in self._history if e.event_type == event_type]
        else:
            filtered = self._history
        return filtered[-limit:]

    @classmethod
    def reset(cls) -> None:
        """싱글턴 리셋 (테스트용)."""
        cls._instance = None


# 표준 이벤트 타입 상수
class EventTypes:
    # 뉴스 / 센티먼트
    NEWS_COLLECTED = "news.collected"
    NEWS_ANALYZED = "news.analyzed"
    RUMOR_DETECTED = "rumor.detected"
    SENTIMENT_UPDATED = "sentiment.updated"

    # 정책
    POLICY_DETECTED = "policy.detected"
    POLICY_ANALYZED = "policy.analyzed"

    # 종목 후보
    CANDIDATES_UPDATED = "candidates.updated"

    # 트레이딩
    SIGNAL_GENERATED = "signal.generated"
    ORDER_SUBMITTED = "order.submitted"
    ORDER_FILLED = "order.filled"
    ORDER_CANCELLED = "order.cancelled"
    POSITION_OPENED = "position.opened"
    POSITION_CLOSED = "position.closed"

    # 시스템
    MARKET_OPEN = "market.open"
    MARKET_CLOSE = "market.close"
    MODULE_ERROR = "module.error"
    DAILY_REPORT = "daily.report"
