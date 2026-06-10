# -*- coding: utf-8 -*-
"""
dynamics.py — 가상 차량 동역학 / 센서 목업 / 주행 시나리오 (SITL)

Design 문서 2.2절의 VehicleDynamics, SensorMockup, ScenarioManager 클래스 구현.
실제 하드웨어 없이 물리 법칙 기반으로 속도·RPM·배터리·온도를 연산한다.
"""
import math
import random


class VehicleDynamics:
    """질량/공기저항 기반의 간단한 종방향 차량 동역학 모델 (정출력 제한 모터)."""

    GEAR_RATIO = 4.2          # 감속비
    WHEEL_RADIUS = 0.26       # 휠 반경 (m)
    MAX_MOTOR_TORQUE = 95.0   # 최대 모터 토크 (Nm)
    MAX_MOTOR_POWER = 16000.0 # 최대 모터 기계 출력 (W)

    def __init__(self):
        self.mass = 320.0             # 자작차 질량 (kg)
        self.drag_coefficient = 0.55  # 공기저항 계수 x 전면적 근사
        self.rolling_resist = 40.0    # 구름저항 (N)
        self.current_throttle = 0.0   # 0.0 ~ 1.0
        self.speed_ms = 0.0           # 현재 속도 (m/s)
        self.odometer_m = 0.0         # 누적 주행거리 (m)
        self.braking = False

    def set_throttle(self, value: float):
        self.current_throttle = max(0.0, min(1.0, value))

    def _motor_torque(self, mode_power_scale: float) -> float:
        """저속: 토크 제한 / 고속: 출력 제한 (정출력 영역)."""
        omega = max(1.0, self.calculate_rpm() * 2 * math.pi / 60)
        t_max = min(self.MAX_MOTOR_TORQUE, self.MAX_MOTOR_POWER / omega)
        return t_max * self.current_throttle * mode_power_scale

    def update(self, dt: float, mode_power_scale: float = 1.0):
        """dt초 동안 물리 상태 적분."""
        torque = self._motor_torque(mode_power_scale)
        drive_force = torque * self.GEAR_RATIO / self.WHEEL_RADIUS
        drag = self.drag_coefficient * self.speed_ms ** 2
        brake = 2200.0 if self.braking else 0.0
        roll = self.rolling_resist if self.speed_ms > 0.1 else 0.0
        accel = (drive_force - drag - roll - brake) / self.mass
        self.speed_ms = max(0.0, self.speed_ms + accel * dt)
        self.odometer_m += self.speed_ms * dt

    def calculate_speed(self) -> float:
        """현재 차량 속도 (km/h)."""
        return self.speed_ms * 3.6

    def calculate_rpm(self) -> int:
        """휠 속도와 감속비 기반 모터 RPM 연산."""
        wheel_rps = self.speed_ms / (2 * math.pi * self.WHEEL_RADIUS)
        return int(wheel_rps * 60 * self.GEAR_RATIO)

    def electrical_power_w(self, mode_power_scale: float = 1.0) -> float:
        """모터 소비 전력(W) 근사 — 토크 x 각속도 / 효율 + 보조 전장 부하."""
        omega = self.calculate_rpm() * 2 * math.pi / 60
        torque = self._motor_torque(mode_power_scale)
        mech = torque * omega
        return mech / 0.88 + 180.0  # 효율 88%, 상시 전장 180W


class BatteryPack:
    """12셀 배터리 팩 — SOC, 셀 전압 분포, 온도 시뮬레이션."""

    CELL_COUNT = 12
    CAPACITY_WH = 5200.0  # 팩 용량 (Wh)

    def __init__(self):
        self.soc = 100.0  # %
        self.temp = 26.0  # 팩 온도 (℃)
        # 셀 별 고유 편차(불균형 모사)
        self._cell_bias = [random.uniform(-12, 12) for _ in range(self.CELL_COUNT)]
        self._cell_bias[6] = -38.0  # 7번 셀(C7)을 의도적으로 약한 셀로 설정 (분석 시연용)

    def drain(self, power_w: float, dt: float):
        """소모 전력에 따른 배터리 잔량 감소 및 발열."""
        used_wh = power_w * dt / 3600.0
        self.soc = max(0.0, self.soc - used_wh / self.CAPACITY_WH * 100.0)
        # 전류량에 비례한 발열, 자연 냉각
        self.temp += (power_w / 9000.0) * dt - (self.temp - 24.0) * 0.012 * dt
        if self.soc <= 0.5:  # 상시 가동 데모 — 방전 시 충전 완료 팩 교체 모사
            self.soc = 100.0
            self.temp = 26.0

    def pack_voltage(self) -> float:
        """SOC 기반 팩 전압 (3.0V ~ 4.2V/셀 선형 근사)."""
        cell_v = 3.0 + 1.2 * (self.soc / 100.0)
        return cell_v * self.CELL_COUNT

    def pack_current(self, power_w: float) -> float:
        v = self.pack_voltage()
        return power_w / v if v > 1 else 0.0

    def cell_voltages_mv(self):
        base = self.pack_voltage() / self.CELL_COUNT * 1000.0
        return [base + b for b in self._cell_bias]


class SensorMockup:
    """가상 센서 — 동역학 데이터에 측정 노이즈를 부여해 실제 센서처럼 만든다."""

    def __init__(self):
        self.noise_level = 1.0
        self.base_temperature = 26.0
        self.motor_temp = 28.0
        self.inverter_temp = 27.0

    def apply_random_noise(self, value: float, scale: float = 0.4) -> float:
        return value + random.gauss(0, scale * self.noise_level)

    def update_temps(self, power_w: float, dt: float):
        """부하에 따른 모터/인버터 온도 변화."""
        self.motor_temp += (power_w / 6500.0) * dt - (self.motor_temp - self.base_temperature) * 0.02 * dt
        self.inverter_temp += (power_w / 8000.0) * dt - (self.inverter_temp - self.base_temperature) * 0.025 * dt

    def generate_motor_temp(self) -> float:
        return self.apply_random_noise(self.motor_temp, 0.15)

    def generate_inverter_temp(self) -> float:
        return self.apply_random_noise(self.inverter_temp, 0.15)


class ScenarioManager:
    """주행 시나리오 로드/진행 — 시간에 따른 목표 스로틀 프로파일을 제공한다."""

    SCENARIOS = {
        "city": "시내 주행 (가감속 반복)",
        "highway": "고속 주행 (고부하 지속)",
        "endurance": "내구 주행 (효율 위주)",
        "aggressive": "스포츠 주행 (급가속/과부하)",
    }

    def __init__(self):
        self.active_scenario = "city"
        self.duration_timer = 0.0

    def load_scenario(self, name: str) -> bool:
        if name in self.SCENARIOS:
            self.active_scenario = name
            self.duration_timer = 0.0
            return True
        return False

    def target_throttle(self, dt: float) -> float:
        """현재 시나리오와 경과 시간에 따른 목표 스로틀 산출."""
        self.duration_timer += dt
        t = self.duration_timer
        if self.active_scenario == "city":
            # 가속-순항-감속 사이클 (주기 24초)
            phase = t % 24
            if phase < 8:
                return 0.55
            if phase < 14:
                return 0.30
            if phase < 18:
                return 0.05
            return 0.0
        if self.active_scenario == "highway":
            return 0.72 + 0.08 * math.sin(t / 7.0)
        if self.active_scenario == "endurance":
            return 0.34 + 0.04 * math.sin(t / 11.0)
        if self.active_scenario == "aggressive":
            phase = t % 14
            return 0.97 if phase < 6 else (0.15 if phase < 9 else 0.65)
        return 0.0
