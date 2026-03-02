"""24시간 스케줄러 시스템.

APScheduler를 사용하여 장전/장중/장후 스케줄을 자동 관리.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from loguru import logger
import pytz

KST = pytz.timezone("Asia/Seoul")


class TradingScheduler:
    """KRX 시장 시간 기반 스케줄러."""

    def __init__(self, settings: dict[str, Any]):
        self._scheduler = AsyncIOScheduler(timezone=KST)
        self._settings = settings
        self._market = settings.get("market", {})

    def start(self) -> None:
        """스케줄러 시작."""
        self._scheduler.start()
        logger.info("스케줄러 시작")

    def shutdown(self) -> None:
        """스케줄러 종료."""
        self._scheduler.shutdown(wait=False)
        logger.info("스케줄러 종료")

    # ─── 스케줄 등록 헬퍼 ──────────────────────────────────

    def add_pre_market_job(
        self,
        func: Callable[..., Coroutine],
        hour: int = 8,
        minute: int = 0,
        job_id: str | None = None,
        **kwargs,
    ) -> None:
        """장전 작업 등록 (매일 월~금 지정 시각)."""
        self._scheduler.add_job(
            func,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=KST),
            id=job_id or f"pre_market_{func.__name__}",
            replace_existing=True,
            kwargs=kwargs,
        )
        logger.info(f"장전 작업 등록: {func.__name__} @ {hour:02d}:{minute:02d}")

    def add_market_hours_job(
        self,
        func: Callable[..., Coroutine],
        interval_seconds: int = 60,
        job_id: str | None = None,
        **kwargs,
    ) -> None:
        """장중 반복 작업 등록 (09:00~15:30 사이 interval마다)."""
        open_h, open_m = (9, 0)
        close_h, close_m = (15, 30)

        async def _market_hours_wrapper(**kw):
            now = datetime.now(KST)
            weekday = now.weekday()
            if weekday >= 5:  # 주말
                return
            current_time = now.hour * 60 + now.minute
            open_time = open_h * 60 + open_m
            close_time = close_h * 60 + close_m
            if open_time <= current_time <= close_time:
                await func(**kw)

        self._scheduler.add_job(
            _market_hours_wrapper,
            IntervalTrigger(seconds=interval_seconds),
            id=job_id or f"market_{func.__name__}",
            replace_existing=True,
            kwargs=kwargs,
        )
        logger.info(f"장중 작업 등록: {func.__name__} (매 {interval_seconds}초)")

    def add_post_market_job(
        self,
        func: Callable[..., Coroutine],
        hour: int = 16,
        minute: int = 0,
        job_id: str | None = None,
        **kwargs,
    ) -> None:
        """장후 작업 등록."""
        self._scheduler.add_job(
            func,
            CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute, timezone=KST),
            id=job_id or f"post_market_{func.__name__}",
            replace_existing=True,
            kwargs=kwargs,
        )
        logger.info(f"장후 작업 등록: {func.__name__} @ {hour:02d}:{minute:02d}")

    def add_interval_job(
        self,
        func: Callable[..., Coroutine],
        minutes: int = 10,
        job_id: str | None = None,
        **kwargs,
    ) -> None:
        """시간 무관 반복 작업 등록."""
        self._scheduler.add_job(
            func,
            IntervalTrigger(minutes=minutes),
            id=job_id or f"interval_{func.__name__}",
            replace_existing=True,
            kwargs=kwargs,
        )
        logger.info(f"반복 작업 등록: {func.__name__} (매 {minutes}분)")

    def add_cron_job(
        self,
        func: Callable[..., Coroutine],
        cron_expr: dict,
        job_id: str | None = None,
        **kwargs,
    ) -> None:
        """커스텀 cron 작업."""
        self._scheduler.add_job(
            func,
            CronTrigger(**cron_expr, timezone=KST),
            id=job_id or f"cron_{func.__name__}",
            replace_existing=True,
            kwargs=kwargs,
        )

    def remove_job(self, job_id: str) -> None:
        """작업 제거."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:
            pass

    def get_jobs(self) -> list[dict]:
        """등록된 작업 목록."""
        jobs = []
        for job in self._scheduler.get_jobs():
            try:
                next_run = str(job.next_run_time) if job.next_run_time else None
            except Exception:
                next_run = None
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run": next_run,
                "trigger": str(job.trigger),
            })
        return jobs

    @staticmethod
    def is_market_open() -> bool:
        """현재 장중인지 확인."""
        now = datetime.now(KST)
        if now.weekday() >= 5:
            return False
        current_minutes = now.hour * 60 + now.minute
        return 9 * 60 <= current_minutes <= 15 * 60 + 30
