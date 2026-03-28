"""크립토 트레이딩 모듈.

업비트 KRW 전체 마켓 코인에 대한 기술적 분석 수행.
RSI, MACD, 볼린저 밴드, EMA, ATR 지표 기반 매수 신호 생성.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from loguru import logger

from broker.upbit_api import UpbitClient
from core.base_module import BaseModule
from core.database import get_db

try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False
    logger.warning("pandas_ta 미설치. 내장 지표 계산 사용.")

# 매매 신호 상수
SIGNAL_STRONG_BUY = "STRONG_BUY"
SIGNAL_BUY = "BUY"
SIGNAL_HOLD = "HOLD"
SIGNAL_SELL = "SELL"
SIGNAL_STRONG_SELL = "STRONG_SELL"


class CryptoTradingModule(BaseModule):
    """암호화폐 기술적 분석 모듈."""

    def __init__(self, config: dict[str, Any], upbit: UpbitClient):
        super().__init__("crypto_trading", config)
        self._upbit = upbit
        # 코인별 분석 결과 캐시: {market: signal_dict}
        self._signals: dict[str, dict] = {}
        # 분석 완료 시간 캐시
        self._signal_times: dict[str, datetime] = {}

    async def initialize(self) -> None:
        logger.info("크립토 트레이딩 모듈 초기화 완료")

    async def execute(self) -> dict[str, dict]:
        """실행 시 아무것도 하지 않음. analyze()를 통해 개별 분석."""
        return self._signals

    async def shutdown(self) -> None:
        logger.info("크립토 트레이딩 모듈 종료")

    # ─── 공개 인터페이스 ───────────────────────────────────

    async def analyze(self, market: str) -> dict:
        """특정 코인에 대한 기술적 분석.

        Args:
            market: 업비트 마켓 코드 (예: "KRW-BTC")

        Returns:
            {
                "market": "KRW-BTC",
                "signal": "BUY",
                "confidence": 0.72,
                "rsi": 38.5,
                "macd": 150000.0,
                "bb_position": 0.25,
                "atr": 2500000.0,
                "ema_trend": "bullish",
                "current_price": 85000000.0,
                "entry_price": 85000000.0,
                "stop_loss_price": 78200000.0,
                "take_profit_price": 97750000.0,
            }
        """
        # 캐시 확인 (5분 이내는 재사용)
        if market in self._signal_times:
            age = (datetime.now() - self._signal_times[market]).total_seconds()
            if age < 300 and market in self._signals:
                return self._signals[market]

        try:
            candle_count = self.config.get("candle_count", 200)
            candles = await self._upbit.get_candles_days(market, count=candle_count)
            if len(candles) < 26:
                logger.debug(f"[{market}] 캔들 데이터 부족: {len(candles)}개")
                return self._make_hold_signal(market)

            df = self._candles_to_df(candles)
            indicators = self._calculate_indicators(df)
            signal, confidence = self._evaluate_signal(indicators)
            current_price = float(df["close"].iloc[-1])
            stop_loss, take_profit = self._calc_price_targets(
                current_price, signal, indicators
            )

            result = {
                "market": market,
                "signal": signal,
                "confidence": round(confidence, 3),
                "rsi": round(indicators.get("rsi", 50), 2),
                "macd": indicators.get("macd", 0),
                "macd_hist": indicators.get("macd_hist", 0),
                "bb_position": round(indicators.get("bb_position", 0.5), 3),
                "atr": indicators.get("atr", 0),
                "ema_trend": {1.0: "bullish", -1.0: "bearish", 0.0: "neutral"}.get(
                    indicators.get("ema_trend", 0.0), "neutral"
                ),
                "current_price": current_price,
                "entry_price": current_price,
                "stop_loss_price": stop_loss,
                "take_profit_price": take_profit,
                "analyzed_at": datetime.now().isoformat(),
            }

            self._signals[market] = result
            self._signal_times[market] = datetime.now()
            return result

        except Exception as e:
            logger.error(f"[{market}] 기술적 분석 오류: {e}")
            return self._make_hold_signal(market)

    async def analyze_batch(self, markets: list[str]) -> list[dict]:
        """여러 코인 배치 분석. 결과를 confidence 내림차순으로 정렬."""
        results = []
        for market in markets:
            result = await self.analyze(market)
            results.append(result)

        results.sort(key=lambda x: x.get("confidence", 0), reverse=True)
        return results

    def get_signal(self, market: str) -> dict | None:
        """캐시된 신호 반환."""
        return self._signals.get(market)

    def get_technical_score(self, market: str) -> float:
        """코인의 기술적 분석 점수 반환 (0.0~1.0).

        BUY/STRONG_BUY 방향의 confidence를 점수로 변환.
        """
        sig = self._signals.get(market, {})
        signal = sig.get("signal", SIGNAL_HOLD)
        confidence = sig.get("confidence", 0.0)

        if signal in (SIGNAL_STRONG_BUY, SIGNAL_BUY):
            return confidence
        elif signal in (SIGNAL_STRONG_SELL, SIGNAL_SELL):
            return 0.0
        return 0.3  # HOLD → 중립

    # ─── 데이터 변환 ───────────────────────────────────────

    @staticmethod
    def _candles_to_df(candles: list[dict]) -> pd.DataFrame:
        """업비트 캔들 데이터를 DataFrame으로 변환."""
        rows = []
        for c in candles:
            rows.append({
                "date": c.get("candle_date_time_kst", c.get("candle_date_time_utc", "")),
                "open": c.get("opening_price", 0),
                "high": c.get("high_price", 0),
                "low": c.get("low_price", 0),
                "close": c.get("trade_price", 0),
                "volume": c.get("candle_acc_trade_volume", 0),
            })
        df = pd.DataFrame(rows)
        df = df.sort_values("date").reset_index(drop=True)
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    # ─── 지표 계산 ─────────────────────────────────────────

    def _calculate_indicators(self, df: pd.DataFrame) -> dict[str, float]:
        """기술적 지표 계산."""
        if HAS_PANDAS_TA:
            indicators = self._calc_with_pandas_ta(df)
        else:
            indicators = self._calc_builtin(df)

        # 거래량 비율
        if len(df) >= 20:
            avg_vol = df["volume"].rolling(20).mean().iloc[-1]
            cur_vol = df["volume"].iloc[-1]
            indicators["volume_ratio"] = float(cur_vol / avg_vol) if avg_vol > 0 else 1.0

        # 양봉/음봉 판단용 가격
        indicators["open_price"] = float(df["open"].iloc[-1])
        indicators["close_price"] = float(df["close"].iloc[-1])

        return indicators

    def _calc_with_pandas_ta(self, df: pd.DataFrame) -> dict[str, float]:
        """pandas_ta 기반 지표 계산."""
        indicators: dict[str, float] = {}
        close = df["close"]

        def safe_last(series) -> float:
            if series is None or len(series) == 0:
                return 0.0
            val = series.iloc[-1]
            return float(val) if not pd.isna(val) else 0.0

        # EMA (7, 25, 99)
        ema7 = ta.ema(close, length=7)
        ema25 = ta.ema(close, length=25)
        ema99 = ta.ema(close, length=99)
        indicators["ema7"] = safe_last(ema7)
        indicators["ema25"] = safe_last(ema25)
        indicators["ema99"] = safe_last(ema99)

        # EMA 추세 판단
        e7, e25, e99 = indicators["ema7"], indicators["ema25"], indicators["ema99"]
        if e7 and e25 and e99:
            if e7 > e25 > e99:
                indicators["ema_trend"] = 1.0  # bullish
            elif e7 < e25 < e99:
                indicators["ema_trend"] = -1.0  # bearish
            else:
                indicators["ema_trend"] = 0.0  # neutral
        else:
            indicators["ema_trend"] = 0.0

        # RSI (14)
        rsi = ta.rsi(close, length=14)
        indicators["rsi"] = safe_last(rsi) or 50.0

        # MACD (12, 26, 9)
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)
        if macd_df is not None and len(macd_df) > 0:
            cols = macd_df.columns.tolist()
            indicators["macd"] = float(macd_df[cols[0]].iloc[-1]) if not pd.isna(macd_df[cols[0]].iloc[-1]) else 0.0
            indicators["macd_signal"] = float(macd_df[cols[2]].iloc[-1]) if not pd.isna(macd_df[cols[2]].iloc[-1]) else 0.0
            indicators["macd_hist"] = float(macd_df[cols[1]].iloc[-1]) if not pd.isna(macd_df[cols[1]].iloc[-1]) else 0.0

        # 볼린저 밴드 (20, 2)
        bbands = ta.bbands(close, length=20, std=2)
        if bbands is not None and len(bbands) > 0:
            cols = bbands.columns.tolist()
            bb_lower = float(bbands[cols[0]].iloc[-1]) if not pd.isna(bbands[cols[0]].iloc[-1]) else 0.0
            bb_mid = float(bbands[cols[1]].iloc[-1]) if not pd.isna(bbands[cols[1]].iloc[-1]) else 0.0
            bb_upper = float(bbands[cols[2]].iloc[-1]) if not pd.isna(bbands[cols[2]].iloc[-1]) else 0.0
            indicators["bb_lower"] = bb_lower
            indicators["bb_mid"] = bb_mid
            indicators["bb_upper"] = bb_upper
            if bb_upper != bb_lower and bb_upper > 0:
                indicators["bb_position"] = (close.iloc[-1] - bb_lower) / (bb_upper - bb_lower)
            else:
                indicators["bb_position"] = 0.5

        # ATR (14) - 크립토 변동성 측정
        atr = ta.atr(df["high"], df["low"], close, length=14)
        indicators["atr"] = safe_last(atr)

        return indicators

    def _calc_builtin(self, df: pd.DataFrame) -> dict[str, float]:
        """내장 폴백 지표 계산."""
        indicators: dict[str, float] = {}
        close = df["close"]

        # EMA
        for period, key in [(7, "ema7"), (25, "ema25"), (99, "ema99")]:
            if len(close) >= period:
                indicators[key] = float(close.ewm(span=period, adjust=False).mean().iloc[-1])

        e7 = indicators.get("ema7", 0)
        e25 = indicators.get("ema25", 0)
        e99 = indicators.get("ema99", 0)
        if e7 and e25 and e99:
            indicators["ema_trend"] = 1.0 if e7 > e25 > e99 else (-1.0 if e7 < e25 < e99 else 0.0)
        else:
            indicators["ema_trend"] = 0.0

        # RSI
        if len(close) >= 15:
            delta = close.diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            val = rsi.iloc[-1]
            indicators["rsi"] = float(val) if not pd.isna(val) else 50.0

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
            bb_upper = float((sma20 + 2 * std20).iloc[-1])
            bb_lower = float((sma20 - 2 * std20).iloc[-1])
            indicators["bb_upper"] = bb_upper
            indicators["bb_lower"] = bb_lower
            indicators["bb_mid"] = float(sma20.iloc[-1])
            bb_range = bb_upper - bb_lower
            if bb_range > 0:
                indicators["bb_position"] = (float(close.iloc[-1]) - bb_lower) / bb_range
            else:
                indicators["bb_position"] = 0.5

        # ATR (True Range 기반)
        if len(df) >= 14:
            high = df["high"]
            low = df["low"]
            tr = pd.concat([
                high - low,
                (high - close.shift(1)).abs(),
                (low - close.shift(1)).abs(),
            ], axis=1).max(axis=1)
            atr_val = tr.rolling(14).mean().iloc[-1]
            indicators["atr"] = float(atr_val) if not pd.isna(atr_val) else 0.0

        return indicators

    # ─── 3가지 알려진 투자기법 ────────────────────────────

    def _strategy_rsi_oversold(self, indicators: dict) -> tuple[str, float]:
        """① RSI 과매도 반등 전략.

        RSI < 33 + BB 하단 근접 → BUY
        RSI < 25 + BB 최하단 → STRONG_BUY
        RSI > 68 → SELL / RSI > 75 → STRONG_SELL
        """
        rsi = indicators.get("rsi", 50)
        bb_pos = indicators.get("bb_position", 0.5)

        if rsi < 25 and bb_pos < 0.15:
            return SIGNAL_STRONG_BUY, 0.9
        elif rsi < 33 and bb_pos < 0.3:
            conf = 0.5 + (33 - rsi) / 33 * 0.25 + max(0, 0.3 - bb_pos) / 0.3 * 0.15
            return SIGNAL_BUY, min(round(conf, 3), 0.85)
        elif rsi > 75:
            return SIGNAL_STRONG_SELL, 0.85
        elif rsi > 68:
            conf = 0.5 + (rsi - 68) / 32 * 0.3
            return SIGNAL_SELL, min(round(conf, 3), 0.85)
        return SIGNAL_HOLD, 0.0

    def _strategy_macd_cross(self, indicators: dict) -> tuple[str, float]:
        """② MACD 골든크로스/데드크로스 전략.

        MACD선 > 시그널선 (히스토그램 양수) → BUY
        MACD선 < 시그널선 (히스토그램 음수) → SELL
        """
        macd = indicators.get("macd", 0)
        macd_sig = indicators.get("macd_signal", 0)
        macd_hist = indicators.get("macd_hist", 0)

        if not macd_hist:
            return SIGNAL_HOLD, 0.0

        denominator = abs(macd) + 1e-10
        strength = min(abs(macd_hist) / denominator, 1.0)

        if macd > macd_sig and macd_hist > 0:
            conf = 0.5 + strength * 0.35
            return SIGNAL_BUY, round(min(conf, 0.85), 3)
        elif macd < macd_sig and macd_hist < 0:
            conf = 0.5 + strength * 0.35
            return SIGNAL_SELL, round(min(conf, 0.85), 3)
        return SIGNAL_HOLD, 0.0

    def _strategy_bb_bounce(self, indicators: dict) -> tuple[str, float]:
        """③ 볼린저 밴드 하단 반등 전략.

        bb_position < 0.1 + 양봉 → BUY
        bb_position > 0.9 → SELL
        """
        bb_pos = indicators.get("bb_position", 0.5)
        open_price = indicators.get("open_price", 0)
        close_price = indicators.get("close_price", 0)

        is_bullish = close_price >= open_price if open_price > 0 else True

        if bb_pos < 0.05:
            conf = 0.75 + (0.05 - bb_pos) / 0.05 * 0.1 if is_bullish else 0.6
            return SIGNAL_STRONG_BUY if is_bullish else SIGNAL_BUY, round(min(conf, 0.85), 3)
        elif bb_pos < 0.1:
            conf = (0.6 + (0.1 - bb_pos) / 0.1 * 0.2) if is_bullish else 0.5
            return SIGNAL_BUY, round(min(conf, 0.8), 3)
        elif bb_pos > 0.9:
            conf = 0.55 + (bb_pos - 0.9) / 0.1 * 0.25
            return SIGNAL_SELL, round(min(conf, 0.8), 3)
        return SIGNAL_HOLD, 0.0

    # ─── 신호 판단 (다중 전략 투표) ────────────────────────

    def _evaluate_signal(
        self, indicators: dict[str, float]
    ) -> tuple[str, float]:
        """3가지 전략 다수결 + 가중 평균 → 최종 매매 신호."""

        _signal_to_score = {
            SIGNAL_STRONG_BUY: 2,
            SIGNAL_BUY: 1,
            SIGNAL_HOLD: 0,
            SIGNAL_SELL: -1,
            SIGNAL_STRONG_SELL: -2,
        }

        strategies = [
            self._strategy_rsi_oversold(indicators),
            self._strategy_macd_cross(indicators),
            self._strategy_bb_bounce(indicators),
        ]

        # EMA 추세를 보조 필터로 사용 (배열 방향)
        ema_trend = indicators.get("ema_trend", 0.0)

        weighted_score = 0.0
        total_weight = 0.0
        buy_votes = 0
        sell_votes = 0

        for sig, conf in strategies:
            score = _signal_to_score.get(sig, 0)
            if score != 0:
                weight = conf
                weighted_score += score * weight
                total_weight += weight
                if score > 0:
                    buy_votes += 1
                elif score < 0:
                    sell_votes += 1

        # EMA 추세 반영 (약한 보조 신호, 가중치 0.3)
        if ema_trend != 0.0:
            weighted_score += ema_trend * 0.3
            total_weight += 0.3

        # 거래량 증폭 (방향 일치 시만 가중)
        vol_ratio = indicators.get("volume_ratio", 1.0)
        if vol_ratio > 1.5 and total_weight > 0:
            direction = 1 if weighted_score > 0 else -1
            weighted_score += direction * min((vol_ratio - 1.5) * 0.1, 0.3)

        if total_weight == 0:
            return SIGNAL_HOLD, 0.0

        normalized = weighted_score / (total_weight * 2.0 + 1e-10)
        # 다수결 일치 여부로 confidence 조정
        agreeing = max(buy_votes, sell_votes)
        agreement_bonus = (agreeing - 1) * 0.05  # 2개 일치 +0.05, 3개 일치 +0.10

        confidence = min(abs(normalized) + agreement_bonus, 1.0)

        if normalized > 0.45:
            signal = SIGNAL_STRONG_BUY
        elif normalized > 0.15:
            signal = SIGNAL_BUY
        elif normalized < -0.45:
            signal = SIGNAL_STRONG_SELL
        elif normalized < -0.15:
            signal = SIGNAL_SELL
        else:
            signal = SIGNAL_HOLD

        return signal, round(confidence, 3)

    # ─── 목표가 / 손절가 ───────────────────────────────────

    def _calc_price_targets(
        self,
        current_price: float,
        signal: str,
        indicators: dict[str, float],
    ) -> tuple[float, float]:
        """스탑로스 및 익절 목표가 계산.

        Returns:
            (stop_loss_price, take_profit_price)
        """
        stop_loss_pct = self.config.get("stop_loss_pct", 8.0) / 100
        take_profit_pct = self.config.get("take_profit_pct", 15.0) / 100

        # ATR 기반 동적 스탑로스 (ATR이 있으면 활용)
        atr = indicators.get("atr", 0)
        if atr > 0 and current_price > 0:
            atr_ratio = atr / current_price
            # ATR이 기본 스탑로스보다 크면 ATR 기준 사용 (최대 15%)
            dynamic_stop = min(atr_ratio * 2, 0.15)
            stop_loss_pct = max(stop_loss_pct, dynamic_stop)

        stop_loss = round(current_price * (1 - stop_loss_pct), 8)
        take_profit = round(current_price * (1 + take_profit_pct), 8)
        return stop_loss, take_profit

    # ─── DB 저장 ───────────────────────────────────────────

    async def save_signal(self, signal_data: dict) -> None:
        """신호 DB 저장."""
        try:
            db = await get_db()
            try:
                await db.execute(
                    """INSERT INTO crypto_signals
                       (market, signal, confidence, rsi, macd, bb_position, atr,
                        ema_trend, fear_greed_index, news_score, total_score)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        signal_data.get("market", ""),
                        signal_data.get("signal", SIGNAL_HOLD),
                        signal_data.get("confidence", 0.0),
                        signal_data.get("rsi"),
                        signal_data.get("macd"),
                        signal_data.get("bb_position"),
                        signal_data.get("atr"),
                        signal_data.get("ema_trend", "neutral"),
                        signal_data.get("fear_greed_index"),
                        signal_data.get("news_score", 0.0),
                        signal_data.get("total_score", 0.0),
                    ),
                )
                await db.commit()
            finally:
                await db.close()
        except Exception as e:
            logger.error(f"신호 DB 저장 오류: {e}")

    # ─── 헬퍼 ─────────────────────────────────────────────

    def _make_hold_signal(self, market: str) -> dict:
        return {
            "market": market,
            "signal": SIGNAL_HOLD,
            "confidence": 0.0,
            "rsi": 50.0,
            "macd": 0.0,
            "macd_hist": 0.0,
            "bb_position": 0.5,
            "atr": 0.0,
            "ema_trend": "neutral",
            "current_price": 0.0,
            "entry_price": 0.0,
            "stop_loss_price": 0.0,
            "take_profit_price": 0.0,
            "analyzed_at": datetime.now().isoformat(),
        }
