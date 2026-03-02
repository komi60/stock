"""모든 모듈의 기반이 되는 Abstract Base Class 및 플러그인 레지스트리."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from loguru import logger

from core.data_models import ModuleHealthReport, ModuleStatus


class BaseModule(ABC):
    """모든 분석/매매 모듈이 반드시 상속해야 하는 추상 기반 클래스.

    새로운 모듈을 추가할 때:
    1. BaseModule을 상속
    2. initialize(), execute(), shutdown() 구현
    3. PluginRegistry에 등록
    """

    def __init__(self, name: str, config: dict[str, Any] | None = None):
        self.name = name
        self.config = config or {}
        self._status = ModuleStatus.IDLE
        self._last_execution: Optional[datetime] = None
        self._execution_count = 0
        self._error_count = 0
        self._last_error: Optional[str] = None
        self._running = False
        self._lock = asyncio.Lock()

    # ─── 추상 메서드 (반드시 구현) ──────────────────────────

    @abstractmethod
    async def initialize(self) -> None:
        """모듈 초기화. 리소스 할당, API 연결 등."""
        ...

    @abstractmethod
    async def execute(self) -> Any:
        """모듈의 핵심 로직 1회 실행. 스케줄러에 의해 호출됨."""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """모듈 종료. 리소스 해제."""
        ...

    # ─── 공통 메서드 ───────────────────────────────────────

    async def safe_execute(self) -> Any:
        """에러 핸들링이 포함된 안전한 실행 래퍼."""
        async with self._lock:
            try:
                self._status = ModuleStatus.RUNNING
                result = await self.execute()
                self._last_execution = datetime.now()
                self._execution_count += 1
                self._status = ModuleStatus.IDLE
                return result
            except Exception as e:
                self._error_count += 1
                self._last_error = str(e)
                self._status = ModuleStatus.ERROR
                logger.error(f"[{self.name}] 실행 오류: {e}")
                raise

    def get_status(self) -> ModuleHealthReport:
        """모듈 상태 리포트."""
        return ModuleHealthReport(
            module_name=self.name,
            status=self._status,
            last_execution=self._last_execution,
            execution_count=self._execution_count,
            error_count=self._error_count,
            last_error=self._last_error,
        )

    @property
    def is_running(self) -> bool:
        return self._status == ModuleStatus.RUNNING

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(name={self.name}, status={self._status.value})>"


class DataProviderModule(BaseModule):
    """데이터 제공 모듈의 추상 클래스 (모듈 1, 2, 3).

    마스터 모듈에 종목 후보 리스트를 제공하는 역할.
    """

    @abstractmethod
    async def get_candidates(self) -> list[Any]:
        """현재까지 분석된 투자 후보 종목 리스트를 반환."""
        ...

    @abstractmethod
    async def get_score(self, ticker: str) -> float:
        """특정 종목에 대한 이 모듈의 점수를 반환."""
        ...


class TradingModule(BaseModule):
    """트레이딩 모듈의 추상 클래스 (모듈 4).

    기술적 분석 신호를 제공하는 역할.
    """

    @abstractmethod
    async def analyze(self, ticker: str) -> Any:
        """특정 종목에 대한 기술적 분석 수행."""
        ...

    @abstractmethod
    async def get_signal(self, ticker: str) -> Any:
        """특정 종목의 매매 시그널 반환."""
        ...


class PluginRegistry:
    """모듈을 동적으로 등록/관리하는 레지스트리.

    마스터 모듈이 이 레지스트리를 통해 플러그인 모듈에 접근.
    """

    def __init__(self):
        self._modules: dict[str, BaseModule] = {}

    def register(self, module: BaseModule) -> None:
        """모듈 등록."""
        if module.name in self._modules:
            logger.warning(f"모듈 '{module.name}'이 이미 등록되어 있습니다. 덮어씁니다.")
        self._modules[module.name] = module
        logger.info(f"모듈 등록: {module.name}")

    def unregister(self, name: str) -> None:
        """모듈 등록 해제."""
        if name in self._modules:
            del self._modules[name]
            logger.info(f"모듈 해제: {name}")

    def get(self, name: str) -> BaseModule | None:
        """이름으로 모듈 조회."""
        return self._modules.get(name)

    def get_all(self) -> dict[str, BaseModule]:
        """등록된 모든 모듈."""
        return dict(self._modules)

    def get_data_providers(self) -> list[DataProviderModule]:
        """DataProviderModule 타입 모듈만 반환."""
        return [m for m in self._modules.values() if isinstance(m, DataProviderModule)]

    def get_trading_modules(self) -> list[TradingModule]:
        """TradingModule 타입 모듈만 반환."""
        return [m for m in self._modules.values() if isinstance(m, TradingModule)]

    async def initialize_all(self) -> None:
        """모든 등록 모듈 초기화."""
        for name, module in self._modules.items():
            try:
                await module.initialize()
                logger.info(f"모듈 초기화 완료: {name}")
            except Exception as e:
                logger.error(f"모듈 초기화 실패: {name} - {e}")

    async def shutdown_all(self) -> None:
        """모든 등록 모듈 종료."""
        for name, module in self._modules.items():
            try:
                await module.shutdown()
                logger.info(f"모듈 종료: {name}")
            except Exception as e:
                logger.error(f"모듈 종료 오류: {name} - {e}")

    def __len__(self) -> int:
        return len(self._modules)

    def __contains__(self, name: str) -> bool:
        return name in self._modules
