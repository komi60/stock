@echo off
chcp 65001 > nul
title Upbit Crypto AutoTrader

echo ============================================================
echo   Upbit Crypto AutoTrader 시작
echo   종료하려면 이 창을 닫거나 Ctrl+C 를 누르세요
echo ============================================================
echo.

:: Python 확인
python --version > nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되지 않았습니다. setup.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: .env 파일 확인
if not exist "config\.env" (
    echo [오류] config\.env 파일이 없습니다. setup.bat 을 먼저 실행하세요.
    pause
    exit /b 1
)

:: 기존 프로세스 종료 (포트 8080)
echo 포트 8080 확인 중...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8080 ^| findstr LISTENING') do (
    echo 기존 프로세스 종료: PID %%a
    taskkill /PID %%a /F > nul 2>&1
)

echo.
echo 시스템 시작 중...
echo 대시보드: http://localhost:8080
echo.

python main.py

echo.
echo 시스템이 종료되었습니다.
pause
