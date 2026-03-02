"""시스템 통합 테스트.

각 모듈을 단계별로 초기화하고 기본 동작을 검증.
API 키 없이도 구조적 검증이 가능하도록 설계.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger


async def test_step(name: str, coro):
    """테스트 단계 실행 래퍼."""
    try:
        result = await coro
        logger.success(f"✅ {name}")
        return result
    except Exception as e:
        logger.error(f"❌ {name}: {e}")
        return None


async def run_tests():
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>")

    logger.info("=" * 60)
    logger.info("  KR Stock AutoTrader - 시스템 테스트 시작")
    logger.info("=" * 60)

    passed = 0
    failed = 0

    # ─── 1. Core 모듈 테스트 ───────────────────────────────

    logger.info("\n📦 [1/7] Core 모듈 테스트")

    # 설정 로드
    try:
        from core.config import AppConfig, load_settings
        settings = load_settings()
        assert "system" in settings
        assert "modules" in settings
        logger.success("✅ settings.yaml 로드 성공")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 설정 로드 실패: {e}")
        failed += 1

    # 데이터 모델
    try:
        from core.data_models import (
            NewsArticle, RumorData, PolicyEvent,
            StockCandidate, TechnicalSignal, Order,
            OrderSide, OrderType, Sentiment, SignalStrength,
        )
        article = NewsArticle(
            title="테스트 뉴스", content="내용", source="test",
            url="http://test.com", published_at="2025-01-01T00:00:00",
        )
        assert article.sentiment == Sentiment.NEUTRAL
        assert article.impact_score == 0.0

        order = Order(
            ticker="005930", side=OrderSide.BUY,
            order_type=OrderType.LIMIT, quantity=10, price=70000,
        )
        assert order.status.value == "pending"

        candidate = StockCandidate(ticker="005930", name="삼성전자", score=85.5)
        assert candidate.score == 85.5

        logger.success("✅ 데이터 모델 검증 완료")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 데이터 모델 실패: {e}")
        failed += 1

    # 이벤트 버스
    try:
        from core.events import EventBus, Event, EventTypes
        EventBus.reset()
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe(EventTypes.NEWS_ANALYZED, handler)
        await bus.publish(Event(
            event_type=EventTypes.NEWS_ANALYZED,
            data={"test": True},
            source="test",
        ))
        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].data["test"] is True
        logger.success("✅ 이벤트 버스 Pub/Sub 동작 확인")
        passed += 1
        EventBus.reset()
    except Exception as e:
        logger.error(f"❌ 이벤트 버스 실패: {e}")
        failed += 1

    # 데이터베이스
    try:
        from core.database import init_db, get_db
        await init_db()
        db = await get_db()
        # 테이블 존재 확인
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        tables = [row[0] for row in await cursor.fetchall()]
        await db.close()

        expected_tables = [
            "news", "rumors", "policy_events", "orders",
            "positions", "daily_reports", "channel_trust", "stock_candidates",
        ]
        for t in expected_tables:
            assert t in tables, f"테이블 누락: {t}"

        logger.success(f"✅ 데이터베이스 초기화 완료 ({len(tables)}개 테이블)")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 데이터베이스 실패: {e}")
        failed += 1

    # ─── 2. BaseModule / PluginRegistry 테스트 ─────────────

    logger.info("\n🔌 [2/7] 플러그인 아키텍처 테스트")

    try:
        from core.base_module import BaseModule, DataProviderModule, PluginRegistry

        class DummyModule(BaseModule):
            async def initialize(self):
                pass
            async def execute(self):
                return "executed"
            async def shutdown(self):
                pass

        class DummyProvider(DataProviderModule):
            async def initialize(self):
                pass
            async def execute(self):
                return []
            async def shutdown(self):
                pass
            async def get_candidates(self):
                return [StockCandidate(ticker="TEST", name="테스트", score=50)]
            async def get_score(self, ticker):
                return 50.0

        registry = PluginRegistry()
        mod1 = DummyModule("dummy1")
        mod2 = DummyProvider("dummy_provider")

        registry.register(mod1)
        registry.register(mod2)

        assert len(registry) == 2
        assert "dummy1" in registry
        assert registry.get("dummy1") is mod1

        # DataProvider 필터링
        providers = registry.get_data_providers()
        assert len(providers) == 1
        assert providers[0].name == "dummy_provider"

        # safe_execute
        result = await mod1.safe_execute()
        assert result == "executed"
        status = mod1.get_status()
        assert status.execution_count == 1
        assert status.status.value == "idle"

        # 후보 조회
        candidates = await mod2.get_candidates()
        assert len(candidates) == 1
        assert candidates[0].ticker == "TEST"

        await registry.shutdown_all()
        logger.success("✅ PluginRegistry + BaseModule 동작 확인")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 플러그인 아키텍처 실패: {e}")
        failed += 1

    # ─── 3. 스케줄러 테스트 ────────────────────────────────

    logger.info("\n⏰ [3/7] 스케줄러 테스트")

    try:
        from core.scheduler import TradingScheduler

        scheduler = TradingScheduler(settings)

        call_count = {"value": 0}

        async def test_job():
            call_count["value"] += 1

        scheduler.add_pre_market_job(test_job, hour=8, minute=0, job_id="test_pre")
        scheduler.add_post_market_job(test_job, hour=16, minute=30, job_id="test_post")

        jobs = scheduler.get_jobs()
        assert len(jobs) == 2
        job_ids = [j["id"] for j in jobs]
        assert "test_pre" in job_ids
        assert "test_post" in job_ids

        # 시장 상태 확인
        is_open = scheduler.is_market_open()
        logger.info(f"  현재 장 상태: {'장중' if is_open else '장외'}")

        logger.success(f"✅ 스케줄러 작업 등록 확인 ({len(jobs)}개)")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 스케줄러 실패: {e}")
        failed += 1

    # ─── 4. 한국투자증권 API 클라이언트 구조 테스트 ─────────

    logger.info("\n🏦 [4/7] 한국투자증권 API 구조 테스트")

    try:
        from broker.kis_api import KISClient, KISAuth, KISAPIError
        from core.config import KISConfig

        config = KISConfig()
        client = KISClient(config)

        # 모의투자 URL 확인
        assert "openapivts" in config.base_url
        logger.info(f"  Base URL: {config.base_url}")

        # API 에러 클래스
        err = KISAPIError("테스트 에러", "EGW00001")
        assert "EGW00001" in str(err)

        logger.success("✅ KIS API 클라이언트 구조 확인 (실제 연결은 키 필요)")
        passed += 1
    except Exception as e:
        logger.error(f"❌ KIS API 구조 실패: {e}")
        failed += 1

    # ─── 5. 트레이딩 엔진 (기술적 분석) 테스트 ─────────────

    logger.info("\n📈 [5/7] 트레이딩 엔진 테스트")

    try:
        import pandas as pd
        import numpy as np
        from modules.trading.trading_engine import TradingEngine

        # 모의 캔들 데이터 생성
        np.random.seed(42)
        dates = pd.date_range("2025-01-01", periods=120, freq="B")
        prices = 70000 + np.cumsum(np.random.randn(120) * 500)

        mock_candles = []
        for i, date in enumerate(dates):
            p = prices[i]
            mock_candles.append({
                "date": date.strftime("%Y%m%d"),
                "open": int(p - 200),
                "high": int(p + 500),
                "low": int(p - 500),
                "close": int(p),
                "volume": int(np.random.randint(100000, 1000000)),
            })

        # 엔진 인스턴스 (KIS 연결 없이 지표 계산만 테스트)
        engine = TradingEngine({}, None)
        df = engine._candles_to_df(mock_candles)
        assert len(df) == 120

        # 지표 계산
        indicators = engine._calculate_indicators(df)
        logger.info(f"  계산된 지표: {list(indicators.keys())}")

        assert "rsi" in indicators
        assert "macd" in indicators
        assert "sma20" in indicators
        assert 0 <= indicators["rsi"] <= 100

        # 시그널 판단
        signal, confidence = engine._evaluate_signal(indicators, df)
        logger.info(f"  시그널: {signal.value} (신뢰도: {confidence:.3f})")
        logger.info(f"  RSI: {indicators.get('rsi', 0):.1f}")
        logger.info(f"  MACD Hist: {indicators.get('macd_hist', 0):.1f}")
        logger.info(f"  BB Position: {indicators.get('bb_position', 0):.3f}")

        # 목표가/손절가
        target, stop = engine._calc_price_targets(
            df["close"].iloc[-1], indicators, signal
        )
        if target:
            logger.info(f"  목표가: {target:,}원 / 손절가: {stop:,}원")

        logger.success("✅ 기술적 분석 엔진 동작 확인")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 트레이딩 엔진 실패: {e}")
        failed += 1

    # ─── 6. 마스터 모듈 스코어링 테스트 ────────────────────

    logger.info("\n🎯 [6/7] 마스터 모듈 스코어링 테스트")

    try:
        from modules.master.master_module import MasterModule

        # 스코어링 로직만 테스트
        master = MasterModule(
            config=settings.get("modules", {}),
            registry=PluginRegistry(),
            kis_client=None,
            trading_engine=None,
        )

        # 후보 종목 생성
        test_candidates = [
            StockCandidate(ticker="005930", name="삼성전자",
                          news_score=80, sentiment_score=60, policy_score=90),
            StockCandidate(ticker="000660", name="SK하이닉스",
                          news_score=70, sentiment_score=50, policy_score=40),
            StockCandidate(ticker="035420", name="NAVER",
                          news_score=30, sentiment_score=80, policy_score=20),
        ]

        for c in test_candidates:
            c.score = master._calculate_composite_score(c)

        test_candidates.sort(key=lambda x: x.score, reverse=True)

        for c in test_candidates:
            logger.info(
                f"  {c.ticker} ({c.name}): 종합={c.score:.1f} "
                f"(뉴스:{c.news_score:.0f} 센티:{c.sentiment_score:.0f} 정책:{c.policy_score:.0f})"
            )

        assert test_candidates[0].ticker == "005930"  # 삼성전자가 1위
        logger.success("✅ 종합 스코어링 로직 확인")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 마스터 모듈 실패: {e}")
        failed += 1

    # ─── 7. 대시보드 API 테스트 ────────────────────────────

    logger.info("\n🌐 [7/7] 대시보드 API 테스트")

    try:
        from fastapi.testclient import TestClient
        from modules.dashboard.dashboard import create_dashboard

        test_registry = PluginRegistry()
        test_scheduler = TradingScheduler(settings)

        app = create_dashboard(
            registry=test_registry,
            scheduler=test_scheduler,
            master_module=None,
            config=settings,
        )

        with TestClient(app) as client:
            # 메인 페이지
            resp = client.get("/")
            assert resp.status_code == 200
            assert "AutoTrader" in resp.text
            logger.info("  GET / → 200 OK")

            # 시스템 상태 API
            resp = client.get("/api/status")
            assert resp.status_code == 200
            data = resp.json()
            assert "system" in data
            assert data["system"] == "running"
            logger.info(f"  GET /api/status → market_open={data['market_open']}")

            # 포트폴리오 API
            resp = client.get("/api/portfolio")
            assert resp.status_code == 200
            logger.info("  GET /api/portfolio → 200 OK")

            # 후보 종목 API
            resp = client.get("/api/candidates")
            assert resp.status_code == 200
            logger.info("  GET /api/candidates → 200 OK")

            # 주문 내역 API
            resp = client.get("/api/orders")
            assert resp.status_code == 200
            logger.info("  GET /api/orders → 200 OK")

            # 성과 히스토리 API
            resp = client.get("/api/performance")
            assert resp.status_code == 200
            logger.info("  GET /api/performance → 200 OK")

            # 뉴스 API
            resp = client.get("/api/news")
            assert resp.status_code == 200
            logger.info("  GET /api/news → 200 OK")

            # 로컬 전용 API (TestClient는 로컬로 인식)
            resp = client.post("/api/settings", json={"test": True})
            assert resp.status_code == 200
            logger.info("  POST /api/settings (로컬) → 200 OK")

        logger.success("✅ 대시보드 전체 API 엔드포인트 확인")
        passed += 1
    except Exception as e:
        logger.error(f"❌ 대시보드 실패: {e}")
        failed += 1

    # ─── 결과 요약 ─────────────────────────────────────────

    logger.info("\n" + "=" * 60)
    total = passed + failed
    logger.info(f"  테스트 결과: {passed}/{total} 통과")
    if failed == 0:
        logger.success("  🎉 모든 테스트 통과!")
    else:
        logger.warning(f"  ⚠️  {failed}개 실패")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_tests())
