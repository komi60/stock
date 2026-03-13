"""모듈 7: AI 가격 예측 모듈.

XGBoost(단기 분류) + LSTM(추세 예측) 앙상블로
종목의 향후 수익률을 예측하여 매매 신호에 반영.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

import numpy as np
import pandas as pd
from loguru import logger

from broker.kis_api import KISClient
from core.base_module import DataProviderModule
from core.data_models import SignalStrength, StockCandidate
from core.events import Event, EventBus, EventTypes

try:
    from xgboost import XGBClassifier
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    logger.warning("xgboost 미설치. XGBoost 예측 비활성화.")

try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False
    logger.warning("pytorch 미설치. LSTM 예측 비활성화.")

try:
    import pandas_ta as ta
    HAS_PANDAS_TA = True
except ImportError:
    HAS_PANDAS_TA = False


class LSTMPredictor(nn.Module if HAS_TORCH else object):
    """LSTM 기반 시계열 가격 추세 예측 모델."""

    def __init__(self, input_size: int = 15, hidden_size: int = 64,
                 num_layers: int = 2, dropout: float = 0.2):
        if not HAS_TORCH:
            return
        super().__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0,
        )
        self.fc = nn.Sequential(
            nn.Linear(hidden_size, 32),
            nn.ReLU(),
            nn.Dropout(dropout),
            nn.Linear(32, 3),  # 상승/보합/하락
        )

    def forward(self, x):
        lstm_out, _ = self.lstm(x)
        last_hidden = lstm_out[:, -1, :]
        return self.fc(last_hidden)


class PredictionModule(DataProviderModule):
    """AI 가격 예측 모듈.

    기술적 지표 + 가격 패턴을 학습하여 향후 수익률을 예측.
    XGBoost (단기 분류) + LSTM (추세 방향) 앙상블.
    """

    # 피처 컬럼 정의
    FEATURE_COLS = [
        "returns_1d", "returns_3d", "returns_5d",
        "volatility_5d", "volatility_20d",
        "rsi", "macd_hist", "bb_position",
        "sma5_ratio", "sma20_ratio", "sma60_ratio",
        "volume_ratio", "stoch_k", "stoch_d",
        "atr_ratio",
    ]

    def __init__(self, config: dict[str, Any], kis_client: KISClient):
        super().__init__("prediction", config)
        self._kis = kis_client
        self._event_bus = EventBus()

        # 모델
        self._xgb_model: Optional[XGBClassifier] = None
        self._lstm_model: Optional[LSTMPredictor] = None

        # 설정
        self._lookback = config.get("lookback_days", 120)
        self._sequence_len = config.get("sequence_length", 20)
        self._min_train_samples = config.get("min_train_samples", 60)
        self._prediction_horizon = config.get("prediction_horizon", 3)  # 3일 후 수익률 예측
        self._xgb_weight = config.get("xgb_weight", 0.5)
        self._lstm_weight = config.get("lstm_weight", 0.5)

        # 캐시
        self._predictions: dict[str, dict] = {}
        self._candidates: list[StockCandidate] = []

    async def initialize(self) -> None:
        """모델 초기화."""
        if HAS_XGBOOST:
            self._xgb_model = XGBClassifier(
                n_estimators=100,
                max_depth=5,
                learning_rate=0.1,
                objective="multi:softprob",
                num_class=3,  # 상승/보합/하락
                eval_metric="mlogloss",
                use_label_encoder=False,
                random_state=42,
                n_jobs=-1,
            )
            logger.info("XGBoost 모델 초기화 완료")

        if HAS_TORCH:
            self._lstm_model = LSTMPredictor(
                input_size=len(self.FEATURE_COLS),
                hidden_size=64,
                num_layers=2,
            )
            self._lstm_model.eval()
            logger.info("LSTM 모델 초기화 완료")

        logger.info("AI 예측 모듈 초기화 완료")

    async def execute(self) -> list[StockCandidate]:
        """등록된 후보 종목에 대해 AI 예측 실행."""
        return self._candidates

    async def shutdown(self) -> None:
        self._predictions.clear()
        self._candidates.clear()
        logger.info("AI 예측 모듈 종료")

    # ─── DataProviderModule 인터페이스 ───────────────────

    async def get_candidates(self) -> list[StockCandidate]:
        return self._candidates

    async def get_score(self, ticker: str) -> float:
        pred = self._predictions.get(ticker)
        if pred:
            return pred.get("prediction_score", 0.0)
        return 0.0

    # ─── 핵심 예측 로직 ──────────────────────────────────

    async def predict(self, ticker: str) -> dict:
        """단일 종목 AI 예측 수행.

        Returns:
            {
                "ticker": str,
                "prediction_score": float (0~100),
                "direction": "up" | "neutral" | "down",
                "confidence": float (0~1),
                "xgb_probs": [down_prob, neutral_prob, up_prob],
                "lstm_probs": [down_prob, neutral_prob, up_prob],
                "ensemble_probs": [down_prob, neutral_prob, up_prob],
            }
        """
        try:
            # 1. 차트 데이터 조회
            candles = await self._kis.get_daily_chart(
                ticker, period="D", count=self._lookback
            )
            if len(candles) < self._min_train_samples:
                logger.warning(f"[{ticker}] 데이터 부족: {len(candles)}봉 < {self._min_train_samples}")
                return self._default_prediction(ticker)

            df = self._build_feature_df(candles)
            if df is None or len(df) < self._min_train_samples:
                return self._default_prediction(ticker)

            # 2. 레이블 생성 (학습용)
            df = self._create_labels(df)

            # 3. 학습/예측 분리
            train_df = df.iloc[:-1].dropna()
            latest_features = df[self.FEATURE_COLS].iloc[-1:].values

            if len(train_df) < 30:
                return self._default_prediction(ticker)

            X_train = train_df[self.FEATURE_COLS].values
            y_train = train_df["label"].values.astype(int)

            # 4. XGBoost 예측
            xgb_probs = await self._predict_xgboost(X_train, y_train, latest_features)

            # 5. LSTM 예측
            lstm_probs = await self._predict_lstm(df, latest_features)

            # 6. 앙상블
            ensemble_probs = self._ensemble_predictions(xgb_probs, lstm_probs)

            # 7. 결과 정리
            direction_idx = int(np.argmax(ensemble_probs))
            directions = ["down", "neutral", "up"]
            direction = directions[direction_idx]
            confidence = float(ensemble_probs[direction_idx])

            # prediction_score: 상승 확률 기반 0~100 스코어
            # up_prob에 가중치를 더해서 score 계산
            up_prob = ensemble_probs[2]
            down_prob = ensemble_probs[0]
            prediction_score = max(0.0, min(100.0, (up_prob - down_prob + 1) * 50))

            result = {
                "ticker": ticker,
                "prediction_score": round(prediction_score, 2),
                "direction": direction,
                "confidence": round(confidence, 3),
                "xgb_probs": [round(p, 4) for p in xgb_probs],
                "lstm_probs": [round(p, 4) for p in lstm_probs],
                "ensemble_probs": [round(p, 4) for p in ensemble_probs],
                "predicted_at": datetime.now(),
            }

            self._predictions[ticker] = result

            await self._event_bus.publish(Event(
                event_type=EventTypes.PREDICTION_GENERATED,
                data=result,
                source=self.name,
            ))

            logger.info(
                f"[{ticker}] AI 예측: {direction} "
                f"(score={prediction_score:.1f}, conf={confidence:.3f})"
            )
            return result

        except Exception as e:
            logger.error(f"[{ticker}] AI 예측 오류: {e}")
            return self._default_prediction(ticker)

    async def predict_batch(self, tickers: list[str]) -> list[dict]:
        """여러 종목 병렬 예측."""
        tasks = [self.predict(ticker) for ticker in tickers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        predictions = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"[{tickers[i]}] 예측 실패: {result}")
                predictions.append(self._default_prediction(tickers[i]))
            else:
                predictions.append(result)

        # 후보 종목 생성
        self._candidates = []
        for pred in predictions:
            if pred["prediction_score"] >= 55:  # 55점 이상만 후보
                self._candidates.append(StockCandidate(
                    ticker=pred["ticker"],
                    name=pred["ticker"],
                    prediction_score=pred["prediction_score"],
                    reasons=[
                        f"AI 예측: {pred['direction']} (신뢰도 {pred['confidence']:.1%})",
                        f"상승확률: {pred['ensemble_probs'][2]:.1%}",
                    ],
                ))

        return predictions

    # ─── 피처 엔지니어링 ─────────────────────────────────

    def _build_feature_df(self, candles: list[dict]) -> Optional[pd.DataFrame]:
        """캔들 데이터에서 ML 피처 DataFrame 생성."""
        try:
            df = pd.DataFrame(candles)
            df = df.sort_values("date").reset_index(drop=True)
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            close = df["close"]
            high = df["high"]
            low = df["low"]

            # 수익률 피처
            df["returns_1d"] = close.pct_change(1)
            df["returns_3d"] = close.pct_change(3)
            df["returns_5d"] = close.pct_change(5)

            # 변동성
            df["volatility_5d"] = close.pct_change().rolling(5).std()
            df["volatility_20d"] = close.pct_change().rolling(20).std()

            # 기술적 지표
            if HAS_PANDAS_TA:
                rsi = ta.rsi(close, length=14)
                df["rsi"] = rsi if rsi is not None else 50.0

                macd_df = ta.macd(close, fast=12, slow=26, signal=9)
                if macd_df is not None:
                    df["macd_hist"] = macd_df.iloc[:, 2]
                else:
                    df["macd_hist"] = 0.0

                bbands = ta.bbands(close, length=20, std=2)
                if bbands is not None and bbands.shape[1] >= 3:
                    bb_upper = bbands.iloc[:, 0]
                    bb_lower = bbands.iloc[:, 2]
                    bb_range = bb_upper - bb_lower
                    df["bb_position"] = np.where(
                        bb_range > 0, (close - bb_lower) / bb_range, 0.5
                    )
                else:
                    df["bb_position"] = 0.5

                stoch = ta.stoch(high, low, close)
                if stoch is not None:
                    df["stoch_k"] = stoch.iloc[:, 0]
                    df["stoch_d"] = stoch.iloc[:, 1]
                else:
                    df["stoch_k"] = 50.0
                    df["stoch_d"] = 50.0

                atr = ta.atr(high, low, close, length=14)
                df["atr_ratio"] = (atr / close) if atr is not None else 0.0
            else:
                df["rsi"] = self._calc_rsi(close)
                df["macd_hist"] = self._calc_macd_hist(close)
                df["bb_position"] = self._calc_bb_position(close)
                df["stoch_k"] = 50.0
                df["stoch_d"] = 50.0
                df["atr_ratio"] = self._calc_atr_ratio(high, low, close)

            # 이동평균 대비 비율
            sma5 = close.rolling(5).mean()
            sma20 = close.rolling(20).mean()
            sma60 = close.rolling(60).mean()
            df["sma5_ratio"] = (close / sma5 - 1) * 100
            df["sma20_ratio"] = (close / sma20 - 1) * 100
            df["sma60_ratio"] = np.where(sma60 > 0, (close / sma60 - 1) * 100, 0.0)

            # 거래량 비율
            avg_vol = df["volume"].rolling(20).mean()
            df["volume_ratio"] = np.where(avg_vol > 0, df["volume"] / avg_vol, 1.0)

            # NaN 처리
            df[self.FEATURE_COLS] = df[self.FEATURE_COLS].fillna(0.0)

            # 무한값 처리
            df[self.FEATURE_COLS] = df[self.FEATURE_COLS].replace(
                [np.inf, -np.inf], 0.0
            )

            return df

        except Exception as e:
            logger.error(f"피처 생성 오류: {e}")
            return None

    def _create_labels(self, df: pd.DataFrame) -> pd.DataFrame:
        """향후 N일 수익률 기반 레이블 생성.

        0=하락(-1% 이하), 1=보합(-1%~+1%), 2=상승(+1% 이상)
        """
        future_returns = df["close"].pct_change(self._prediction_horizon).shift(
            -self._prediction_horizon
        )
        df["future_returns"] = future_returns
        df["label"] = np.where(
            future_returns > 0.01, 2,      # 상승
            np.where(future_returns < -0.01, 0, 1)  # 하락 / 보합
        )
        return df

    # ─── XGBoost 예측 ────────────────────────────────────

    async def _predict_xgboost(
        self, X_train: np.ndarray, y_train: np.ndarray, X_latest: np.ndarray
    ) -> list[float]:
        """XGBoost 학습 및 예측."""
        if not HAS_XGBOOST or self._xgb_model is None:
            return [1 / 3, 1 / 3, 1 / 3]

        try:
            # 비동기 래핑 (CPU 바운드)
            loop = asyncio.get_event_loop()
            probs = await loop.run_in_executor(
                None, self._xgb_train_predict, X_train, y_train, X_latest
            )
            return probs
        except Exception as e:
            logger.error(f"XGBoost 예측 오류: {e}")
            return [1 / 3, 1 / 3, 1 / 3]

    def _xgb_train_predict(
        self, X_train: np.ndarray, y_train: np.ndarray, X_latest: np.ndarray
    ) -> list[float]:
        """XGBoost 학습 + 예측 (동기)."""
        # 클래스 분포 확인 - 최소 2개 클래스 필요
        unique_classes = np.unique(y_train)
        if len(unique_classes) < 2:
            return [1 / 3, 1 / 3, 1 / 3]

        model = XGBClassifier(
            n_estimators=100,
            max_depth=5,
            learning_rate=0.1,
            objective="multi:softprob",
            num_class=3,
            eval_metric="mlogloss",
            use_label_encoder=False,
            random_state=42,
            n_jobs=-1,
        )
        model.fit(X_train, y_train, verbose=False)
        probs = model.predict_proba(X_latest)[0]

        # 3개 클래스 보장
        full_probs = [0.0, 0.0, 0.0]
        for i, cls in enumerate(model.classes_):
            full_probs[int(cls)] = float(probs[i])

        # 정규화
        total = sum(full_probs)
        if total > 0:
            full_probs = [p / total for p in full_probs]

        return full_probs

    # ─── LSTM 예측 ───────────────────────────────────────

    async def _predict_lstm(
        self, df: pd.DataFrame, latest_features: np.ndarray
    ) -> list[float]:
        """LSTM 추세 예측."""
        if not HAS_TORCH or self._lstm_model is None:
            return [1 / 3, 1 / 3, 1 / 3]

        try:
            # 시퀀스 데이터 준비
            feature_data = df[self.FEATURE_COLS].values
            if len(feature_data) < self._sequence_len:
                return [1 / 3, 1 / 3, 1 / 3]

            # 마지막 sequence_len 일 피처
            sequence = feature_data[-self._sequence_len:]

            # 정규화 (Z-score)
            mean = np.nanmean(feature_data, axis=0)
            std = np.nanstd(feature_data, axis=0)
            std[std == 0] = 1  # 0으로 나누기 방지
            sequence = (sequence - mean) / std

            # NaN/Inf 처리
            sequence = np.nan_to_num(sequence, nan=0.0, posinf=0.0, neginf=0.0)

            loop = asyncio.get_event_loop()
            probs = await loop.run_in_executor(
                None, self._lstm_inference, sequence
            )
            return probs

        except Exception as e:
            logger.error(f"LSTM 예측 오류: {e}")
            return [1 / 3, 1 / 3, 1 / 3]

    def _lstm_inference(self, sequence: np.ndarray) -> list[float]:
        """LSTM 추론 (동기)."""
        with torch.no_grad():
            x = torch.FloatTensor(sequence).unsqueeze(0)  # (1, seq_len, features)
            logits = self._lstm_model(x)
            probs = torch.softmax(logits, dim=-1).squeeze().numpy()
        return [float(p) for p in probs]

    # ─── 앙상블 ──────────────────────────────────────────

    def _ensemble_predictions(
        self, xgb_probs: list[float], lstm_probs: list[float]
    ) -> list[float]:
        """XGBoost + LSTM 앙상블.

        둘 다 사용 가능하면 가중 평균, 하나만 사용 가능하면 단독 사용.
        """
        has_xgb = HAS_XGBOOST and self._xgb_model is not None
        has_lstm = HAS_TORCH and self._lstm_model is not None

        if has_xgb and has_lstm:
            probs = [
                self._xgb_weight * xgb_probs[i] + self._lstm_weight * lstm_probs[i]
                for i in range(3)
            ]
        elif has_xgb:
            probs = xgb_probs
        elif has_lstm:
            probs = lstm_probs
        else:
            probs = [1 / 3, 1 / 3, 1 / 3]

        # 정규화
        total = sum(probs)
        if total > 0:
            probs = [p / total for p in probs]

        return probs

    # ─── 유틸리티 ────────────────────────────────────────

    def _default_prediction(self, ticker: str) -> dict:
        """기본 (중립) 예측값."""
        return {
            "ticker": ticker,
            "prediction_score": 50.0,
            "direction": "neutral",
            "confidence": 0.0,
            "xgb_probs": [1 / 3, 1 / 3, 1 / 3],
            "lstm_probs": [1 / 3, 1 / 3, 1 / 3],
            "ensemble_probs": [1 / 3, 1 / 3, 1 / 3],
            "predicted_at": datetime.now(),
        }

    # ─── 폴백 지표 계산 ──────────────────────────────────

    @staticmethod
    def _calc_rsi(close: pd.Series, period: int = 14) -> pd.Series:
        delta = close.diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    @staticmethod
    def _calc_macd_hist(close: pd.Series) -> pd.Series:
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema12 - ema26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        return macd_line - signal_line

    @staticmethod
    def _calc_bb_position(close: pd.Series, period: int = 20) -> pd.Series:
        sma = close.rolling(period).mean()
        std = close.rolling(period).std()
        upper = sma + 2 * std
        lower = sma - 2 * std
        bb_range = upper - lower
        return np.where(bb_range > 0, (close - lower) / bb_range, 0.5)

    @staticmethod
    def _calc_atr_ratio(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> pd.Series:
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        return atr / close
