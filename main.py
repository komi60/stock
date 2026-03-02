"""KR Stock AutoTrader - 메인 엔트리포인트.

24시간 자동매매 시스템의 진입점.
모든 모듈을 초기화하고 스케줄러에 등록한 뒤 실행.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
from pathlib import Path

import uvicorn
from loguru import logger

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent))

from core.config import AppConfig
from core.database import init_db
from core.logger import setup_logger
from core.base_module import PluginRegistry
from core.scheduler import TradingScheduler

from ai.gemini_client import GeminiClient
from broker.kis_api import KISClient

from modules.news.news_module import NewsAnalysisModule
from modules.sentiment.sentiment_module import SentimentModule
from modules.policy.policy_module import PolicyModule
from modules.trading.trading_engine import TradingEngine
from modules.master.master_module import MasterModule
from modules.watcher.watcher_module import WatcherModule
from modules.dashboard.dashboard import create_dashboard


class AutoTraderApp:
    """메인 애플리케이션. 모든 모듈의 생명주기를 관리."""

    def __init__(self):
        self._config: AppConfig | None = None
        self._registry = PluginRegistry()
        self._scheduler: TradingScheduler | None = None
        self._kis: KISClient | None = None
        self._gemini: GeminiClient | None = None
        self._master: MasterModule | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """시스템 시작."""
        logger.info("=" * 60)
        logger.info("  KR Stock AutoTrader 시스템 시작")
        logger.info("=" * 60)

        # 1. 설정 로드
        self._config = AppConfig.load()
        settings = self._config.settings
        setup_logger(
            log_dir=settings.get("system", {}).get("log_dir", "./logs"),
            level=settings.get("system", {}).get("log_level", "INFO"),
        )

        # 2. 데이터베이스 초기화
        await init_db()

        # 3. 외부 서비스 연결
        await self._init_external_services()

        # 4. 모듈 생성 및 등록
        self._init_modules(settings)

        # 5. 모든 모듈 초기화
        await self._registry.initialize_all()

        # 6. 스케줄러 설정 및 시작
        self._setup_scheduler(settings)
        self._scheduler.start()

        # 7. 대시보드 서버 시작 (별도 스레드)
        self._start_dashboard(settings)

        logger.info("=" * 60)
        logger.info("  시스템 가동 완료 - 24시간 운영 중")
        logger.info("=" * 60)

        # 8. 종료 시그널 대기
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """시스템 종료."""
        logger.info("시스템 종료 시작...")

        if self._scheduler:
            self._scheduler.shutdown()

        await self._registry.shutdown_all()

        if self._kis:
            await self._kis.close()

        logger.info("시스템 종료 완료")

    # ─── 초기화 ────────────────────────────────────────────

    async def _init_external_services(self) -> None:
        """외부 서비스 (브로커 API, AI) 초기화."""
        # 한국투자증권 API
        self._kis = KISClient(self._config.kis)
        try:
            await self._kis.connect()
            logger.info("한국투자증권 API 연결 완료")
        except Exception as e:
            logger.error(f"한국투자증권 API 연결 실패: {e}")
            logger.warning("API 연결 없이 시스템 시작 (모의 모드)")

        # Gemini AI
        self._gemini = GeminiClient(self._config.gemini)
        try:
            await self._gemini.initialize()
            logger.info("Gemini AI 초기화 완료")
        except Exception as e:
            logger.error(f"Gemini AI 초기화 실패: {e}")

    def _init_modules(self, settings: dict) -> None:
        """모듈 생성 및 레지스트리 등록."""
        module_cfg = settings.get("modules", {})

        # 모듈 1: 뉴스 분석
        if module_cfg.get("news", {}).get("enabled", True):
            news = NewsAnalysisModule(module_cfg.get("news", {}), self._gemini)
            self._registry.register(news)

        # 모듈 2: 센티먼트
        if module_cfg.get("sentiment", {}).get("enabled", True):
            sentiment = SentimentModule(module_cfg.get("sentiment", {}), self._gemini)
            self._registry.register(sentiment)

        # 모듈 3: 정책
        if module_cfg.get("policy", {}).get("enabled", True):
            policy = PolicyModule(module_cfg.get("policy", {}), self._gemini)
            self._registry.register(policy)

        # 모듈 4: 트레이딩 엔진
        trading = TradingEngine(module_cfg.get("trading", {}), self._kis)
        self._registry.register(trading)

        # 모듈 5: 마스터
        self._master = MasterModule(
            config=module_cfg,
            registry=self._registry,
            kis_client=self._kis,
            trading_engine=trading,
        )
        self._registry.register(self._master)

        # 모듈 6: 감시자
        if module_cfg.get("watcher", {}).get("enabled", True):
            watcher = WatcherModule(
                config=module_cfg.get("watcher", {}),
                registry=self._registry,
                email_config=self._config.email,
                gemini=self._gemini,
            )
            self._registry.register(watcher)

        logger.info(f"등록된 모듈: {len(self._registry)}개")

    # ─── 스케줄러 ──────────────────────────────────────────

    def _setup_scheduler(self, settings: dict) -> None:
        """스케줄러에 작업 등록."""
        self._scheduler = TradingScheduler(settings)

        # ── 장전 작업 ──

        # 08:00 - 모듈 1,2,3 데이터 수집 실행
        async def pre_market_data_collection():
            logger.info("=== 장전 데이터 수집 시작 ===")
            for provider in self._registry.get_data_providers():
                try:
                    await provider.safe_execute()
                except Exception as e:
                    logger.error(f"[{provider.name}] 장전 수집 오류: {e}")

        self._scheduler.add_pre_market_job(
            pre_market_data_collection, hour=8, minute=0,
            job_id="pre_market_collect",
        )

        # 08:30 - 마스터 모듈: 최종 종목 선정
        async def pre_market_selection():
            if self._master:
                await self._master.pre_market_routine()

        self._scheduler.add_pre_market_job(
            pre_market_selection, hour=8, minute=30,
            job_id="pre_market_select",
        )

        # ── 장중 작업 ──

        # 장중 매 30초마다 - 매매 로직 실행
        async def market_trading():
            if self._master:
                await self._master.market_hours_routine()

        self._scheduler.add_market_hours_job(
            market_trading, interval_seconds=30,
            job_id="market_trading",
        )

        # 장중 매 10분마다 - 센티먼트 업데이트
        async def update_sentiment():
            sentiment = self._registry.get("sentiment")
            if sentiment:
                await sentiment.safe_execute()

        self._scheduler.add_market_hours_job(
            update_sentiment, interval_seconds=600,
            job_id="sentiment_update",
        )

        # ── 장후 작업 ──

        # 16:30 - 일일 리포트 생성 및 발송
        async def daily_report():
            watcher = self._registry.get("watcher")
            if watcher:
                await watcher.safe_execute()

        self._scheduler.add_post_market_job(
            daily_report, hour=16, minute=30,
            job_id="daily_report",
        )

        # ── 상시 작업 ──

        # 15분마다 - 정책 뉴스 모니터링
        async def policy_monitor():
            policy = self._registry.get("policy")
            if policy:
                await policy.safe_execute()

        self._scheduler.add_interval_job(
            policy_monitor, minutes=15,
            job_id="policy_monitor",
        )

    # ─── 대시보드 ──────────────────────────────────────────

    def _start_dashboard(self, settings: dict) -> None:
        """FastAPI 대시보드를 별도 스레드에서 실행."""
        dashboard_cfg = settings.get("modules", {}).get("dashboard", {})
        host = dashboard_cfg.get("host", "0.0.0.0")
        port = dashboard_cfg.get("port", 8080)

        app = create_dashboard(
            registry=self._registry,
            scheduler=self._scheduler,
            master_module=self._master,
            config=settings,
        )

        def run_server():
            uvicorn.run(app, host=host, port=port, log_level="warning")

        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        logger.info(f"대시보드 서버 시작: http://{host}:{port}")


async def main():
    """메인 함수."""
    app = AutoTraderApp()

    # 종료 시그널 핸들링
    loop = asyncio.get_event_loop()

    def handle_shutdown(sig):
        logger.info(f"종료 시그널 수신: {sig}")
        asyncio.create_task(app.stop())
        app._shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_shutdown(s))
        except NotImplementedError:
            # Windows에서는 signal_handler 미지원
            signal.signal(sig, lambda s, f: handle_shutdown(s))

    try:
        await app.start()
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
