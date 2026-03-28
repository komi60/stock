"""모듈 7: FastAPI 기반 모니터링 대시보드.

- 외부 IP: Read-Only (수익률, 보유 종목, 모듈 상태 모니터링)
- 로컬 IP (127.0.0.1): Read/Write (설정 변경, 수동 매매, API 키 관리)
- API 키는 Fernet(AES) 암호화하여 로컬 파일에만 저장
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from loguru import logger

from core.base_module import PluginRegistry
from core.database import get_db
from core.scheduler import TradingScheduler
from core.security import SecureVault


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  헬퍼 & 모델
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def is_local(request: Request) -> bool:
    client_host = request.client.host if request.client else ""
    return client_host in ("127.0.0.1", "::1", "localhost", "testclient")


def require_local(request: Request) -> None:
    if not is_local(request):
        raise HTTPException(status_code=403, detail="이 작업은 로컬에서만 가능합니다.")


class ManualOrderRequest(BaseModel):
    ticker: str
    side: str
    quantity: int
    price: float | None = None
    order_type: str = "limit"


class ApiKeysRequest(BaseModel):
    UPBIT_ACCESS_KEY: str = ""
    UPBIT_SECRET_KEY: str = ""
    GEMINI_API_KEY: str = ""
    CLAUDE_API_KEY: str = ""
    GMAIL_ADDRESS: str = ""
    GMAIL_APP_PASSWORD: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FastAPI 앱 생성
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def create_dashboard(
    registry: PluginRegistry,
    scheduler: TradingScheduler,
    master_module: Any,
    config: dict[str, Any],
) -> FastAPI:
    app = FastAPI(title="KR Stock AutoTrader Dashboard", version="2.0.0")
    vault = SecureVault()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ─── 페이지 라우트 ─────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        local = is_local(request)
        if local and not vault.has_keys():
            return _render_setup_html()
        return _render_dashboard_html(local, vault.has_keys())

    @app.get("/setup", response_class=HTMLResponse, dependencies=[Depends(require_local)])
    async def setup_page():
        return _render_setup_html()

    # ─── API 키 관리 (로컬 전용) ───────────────────────────

    @app.get("/api/keys/status", dependencies=[Depends(require_local)])
    async def keys_status():
        return {"configured": vault.has_keys(), "keys": vault.get_status()}

    @app.post("/api/keys/save", dependencies=[Depends(require_local)])
    async def save_keys(keys: ApiKeysRequest):
        data = keys.model_dump()
        for k, v in data.items():
            if v:
                vault.set(k, v)
        vault.save()
        vault.export_to_env()
        logger.info("API 키 암호화 저장 완료")
        return {"status": "saved", "configured": vault.has_keys()}

    # ─── READ-ONLY 엔드포인트 ──────────────────────────────

    @app.get("/api/status")
    async def system_status():
        modules_status = {}
        for name, module in registry.get_all().items():
            report = module.get_status()
            modules_status[name] = {
                "status": report.status.value,
                "last_execution": report.last_execution.isoformat() if report.last_execution else None,
                "execution_count": report.execution_count,
                "error_count": report.error_count,
                "last_error": report.last_error,
            }
        return {
            "system": "running",
            "timestamp": datetime.now().isoformat(),
            "market_open": scheduler.is_market_open(),
            "keys_configured": vault.has_keys(),
            "modules": modules_status,
            "scheduled_jobs": scheduler.get_jobs(),
        }

    @app.get("/api/portfolio")
    async def portfolio():
        if master_module:
            return master_module.get_portfolio_summary()
        return {"positions": [], "total_value": 0, "total_pnl": 0, "total_pnl_pct": 0}

    @app.get("/api/candidates")
    async def candidates():
        try:
            db = await get_db()
            today = datetime.now().strftime("%Y-%m-%d")
            cursor = await db.execute(
                "SELECT * FROM stock_candidates WHERE date = ? ORDER BY total_score DESC", (today,))
            rows = await cursor.fetchall()
            await db.close()
            result = [{"ticker": r[2], "name": r[3], "total_score": r[4], "news_score": r[5],
                        "sentiment_score": r[6], "policy_score": r[7], "technical_score": r[8], "signal": r[9]}
                       for r in rows]
            return {"date": today, "candidates": result}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/orders")
    async def recent_orders():
        try:
            db = await get_db()
            cursor = await db.execute("SELECT * FROM orders ORDER BY created_at DESC LIMIT 50")
            rows = await cursor.fetchall()
            await db.close()
            orders = [{"id": r[0], "order_id": r[1], "ticker": r[2], "side": r[3], "order_type": r[4],
                        "quantity": r[5], "price": r[6], "status": r[7], "filled_quantity": r[8],
                        "filled_price": r[9], "created_at": r[10]} for r in rows]
            return {"orders": orders}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/performance")
    async def performance_history():
        try:
            db = await get_db()
            cursor = await db.execute("SELECT * FROM daily_reports ORDER BY date DESC LIMIT 30")
            rows = await cursor.fetchall()
            await db.close()
            reports = [{"date": r[1], "total_trades": r[2], "winning_trades": r[3], "losing_trades": r[4],
                         "total_pnl": r[5], "total_pnl_pct": r[6], "max_drawdown_pct": r[7]} for r in rows]
            return {"reports": reports}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/news")
    async def recent_news():
        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT title, source, sentiment, impact_score, ai_summary, created_at "
                "FROM news ORDER BY created_at DESC LIMIT 50")
            rows = await cursor.fetchall()
            await db.close()
            news = [{"title": r[0], "source": r[1], "sentiment": r[2], "impact_score": r[3],
                      "summary": r[4], "time": r[5]} for r in rows]
            return {"news": news}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/rumors")
    async def recent_rumors():
        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT content, source_channel, channel_trust_score, sentiment, "
                "impact_score, related_tickers, verified, collected_at "
                "FROM rumors ORDER BY collected_at DESC LIMIT 30")
            rows = await cursor.fetchall()
            await db.close()
            rumors = [{"content": r[0], "source_channel": r[1], "trust_score": r[2],
                        "sentiment": r[3], "impact_score": r[4], "related_tickers": r[5],
                        "verified": r[6], "time": r[7]} for r in rows]
            return {"rumors": rumors}
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/stats")
    async def dashboard_stats():
        try:
            db = await get_db()
            today = datetime.now().strftime("%Y-%m-%d")
            c1 = await db.execute("SELECT COUNT(*) FROM news WHERE created_at >= ?", (today,))
            news_today = (await c1.fetchone())[0]
            c2 = await db.execute("SELECT COUNT(*) FROM news")
            news_total = (await c2.fetchone())[0]
            c3 = await db.execute(
                "SELECT sentiment, COUNT(*) FROM news WHERE sentiment != 'pending' GROUP BY sentiment")
            sentiment_dist = {r[0]: r[1] for r in await c3.fetchall()}
            c4 = await db.execute("SELECT COUNT(*) FROM rumors WHERE collected_at >= ?", (today,))
            rumors_today = (await c4.fetchone())[0]
            c5 = await db.execute("SELECT COUNT(*) FROM rumors")
            rumors_total = (await c5.fetchone())[0]
            c6 = await db.execute("SELECT COUNT(*) FROM orders WHERE created_at >= ?", (today,))
            orders_today = (await c6.fetchone())[0]
            c7 = await db.execute(
                "SELECT COUNT(*) FROM stock_candidates WHERE date = ?", (today,))
            candidates_today = (await c7.fetchone())[0]
            await db.close()
            return {
                "news_today": news_today, "news_total": news_total,
                "rumors_today": rumors_today, "rumors_total": rumors_total,
                "orders_today": orders_today, "candidates_today": candidates_today,
                "sentiment_distribution": sentiment_dist,
            }
        except Exception as e:
            return {"error": str(e)}

    @app.get("/api/modules/{module_name}")
    async def module_detail(module_name: str):
        module = registry.get(module_name)
        if not module:
            raise HTTPException(404, f"모듈 '{module_name}'을 찾을 수 없습니다.")
        return module.get_status().model_dump()

    # ─── WRITE 엔드포인트 (로컬 전용) ─────────────────────

    @app.post("/api/manual-order", dependencies=[Depends(require_local)])
    async def manual_order(order_req: ManualOrderRequest):
        if not master_module:
            raise HTTPException(500, "마스터 모듈이 초기화되지 않았습니다.")
        from core.data_models import Order, OrderSide, OrderType
        try:
            order = Order(ticker=order_req.ticker, side=OrderSide(order_req.side),
                          order_type=OrderType(order_req.order_type),
                          quantity=order_req.quantity, price=order_req.price)
            if order.side == OrderSide.BUY:
                from core.data_models import StockCandidate, SignalStrength, TechnicalSignal
                candidate = StockCandidate(ticker=order.ticker, name="수동매매")
                signal = TechnicalSignal(ticker=order.ticker, signal=SignalStrength.BUY, entry_price=order.price)
                order_id = await master_module._execute_buy(candidate, signal)
            else:
                from core.data_models import Position
                pos = Position(ticker=order.ticker, name="수동매매",
                               quantity=order.quantity, avg_price=order.price or 0)
                order_id = await master_module._execute_sell(pos, "수동 매도")
            return {"status": "submitted", "order_id": order_id}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/settings", dependencies=[Depends(require_local)])
    async def update_settings(new_settings: dict):
        logger.info(f"설정 변경 요청: {new_settings}")
        return {"status": "updated", "settings": new_settings}

    @app.post("/api/module/{module_name}/restart", dependencies=[Depends(require_local)])
    async def restart_module(module_name: str):
        module = registry.get(module_name)
        if not module:
            raise HTTPException(404, f"모듈 '{module_name}'을 찾을 수 없습니다.")
        try:
            await module.shutdown()
            await module.initialize()
            return {"status": "restarted", "module": module_name}
        except Exception as e:
            raise HTTPException(500, str(e))

    # ─── 암호화폐 API 엔드포인트 ──────────────────────────

    @app.get("/api/crypto/portfolio")
    async def crypto_portfolio():
        """크립토 포트폴리오 요약."""
        crypto_master = registry.get("crypto_master")
        if crypto_master and hasattr(crypto_master, "get_portfolio_summary"):
            return crypto_master.get_portfolio_summary()
        return {
            "positions": [],
            "total_positions": 0,
            "total_value": 0,
            "total_pnl_pct": 0,
            "is_paper": True,
            "message": "크립토 모듈이 비활성화 상태입니다.",
        }

    @app.get("/api/crypto/signals")
    async def crypto_signals():
        """최근 크립토 매매 신호."""
        try:
            db = await get_db()
            cursor = await db.execute(
                """SELECT market, signal, confidence, rsi, macd, bb_position,
                          fear_greed_index, total_score, created_at
                   FROM crypto_signals
                   ORDER BY created_at DESC LIMIT 50"""
            )
            rows = await cursor.fetchall()
            await db.close()
            return {
                "signals": [
                    {
                        "market": r[0], "signal": r[1], "confidence": r[2],
                        "rsi": r[3], "macd": r[4], "bb_position": r[5],
                        "fear_greed_index": r[6], "total_score": r[7],
                        "created_at": r[8],
                    }
                    for r in rows
                ]
            }
        except Exception as e:
            return {"error": str(e), "signals": []}

    @app.get("/api/crypto/orders")
    async def crypto_orders():
        """최근 크립토 주문 내역."""
        try:
            db = await get_db()
            cursor = await db.execute(
                """SELECT market, side, volume, price, ord_type, status,
                          uuid, is_paper, created_at
                   FROM crypto_orders
                   ORDER BY created_at DESC LIMIT 50"""
            )
            rows = await cursor.fetchall()
            await db.close()
            return {
                "orders": [
                    {
                        "market": r[0], "side": r[1], "volume": r[2],
                        "price": r[3], "ord_type": r[4], "status": r[5],
                        "uuid": r[6], "is_paper": bool(r[7]), "created_at": r[8],
                    }
                    for r in rows
                ]
            }
        except Exception as e:
            return {"error": str(e), "orders": []}

    @app.get("/api/crypto/news")
    async def crypto_news_feed():
        """최근 크립토 뉴스."""
        try:
            db = await get_db()
            cursor = await db.execute(
                """SELECT title, source, sentiment, impact_score,
                          related_coins, ai_summary, created_at
                   FROM crypto_news
                   ORDER BY created_at DESC LIMIT 50"""
            )
            rows = await cursor.fetchall()
            await db.close()
            return {
                "news": [
                    {
                        "title": r[0], "source": r[1], "sentiment": r[2],
                        "impact_score": r[3], "related_coins": r[4],
                        "summary": r[5], "time": r[6],
                    }
                    for r in rows
                ]
            }
        except Exception as e:
            return {"error": str(e), "news": []}

    @app.get("/api/crypto/fear-greed")
    async def crypto_fear_greed():
        """현재 Fear & Greed Index."""
        crypto_news_module = registry.get("crypto_news")
        if crypto_news_module and hasattr(crypto_news_module, "get_fear_greed_index"):
            index = crypto_news_module.get_fear_greed_index()
            label_map = {
                (0, 25): "Extreme Fear",
                (25, 45): "Fear",
                (45, 55): "Neutral",
                (55, 75): "Greed",
                (75, 101): "Extreme Greed",
            }
            label = next(
                (v for (lo, hi), v in label_map.items() if lo <= index < hi),
                "Neutral"
            )
            return {"value": index, "label": label}
        return {"value": 50, "label": "Neutral", "message": "크립토 뉴스 모듈 비활성"}

    @app.post("/api/crypto/keys", dependencies=[Depends(require_local)])
    async def save_upbit_keys(keys: dict):
        """업비트 API 키 저장 (로컬 전용)."""
        access_key = keys.get("UPBIT_ACCESS_KEY", "")
        secret_key = keys.get("UPBIT_SECRET_KEY", "")
        if access_key:
            vault.set("UPBIT_ACCESS_KEY", access_key)
        if secret_key:
            vault.set("UPBIT_SECRET_KEY", secret_key)
        vault.save()
        vault.export_to_env()
        logger.info("업비트 API 키 저장 완료")
        return {"status": "saved"}

    return app


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  CSS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_COMMON_STYLE = """
<style>
:root {
    --bg-primary:#0a0e1a; --bg-card:rgba(17,24,39,0.7); --bg-card-solid:#111827;
    --border:#1e293b; --border-glass:rgba(255,255,255,0.06);
    --text:#e0e0e0; --text-sub:#94a3b8; --text-muted:#64748b;
    --blue:#3b82f6; --green:#22c55e; --red:#ef4444; --amber:#f59e0b; --purple:#a855f7;
    --glass-blur:12px;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg-primary);color:var(--text);min-height:100vh;}
.header{background:linear-gradient(135deg,#0f1628,#1a2342);padding:16px 28px;display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid var(--blue);position:sticky;top:0;z-index:100;backdrop-filter:blur(20px);}
.header h1{color:var(--blue);font-size:1.35em;display:flex;align-items:center;gap:10px;}
.nav{display:flex;gap:8px;}
.nav a{color:var(--text-sub);text-decoration:none;padding:6px 14px;border-radius:8px;font-size:0.85em;transition:all 0.2s;}
.nav a:hover,.nav a.active{background:rgba(59,130,246,0.12);color:var(--blue);}
.container{max-width:1440px;margin:0 auto;padding:20px;}
input,select,button,textarea{padding:10px 14px;border-radius:8px;border:1px solid var(--border);background:var(--bg-primary);color:var(--text);font-size:0.9em;outline:none;transition:border-color 0.2s;}
input:focus,select:focus,textarea:focus{border-color:var(--blue);}
button{background:linear-gradient(135deg,var(--blue),#2563eb);border:none;cursor:pointer;font-weight:700;color:white;transition:transform 0.1s,box-shadow 0.2s;}
button:hover{transform:translateY(-1px);box-shadow:0 4px 16px rgba(59,130,246,0.3);}
.access-mode{font-size:0.78em;padding:4px 12px;border-radius:12px;}
.access-mode.local{background:rgba(34,197,94,0.12);color:var(--green);}
.access-mode.remote{background:rgba(245,158,11,0.12);color:var(--amber);}
</style>
"""

_DASHBOARD_CSS = """
/* ─── Stats Ribbon ─── */
.stats-ribbon{display:flex;justify-content:center;gap:32px;padding:14px 20px;background:linear-gradient(135deg,rgba(15,22,40,0.9),rgba(26,35,66,0.9));border-bottom:1px solid var(--border);}
.stat-item{text-align:center;}
.stat-value{display:block;font-size:1.5em;font-weight:800;color:var(--text);font-variant-numeric:tabular-nums;}
.stat-label{font-size:0.72em;color:var(--text-muted);text-transform:uppercase;letter-spacing:0.5px;margin-top:2px;}

/* ─── Grid ─── */
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;margin-top:18px;}
@media(max-width:1200px){.grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:768px){.grid{grid-template-columns:1fr;}.header{flex-direction:column;gap:10px;}.stats-ribbon{flex-wrap:wrap;gap:16px;}}
.span-2{grid-column:span 2;}
@media(max-width:1200px){.span-2{grid-column:span 1;}}

/* ─── Cards ─── */
.card{background:var(--bg-card);backdrop-filter:blur(var(--glass-blur));-webkit-backdrop-filter:blur(var(--glass-blur));border:1px solid var(--border-glass);border-radius:16px;padding:22px;box-shadow:0 4px 24px rgba(0,0,0,0.2);transition:transform 0.2s,box-shadow 0.2s;}
.card:hover{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,0.35);}
.card h2{color:var(--blue);font-size:1.05em;margin-bottom:14px;display:flex;align-items:center;gap:8px;font-weight:700;}
.card h2 svg{width:20px;height:20px;flex-shrink:0;}

/* ─── Tables ─── */
table{width:100%;border-collapse:collapse;font-size:0.84em;}
th{text-align:left;padding:8px 6px;color:var(--text-muted);border-bottom:1px solid var(--border);font-weight:600;font-size:0.85em;white-space:nowrap;}
td{padding:8px 6px;border-bottom:1px solid rgba(30,41,59,0.5);}
tr:hover{background:rgba(30,41,59,0.3);}

/* ─── Badges ─── */
.badge{display:inline-block;padding:2px 10px;border-radius:6px;font-size:0.75em;font-weight:700;}
.badge.running{background:rgba(34,197,94,0.12);color:var(--green);}
.badge.idle{background:rgba(59,130,246,0.12);color:var(--blue);}
.badge.error{background:rgba(239,68,68,0.12);color:var(--red);}
.badge.stopped{background:rgba(100,116,139,0.12);color:var(--text-muted);}
.sentiment{display:inline-block;padding:2px 10px;border-radius:6px;font-size:0.75em;font-weight:700;}
.sentiment.very_positive{background:rgba(34,197,94,0.15);color:#22c55e;}
.sentiment.positive{background:rgba(74,222,128,0.12);color:#4ade80;}
.sentiment.neutral{background:rgba(148,163,184,0.12);color:#94a3b8;}
.sentiment.negative{background:rgba(251,146,60,0.12);color:#fb923c;}
.sentiment.very_negative{background:rgba(239,68,68,0.12);color:#ef4444;}
.sentiment.pending{background:rgba(100,116,139,0.08);color:#475569;}

/* ─── Impact Bar ─── */
.impact-bar{width:54px;height:5px;background:rgba(30,41,59,0.8);border-radius:3px;overflow:hidden;display:inline-block;vertical-align:middle;margin-right:6px;}
.impact-fill{height:100%;border-radius:3px;transition:width 0.5s ease;}

/* ─── Score Bar ─── */
.score-wrap{display:flex;align-items:center;gap:4px;}
.score-bar{flex:1;height:6px;background:rgba(30,41,59,0.8);border-radius:3px;overflow:hidden;}
.score-fill{height:100%;border-radius:3px;transition:width 0.5s ease;}
.score-val{font-size:0.78em;min-width:26px;text-align:right;color:var(--text-muted);}

/* ─── Metrics ─── */
.big-number{font-size:1.9em;font-weight:800;letter-spacing:-1px;font-variant-numeric:tabular-nums;}
.metric{text-align:center;padding:10px;}
.metric label{display:block;color:var(--text-muted);font-size:0.78em;margin-top:4px;}
.positive{color:var(--green);}.negative{color:var(--red);}

/* ─── Clock ─── */
.clock-row{display:flex;align-items:center;gap:16px;flex-wrap:wrap;}
.clock-time{font-size:2.2em;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-1px;color:var(--text);}
.clock-date{font-size:0.88em;color:var(--text-sub);}
.mkt-cd{font-size:0.82em;padding:5px 14px;border-radius:8px;font-weight:600;}
.mkt-cd.open{background:rgba(34,197,94,0.1);color:var(--green);}
.mkt-cd.closed{background:rgba(239,68,68,0.08);color:var(--red);}

/* ─── Tab Nav ─── */
.tab-nav{display:flex;gap:0;margin-bottom:12px;border-bottom:1px solid var(--border);}
.tab-btn{background:none;border:none;color:var(--text-muted);padding:8px 16px;cursor:pointer;border-bottom:2px solid transparent;transition:all 0.2s;font-size:0.84em;font-weight:600;}
.tab-btn:hover{color:var(--text);transform:none;box-shadow:none;}
.tab-btn.active{color:var(--blue);border-bottom-color:var(--blue);}

/* ─── Rumor Feed ─── */
.rumor-item{padding:10px 0;border-bottom:1px solid rgba(30,41,59,0.4);}
.rumor-item:last-child{border-bottom:none;}
.rumor-meta{display:flex;align-items:center;gap:8px;margin-bottom:4px;font-size:0.78em;}
.rumor-content{font-size:0.86em;color:var(--text-sub);line-height:1.5;}
.trust-dot{width:8px;height:8px;border-radius:50%;display:inline-block;}

/* ─── Skeleton ─── */
.sk{background:linear-gradient(90deg,#1e293b 25%,#2d3a4f 50%,#1e293b 75%);background-size:200% 100%;animation:sk-pulse 1.5s ease-in-out infinite;border-radius:6px;height:14px;margin-bottom:8px;}
@keyframes sk-pulse{0%{background-position:200% 0;}100%{background-position:-200% 0;}}

/* ─── Empty State ─── */
.empty{text-align:center;padding:28px 12px;color:var(--text-muted);font-size:0.86em;}
.empty svg{width:40px;height:40px;margin-bottom:8px;opacity:0.3;}

/* ─── Misc ─── */
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;}
.dot.green{background:var(--green);box-shadow:0 0 8px var(--green);}
.dot.red{background:var(--red);box-shadow:0 0 8px var(--red);}
.title-trunc{max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block;vertical-align:bottom;}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:translateY(0);}}
.fade-in{animation:fadeIn 0.3s ease-out;}
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dashboard JS (plain strings, no brace escaping)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_JS_UTILS = """
const SENTI_LABELS={very_positive:'매우 긍정',positive:'긍정',neutral:'중립',negative:'부정',very_negative:'매우 부정',pending:'대기'};
const SENTI_COLORS={very_positive:'#22c55e',positive:'#4ade80',neutral:'#94a3b8',negative:'#fb923c',very_negative:'#ef4444'};
const DOMESTIC=['naver','hankyung','mk','chosun','yonhap','sbs','kbs','mbc','edaily','sedaily'];

function sentiBadge(s){return '<span class="sentiment '+(s||'pending')+'">'+(SENTI_LABELS[s]||s||'-')+'</span>';}
function impactHtml(v){
    if(v==null)return'-';
    var p=Math.round(Math.abs(v)*100),c=v>=0.6?'var(--green)':v>=0.3?'var(--amber)':'var(--red)';
    return '<div class="impact-bar"><div class="impact-fill" style="width:'+p+'%;background:'+c+'"></div></div> '+v.toFixed(2);
}
function scoreHtml(v,max){
    max=max||100;if(v==null)return'-';
    var p=Math.round((v/max)*100);
    return '<div class="score-wrap"><div class="score-bar"><div class="score-fill" style="width:'+p+'%;background:var(--blue)"></div></div><span class="score-val">'+v.toFixed(0)+'</span></div>';
}
function trunc(s,n){n=n||45;if(!s)return'';return s.length<=n?s:'<span class="title-trunc" title="'+s.replace(/"/g,'&quot;')+'">'+s.slice(0,n)+'...</span>';}
function emptyHtml(msg){return '<div class="empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-4l-2 3H10l-2-3H4"/></svg><p>'+msg+'</p></div>';}
function animateNum(el,target,dur){
    dur=dur||500;var start=parseInt(el.textContent)||0,diff=target-start,t0=performance.now();
    function step(now){var p=Math.min((now-t0)/dur,1),e=1-Math.pow(1-p,3);el.textContent=Math.round(start+diff*e).toLocaleString();if(p<1)requestAnimationFrame(step);}
    requestAnimationFrame(step);
}
function fmtKRW(v){return (v||0).toLocaleString()+'원';}
function fmtTime(t){return t?t.slice(11,19):'';}
"""

_JS_CLOCK = """
function updateClock(){
    var now=new Date(new Date().toLocaleString('en-US',{timeZone:'Asia/Seoul'}));
    var h=String(now.getHours()).padStart(2,'0'),m=String(now.getMinutes()).padStart(2,'0'),s=String(now.getSeconds()).padStart(2,'0');
    document.getElementById('kstClock').textContent=h+':'+m+':'+s;
    var days=['일','월','화','수','목','금','토'];
    document.getElementById('kstDate').textContent=(now.getMonth()+1)+'/'+now.getDate()+' ('+days[now.getDay()]+')';
    var day=now.getDay(),mins=now.getHours()*60+now.getMinutes(),el=document.getElementById('mktCd');
    if(day>=1&&day<=5){
        if(mins<540){var d=540-mins;el.textContent='장 시작까지 '+Math.floor(d/60)+'h '+d%60+'m';el.className='mkt-cd closed';}
        else if(mins<=930){var d=930-mins;el.textContent='장 마감까지 '+Math.floor(d/60)+'h '+d%60+'m';el.className='mkt-cd open';}
        else{el.textContent='장 마감';el.className='mkt-cd closed';}
    }else{el.textContent='주말 휴장';el.className='mkt-cd closed';}
}
setInterval(updateClock,1000);updateClock();
"""

_JS_CHARTS = """
var sentiChart=null;
function updateSentiChart(dist){
    var ctx=document.getElementById('sentiCanvas');if(!ctx)return;
    var keys=['very_positive','positive','neutral','negative','very_negative'];
    var labels=keys.map(function(k){return SENTI_LABELS[k];});
    var colors=keys.map(function(k){return SENTI_COLORS[k];});
    var data=keys.map(function(k){return dist[k]||0;});
    var total=data.reduce(function(a,b){return a+b;},0);
    document.getElementById('sentiTotal').textContent=total+'건 분석';
    if(sentiChart){sentiChart.data.datasets[0].data=data;sentiChart.update('none');return;}
    sentiChart=new Chart(ctx,{type:'doughnut',data:{labels:labels,datasets:[{data:data,backgroundColor:colors,borderWidth:0,hoverBorderWidth:2,hoverBorderColor:'#fff'}]},options:{responsive:true,maintainAspectRatio:true,cutout:'62%',plugins:{legend:{position:'bottom',labels:{color:'#94a3b8',padding:10,font:{size:11},usePointStyle:true,pointStyleWidth:8}}}}});
}
"""

_JS_FETCHERS = """
var allNews=[];
async function fetchAll(){
    var results=await Promise.allSettled([
        fetch('/api/status').then(function(r){return r.json();}),
        fetch('/api/portfolio').then(function(r){return r.json();}),
        fetch('/api/candidates').then(function(r){return r.json();}),
        fetch('/api/orders').then(function(r){return r.json();}),
        fetch('/api/news').then(function(r){return r.json();}),
        fetch('/api/rumors').then(function(r){return r.json();}),
        fetch('/api/stats').then(function(r){return r.json();}),
    ]);
    var vals=results.map(function(r){return r.status==='fulfilled'?r.value:null;});
    if(vals[0])renderStatus(vals[0]);
    if(vals[1])renderPortfolio(vals[1]);
    if(vals[2])renderCandidates(vals[2]);
    if(vals[3])renderOrders(vals[3]);
    if(vals[4]){allNews=vals[4].news||[];renderNews(allNews);}
    if(vals[5])renderRumors(vals[5]);
    if(vals[6])renderStats(vals[6]);
}

function renderStatus(d){
    var dot=document.getElementById('statusDot');
    if(dot)dot.className='dot '+(d.market_open?'green':'red');
    var rows=Object.entries(d.modules).map(function(e){
        var n=e[0],m=e[1];
        return '<tr><td>'+n+'</td><td><span class="badge '+m.status+'">'+m.status+'</span></td><td>'+m.execution_count+'</td><td style="color:'+(m.error_count>0?'var(--red)':'var(--text-muted)')+'">'+m.error_count+'</td></tr>';
    }).join('');
    document.getElementById('modulesTable').innerHTML=rows||emptyHtml('모듈 없음');
    // scheduler
    var jobs=(d.scheduled_jobs||[]);
    var jH=jobs.map(function(j){
        var next=j.next_run?new Date(j.next_run).toLocaleTimeString('ko-KR',{hour:'2-digit',minute:'2-digit'}):'—';
        return '<tr><td>'+j.name+'</td><td>'+next+'</td><td style="color:var(--text-muted);font-size:0.8em">'+j.trigger+'</td></tr>';
    }).join('');
    document.getElementById('schedTable').innerHTML=jH||emptyHtml('예정 작업 없음');
}

function renderPortfolio(p){
    var tv=p.total_value||0,pnl=p.total_pnl||0,pct=p.total_pnl_pct||0;
    document.getElementById('totalValue').textContent=fmtKRW(tv);
    var pe=document.getElementById('totalPnl');
    pe.textContent=(pnl>=0?'+':'')+fmtKRW(pnl);
    pe.className='big-number '+(pnl>=0?'positive':'negative');
    var positions=p.positions||[];
    var rows=positions.map(function(x){
        var pctCls=(x.unrealized_pnl_pct||0)>=0?'positive':'negative';
        return '<tr><td>'+(x.name||x.ticker)+'</td><td>'+x.quantity+'</td><td>'+(x.avg_price||0).toLocaleString()+'</td><td>'+(x.current_price||0).toLocaleString()+'</td><td class="'+pctCls+'">'+(x.unrealized_pnl_pct||0).toFixed(2)+'%</td></tr>';
    }).join('');
    document.getElementById('posTable').innerHTML=rows||emptyHtml('보유 종목 없음');
}

function renderCandidates(c){
    var list=(c.candidates||[]).slice(0,10);
    var rows=list.map(function(x){
        return '<tr><td><strong>'+x.ticker+'</strong></td><td>'+scoreHtml(x.total_score)+'</td><td>'+scoreHtml(x.news_score)+'</td><td>'+scoreHtml(x.sentiment_score)+'</td><td><span class="badge '+(x.signal||'')+'">'+({strong_buy:'강력매수',buy:'매수',hold:'관망',sell:'매도',strong_sell:'강력매도'}[x.signal]||x.signal||'-')+'</span></td></tr>';
    }).join('');
    document.getElementById('candTable').innerHTML=rows||emptyHtml('오늘 후보 없음');
}

function renderOrders(o){
    var list=(o.orders||[]).slice(0,10);
    var rows=list.map(function(x){
        var sideCls=x.side==='buy'?'positive':'negative';
        var sideText=x.side==='buy'?'매수':'매도';
        return '<tr><td>'+fmtTime(x.created_at)+'</td><td>'+x.ticker+'</td><td class="'+sideCls+'">'+sideText+'</td><td>'+x.quantity+'</td><td>'+(x.price||0).toLocaleString()+'</td><td><span class="badge '+(x.status||'')+'">'+x.status+'</span></td></tr>';
    }).join('');
    document.getElementById('ordersTable').innerHTML=rows||emptyHtml('주문 내역 없음');
}

function renderNews(list){
    var rows=list.slice(0,20).map(function(x){
        return '<tr class="fade-in"><td style="white-space:nowrap;color:var(--text-muted)">'+fmtTime(x.time)+'</td><td>'+trunc(x.title,50)+'</td><td style="color:var(--text-muted)">'+((x.source||'').length>12?(x.source||'').slice(0,12)+'..':x.source||'-')+'</td><td>'+sentiBadge(x.sentiment)+'</td><td>'+impactHtml(x.impact_score)+'</td></tr>';
    }).join('');
    document.getElementById('newsTable').innerHTML=rows||emptyHtml('수집된 뉴스 없음');
}
function filterNews(f){
    document.querySelectorAll('.tab-btn').forEach(function(b){b.classList.remove('active');});
    event.target.classList.add('active');
    var filtered=allNews;
    if(f==='domestic')filtered=allNews.filter(function(n){return DOMESTIC.some(function(s){return (n.source||'').toLowerCase().includes(s);});});
    else if(f==='global')filtered=allNews.filter(function(n){return !DOMESTIC.some(function(s){return (n.source||'').toLowerCase().includes(s);});});
    renderNews(filtered);
}

function renderRumors(d){
    var list=(d.rumors||[]).slice(0,12);
    if(!list.length){document.getElementById('rumorsContent').innerHTML=emptyHtml('수집된 루머 없음');return;}
    var html=list.map(function(r){
        var trustColor=r.trust_score>=0.7?'var(--green)':r.trust_score>=0.4?'var(--amber)':'var(--red)';
        var verified=r.verified?'<span style="color:var(--green);font-size:0.75em">✓ 검증</span>':'<span style="color:var(--text-muted);font-size:0.75em">미검증</span>';
        return '<div class="rumor-item"><div class="rumor-meta"><span class="trust-dot" style="background:'+trustColor+'"></span><strong style="color:var(--text-sub)">'+(r.source_channel||'익명')+'</strong>'+sentiBadge(r.sentiment)+verified+'<span style="color:var(--text-muted)">'+fmtTime(r.time)+'</span></div><div class="rumor-content">'+(r.content||'').slice(0,120)+(r.content&&r.content.length>120?'...':'')+'</div></div>';
    }).join('');
    document.getElementById('rumorsContent').innerHTML=html;
}

function renderStats(s){
    animateNum(document.getElementById('statNews'),s.news_total||0);
    animateNum(document.getElementById('statRumors'),s.rumors_total||0);
    animateNum(document.getElementById('statCandidates'),s.candidates_today||0);
    animateNum(document.getElementById('statOrders'),s.orders_today||0);
    if(s.sentiment_distribution)updateSentiChart(s.sentiment_distribution);
}

async function submitOrder(e){
    e.preventDefault();
    var d={ticker:document.getElementById('ticker').value,side:document.getElementById('side').value,quantity:parseInt(document.getElementById('quantity').value),price:parseFloat(document.getElementById('price').value)||null,order_type:document.getElementById('price').value?'limit':'market'};
    try{var r=await fetch('/api/manual-order',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});var res=await r.json();alert(res.status==='submitted'?'주문 완료: '+res.order_id:'오류: '+JSON.stringify(res));fetchAll();}catch(err){alert('실패: '+err.message);}
}
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Dashboard HTML Snippets
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TOPBAR_HTML = """
<div class="header">
    <h1>
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="var(--blue)" stroke-width="2"><path d="M3 3v18h18"/><path d="M7 16l4-8 4 4 5-9"/></svg>
        KR Stock AutoTrader
    </h1>
    <div style="display:flex;align-items:center;gap:12px;">
        <div class="nav">
            <a href="/" class="active">대시보드</a>
            {setup_link}
        </div>
        <span class="access-mode {access_class}">{access_label}</span>
        <span class="dot" id="statusDot"></span>
    </div>
</div>
<div class="stats-ribbon">
    <div class="stat-item"><span class="stat-value" id="statNews">--</span><span class="stat-label">뉴스 수집</span></div>
    <div class="stat-item"><span class="stat-value" id="statRumors">--</span><span class="stat-label">루머</span></div>
    <div class="stat-item"><span class="stat-value" id="statCandidates">--</span><span class="stat-label">투자 후보</span></div>
    <div class="stat-item"><span class="stat-value" id="statOrders">--</span><span class="stat-label">오늘 주문</span></div>
</div>
"""

_GRID_HTML = """
<!-- Row 1: Clock + Portfolio -->
<div class="card">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg>
        시장 시계
    </h2>
    <div class="clock-row">
        <span class="clock-time" id="kstClock">--:--:--</span>
        <span class="clock-date" id="kstDate">-</span>
    </div>
    <div style="margin-top:12px;">
        <span class="mkt-cd closed" id="mktCd">로딩...</span>
    </div>
</div>

<div class="card span-2">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8V7m0 10v1m9-9a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
        포트폴리오
    </h2>
    <div style="display:flex;justify-content:space-around;margin-bottom:14px;">
        <div class="metric"><div class="big-number" id="totalValue">0원</div><label>총 평가금</label></div>
        <div class="metric"><div class="big-number positive" id="totalPnl">+0원</div><label>총 손익</label></div>
    </div>
    <table>
        <thead><tr><th>종목</th><th>수량</th><th>평균가</th><th>현재가</th><th>수익률</th></tr></thead>
        <tbody id="posTable"><tr><td colspan="5"><div class="sk" style="width:70%"></div><div class="sk" style="width:50%"></div></td></tr></tbody>
    </table>
</div>

<!-- Row 2: Sentiment Chart + Modules + Scheduler -->
<div class="card">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 3.055A9.001 9.001 0 1020.945 13H11V3.055z"/><path d="M20.488 9H15V3.512A9.025 9.025 0 0120.488 9z"/></svg>
        감정 분포
    </h2>
    <div style="max-width:220px;margin:0 auto;">
        <canvas id="sentiCanvas"></canvas>
    </div>
    <p style="text-align:center;color:var(--text-muted);font-size:0.82em;margin-top:8px;" id="sentiTotal">분석 중...</p>
</div>

<div class="card">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><circle cx="12" cy="12" r="3"/></svg>
        모듈 상태
    </h2>
    <table>
        <thead><tr><th>모듈</th><th>상태</th><th>실행</th><th>오류</th></tr></thead>
        <tbody id="modulesTable"><tr><td colspan="4"><div class="sk" style="width:80%"></div><div class="sk" style="width:60%"></div></td></tr></tbody>
    </table>
</div>

<div class="card">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
        스케줄러
    </h2>
    <table>
        <thead><tr><th>작업</th><th>다음 실행</th><th>트리거</th></tr></thead>
        <tbody id="schedTable"><tr><td colspan="3"><div class="sk" style="width:70%"></div><div class="sk" style="width:50%"></div></td></tr></tbody>
    </table>
</div>

<!-- Row 3: Candidates -->
<div class="card span-2">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
        오늘의 투자 후보
    </h2>
    <table>
        <thead><tr><th>종목</th><th>종합</th><th>뉴스</th><th>센티멘트</th><th>시그널</th></tr></thead>
        <tbody id="candTable"><tr><td colspan="5"><div class="sk" style="width:80%"></div><div class="sk" style="width:60%"></div></td></tr></tbody>
    </table>
</div>

<!-- Rumors -->
<div class="card">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/></svg>
        루머 피드
    </h2>
    <div id="rumorsContent">
        <div class="sk" style="width:85%"></div><div class="sk" style="width:65%"></div><div class="sk" style="width:75%"></div>
    </div>
</div>

<!-- Row 4: News (wide) -->
<div class="card span-2">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z"/></svg>
        뉴스
    </h2>
    <div class="tab-nav">
        <button class="tab-btn active" onclick="filterNews('all')">전체</button>
        <button class="tab-btn" onclick="filterNews('domestic')">국내</button>
        <button class="tab-btn" onclick="filterNews('global')">해외</button>
    </div>
    <table>
        <thead><tr><th>시간</th><th>제목</th><th>출처</th><th>감정</th><th>영향도</th></tr></thead>
        <tbody id="newsTable"><tr><td colspan="5"><div class="sk" style="width:90%"></div><div class="sk" style="width:70%"></div><div class="sk" style="width:80%"></div></td></tr></tbody>
    </table>
</div>

<!-- Orders -->
<div class="card">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>
        최근 주문
    </h2>
    <table>
        <thead><tr><th>시간</th><th>종목</th><th>구분</th><th>수량</th><th>가격</th><th>상태</th></tr></thead>
        <tbody id="ordersTable"><tr><td colspan="6"><div class="sk" style="width:80%"></div><div class="sk" style="width:55%"></div></td></tr></tbody>
    </table>
</div>
"""

_WRITE_CONTROLS_HTML = """
<div class="card span-2">
    <h2>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"/></svg>
        수동 주문
    </h2>
    <form id="manualOrderForm" onsubmit="submitOrder(event)" style="display:flex;flex-wrap:wrap;gap:8px;align-items:end;">
        <div style="flex:1;min-width:110px;">
            <label style="display:block;color:var(--text-muted);font-size:0.78em;margin-bottom:4px;">종목코드</label>
            <input type="text" id="ticker" placeholder="005930" required style="width:100%;">
        </div>
        <div>
            <label style="display:block;color:var(--text-muted);font-size:0.78em;margin-bottom:4px;">구분</label>
            <select id="side" style="width:100%;"><option value="buy">매수</option><option value="sell">매도</option></select>
        </div>
        <div style="flex:1;min-width:80px;">
            <label style="display:block;color:var(--text-muted);font-size:0.78em;margin-bottom:4px;">수량</label>
            <input type="number" id="quantity" placeholder="수량" required style="width:100%;">
        </div>
        <div style="flex:1;min-width:100px;">
            <label style="display:block;color:var(--text-muted);font-size:0.78em;margin-bottom:4px;">가격</label>
            <input type="number" id="price" placeholder="시장가=빈칸" style="width:100%;">
        </div>
        <button type="submit" style="height:42px;padding:0 20px;">주문 실행</button>
    </form>
</div>
"""

_KEYS_WARNING_HTML = """
<div style="background:rgba(245,158,11,0.08);border:1px solid rgba(245,158,11,0.2);border-radius:12px;padding:14px 18px;margin-bottom:16px;display:flex;align-items:center;gap:12px;">
    <span style="font-size:1.3em;">⚠️</span>
    <div>
        <strong style="color:var(--amber);">API 키 미설정</strong>
        <p style="color:var(--text-sub);font-size:0.82em;margin-top:2px;">로컬에서 <a href="/setup" style="color:var(--blue);">/setup</a> 페이지에서 API 키를 등록하세요.</p>
    </div>
</div>
"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML 렌더링 함수
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_dashboard_html(is_local_: bool, keys_configured: bool) -> str:
    setup_link = '<a href="/setup">API 설정</a>' if is_local_ else ''
    access_class = 'local' if is_local_ else 'remote'
    access_label = 'LOCAL' if is_local_ else 'REMOTE'
    keys_warning = _KEYS_WARNING_HTML if not keys_configured else ""
    write_controls = _WRITE_CONTROLS_HTML if is_local_ else ""

    topbar = _TOPBAR_HTML.format(
        setup_link=setup_link,
        access_class=access_class,
        access_label=access_label,
    )

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>KR Stock AutoTrader</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js"></script>
    {_COMMON_STYLE}
    <style>{_DASHBOARD_CSS}</style>
</head>
<body>
    {topbar}
    <div class="container">
        {keys_warning}
        <div class="grid">
            {_GRID_HTML}
            {write_controls}
        </div>
    </div>
    <script>
{_JS_UTILS}
{_JS_CLOCK}
{_JS_CHARTS}
{_JS_FETCHERS}
fetchAll();setInterval(fetchAll,10000);
    </script>
</body>
</html>"""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Setup Page (mostly unchanged)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _render_setup_html() -> str:
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>AutoTrader - 초기 설정</title>
    {_COMMON_STYLE}
    <style>
        .setup-container {{ max-width:720px; margin:40px auto; padding:0 20px; }}
        .setup-card {{
            background:var(--bg-card-solid); border-radius:16px; padding:32px;
            border:1px solid var(--border); margin-bottom:24px;
        }}
        .setup-card h2 {{ color:var(--blue); margin-bottom:20px; font-size:1.15em; }}
        .form-group {{ margin-bottom:18px; }}
        .form-group label {{
            display:block; color:var(--text-sub); font-size:0.83em;
            margin-bottom:6px; font-weight:600;
        }}
        .form-group input, .form-group select {{ width:100%; padding:12px 16px; }}
        .form-group .hint {{ color:#475569; font-size:0.76em; margin-top:4px; }}
        .save-btn {{ width:100%; padding:14px; font-size:1.05em; margin-top:10px; border-radius:10px; }}
        .status-dot {{ display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; }}
        .status-dot.set {{ background:var(--green); }}
        .status-dot.unset {{ background:var(--red); }}
        .shield {{
            background:#0f172a; border:1px solid var(--border); border-radius:12px;
            padding:16px; margin-bottom:24px; display:flex; align-items:center; gap:12px;
        }}
        .shield-icon {{ font-size:1.8em; }}
        .shield-text h3 {{ color:var(--green); font-size:0.92em; }}
        .shield-text p {{ color:var(--text-muted); font-size:0.78em; margin-top:4px; }}
        .toast {{
            position:fixed; top:20px; right:20px; padding:14px 24px; border-radius:10px;
            background:var(--green); color:white; font-weight:700; display:none;
            box-shadow:0 8px 24px rgba(0,0,0,0.4); z-index:999;
            animation: slideIn 0.3s ease-out;
        }}
        .toast.error {{ background:var(--red); }}
        @keyframes slideIn {{ from {{ transform:translateX(100px); opacity:0; }} to {{ transform:translateX(0); opacity:1; }} }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🔐 AutoTrader 초기 설정</h1>
        <div class="nav">
            <a href="/">대시보드</a>
            <a href="/setup" class="active">API 설정</a>
        </div>
    </div>

    <div class="setup-container">
        <div class="shield">
            <div class="shield-icon">🛡️</div>
            <div class="shield-text">
                <h3>AES-256 암호화 보호</h3>
                <p>모든 API 키는 이 PC에서만 복호화 가능한 암호화 저장소에 보관됩니다.</p>
            </div>
        </div>

        <form id="keysForm" onsubmit="saveKeys(event)">
            <div class="setup-card">
                <h2>🪙 업비트 Open API</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_UPBIT_ACCESS_KEY"></span> Access Key</label>
                    <input type="password" id="UPBIT_ACCESS_KEY" placeholder="업비트 Access Key 입력" autocomplete="off">
                    <div class="hint">upbit.com → 마이페이지 → Open API 관리</div>
                </div>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_UPBIT_SECRET_KEY"></span> Secret Key</label>
                    <input type="password" id="UPBIT_SECRET_KEY" placeholder="업비트 Secret Key 입력" autocomplete="off">
                </div>
            </div>

            <div class="setup-card">
                <h2>🤖 Google Gemini API</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_GEMINI_API_KEY"></span> API KEY</label>
                    <input type="password" id="GEMINI_API_KEY" placeholder="Gemini API 키 입력" autocomplete="off">
                    <div class="hint">aistudio.google.com/apikey 에서 발급</div>
                </div>
            </div>

            <div class="setup-card">
                <h2>🧠 Claude API (선택사항)</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_CLAUDE_API_KEY"></span> API KEY</label>
                    <input type="password" id="CLAUDE_API_KEY" placeholder="보조 AI — 입력하지 않아도 됩니다" autocomplete="off">
                </div>
            </div>

            <div class="setup-card">
                <h2>📧 Gmail 리포트 발송</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_GMAIL_ADDRESS"></span> Gmail 주소</label>
                    <input type="email" id="GMAIL_ADDRESS" placeholder="your@gmail.com" autocomplete="off">
                </div>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_GMAIL_APP_PASSWORD"></span> 앱 비밀번호</label>
                    <input type="password" id="GMAIL_APP_PASSWORD" placeholder="Google 앱 비밀번호 (16자리)" autocomplete="off">
                    <div class="hint">Google 계정 → 보안 → 2단계 인증 → 앱 비밀번호</div>
                </div>
            </div>

            <button type="submit" class="save-btn">🔒 암호화 저장</button>
        </form>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        async function loadStatus() {{
            try {{
                const resp = await fetch('/api/keys/status');
                const data = await resp.json();
                const keys = data.keys || {{}};
                for (const [k, v] of Object.entries(keys)) {{
                    const dot = document.getElementById('dot_' + k);
                    const input = document.getElementById(k);
                    if (dot) dot.className = 'status-dot ' + (v ? 'set' : 'unset');
                    if (input && v) input.placeholder = v + '  (저장됨)';
                }}
            }} catch(e) {{ console.error(e); }}
        }}
        function showToast(msg, isError) {{
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'toast' + (isError ? ' error' : '');
            t.style.display = 'block';
            setTimeout(() => {{ t.style.display = 'none'; }}, 3000);
        }}
        async function saveKeys(e) {{
            e.preventDefault();
            const fields = ['UPBIT_ACCESS_KEY','UPBIT_SECRET_KEY',
                'GEMINI_API_KEY','CLAUDE_API_KEY','GMAIL_ADDRESS','GMAIL_APP_PASSWORD'];
            const body = {{}};
            fields.forEach(f => {{ body[f] = document.getElementById(f).value; }});
            try {{
                const resp = await fetch('/api/keys/save', {{
                    method: 'POST', headers: {{'Content-Type':'application/json'}}, body: JSON.stringify(body),
                }});
                const result = await resp.json();
                if (result.status === 'saved') {{
                    showToast('✅ 암호화 저장 완료!', false);
                    setTimeout(() => loadStatus(), 500);
                    fields.forEach(f => {{ document.getElementById(f).value = ''; }});
                }} else {{
                    showToast('❌ 저장 실패', true);
                }}
            }} catch (err) {{ showToast('❌ 오류: ' + err.message, true); }}
        }}
        loadStatus();
    </script>
</body>
</html>"""
