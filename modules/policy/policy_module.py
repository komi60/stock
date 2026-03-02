"""모듈 3: 정치 및 정책 분석 모듈.

정부 정책 발표, 고위 인사 발언을 수집하고 수혜/피해 섹터를 추론.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import aiohttp
from bs4 import BeautifulSoup
from loguru import logger

from ai.gemini_client import GeminiClient
from core.base_module import DataProviderModule
from core.data_models import PolicyEvent, StockCandidate
from core.database import get_db
from core.events import Event, EventBus, EventTypes


class PolicyModule(DataProviderModule):
    """정치/정책 분석 모듈."""

    def __init__(self, config: dict[str, Any], gemini: GeminiClient):
        super().__init__("policy", config)
        self._gemini = gemini
        self._events: list[PolicyEvent] = []
        self._candidates: list[StockCandidate] = []
        self._event_bus = EventBus()
        self._session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        logger.info("정책 분석 모듈 초기화 완료")

    async def execute(self) -> list[StockCandidate]:
        """정책 수집 → AI 분석 → 수혜 종목 추출."""
        raw_events = await self._collect_policy_events()
        logger.info(f"수집된 정책/발언: {len(raw_events)}건")

        if raw_events:
            analyzed = await self._analyze_events(raw_events)
            self._events = analyzed

            await self._save_events(analyzed)

            self._candidates = self._extract_candidates(analyzed)

            await self._event_bus.publish(Event(
                event_type=EventTypes.POLICY_ANALYZED,
                data={"candidates": self._candidates, "event_count": len(analyzed)},
                source=self.name,
            ))

        return self._candidates

    async def get_candidates(self) -> list[StockCandidate]:
        return self._candidates

    async def get_score(self, ticker: str) -> float:
        for c in self._candidates:
            if c.ticker == ticker:
                return c.policy_score
        return 0.0

    async def shutdown(self) -> None:
        if self._session:
            await self._session.close()

    # ─── 정책 수집 ─────────────────────────────────────────

    async def _collect_policy_events(self) -> list[dict]:
        """정부/기관 정책 뉴스 수집."""
        tasks = [
            self._collect_government_news(),
            self._collect_bok_news(),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        events = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"정책 수집 오류: {result}")
                continue
            events.extend(result)
        return events

    async def _collect_government_news(self) -> list[dict]:
        """정부 정책 관련 뉴스 (네이버 뉴스 정책 섹션)."""
        events = []
        try:
            url = "https://search.naver.com/search.naver"
            params = {"where": "news", "query": "정부 정책 경제", "sort": "1"}  # 최신순
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return events
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            news_items = soup.select("div.news_area")

            for item in news_items[:10]:
                title_tag = item.select_one("a.news_tit")
                desc_tag = item.select_one("div.news_dsc")
                if not title_tag:
                    continue

                title = title_tag.get_text(strip=True)
                # 정책 관련 키워드 필터
                policy_keywords = [
                    "정책", "규제", "법안", "예산", "금리", "기준금리",
                    "부양", "완화", "긴축", "세제", "보조금", "지원",
                    "장관", "대통령", "총리", "위원장",
                ]
                if not any(kw in title for kw in policy_keywords):
                    continue

                events.append({
                    "title": title,
                    "content": desc_tag.get_text(strip=True) if desc_tag else "",
                    "source": "government_news",
                    "event_type": "policy",
                    "url": title_tag.get("href", ""),
                })
        except Exception as e:
            logger.error(f"정부 뉴스 수집 오류: {e}")
        return events

    async def _collect_bok_news(self) -> list[dict]:
        """한국은행 보도자료."""
        events = []
        try:
            url = "https://www.bok.or.kr/portal/bbs/P0000559/list.do"
            params = {"menuNo": "200761", "pageIndex": "1"}
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return events
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            items = soup.select("ul.bd-list li")

            for item in items[:10]:
                title_tag = item.select_one("a")
                if not title_tag:
                    continue
                title = title_tag.get_text(strip=True)
                if len(title) < 5:
                    continue

                events.append({
                    "title": title,
                    "content": title,
                    "source": "bok",
                    "event_type": "policy",
                    "url": "https://www.bok.or.kr" + title_tag.get("href", ""),
                })
        except Exception as e:
            logger.error(f"한국은행 뉴스 수집 오류: {e}")
        return events

    # ─── AI 분석 ───────────────────────────────────────────

    async def _analyze_events(self, raw_events: list[dict]) -> list[PolicyEvent]:
        """Gemini를 통한 정책 영향 분석."""
        analyzed = []
        for event in raw_events:
            try:
                policy_text = f"제목: {event['title']}\n내용: {event.get('content', '')}"
                ai_result = await self._gemini.analyze_policy(policy_text)

                analyzed.append(PolicyEvent(
                    title=event["title"],
                    content=event.get("content", ""),
                    source=event.get("source", ""),
                    event_type=event.get("event_type", "policy"),
                    published_at=datetime.now(),
                    impact_score=ai_result.get("impact_score", 0),
                    beneficiary_sectors=ai_result.get("beneficiary_sectors", []),
                    affected_tickers=ai_result.get("affected_tickers", []),
                    ai_analysis=ai_result.get("analysis", ""),
                ))
            except Exception as e:
                logger.error(f"정책 분석 오류: {e}")
                analyzed.append(PolicyEvent(
                    title=event["title"],
                    content=event.get("content", ""),
                    source=event.get("source", ""),
                    event_type=event.get("event_type", "policy"),
                    published_at=datetime.now(),
                ))
            await asyncio.sleep(0.5)

        return analyzed

    # ─── 후보 종목 추출 ────────────────────────────────────

    def _extract_candidates(self, events: list[PolicyEvent]) -> list[StockCandidate]:
        """정책 분석에서 수혜 종목 추출."""
        ticker_data: dict[str, dict] = {}

        for event in events:
            for ticker in event.affected_tickers:
                if not ticker:
                    continue
                if ticker not in ticker_data:
                    ticker_data[ticker] = {
                        "total_impact": 0.0,
                        "count": 0,
                        "sectors": set(),
                        "reasons": [],
                    }
                ticker_data[ticker]["total_impact"] += event.impact_score
                ticker_data[ticker]["count"] += 1
                ticker_data[ticker]["sectors"].update(event.beneficiary_sectors)
                if event.impact_score > 0.2:
                    ticker_data[ticker]["reasons"].append(
                        f"[정책] {event.title[:30]}"
                    )

        candidates = []
        for ticker, info in ticker_data.items():
            avg = info["total_impact"] / max(info["count"], 1)
            score = min(abs(avg) * 60, 100)

            if avg > 0:
                candidates.append(StockCandidate(
                    ticker=ticker,
                    name="",
                    policy_score=round(score, 2),
                    score=round(score, 2),
                    reasons=info["reasons"][:5],
                ))

        candidates.sort(key=lambda x: x.policy_score, reverse=True)
        return candidates[:20]

    # ─── DB ────────────────────────────────────────────────

    async def _save_events(self, events: list[PolicyEvent]) -> None:
        try:
            db = await get_db()
            for e in events:
                await db.execute(
                    """INSERT INTO policy_events
                       (title, content, source, event_type, published_at,
                        impact_score, beneficiary_sectors, affected_tickers, ai_analysis)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (e.title, e.content, e.source, e.event_type,
                     e.published_at.isoformat(), e.impact_score,
                     ",".join(e.beneficiary_sectors),
                     ",".join(e.affected_tickers), e.ai_analysis),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"정책 DB 저장 오류: {e}")
