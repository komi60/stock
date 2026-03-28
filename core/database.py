"""SQLite 비동기 데이터베이스 관리."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
from loguru import logger

DB_PATH = Path("data/trading.db")


async def get_db() -> aiosqlite.Connection:
    """DB 연결 반환."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    return db


async def init_db() -> None:
    """데이터베이스 테이블 초기화."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executescript("""
            -- 구버전 주식 트레이딩 테이블 제거 --
            DROP TABLE IF EXISTS news;
            DROP TABLE IF EXISTS rumors;
            DROP TABLE IF EXISTS policy_events;
            DROP TABLE IF EXISTS orders;
            DROP TABLE IF EXISTS positions;
            DROP TABLE IF EXISTS daily_reports;
            DROP TABLE IF EXISTS stock_candidates;

            -- AI 종목선정 및 채널 신뢰도 테이블 --

            CREATE TABLE IF NOT EXISTS ai_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                reason TEXT,
                confidence REAL DEFAULT 0,
                news_summary TEXT,
                fear_greed_index INTEGER DEFAULT 50,
                price_at_selection REAL DEFAULT 0,
                price_after_24h REAL,
                result TEXT,
                selected_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS channel_trust (
                channel TEXT PRIMARY KEY,
                trust_score REAL DEFAULT 0.5,
                total_predictions INTEGER DEFAULT 0,
                correct_predictions INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            -- 암호화폐 자동매매 테이블 --

            CREATE TABLE IF NOT EXISTS crypto_news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                source TEXT,
                url TEXT UNIQUE,
                sentiment TEXT,
                impact_score REAL DEFAULT 0,
                related_coins TEXT,
                ai_summary TEXT,
                published_at TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS crypto_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                signal TEXT NOT NULL,
                confidence REAL DEFAULT 0,
                rsi REAL,
                macd REAL,
                bb_position REAL,
                atr REAL,
                ema_trend TEXT,
                fear_greed_index INTEGER,
                news_score REAL DEFAULT 0,
                total_score REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS crypto_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                volume REAL,
                price REAL,
                ord_type TEXT DEFAULT 'limit',
                status TEXT DEFAULT 'pending',
                uuid TEXT,
                is_paper INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS crypto_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market TEXT UNIQUE NOT NULL,
                volume REAL NOT NULL,
                avg_price REAL NOT NULL,
                current_price REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                stop_loss_price REAL,
                take_profit_price REAL,
                is_paper INTEGER DEFAULT 1,
                opened_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)
        await db.commit()

        # channel_trust 컬럼 마이그레이션 (구버전 DB 호환)
        for col, definition in [
            ("total_predictions", "INTEGER DEFAULT 0"),
            ("correct_predictions", "INTEGER DEFAULT 0"),
        ]:
            try:
                await db.execute(
                    f"ALTER TABLE channel_trust ADD COLUMN {col} {definition}"
                )
                await db.commit()
                logger.info(f"channel_trust 컬럼 추가: {col}")
            except Exception:
                pass  # 이미 존재하면 무시

        logger.info("데이터베이스 초기화 완료")
