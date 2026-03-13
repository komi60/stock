"""데모 실행 스크립트.

API 키 없이 전체 매매 파이프라인을 시뮬레이션하여
각 모듈의 동작을 눈으로 확인할 수 있습니다.

실행: python run_demo.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
import pandas as pd
from loguru import logger

logger.remove()
logger.add(
    sys.stderr,
    level="INFO",
    format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>",
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  모의 데이터 생성
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def generate_mock_candles(ticker: str, base_price: int, days: int = 120) -> list[dict]:
    """모의 주가 캔들 데이터 생성."""
    np.random.seed(hash(ticker) % 2**31)
    dates = pd.date_range(end=pd.Timestamp.now(), periods=days, freq="B")
    prices = base_price + np.cumsum(np.random.randn(days) * (base_price * 0.015))
    prices = np.maximum(prices, base_price * 0.5)  # 최소가 보장

    candles = []
    for i, date in enumerate(dates):
        p = prices[i]
        candles.append({
            "date": date.strftime("%Y%m%d"),
            "open": int(p * (1 + np.random.uniform(-0.01, 0.01))),
            "high": int(p * (1 + np.random.uniform(0.005, 0.03))),
            "low": int(p * (1 - np.random.uniform(0.005, 0.03))),
            "close": int(p),
            "volume": int(np.random.randint(100000, 2000000)),
        })
    return candles


MOCK_STOCKS = {
    "005930": ("삼성전자", 72000),
    "000660": ("SK하이닉스", 185000),
    "035420": ("NAVER", 210000),
    "006400": ("삼성SDI", 380000),
    "035720": ("카카오", 42000),
    "051910": ("LG화학", 340000),
    "005380": ("현대차", 230000),
    "068270": ("셀트리온", 175000),
    "003670": ("포스코퓨처엠", 260000),
    "105560": ("KB금융", 78000),
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  데모 실행
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


async def run_demo():
    logger.info("=" * 65)
    logger.info("  KR Stock AutoTrader - 데모 시뮬레이션")
    logger.info("  (API 키 없이 전체 파이프라인 시연)")
    logger.info("=" * 65)

    # ── 1단계: Core 초기화 ──────────────────────────────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  📦 [1단계] 시스템 초기화")
    logger.info("━" * 65)

    from core.database import init_db
    await init_db()

    from core.base_module import PluginRegistry
    from core.events import EventBus, Event, EventTypes
    EventBus.reset()
    bus = EventBus()

    logger.success("Core 시스템 초기화 완료")
    await asyncio.sleep(0.5)

    # ── 2단계: 장전 뉴스/센티먼트/정책 시뮬레이션 ───────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  📰 [2단계] 장전 분석 (08:00 KST 시뮬레이션)")
    logger.info("━" * 65)
    await asyncio.sleep(0.5)

    from core.data_models import StockCandidate, SignalStrength

    # 모의 뉴스 분석 결과
    mock_news_candidates = [
        StockCandidate(
            ticker="005930", name="삼성전자",
            news_score=82.5,
            reasons=["[뉴스] 삼성전자 HBM4 대량생산 돌입, 엔비디아 공급 확정"],
        ),
        StockCandidate(
            ticker="000660", name="SK하이닉스",
            news_score=75.3,
            reasons=["[뉴스] SK하이닉스 AI 메모리 매출 전년비 180% 증가"],
        ),
        StockCandidate(
            ticker="035420", name="NAVER",
            news_score=63.0,
            reasons=["[뉴스] 네이버 클라우드 AI 플랫폼 일본 진출 가속"],
        ),
        StockCandidate(
            ticker="051910", name="LG화학",
            news_score=55.2,
            reasons=["[뉴스] LG화학 2차전지 분리막 신기술 특허 획득"],
        ),
    ]

    logger.info("📰 뉴스 분석 결과:")
    for c in mock_news_candidates:
        logger.info(f"  {c.ticker} ({c.name}): 뉴스 점수 {c.news_score:.1f} | {c.reasons[0]}")
    await asyncio.sleep(1)

    # 모의 센티먼트 결과
    sentiment_scores = {
        "005930": 70.0, "000660": 65.0, "035420": 55.0, "051910": 40.0,
    }
    logger.info("")
    logger.info("💬 커뮤니티 센티먼트 분석:")
    for ticker, score in sentiment_scores.items():
        name = MOCK_STOCKS[ticker][0]
        logger.info(f"  {ticker} ({name}): 센티먼트 점수 {score:.1f}")
    await asyncio.sleep(0.5)

    # 모의 정책 결과
    policy_scores = {
        "005930": 85.0, "000660": 80.0, "035420": 30.0, "051910": 70.0,
    }
    logger.info("")
    logger.info("🏛️ 정책 분석 결과:")
    logger.info("  [정책] 반도체 국가전략기술 세액공제 확대 → 삼성전자/SK하이닉스 수혜")
    logger.info("  [정책] 2차전지 핵심광물 비축 예산 증액 → LG화학 수혜")
    await asyncio.sleep(0.5)

    # ── 3단계: AI 예측 모듈 시뮬레이션 ──────────────────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  🤖 [3단계] AI 가격 예측 (XGBoost + LSTM)")
    logger.info("━" * 65)
    await asyncio.sleep(0.5)

    from modules.prediction.prediction_module import PredictionModule
    from modules.trading.trading_engine import TradingEngine

    # TradingEngine으로 기술적 분석도 함께 수행
    engine = TradingEngine({}, None)

    prediction_results = {}
    for ticker in ["005930", "000660", "035420", "051910"]:
        name, base_price = MOCK_STOCKS[ticker]
        candles = generate_mock_candles(ticker, base_price)

        # 기술적 분석
        df = engine._candles_to_df(candles)
        indicators = engine._calculate_indicators(df)
        signal, confidence = engine._evaluate_signal(indicators, df)

        # AI 예측 시뮬레이션 (실제 XGBoost 학습/예측)
        pred_module = PredictionModule({}, None)
        await pred_module.initialize()

        feature_df = pred_module._build_feature_df(candles)
        if feature_df is not None and len(feature_df) >= 60:
            feature_df = pred_module._create_labels(feature_df)
            train_df = feature_df.iloc[:-1].dropna()
            if len(train_df) >= 30:
                X_train = train_df[pred_module.FEATURE_COLS].values
                y_train = train_df["label"].values.astype(int)
                latest = feature_df[pred_module.FEATURE_COLS].iloc[-1:].values
                xgb_probs = await pred_module._predict_xgboost(X_train, y_train, latest)
            else:
                xgb_probs = [1/3, 1/3, 1/3]
        else:
            xgb_probs = [1/3, 1/3, 1/3]

        direction_idx = int(np.argmax(xgb_probs))
        directions = ["하락📉", "보합➡️", "상승📈"]
        direction = directions[direction_idx]
        pred_confidence = xgb_probs[direction_idx]
        pred_score = max(0, min(100, (xgb_probs[2] - xgb_probs[0] + 1) * 50))

        prediction_results[ticker] = {
            "score": pred_score,
            "direction": direction,
            "confidence": pred_confidence,
            "probs": xgb_probs,
        }

        current_price = int(df["close"].iloc[-1])
        logger.info(
            f"  {ticker} ({name:8s}) | 현재가: {current_price:>8,}원 | "
            f"기술적: {signal.value:11s} (신뢰도 {confidence:.3f}) | "
            f"AI 예측: {direction} ({pred_confidence:.1%})"
        )
        logger.info(
            f"    └ XGBoost 확률: 하락={xgb_probs[0]:.1%} 보합={xgb_probs[1]:.1%} 상승={xgb_probs[2]:.1%} | "
            f"RSI={indicators.get('rsi', 0):.1f} MACD={indicators.get('macd_hist', 0):.0f}"
        )
        await asyncio.sleep(0.3)

    # ── 4단계: 종합 점수 계산 ────────────────────────────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  🎯 [4단계] 종합 점수 계산 (08:30 KST 시뮬레이션)")
    logger.info("━" * 65)
    await asyncio.sleep(0.5)

    # 가중치: 뉴스 25%, 센티먼트 20%, 정책 30%, AI예측 25%
    weights = {"news": 0.25, "sentiment": 0.20, "policy": 0.30, "prediction": 0.25}
    logger.info(f"  가중치: 뉴스={weights['news']:.0%} 센티먼트={weights['sentiment']:.0%} "
                f"정책={weights['policy']:.0%} AI예측={weights['prediction']:.0%}")
    logger.info("")

    final_candidates = []
    for c in mock_news_candidates:
        news_s = c.news_score
        sent_s = sentiment_scores.get(c.ticker, 0)
        pol_s = policy_scores.get(c.ticker, 0)
        pred_s = prediction_results.get(c.ticker, {}).get("score", 50)

        composite = (
            news_s * weights["news"]
            + sent_s * weights["sentiment"]
            + pol_s * weights["policy"]
            + pred_s * weights["prediction"]
        )
        final_candidates.append((c.ticker, c.name, composite, news_s, sent_s, pol_s, pred_s))

    final_candidates.sort(key=lambda x: x[2], reverse=True)

    logger.info(f"  {'순위':>4} | {'종목코드':<8} {'종목명':>10} | {'종합':>6} | {'뉴스':>6} {'센티':>6} {'정책':>6} {'AI예측':>6}")
    logger.info(f"  {'─'*4:>4} | {'─'*8:<8} {'─'*10:>10} | {'─'*6:>6} | {'─'*6:>6} {'─'*6:>6} {'─'*6:>6} {'─'*6:>6}")
    for i, (ticker, name, comp, n, s, p, pred) in enumerate(final_candidates, 1):
        logger.info(
            f"  {i:>4} | {ticker:<8} {name:>10} | {comp:>6.1f} | {n:>6.1f} {s:>6.1f} {p:>6.1f} {pred:>6.1f}"
        )
    await asyncio.sleep(1)

    # ── 5단계: 매매 결정 시뮬레이션 ──────────────────────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  💰 [5단계] 매매 결정 (장중 시뮬레이션)")
    logger.info("━" * 65)
    await asyncio.sleep(0.5)

    initial_cash = 100_000_000  # 1억원
    remaining_cash = initial_cash
    positions = []

    logger.info(f"  초기 투자금: {initial_cash:>14,}원")
    logger.info(f"  최대 종목 수: 10개 | 종목당 최대: 15%")
    logger.info("")

    for ticker, name, composite, *_ in final_candidates:
        if composite < 30:
            logger.info(f"  ❌ {ticker} ({name}): 종합점수 {composite:.1f} < 30 → 매수 스킵")
            continue

        pred = prediction_results.get(ticker, {})
        if pred.get("direction", "") == "하락📉":
            logger.info(f"  ❌ {ticker} ({name}): AI 하락 예측 → 매수 스킵")
            continue

        base_price = MOCK_STOCKS[ticker][1]
        max_amount = int(remaining_cash * 0.15)
        quantity = max_amount // base_price
        if quantity <= 0:
            continue

        cost = quantity * base_price
        remaining_cash -= cost
        positions.append((ticker, name, quantity, base_price, cost))

        logger.info(
            f"  ✅ 매수: {ticker} ({name}) | {quantity}주 × {base_price:,}원 = {cost:>12,}원 | "
            f"종합 {composite:.1f}점"
        )
        await asyncio.sleep(0.3)

    logger.info("")
    logger.info(f"  잔여 현금: {remaining_cash:>14,}원")
    logger.info(f"  투자 종목: {len(positions)}개")

    # ── 6단계: 포트폴리오 요약 ────────────────────────────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  📊 [6단계] 포트폴리오 요약")
    logger.info("━" * 65)
    await asyncio.sleep(0.5)

    total_invested = sum(cost for _, _, _, _, cost in positions)
    # 모의 수익률 적용
    np.random.seed(42)

    logger.info(f"  {'종목코드':<8} {'종목명':>10} | {'수량':>6} {'평균단가':>10} {'투자금액':>14} {'비중':>6}")
    logger.info(f"  {'─'*8:<8} {'─'*10:>10} | {'─'*6:>6} {'─'*10:>10} {'─'*14:>14} {'─'*6:>6}")
    for ticker, name, qty, price, cost in positions:
        pct = cost / initial_cash * 100
        logger.info(
            f"  {ticker:<8} {name:>10} | {qty:>5}주 {price:>9,}원 {cost:>13,}원 {pct:>5.1f}%"
        )
    logger.info(f"  {'─'*70}")
    logger.info(f"  총 투자금: {total_invested:>13,}원 | 잔여현금: {remaining_cash:>13,}원")
    logger.info(f"  현금 비율: {remaining_cash/initial_cash*100:.1f}%")

    # ── 7단계: 실시간 모니터링 시뮬레이션 ────────────────────
    logger.info("")
    logger.info("━" * 65)
    logger.info("  📡 [7단계] 실시간 모니터링 (5초간 시뮬레이션)")
    logger.info("━" * 65)
    await asyncio.sleep(0.5)

    for tick in range(5):
        total_value = 0
        changes = []
        for ticker, name, qty, buy_price, cost in positions:
            change_pct = np.random.uniform(-2.5, 3.0)
            cur_price = int(buy_price * (1 + change_pct / 100))
            value = qty * cur_price
            pnl = value - cost
            pnl_pct = pnl / cost * 100
            total_value += value
            emoji = "🔴" if pnl < 0 else "🟢"
            changes.append(f"  {emoji} {ticker}({name}): {cur_price:,}원 ({pnl_pct:+.2f}%)")

        total_pnl = total_value + remaining_cash - initial_cash
        total_pnl_pct = total_pnl / initial_cash * 100
        portfolio_emoji = "📈" if total_pnl >= 0 else "📉"

        logger.info(f"\n  ⏱️  Tick #{tick+1} {'─'*45}")
        for change in changes:
            logger.info(change)
        logger.info(
            f"  {portfolio_emoji} 총 자산: {total_value + remaining_cash:,}원 | "
            f"일일 수익: {total_pnl:+,}원 ({total_pnl_pct:+.2f}%)"
        )
        await asyncio.sleep(1)

    # ── 결과 요약 ─────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 65)
    logger.info("  ✅ 데모 시뮬레이션 완료!")
    logger.info("=" * 65)
    logger.info("")
    logger.info("  실제 운영 시 필요한 설정:")
    logger.info("    1. config/.env 파일에 API 키 입력")
    logger.info("       - KIS_APP_KEY, KIS_APP_SECRET (한국투자증권)")
    logger.info("       - GEMINI_API_KEY (Google AI)")
    logger.info("    2. python main.py 로 24시간 자동매매 시작")
    logger.info("    3. http://localhost:8080 대시보드에서 모니터링")
    logger.info("")
    logger.info("  또는 대시보드에서 직접 API 키 설정 가능:")
    logger.info("    http://localhost:8080 → 설정 탭 → API 키 입력")
    logger.info("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_demo())
