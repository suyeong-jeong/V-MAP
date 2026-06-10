# -*- coding: utf-8 -*-
"""
engine.py — SimulationEngine (Design 문서 4.2 Server State Machine 구현)

상태 전이:
  SERVER_STANDBY → CLIENT_LISTENING → SIMULATION_READY → TELEMETRY_STREAMING
  TELEMETRY_STREAMING --EMERGENCY_STOP--> EMERGENCY_HALT --reset--> SIMULATION_READY
"""
import threading
import time

from dynamics import VehicleDynamics, BatteryPack, SensorMockup, ScenarioManager
from protocol import CanPacketBuilder

MODE_NAMES = {0: "NORMAL", 1: "ECO", 2: "SPORT"}
MODE_POWER = {0: 1.0, 1: 0.65, 2: 1.25}


class SimulationEngine:
    """틱 레이트 기반으로 차량 동역학·센서·패킷 생성을 총괄하는 서버 코어."""

    def __init__(self, tick_rate: int = 10):
        self.tick_rate = tick_rate            # 데이터 갱신 주기 (Hz)
        self.is_running = False
        self.state = "SIMULATION_READY"       # 서버 상태 머신의 현재 상태
        self.dynamics = VehicleDynamics()
        self.battery = BatteryPack()
        self.sensors = SensorMockup()
        self.scenario = ScenarioManager()
        self.builder = CanPacketBuilder()
        self.drive_mode = 0                   # 0 Normal / 1 Eco / 2 Sport
        self.lights = 0                       # 비트필드
        self.manual_throttle = None           # None이면 시나리오 자동 주행
        self._lock = threading.Lock()
        self._latest = None                   # 마지막 생성 스냅샷
        self._thread = None

    # ---------- 제어 명령 (UC05 Send Commands) ----------
    def start_simulation(self):
        with self._lock:
            if self.is_running:
                return False
            self.is_running = True
            self.state = "TELEMETRY_STREAMING"
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop_simulation(self):
        with self._lock:
            self.is_running = False
            self.state = "SIMULATION_READY"

    def emergency_stop(self):
        """UC07 — 긴급 정지: 스로틀 차단 + 제동, 상태 머신 EMERGENCY_HALT 전이."""
        with self._lock:
            self.manual_throttle = 0.0
            self.dynamics.set_throttle(0.0)
            self.dynamics.braking = True
            self.state = "EMERGENCY_HALT"

    def reset(self):
        with self._lock:
            self.dynamics.braking = False
            self.manual_throttle = None
            if self.is_running:
                self.state = "TELEMETRY_STREAMING"
            else:
                self.state = "SIMULATION_READY"

    def set_mode(self, mode: int):
        if mode in MODE_NAMES:
            self.drive_mode = mode
            return True
        return False

    def set_lights(self, bitfield: int):
        self.lights = bitfield & 0xFF

    def set_throttle(self, value):
        """수동 제어 모드. None을 주면 시나리오 자동 주행으로 복귀."""
        self.manual_throttle = None if value is None else max(0.0, min(1.0, float(value)))

    def load_scenario(self, name: str) -> bool:
        return self.scenario.load_scenario(name)

    # ---------- 메인 루프 ----------
    def _loop(self):
        dt = 1.0 / self.tick_rate
        while True:
            with self._lock:
                if not self.is_running:
                    break
                try:
                    self.update_tick(dt)
                except Exception as e:  # 1틱 오류로 스트리밍 전체가 중단되지 않도록 보호
                    print(f"[engine] tick error: {e}")
            time.sleep(dt)

    def update_tick(self, dt: float):
        """틱레이트에 맞춰 차량 동역학 및 센서 상태 갱신 후 원시 패킷 생성."""
        halted = self.state == "EMERGENCY_HALT"
        if halted:
            throttle = 0.0
        elif self.manual_throttle is not None:
            throttle = self.manual_throttle
        else:
            throttle = self.scenario.target_throttle(dt)

        scale = MODE_POWER[self.drive_mode]
        self.dynamics.set_throttle(throttle)
        self.dynamics.update(dt, scale)

        power = self.dynamics.electrical_power_w(scale)
        self.battery.drain(power, dt)
        self.sensors.update_temps(power, dt)

        speed = max(0.0, self.sensors.apply_random_noise(self.dynamics.calculate_speed(), 0.25))
        rpm = max(0, int(self.sensors.apply_random_noise(self.dynamics.calculate_rpm(), 12)))
        batt_v = self.battery.pack_voltage()
        batt_a = self.battery.pack_current(power)

        telemetry_pkt = self.builder.build_telemetry_packet(
            speed, rpm, self.battery.soc, batt_v, batt_a,
            self.sensors.generate_motor_temp(), self.sensors.generate_inverter_temp(),
            self.drive_mode, self.lights, int(self.dynamics.odometer_m))
        cell_pkt = self.builder.build_cell_packet(
            self.battery.cell_voltages_mv(), self.battery.temp)

        self._latest = {
            "ts": time.time(),
            "seq": self.builder.message_counter,
            "state": self.state,
            "scenario": self.scenario.active_scenario,
            "raw": telemetry_pkt,       # 클라이언트 TelemetryParser가 파싱할 원시 패킷
            "raw_cells": cell_pkt,
        }

    def latest(self):
        with self._lock:
            return dict(self._latest) if self._latest else None

    def status(self):
        with self._lock:
            return {
                "state": self.state,
                "running": self.is_running,
                "tick_rate": self.tick_rate,
                "scenario": self.scenario.active_scenario,
                "scenarios": ScenarioManager.SCENARIOS,
                "mode": MODE_NAMES[self.drive_mode],
                "lights": self.lights,
                "manual_throttle": self.manual_throttle,
            }
