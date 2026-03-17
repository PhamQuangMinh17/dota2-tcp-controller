from __future__ import annotations

from typing import List, Optional, Tuple

from client_core.core import ConnectionPool, ScheduleRunner, parse_mmss_or_seconds, print_results

"""
latency_control.py

Author: Quang Minh Pham

Description:
This module handles all latency-related commands and logic.

Responsibilities:
- Handle "set <ms>" command (apply same latency to all servers)
- Handle "start_latency_loop" command:
    - Prompt user for duration and latency values
    - Generate looping latency schedule
    - Apply latency changes sequentially
- Validate latency input values
- Convert user input into schedule format
- Control latency loop execution using ScheduleRunner

Behavior:
- Latency loop runs continuously until "stop" command is issued
- Each latency value is applied for a fixed duration
- Applies to ALL connected servers

Notes:
- Does not control OBS or input
- Purely focused on latency injection logic
"""
def _prompt_duration_seconds() -> Optional[float]:
    """
    Interactive prompt for duration.
    Returns seconds, or None if user cancels.
    """
    while True:
        s = input("duration (mm:ss or seconds, 'q' to cancel)> ").strip()
        if s.lower() in ("q", "quit", "cancel"):
            return None
        try:
            sec = parse_mmss_or_seconds(s)
            if sec <= 0:
                raise ValueError("duration must be > 0")
            return sec
        except Exception as e:
            print(f"[tcp_client] Invalid duration '{s}': {e}")



def _prompt_latency_ms(idx: int) -> Optional[int]:
    """
    Interactive prompt for one latency value.
    Returns latency ms, or None if user cancels.
    """
    while True:
        s = input(f"latency value {idx} (ms, 'q' to cancel)> ").strip()
        if s.lower() in ("q", "quit", "cancel"):
            return None
        try:
            ms = int(s)
            if ms < 0:
                raise ValueError("must be >= 0")
            return ms
        except Exception as e:
            print(f"[tcp_client] Invalid latency '{s}': {e}")



def build_latency_loop_schedule(duration_sec: float, latencies_ms: List[int]) -> List[Tuple[int, float]]:
    """
    Convert duration + list of latencies into schedule items for latency loop.
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0")
    if len(latencies_ms) != 4:
        raise ValueError("latencies_ms must contain exactly 4 values")
    for v in latencies_ms:
        if v < 0:
            raise ValueError("latency must be >= 0")
    return [(lat, float(duration_sec)) for lat in latencies_ms]


class LatencyController:
    """
    Everything related to latency commands:
      - set <ms>
      - start_latency_loop
      - stop latency injection across all servers
    """
    def __init__(self, pool: ConnectionPool, scheduler: ScheduleRunner):
        self.pool = pool
        self.scheduler = scheduler

    def handle_set_command(self, parts: List[str]) -> None:
        if len(parts) != 2:
            print("[tcp_client] Usage: set <ms>")
            return
        try:
            ms = int(parts[1])
            if ms < 0:
                raise ValueError()
        except ValueError:
            print("[tcp_client] Latency must be an integer >= 0")
            return

        res = self.pool.broadcast_collect(f"SET_LATENCY {ms}")
        print_results("[tcp_server:", res)

    def handle_start_latency_loop_command(self, parts: List[str]) -> None:
        duration_sec: Optional[float] = None
        latencies: List[int] = []

        if len(parts) == 1:
            duration_sec = _prompt_duration_seconds()
            if duration_sec is None:
                print("[tcp_client] Canceled.")
                return
            for i in range(1, 5):
                v = _prompt_latency_ms(i)
                if v is None:
                    print("[tcp_client] Canceled.")
                    duration_sec = None
                    break
                latencies.append(v)
            if duration_sec is None:
                return
        else:
            if len(parts) != 6:
                print("[tcp_client] Usage: start_latency_loop <mm:ss|sec> <lat1> <lat2> <lat3> <lat4>")
                return
            try:
                duration_sec = parse_mmss_or_seconds(parts[1])
                if duration_sec <= 0:
                    raise ValueError("duration must be > 0")
                latencies = [int(x) for x in parts[2:6]]
                if any(x < 0 for x in latencies):
                    raise ValueError("latency must be >= 0")
            except Exception as e:
                print(f"[tcp_client] Invalid args: {e}")
                return

        schedule_items = build_latency_loop_schedule(duration_sec, latencies)
        self.start_latency_loop(schedule_items)

    def start_latency_loop(self, schedule_items: List[Tuple[int, float]], loop: bool = True) -> None:
        if not schedule_items:
            raise ValueError("Schedule is empty.")
        if self.scheduler.running():
            print("[tcp_client] Schedule already running. Type 'stop' first.")
            return

        if not self.pool.labels():
            print("[tcp_client] No connected servers. Cannot start schedule.")
            return

        def _worker(stop_evt) -> None:
            print("[tcp_client] Schedule mode started (ALL servers).")
            print("[tcp_client] Schedule items:", schedule_items)
            print("[tcp_client] Type 'stop' to stop schedule.\n")
            try:
                while not stop_evt.is_set():
                    for lat_ms, duration_sec in schedule_items:
                        if stop_evt.is_set():
                            break
                        res = self.pool.broadcast_collect(f"SET_LATENCY {lat_ms}")
                        for lbl, resp in res.items():
                            print(f"[tcp_client] ({lbl}) SET_LATENCY {lat_ms} -> {resp}")
                        if stop_evt.wait(duration_sec):
                            break
                    if not loop:
                        break
            except Exception as e:
                print(f"[tcp_client] Schedule thread error: {e}")
            finally:
                print("[tcp_client] Schedule stopped.\n")

        self.scheduler.start_task(
            mode="latency_loop",
            target=_worker,
            thread_name="latency-loop-runner",
        )

    def stop_all(self) -> None:
        res = self.pool.broadcast_collect("STOP")
        print_results("[tcp_server:", res)
