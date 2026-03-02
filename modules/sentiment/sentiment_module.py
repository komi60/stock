"""모듈 2: 루머 및 센티먼트 수집 모듈.

온라인 커뮤니티/SNS에서 루머를 수집하고 채널 신뢰도 점수화 시스템을 운영.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from loguru import logger

from ai.gemini_client import GeminiClient
from core.base_module import DataProviderModule
from core.data_models import RumorData, Sentiment, StockCandidate
from core.database import get_db
from core.events import Event, EventBus, EventTypes


class SentimentModule(DataProviderModule):
    """루머/센티먼트 수집 및 신뢰도 평가 모듈."""

    def __init__(self, config: dict[str, Any], gemini: GeminiClient):
        super().__init__("sentiment", config)
        self._gemini = gemini
        self._rumors: list[RumorData] = []
        self._candidates: list[StockCandidate] = []
        self._channel_trust: dict[str, float] = {}
        self._event_bus = EventBus()
        self._session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        await self._load_channel_trust()
        logger.info("센티먼트 모듈 초기화 완료")

    async def execute(self) -> list[StockCandidate]:
        """루머 수집 → AI 분석 → 신뢰도 반영 → 후보 추출."""
        # 1. 루머 수집
        raw_rumors = await self._collect_rumors()
        logger.info(f"수집된 루머/센티먼트: {len(raw_rumors)}건")

        # 2. AI 분석 + 신뢰도 반영
        if raw_rumors:
            analyzed = await self._analyze_rumors(raw_rumors)
            self._rumors.extend(analyzed)
            # 최근 200개만 유지
            self._rumors = self._rumors[-200:]

            # 3. DB 저장
            await self._save_rumors(analyzed)

            # 4. 후보 종목 추출
            self._candidates = self._extract_candidates(analyzed)

            await self._event_bus.publish(Event(
                event_type=EventTypes.SENTIMENT_UPDATED,
                data={"candidates": self._candidates, "rumor_count": len(analyzed)},
                source=self.name,
            ))

        return self._candidates

    async def get_candidates(self) -> list[StockCandidate]:
        return self._candidates

    async def get_score(self, ticker: str) -> float:
        for c in self._candidates:
            if c.ticker == ticker:
                return c.sentiment_score
        return 0.0

    async def shutdown(self) -> None:
        if self._session:
            await self._session.close()

    # ─── 채널 신뢰도 시스템 ────────────────────────────────

    async def _load_channel_trust(self) -> None:
        """DB에서 채널 신뢰도 점수 로드."""
        try:
            db = await get_db()
            cursor = await db.execute("SELECT channel, trust_score FROM channel_trust")
            rows = await cursor.fetchall()
            self._channel_trust = {row[0]: row[1] for row in rows}
            await db.close()
        except Exception as e:
            logger.error(f"채널 신뢰도 로드 오류: {e}")

        # 기본 채널 신뢰도
        defaults = {
            "naver_stock_forum": 0.3,
            "theqoo": 0.2,
            "dcinside_stock": 0.2,
            "naver_blog": 0.25,
            "twitter": 0.15,
        }
        for ch, score in defaults.items():
            if ch not in self._channel_trust:
                self._channel_trust[ch] = score

    async def update_channel_trust(self, channel: str, was_accurate: bool) -> None:
        """루머 결과에 따라 채널 신뢰도 업데이트.

        과거 루머와 실제 주가 등락폭의 상관관계를 반영.
        """
        current = self._channel_trust.get(channel, 0.5)
        if was_accurate:
            new_score = min(current + 0.02, 1.0)
        else:
            new_score = max(current - 0.03, 0.0)

        self._channel_trust[channel] = new_score

        try:
            db = await get_db()
            await db.execute(
                """INSERT INTO channel_trust (channel, trust_score, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(channel) DO UPDATE SET trust_score=?, updated_at=?""",
                (channel, new_score, datetime.now().isoformat(),
                 new_score, datetime.now().isoformat()),
            )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"채널 신뢰도 업데이트 오류: {e}")

    # ─── 루머 수집 ─────────────────────────────────────────

    async def _collect_rumors(self) -> list[dict]:
        """커뮤니티/SNS 루머 수집."""
        tasks = [
            self._collect_naver_stock_forum(),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        rumors = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"루머 수집 오류: {result}")
                continue
            rumors.extend(result)
        return rumors

    async def _collect_naver_stock_forum(self) -> list[dict]:
        """네이버 주식 토론방 인기글 수집."""
        rumors = []
        try:
            # 인기 종목 토론방 (삼성전자 예시)
            popular_tickers = ["005930", "000660", "373220", "006400", "035420"]
            for ticker in popular_tickers[:3]:
                url = f"https://finance.naver.com/item/board.naver?code={ticker}"
                try:
                    async with self._session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()

                    soup = BeautifulSoup(html, "html.parser")
                    rows = soup.select("table.type2 tbody tr")

                    for row in rows[:10]:
                        title_tag = row.select_one("td.title a")
                        if not title_tag:
                            continue
                        title = title_tag.get_text(strip=True)
                        if len(title) < 5:
                            continue

                        rumors.append({
                            "content": title,
                            "source_channel": "naver_stock_forum",
                            "ticker_hint": ticker,
                        })
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"네이버 주식 토론방 수집 오류: {e}")
        return rumors

    # ─── AI 분석 ───────────────────────────────────────────

    async def _analyze_rumors(self, raw_rumors: list[dict]) -> list[RumorData]:
        """Gemini를 통한 루머 진위/파급력 분석."""
        analyzed = []
        for rumor in raw_rumors:
            channel = rumor.get("source_channel", "unknown")
            trust = self._channel_trust.get(channel, 0.3)

            try:
                ai_result = await self._gemini.analyze_rumor(
                    rumor["content"], channel
                )
                sentiment_str = ai_result.get("sentiment", "neutral")
                try:
                    sentiment = Sentiment(sentiment_str)
                except ValueError:
                    sentiment = Sentiment.NEUTRAL

                # AI 신뢰도와 채널 신뢰도를 결합
                credibility = ai_result.get("credibility", 0.5)
                combined_trust = credibility * 0.6 + trust * 0.4

                related_tickers = ai_result.get("related_tickers", [])
                if not related_tickers and "ticker_hint" in rumor:
                    related_tickers = [rumor["ticker_hint"]]

                analyzed.append(RumorData(
                    content=rumor["content"],
                    source_channel=channel,
                    channel_trust_score=combined_trust,
                    sentiment=sentiment,
                    impact_score=ai_result.get("impact_score", 0) * combined_trust,
                    related_tickers=related_tickers,
                ))
            except Exception as e:
                logger.error(f"루머 분석 오류: {e}")
                # 분석 실패해도 저장
                analyzed.append(RumorData(
                    content=rumor["content"],
                    source_channel=channel,
                    channel_trust_score=trust,
                    related_tickers=[rumor.get("ticker_hint", "")],
                ))

            # API 레이트 리밋 방지
            await asyncio.sleep(0.5)

        return analyzed

    # ─── 후보 종목 추출 ────────────────────────────────────

    def _extract_candidates(self, rumors: list[RumorData]) -> list[StockCandidate]:
        """루머에서 투자 후보 추출."""
        ticker_data: dict[str, dict] = {}

        for rumor in rumors:
            for ticker in rumor.related_tickers:
                if not ticker:
                    continue
                if ticker not in ticker_data:
                    ticker_data[ticker] = {"total_impact": 0.0, "count": 0, "reasons": []}
                ticker_data[ticker]["total_impact"] += rumor.impact_score
                ticker_data[ticker]["count"] += 1
                if abs(rumor.impact_score) > 0.2:
                    ticker_data[ticker]["reasons"].append(
                        f"[루머] {rumor.content[:30]} (신뢰도:{rumor.channel_trust_score:.1f})"
                    )

        candidates = []
        for ticker, info in ticker_data.items():
            avg = info["total_impact"] / max(info["count"], 1)
            score = min(abs(avg) * 50 * (1 + info["count"] * 0.05), 100)

            if avg > 0:
                candidates.append(StockCandidate(
                    ticker=ticker,
                    name="",
                    sentiment_score=round(score, 2),
                    score=round(score, 2),
                    reasons=info["reasons"][:5],
                ))

        candidates.sort(key=lambda x: x.sentiment_score, reverse=True)
        return candidates[:20]

    # ─── DB ────────────────────────────────────────────────

    async def _save_rumors(self, rumors: list[RumorData]) -> None:
        try:
            db = await get_db()
            for r in rumors:
                await db.execute(
                    """INSERT INTO rumors
                       (content, source_channel, channel_trust_score, sentiment, impact_score, related_tickers)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (r.content, r.source_channel, r.channel_trust_score,
                     r.sentiment.value, r.impact_score, ",".join(r.related_tickers)),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"루머 DB 저장 오류: {e}")
