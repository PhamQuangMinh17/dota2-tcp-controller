"""
latency_change_divert.py

Headless (no-GUI) latency injector for Windows using WinDivert directly.

Why this exists:
- Your previous approach controlled Clumsy by *restarting a GUI app* whenever latency changes,
  which causes the Clumsy window to pop up. fileciteturn1file1
- This module replaces that with a background WinDivert loop that delays packets in-process.

What you need to ship alongside this file:
- WinDivert.dll
- WinDivert64.sys  (and WinDivert32.sys if you plan to run 32-bit Python)

These are included in the Clumsy repo under:
  external/WinDivert-2.2.0-*/x64/  (for 64-bit)   [WinDivert.dll + WinDivert64.sys]
  external/WinDivert-2.2.0-*/x86/  (for 32-bit)   [WinDivert.dll + WinDivert32.sys + WinDivert64.sys]

Notes:
- Requires Administrator (WinDivert is a kernel driver).
- This script focuses ONLY on "Lag" (delay) — no drop/throttle/tamper.
- Latency changes are applied instantly without spawning any GUI.

Public API (mirrors your usage pattern):
- require_admin(auto_elevate=True)
- DivertConfig(...)
- DivertLatencyController(...).set_latency(ms)
- DivertLatencyController(...).stop()
"""

from __future__ import annotations

import argparse
import ctypes
import os
import platform
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional


# -------------------------
# Admin elevation helpers
# -------------------------

def _is_windows() -> bool:
    return platform.system().lower().startswith("win")


def is_admin() -> bool:
    """
    Returns True if the current process has Administrator privileges on Windows.
    On non-Windows platforms, returns True.
    """
    if not _is_windows():
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> None:
    """
    Relaunches the current Python script with Administrator privileges (Windows only).
    """
    if not _is_windows():
        return

    params = " ".join([f'"{a}"' if " " in a else a for a in sys.argv])
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    if rc <= 32:
        raise RuntimeError("Failed to elevate. Please run terminal as Administrator.")
    sys.exit(0)


def require_admin(auto_elevate: bool = True) -> None:
    """
    Ensures the process is running as Administrator on Windows.
    """
    if is_admin():
        return
    if auto_elevate and _is_windows():
        print("[!] Not running as Administrator. Relaunching with elevation...")
        relaunch_as_admin()
    raise PermissionError("Administrator privileges are required to use WinDivert.")


# -------------------------
# WinDivert ctypes bindings
# -------------------------

# WinDivert constants (stable across versions)
WINDIVERT_LAYER_NETWORK = 0
WINDIVERT_SHUTDOWN_RECV = 0x1
WINDIVERT_SHUTDOWN_SEND = 0x2
WINDIVERT_PARAM_QUEUE_LENGTH = 0
WINDIVERT_PARAM_QUEUE_TIME = 1

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class WINDIVERT_ADDRESS(ctypes.Structure):
    """
    Mirror of WINDIVERT_ADDRESS from windivert.h (WinDivert 2.x).

    We only *read* the Outbound bit and otherwise treat this as an opaque struct
    to pass back into WinDivertSend().
    """
    _fields_ = [
        ("Timestamp", ctypes.c_int64),
        ("Layer", ctypes.c_uint32, 8),
        ("Event", ctypes.c_uint32, 8),
        ("Sniffed", ctypes.c_uint32, 1),
        ("Outbound", ctypes.c_uint32, 1),
        ("Loopback", ctypes.c_uint32, 1),
        ("Impostor", ctypes.c_uint32, 1),
        ("IPv6", ctypes.c_uint32, 1),
        ("IPChecksum", ctypes.c_uint32, 1),
        ("TCPChecksum", ctypes.c_uint32, 1),
        ("UDPChecksum", ctypes.c_uint32, 1),
        ("Reserved1", ctypes.c_uint32, 8),
        ("Reserved2", ctypes.c_uint32),
        ("Data", ctypes.c_uint8 * 64),
    ]


def _format_winerror(err: int) -> str:
    try:
        return ctypes.FormatError(err).strip()
    except Exception:
        return f"WinError {err}"


def _monotonic() -> float:
    # monotonic seconds
    return time.perf_counter()


@dataclass
class DivertConfig:
    """
    Config for DivertLatencyController.
    """
    # WinDivert filter expression (same language as Clumsy filter box).
    filter_expr: str = "udp"

    # If empty, tries to load WinDivert.dll from the same directory as this script.
    windivert_dll: str = ""

    # Delay directions:
    delay_inbound: bool = True
    delay_outbound: bool = True

    # If True: set_latency(0) will disable filtering entirely (shutdown + close).
    # If False: latency=0 means "no delay" but packets are still intercepted and reinjected.
    stop_on_zero: bool = False

    # Safety: prevent unbounded RAM growth under high packet rate.
    max_buffer_packets: int = 2000
    flush_when_full: int = 800

    # WinDivert internal kernel queue sizing (helps avoid drops if user-mode pauses briefly).
    queue_length: int = 2048
    queue_time_ms: int = 512

    # Improve timer resolution (system-wide). 0 disables.
    timer_resolution_ms: int = 1


@dataclass
class _BufferedPacket:
    t_capture: float
    packet: bytes
    addr_bytes: bytes


class DivertLatencyController:
    """
    Headless latency injector: intercept packets and re-inject after N ms.

    Usage:
        require_admin(auto_elevate=True)
        ctl = DivertLatencyController(DivertConfig(filter_expr="udp", stop_on_zero=True))
        ctl.set_latency(100)
        ...
        ctl.set_latency(500)
        ...
        ctl.stop()
    """

    def __init__(self, cfg: DivertConfig):
        self.cfg = cfg

        self._dll = None
        self._handle: Optional[int] = None

        self._latency_ms: int = 0
        self._buf: Deque[_BufferedPacket] = deque()

        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop_requested = False

        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None

        self._timer_set = False

    @property
    def running(self) -> bool:
        return self._handle is not None

    @property
    def last_latency_ms(self) -> int:
        return self._latency_ms

    # --------
    # Public
    # --------

    def set_latency(self, lat_ms: int) -> None:
        """
        Set latency in ms. Applies immediately without any GUI.

        If cfg.stop_on_zero is True and lat_ms == 0:
            stop() is called (disables filtering).
        """
        lat_ms = int(lat_ms)
        if lat_ms < 0:
            raise ValueError("Latency must be >= 0 ms.")

        if self.cfg.stop_on_zero and lat_ms == 0:
            self.stop()
            with self._cv:
                self._latency_ms = 0
            return

        # Ensure engine is running before applying non-zero latency.
        if not self.running:
            self._start_engine()

        with self._cv:
            self._latency_ms = lat_ms
            self._cv.notify_all()

    def stop(self) -> None:
        """
        Stop diverting. Flushes buffered packets first.
        """
        with self._cv:
            if not self.running:
                self._stop_requested = False
                self._buf.clear()
                return
            self._stop_requested = True
            self._cv.notify_all()

        # Stop receiving new packets but keep handle open to flush buffered packets.
        try:
            self._dll.WinDivertShutdown(self._handle, WINDIVERT_SHUTDOWN_RECV)
        except Exception:
            pass

        # Join threads; TX thread will flush and close the handle.
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=5.0)
        if self._tx_thread and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=5.0)

        # Best-effort cleanup if TX thread didn't close (shouldn't happen).
        if self._handle is not None:
            try:
                self._dll.WinDivertShutdown(self._handle, WINDIVERT_SHUTDOWN_SEND)
            except Exception:
                pass
            try:
                self._dll.WinDivertClose(self._handle)
            except Exception:
                pass
            self._handle = None

        self._stop_requested = False
        self._buf.clear()

        self._end_timer_resolution()

    # -------------
    # Internal
    # -------------

    def _start_engine(self) -> None:
        if not _is_windows():
            raise OSError("WinDivert is Windows-only.")
        require_admin(auto_elevate=True)

        dll_path = self.cfg.windivert_dll.strip()
        if not dll_path:
            dll_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "WinDivert.dll")

        if not os.path.isfile(dll_path):
            raise FileNotFoundError(
                f"WinDivert.dll not found at: {dll_path}\n"
                "Copy WinDivert.dll (and WinDivert64.sys) next to this script, "
                "or pass --windivert-dll."
            )

        dll_dir = os.path.dirname(os.path.abspath(dll_path))
        # Make sure dependent loads & driver discovery work from that folder.
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(dll_dir)

        self._dll = ctypes.WinDLL(dll_path, use_last_error=True)

        # Prototype WinDivert functions we need.
        self._dll.WinDivertOpen.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_int16, ctypes.c_uint64]
        self._dll.WinDivertOpen.restype = ctypes.c_void_p

        self._dll.WinDivertRecv.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(WINDIVERT_ADDRESS)
        ]
        self._dll.WinDivertRecv.restype = ctypes.c_bool

        self._dll.WinDivertSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(WINDIVERT_ADDRESS)
        ]
        self._dll.WinDivertSend.restype = ctypes.c_bool

        self._dll.WinDivertShutdown.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._dll.WinDivertShutdown.restype = ctypes.c_bool

        self._dll.WinDivertClose.argtypes = [ctypes.c_void_p]
        self._dll.WinDivertClose.restype = ctypes.c_bool

        self._dll.WinDivertSetParam.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint64]
        self._dll.WinDivertSetParam.restype = ctypes.c_bool

        # Open handle
        filt = self.cfg.filter_expr.encode("utf-8")
        handle = self._dll.WinDivertOpen(filt, WINDIVERT_LAYER_NETWORK, 0, 0)
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            raise RuntimeError(f"WinDivertOpen failed: {err} ({_format_winerror(err)})")

        self._handle = int(handle)

        # Set internal queue params (best-effort).
        try:
            self._dll.WinDivertSetParam(self._handle, WINDIVERT_PARAM_QUEUE_LENGTH, int(self.cfg.queue_length))
            self._dll.WinDivertSetParam(self._handle, WINDIVERT_PARAM_QUEUE_TIME, int(self.cfg.queue_time_ms))
        except Exception:
            pass

        # Start timer resolution (optional)
        self._start_timer_resolution()

        # Start threads
        self._stop_requested = False
        self._buf.clear()

        self._rx_thread = threading.Thread(target=self._rx_loop, name="windivert-rx", daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_loop, name="windivert-tx", daemon=True)
        self._rx_thread.start()
        self._tx_thread.start()

    def _start_timer_resolution(self) -> None:
        if not _is_windows():
            return
        if self.cfg.timer_resolution_ms <= 0:
            return
        if self._timer_set:
            return
        try:
            # timeBeginPeriod affects global timer resolution; use sparingly.
            ctypes.windll.winmm.timeBeginPeriod(int(self.cfg.timer_resolution_ms))
            self._timer_set = True
        except Exception:
            self._timer_set = False

    def _end_timer_resolution(self) -> None:
        if not _is_windows():
            return
        if not self._timer_set:
            return
        try:
            ctypes.windll.winmm.timeEndPeriod(int(self.cfg.timer_resolution_ms))
        except Exception:
            pass
        self._timer_set = False

    def _send_packet(self, packet: bytes, addr_bytes: bytes) -> None:
        if self._handle is None:
            return

        # Recreate structs/buffers for ctypes call.
        pkt_len = len(packet)
        pkt_buf = (ctypes.c_uint8 * pkt_len).from_buffer_copy(packet)
        addr = WINDIVERT_ADDRESS.from_buffer_copy(addr_bytes)
        send_len = ctypes.c_uint(0)

        ok = self._dll.WinDivertSend(self._handle, ctypes.byref(pkt_buf), pkt_len, ctypes.byref(send_len), ctypes.byref(addr))
        if not ok:
            # Most of the time for experiments it's okay to log+continue.
            err = ctypes.get_last_error()
            # Avoid spamming logs too hard: only print occasionally.
            # (You can improve this with a rate limiter if needed.)
            # print(f"[windivert] Send failed: {err} ({_format_winerror(err)})")
            _ = err

    def _rx_loop(self) -> None:
        assert self._dll is not None
        assert self._handle is not None

        MAX_PACKET = 0xFFFF
        pkt_buf = (ctypes.c_uint8 * MAX_PACKET)()
        read_len = ctypes.c_uint(0)
        addr = WINDIVERT_ADDRESS()

        while True:
            ok = self._dll.WinDivertRecv(self._handle, ctypes.byref(pkt_buf), MAX_PACKET, ctypes.byref(read_len), ctypes.byref(addr))
            if not ok:
                err = ctypes.get_last_error()
                # After WinDivertShutdown(RECV), WinDivertRecv eventually fails when queue empty.
                # Treat that as exit when stopping.
                with self._cv:
                    if self._stop_requested:
                        return
                # Otherwise, ignore transient errors and continue.
                # print(f"[windivert] Recv failed: {err} ({_format_winerror(err)})")
                continue

            pkt = bytes(pkt_buf[: int(read_len.value)])
            addr_bytes = ctypes.string_at(ctypes.byref(addr), ctypes.sizeof(addr))
            outbound = bool(addr.Outbound)

            with self._cv:
                if self._stop_requested:
                    # We're stopping; don't buffer anything new. Reinject immediately.
                    pass_through = True
                    lat_ms = 0
                else:
                    lat_ms = int(self._latency_ms)
                    pass_through = (lat_ms <= 0)

                if not pass_through:
                    if outbound and not self.cfg.delay_outbound:
                        pass_through = True
                    if (not outbound) and not self.cfg.delay_inbound:
                        pass_through = True

                if pass_through:
                    # Send immediately (outside the lock).
                    pass

                else:
                    self._buf.append(_BufferedPacket(_monotonic(), pkt, addr_bytes))

                    # Prevent runaway growth: flush oldest packets if needed.
                    if len(self._buf) > self.cfg.max_buffer_packets:
                        flush_n = min(self.cfg.flush_when_full, len(self._buf))
                        to_flush = [self._buf.popleft() for _ in range(flush_n)]
                    else:
                        to_flush = []

                    self._cv.notify_all()

            # Execute sends outside the lock.
            if pass_through:
                self._send_packet(pkt, addr_bytes)
            else:
                for bp in to_flush:
                    self._send_packet(bp.packet, bp.addr_bytes)

    def _tx_loop(self) -> None:
        assert self._dll is not None
        assert self._handle is not None

        while True:
            # Decide what to send, and how long to sleep, under lock.
            with self._cv:
                if self._stop_requested:
                    # Flush everything immediately, then close handle.
                    pending = list(self._buf)
                    self._buf.clear()
                    break

                if not self._buf:
                    self._cv.wait(timeout=0.5)
                    continue

                lat_ms = int(self._latency_ms)
                if lat_ms <= 0:
                    # If latency is zero, flush immediately (still keeps ordering).
                    bp = self._buf.popleft()
                    pending_one = bp
                    wait_sec = 0.0
                else:
                    now = _monotonic()
                    bp0 = self._buf[0]
                    due = bp0.t_capture + (lat_ms / 1000.0)
                    if now >= due:
                        pending_one = self._buf.popleft()
                        wait_sec = 0.0
                    else:
                        pending_one = None
                        # Wait until due or until a latency change/new packet arrives.
                        wait_sec = min(max(due - now, 0.0), 0.05)

            # Outside lock: send or sleep.
            if pending_one is not None:
                self._send_packet(pending_one.packet, pending_one.addr_bytes)
                continue
            if wait_sec > 0:
                time.sleep(wait_sec)

        # Stop requested: flush pending and close.
        for bp in pending:
            self._send_packet(bp.packet, bp.addr_bytes)

        try:
            self._dll.WinDivertShutdown(self._handle, WINDIVERT_SHUTDOWN_SEND)
        except Exception:
            pass
        try:
            self._dll.WinDivertClose(self._handle)
        except Exception:
            pass
        self._handle = None
        self._end_timer_resolution()

    # Alias for compatibility with your old naming (optional)
    def start(self, lat_ms: int) -> None:
        self.set_latency(lat_ms)


def _cli() -> int:
    """
    Manual test mode (optional):
      python latency_change_divert.py --windivert-dll .\\WinDivert.dll --filter "udp" --latency 100
    """
    p = argparse.ArgumentParser(description="Headless latency injector using WinDivert (no GUI).")
    p.add_argument("--windivert-dll", default="", help="Path to WinDivert.dll (default: ./WinDivert.dll next to script)")
    p.add_argument("--filter", default="udp", help="WinDivert filter expression (e.g., udp, tcp, outbound and udp)")
    p.add_argument("--latency", type=int, required=True, help="Latency in ms (>= 0)")
    p.add_argument("--inbound", action="store_true", help="Delay inbound packets (default: True)")
    p.add_argument("--outbound", action="store_true", help="Delay outbound packets (default: True)")
    p.add_argument("--stop-on-zero", action="store_true", help="Disable filtering when latency==0")
    p.add_argument("--no-elevate", action="store_true", help="Do not auto-elevate to Admin on Windows")

    args = p.parse_args()

    require_admin(auto_elevate=not args.no_elevate)

    cfg = DivertConfig(
        filter_expr=args.filter,
        windivert_dll=args.windivert_dll,
        delay_inbound=True if not (args.inbound or args.outbound) else args.inbound,
        delay_outbound=True if not (args.inbound or args.outbound) else args.outbound,
        stop_on_zero=args.stop_on_zero,
    )

    ctl = DivertLatencyController(cfg)
    ctl.set_latency(args.latency)

    try:
        print(f"[windivert] Running with latency={args.latency} ms, filter={args.filter}")
        print("[windivert] Press Ctrl-C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[windivert] Stopping...")
        ctl.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
