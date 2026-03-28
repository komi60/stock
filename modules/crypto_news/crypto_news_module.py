"""크립토 뉴스 분석 모듈.

글로벌 암호화폐 뉴스를 수집하고 Gemini AI로 감성 분석 및 관련 코인 추출.
Fear & Greed Index도 함께 수집.
24/7 운영 (30분 간격).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from loguru import logger

from ai.gemini_client import GeminiClient
from core.base_module import BaseModule
from core.database import get_db


# 글로벌 크립토 뉴스 RSS 피드
CRYPTO_RSS_FEEDS = [
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "coindesk"),
    ("https://cointelegraph.com/rss", "cointelegraph"),
    ("https://decrypt.co/feed", "decrypt"),
    ("https://coindesk.co.kr/feed/", "coindesk_kr"),
    ("https://www.coindeskkorea.com/feed/", "coindesk_korea"),
    ("https://cryptonews.com/news/feed/", "cryptonews"),
]

# Fear & Greed Index API
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"


class CryptoNewsModule(BaseModule):
    """암호화폐 뉴스 수집 및 AI 분석 모듈."""

    def __init__(self, config: dict[str, Any], gemini: GeminiClient):
        super().__init__("crypto_news", config)
        self._gemini = gemini
        self._session: aiohttp.ClientSession | None = None
        self._latest_articles: list[dict] = []
        self._fear_greed_index: int = 50  # 기본값: 중립
        self._fear_greed_label: str = "Neutral"
        # 코인별 뉴스 점수 캐시
        self._coin_news_scores: dict[str, float] = {}

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        logger.info("크립토 뉴스 모듈 초기화 완료")

    async def execute(self) -> dict:
        """뉴스 수집 → AI 분석 → Fear&Greed 수집."""
        results = await asyncio.gather(
            self._collect_all_news(),
            self._fetch_fear_greed_index(),
            return_exceptions=True,
        )

        articles_raw = results[0] if not isinstance(results[0], Exception) else []
        if isinstance(results[0], Exception):
            logger.error(f"뉴스 수집 오류: {results[0]}")

        if not isinstance(results[1], Exception):
            self._fear_greed_index, self._fear_greed_label = results[1]
            logger.info(
                f"Fear & Greed Index: {self._fear_greed_index} ({self._fear_greed_label})"
            )

        if articles_raw:
            analyzed = await self._analyze_articles(articles_raw)
            self._latest_articles = analyzed
            self._update_coin_scores(analyzed)
            await self._save_articles(analyzed)
            logger.info(f"크립토 뉴스 분석 완료: {len(analyzed)}건")

        return {
            "article_count": len(self._latest_articles),
            "fear_greed_index": self._fear_greed_index,
            "fear_greed_label": self._fear_greed_label,
        }

    async def shutdown(self) -> None:
        if self._session:
            await self._session.close()

    # ─── 공개 인터페이스 ───────────────────────────────────

    def get_coin_news_score(self, market: str) -> float:
        """특정 코인의 뉴스 감성 점수 반환 (0.0~1.0).

        market 형식: "KRW-BTC" → coin 심볼 "BTC"로 조회.
        """
        coin = market.split("-")[-1] if "-" in market else market
        return self._coin_news_scores.get(coin.upper(), 0.5)

    def get_fear_greed_index(self) -> int:
        """현재 Fear & Greed Index 값 반환 (0~100)."""
        return self._fear_greed_index

    def get_latest_articles(self, limit: int = 20) -> list[dict]:
        """최근 수집된 뉴스 반환."""
        return self._latest_articles[:limit]

    # ─── 뉴스 수집 ─────────────────────────────────────────

    async def _collect_all_news(self) -> list[dict]:
        """모든 크립토 뉴스 소스에서 수집."""
        tasks = [self._collect_rss(url, source) for url, source in CRYPTO_RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                source = CRYPTO_RSS_FEEDS[i][1]
                logger.debug(f"크립토 뉴스 RSS 수집 실패 ({source}): {result}")
                continue
            articles.extend(result)

        logger.info(f"크립토 뉴스 수집: 총 {len(articles)}건")
        return articles

    async def _collect_rss(self, feed_url: str, source: str) -> list[dict]:
        """RSS 피드 수집."""
        articles = []
        try:
            async with self._session.get(
                feed_url, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    return articles
                content = await resp.text()

            feed = feedparser.parse(content)
            max_articles = self.config.get("max_articles_per_source", 30)
            for entry in feed.entries[:max_articles]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                summary = BeautifulSoup(
                    entry.get("summary", entry.get("description", "")), "html.parser"
                ).get_text()[:600]

                articles.append({
                    "title": title,
                    "content": summary,
                    "source": source,
                    "url": entry.get("link", ""),
                    "published_at": entry.get("published", datetime.now().isoformat()),
                })
        except Exception as e:
            logger.debug(f"RSS 수집 실패 ({source}): {e}")
        return articles

    async def _fetch_fear_greed_index(self) -> tuple[int, str]:
        """Fear & Greed Index 조회.

        Returns:
            (index_value, label) - 예: (25, "Extreme Fear")
        """
        try:
            async with self._session.get(
                FEAR_GREED_URL, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return self._fear_greed_index, self._fear_greed_label
                data = await resp.json()

            fng_data = data.get("data", [{}])[0]
            value = int(fng_data.get("value", 50))
            label = fng_data.get("value_classification", "Neutral")
            return value, label
        except Exception as e:
            logger.debug(f"Fear & Greed Index 조회 실패: {e}")
            return self._fear_greed_index, self._fear_greed_label

    # ─── AI 분석 ───────────────────────────────────────────

    async def _analyze_articles(self, raw_articles: list[dict]) -> list[dict]:
        """Gemini AI로 크립토 뉴스 감성 분석."""
        analyzed = []
        for i in range(0, len(raw_articles), 10):
            batch = raw_articles[i : i + 10]
            try:
                ai_results = await self._analyze_batch(batch)
                for j, article in enumerate(batch):
                    ai = ai_results[j] if j < len(ai_results) else {}
                    analyzed.append({
                        **article,
                        "sentiment": ai.get("sentiment", "neutral"),
                        "impact_score": float(ai.get("impact_score", 0.0)),
                        "related_coins": ai.get("related_coins", []),
                        "ai_summary": ai.get("summary", ""),
                    })
            except Exception as e:
                logger.error(f"크립토 뉴스 배치 분석 오류: {e}")
                for article in batch:
                    analyzed.append({
                        **article,
                        "sentiment": "neutral",
                        "impact_score": 0.0,
                        "related_coins": [],
                        "ai_summary": "",
                    })
        return analyzed

    async def _analyze_batch(self, articles: list[dict]) -> list[dict]:
        """배치 단위 Gemini 분석."""
        articles_text = "\n---\n".join(
            f"제목: {a['title']}\n내용: {a.get('content', '')[:400]}"
            for a in articles
        )

        system_instruction = """당신은 암호화폐 시장 전문 분석가입니다.
다음 크립토 뉴스 기사들을 분석하여 JSON 배열로 응답하세요.
한국어와 영어 뉴스가 혼재할 수 있습니다.

각 기사에 대해:
1. sentiment: "very_positive", "positive", "neutral", "negative", "very_negative"
2. impact_score: -1.0 ~ 1.0 (암호화폐 시장 전반에 미치는 영향)
3. related_coins: 관련 코인 심볼 배열 (예: ["BTC", "ETH", "SOL"])
   - 특정 코인 언급 없으면 빈 배열 []
   - 전체 시장 영향이면 ["BTC", "ETH"] 포함
4. summary: 한 문장 한국어 요약

반드시 JSON 배열만 응답하세요."""

        response = await self._gemini.analyze(articles_text, system_instruction)
        return self._gemini._parse_json_response(response)

    # ─── 코인 점수 업데이트 ────────────────────────────────

    def _update_coin_scores(self, articles: list[dict]) -> None:
        """뉴스 감성 분석 결과로 코인별 점수 업데이트."""
        coin_data: dict[str, list[float]] = {}

        for article in articles:
            impact = article.get("impact_score", 0.0)
            coins = article.get("related_coins", [])
            for coin in coins:
                coin = coin.upper()
                if coin not in coin_data:
                    coin_data[coin] = []
                coin_data[coin].append(impact)

        self._coin_news_scores = {}
        for coin, scores in coin_data.items():
            if not scores:
                continue
            avg_impact = sum(scores) / len(scores)
            # -1~1 범위를 0~1로 정규화, 뉴스 수 가중치 적용
            normalized = (avg_impact + 1) / 2
            count_weight = min(1 + len(scores) * 0.05, 1.3)
            self._coin_news_scores[coin] = min(normalized * count_weight, 1.0)

    # ─── DB 저장 ───────────────────────────────────────────

    async def _save_articles(self, articles: list[dict]) -> None:
        """분석된 크립토 뉴스를 DB에 저장."""
        try:
            db = await get_db()
            for a in articles:
                await db.execute(
                    """INSERT OR IGNORE INTO crypto_news
                       (title, content, source, url, sentiment, impact_score,
                        related_coins, ai_summary, published_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        a["title"],
                        a.get("content", ""),
                        a.get("source", ""),
                        a.get("url", ""),
                        a.get("sentiment", "neutral"),
                        a.get("impact_score", 0.0),
                        json.dumps(a.get("related_coins", []), ensure_ascii=False),
                        a.get("ai_summary", ""),
                        a.get("published_at", datetime.now().isoformat()),
                    ),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"크립토 뉴스 DB 저장 오류: {e}")
