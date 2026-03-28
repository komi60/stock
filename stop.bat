@echo off
chcp 65001 > nul
echo AutoTrader 종료 중...

:: main.py 프로세스 찾아서 종료
for /f "tokens=2" %%a in ('tasklist /fi "imagename eq python.exe" /fo list ^| findstr "PID"') do (
    wmic process where "ProcessId=%%a" get CommandLine 2>nul | findstr "main.py" > nul
    if not errorlevel 1 (
        echo PID %%a 종료
        taskkill /PID %%a /F > nul 2>&1
    )
)

echo 완료.
timeout /t 2 > nul
