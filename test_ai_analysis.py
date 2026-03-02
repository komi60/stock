"""수집된 뉴스/루머에 Gemini AI 분석을 실행."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import aiosqlite
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | <level>{message}</level>")


async def main():
    # 암호화 저장소에서 키 로드
    from core.security import SecureVault
    vault = SecureVault()
    vault.export_to_env()

    from core.config import GeminiConfig
    from ai.gemini_client import GeminiClient

    config = GeminiConfig()
    gemini = GeminiClient(config)
    await gemini.initialize()

    db = await aiosqlite.connect("data/trading.db")

    # ── 1. 뉴스 AI 분석 ──
    logger.info("=" * 60)
    logger.info("  뉴스 AI 감정 분석 시작")
    logger.info("=" * 60)

    cursor = await db.execute(
        "SELECT id, title, content, source FROM news WHERE sentiment = 'pending' LIMIT 20"
    )
    pending_news = await cursor.fetchall()
    logger.info(f"분석 대기 뉴스: {len(pending_news)}건")

    # 10개씩 배치 분석
    for i in range(0, len(pending_news), 10):
        batch = pending_news[i:i+10]
        articles = [{"title": row[1], "content": row[2] or ""} for row in batch]

        logger.info(f"\n배치 {i//10 + 1}: {len(batch)}건 분석 중...")
        results = await gemini.analyze_news_sentiment(articles)

        if results:
            for j, row in enumerate(batch):
                if j < len(results):
                    r = results[j]
                    sentiment = r.get("sentiment", "neutral")
                    impact = r.get("impact_score", 0)
                    tickers = ",".join(r.get("related_tickers", []))
                    summary = r.get("summary", "")

                    await db.execute(
                        "UPDATE news SET sentiment=?, impact_score=?, related_tickers=?, ai_summary=? WHERE id=?",
                        (sentiment, impact, tickers, summary, row[0]),
                    )
                    logger.info(
                        f"  {row[1][:40]:40s} → {sentiment:16s} | 영향:{impact:+.2f} | 종목:{tickers or '-'}"
                    )
        await asyncio.sleep(1)  # API 레이트 리밋

    await db.commit()

    # ── 2. 루머 AI 분석 ──
    logger.info("\n" + "=" * 60)
    logger.info("  루머 AI 분석 시작")
    logger.info("=" * 60)

    cursor = await db.execute(
        "SELECT id, content, source_channel, related_tickers FROM rumors WHERE sentiment = 'pending' LIMIT 15"
    )
    pending_rumors = await cursor.fetchall()
    logger.info(f"분석 대기 루머: {len(pending_rumors)}건")

    ticker_names = {
        "005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
        "035420": "NAVER", "006400": "삼성SDI",
    }

    for row in pending_rumors:
        rumor_id, content, channel, tickers = row
        name = ticker_names.get(tickers, tickers)

        try:
            result = await gemini.analyze_rumor(content, channel)
            sentiment = result.get("sentiment", "neutral")
            impact = result.get("impact_score", 0)
            credibility = result.get("credibility", 0.5)

            await db.execute(
                "UPDATE rumors SET sentiment=?, impact_score=?, channel_trust_score=? WHERE id=?",
                (sentiment, impact * credibility, credibility, rumor_id),
            )
            logger.info(
                f"  [{name:8s}] {content[:35]:35s} → {sentiment:16s} | 신뢰:{credibility:.2f} | 영향:{impact:+.2f}"
            )
        except Exception as e:
            logger.warning(f"  [{name}] 분석 실패: {e}")

        await asyncio.sleep(0.8)

    await db.commit()

    # ── 3. 결과 요약 ──
    logger.info("\n" + "=" * 60)
    logger.info("  분석 결과 요약")
    logger.info("=" * 60)

    # 감정별 뉴스 통계
    cursor = await db.execute(
        "SELECT sentiment, COUNT(*) FROM news WHERE sentiment != 'pending' GROUP BY sentiment ORDER BY COUNT(*) DESC"
    )
    sentiments = await cursor.fetchall()
    logger.info("\n📰 뉴스 감정 분포:")
    for s in sentiments:
        bar = "█" * s[1]
        logger.info(f"  {s[0]:16s} {s[1]:3d}건 {bar}")

    # 영향력 높은 뉴스 Top 5
    cursor = await db.execute(
        "SELECT title, sentiment, impact_score, related_tickers, ai_summary FROM news "
        "WHERE sentiment != 'pending' ORDER BY ABS(impact_score) DESC LIMIT 5"
    )
    top_news = await cursor.fetchall()
    logger.info("\n🔥 영향력 높은 뉴스 Top 5:")
    for i, n in enumerate(top_news, 1):
        logger.info(f"  {i}. [{n[1]:16s} | 영향:{n[2]:+.2f}] {n[0][:55]}")
        if n[4]:
            logger.info(f"     요약: {n[4][:80]}")
        if n[3]:
            logger.info(f"     관련: {n[3]}")

    # 루머 분석 결과
    cursor = await db.execute(
        "SELECT content, sentiment, impact_score, channel_trust_score, related_tickers "
        "FROM rumors WHERE sentiment != 'pending' ORDER BY ABS(impact_score) DESC LIMIT 5"
    )
    top_rumors = await cursor.fetchall()
    if top_rumors:
        logger.info("\n💬 주요 루머 분석 Top 5:")
        for i, r in enumerate(top_rumors, 1):
            name = ticker_names.get(r[4], r[4])
            logger.info(f"  {i}. [{name:8s}] {r[0][:40]:40s} → {r[1]:16s} | 신뢰:{r[3]:.2f} | 영향:{r[2]:+.3f}")

    await db.close()
    logger.info("\n✅ AI 분석 완료! 대시보드 새로고침하면 결과 반영됩니다.")


if __name__ == "__main__":
    asyncio.run(main())
