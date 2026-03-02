"""대시보드 단독 실행 스크립트.

API 키 설정 전에도 대시보드를 미리 볼 수 있도록
최소한의 컴포넌트만 초기화하여 실행.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import asyncio
import uvicorn
from loguru import logger

from core.config import load_settings
from core.database import init_db
from core.base_module import PluginRegistry
from core.scheduler import TradingScheduler
from modules.dashboard.dashboard import create_dashboard


async def setup():
    """최소 초기화."""
    settings = load_settings()
    await init_db()
    return settings


def main():
    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>")

    settings = asyncio.run(setup())

    registry = PluginRegistry()
    scheduler = TradingScheduler(settings)

    app = create_dashboard(
        registry=registry,
        scheduler=scheduler,
        master_module=None,
        config=settings,
    )

    host = "127.0.0.1"
    port = 8080

    logger.info("=" * 50)
    logger.info("  KR Stock AutoTrader - 대시보드 서버")
    logger.info(f"  http://{host}:{port}")
    logger.info(f"  API 키 설정: http://{host}:{port}/setup")
    logger.info("=" * 50)

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
