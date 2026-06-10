# V-MAP — Vehicle Monitoring & Analysis Platform (Implementation)

- **학번/이름**: 22411985 / 정수영 (sy.jeong100@gmail.com)
- **GitHub**: https://github.com/suyeong-jeong/V-MAP
- **단계**: 4. Implementation (Conceptualization → Analysis → Design 문서 기반 구현)

전기 자작차의 CAN 텔레메트리를 가상 시뮬레이터(SITL)로 생성하여 실시간 관제·원격 제어·분석하는
클라이언트–서버 시스템입니다. 서버는 **Python 3 표준 라이브러리만** 사용하여 어떤 환경에서도
추가 설치 없이 실행되며(실행 환경 독립성), 클라이언트는 단일 HTML 파일 웹 앱입니다.

---

## 1. 실행 방법

```bash
# 요구사항: Python 3.8+ (외부 패키지 불필요, pip 설치 없음)
cd server
python3 telemetry_server.py 8000
```

브라우저에서 `http://localhost:8000` 접속.
(과제 채점용 공개 서버를 여는 경우 같은 명령을 공인 IP 서버에서 실행 — 6/25까지 오픈)

### 기본 계정
| ID | PW | 권한 |
|---|---|---|
| `engineer` | `1234` | 엔지니어 (전체 탭) |
| `driver` | `1234` | 드라이버 (대시보드만) |
| `22411985` | `1234` | 엔지니어 |

회원가입(Register)으로 신규 계정 생성 가능. 비밀번호는 SHA-256 해시로 `users.json`에 저장.

---

## 2. 아키텍처 (OOP)

```
server/  (Python — Server/Simulator System, Design §2.2)
├ telemetry_server.py  TelemetryServer·ClientHandler  : HTTP+SSE 서버, 명령 수신/ACK
├ engine.py            SimulationEngine               : 10Hz 틱, 상태머신
│                       (SIMULATION_READY ↔ TELEMETRY_STREAMING ↔ EMERGENCY_HALT)
├ dynamics.py          VehicleDynamics·BatteryPack·SensorMockup·ScenarioManager
│                       : 차량 동역학(질량/항력/모터), 12셀 배터리, 센서 노이즈, 4개 시나리오
├ protocol.py          CanPacketBuilder               : 원시 CAN 모사 패킷
│                       STX|CMD|LEN|PAYLOAD|XOR체크섬|ETX (hex 스트림)
└ managers.py          SessionManager·LogManager      : 인증/세션, CSV 로깅·요약 통계

client/  (HTML+JS — Client/App System, Design §2.1)
└ index.html           LoginView·DashboardView·TelemetryView·ControlPanelView
                       VehicleState·BatteryCellData·ThresholdConfig·AlertController
                       AnalysisEngine·TelemetryParser·SimulatorConnector
                       LogManager(재생)·ReportExporter·LineChart·Gauge·PlaybackController
```

도메인 분석(Analysis §3)의 14개 클래스와 설계(Design §2)의 Class Diagram을 그대로 코드 클래스로
매핑했습니다. 통신은 서버가 원시 hex 패킷을 SSE로 송출하고, **클라이언트의 TelemetryParser가
체크섬 검증 후 물리값으로 파싱**하는 구조로, 설계의 Raw CAN Data 흐름을 충실히 재현합니다.

---

## 3. 유스케이스 ↔ 구현 매핑 (13/13)

| UC | 기능 | 구현 |
|---|---|---|
| 01 | Login | ID/PW 인증, 역할별 화면 전환, 실패 메시지·필드 초기화, 회원가입 |
| 02 | View Dashboard | 캔버스 아날로그 게이지(속도/RPM), SOC 바 — SSE 10Hz 실시간 |
| 03 | Generate Raw Data | 시나리오 기반 동역학 → CAN 패킷화(XOR 체크섬) → 스트리밍 |
| 04 | Monitor Telemetry | 전압/전류/온도 실시간 차트 + 장치별 데이터 테이블 |
| 05 | Send Commands | 모드/램프/스로틀/긴급정지 — ACK 피드백, HALT 중 명령 거부 사유 표시 |
| 06 | Save Drive Logs | LOG START/STOP → 서버측 CSV 기록(`logs/drive_*.csv`) |
| 07 | Playback Logs | 저장 로그 선택 재생 — 1x/2x/4x 배속, 시킹, 대시보드 재현 |
| 08 | Receive Alerts | 우선순위 정렬 경고 팝업(최상위 위험 상단), 알람 로그 기록 |
| 09 | Analyze Battery Cells | 12셀 막대그래프, 편차 계산, 이상 셀(7번 약셀) 강조 |
| 10 | Analyze Efficiency | 전비 km/kWh + 주행가능거리, 정차 시 분모 0 예외 처리 |
| 11 | Set Thresholds | 슬라이더 설정 → 서버 유효성 검사 → `thresholds.json` 영속화, 게이지 레드라인 연동 |
| 12 | Check Latency | 1초 주기 RTT 측정, Good/Fair/Poor 등급 상시 표시 |
| 13 | Export Reports | 통계 요약+속도 프로파일 포함 인쇄용 PDF 리포트, CSV 다운로드 |

---

## 4. API 요약

| Method | Path | 설명 |
|---|---|---|
| POST | `/api/login` `/api/register` `/api/logout` | 인증 |
| GET | `/api/stream` | SSE — 원시 CAN hex 패킷 10Hz |
| POST | `/api/command` | `{"cmd": "set_mode"\|"set_lights"\|"set_throttle"\|"emergency_stop"\|"reset"\|"load_scenario", "value": ...}` |
| GET/POST | `/api/thresholds` | 임계값 조회/저장(서버 유효성 검사) |
| POST | `/api/log/start` `/api/log/stop` | CSV 로깅 제어 |
| GET | `/api/logs`, `/api/logs/<file>?summary=1` | 로그 목록/내용/요약 통계 |
| GET | `/api/ping` | RTT 측정용 |

---

## 5. 테스트 결과 (통합 검증 완료)

- 로그인 성공/실패, 권한별 화면 분기 ✔
- aggressive 시나리오: 속도 ~99km/h, 전류 ~58A — 인코딩 범위 정상, 체크섬 검증 통과 ✔
- 긴급정지 → 스트리밍 중단·명령 거부 → 리셋 복귀 상태머신 ✔
- 임계값 9999 입력 거부(유효성), 120 저장·즉시 반영 ✔
- 12초 로깅 → 120행 CSV, 요약 통계(전비 7.26 km/kWh 등) 산출 ✔
