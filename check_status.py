"""시스템 상태 진단 스크립트.

실행: python check_status.py
"""

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import aiohttp
import feedparser

DB_PATH = "data/trading.db"

FEEDS = [
    ("https://www.coindesk.com/arc/outboundfeeds/rss/", "coindesk"),
    ("https://cointelegraph.com/rss", "cointelegraph"),
    ("https://decrypt.co/feed", "decrypt"),
    ("https://cryptonews.com/news/feed/", "cryptonews"),
    ("https://www.theblock.co/rss.xml", "theblock"),
    ("https://bitcoinmagazine.com/feed", "bitcoinmagazine"),
    ("https://beincrypto.com/feed/", "beincrypto"),
    ("https://www.cnbc.com/id/10001147/device/rss/rss.html", "cnbc"),
    ("https://feeds.reuters.com/reuters/businessNews", "reuters_business"),
    ("https://feeds.reuters.com/Reuters/worldNews", "reuters_world"),
    ("https://thehill.com/feed/", "thehill"),
    ("https://www.politico.com/rss/politics08.xml", "politico"),
    ("https://coindesk.co.kr/feed/", "coindesk_kr"),
    ("https://www.coindeskkorea.com/feed/", "coindesk_korea"),
]


async def check_feeds():
    print("\n" + "=" * 60)
    print("  📡 RSS 피드 접속 테스트")
    print("=" * 60)
    ok_count = 0
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as session:
        tasks = []
        for url, name in FEEDS:
            tasks.append(_check_one_feed(session, url, name))
        results = await asyncio.gather(*tasks)
    for ok, name, msg in results:
        icon = "✅" if ok else "❌"
        print(f"  {icon} [{name:<18}] {msg}")
        if ok:
            ok_count += 1
    print(f"\n  결과: {ok_count}/{len(FEEDS)}개 접속 가능")
    return ok_count


async def _check_one_feed(session, url, name):
    try:
        async with session.get(url) as r:
            if r.status == 200:
                text = await r.text()
                feed = feedparser.parse(text)
                count = len(feed.entries)
                latest = feed.entries[0].title[:35] if count else "(기사 없음)"
                return True, name, f"{count}건 | 최신: {latest}"
            return False, name, f"HTTP {r.status}"
    except asyncio.TimeoutError:
        return False, name, "타임아웃"
    except Exception as e:
        return False, name, f"{type(e).__name__}: {str(e)[:40]}"


def check_db():
    print("\n" + "=" * 60)
    print("  🗄️  데이터베이스 현황")
    print("=" * 60)
    if not Path(DB_PATH).exists():
        print("  ⚠️  DB 파일 없음 (아직 실행 안 됨)")
        return

    db = sqlite3.connect(DB_PATH)
    today = datetime.now().strftime("%Y-%m-%d")

    # 뉴스
    total = db.execute("SELECT COUNT(*) FROM crypto_news").fetchone()[0]
    today_n = db.execute(
        "SELECT COUNT(*) FROM crypto_news WHERE created_at >= ?", (today,)
    ).fetchone()[0]
    print(f"\n  📰 크립토 뉴스: 전체 {total}건 | 오늘 {today_n}건")

    if total > 0:
        rows = db.execute(
            "SELECT source, COUNT(*) c FROM crypto_news GROUP BY source ORDER BY c DESC"
        ).fetchall()
        for src, cnt in rows:
            print(f"       {src:<20} {cnt}건")

        print("\n  최근 수집 기사 5건:")
        rows = db.execute(
            "SELECT title, source, sentiment, impact_score, created_at "
            "FROM crypto_news ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        for r in rows:
            print(f"    [{r[1]}] {r[0][:45]}")
            print(f"          sentiment={r[2]} | impact={r[3]:.2f} | {r[4]}")

    # AI 선정 종목
    ai_total = db.execute("SELECT COUNT(*) FROM ai_selections").fetchone()[0]
    ai_today = db.execute(
        "SELECT COUNT(*) FROM ai_selections WHERE selected_at >= ?", (today,)
    ).fetchone()[0]
    print(f"\n  🤖 AI 선정 종목: 전체 {ai_total}건 | 오늘 {ai_today}건")
    if ai_today > 0:
        rows = db.execute(
            "SELECT market, confidence, reason, selected_at "
            "FROM ai_selections WHERE selected_at >= ? ORDER BY confidence DESC",
            (today,),
        ).fetchall()
        for r in rows:
            print(f"    {r[0]:<12} confidence={r[1]:.2f} | {r[2][:40]}")

    # 채널 신뢰도
    print("\n  📊 채널 신뢰도:")
    try:
        rows = db.execute(
            "SELECT channel, trust_score, total_predictions, correct_predictions "
            "FROM channel_trust ORDER BY trust_score DESC"
        ).fetchall()
        if rows:
            for r in rows:
                hit_rate = f"{r[3]}/{r[2]}" if r[2] > 0 else "0/0"
                print(f"    {r[0]:<20} trust={r[1]:.2f} | 적중 {hit_rate}")
        else:
            print("    (아직 없음)")
    except Exception:
        # 구버전 DB: total_predictions 컬럼 없을 수 있음 (재시작 후 자동 마이그레이션)
        rows = db.execute(
            "SELECT channel, trust_score FROM channel_trust ORDER BY trust_score DESC"
        ).fetchall()
        if rows:
            for r in rows:
                print(f"    {r[0]:<20} trust={r[1]:.2f}")
        print("    ⚠️  DB 재시작 후 컬럼 마이그레이션 필요 (python main.py 재실행)")

    # 주문
    order_total = db.execute("SELECT COUNT(*) FROM crypto_orders").fetchone()[0]
    order_today = db.execute(
        "SELECT COUNT(*) FROM crypto_orders WHERE created_at >= ?", (today,)
    ).fetchone()[0]
    print(f"\n  📋 주문: 전체 {order_total}건 | 오늘 {order_today}건")
    if order_today > 0:
        rows = db.execute(
            "SELECT market, side, price, status, created_at "
            "FROM crypto_orders ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        for r in rows:
            print(f"    {r[0]} {r[1]} | {r[2]:,.0f}원 | {r[3]} | {r[4]}")

    db.close()


def check_fear_greed():
    """Fear & Greed 빠른 확인."""
    import urllib.request, json as _json
    print("\n" + "=" * 60)
    print("  😱 Fear & Greed Index")
    print("=" * 60)
    try:
        with urllib.request.urlopen(
            "https://api.alternative.me/fng/?limit=1", timeout=8
        ) as r:
            data = _json.loads(r.read())["data"][0]
            print(f"  현재: {data['value']} ({data['value_classification']})")
    except Exception as e:
        print(f"  조회 실패: {e}")


if __name__ == "__main__":
    print("\n🔍 크립토 자동매매 시스템 상태 점검")
    print(f"   실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    check_db()
    check_fear_greed()
    asyncio.run(check_feeds())

    print("\n" + "=" * 60)
    print("  점검 완료")
    print("=" * 60 + "\n")
