# -*- coding: utf-8 -*-
"""
protocol.py — 사용자 정의 CAN 모사 프로토콜 (Problem #3: 통신 프로토콜 설계 및 무결성 확보)

패킷 구조 (바이트 단위, hex 문자열로 전송):
  STX(0x02) | CMD(1B) | LEN(1B) | PAYLOAD(LEN B) | CHK(1B, XOR) | ETX(0x03)

CMD 종류:
  0x10 TELEMETRY  : 차량 주행 텔레메트리 (속도/RPM/SOC/전압/전류/온도/모드/램프/주행거리)
  0x11 BATT_CELLS : 배터리 12셀 전압(mV) + 셀 온도
"""
import struct

STX = 0x02
ETX = 0x03
CMD_TELEMETRY = 0x10
CMD_BATT_CELLS = 0x11


class CanPacketBuilder:
    """원시 CAN 패킷 생성기 — 데이터 무결성을 위한 XOR 체크섬 포함."""

    def __init__(self):
        self.message_counter = 0  # 전송된 메시지 시퀀스 카운터

    @staticmethod
    def calculate_checksum(data: bytes) -> int:
        chk = 0
        for b in data:
            chk ^= b
        return chk & 0xFF

    def _wrap(self, cmd: int, payload: bytes) -> str:
        body = bytes([cmd, len(payload)]) + payload
        chk = self.calculate_checksum(body)
        packet = bytes([STX]) + body + bytes([chk, ETX])
        self.message_counter += 1
        return packet.hex().upper()

    def build_telemetry_packet(self, speed_kmh: float, rpm: int, soc: float,
                               batt_v: float, batt_a: float, motor_temp: float,
                               inv_temp: float, mode: int, lights: int,
                               odometer_m: int) -> str:
        """주행 텔레메트리 패킷 생성. 고정소수점 스케일링 + 인코딩 범위 클램프."""
        u16 = lambda v: max(0, min(0xFFFF, int(v)))
        i16 = lambda v: max(-32768, min(32767, int(v)))
        payload = struct.pack(
            ">HHHHhhhBBI",
            u16(speed_kmh * 10),            # 속도 x10
            u16(rpm),                       # RPM
            u16(soc * 10),                  # SOC % x10
            u16(batt_v * 10),               # 팩 전압 x10
            i16(batt_a * 10),               # 팩 전류 x10 (부호 있음, 회생 시 음수)
            i16(motor_temp * 10),           # 모터 온도 x10
            i16(inv_temp * 10),             # 인버터 온도 x10
            mode & 0xFF,                    # 주행 모드 0:Normal 1:Eco 2:Sport
            lights & 0xFF,                  # 비트필드: b0 헤드램프 b1 상향등 b2 비상등
            int(odometer_m) & 0xFFFFFFFF,   # 누적 주행거리(m)
        )
        return self._wrap(CMD_TELEMETRY, payload)

    def build_cell_packet(self, cell_voltages_mv, cell_temp: float) -> str:
        """배터리 12셀 전압 패킷 생성."""
        payload = struct.pack(">12H", *[int(v) & 0xFFFF for v in cell_voltages_mv])
        payload += struct.pack(">h", int(cell_temp * 10))
        return self._wrap(CMD_BATT_CELLS, payload)


def validate_packet(hex_str: str):
    """패킷 무결성 검증 후 (cmd, payload) 반환. 손상 시 None. (서버측 자가 검증/테스트용)"""
    try:
        raw = bytes.fromhex(hex_str)
        if raw[0] != STX or raw[-1] != ETX:
            return None
        body, chk = raw[1:-2], raw[-2]
        if CanPacketBuilder.calculate_checksum(body) != chk:
            return None
        cmd, length = body[0], body[1]
        payload = body[2:2 + length]
        if len(payload) != length:
            return None
        return cmd, payload
    except (ValueError, IndexError):
        return None
