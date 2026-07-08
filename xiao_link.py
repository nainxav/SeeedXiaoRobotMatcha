#!/usr/bin/env python3
"""
xiao_link.py
------------------------------------------------------------------
Raspberry Pi <-> Seeed Studio XIAO (RP2040/RP2350) USB-serial bridge.

The XIAO firmware (seeed_xiao/klipper_buttons_xiao) is a *smart peripheral*:

  * INPUT  - it sends a semantic JSON event for every button, e.g.
                {"evt":"button","action":"start_bowl1"}
  * OUTPUT - it renders the OLED UI on-device from JSON state pushed by the
             host, e.g.
                {"type":"state","current_state":"whisking_bowl1", ...}
             and understands a few commands
                {"type":"cmd","action":"host_startup"}   (ping/host_startup/estop/...)

This module keeps the existing button service unchanged by presenting the
button events as *virtual GPIO levels*: when a button event arrives it drives
that logical channel LOW for a short hold window, so the service's normal
HIGH->LOW edge detection fires the matching handler.

  * XiaoLink.read(pin)   -> mirrors KlipperButtonsService._read_gpio
  * XiaoLink.set_pin_map -> maps logical channel names to config pin numbers
  * XiaoLink.send_state  -> pushes a compact JSON state line to the display

A single USB serial port can only be opened by one process, so share ONE
XiaoLink instance for both roles (see xiao_klipper.py).

Serial protocol: see the header of klipper_buttons_xiao.ino.
"""

import json
import logging
import threading
import time
from typing import Dict, Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - handled at runtime
    serial = None

logger = logging.getLogger(__name__)

# How long a button event holds its virtual pin LOW so the service's 1ms
# polling loop reliably samples the HIGH->LOW->HIGH edge (debounce is 50ms).
PRESS_HOLD_S = 0.15

# Firmware button "action" -> logical channel name used in set_pin_map().
ACTION_TO_CHANNEL = {
    "start_bowl1": "startBowl1",
    "start_bowl2": "startBowl2",
    "stop": "stop",
    "clean": "clean",
}


class XiaoLink:
    """Owns the serial connection to the XIAO smart-peripheral.

    A background reader thread turns incoming button events into momentary
    virtual pin presses and tracks the peripheral's readiness. Outgoing
    state/command writes are serialized with a lock so both roles can share
    the port safely.
    """

    HIGH = 1
    LOW = 0

    def __init__(self, port: str, baud: int = 115200, connect: bool = True,
                 on_button=None):
        self.port = port
        self.baud = baud
        # Optional callback invoked with the action string for every button
        # event (in addition to the virtual-pin press). Used by the standalone
        # connection test; the button service does not need it.
        self.on_button = on_button
        self._ser = None
        self._running = False
        self._reader: Optional[threading.Thread] = None

        # Virtual pin state: logical channel name -> monotonic time until which
        # the channel should read LOW (i.e. "pressed").
        self._press_until: Dict[str, float] = {}
        self._press_lock = threading.Lock()

        # Map config pin numbers -> logical channel names (reverse of set_pin_map).
        self._pin_to_channel: Dict[int, str] = {}

        self._write_lock = threading.Lock()
        self._ready = False
        self._last_event_time = 0.0

        if connect:
            self.open()

    # ---------------- connection management ----------------
    def open(self) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is required for the XIAO backend. Install it with "
                "'pip install pyserial'."
            )
        self._ser = serial.Serial(self.port, self.baud, timeout=0.1)
        self._running = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        logger.info("XiaoLink opened on %s @ %d baud", self.port, self.baud)
        # Greet the peripheral in case it booted before us (we may miss its
        # "ready" announcement otherwise).
        self.send_cmd("host_startup")

    def close(self) -> None:
        self._running = False
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        logger.info("XiaoLink closed")

    def is_ready(self) -> bool:
        """True once the XIAO has announced itself or sent any event."""
        return self._ready

    def wait_ready(self, timeout: float = 3.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._ready:
                return True
            time.sleep(0.02)
        return self._ready

    # ---------------- input side (virtual GPIO) ----------------
    def set_pin_map(self, mapping: Dict[str, Optional[int]]) -> None:
        """Map logical channel names -> the pin numbers used in config.json.

        Lets read(pin) accept the same pin numbers the existing code passes
        around. Only the four button channels are actuated; rotary channels
        (if present) always read HIGH since the XIAO no longer has an encoder.
        """
        self._pin_to_channel.clear()
        for name, pin in mapping.items():
            if pin is not None:
                self._pin_to_channel[int(pin)] = name

    def read(self, pin: int) -> int:
        """Return the current virtual logic level (0/1) for a config pin."""
        channel = self._pin_to_channel.get(int(pin))
        if channel is None:
            return self.HIGH
        with self._press_lock:
            until = self._press_until.get(channel, 0.0)
        return self.LOW if time.monotonic() < until else self.HIGH

    def _press_channel(self, channel: str) -> None:
        with self._press_lock:
            # Extend (not stack) the hold window for repeated events.
            self._press_until[channel] = time.monotonic() + PRESS_HOLD_S

    # ---------------- reader ----------------
    def _read_loop(self) -> None:
        assert self._ser is not None
        while self._running:
            try:
                raw = self._ser.readline()
                if not raw:
                    continue
                line = raw.decode("ascii", "ignore").strip()
                if not line:
                    continue
                self._handle_line(line)
            except Exception as e:  # keep the reader alive
                logger.debug("XiaoLink read error: %s", e)
                time.sleep(0.05)

    def _handle_line(self, line: str) -> None:
        if line[0] != "{":
            logger.debug("[XIAO] %s", line)
            return
        try:
            msg = json.loads(line)
        except ValueError:
            logger.debug("[XIAO] non-JSON: %s", line)
            return

        self._ready = True

        # Button event: {"evt":"button","action":"start_bowl1"}
        if msg.get("evt") == "button":
            action = msg.get("action", "")
            channel = ACTION_TO_CHANNEL.get(action)
            if channel:
                self._last_event_time = time.time()
                self._press_channel(channel)
                logger.info("XIAO button event: %s -> %s", action, channel)
            else:
                logger.debug("XIAO unknown button action: %s", action)
            if self.on_button is not None:
                try:
                    self.on_button(action)
                except Exception as e:
                    logger.debug("on_button callback error: %s", e)
            return

        # System message: {"type":"sys","action":"ready"|"pong"|"reset_done"}
        if msg.get("type") == "sys":
            action = msg.get("action", "")
            if action == "ready":
                logger.info("XIAO announced ready (fw v%s)", msg.get("version"))
                self.send_cmd("host_startup")
            else:
                logger.debug("XIAO sys: %s", action)
            return

    # ---------------- output side ----------------
    def _write_line(self, obj: dict) -> None:
        if self._ser is None:
            return
        payload = (json.dumps(obj, separators=(",", ":")) + "\n").encode("ascii", "ignore")
        with self._write_lock:
            try:
                self._ser.write(payload)
            except Exception as e:
                logger.error("XiaoLink write error: %s", e)

    def send_state(self, state: dict) -> None:
        """Push a state object to the display. 'type':'state' is added."""
        obj = dict(state)
        obj["type"] = "state"
        self._write_line(obj)

    def send_cmd(self, action: str, reason: Optional[str] = None) -> None:
        obj = {"type": "cmd", "action": action}
        if reason:
            obj["reason"] = reason
        self._write_line(obj)

    def ping(self) -> None:
        self.send_cmd("ping")
