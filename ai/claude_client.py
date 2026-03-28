"""Anthropic Claude API 클라이언트."""

from __future__ import annotations

import json
from typing import Any

import anthropic
from loguru import logger

from core.config import ClaudeConfig


class ClaudeClient:
    """Claude API를 이용한 AI 분석 클라이언트."""

    def __init__(self, config: ClaudeConfig):
        self.config = config
        self._client: anthropic.AsyncAnthropic | None = None

    async def initialize(self) -> None:
        """Claude 클라이언트 초기화."""
        if not self.config.api_key:
            raise ValueError(
                "CLAUDE_API_KEY 미설정.\n"
                "config\\.env 파일에 다음을 추가하세요:\n"
                "  CLAUDE_API_KEY=sk-ant-...\n"
                "API 키 발급: https://console.anthropic.com/"
            )

        try:
            self._client = anthropic.AsyncAnthropic(api_key=self.config.api_key)
            # 연결 확인
            await self._client.messages.create(
                model=self.config.model,
                max_tokens=10,
                messages=[{"role": "user", "content": "ping"}],
            )
            logger.info(f"Claude AI 초기화 완료: {self.config.model}")
        except Exception as e:
            logger.error(f"Claude AI 초기화 실패: {e}")
            raise

    async def analyze(self, prompt: str, system_instruction: str = "") -> str:
        """텍스트 분석 요청."""
        if not self._client:
            await self.initialize()

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_instruction:
            kwargs["system"] = system_instruction

        try:
            response = await self._client.messages.create(**kwargs)
            return response.content[0].text
        except Exception as e:
            logger.error(f"Claude API 호출 실패: {e}")
            raise

    def _parse_json_response(self, text: str) -> Any:
        """AI 응답에서 JSON 파싱 (코드블록 제거)."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            cleaned = cleaned.rsplit("```", 1)[0]
        return json.loads(cleaned)
