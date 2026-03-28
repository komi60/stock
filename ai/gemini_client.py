"""Google Gemini API 클라이언트.

google-genai SDK (최신) 우선, google-generativeai (구버전) 폴백.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from loguru import logger

from core.config import GeminiConfig


def _detect_sdk() -> str:
    """설치된 Gemini SDK 종류 감지."""
    try:
        from google import genai  # noqa: F401
        return "new"
    except ImportError:
        pass
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import google.generativeai  # noqa: F401
        return "legacy"
    except ImportError:
        pass
    raise ImportError(
        "Gemini SDK 미설치. 다음을 실행하세요:\n"
        "  pip install google-genai"
    )


class GeminiClient:
    """Gemini API를 이용한 AI 분석 클라이언트."""

    def __init__(self, config: GeminiConfig):
        self.config = config
        self._client = None
        self._sdk_mode: str | None = None

    async def initialize(self) -> None:
        """Gemini 클라이언트 초기화."""
        if not self.config.api_key:
            raise ValueError(
                "GEMINI_API_KEY 미설정.\n"
                "config\\.env 파일에 다음을 추가하세요:\n"
                "  GEMINI_API_KEY=your_api_key_here\n"
                "API 키 발급: https://aistudio.google.com/app/apikey"
            )

        try:
            self._sdk_mode = _detect_sdk()

            if self._sdk_mode == "new":
                from google import genai
                self._client = genai.Client(api_key=self.config.api_key)
                await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self.config.model,
                    contents="ping",
                )
            else:
                import warnings
                import google.generativeai as genai
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    genai.configure(api_key=self.config.api_key)
                self._client = genai.GenerativeModel(self.config.model)
                await asyncio.to_thread(self._client.generate_content, "ping")

            logger.info(f"Gemini 초기화 완료: {self.config.model} (SDK: {self._sdk_mode})")
        except Exception as e:
            logger.error(f"Gemini 초기화 실패: {e}")
            raise

    async def analyze(self, prompt: str, system_instruction: str = "") -> str:
        """텍스트 분석 요청."""
        if not self._client:
            await self.initialize()

        full_prompt = f"{system_instruction}\n\n{prompt}" if system_instruction else prompt

        try:
            if self._sdk_mode == "new":
                response = await asyncio.to_thread(
                    self._client.models.generate_content,
                    model=self.config.model,
                    contents=full_prompt,
                )
            else:
                response = await asyncio.to_thread(
                    self._client.generate_content,
                    full_prompt,
                )
            return response.text
        except Exception as e:
            logger.error(f"Gemini API 호출 실패: {e}")
            raise

    def _parse_json_response(self, text: str) -> Any:
        """AI 응답에서 JSON 파싱 (코드블록 제거)."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1]
            cleaned = cleaned.rsplit("```", 1)[0]
        return json.loads(cleaned)

    async def analyze_news_sentiment(self, articles: list[dict]) -> list[dict]:
        """뉴스 기사 감정 분석.

        Args:
            articles: [{"title": ..., "content": ...}, ...]

        Returns:
            [{"sentiment": ..., "impact_score": ..., "related_tickers": [...], "summary": ...}, ...]
        """
        if not articles:
            return []

        articles_text = "\n---\n".join(
            f"제목: {a['title']}\n내용: {a.get('content', '')[:500]}"
            for a in articles[:10]
        )

        system_instruction = """당신은 한국 주식시장 전문 분석가입니다.
다음 뉴스 기사들을 분석하여 JSON 배열로 응답하세요.
국내 뉴스와 해외(영문) 뉴스가 혼재할 수 있습니다.
해외 뉴스는 한국 시장(KOSPI/KOSDAQ)에 미치는 간접 영향을 분석하세요.
예: 미국 반도체 호재 → 삼성전자(005930)/SK하이닉스(000660) 수혜,
   유가 급등 → 정유주(010950,096770) 수혜/항공주(003490) 악재,
   연준 금리 동결 → 성장주 전반 호재 등.

각 기사에 대해:
1. sentiment: "very_positive", "positive", "neutral", "negative", "very_negative"
2. impact_score: -1.0 ~ 1.0 (한국 시장에 미치는 영향력)
3. related_tickers: 영향받는 한국 종목코드 배열 (예: ["005930", "000660"])
4. summary: 한 문장 한국어 요약

반드시 JSON 배열만 응답하세요. 다른 텍스트 없이."""

        try:
            response = await self.analyze(articles_text, system_instruction)
            return self._parse_json_response(response)
        except Exception as e:
            logger.error(f"뉴스 감정 분석 실패: {e}")
            return []

    async def analyze_rumor(self, rumor_text: str, source: str) -> dict:
        """루머 진위/파급력 분석."""
        system_instruction = """당신은 한국 주식시장 루머 분석 전문가입니다.
다음 루머를 분석하여 JSON으로 응답하세요:

1. credibility: 0.0 ~ 1.0 (신뢰도)
2. sentiment: "very_positive", "positive", "neutral", "negative", "very_negative"
3. impact_score: -1.0 ~ 1.0 (시장 영향력)
4. related_tickers: 관련 종목코드 배열
5. analysis: 한 문장 분석

JSON만 응답하세요."""

        prompt = f"출처: {source}\n내용: {rumor_text}"

        try:
            response = await self.analyze(prompt, system_instruction)
            return self._parse_json_response(response)
        except Exception as e:
            logger.error(f"루머 분석 실패: {e}")
            return {"credibility": 0.0, "sentiment": "neutral", "impact_score": 0.0}

    async def analyze_policy(self, policy_text: str) -> dict:
        """정책/발언 영향 분석."""
        system_instruction = """당신은 한국 정부 정책 및 경제 전문가입니다.
다음 정책/발언을 분석하여 JSON으로 응답하세요:

1. impact_score: -1.0 ~ 1.0
2. beneficiary_sectors: 수혜 섹터 배열 (예: ["반도체", "2차전지"])
3. affected_tickers: 관련 종목코드 배열
4. analysis: 분석 요약 (2-3문장)

JSON만 응답하세요."""

        try:
            response = await self.analyze(policy_text, system_instruction)
            return self._parse_json_response(response)
        except Exception as e:
            logger.error(f"정책 분석 실패: {e}")
            return {"impact_score": 0.0, "beneficiary_sectors": [], "affected_tickers": []}

    async def generate_daily_report(self, data: dict) -> str:
        """일일 투자 보고서 생성."""
        system_instruction = """당신은 투자 자문 보고서 작성 전문가입니다.
제공된 데이터를 바탕으로 깔끔한 HTML 형식의 일일 투자 보고서를 작성하세요.
한국어로 작성하며, 핵심 데이터와 인사이트를 포함하세요."""

        prompt = json.dumps(data, ensure_ascii=False, default=str)
        return await self.analyze(prompt, system_instruction)
