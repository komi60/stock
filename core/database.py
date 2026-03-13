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
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                source TEXT,
                url TEXT UNIQUE,
                published_at TEXT,
                sentiment TEXT,
                impact_score REAL DEFAULT 0,
                related_tickers TEXT,
                ai_summary TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS rumors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source_channel TEXT,
                channel_trust_score REAL DEFAULT 0.5,
                sentiment TEXT,
                impact_score REAL DEFAULT 0,
                related_tickers TEXT,
                verified INTEGER,
                collected_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS policy_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT,
                source TEXT,
                event_type TEXT,
                published_at TEXT,
                impact_score REAL DEFAULT 0,
                beneficiary_sectors TEXT,
                affected_tickers TEXT,
                ai_analysis TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT DEFAULT 'limit',
                quantity INTEGER NOT NULL,
                price REAL,
                status TEXT DEFAULT 'pending',
                filled_quantity INTEGER DEFAULT 0,
                filled_price REAL,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                name TEXT,
                quantity INTEGER NOT NULL,
                avg_price REAL NOT NULL,
                current_price REAL DEFAULT 0,
                opened_at TEXT DEFAULT (datetime('now', 'localtime')),
                closed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT UNIQUE NOT NULL,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_pnl_pct REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                module_reports TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS channel_trust (
                channel TEXT PRIMARY KEY,
                trust_score REAL DEFAULT 0.5,
                total_rumors INTEGER DEFAULT 0,
                accurate_rumors INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS stock_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                name TEXT,
                total_score REAL DEFAULT 0,
                news_score REAL DEFAULT 0,
                sentiment_score REAL DEFAULT 0,
                policy_score REAL DEFAULT 0,
                technical_score REAL DEFAULT 0,
                prediction_score REAL DEFAULT 0,
                signal TEXT,
                reasons TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
        """)
        await db.commit()
        logger.info("데이터베이스 초기화 완료")
