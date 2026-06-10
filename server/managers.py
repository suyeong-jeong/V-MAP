# -*- coding: utf-8 -*-
"""
managers.py — SessionManager(인증/권한), LogManager(주행 로그 기록/재생/통계)
"""
import csv
import hashlib
import json
import os
import secrets
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
USER_FILE = os.path.join(BASE_DIR, "server", "users.json")

LOG_FIELDS = ["time", "speed", "rpm", "soc", "voltage", "current",
              "motor_temp", "inverter_temp", "mode", "odometer_m"]


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(("vmap::" + pw).encode()).hexdigest()


class SessionManager:
    """UC01 — 사용자 인증, 권한(Driver/Engineer) 식별, 세션 토큰 관리."""

    def __init__(self):
        self._sessions = {}  # token -> {"id":..., "role":...}
        self._lock = threading.Lock()
        self.users = self._load_users()

    def _load_users(self):
        if os.path.exists(USER_FILE):
            with open(USER_FILE, encoding="utf-8") as f:
                return json.load(f)
        users = {  # 기본 계정 (과제 시연용)
            "driver":   {"pw": _hash_pw("1234"), "role": "driver"},
            "engineer": {"pw": _hash_pw("1234"), "role": "engineer"},
            "22411985": {"pw": _hash_pw("1234"), "role": "engineer"},
        }
        self._save_users(users)
        return users

    def _save_users(self, users):
        with open(USER_FILE, "w", encoding="utf-8") as f:
            json.dump(users, f, indent=2, ensure_ascii=False)

    def register(self, user_id: str, pw: str, role: str):
        if not user_id or not pw or role not in ("driver", "engineer"):
            return False, "입력값이 올바르지 않습니다"
        with self._lock:
            if user_id in self.users:
                return False, "이미 존재하는 ID입니다"
            self.users[user_id] = {"pw": _hash_pw(pw), "role": role}
            self._save_users(self.users)
        return True, "가입 완료"

    def login(self, user_id: str, pw: str):
        user = self.users.get(user_id)
        if not user or user["pw"] != _hash_pw(pw):
            return None  # 인증 실패 (UC01 확장 3a)
        token = secrets.token_hex(16)
        with self._lock:
            self._sessions[token] = {"id": user_id, "role": user["role"]}
        return {"token": token, "id": user_id, "role": user["role"]}

    def logout(self, token: str):
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def validate(self, token: str):
        with self._lock:
            return self._sessions.get(token)


class LogManager:
    """UC06/07/13 — 주행 데이터 CSV 기록, 로그 목록/재생 로드, 통계 산출."""

    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.is_logging = False
        self.log_file_path = None
        self._fh = None
        self._writer = None
        self._lock = threading.Lock()
        self._row_count = 0

    def start_logging(self) -> str:
        with self._lock:
            if self.is_logging:
                return os.path.basename(self.log_file_path)
            name = time.strftime("drive_%Y%m%d_%H%M%S.csv")
            self.log_file_path = os.path.join(LOG_DIR, name)
            self._fh = open(self.log_file_path, "w", newline="", encoding="utf-8")
            self._writer = csv.DictWriter(self._fh, fieldnames=LOG_FIELDS)
            self._writer.writeheader()
            self._row_count = 0
            self.is_logging = True
            return name

    def write_log(self, state: dict):
        """실시간 상태 데이터를 버퍼에 기록 (주기적 flush로 I/O 부하 분산)."""
        with self._lock:
            if not self.is_logging:
                return
            self._writer.writerow({k: state.get(k, "") for k in LOG_FIELDS})
            self._row_count += 1
            if self._row_count % 20 == 0:  # UC06 step3 — 일정 간격 물리 기록
                self._fh.flush()

    def stop_logging(self):
        with self._lock:
            if not self.is_logging:
                return None
            self.is_logging = False
            self._fh.flush()
            self._fh.close()
            name = os.path.basename(self.log_file_path)
            rows = self._row_count
            self._fh = self._writer = None
            return {"file": name, "rows": rows}

    @staticmethod
    def list_logs():
        files = []
        for n in sorted(os.listdir(LOG_DIR), reverse=True):
            if n.endswith(".csv"):
                p = os.path.join(LOG_DIR, n)
                files.append({"name": n, "size": os.path.getsize(p),
                              "mtime": int(os.path.getmtime(p))})
        return files

    @staticmethod
    def read_log(name: str):
        """경로 탈출 방지 후 로그 파일 행 목록 반환 (UC07 재생용)."""
        if "/" in name or "\\" in name or ".." in name:
            return None
        path = os.path.join(LOG_DIR, name)
        if not os.path.exists(path):
            return None
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    @staticmethod
    def summarize(rows):
        """UC13 — 리포트용 통계치(평균/최대 속도, 최대 온도, 소비 에너지, 전비) 산출."""
        if not rows:
            return None
        def col(k):
            out = []
            for r in rows:
                try:
                    out.append(float(r[k]))
                except (ValueError, KeyError):
                    pass
            return out
        speeds, m_temps = col("speed"), col("motor_temp")
        volts, amps, socs = col("voltage"), col("current"), col("soc")
        t = col("time")
        duration = (t[-1] - t[0]) if len(t) > 1 else 0
        dist_km = 0.0
        od = col("odometer_m")
        if len(od) > 1:
            dist_km = max(0.0, od[-1] - od[0]) / 1000.0
        energy_kwh = 0.0
        for i in range(1, min(len(volts), len(amps), len(t))):
            dt = max(0.0, t[i] - t[i - 1])
            energy_kwh += volts[i] * amps[i] * dt / 3.6e6
        return {
            "rows": len(rows),
            "duration_s": round(duration, 1),
            "distance_km": round(dist_km, 3),
            "avg_speed": round(sum(speeds) / len(speeds), 1) if speeds else 0,
            "max_speed": round(max(speeds), 1) if speeds else 0,
            "max_motor_temp": round(max(m_temps), 1) if m_temps else 0,
            "soc_used": round(socs[0] - socs[-1], 2) if len(socs) > 1 else 0,
            "energy_kwh": round(energy_kwh, 4),
            "efficiency_km_kwh": round(dist_km / energy_kwh, 2) if energy_kwh > 1e-6 else 0,
        }
