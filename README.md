# KR Stock AutoTrader

24시간 자동 한국 주식 매매 시스템 (KRX KOSPI/KOSDAQ)

## 주요 기능

| 모듈 | 설명 |
|------|------|
| **News Analysis** | 국내(네이버·RSS) + 해외(CNBC·Reuters·Yahoo 등) 뉴스 실시간 수집 |
| **Sentiment** | Gemini 2.5 Flash AI 기반 감정/영향도 분석 |
| **Policy** | 정책·규제 이벤트 모니터링 |
| **Trading Engine** | KIS API 연동 자동 매매 (모의/실전) |
| **Master Control** | 종합 판단 및 매매 실행 |
| **Watcher** | 포지션 모니터링 및 리스크 관리 |
| **Dashboard** | 웹 대시보드 (PC·모바일 반응형) |

## 아키텍처

```
main.py                     # 24시간 시스템 엔트리포인트
├── core/
│   ├── config.py            # 설정 관리 (YAML + 환경변수 + 암호화 볼트)
│   ├── database.py          # SQLite 비동기 DB (9 테이블)
│   ├── scheduler.py         # APScheduler 기반 KRX 시간 인식 스케줄러
│   ├── security.py          # Fernet AES-256 API 키 암호화
│   ├── events.py            # 이벤트 버스
│   └── base_module.py       # 플러그인 레지스트리
├── ai/
│   └── gemini_client.py     # Google Gemini 2.5 Flash 클라이언트
├── broker/
│   └── kis_api.py           # 한국투자증권 API 클라이언트
├── modules/
│   ├── news/                # 뉴스 수집 (RSS + 스크래핑)
│   ├── sentiment/           # 감정 분석
│   ├── policy/              # 정책 모니터링
│   ├── trading/             # 매매 엔진
│   ├── master/              # 마스터 컨트롤
│   ├── watcher/             # 워처
│   └── dashboard/           # 웹 대시보드
└── tests/
    └── test_system.py       # 통합 테스트
```

## 빠른 시작

### 1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 2. 시스템 시작

```bash
python main.py
```

### 3. API 키 설정

브라우저에서 `http://127.0.0.1:8080/setup` 접속 후 API 키 입력:

- **한국투자증권**: APP KEY, SECRET, 계좌번호
- **Google Gemini**: API KEY
- **Gmail** (선택): 일일 리포트 발송용

> 모든 API 키는 AES-256 암호화되어 로컬에만 저장됩니다.

### 4. 모바일 접속

같은 Wi-Fi 네트워크에서 `http://<PC_IP>:8080` 으로 접속
(예: `http://192.168.45.50:8080`)

## 대시보드

- 실시간 KST 시계 + 장 카운트다운
- 포트폴리오 현황 (평가금·손익)
- 감정 분포 도넛 차트 (Chart.js)
- 뉴스 피드 (국내/해외 탭 필터)
- 루머 피드 (신뢰도·검증 상태)
- 모듈 상태 + 스케줄러
- 수동 주문 (로컬 전용)
- 10초 자동 갱신

## 스케줄러

| 시간 | 작업 |
|------|------|
| 08:00 | 장전 데이터 수집 |
| 08:30 | 종목 선정 |
| 09:00~15:30 | 장중 매매 (30초 간격) |
| 15분 간격 | 정책 모니터링 |
| 16:30 | 일일 리포트 생성 |

## 기술 스택

- **Backend**: Python 3.11+, FastAPI, uvicorn, APScheduler
- **AI**: Google Gemini 2.5 Flash (google-genai SDK)
- **Broker**: 한국투자증권 Open API (httpx)
- **DB**: SQLite (aiosqlite)
- **Frontend**: Vanilla JS, Chart.js, CSS Glassmorphism
- **Security**: Fernet AES-256 (cryptography)

## 개발

```bash
# 테스트 실행
pytest tests/ -v

# 대시보드만 실행 (API 키 없이)
python run_dashboard.py

# 뉴스 수집 테스트
python test_collect.py

# AI 분석 테스트
python test_ai_analysis.py
```

## License

Private - All Rights Reserved
