"""모듈 간 데이터 교환용 Pydantic 모델."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ─── Enums ────────────────────────────────────────────────

class Sentiment(str, Enum):
    VERY_POSITIVE = "very_positive"
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    VERY_NEGATIVE = "very_negative"


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class SignalStrength(str, Enum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class ModuleStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    ERROR = "error"
    STOPPED = "stopped"


# ─── 뉴스 / 센티먼트 모델 ─────────────────────────────────

class NewsArticle(BaseModel):
    """뉴스 기사 데이터."""
    title: str
    content: str
    source: str
    url: str
    published_at: datetime
    sentiment: Sentiment = Sentiment.NEUTRAL
    impact_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    related_tickers: list[str] = Field(default_factory=list)
    ai_summary: str = ""


class RumorData(BaseModel):
    """루머/센티먼트 데이터."""
    content: str
    source_channel: str
    channel_trust_score: float = Field(default=0.5, ge=0.0, le=1.0)
    sentiment: Sentiment = Sentiment.NEUTRAL
    impact_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    related_tickers: list[str] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=datetime.now)
    verified: Optional[bool] = None


class PolicyEvent(BaseModel):
    """정책/정치 이벤트 데이터."""
    title: str
    content: str
    source: str
    event_type: str  # "policy", "speech", "regulation"
    published_at: datetime
    impact_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    beneficiary_sectors: list[str] = Field(default_factory=list)
    affected_tickers: list[str] = Field(default_factory=list)
    ai_analysis: str = ""


# ─── 트레이딩 모델 ─────────────────────────────────────────

class StockCandidate(BaseModel):
    """투자 후보 종목."""
    ticker: str
    name: str
    score: float = Field(default=0.0, ge=0.0, le=100.0)
    reasons: list[str] = Field(default_factory=list)
    signal: SignalStrength = SignalStrength.HOLD
    news_score: float = 0.0
    sentiment_score: float = 0.0
    policy_score: float = 0.0
    technical_score: float = 0.0
    target_price: Optional[float] = None
    stop_loss_price: Optional[float] = None
    evaluated_at: datetime = Field(default_factory=datetime.now)


class TechnicalSignal(BaseModel):
    """기술적 분석 시그널."""
    ticker: str
    signal: SignalStrength
    indicators: dict[str, float] = Field(default_factory=dict)
    entry_price: Optional[float] = None
    target_price: Optional[float] = None
    stop_loss: Optional[float] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    timestamp: datetime = Field(default_factory=datetime.now)


class Order(BaseModel):
    """주문 데이터."""
    order_id: Optional[str] = None
    ticker: str
    side: OrderSide
    order_type: OrderType = OrderType.LIMIT
    quantity: int
    price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    filled_price: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)


class Position(BaseModel):
    """보유 포지션."""
    ticker: str
    name: str
    quantity: int
    avg_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    opened_at: datetime = Field(default_factory=datetime.now)


# ─── 모듈 상태 / 리포트 ────────────────────────────────────

class ModuleHealthReport(BaseModel):
    """모듈 상태 리포트."""
    module_name: str
    status: ModuleStatus
    last_execution: Optional[datetime] = None
    execution_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    accuracy_score: Optional[float] = None
    details: dict = Field(default_factory=dict)


class DailyPerformanceReport(BaseModel):
    """일일 성과 리포트."""
    date: datetime
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    module_reports: list[ModuleHealthReport] = Field(default_factory=list)
