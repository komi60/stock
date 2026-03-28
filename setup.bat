@echo off
chcp 65001 > nul
echo ============================================================
echo   Upbit Crypto AutoTrader - Windows 초기 설정
echo ============================================================
echo.

:: Python 설치 확인
python --version > nul 2>&1
if errorlevel 1 (
    echo [오류] Python이 설치되어 있지 않습니다.
    echo Python 3.11 이상을 설치하세요: https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python 확인:
python --version

:: pip 업그레이드
echo.
echo [1/3] pip 업그레이드 중...
python -m pip install --upgrade pip --quiet

:: 패키지 설치
echo.
echo [2/3] 패키지 설치 중... (수 분 소요)
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [경고] 일부 패키지 설치 실패. pandas-ta 없이도 동작 가능합니다.
)

:: config/.env 파일 생성 안내
echo.
echo [3/3] API 키 설정
if not exist "config\.env" (
    echo config\.env 파일을 생성합니다...
    (
        echo GEMINI_API_KEY=여기에_Gemini_API_키_입력
        echo UPBIT_ACCESS_KEY=여기에_업비트_Access_키_입력
        echo UPBIT_SECRET_KEY=여기에_업비트_Secret_키_입력
    ) > config\.env
    echo.
    echo [!] config\.env 파일이 생성되었습니다.
    echo     메모장으로 열어 API 키를 입력하세요:
    echo     - Gemini API 키: https://aistudio.google.com/app/apikey
    echo     - 업비트 API 키: https://upbit.com/mypage/open_api_management
    echo.
    start notepad config\.env
) else (
    echo config\.env 파일이 이미 존재합니다.
)

echo.
echo ============================================================
echo   설정 완료! run.bat 을 실행하여 시작하세요.
echo ============================================================
pause
