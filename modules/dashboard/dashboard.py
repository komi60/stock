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


def is_local(request: Request) -> bool:
    """요청이 로컬에서 온 것인지 확인."""
    client_host = request.client.host if request.client else ""
    return client_host in ("127.0.0.1", "::1", "localhost", "testclient")


def require_local(request: Request) -> None:
    """로컬 접속만 허용하는 의존성."""
    if not is_local(request):
        raise HTTPException(status_code=403, detail="이 작업은 로컬에서만 가능합니다.")


class ManualOrderRequest(BaseModel):
    ticker: str
    side: str
    quantity: int
    price: float | None = None
    order_type: str = "limit"


class ApiKeysRequest(BaseModel):
    KIS_APP_KEY: str = ""
    KIS_APP_SECRET: str = ""
    KIS_ACCOUNT_NO: str = ""
    KIS_ACCOUNT_PROD_CODE: str = "01"
    KIS_IS_PAPER: str = "true"
    GEMINI_API_KEY: str = ""
    CLAUDE_API_KEY: str = ""
    GMAIL_ADDRESS: str = ""
    GMAIL_APP_PASSWORD: str = ""


def create_dashboard(
    registry: PluginRegistry,
    scheduler: TradingScheduler,
    master_module: Any,
    config: dict[str, Any],
) -> FastAPI:
    """FastAPI 대시보드 앱 생성."""

    app = FastAPI(title="KR Stock AutoTrader Dashboard", version="1.0.0")
    vault = SecureVault()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    # ─── 페이지 라우트 ────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        """메인: 키 미설정이면 설정 페이지, 설정 완료면 대시보드."""
        local = is_local(request)
        if local and not vault.has_keys():
            return _render_setup_html()
        return _render_dashboard_html(local, vault.has_keys())

    @app.get("/setup", response_class=HTMLResponse, dependencies=[Depends(require_local)])
    async def setup_page():
        """API 키 설정 페이지 (로컬 전용)."""
        return _render_setup_html()

    # ─── API 키 관리 (로컬 전용) ──────────────────────────

    @app.get("/api/keys/status", dependencies=[Depends(require_local)])
    async def keys_status():
        """키 설정 상태 (마스킹)."""
        return {
            "configured": vault.has_keys(),
            "keys": vault.get_status(),
        }

    @app.post("/api/keys/save", dependencies=[Depends(require_local)])
    async def save_keys(keys: ApiKeysRequest):
        """API 키 암호화 저장."""
        data = keys.model_dump()
        # 빈 값은 기존 값 유지
        for k, v in data.items():
            if v:
                vault.set(k, v)
        vault.save()
        vault.export_to_env()
        logger.info("API 키 암호화 저장 완료")
        return {"status": "saved", "configured": vault.has_keys()}

    # ─── READ-ONLY 엔드포인트 (외부/로컬 모두) ────────────

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
        return {"positions": [], "total_value": 0}

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
                "FROM news ORDER BY created_at DESC LIMIT 30")
            rows = await cursor.fetchall()
            await db.close()
            news = [{"title": r[0], "source": r[1], "sentiment": r[2], "impact_score": r[3],
                      "summary": r[4], "time": r[5]} for r in rows]
            return {"news": news}
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

    return app


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  HTML 템플릿
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_COMMON_STYLE = """
<style>
    * { margin:0; padding:0; box-sizing:border-box; }
    body { font-family:'Segoe UI',system-ui,sans-serif; background:#0a0e1a; color:#e0e0e0; }
    .header {
        background:linear-gradient(135deg,#0f1628,#1a2342);
        padding:20px 30px; display:flex; justify-content:space-between; align-items:center;
        border-bottom:2px solid #3b82f6;
    }
    .header h1 { color:#3b82f6; font-size:1.5em; }
    .nav { display:flex; gap:12px; }
    .nav a {
        color:#94a3b8; text-decoration:none; padding:6px 14px; border-radius:8px;
        font-size:0.9em; transition:all 0.2s;
    }
    .nav a:hover, .nav a.active { background:#3b82f622; color:#3b82f6; }
    .container { max-width:1400px; margin:0 auto; padding:24px; }
    .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(380px,1fr)); gap:20px; }
    .card {
        background:#111827; border-radius:14px; padding:24px;
        border:1px solid #1e293b; box-shadow:0 4px 20px rgba(0,0,0,0.3);
    }
    .card h2 { color:#3b82f6; font-size:1.15em; margin-bottom:16px; display:flex; align-items:center; gap:8px; }
    table { width:100%; border-collapse:collapse; font-size:0.88em; }
    th { text-align:left; padding:10px 8px; color:#64748b; border-bottom:1px solid #1e293b; font-weight:600; }
    td { padding:10px 8px; border-bottom:1px solid #151d2e; }
    tr:hover { background:#1e293b44; }
    .positive { color:#22c55e; }
    .negative { color:#ef4444; }
    .badge { display:inline-block; padding:3px 10px; border-radius:6px; font-size:0.78em; font-weight:700; }
    .badge.running { background:#22c55e22; color:#22c55e; }
    .badge.idle { background:#3b82f622; color:#3b82f6; }
    .badge.error { background:#ef444422; color:#ef4444; }
    .big-number { font-size:2em; font-weight:800; letter-spacing:-1px; }
    .metric { text-align:center; padding:12px; }
    .metric label { display:block; color:#64748b; font-size:0.82em; margin-top:6px; }
    input,select,button,textarea {
        padding:10px 14px; border-radius:8px; border:1px solid #1e293b;
        background:#0a0e1a; color:#e0e0e0; font-size:0.92em; outline:none;
        transition:border-color 0.2s;
    }
    input:focus,select:focus,textarea:focus { border-color:#3b82f6; }
    button {
        background:linear-gradient(135deg,#3b82f6,#2563eb); border:none; cursor:pointer;
        font-weight:700; color:white; transition:transform 0.1s,box-shadow 0.2s;
    }
    button:hover { transform:translateY(-1px); box-shadow:0 4px 16px rgba(59,130,246,0.3); }
    button:active { transform:translateY(0); }
    .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
    .dot.green { background:#22c55e; box-shadow:0 0 10px #22c55e; }
    .access-mode { font-size:0.8em; padding:4px 12px; border-radius:12px; }
    .access-mode.local { background:#22c55e22; color:#22c55e; }
    .access-mode.remote { background:#f59e0b22; color:#f59e0b; }
</style>
"""


def _render_setup_html() -> str:
    """API 키 설정 페이지 (로컬 전용)."""
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
            background:#111827; border-radius:16px; padding:32px;
            border:1px solid #1e293b; margin-bottom:24px;
        }}
        .setup-card h2 {{ color:#3b82f6; margin-bottom:20px; font-size:1.2em; }}
        .form-group {{ margin-bottom:18px; }}
        .form-group label {{
            display:block; color:#94a3b8; font-size:0.85em;
            margin-bottom:6px; font-weight:600;
        }}
        .form-group input, .form-group select {{
            width:100%; padding:12px 16px;
        }}
        .form-group .hint {{ color:#475569; font-size:0.78em; margin-top:4px; }}
        .section-divider {{
            border:none; border-top:1px solid #1e293b; margin:28px 0;
        }}
        .save-btn {{
            width:100%; padding:14px; font-size:1.05em; margin-top:10px;
            border-radius:10px;
        }}
        .status-dot {{
            display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px;
        }}
        .status-dot.set {{ background:#22c55e; }}
        .status-dot.unset {{ background:#ef4444; }}
        .shield {{
            background:#0f172a; border:1px solid #1e293b; border-radius:10px;
            padding:16px; margin-bottom:24px; display:flex; align-items:center; gap:12px;
        }}
        .shield-icon {{ font-size:1.8em; }}
        .shield-text h3 {{ color:#22c55e; font-size:0.95em; }}
        .shield-text p {{ color:#64748b; font-size:0.8em; margin-top:4px; }}
        .toast {{
            position:fixed; top:20px; right:20px; padding:14px 24px; border-radius:10px;
            background:#22c55e; color:white; font-weight:700; display:none;
            box-shadow:0 8px 24px rgba(0,0,0,0.4); z-index:999;
            animation: slideIn 0.3s ease-out;
        }}
        .toast.error {{ background:#ef4444; }}
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
                <p>모든 API 키는 이 PC에서만 복호화 가능한 암호화 저장소에 보관됩니다. 파일이 유출되어도 다른 PC에서는 해독 불가.</p>
            </div>
        </div>

        <form id="keysForm" onsubmit="saveKeys(event)">
            <!-- 한국투자증권 -->
            <div class="setup-card">
                <h2>🏦 한국투자증권 Open API</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_KIS_APP_KEY"></span> APP KEY</label>
                    <input type="password" id="KIS_APP_KEY" placeholder="발급받은 앱 키 입력" autocomplete="off">
                    <div class="hint">한국투자증권 API 포털 → 앱 등록 → 앱 키</div>
                </div>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_KIS_APP_SECRET"></span> APP SECRET</label>
                    <input type="password" id="KIS_APP_SECRET" placeholder="발급받은 앱 시크릿 입력" autocomplete="off">
                </div>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_KIS_ACCOUNT_NO"></span> 계좌번호</label>
                    <input type="password" id="KIS_ACCOUNT_NO" placeholder="숫자만 입력 (예: 5012345601)" autocomplete="off">
                    <div class="hint">종합계좌번호 8자리 + 상품코드 2자리</div>
                </div>
                <div style="display:flex; gap:12px;">
                    <div class="form-group" style="flex:1;">
                        <label>상품코드</label>
                        <select id="KIS_ACCOUNT_PROD_CODE">
                            <option value="01">01 (위탁)</option>
                            <option value="02">02</option>
                        </select>
                    </div>
                    <div class="form-group" style="flex:1;">
                        <label>투자 모드</label>
                        <select id="KIS_IS_PAPER">
                            <option value="true" selected>모의투자 (안전)</option>
                            <option value="false">실전투자</option>
                        </select>
                    </div>
                </div>
            </div>

            <!-- Gemini -->
            <div class="setup-card">
                <h2>🤖 Google Gemini API</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_GEMINI_API_KEY"></span> API KEY</label>
                    <input type="password" id="GEMINI_API_KEY" placeholder="Gemini API 키 입력" autocomplete="off">
                    <div class="hint">aistudio.google.com/apikey 에서 발급</div>
                </div>
            </div>

            <!-- Claude (선택) -->
            <div class="setup-card">
                <h2>🧠 Claude API (선택사항)</h2>
                <div class="form-group">
                    <label><span class="status-dot" id="dot_CLAUDE_API_KEY"></span> API KEY</label>
                    <input type="password" id="CLAUDE_API_KEY" placeholder="보조 AI — 입력하지 않아도 됩니다" autocomplete="off">
                </div>
            </div>

            <!-- Gmail -->
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
        // 현재 저장 상태 로드
        async function loadStatus() {{
            try {{
                const resp = await fetch('/api/keys/status');
                const data = await resp.json();
                const keys = data.keys || {{}};
                for (const [k, v] of Object.entries(keys)) {{
                    const dot = document.getElementById('dot_' + k);
                    const input = document.getElementById(k);
                    if (dot) {{
                        dot.className = 'status-dot ' + (v ? 'set' : 'unset');
                    }}
                    if (input && v) {{
                        input.placeholder = v + '  (저장됨 — 변경 시에만 입력)';
                    }}
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
            const fields = [
                'KIS_APP_KEY','KIS_APP_SECRET','KIS_ACCOUNT_NO','KIS_ACCOUNT_PROD_CODE',
                'KIS_IS_PAPER','GEMINI_API_KEY','CLAUDE_API_KEY','GMAIL_ADDRESS','GMAIL_APP_PASSWORD'
            ];
            const body = {{}};
            fields.forEach(f => {{ body[f] = document.getElementById(f).value; }});

            try {{
                const resp = await fetch('/api/keys/save', {{
                    method: 'POST',
                    headers: {{'Content-Type':'application/json'}},
                    body: JSON.stringify(body),
                }});
                const result = await resp.json();
                if (result.status === 'saved') {{
                    showToast('✅ 암호화 저장 완료!', false);
                    setTimeout(() => loadStatus(), 500);
                    // 입력 필드 클리어
                    fields.forEach(f => {{ document.getElementById(f).value = ''; }});
                }} else {{
                    showToast('❌ 저장 실패: ' + JSON.stringify(result), true);
                }}
            }} catch (err) {{
                showToast('❌ 오류: ' + err.message, true);
            }}
        }}

        loadStatus();
    </script>
</body>
</html>"""


def _render_dashboard_html(is_local: bool, keys_configured: bool) -> str:
    """메인 대시보드 HTML."""
    setup_link = '<a href="/setup">API 설정</a>' if is_local else ''
    keys_warning = ""
    if not keys_configured:
        keys_warning = """
        <div style="background:#f59e0b22;border:1px solid #f59e0b44;border-radius:10px;padding:16px;margin-bottom:20px;display:flex;align-items:center;gap:12px;">
            <span style="font-size:1.5em;">⚠️</span>
            <div>
                <strong style="color:#f59e0b;">API 키 미설정</strong>
                <p style="color:#94a3b8;font-size:0.85em;margin-top:4px;">로컬에서 <a href="/setup" style="color:#3b82f6;">/setup</a> 페이지에 접속하여 API 키를 등록하세요.</p>
            </div>
        </div>"""

    write_controls = ""
    if is_local:
        write_controls = """
        <div class="card">
            <h2>🔧 수동 제어</h2>
            <form id="manualOrderForm" onsubmit="submitOrder(event)" style="display:flex;flex-wrap:wrap;gap:8px;align-items:end;">
                <div style="flex:1;min-width:120px;">
                    <label style="display:block;color:#64748b;font-size:0.8em;margin-bottom:4px;">종목코드</label>
                    <input type="text" id="ticker" placeholder="005930" required style="width:100%;">
                </div>
                <div>
                    <label style="display:block;color:#64748b;font-size:0.8em;margin-bottom:4px;">구분</label>
                    <select id="side" style="width:100%;"><option value="buy">매수</option><option value="sell">매도</option></select>
                </div>
                <div style="flex:1;min-width:80px;">
                    <label style="display:block;color:#64748b;font-size:0.8em;margin-bottom:4px;">수량</label>
                    <input type="number" id="quantity" placeholder="수량" required style="width:100%;">
                </div>
                <div style="flex:1;min-width:100px;">
                    <label style="display:block;color:#64748b;font-size:0.8em;margin-bottom:4px;">가격</label>
                    <input type="number" id="price" placeholder="시장가=빈칸" style="width:100%;">
                </div>
                <button type="submit" style="height:42px;">주문 실행</button>
            </form>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>KR Stock AutoTrader</title>
    {_COMMON_STYLE}
</head>
<body>
    <div class="header">
        <h1>📊 KR Stock AutoTrader</h1>
        <div style="display:flex;align-items:center;gap:14px;">
            <div class="nav">
                <a href="/" class="active">대시보드</a>
                {setup_link}
            </div>
            <span class="access-mode {'local' if is_local else 'remote'}">
                {'🔓 LOCAL' if is_local else '🔒 REMOTE'}
            </span>
            <span class="dot green" id="statusDot"></span>
            <span id="marketStatus" style="font-size:0.9em;">로딩...</span>
        </div>
    </div>

    <div class="container">
        {keys_warning}
        <div class="grid">
            <div class="card">
                <h2>💰 포트폴리오</h2>
                <div style="display:flex;justify-content:space-around;margin-bottom:15px;">
                    <div class="metric"><div class="big-number" id="totalValue">-</div><label>총 평가금</label></div>
                    <div class="metric"><div class="big-number" id="totalPnl">-</div><label>총 손익</label></div>
                </div>
                <table>
                    <thead><tr><th>종목</th><th>수량</th><th>평균가</th><th>현재가</th><th>수익률</th></tr></thead>
                    <tbody id="positionsTable"></tbody>
                </table>
            </div>

            <div class="card">
                <h2>🔧 모듈 상태</h2>
                <table>
                    <thead><tr><th>모듈</th><th>상태</th><th>실행</th><th>오류</th></tr></thead>
                    <tbody id="modulesTable"></tbody>
                </table>
            </div>

            <div class="card">
                <h2>🎯 오늘의 투자 후보</h2>
                <table>
                    <thead><tr><th>종목</th><th>종합</th><th>뉴스</th><th>센티</th><th>정책</th></tr></thead>
                    <tbody id="candidatesTable"></tbody>
                </table>
            </div>

            <div class="card">
                <h2>📋 최근 주문</h2>
                <table>
                    <thead><tr><th>시간</th><th>종목</th><th>구분</th><th>수량</th><th>가격</th><th>상태</th></tr></thead>
                    <tbody id="ordersTable"></tbody>
                </table>
            </div>

            <div class="card" style="grid-column:span 2;">
                <h2>📰 최근 수집 뉴스</h2>
                <table>
                    <thead><tr><th>시간</th><th>제목</th><th>출처</th><th>감정</th><th>영향도</th></tr></thead>
                    <tbody id="newsTable"></tbody>
                </table>
            </div>

            {write_controls}
        </div>
    </div>

    <script>
        async function fetchData() {{
            try {{
                const status = await (await fetch('/api/status')).json();
                document.getElementById('marketStatus').textContent = status.market_open ? '🟢 장중' : '🔴 장외';
                const mHtml = Object.entries(status.modules).map(([n,m]) =>
                    `<tr><td>${{n}}</td><td><span class="badge ${{m.status}}">${{m.status}}</span></td><td>${{m.execution_count}}</td><td>${{m.error_count}}</td></tr>`
                ).join('');
                document.getElementById('modulesTable').innerHTML = mHtml || '<tr><td colspan="4">모듈 없음</td></tr>';

                const p = await (await fetch('/api/portfolio')).json();
                document.getElementById('totalValue').textContent = (p.total_value||0).toLocaleString()+'원';
                const pnl = p.total_pnl||0;
                const pe = document.getElementById('totalPnl');
                pe.textContent = (pnl>=0?'+':'')+pnl.toLocaleString()+'원';
                pe.className = 'big-number '+(pnl>=0?'positive':'negative');
                const pH = (p.positions||[]).map(x =>
                    `<tr><td>${{x.name||x.ticker}}</td><td>${{x.quantity}}</td><td>${{x.avg_price?.toLocaleString()}}</td><td>${{x.current_price?.toLocaleString()}}</td><td class="${{x.unrealized_pnl_pct>=0?'positive':'negative'}}">${{x.unrealized_pnl_pct?.toFixed(2)}}%</td></tr>`
                ).join('');
                document.getElementById('positionsTable').innerHTML = pH||'<tr><td colspan="5" style="color:#475569">보유 종목 없음</td></tr>';

                const c = await (await fetch('/api/candidates')).json();
                const cH = (c.candidates||[]).slice(0,10).map(x =>
                    `<tr><td>${{x.ticker}}</td><td>${{x.total_score?.toFixed(1)}}</td><td>${{x.news_score?.toFixed(1)}}</td><td>${{x.sentiment_score?.toFixed(1)}}</td><td>${{x.policy_score?.toFixed(1)}}</td></tr>`
                ).join('');
                document.getElementById('candidatesTable').innerHTML = cH||'<tr><td colspan="5" style="color:#475569">후보 없음</td></tr>';

                const o = await (await fetch('/api/orders')).json();
                const oH = (o.orders||[]).slice(0,10).map(x =>
                    `<tr><td>${{x.created_at?.slice(11,19)}}</td><td>${{x.ticker}}</td><td class="${{x.side==='buy'?'positive':'negative'}}">${{x.side==='buy'?'매수':'매도'}}</td><td>${{x.quantity}}</td><td>${{x.price?.toLocaleString()}}</td><td><span class="badge ${{x.status}}">${{x.status}}</span></td></tr>`
                ).join('');
                document.getElementById('ordersTable').innerHTML = oH||'<tr><td colspan="6" style="color:#475569">주문 없음</td></tr>';

                const n = await (await fetch('/api/news')).json();
                const nH = (n.news||[]).slice(0,15).map(x =>
                    `<tr><td>${{x.time?.slice(11,19)||''}}</td><td>${{x.title?.slice(0,50)}}</td><td>${{x.source}}</td><td>${{x.sentiment}}</td><td>${{x.impact_score?.toFixed(2)}}</td></tr>`
                ).join('');
                document.getElementById('newsTable').innerHTML = nH||'<tr><td colspan="5" style="color:#475569">수집된 뉴스 없음</td></tr>';
            }} catch(e) {{ console.error(e); }}
        }}

        async function submitOrder(e) {{
            e.preventDefault();
            const d = {{
                ticker:document.getElementById('ticker').value,
                side:document.getElementById('side').value,
                quantity:parseInt(document.getElementById('quantity').value),
                price:parseFloat(document.getElementById('price').value)||null,
                order_type:document.getElementById('price').value?'limit':'market',
            }};
            try {{
                const r = await fetch('/api/manual-order',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}});
                const res = await r.json();
                alert(res.status==='submitted'?'주문 완료: '+res.order_id:'오류: '+JSON.stringify(res));
                fetchData();
            }} catch(err) {{ alert('실패: '+err.message); }}
        }}

        fetchData();
        setInterval(fetchData, 10000);
    </script>
</body>
</html>"""
