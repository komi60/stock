"""Upbit Crypto AutoTrader - 메인 엔트리포인트.

24시간 암호화폐 자동매매 시스템의 진입점.
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

from ai.claude_client import ClaudeClient

# 암호화폐 자동매매 모듈
from broker.upbit_api import UpbitClient
from modules.crypto_news.crypto_news_module import CryptoNewsModule
from modules.crypto_trading.crypto_trading_module import CryptoTradingModule
from modules.crypto_master.crypto_master_module import CryptoMasterModule
from modules.dashboard.dashboard import create_dashboard


class AutoTraderApp:
    """메인 애플리케이션. 모든 모듈의 생명주기를 관리."""

    def __init__(self):
        self._config: AppConfig | None = None
        self._registry = PluginRegistry()
        self._scheduler: TradingScheduler | None = None
        self._claude: ClaudeClient | None = None
        self._upbit: UpbitClient | None = None
        self._crypto_master: CryptoMasterModule | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """시스템 시작."""
        logger.info("=" * 60)
        logger.info("  Upbit Crypto AutoTrader 시스템 시작")
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

        # 시작 즉시 뉴스 수집 1회 실행 (30분 대기 없이 바로 시작)
        asyncio.create_task(self._crypto_news_collect())

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

        if self._upbit:
            await self._upbit.close()

        logger.info("시스템 종료 완료")

    # ─── 초기화 ────────────────────────────────────────────

    async def _init_external_services(self) -> None:
        """외부 서비스 (브로커 API, AI) 초기화."""
        # Claude AI
        self._claude = ClaudeClient(self._config.claude)
        try:
            await self._claude.initialize()
        except Exception as e:
            logger.error(f"Claude AI 초기화 실패: {e}")

        # 업비트 API
        import os
        crypto_cfg = self._config.settings.get("crypto", {})
        upbit_access_key = os.getenv("UPBIT_ACCESS_KEY", "")
        upbit_secret_key = os.getenv("UPBIT_SECRET_KEY", "")
        is_paper = crypto_cfg.get("use_paper_trading", True)
        self._upbit = UpbitClient(
            access_key=upbit_access_key,
            secret_key=upbit_secret_key,
            is_paper=is_paper,
        )
        try:
            await self._upbit.connect()
            logger.info("업비트 API 연결 완료")
        except Exception as e:
            logger.error(f"업비트 API 연결 실패: {e}")

    def _init_modules(self, settings: dict) -> None:
        """모듈 생성 및 레지스트리 등록."""
        crypto_cfg = settings.get("crypto", {})
        crypto_modules_cfg = crypto_cfg.get("modules", {})

        # 크립토 뉴스 모듈
        crypto_news = CryptoNewsModule(
            config=crypto_modules_cfg.get("crypto_news", {}),
            ai=self._claude,
        )
        self._registry.register(crypto_news)

        # 크립토 트레이딩 모듈
        crypto_trading_cfg = crypto_modules_cfg.get("crypto_trading", {})
        crypto_trading_cfg["stop_loss_pct"] = crypto_cfg.get("risk", {}).get("stop_loss_pct", 8.0)
        crypto_trading_cfg["take_profit_pct"] = crypto_cfg.get("risk", {}).get("take_profit_pct", 15.0)
        crypto_trading = CryptoTradingModule(
            config=crypto_trading_cfg,
            upbit=self._upbit,
        )
        self._registry.register(crypto_trading)

        # 크립토 마스터 모듈
        self._crypto_master = CryptoMasterModule(
            config=crypto_cfg,
            upbit=self._upbit,
            crypto_news=crypto_news,
            crypto_trading=crypto_trading,
        )
        self._registry.register(self._crypto_master)

        logger.info(f"등록된 모듈: {len(self._registry)}개")

    # ─── 스케줄러 ──────────────────────────────────────────

    def _setup_scheduler(self, settings: dict) -> None:
        """스케줄러에 작업 등록."""
        self._scheduler = TradingScheduler(settings)

        # 30분마다 - 크립토 뉴스 수집 및 Fear&Greed Index 갱신
        async def crypto_news_collect():
            crypto_news = self._registry.get("crypto_news")
            if crypto_news:
                await crypto_news.safe_execute()

        self._crypto_news_collect = crypto_news_collect  # 즉시 실행용 참조 보관

        crypto_news_interval = (
            settings.get("crypto", {})
            .get("modules", {})
            .get("crypto_news", {})
            .get("interval_minutes", 30)
        )
        self._scheduler.add_interval_job(
            crypto_news_collect,
            minutes=crypto_news_interval,
            job_id="crypto_news_collect",
        )

        # 5분마다 - 크립토 마스터 (분석 + 매매 실행)
        async def crypto_trading_loop():
            if self._crypto_master:
                await self._crypto_master.safe_execute()

        crypto_master_interval = (
            settings.get("crypto", {})
            .get("modules", {})
            .get("crypto_master", {})
            .get("execution_interval_minutes", 5)
        )
        self._scheduler.add_interval_job(
            crypto_trading_loop,
            minutes=crypto_master_interval,
            job_id="crypto_trading_loop",
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
            master_module=self._crypto_master,
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

    loop = asyncio.get_event_loop()

    def handle_shutdown(sig):
        logger.info(f"종료 시그널 수신: {sig}")
        asyncio.create_task(app.stop())
        app._shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_shutdown(s))
        except NotImplementedError:
            signal.signal(sig, lambda s, f: handle_shutdown(s))

    try:
        await app.start()
    except KeyboardInterrupt:
        logger.info("키보드 인터럽트")
    finally:
        await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
