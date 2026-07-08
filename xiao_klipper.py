#!/usr/bin/env python3
"""
xiao_klipper.py
------------------------------------------------------------------
Host-side bridge for the Seeed Studio XIAO smart peripheral.

The XIAO renders its own OLED UI, so this runner has two jobs over ONE shared
serial port:

  * INPUT  - run the existing button service (klipper_buttons.KlipperButtonsService)
             with the "xiao" GPIO backend. Button events from the XIAO are
             turned into virtual pin presses by XiaoLink, so the service's
             normal handlers (start_bowl / estop / clean) fire and drive
             Moonraker exactly as with real GPIO buttons.
  * OUTPUT - forward the service's state (/tmp/klipper_buttons_state.json) to
             the XIAO as compact JSON so it can render idle / whisking /
             cleaning / memory screens.

Usage:
    python3 xiao_klipper.py [--port /dev/ttyACM0] [--baud 115200]

The port falls back to gpio.serialPort in src/config.json, then the
XIAO_PORT environment variable, then /dev/ttyACM0.
"""

import argparse
import json
import logging
import os
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
for _p in (BASE_DIR, SRC_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from xiao_link import XiaoLink  # noqa: E402
from klipper_buttons import KlipperButtonsService, load_config  # noqa: E402

try:
    import serial  # noqa: E402
    from serial.tools import list_ports  # noqa: E402
except ImportError:  # pragma: no cover
    serial = None
    list_ports = None

# USB Vendor ID used by the RP2040 / RP2350 native USB (Raspberry Pi).
XIAO_USB_VID = 0x2E8A

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("xiao_klipper")

STATE_FILE = "/tmp/klipper_buttons_state.json"

# How often to push state to the XIAO, and a heartbeat interval that keeps the
# peripheral's "host connected" (ON) indicator alive even when idle.
FORWARD_POLL_S = 0.2
HEARTBEAT_S = 1.0


def autodetect_xiao_port():
    """Return the serial device of a connected XIAO (RP2040/RP2350), or None.

    Matches the Raspberry Pi USB VID (0x2E8A), which covers the XIAO RP2040
    and RP2350 native USB CDC. Works on both Windows (COMxx) and Linux
    (/dev/ttyACMx).
    """
    if list_ports is None:
        return None
    candidates = []
    for p in list_ports.comports():
        vid = getattr(p, "vid", None)
        hwid = (getattr(p, "hwid", "") or "").upper()
        if vid == XIAO_USB_VID or "2E8A" in hwid:
            candidates.append(p.device)
    if candidates:
        logger.info("Auto-detected XIAO on %s", candidates[0])
        return candidates[0]
    return None


def _read_state(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _int(value, default=0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def build_compact_state(sd: dict) -> dict:
    """Map the full state file to the compact schema the firmware parses.

    Kept small on purpose: the firmware reads whole lines into a fixed buffer
    (RX_MAX) and matches fields by substring, so we send only what it renders.
    """
    slots = sd.get("memory_slots", {}) or {}
    memory_mode = bool(sd.get("memory_mode", False))
    current_slot = _int(sd.get("current_memory_slot"), 1)

    def slot(key: str) -> dict:
        s = slots.get(key, {}) or {}
        out = {"isEmpty": bool(s.get("isEmpty", True))}
        # Preview values are only rendered for the slot currently being browsed,
        # so include them only there to keep the line inside the firmware buffer.
        if memory_mode and key == "slot%d" % current_slot and not out["isEmpty"]:
            for k in ("bowl1Duration", "bowl1Pattern", "bowl2Duration", "bowl2Pattern"):
                if k in s:
                    out[k] = _int(s.get(k))
        return out

    op = sd.get("current_operation", {}) or {}

    return {
        "current_state": sd.get("current_state", "idle"),
        "operation_status": sd.get("operation_status", "Idle"),
        "bowl1_duration": _int(sd.get("bowl1_duration")),
        "bowl1_pattern": _int(sd.get("bowl1_pattern"), 1),
        "bowl2_duration": _int(sd.get("bowl2_duration")),
        "bowl2_pattern": _int(sd.get("bowl2_pattern"), 1),
        "nav_mode": bool(sd.get("nav_mode", False)),
        "edit_mode": bool(sd.get("edit_mode", False)),
        "memory_mode": bool(sd.get("memory_mode", False)),
        "navigation_option": sd.get("navigation_option", ""),
        "current_memory_slot": _int(sd.get("current_memory_slot"), 1),
        "clock": time.strftime("%H:%M"),
        "memory_slots": {
            "slot1": slot("slot1"),
            "slot2": slot("slot2"),
            "slot3": slot("slot3"),
        },
        "current_operation": {
            "bowl": _int(op.get("bowl")),
            "total": _int(op.get("total")),
            "elapsed": _int(op.get("elapsed")),
            "percent": _int(op.get("percent")),
            "pattern": _int(op.get("pattern"), 1),
        },
    }


def state_forwarder(link: XiaoLink, state_path: str, stop_event: threading.Event) -> None:
    """Watch the state file and stream compact state to the XIAO."""
    last_mtime = 0.0
    last_send = 0.0
    logger.info("State forwarder started (watching %s)", state_path)
    while not stop_event.is_set():
        now = time.time()
        try:
            mtime = os.path.getmtime(state_path)
        except OSError:
            mtime = 0.0

        changed = mtime != last_mtime
        heartbeat = (now - last_send) >= HEARTBEAT_S

        if changed or heartbeat:
            sd = _read_state(state_path)
            if sd is not None:
                link.send_state(build_compact_state(sd))
                last_send = now
                last_mtime = mtime
            elif heartbeat:
                # No state file yet - keep the connection indicator alive.
                link.ping()
                last_send = now

        stop_event.wait(FORWARD_POLL_S)


def run_connection_test(link: XiaoLink, config: dict, stop_event: threading.Event) -> None:
    """Klipper-free loopback test: drive the XIAO screen from button events only.

    No Moonraker/Klipper. Pressing a bowl button starts a local countdown so you
    can visually confirm the two-way serial link (events in, state/render out).
      Bowl 1 / Bowl 2 -> whisking countdown    Stop -> idle    Clean -> cleaning (5s)
    """
    durations = config.get("durations", {}) or {}
    patterns = config.get("patterns", {}) or {}
    b1_dur = _int(durations.get("bowl1Seconds"), 30)
    b2_dur = _int(durations.get("bowl2Seconds"), 30)
    b1_pat = _int(patterns.get("bowl1Pattern"), 1)
    b2_pat = _int(patterns.get("bowl2Pattern"), 1)

    lock = threading.Lock()
    st = {"state": "idle", "bowl": 0, "total": 0, "pattern": 1, "start": 0.0, "until": 0.0}

    def on_button(action: str) -> None:
        now = time.time()
        with lock:
            if action == "start_bowl1":
                st.update(state="whisking_bowl1", bowl=1, total=b1_dur, pattern=b1_pat, start=now)
            elif action == "start_bowl2":
                st.update(state="whisking_bowl2", bowl=2, total=b2_dur, pattern=b2_pat, start=now)
            elif action == "clean":
                st.update(state="cleaning", bowl=0, total=0, start=now, until=now + 5.0)
            elif action == "stop":
                st.update(state="idle", bowl=0, total=0, start=0.0, until=0.0)
        print(f"[TEST] button: {action} -> {st['state']}", flush=True)

    link.on_button = on_button
    logger.info("Connection test running (no Klipper). Press buttons on the XIAO; Ctrl+C to stop.")

    while not stop_event.is_set():
        now = time.time()
        with lock:
            state = st["state"]
            bowl = st["bowl"]
            total = st["total"]
            pattern = st["pattern"]
            elapsed = int(now - st["start"]) if st["start"] else 0
            if state.startswith("whisking"):
                if total > 0 and elapsed >= total:
                    st.update(state="idle", bowl=0, total=0, start=0.0)
                    state, bowl, total, elapsed = "idle", 0, 0, 0
            elif state == "cleaning" and now >= st["until"]:
                st.update(state="idle", start=0.0, until=0.0)
                state = "idle"

        percent = int((elapsed / total) * 100) if total > 0 else 0
        op_status = {
            "whisking_bowl1": "Whisking Bowl 1",
            "whisking_bowl2": "Whisking Bowl 2",
            "cleaning": "Cleaning",
        }.get(state, "Idle")

        link.send_state({
            "current_state": state,
            "operation_status": op_status,
            "bowl1_duration": b1_dur,
            "bowl1_pattern": b1_pat,
            "bowl2_duration": b2_dur,
            "bowl2_pattern": b2_pat,
            "nav_mode": False,
            "edit_mode": False,
            "memory_mode": False,
            "navigation_option": "",
            "current_memory_slot": 1,
            "clock": time.strftime("%H:%M"),
            "memory_slots": {
                "slot1": {"isEmpty": True},
                "slot2": {"isEmpty": True},
                "slot3": {"isEmpty": True},
            },
            "current_operation": {
                "bowl": bowl,
                "total": total,
                "elapsed": min(elapsed, total) if total else 0,
                "percent": percent,
                "pattern": pattern,
            },
        })
        stop_event.wait(0.3)


def main() -> int:
    parser = argparse.ArgumentParser(description="pi-klipper-buttons XIAO bridge")
    parser.add_argument("--port", help="Serial port of the XIAO (e.g. /dev/ttyACM0)")
    parser.add_argument("--baud", type=int, default=None, help="Serial baud rate")
    parser.add_argument(
        "--no-klipper", "--test", dest="no_klipper", action="store_true",
        help="Skip Moonraker/Klipper and just test the XIAO serial connection "
             "(buttons drive a local demo on the screen).",
    )
    args = parser.parse_args()

    service_config_path = os.path.join(SRC_DIR, "config.json")
    service_config = load_config(service_config_path)
    gpio_cfg = service_config.get("gpio", {})

    # Explicit port wins; otherwise prefer auto-detection over the config/env
    # default so a plugged-in XIAO "just works" on Windows (COMxx) and Linux.
    port = (
        args.port
        or autodetect_xiao_port()
        or gpio_cfg.get("serialPort")
        or os.environ.get("XIAO_PORT")
        or "/dev/ttyACM0"
    )
    baud = args.baud or int(gpio_cfg.get("serialBaud", 115200))

    logger.info("Opening XIAO on %s @ %d baud", port, baud)
    try:
        link = XiaoLink(port, baud)
    except Exception as e:
        logger.error("Could not open %s: %s", port, e)
        fallback = autodetect_xiao_port()
        if not fallback or fallback == port:
            logger.error(
                "No XIAO serial port available. Plug it in and/or pass --port "
                "(Windows uses COMxx, e.g. --port COM21; Linux uses /dev/ttyACMx)."
            )
            return 1
        logger.info("Retrying on auto-detected port %s", fallback)
        link = XiaoLink(fallback, baud)
    link.wait_ready(timeout=3.0)

    stop_event = threading.Event()

    # Connection-test mode: no Moonraker/Klipper, just verify the XIAO link.
    if args.no_klipper:
        try:
            run_connection_test(link, service_config, stop_event)
        except KeyboardInterrupt:
            logger.info("Shutdown requested by user")
        finally:
            stop_event.set()
            try:
                link.send_cmd("host_shutdown")
            except Exception:
                pass
            link.close()
        return 0

    # Button service reads the XIAO (virtual) pins via the "xiao" backend.
    service = KlipperButtonsService(service_config)
    service.gpio_backend = "xiao"
    service._xiao = link

    try:
        service_thread = threading.Thread(target=service.run, daemon=True)
        service_thread.start()

        state_path = getattr(service, "state_file_path", STATE_FILE)
        forwarder_thread = threading.Thread(
            target=state_forwarder, args=(link, state_path, stop_event), daemon=True
        )
        forwarder_thread.start()

        logger.info("XIAO bridge started. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            if not service_thread.is_alive():
                logger.error("Button service thread exited")
                return 1
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
    finally:
        stop_event.set()
        try:
            service.running = False
        except Exception:
            pass
        try:
            link.send_cmd("host_shutdown")
        except Exception:
            pass
        link.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
