"""모듈 6: 감시자 모듈 (Watcher Module).

각 모듈(1,2,3,4)의 예측 정확도 및 성능을 백그라운드 분석.
하루 1회 장 마감 후 성능 평가 결과를 Gmail SMTP로 발송.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import aiosmtplib
from loguru import logger

from ai.gemini_client import GeminiClient
from core.base_module import BaseModule, PluginRegistry
from core.config import EmailConfig
from core.data_models import DailyPerformanceReport, ModuleHealthReport
from core.database import get_db


class WatcherModule(BaseModule):
    """감시자 모듈: 모듈별 성능 추적 및 일일 리포트 발송."""

    def __init__(
        self,
        config: dict[str, Any],
        registry: PluginRegistry,
        email_config: EmailConfig,
        gemini: GeminiClient,
    ):
        super().__init__("watcher", config)
        self._registry = registry
        self._email_config = email_config
        self._gemini = gemini
        self._daily_report: DailyPerformanceReport | None = None

    async def initialize(self) -> None:
        logger.info("감시자 모듈 초기화 완료")

    async def execute(self) -> DailyPerformanceReport:
        """장 마감 후 일일 성능 분석 및 리포트 발송."""
        logger.info("=== 일일 성능 분석 시작 ===")

        # 1. 모듈별 상태 수집
        module_reports = self._collect_module_reports()

        # 2. 예측 정확도 계산
        accuracy_data = await self._calculate_accuracy()

        # 3. 매매 성과 집계
        trade_stats = await self._aggregate_trade_stats()

        # 4. 일일 리포트 생성
        self._daily_report = DailyPerformanceReport(
            date=datetime.now(),
            total_trades=trade_stats.get("total", 0),
            winning_trades=trade_stats.get("wins", 0),
            losing_trades=trade_stats.get("losses", 0),
            total_pnl=trade_stats.get("pnl", 0),
            total_pnl_pct=trade_stats.get("pnl_pct", 0),
            max_drawdown_pct=trade_stats.get("max_dd", 0),
            module_reports=module_reports,
        )

        # 5. AI 요약 리포트 생성
        report_html = await self._generate_report_html(accuracy_data, trade_stats)

        # 6. 메일 발송
        await self._send_email_report(report_html)

        # 7. DB 저장
        await self._save_daily_report()

        logger.info("일일 성능 분석 및 리포트 발송 완료")
        return self._daily_report

    async def shutdown(self) -> None:
        logger.info("감시자 모듈 종료")

    # ─── 모듈 상태 수집 ────────────────────────────────────

    def _collect_module_reports(self) -> list[ModuleHealthReport]:
        """등록된 모든 모듈의 상태 리포트 수집."""
        reports = []
        for name, module in self._registry.get_all().items():
            if name == "master":
                continue
            reports.append(module.get_status())
        return reports

    # ─── 예측 정확도 계산 ──────────────────────────────────

    async def _calculate_accuracy(self) -> dict[str, Any]:
        """각 모듈의 예측 정확도 계산.

        오늘 아침에 선정한 후보 종목 vs 실제 주가 변동을 비교.
        """
        accuracy = {}
        today = datetime.now().strftime("%Y-%m-%d")

        try:
            db = await get_db()

            # 오늘 선정된 후보 종목 조회
            cursor = await db.execute(
                "SELECT ticker, news_score, sentiment_score, policy_score, signal "
                "FROM stock_candidates WHERE date = ?",
                (today,),
            )
            candidates = await cursor.fetchall()

            if not candidates:
                await db.close()
                return accuracy

            total = len(candidates)
            correct_direction = 0

            for row in candidates:
                ticker = row[0]
                signal = row[4]

                # 오늘의 가격 변동 확인 (DB에 기록된 주문/포지션 기반)
                cursor2 = await db.execute(
                    "SELECT side, price, filled_price FROM orders "
                    "WHERE ticker = ? AND date(created_at) = ? AND status = 'filled'",
                    (ticker, today),
                )
                orders = await cursor2.fetchall()

                if orders:
                    # 주문이 있으면 수익/손실로 판단
                    for order in orders:
                        if order[2] and order[1]:
                            if order[0] == "buy" and order[2] > order[1]:
                                correct_direction += 1
                            elif order[0] == "sell" and order[2] < order[1]:
                                correct_direction += 1

            accuracy["overall"] = {
                "total_candidates": total,
                "correct_predictions": correct_direction,
                "accuracy_pct": (correct_direction / total * 100) if total > 0 else 0,
            }

            await db.close()
        except Exception as e:
            logger.error(f"정확도 계산 오류: {e}")

        return accuracy

    # ─── 매매 성과 집계 ────────────────────────────────────

    async def _aggregate_trade_stats(self) -> dict[str, Any]:
        """오늘의 매매 성과 집계."""
        today = datetime.now().strftime("%Y-%m-%d")
        stats = {"total": 0, "wins": 0, "losses": 0, "pnl": 0.0, "pnl_pct": 0.0, "max_dd": 0.0}

        try:
            db = await get_db()
            cursor = await db.execute(
                "SELECT side, quantity, price, filled_price, status FROM orders "
                "WHERE date(created_at) = ?",
                (today,),
            )
            orders = await cursor.fetchall()

            for order in orders:
                if order[4] != "filled":
                    continue
                stats["total"] += 1
                if order[0] == "sell" and order[3] and order[2]:
                    pnl = (order[3] - order[2]) * order[1]
                    stats["pnl"] += pnl
                    if pnl > 0:
                        stats["wins"] += 1
                    else:
                        stats["losses"] += 1

            await db.close()
        except Exception as e:
            logger.error(f"매매 성과 집계 오류: {e}")

        return stats

    # ─── 리포트 HTML 생성 ──────────────────────────────────

    async def _generate_report_html(
        self, accuracy: dict, trade_stats: dict
    ) -> str:
        """AI 기반 일일 리포트 HTML 생성."""
        # 모듈 상태 요약
        module_summary = []
        for report in self._daily_report.module_reports:
            module_summary.append({
                "name": report.module_name,
                "status": report.status.value,
                "executions": report.execution_count,
                "errors": report.error_count,
                "last_error": report.last_error,
            })

        report_data = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "trade_stats": trade_stats,
            "accuracy": accuracy,
            "modules": module_summary,
        }

        try:
            ai_report = await self._gemini.generate_daily_report(report_data)
            return ai_report
        except Exception as e:
            logger.error(f"AI 리포트 생성 실패: {e}")
            # 폴백: 기본 HTML 생성
            return self._generate_fallback_html(report_data)

    def _generate_fallback_html(self, data: dict) -> str:
        """AI 실패 시 기본 HTML 리포트."""
        stats = data.get("trade_stats", {})
        accuracy = data.get("accuracy", {})
        modules = data.get("modules", [])

        modules_html = ""
        for m in modules:
            status_color = "green" if m["status"] == "idle" else "red"
            modules_html += f"""
            <tr>
                <td>{m['name']}</td>
                <td style="color:{status_color}">{m['status']}</td>
                <td>{m['executions']}</td>
                <td>{m['errors']}</td>
            </tr>"""

        overall = accuracy.get("overall", {})

        return f"""
        <html>
        <body style="font-family:Arial; padding:20px; background:#f5f5f5;">
            <div style="max-width:600px; margin:0 auto; background:white; padding:30px; border-radius:10px;">
                <h1 style="color:#1a73e8;">📊 일일 투자 리포트</h1>
                <p style="color:#666;">{data.get('date', '')}</p>

                <h2>📈 매매 성과</h2>
                <table style="width:100%; border-collapse:collapse;">
                    <tr><td>총 매매</td><td><b>{stats.get('total', 0)}건</b></td></tr>
                    <tr><td>수익 매매</td><td style="color:red"><b>{stats.get('wins', 0)}건</b></td></tr>
                    <tr><td>손실 매매</td><td style="color:blue"><b>{stats.get('losses', 0)}건</b></td></tr>
                    <tr><td>총 손익</td><td><b>{stats.get('pnl', 0):,.0f}원</b></td></tr>
                </table>

                <h2>🎯 예측 정확도</h2>
                <p>후보 종목: {overall.get('total_candidates', 0)}개 |
                   적중: {overall.get('correct_predictions', 0)}개 |
                   정확도: {overall.get('accuracy_pct', 0):.1f}%</p>

                <h2>🔧 모듈 상태</h2>
                <table style="width:100%; border-collapse:collapse; border:1px solid #ddd;">
                    <tr style="background:#f0f0f0;">
                        <th style="padding:8px; text-align:left;">모듈</th>
                        <th>상태</th><th>실행 수</th><th>오류 수</th>
                    </tr>
                    {modules_html}
                </table>

                <hr style="margin:20px 0;">
                <p style="color:#999; font-size:12px;">KR Stock AutoTrader - 자동 생성 리포트</p>
            </div>
        </body>
        </html>
        """

    # ─── 이메일 발송 ───────────────────────────────────────

    async def _send_email_report(self, html_content: str) -> None:
        """Gmail SMTP를 통한 리포트 메일 발송."""
        if not self._email_config.address or not self._email_config.app_password:
            logger.warning("이메일 설정 미완료. 리포트 발송 건너뜀.")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"[AutoTrader] 일일 리포트 - {datetime.now().strftime('%Y-%m-%d')}"
            msg["From"] = self._email_config.address
            msg["To"] = self._email_config.address  # 자신에게 발송

            msg.attach(MIMEText(html_content, "html", "utf-8"))

            await aiosmtplib.send(
                msg,
                hostname=self._email_config.smtp_server,
                port=self._email_config.smtp_port,
                start_tls=True,
                username=self._email_config.address,
                password=self._email_config.app_password,
            )
            logger.info("일일 리포트 메일 발송 완료")
        except Exception as e:
            logger.error(f"메일 발송 실패: {e}")

    # ─── DB 저장 ───────────────────────────────────────────

    async def _save_daily_report(self) -> None:
        if not self._daily_report:
            return
        try:
            db = await get_db()
            await db.execute(
                """INSERT OR REPLACE INTO daily_reports
                   (date, total_trades, winning_trades, losing_trades,
                    total_pnl, total_pnl_pct, max_drawdown_pct, module_reports)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self._daily_report.date.strftime("%Y-%m-%d"),
                    self._daily_report.total_trades,
                    self._daily_report.winning_trades,
                    self._daily_report.losing_trades,
                    self._daily_report.total_pnl,
                    self._daily_report.total_pnl_pct,
                    self._daily_report.max_drawdown_pct,
                    json.dumps(
                        [r.model_dump() for r in self._daily_report.module_reports],
                        default=str,
                    ),
                ),
            )
            await db.commit()
            await db.close()
        except Exception as e:
            logger.error(f"일일 리포트 DB 저장 오류: {e}")
