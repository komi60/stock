"""모듈 1: 뉴스 분석 모듈.

국내/해외 뉴스를 크롤링하고 Gemini API로 감정 분석 및 종목 추출.
매일 08:30 KST까지 투자 관심 종목 리포트 생성.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from loguru import logger

from ai.gemini_client import GeminiClient
from core.base_module import DataProviderModule
from core.data_models import NewsArticle, Sentiment, StockCandidate
from core.database import get_db
from core.events import Event, EventBus, EventTypes


class NewsAnalysisModule(DataProviderModule):
    """뉴스 수집 및 AI 분석 모듈."""

    def __init__(self, config: dict[str, Any], gemini: GeminiClient):
        super().__init__("news_analysis", config)
        self._gemini = gemini
        self._articles: list[NewsArticle] = []
        self._candidates: list[StockCandidate] = []
        self._event_bus = EventBus()
        self._session: aiohttp.ClientSession | None = None

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
        logger.info("뉴스 분석 모듈 초기화 완료")

    async def execute(self) -> list[StockCandidate]:
        """뉴스 수집 → AI 분석 → 후보 종목 추출."""
        # 1. 뉴스 수집
        raw_articles = await self._collect_all_news()
        logger.info(f"수집된 뉴스: {len(raw_articles)}건")

        # 2. AI 감정 분석
        if raw_articles:
            analyzed = await self._analyze_articles(raw_articles)
            self._articles = analyzed

            # 3. DB 저장
            await self._save_articles(analyzed)

            # 4. 후보 종목 추출
            self._candidates = self._extract_candidates(analyzed)

            # 5. 이벤트 발행
            await self._event_bus.publish(Event(
                event_type=EventTypes.NEWS_ANALYZED,
                data={"candidates": self._candidates, "article_count": len(analyzed)},
                source=self.name,
            ))

        return self._candidates

    async def get_candidates(self) -> list[StockCandidate]:
        return self._candidates

    async def get_score(self, ticker: str) -> float:
        for c in self._candidates:
            if c.ticker == ticker:
                return c.news_score
        return 0.0

    async def shutdown(self) -> None:
        if self._session:
            await self._session.close()

    # ─── 뉴스 수집 ─────────────────────────────────────────

    async def _collect_all_news(self) -> list[dict]:
        """모든 소스에서 뉴스 수집 (국내 + 해외)."""
        tasks = [
            self._collect_naver_finance_news(),
            self._collect_rss_news(),
            self._collect_global_rss_news(),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        articles = []
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"뉴스 수집 오류: {result}")
                continue
            articles.extend(result)
        return articles

    async def _collect_naver_finance_news(self) -> list[dict]:
        """네이버 금융 뉴스 수집."""
        articles = []
        try:
            url = "https://finance.naver.com/news/mainnews.naver"
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return articles
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            news_items = soup.select("li.block1")

            for item in news_items[:20]:
                title_tag = item.select_one("dd.articleSubject a")
                summary_tag = item.select_one("dd.articleSummary")
                if not title_tag:
                    continue
                title = title_tag.get_text(strip=True)
                link = "https://finance.naver.com" + title_tag.get("href", "")
                summary = summary_tag.get_text(strip=True) if summary_tag else ""

                articles.append({
                    "title": title,
                    "content": summary[:500],
                    "source": "naver_finance",
                    "url": link,
                    "published_at": datetime.now().isoformat(),
                })
        except Exception as e:
            logger.error(f"네이버 금융 뉴스 수집 오류: {e}")
        return articles

    async def _collect_rss_news(self) -> list[dict]:
        """국내 RSS 피드를 통한 뉴스 수집."""
        rss_feeds = [
            ("https://www.hankyung.com/feed/finance", "hankyung"),
            ("https://rss.etnews.com/Section902.xml", "etnews"),
            ("https://www.mk.co.kr/rss/30100041/", "mk"),
        ]
        articles = []
        for feed_url, source in rss_feeds:
            try:
                async with self._session.get(feed_url) as resp:
                    if resp.status != 200:
                        continue
                    content = await resp.text()

                feed = feedparser.parse(content)
                for entry in feed.entries[:15]:
                    articles.append({
                        "title": entry.get("title", ""),
                        "content": BeautifulSoup(
                            entry.get("summary", ""), "html.parser"
                        ).get_text()[:500],
                        "source": source,
                        "url": entry.get("link", ""),
                        "published_at": entry.get("published", datetime.now().isoformat()),
                    })
            except Exception as e:
                logger.error(f"RSS 수집 오류 ({source}): {e}")
        return articles

    async def _collect_global_rss_news(self) -> list[dict]:
        """해외 글로벌 뉴스 RSS 수집 (Reuters, CNBC, Bloomberg, Yahoo Finance 등)."""
        global_feeds = [
            # Reuters
            ("https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best", "reuters"),
            # CNBC
            ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147", "cnbc_world"),
            ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135", "cnbc_asia"),
            ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "cnbc_tech"),
            # Yahoo Finance
            ("https://finance.yahoo.com/news/rssindex", "yahoo_finance"),
            # MarketWatch
            ("https://feeds.marketwatch.com/marketwatch/topstories/", "marketwatch"),
            ("https://feeds.marketwatch.com/marketwatch/marketpulse/", "marketwatch_pulse"),
            # Investing.com
            ("https://www.investing.com/rss/news.rss", "investing_com"),
            # Financial Times (무료 피드)
            ("https://www.ft.com/rss/home", "ft"),
            # Nikkei Asia (아시아 시장)
            ("https://asia.nikkei.com/rss", "nikkei_asia"),
            # Seeking Alpha
            ("https://seekingalpha.com/market_currents.xml", "seeking_alpha"),
        ]
        articles = []
        for feed_url, source in global_feeds:
            try:
                async with self._session.get(
                    feed_url, timeout=aiohttp.ClientTimeout(total=12)
                ) as resp:
                    if resp.status != 200:
                        continue
                    content = await resp.text()

                feed = feedparser.parse(content)
                count = 0
                for entry in feed.entries[:10]:
                    title = entry.get("title", "").strip()
                    if not title:
                        continue
                    summary = BeautifulSoup(
                        entry.get("summary", entry.get("description", "")),
                        "html.parser",
                    ).get_text()[:500]

                    articles.append({
                        "title": title,
                        "content": summary,
                        "source": source,
                        "url": entry.get("link", ""),
                        "published_at": entry.get(
                            "published", datetime.now().isoformat()
                        ),
                    })
                    count += 1
                if count > 0:
                    logger.info(f"  해외 RSS [{source}] → {count}건")
            except Exception as e:
                logger.debug(f"해외 RSS [{source}] 수집 실패: {e}")
        return articles

    # ─── AI 분석 ───────────────────────────────────────────

    async def _analyze_articles(self, raw_articles: list[dict]) -> list[NewsArticle]:
        """Gemini API를 통한 뉴스 감정 분석."""
        analyzed = []
        # 10개 단위 배치 분석
        for i in range(0, len(raw_articles), 10):
            batch = raw_articles[i : i + 10]
            try:
                ai_results = await self._gemini.analyze_news_sentiment(batch)
                for j, article_data in enumerate(batch):
                    ai = ai_results[j] if j < len(ai_results) else {}
                    sentiment_str = ai.get("sentiment", "neutral")
                    try:
                        sentiment = Sentiment(sentiment_str)
                    except ValueError:
                        sentiment = Sentiment.NEUTRAL

                    analyzed.append(NewsArticle(
                        title=article_data["title"],
                        content=article_data.get("content", ""),
                        source=article_data.get("source", ""),
                        url=article_data.get("url", ""),
                        published_at=datetime.fromisoformat(
                            article_data.get("published_at", datetime.now().isoformat())
                        ),
                        sentiment=sentiment,
                        impact_score=float(ai.get("impact_score", 0)),
                        related_tickers=ai.get("related_tickers", []),
                        ai_summary=ai.get("summary", ""),
                    ))
            except Exception as e:
                logger.error(f"배치 뉴스 분석 오류: {e}")
                # 분석 실패 시 raw 데이터라도 저장
                for article_data in batch:
                    analyzed.append(NewsArticle(
                        title=article_data["title"],
                        content=article_data.get("content", ""),
                        source=article_data.get("source", ""),
                        url=article_data.get("url", ""),
                        published_at=datetime.now(),
                    ))
        return analyzed

    # ─── 후보 종목 추출 ────────────────────────────────────

    def _extract_candidates(self, articles: list[NewsArticle]) -> list[StockCandidate]:
        """분석된 뉴스에서 투자 후보 종목 추출."""
        ticker_scores: dict[str, dict] = {}

        for article in articles:
            for ticker in article.related_tickers:
                if ticker not in ticker_scores:
                    ticker_scores[ticker] = {
                        "total_impact": 0.0,
                        "count": 0,
                        "reasons": [],
                    }
                ticker_scores[ticker]["total_impact"] += article.impact_score
                ticker_scores[ticker]["count"] += 1
                if article.impact_score > 0.3:
                    ticker_scores[ticker]["reasons"].append(
                        f"[뉴스] {article.title[:40]}"
                    )

        candidates = []
        for ticker, info in ticker_scores.items():
            avg_impact = info["total_impact"] / max(info["count"], 1)
            # 뉴스 점수: 평균 영향력 * 기사 수 가중치
            news_score = min(abs(avg_impact) * (1 + info["count"] * 0.1) * 50, 100)

            if avg_impact > 0:  # 긍정적인 종목만
                candidates.append(StockCandidate(
                    ticker=ticker,
                    name="",  # 마스터 모듈에서 채움
                    news_score=round(news_score, 2),
                    score=round(news_score, 2),
                    reasons=info["reasons"][:5],
                ))

        # 점수 내림차순 정렬
        candidates.sort(key=lambda x: x.news_score, reverse=True)
        return candidates[:20]  # 상위 20개

    # ─── DB 저장 ───────────────────────────────────────────

    async def _save_articles(self, articles: list[NewsArticle]) -> None:
        """분석된 뉴스를 DB에 저장."""
        try:
            db = await get_db()
            for article in articles:
                await db.execute(
                    """INSERT OR IGNORE INTO news
                       (title, content, source, url, published_at, sentiment, impact_score, related_tickers, ai_summary)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        article.title,
                        article.content,
                        article.source,
                        article.url,
                        article.published_at.isoformat(),
                        article.sentiment.value,
                        article.impact_score,
                        ",".join(article.related_tickers),
                        article.ai_summary,
                    ),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"뉴스 DB 저장 오류: {e}")
