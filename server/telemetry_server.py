# -*- coding: utf-8 -*-
"""
telemetry_server.py — TelemetryServer / ClientHandler

표준 라이브러리 http.server 기반.
- GET  /                     : 웹 클라이언트(index.html) 제공
- POST /api/login|logout|register
- GET  /api/stream           : SSE 실시간 텔레메트리 스트리밍 (원시 CAN 패킷 포함)
- GET  /api/status, /api/ping
- POST /api/command          : 제어 명령 (mode/lights/throttle/emergency/reset/scenario/sim)
- GET/POST /api/thresholds   : 경고 임계값 조회/설정 (UC11)
- POST /api/log/start|stop, GET /api/logs, /api/logs/<file>
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from engine import SimulationEngine
from managers import SessionManager, LogManager, LOG_DIR
from protocol import validate_packet, CMD_TELEMETRY
import struct

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_DIR = os.path.join(BASE_DIR, "client")
THRESHOLD_FILE = os.path.join(BASE_DIR, "server", "thresholds.json")

DEFAULT_THRESHOLDS = {
    "maxSpeed": 110.0,      # km/h
    "maxRpm": 5200,
    "maxMotorTemp": 95.0,   # ℃
    "maxInverterTemp": 85.0,
    "minSoc": 15.0,         # %
    "minCellVoltage": 3050, # mV
    "maxCellDelta": 60.0,   # mV
}

engine = SimulationEngine(tick_rate=10)
sessions = SessionManager()
logger = LogManager()
_threshold_lock = threading.Lock()


def load_thresholds():
    if os.path.exists(THRESHOLD_FILE):
        with open(THRESHOLD_FILE, encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(DEFAULT_THRESHOLDS)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_THRESHOLDS})
        return merged
    return dict(DEFAULT_THRESHOLDS)


def save_thresholds(data):
    with open(THRESHOLD_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


thresholds = load_thresholds()


def parse_telemetry_for_log(hex_pkt):
    """로깅용 — 서버 측에서도 자체 패킷을 파싱해 기록한다(무결성 자가 검증 겸용)."""
    res = validate_packet(hex_pkt)
    if not res or res[0] != CMD_TELEMETRY:
        return None
    p = res[1]
    s = struct.unpack(">HHHHhhhBBI", p)
    return {
        "speed": s[0] / 10.0, "rpm": s[1], "soc": s[2] / 10.0,
        "voltage": s[3] / 10.0, "current": s[4] / 10.0,
        "motor_temp": s[5] / 10.0, "inverter_temp": s[6] / 10.0,
        "mode": s[7], "odometer_m": s[9],
    }


def logging_pump():
    """로깅 활성 시 엔진 스냅샷을 주기적으로 CSV에 기록하는 백그라운드 스레드."""
    last_seq = -1
    while True:
        time.sleep(1.0 / engine.tick_rate)
        if not logger.is_logging:
            continue
        snap = engine.latest()
        if not snap or snap["seq"] == last_seq:
            continue
        last_seq = snap["seq"]
        parsed = parse_telemetry_for_log(snap["raw"])
        if parsed:
            parsed["time"] = round(snap["ts"], 3)
            logger.write_log(parsed)


class ClientHandler(BaseHTTPRequestHandler):
    """클라이언트 1요청을 처리하는 핸들러 (ThreadingHTTPServer가 스레드 분배)."""
    protocol_version = "HTTP/1.1"

    # ---------- 공통 유틸 ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode())
        except json.JSONDecodeError:
            return {}

    def _auth(self, role=None):
        token = self.headers.get("X-Auth-Token", "")
        sess = sessions.validate(token)
        if not sess:
            return None
        if role and sess["role"] != role:
            return None
        return sess

    def log_message(self, fmt, *args):  # 콘솔 소음 줄이기
        pass

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json({"server": "V-MAP", "engine": engine.status(),
                               "logging": logger.is_logging})
        if path == "/api/ping":
            # UC12 — 지연시간 측정용 경량 응답
            return self._json({"t": time.time()})
        if path == "/api/thresholds":
            with _threshold_lock:
                return self._json(thresholds)
        if path == "/api/stream":
            return self._sse_stream()
        if path == "/api/logs":
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            return self._json({"files": LogManager.list_logs(),
                               "logging": logger.is_logging})
        if path.startswith("/api/logs/"):
            if not self._auth():
                return self._json({"error": "unauthorized"}, 401)
            rows = LogManager.read_log(path.split("/api/logs/", 1)[1])
            if rows is None:
                return self._json({"error": "유효하지 않은 파일"}, 404)  # UC07 확장 2a
            return self._json({"rows": rows, "summary": LogManager.summarize(rows)})
        return self._json({"error": "not found"}, 404)

    def _serve_file(self, name, ctype):
        path = os.path.join(CLIENT_DIR, name)
        if not os.path.exists(path):
            return self._json({"error": "client not found"}, 404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self):
        """UC02/03/04 — Server-Sent Events로 원시 CAN 패킷을 실시간 스트리밍."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_seq = -1
        try:
            while True:
                snap = engine.latest()
                if snap and snap["seq"] != last_seq:
                    last_seq = snap["seq"]
                    payload = json.dumps(snap)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                time.sleep(1.0 / engine.tick_rate / 2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # 클라이언트 연결 종료

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()

        if path == "/api/login":
            res = sessions.login(str(body.get("id", "")), str(body.get("pw", "")))
            if not res:
                return self._json({"error": "인증 정보가 올바르지 않습니다"}, 401)
            return self._json(res)

        if path == "/api/register":
            ok, msg = sessions.register(str(body.get("id", "")).strip(),
                                        str(body.get("pw", "")),
                                        str(body.get("role", "")))
            return self._json({"ok": ok, "msg": msg}, 200 if ok else 400)

        if path == "/api/logout":
            sessions.logout(self.headers.get("X-Auth-Token", ""))
            return self._json({"ok": True})

        sess = self._auth()
        if not sess:
            return self._json({"error": "unauthorized"}, 401)

        if path == "/api/command":
            if sess["role"] != "engineer":
                return self._json({"error": "엔지니어 권한이 필요합니다"}, 403)
            return self._handle_command(body)

        if path == "/api/thresholds":
            if sess["role"] != "engineer":
                return self._json({"error": "엔지니어 권한이 필요합니다"}, 403)
            return self._set_thresholds(body)

        if path == "/api/log/start":
            if sess["role"] != "engineer":
                return self._json({"error": "엔지니어 권한이 필요합니다"}, 403)
            return self._json({"ok": True, "file": logger.start_logging()})

        if path == "/api/log/stop":
            return self._json({"ok": True, "result": logger.stop_logging()})

        return self._json({"error": "not found"}, 404)

    def _handle_command(self, body):
        """UC05 — 제어 명령 처리 후 ACK 회신."""
        cmd = body.get("cmd")
        if engine.status()["state"] == "EMERGENCY_HALT" and cmd not in ("reset", "sim_stop"):
            # UC05 확장 4b — 현재 상태로 인해 명령 수행 불가
            return self._json({"ack": False, "reason": "긴급 정지 상태입니다. RESET 후 재시도하세요."})
        ok, extra = True, {}
        if cmd == "sim_start":
            ok = engine.start_simulation() or True
        elif cmd == "sim_stop":
            engine.stop_simulation()
        elif cmd == "emergency_stop":
            engine.emergency_stop()
        elif cmd == "reset":
            engine.reset()
        elif cmd == "set_mode":
            ok = engine.set_mode(int(body.get("value", 0)))
        elif cmd == "set_lights":
            engine.set_lights(int(body.get("value", 0)))
        elif cmd == "set_throttle":
            v = body.get("value", None)
            engine.set_throttle(None if v is None else float(v))
        elif cmd == "load_scenario":
            ok = engine.load_scenario(str(body.get("value", "")))
            if not ok:
                extra = {"reason": "존재하지 않는 시나리오"}
        else:
            return self._json({"ack": False, "reason": "알 수 없는 명령"}, 400)
        return self._json({"ack": ok, "state": engine.status(), **extra})

    def _set_thresholds(self, body):
        """UC11 — 유효성 검사 후 임계값 저장."""
        global thresholds
        limits = {  # 물리적으로 가능한 범위 (UC11 확장 5a)
            "maxSpeed": (10, 300), "maxRpm": (500, 20000),
            "maxMotorTemp": (30, 200), "maxInverterTemp": (30, 200),
            "minSoc": (0, 90), "minCellVoltage": (2500, 4200),
            "maxCellDelta": (5, 500),
        }
        new = dict(thresholds)
        for k, v in body.items():
            if k not in limits:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                return self._json({"ok": False, "msg": f"{k}: 유효하지 않은 수치입니다"}, 400)
            lo, hi = limits[k]
            if not (lo <= v <= hi):
                return self._json({"ok": False, "msg": f"{k}: 허용 범위({lo}~{hi})를 벗어났습니다"}, 400)
            new[k] = v
        with _threshold_lock:
            thresholds = new
            save_thresholds(thresholds)
        return self._json({"ok": True, "thresholds": thresholds})


def main(host="0.0.0.0", port=8000):
    engine.state = "CLIENT_LISTENING"
    threading.Thread(target=logging_pump, daemon=True).start()
    engine.start_simulation()  # 서버 가동과 동시에 시뮬레이션 스트리밍 시작
    server = ThreadingHTTPServer((host, port), ClientHandler)
    print(f"[V-MAP] TelemetryServer listening on http://localhost:{port}")
    print(f"[V-MAP] 기본 계정 — engineer/1234 (엔지니어), driver/1234 (드라이버)")
    print(f"[V-MAP] 로그 저장 경로: {LOG_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[V-MAP] 서버 종료")


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    main(port=port)
