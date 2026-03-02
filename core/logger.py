"""통합 로깅 시스템."""

import sys
from pathlib import Path
from loguru import logger


def setup_logger(log_dir: str = "./logs", level: str = "INFO") -> None:
    """전체 시스템 로거 설정."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # 기본 stderr 핸들러 제거
    logger.remove()

    # 콘솔 출력
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )

    # 일반 로그 파일 (일별 rotation)
    logger.add(
        str(log_path / "system_{time:YYYY-MM-DD}.log"),
        level=level,
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} - {message}",
    )

    # 에러 전용 로그
    logger.add(
        str(log_path / "error_{time:YYYY-MM-DD}.log"),
        level="ERROR",
        rotation="00:00",
        retention="60 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {name}:{function}:{line} - {message}",
    )

    # 매매 전용 로그
    logger.add(
        str(log_path / "trades_{time:YYYY-MM-DD}.log"),
        level="INFO",
        rotation="00:00",
        retention="365 days",
        encoding="utf-8",
        filter=lambda record: "trade" in record["extra"],
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
    )


trade_logger = logger.bind(trade=True)
