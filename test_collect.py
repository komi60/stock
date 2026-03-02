"""뉴스 + 루머 수집 테스트 (국내 + 해외).

Gemini AI 분석은 생략하고, 원시 데이터 수집 파이프라인만 확인.
"""

import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import aiohttp
import feedparser
from bs4 import BeautifulSoup
from loguru import logger

from core.database import init_db, get_db


logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>")


async def collect_naver_finance_news(session: aiohttp.ClientSession) -> list[dict]:
    """네이버 금융 메인 뉴스."""
    articles = []
    try:
        url = "https://finance.naver.com/news/mainnews.naver"
        async with session.get(url) as resp:
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
        logger.error(f"네이버 금융 수집 오류: {e}")
    return articles


async def collect_domestic_rss(session: aiohttp.ClientSession) -> list[dict]:
    """국내 RSS 피드 뉴스."""
    rss_feeds = [
        ("https://www.hankyung.com/feed/finance", "hankyung"),
        ("https://rss.etnews.com/Section902.xml", "etnews"),
        ("https://www.mk.co.kr/rss/30100041/", "mk"),
    ]
    articles = []
    for feed_url, source in rss_feeds:
        try:
            async with session.get(feed_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                content = await resp.text()

            feed = feedparser.parse(content)
            count = 0
            for entry in feed.entries[:15]:
                title = entry.get("title", "")
                if not title:
                    continue
                articles.append({
                    "title": title,
                    "content": BeautifulSoup(
                        entry.get("summary", ""), "html.parser"
                    ).get_text()[:500],
                    "source": source,
                    "url": entry.get("link", ""),
                    "published_at": entry.get("published", datetime.now().isoformat()),
                })
                count += 1
            logger.info(f"  국내 RSS [{source}] → {count}건")
        except Exception as e:
            logger.warning(f"  국내 RSS [{source}] 실패: {e}")
    return articles


async def collect_global_rss(session: aiohttp.ClientSession) -> list[dict]:
    """해외 글로벌 뉴스 RSS 수집."""
    global_feeds = [
        # ── 미국 주요 매체 ──
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10001147", "cnbc_world"),
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135", "cnbc_asia"),
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664", "cnbc_tech"),
        ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910", "cnbc_earnings"),
        # MarketWatch
        ("https://feeds.marketwatch.com/marketwatch/topstories/", "marketwatch"),
        ("https://feeds.marketwatch.com/marketwatch/marketpulse/", "marketwatch_pulse"),
        # Yahoo Finance
        ("https://finance.yahoo.com/news/rssindex", "yahoo_finance"),
        # Reuters
        ("https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best", "reuters"),
        # Investing.com
        ("https://www.investing.com/rss/news.rss", "investing_com"),
        # ── 아시아 매체 ──
        ("https://asia.nikkei.com/rss", "nikkei_asia"),
        # ── 기술/반도체 전문 ──
        ("https://seekingalpha.com/market_currents.xml", "seeking_alpha"),
        # Financial Times
        ("https://www.ft.com/rss/home", "ft"),
    ]
    articles = []
    for feed_url, source in global_feeds:
        try:
            async with session.get(
                feed_url, timeout=aiohttp.ClientTimeout(total=12)
            ) as resp:
                if resp.status != 200:
                    logger.debug(f"  해외 RSS [{source}] HTTP {resp.status}")
                    continue
                content = await resp.text()

            feed = feedparser.parse(content)
            count = 0
            for entry in feed.entries[:10]:
                title = entry.get("title", "").strip()
                if not title:
                    continue
                summary_raw = entry.get("summary", entry.get("description", ""))
                summary = BeautifulSoup(summary_raw, "html.parser").get_text()[:500]

                articles.append({
                    "title": title,
                    "content": summary,
                    "source": source,
                    "url": entry.get("link", ""),
                    "published_at": entry.get("published", datetime.now().isoformat()),
                })
                count += 1
            if count > 0:
                logger.info(f"  해외 RSS [{source}] → {count}건")
        except Exception as e:
            logger.debug(f"  해외 RSS [{source}] 실패: {e}")
    return articles


async def collect_naver_stock_forum(session: aiohttp.ClientSession) -> list[dict]:
    """네이버 종목 토론방."""
    rumors = []
    popular = [
        ("005930", "삼성전자"), ("000660", "SK하이닉스"),
        ("373220", "LG에너지솔루션"), ("035420", "NAVER"), ("006400", "삼성SDI"),
    ]
    for ticker, name in popular:
        try:
            url = f"https://finance.naver.com/item/board.naver?code={ticker}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                html = await resp.text()

            soup = BeautifulSoup(html, "html.parser")
            rows = soup.select("table.type2 tbody tr")
            count = 0
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
                    "ticker": ticker,
                    "ticker_name": name,
                })
                count += 1
            logger.info(f"  토론방 [{name}] → {count}건")
        except Exception as e:
            logger.warning(f"  토론방 [{name}] 실패: {e}")
    return rumors


async def save_to_db(news: list[dict], rumors: list[dict]) -> None:
    """수집 데이터를 DB에 저장."""
    db = await get_db()

    saved_news = 0
    for article in news:
        try:
            await db.execute(
                """INSERT OR IGNORE INTO news
                   (title, content, source, url, published_at, sentiment, impact_score, ai_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    article["title"], article.get("content", ""),
                    article["source"], article.get("url", ""),
                    article.get("published_at", datetime.now().isoformat()),
                    "pending", 0.0, "(AI 분석 대기중)",
                ),
            )
            saved_news += 1
        except Exception:
            pass

    saved_rumors = 0
    for rumor in rumors:
        try:
            await db.execute(
                """INSERT INTO rumors
                   (content, source_channel, channel_trust_score, sentiment, impact_score, related_tickers)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    rumor["content"], rumor["source_channel"],
                    0.3, "pending", 0.0, rumor.get("ticker", ""),
                ),
            )
            saved_rumors += 1
        except Exception:
            pass

    await db.commit()
    await db.close()
    logger.info(f"DB 저장: 뉴스 {saved_news}건, 루머 {saved_rumors}건")


async def main():
    logger.info("=" * 60)
    logger.info("  뉴스 + 루머 수집 테스트 (국내 + 해외)")
    logger.info("=" * 60)

    await init_db()

    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    ) as session:

        # ── 1. 국내 뉴스 ──
        logger.info("\n📰 [1/4] 네이버 금융 뉴스")
        naver_news = await collect_naver_finance_news(session)
        logger.info(f"  → {len(naver_news)}건")

        logger.info("\n📡 [2/4] 국내 RSS 피드")
        domestic_rss = await collect_domestic_rss(session)
        logger.info(f"  → 소계 {len(domestic_rss)}건")

        # ── 2. 해외 뉴스 ──
        logger.info("\n🌍 [3/4] 해외 글로벌 뉴스 RSS")
        global_rss = await collect_global_rss(session)
        logger.info(f"  → 소계 {len(global_rss)}건")

        all_news = naver_news + domestic_rss + global_rss

        # ── 3. 루머 ──
        logger.info("\n💬 [4/4] 네이버 종목 토론방")
        rumors = await collect_naver_stock_forum(session)
        logger.info(f"  → {len(rumors)}건")

    # ── 4. DB 저장 ──
    logger.info("\n💾 DB 저장 중...")
    await save_to_db(all_news, rumors)

    # ── 5. 결과 ──
    logger.info("\n" + "=" * 60)
    kr_count = len(naver_news) + len(domestic_rss)
    gl_count = len(global_rss)
    logger.info(f"  국내 뉴스: {kr_count}건 | 해외 뉴스: {gl_count}건 | 루머: {len(rumors)}건")
    logger.info(f"  합계: {len(all_news) + len(rumors)}건")
    logger.info("=" * 60)

    if global_rss:
        logger.info(f"\n🌍 해외 뉴스 샘플 (최근 15건):")
        for i, n in enumerate(global_rss[:15], 1):
            logger.info(f"  {i:2d}. [{n['source']:18s}] {n['title'][:70]}")

    logger.info("\n✅ 수집 완료!")


if __name__ == "__main__":
    asyncio.run(main())
