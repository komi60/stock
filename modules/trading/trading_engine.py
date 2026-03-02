"""모듈 4: 트레이딩 엔진 (Technical Trading Engine).

기계적 차트 분석 및 기술적 지표 기반 매매 타점 포착.
이동평균선, RSI, MACD, 볼린저 밴드 등 전통 알고리즘 사용.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd
from loguru import logger

from broker.kis_api import KISClient
from core.base_module import TradingModule
from core.data_models import SignalStrength, TechnicalSignal
from core.events import Event, EventBus, EventTypes

try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False
    logger.warning("pandas_ta 미설치. 내장 지표 계산 사용.")


class TradingEngine(TradingModule):
    """기술적 분석 기반 트레이딩 엔진."""

    def __init__(self, config: dict[str, Any], kis_client: KISClient):
        super().__init__("trading_engine", config)
        self._kis = kis_client
        self._signals: dict[str, TechnicalSignal] = {}
        self._event_bus = EventBus()

    async def initialize(self) -> None:
        logger.info("트레이딩 엔진 초기화 완료")

    async def execute(self) -> dict[str, TechnicalSignal]:
        """등록된 관심 종목에 대해 기술적 분석 실행."""
        return self._signals

    async def shutdown(self) -> None:
        logger.info("트레이딩 엔진 종료")

    async def analyze(self, ticker: str) -> TechnicalSignal:
        """특정 종목 기술적 분석 수행."""
        try:
            # 1. 일봉 데이터 조회
            candles = await self._kis.get_daily_chart(ticker, period="D", count=120)
            if len(candles) < 20:
                logger.warning(f"[{ticker}] 차트 데이터 부족: {len(candles)}봉")
                return TechnicalSignal(ticker=ticker, signal=SignalStrength.HOLD)

            df = self._candles_to_df(candles)

            # 2. 기술적 지표 계산
            indicators = self._calculate_indicators(df)

            # 3. 종합 시그널 판단
            signal, confidence = self._evaluate_signal(indicators, df)

            # 4. 목표가 / 손절가 계산
            current_price = df["close"].iloc[-1]
            target_price, stop_loss = self._calc_price_targets(
                current_price, indicators, signal
            )

            tech_signal = TechnicalSignal(
                ticker=ticker,
                signal=signal,
                indicators=indicators,
                entry_price=current_price,
                target_price=target_price,
                stop_loss=stop_loss,
                confidence=confidence,
            )

            self._signals[ticker] = tech_signal

            # 이벤트 발행
            await self._event_bus.publish(Event(
                event_type=EventTypes.SIGNAL_GENERATED,
                data=tech_signal,
                source=self.name,
            ))

            return tech_signal

        except Exception as e:
            logger.error(f"[{ticker}] 기술적 분석 오류: {e}")
            return TechnicalSignal(ticker=ticker, signal=SignalStrength.HOLD)

    async def get_signal(self, ticker: str) -> TechnicalSignal:
        """캐시된 시그널 반환, 없으면 새로 분석."""
        if ticker in self._signals:
            sig = self._signals[ticker]
            # 5분 이내 캐시 사용
            age = (datetime.now() - sig.timestamp).total_seconds()
            if age < 300:
                return sig
        return await self.analyze(ticker)

    # ─── 데이터 변환 ───────────────────────────────────────

    @staticmethod
    def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
        """캔들 데이터를 DataFrame으로 변환."""
        df = pd.DataFrame(candles)
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ─── 지표 계산 ─────────────────────────────────────────

    def _calculate_indicators(self, df: pd.DataFrame) -> dict[str, float]:
        """기술적 지표 일괄 계산."""
        indicators = {}
        close = df["close"]
        high = df["high"]
        low = df["low"]

        if HAS_PANDAS_TA:
            indicators.update(self._calc_with_pandas_ta(df))
        else:
            indicators.update(self._calc_builtin(df))

        # 추가 지표: 가격 위치
        if len(close) >= 20:
            sma20 = close.rolling(20).mean().iloc[-1]
            indicators["price_vs_sma20"] = (close.iloc[-1] / sma20 - 1) * 100

        if len(close) >= 60:
            sma60 = close.rolling(60).mean().iloc[-1]
            indicators["price_vs_sma60"] = (close.iloc[-1] / sma60 - 1) * 100

        # 거래량 지표
        if len(df) >= 20:
            avg_vol = df["volume"].rolling(20).mean().iloc[-1]
            cur_vol = df["volume"].iloc[-1]
            indicators["volume_ratio"] = (cur_vol / avg_vol) if avg_vol > 0 else 1.0

        return indicators

    def _calc_with_pandas_ta(self, df: pd.DataFrame) -> dict[str, float]:
        """pandas_ta를 이용한 지표 계산."""
        indicators = {}
        close = df["close"]

        # SMA
        sma5 = ta.sma(close, length=5)
        sma20 = ta.sma(close, length=20)
        sma60 = ta.sma(close, length=60)
        if sma5 is not None and len(sma5) > 0:
            indicators["sma5"] = float(sma5.iloc[-1]) if not pd.isna(sma5.iloc[-1]) else 0
        if sma20 is not None and len(sma20) > 0:
            indicators["sma20"] = float(sma20.iloc[-1]) if not pd.isna(sma20.iloc[-1]) else 0
        if sma60 is not None and len(sma60) > 0:
            indicators["sma60"] = float(sma60.iloc[-1]) if not pd.isna(sma60.iloc[-1]) else 0

        # EMA
        ema12 = ta.ema(close, length=12)
        ema26 = ta.ema(close, length=26)
        if ema12 is not None and len(ema12) > 0:
            indicators["ema12"] = float(ema12.iloc[-1]) if not pd.isna(ema12.iloc[-1]) else 0
        if ema26 is not None and len(ema26) > 0:
            indicators["ema26"] = float(ema26.iloc[-1]) if not pd.isna(ema26.iloc[-1]) else 0

        # RSI
        rsi = ta.rsi(close, length=14)
        if rsi is not None and len(rsi) > 0:
            indicators["rsi"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50

        # MACD
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        if macd_df is not None and len(macd_df) > 0:
            indicators["macd"] = float(macd_df.iloc[-1, 0]) if not pd.isna(macd_df.iloc[-1, 0]) else 0
            indicators["macd_signal"] = float(macd_df.iloc[-1, 1]) if not pd.isna(macd_df.iloc[-1, 1]) else 0
            indicators["macd_hist"] = float(macd_df.iloc[-1, 2]) if not pd.isna(macd_df.iloc[-1, 2]) else 0

        # 볼린저 밴드
        bbands = ta.bbands(close, length=20, std=2)
        if bbands is not None and len(bbands) > 0:
            indicators["bb_upper"] = float(bbands.iloc[-1, 0]) if not pd.isna(bbands.iloc[-1, 0]) else 0
            indicators["bb_mid"] = float(bbands.iloc[-1, 1]) if not pd.isna(bbands.iloc[-1, 1]) else 0
            indicators["bb_lower"] = float(bbands.iloc[-1, 2]) if not pd.isna(bbands.iloc[-1, 2]) else 0
            if indicators["bb_upper"] != indicators["bb_lower"]:
                indicators["bb_position"] = (
                    (close.iloc[-1] - indicators["bb_lower"])
                    / (indicators["bb_upper"] - indicators["bb_lower"])
                )

        # Stochastic
        stoch = ta.stoch(df["high"], df["low"], close)
        if stoch is not None and len(stoch) > 0:
            indicators["stoch_k"] = float(stoch.iloc[-1, 0]) if not pd.isna(stoch.iloc[-1, 0]) else 50
            indicators["stoch_d"] = float(stoch.iloc[-1, 1]) if not pd.isna(stoch.iloc[-1, 1]) else 50

        return indicators

    def _calc_builtin(self, df: pd.DataFrame) -> dict[str, float]:
        """내장 지표 계산 (pandas_ta 없을 때 폴백)."""
        indicators = {}
        close = df["close"]

        # SMA
        for period in [5, 20, 60]:
            if len(close) >= period:
                indicators[f"sma{period}"] = float(close.rolling(period).mean().iloc[-1])

        # EMA
        for period in [12, 26]:
            if len(close) >= period:
                indicators[f"ema{period}"] = float(close.ewm(span=period, adjust=False).mean().iloc[-1])

        # RSI
        if len(close) >= 15:
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            indicators["rsi"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50

        # MACD
        if len(close) >= 26:
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            macd_line = ema12 - ema26
            signal_line = macd_line.ewm(span=9, adjust=False).mean()
            indicators["macd"] = float(macd_line.iloc[-1])
            indicators["macd_signal"] = float(signal_line.iloc[-1])
            indicators["macd_hist"] = float((macd_line - signal_line).iloc[-1])

        # 볼린저 밴드
        if len(close) >= 20:
            sma20 = close.rolling(20).mean()
            std20 = close.rolling(20).std()
            indicators["bb_upper"] = float((sma20 + 2 * std20).iloc[-1])
            indicators["bb_mid"] = float(sma20.iloc[-1])
            indicators["bb_lower"] = float((sma20 - 2 * std20).iloc[-1])
            bb_range = indicators["bb_upper"] - indicators["bb_lower"]
            if bb_range > 0:
                indicators["bb_position"] = (close.iloc[-1] - indicators["bb_lower"]) / bb_range

        return indicators

    # ─── 시그널 판단 ───────────────────────────────────────

    def _evaluate_signal(
        self, indicators: dict[str, float], df: pd.DataFrame
    ) -> tuple[SignalStrength, float]:
        """기술적 지표들을 종합하여 매매 시그널 판단.

        Returns:
            (signal, confidence)
        """
        scores = []  # 양수 = 매수, 음수 = 매도
        close = df["close"].iloc[-1]

        # 1. 이동평균 배열 (정배열/역배열)
        sma5 = indicators.get("sma5", 0)
        sma20 = indicators.get("sma20", 0)
        sma60 = indicators.get("sma60", 0)

        if sma5 and sma20 and sma60:
            if sma5 > sma20 > sma60:  # 정배열
                scores.append(2)
            elif sma5 < sma20 < sma60:  # 역배열
                scores.append(-2)
            elif close > sma20:  # 20일선 위
                scores.append(1)
            else:
                scores.append(-1)

        # 2. RSI
        rsi = indicators.get("rsi", 50)
        if rsi < 30:
            scores.append(2)  # 과매도 → 매수 기회
        elif rsi < 40:
            scores.append(1)
        elif rsi > 70:
            scores.append(-2)  # 과매수 → 매도 신호
        elif rsi > 60:
            scores.append(-1)
        else:
            scores.append(0)

        # 3. MACD
        macd_hist = indicators.get("macd_hist", 0)
        macd = indicators.get("macd", 0)
        macd_signal = indicators.get("macd_signal", 0)

        if macd_hist > 0 and macd > macd_signal:
            scores.append(1.5)
        elif macd_hist < 0 and macd < macd_signal:
            scores.append(-1.5)
        else:
            scores.append(0)

        # MACD 골든/데드크로스 (최근 전환)
        if len(df) >= 2:
            prev_hist = indicators.get("macd_hist", 0)  # 단순화
            if macd_hist > 0 and prev_hist <= 0:
                scores.append(2)  # 골든크로스
            elif macd_hist < 0 and prev_hist >= 0:
                scores.append(-2)  # 데드크로스

        # 4. 볼린저 밴드 위치
        bb_pos = indicators.get("bb_position", 0.5)
        if bb_pos < 0.1:
            scores.append(2)  # 하단 돌파 → 매수
        elif bb_pos < 0.3:
            scores.append(1)
        elif bb_pos > 0.9:
            scores.append(-2)  # 상단 돌파 → 매도
        elif bb_pos > 0.7:
            scores.append(-1)
        else:
            scores.append(0)

        # 5. 거래량
        vol_ratio = indicators.get("volume_ratio", 1.0)
        if vol_ratio > 2.0:
            # 거래량 급증은 현재 추세 강화
            if sum(scores) > 0:
                scores.append(1)
            else:
                scores.append(-1)

        # 6. Stochastic
        stoch_k = indicators.get("stoch_k", 50)
        stoch_d = indicators.get("stoch_d", 50)
        if stoch_k < 20 and stoch_d < 20:
            scores.append(1.5)
        elif stoch_k > 80 and stoch_d > 80:
            scores.append(-1.5)

        # 종합 점수
        total = sum(scores)
        max_possible = len(scores) * 2
        normalized = total / max_possible if max_possible > 0 else 0

        # 시그널 결정
        if normalized > 0.5:
            signal = SignalStrength.STRONG_BUY
        elif normalized > 0.2:
            signal = SignalStrength.BUY
        elif normalized < -0.5:
            signal = SignalStrength.STRONG_SELL
        elif normalized < -0.2:
            signal = SignalStrength.SELL
        else:
            signal = SignalStrength.HOLD

        confidence = min(abs(normalized), 1.0)
        return signal, round(confidence, 3)

    # ─── 목표가/손절가 ────────────────────────────────────

    def _calc_price_targets(
        self,
        current_price: float,
        indicators: dict[str, float],
        signal: SignalStrength,
    ) -> tuple[Optional[float], Optional[float]]:
        """목표가 및 손절가 계산."""
        if signal in (SignalStrength.HOLD,):
            return None, None

        bb_upper = indicators.get("bb_upper", 0)
        bb_lower = indicators.get("bb_lower", 0)
        sma20 = indicators.get("sma20", current_price)

        if signal in (SignalStrength.STRONG_BUY, SignalStrength.BUY):
            # 목표가: 볼린저 상단 또는 +5%
            target = max(bb_upper, current_price * 1.05) if bb_upper else current_price * 1.05
            # 손절: 볼린저 하단 또는 -3%
            stop = max(bb_lower, current_price * 0.97) if bb_lower else current_price * 0.97
            return round(target), round(stop)
        else:
            # 매도 시그널의 경우 (숏은 불가하므로 보유 종목 매도용)
            target = min(bb_lower, current_price * 0.95) if bb_lower else current_price * 0.95
            stop = current_price * 1.03  # 손절 = 3% 위
            return round(target), round(stop)
