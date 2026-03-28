"""크립토 뉴스 분석 모듈.

흐름: 뉴스수집 → 뉴스평가(AI) → AI 종목선정 → 채널성과평가
30분 간격 실행, 24/7 운영.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
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
    # ─── 크립토 전문 (영문) ───────────────────────────────
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "coindesk"),
    ("https://cointelegraph.com/rss", "cointelegraph"),
    ("https://decrypt.co/feed", "decrypt"),
    ("https://cryptonews.com/news/feed/", "cryptonews"),
    ("https://www.theblock.co/rss.xml", "theblock"),
    ("https://bitcoinmagazine.com/feed", "bitcoinmagazine"),
    ("https://beincrypto.com/feed/", "beincrypto"),
    # ─── 미국 경제·금융 뉴스 ─────────────────────────────
    ("https://www.cnbc.com/id/10001147/device/rss/rss.html", "cnbc"),       # CNBC Finance
    ("https://feeds.reuters.com/reuters/businessNews", "reuters_business"),  # Reuters Business
    ("https://feeds.reuters.com/Reuters/worldNews", "reuters_world"),        # Reuters World (정치)
    # ─── 미국 정치 뉴스 (SEC/규제/정부 정책) ─────────────
    ("https://thehill.com/feed/", "thehill"),               # The Hill (정치/규제)
    ("https://www.politico.com/rss/politics08.xml", "politico"),  # Politico
    # ─── 한국어 크립토 뉴스 ──────────────────────────────
    ("https://coindesk.co.kr/feed/", "coindesk_kr"),
    ("https://www.coindeskkorea.com/feed/", "coindesk_korea"),
]

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"


class CryptoNewsModule(BaseModule):
    """암호화폐 뉴스 수집 → AI 분석 → 종목선정 모듈."""

    def __init__(self, config: dict[str, Any], gemini: GeminiClient):
        super().__init__("crypto_news", config)
        self._gemini = gemini
        self._session: aiohttp.ClientSession | None = None
        self._latest_articles: list[dict] = []
        self._fear_greed_index: int = 50
        self._fear_greed_label: str = "Neutral"
        self._coin_news_scores: dict[str, float] = {}
        # AI가 선정한 종목 캐시
        self._ai_selections: list[dict] = []

    async def initialize(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
        )
        # 채널 신뢰도 초기화
        await self._init_channel_trust()
        logger.info("크립토 뉴스 모듈 초기화 완료")

    async def execute(self) -> dict:
        """뉴스수집 → 뉴스평가 → AI 종목선정 → 채널성과평가."""
        # 1. 뉴스 수집 + Fear&Greed 병렬 실행
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
            logger.info(f"Fear & Greed Index: {self._fear_greed_index} ({self._fear_greed_label})")

        # 2. Gemini 뉴스 평가 (채널 신뢰도 반영)
        if articles_raw:
            channel_trust = await self._load_channel_trust()
            analyzed = await self._analyze_articles(articles_raw, channel_trust)
            self._latest_articles = analyzed
            self._update_coin_scores(analyzed)
            await self._save_articles(analyzed)
            logger.info(f"크립토 뉴스 평가 완료: {len(analyzed)}건")

            # 3. AI 종목 선정
            if self._gemini and self._gemini._client:
                await self._select_coins_with_ai(analyzed)

        # 4. 채널 성과 평가 (24시간 지난 예측 적중률 갱신)
        await self._evaluate_channel_performance()

        return {
            "article_count": len(self._latest_articles),
            "ai_selections": len(self._ai_selections),
            "fear_greed_index": self._fear_greed_index,
            "fear_greed_label": self._fear_greed_label,
        }

    async def shutdown(self) -> None:
        if self._session:
            await self._session.close()

    # ─── 공개 인터페이스 ───────────────────────────────────

    def get_coin_news_score(self, market: str) -> float:
        """특정 코인의 뉴스 감성 점수 반환 (0.0~1.0)."""
        coin = market.split("-")[-1] if "-" in market else market
        return self._coin_news_scores.get(coin.upper(), 0.5)

    def get_fear_greed_index(self) -> int:
        """현재 Fear & Greed Index 값 반환 (0~100)."""
        return self._fear_greed_index

    def get_latest_articles(self, limit: int = 20) -> list[dict]:
        """최근 수집된 뉴스 반환."""
        return self._latest_articles[:limit]

    def get_ai_selected_coins(self) -> list[dict]:
        """가장 최근 AI 선정 종목 목록 반환.

        Returns:
            [{"market": "KRW-BTC", "confidence": 0.8, "reason": "...", "news_summary": "..."}, ...]
        """
        return self._ai_selections

    # ─── 뉴스 수집 ─────────────────────────────────────────

    async def _collect_all_news(self) -> list[dict]:
        tasks = [self._collect_rss(url, source) for url, source in CRYPTO_RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        articles = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.debug(f"RSS 수집 실패 ({CRYPTO_RSS_FEEDS[i][1]}): {result}")
                continue
            articles.extend(result)
        logger.info(f"크립토 뉴스 수집: 총 {len(articles)}건")
        return articles

    async def _collect_rss(self, feed_url: str, source: str) -> list[dict]:
        articles = []
        try:
            async with self._session.get(feed_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
        try:
            async with self._session.get(FEAR_GREED_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return self._fear_greed_index, self._fear_greed_label
                data = await resp.json()
            fng_data = data.get("data", [{}])[0]
            return int(fng_data.get("value", 50)), fng_data.get("value_classification", "Neutral")
        except Exception as e:
            logger.debug(f"Fear & Greed Index 조회 실패: {e}")
            return self._fear_greed_index, self._fear_greed_label

    # ─── AI 뉴스 평가 ──────────────────────────────────────

    async def _analyze_articles(
        self, raw_articles: list[dict], channel_trust: dict[str, float]
    ) -> list[dict]:
        """Claude AI로 크립토 뉴스 감성 분석 (채널 신뢰도 반영)."""
        analyzed = []
        for i in range(0, len(raw_articles), 10):
            batch = raw_articles[i : i + 10]
            try:
                ai_results = await self._analyze_batch(batch)
                for j, article in enumerate(batch):
                    ai = ai_results[j] if j < len(ai_results) else {}
                    source = article.get("source", "")
                    trust = channel_trust.get(source, 0.5)
                    raw_impact = float(ai.get("impact_score", 0.0))
                    # 채널 신뢰도 반영: impact_score에 신뢰도 가중치 적용
                    adjusted_impact = raw_impact * (0.5 + trust)
                    analyzed.append({
                        **article,
                        "sentiment": ai.get("sentiment", "neutral"),
                        "impact_score": round(max(-1.0, min(1.0, adjusted_impact)), 3),
                        "raw_impact_score": raw_impact,
                        "channel_trust": trust,
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
                        "raw_impact_score": 0.0,
                        "channel_trust": 0.5,
                        "related_coins": [],
                        "ai_summary": "",
                    })
        return analyzed

    async def _analyze_batch(self, articles: list[dict]) -> list[dict]:
        articles_text = "\n---\n".join(
            f"제목: {a['title']}\n내용: {a.get('content', '')[:400]}"
            for a in articles
        )
        system_instruction = """당신은 글로벌 암호화폐 시장 전문 분석가입니다.
다음 뉴스 기사들을 분석하여 JSON 배열로 응답하세요.
크립토 전문 뉴스뿐 아니라 미국 정치·경제 뉴스도 포함될 수 있습니다.

간접 영향 판단 기준:
- 미국 SEC/CFTC 규제 뉴스 → 크립토 시장 직접 영향
- 트럼프/의회 친크립토 정책 → BTC/ETH 등 강한 호재
- 미국 금리·CPI·경기 지표 → 위험자산(크립토) 방향성
- 지정학적 리스크(전쟁/제재) → 비트코인 안전자산 수요
- 기업 ETF/채택 뉴스 → 해당 코인 직접 호재
- 해킹/사기/거래소 문제 → 시장 악재

각 기사에 대해:
1. sentiment: "very_positive", "positive", "neutral", "negative", "very_negative"
2. impact_score: -1.0 ~ 1.0 (암호화폐 시장 전반에 미치는 영향)
3. related_coins: 영향받는 코인 심볼 배열 (예: ["BTC", "ETH", "SOL"]), 전체 시장이면 ["BTC"]
4. summary: 한 문장 한국어 요약 (크립토 시장 관점에서)

반드시 JSON 배열만 응답하세요."""
        response = await self._gemini.analyze(articles_text, system_instruction)
        return self._gemini._parse_json_response(response)

    # ─── AI 종목 선정 ──────────────────────────────────────

    async def _select_coins_with_ai(self, articles: list[dict]) -> None:
        """Claude AI가 뉴스 흐름 기반으로 투자 유망 종목 선정.

        선정 결과를 self._ai_selections에 캐시하고 DB에 저장.
        """
        try:
            ai_selection_cfg = self.config.get("ai_selection", {})
            max_picks = ai_selection_cfg.get("max_picks", 8)
            min_confidence = ai_selection_cfg.get("min_confidence", 0.6)

            # 긍정 뉴스 중심으로 요약 (상위 30개)
            positive_articles = sorted(
                [a for a in articles if a.get("impact_score", 0) > 0],
                key=lambda x: x.get("impact_score", 0),
                reverse=True,
            )[:30]

            if not positive_articles:
                logger.info("긍정 뉴스 없음 — AI 종목 선정 생략")
                return

            news_text = "\n---\n".join(
                f"출처: {a.get('source', '')} | 신뢰도: {a.get('channel_trust', 0.5):.1f}\n"
                f"제목: {a['title']}\n"
                f"요약: {a.get('ai_summary', a.get('content', '')[:200])}\n"
                f"관련코인: {', '.join(a.get('related_coins', []))}\n"
                f"영향도: {a.get('impact_score', 0):.2f}"
                for a in positive_articles
            )

            system_instruction = f"""당신은 글로벌 거시경제와 암호화폐 시장을 통합 분석하는 투자 전문가입니다.
현재 Fear & Greed Index: {self._fear_greed_index} ({self._fear_greed_label})

아래 뉴스들을 종합 분석하여 향후 24시간 내 상승 가능성이 가장 높은 코인을
최대 {max_picks}개 선정하세요.

분석 시 고려사항:
- 미국 정치(트럼프 정책, 친크립토 법안 등) → 시장 전체 방향성
- 미국 Fed 금리·경제지표 → 위험자산 매수/매도 심리
- SEC/CFTC 규제 결정 → 직접적 코인 영향
- 특정 코인 개발/파트너십/ETF 뉴스 → 해당 코인 개별 모멘텀
- 글로벌 지정학적 이벤트 → 비트코인 안전자산 수요

반드시 업비트 KRW 마켓에서 거래 가능한 코인(KRW-XXX 형식)만 선정하세요.
주요 코인 예: KRW-BTC, KRW-ETH, KRW-XRP, KRW-SOL, KRW-ADA, KRW-DOGE,
KRW-MATIC, KRW-DOT, KRW-LINK, KRW-AVAX, KRW-ATOM, KRW-NEAR, KRW-SUI

응답 형식 (JSON 배열만):
[
  {{
    "market": "KRW-BTC",
    "confidence": 0.85,
    "reason": "선정 이유 (한국어 2~3문장, 미국 정치·경제 영향 포함)",
    "news_summary": "관련 뉴스 핵심 요약"
  }}
]

신뢰도(confidence)가 {min_confidence} 미만인 코인은 제외하세요.
JSON 배열만 응답하세요."""

            response = await self._gemini.analyze(news_text, system_instruction)
            picks = self._gemini._parse_json_response(response)

            # 필터링 및 정규화
            valid_picks = []
            for p in picks:
                market = p.get("market", "").upper()
                confidence = float(p.get("confidence", 0))
                if not market.startswith("KRW-"):
                    market = f"KRW-{market}"
                if confidence >= min_confidence:
                    valid_picks.append({
                        "market": market,
                        "confidence": round(confidence, 3),
                        "reason": p.get("reason", ""),
                        "news_summary": p.get("news_summary", ""),
                        "fear_greed_index": self._fear_greed_index,
                        "selected_at": datetime.now().isoformat(),
                    })

            self._ai_selections = valid_picks
            logger.info(f"AI 종목 선정 완료: {[p['market'] for p in valid_picks]}")

            # DB 저장 + 선정 당시 가격은 master에서 채움
            await self._save_ai_selections(valid_picks)

        except Exception as e:
            logger.error(f"AI 종목 선정 오류: {e}")
            self._ai_selections = []

    async def _save_ai_selections(self, picks: list[dict]) -> None:
        try:
            db = await get_db()
            for p in picks:
                await db.execute(
                    """INSERT INTO ai_selections
                       (market, reason, confidence, news_summary, fear_greed_index, selected_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        p["market"],
                        p.get("reason", ""),
                        p.get("confidence", 0),
                        p.get("news_summary", ""),
                        p.get("fear_greed_index", 50),
                        p.get("selected_at", datetime.now().isoformat()),
                    ),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"AI 선정 DB 저장 오류: {e}")

    # ─── 채널 성과 평가 ────────────────────────────────────

    async def _init_channel_trust(self) -> None:
        """채널 신뢰도 테이블 초기화."""
        try:
            db = await get_db()
            for _, source in CRYPTO_RSS_FEEDS:
                await db.execute(
                    "INSERT OR IGNORE INTO channel_trust (channel) VALUES (?)",
                    (source,),
                )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.debug(f"채널 신뢰도 초기화 오류: {e}")

    async def _load_channel_trust(self) -> dict[str, float]:
        """채널별 신뢰도 로드."""
        try:
            db = await get_db()
            cursor = await db.execute("SELECT channel, trust_score FROM channel_trust")
            rows = await cursor.fetchall()
            await db.close()
            return {r[0]: float(r[1]) for r in rows}
        except Exception:
            return {}

    async def _evaluate_channel_performance(self) -> None:
        """24시간 지난 AI 선정 종목의 실제 성과를 평가하여 채널 신뢰도 갱신."""
        try:
            threshold = (datetime.now() - timedelta(hours=24)).isoformat()
            db = await get_db()
            cursor = await db.execute(
                """SELECT id, market, price_at_selection, selected_at
                   FROM ai_selections
                   WHERE result IS NULL
                     AND price_at_selection > 0
                     AND selected_at <= ?""",
                (threshold,),
            )
            rows = await cursor.fetchall()
            await db.close()

            if not rows:
                return

            # 현재가 조회를 위해 업비트 API 호출은 master가 담당
            # 여기서는 DB에서 price_after_24h가 채워진 것만 평가
            db = await get_db()
            cursor2 = await db.execute(
                """SELECT id, market, price_at_selection, price_after_24h
                   FROM ai_selections
                   WHERE result IS NULL AND price_after_24h IS NOT NULL"""
            )
            pending = await cursor2.fetchall()

            hit_count = 0
            for row in pending:
                sel_id, market, price_buy, price_now = row
                if price_buy and price_buy > 0 and price_now:
                    pnl_pct = (price_now / price_buy - 1) * 100
                    result = "hit" if pnl_pct > 0 else "miss"
                    await db.execute(
                        "UPDATE ai_selections SET result = ? WHERE id = ?",
                        (result, sel_id),
                    )
                    if result == "hit":
                        hit_count += 1

            if pending:
                await db.commit()
                logger.info(f"채널 성과 평가: {len(pending)}건 중 {hit_count}건 적중")

            await db.close()
        except Exception as e:
            logger.debug(f"채널 성과 평가 오류: {e}")

    # ─── 코인 점수 업데이트 ────────────────────────────────

    def _update_coin_scores(self, articles: list[dict]) -> None:
        coin_data: dict[str, list[float]] = {}
        for article in articles:
            impact = article.get("impact_score", 0.0)
            for coin in article.get("related_coins", []):
                coin = coin.upper()
                coin_data.setdefault(coin, []).append(impact)

        self._coin_news_scores = {}
        for coin, scores in coin_data.items():
            avg_impact = sum(scores) / len(scores)
            normalized = (avg_impact + 1) / 2
            count_weight = min(1 + len(scores) * 0.05, 1.3)
            self._coin_news_scores[coin] = min(normalized * count_weight, 1.0)

    # ─── DB 저장 ───────────────────────────────────────────

    async def _save_articles(self, articles: list[dict]) -> None:
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
