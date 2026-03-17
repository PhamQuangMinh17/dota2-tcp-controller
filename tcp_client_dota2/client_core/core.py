from __future__ import annotations

import socket
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List, Callable

"""
core.py

Author: Quang Minh Pham

Description:
This module contains all core infrastructure for the TCP client system.

Responsibilities:
- Manage TCP connections to multiple servers
- Provide thread-safe communication (send/receive commands)
- Maintain connection pool (multiple servers)
- Assign server numbers and teams (Team A / Team B)
- Handle connection lifecycle (connect, reconnect, remove, close)
- Provide broadcast and team-based command execution
- Provide background execution framework (ScheduleRunner)

Key Components:
- ServerConnection: represents one TCP connection to a server
- CommandChannel: thread-safe communication wrapper
- ConnectionPool: manages all connected servers and team logic
- ScheduleRunner: runs latency loop or experiment loop in background threads

Notes:
- All other modules depend on this file
- This is the backbone of the client architecture
"""
MAX_SERVERS = 10
TEAM_SLOT_SIZE = 5  # numbers 1-5 => Team A, 6-10 => Team B


def print_results(prefix: str, results: Dict[str, str]) -> None:
    for lbl, resp in results.items():
        print(f"{prefix}{lbl}] {resp}")


# ---------------------------
# Low-level line protocol I/O
# ---------------------------

def _send_line(sock_file, text: str) -> None:
    sock_file.write((text + "\n").encode("utf-8"))
    sock_file.flush()



def _recv_line(sock_file) -> Optional[str]:
    raw = sock_file.readline()
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace").strip()


# ---------------------------
# Shared parsing / helpers
# ---------------------------

def parse_mmss_or_seconds(text: str) -> float:
    """
    Parse a duration in either:
      - mm:ss  (e.g. 00:05, 1:30)
      - seconds (e.g. 5, 2.5)
    Returns seconds as float.
    """
    s = text.strip()
    if not s:
        raise ValueError("empty duration")

    if ":" in s:
        mm_s, ss_s = s.split(":", 1)
        mm = int(mm_s.strip())
        ss = int(ss_s.strip())
        if mm < 0 or ss < 0:
            raise ValueError("negative values not allowed")
        return float(mm * 60 + ss)

    return float(s)



def normalize_team(team: str) -> str:
    t = team.strip().upper()
    if t in ("A", "TEAM_A", "TEAMA", "1"):
        return "A"
    if t in ("B", "TEAM_B", "TEAMB", "2"):
        return "B"
    raise ValueError("team must be A or B")



def team_for_number(number: int) -> str:
    return "A" if number <= TEAM_SLOT_SIZE else "B"



def parse_server_item(item: str, default_port: int) -> Tuple[str, int]:
    """
    Accept:
      - "192.168.0.231"
      - "192.168.0.231:5000"
    Returns (host, port).
    """
    s = item.strip()
    if not s:
        raise ValueError("empty server entry")
    if ":" in s:
        host, port_s = s.rsplit(":", 1)
        host = host.strip()
        port = int(port_s.strip())
        return host, port
    return s, default_port


# ---------------------------
# Networking / command channel
# ---------------------------

class CommandChannel:
    """
    Thread-safe request/response channel over one TCP connection.
    Ensures background thread and interactive thread won't interleave send/recv.
    """
    def __init__(self, sock_file, label: str):
        self.sock_file = sock_file
        self.label = label
        self._lock = threading.Lock()

    def send_cmd(self, cmd: str) -> str:
        with self._lock:
            _send_line(self.sock_file, cmd)
            resp = _recv_line(self.sock_file)
            if resp is None:
                raise ConnectionError(f"Server {self.label} disconnected.")
            return resp



def _connect(server: Tuple[str, int], timeout_sec: float) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_sec)
    s.connect(server)
    s.settimeout(None)
    return s


@dataclass
class ServerConnection:
    host: str
    port: int
    client_name: str
    connect_timeout_sec: float

    sock: Optional[socket.socket] = None
    sock_file: Optional[object] = None
    chan: Optional[CommandChannel] = None

    number: Optional[int] = None
    team: str = "A"  # "A" or "B"

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = _connect((self.host, self.port), timeout_sec=self.connect_timeout_sec)
        sock_file = sock.makefile("rwb")

        _send_line(sock_file, f"HELLO {self.client_name}")

        self.sock = sock
        self.sock_file = sock_file
        self.chan = CommandChannel(sock_file, label=self.label)

    def close(self) -> None:
        try:
            if self.sock_file is not None:
                try:
                    self.sock_file.close()
                except Exception:
                    pass
        finally:
            self.sock_file = None
        try:
            if self.sock is not None:
                try:
                    self.sock.close()
                except Exception:
                    pass
        finally:
            self.sock = None
        self.chan = None

    def send(self, cmd: str) -> str:
        if self.chan is None:
            raise ConnectionError(f"Not connected to {self.label}")
        return self.chan.send_cmd(cmd)


class ConnectionPool:
    """
    Holds multiple ServerConnection objects.

    Provides:
      - broadcast to all servers
      - send to a specific server (by host / number)
      - thread-safe removal on failure
      - team filtering utilities
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._conns: Dict[str, ServerConnection] = {}

    def add(self, conn: ServerConnection) -> None:
        with self._lock:
            self._conns[conn.label] = conn

    def count(self) -> int:
        with self._lock:
            return len(self._conns)

    def has_label(self, label: str) -> bool:
        with self._lock:
            return label in self._conns

    def labels(self) -> List[str]:
        with self._lock:
            return sorted(self._conns.keys())

    def conns(self) -> List[ServerConnection]:
        with self._lock:
            return list(self._conns.values())

    def get_by_host(self, host: str) -> Optional[ServerConnection]:
        with self._lock:
            for c in self._conns.values():
                if c.host == host:
                    return c
        return None

    def get_by_number(self, number: int) -> Optional[ServerConnection]:
        with self._lock:
            for c in self._conns.values():
                if c.number == number:
                    return c
        return None

    def next_available_number(self) -> Optional[int]:
        with self._lock:
            used = {c.number for c in self._conns.values() if c.number is not None}
        for n in range(1, MAX_SERVERS + 1):
            if n not in used:
                return n
        return None

    def team_conns(self, team: str) -> List[ServerConnection]:
        t = normalize_team(team)
        with self._lock:
            return [c for c in self._conns.values() if (c.team or "A").upper() == t]

    def other_teams(self, active_team: str) -> List[str]:
        t = normalize_team(active_team)
        return [team for team in ("A", "B") if team != t]

    def remove_label(self, label: str) -> None:
        with self._lock:
            conn = self._conns.pop(label, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close_all(self) -> None:
        for lbl in self.labels():
            self.remove_label(lbl)

    def _collect_from_connections(self, conns: List[ServerConnection], cmd: str) -> Dict[str, str]:
        results: Dict[str, str] = {}
        dead: List[str] = []
        for conn in conns:
            try:
                results[conn.label] = conn.send(cmd)
            except Exception as e:
                results[conn.label] = f"ERR {e}"
                dead.append(conn.label)

        for lbl in dead:
            self.remove_label(lbl)

        return results

    def broadcast_collect(self, cmd: str) -> Dict[str, str]:
        with self._lock:
            conns = list(self._conns.values())
        return self._collect_from_connections(conns, cmd)

    def team_collect(self, team: str, cmd: str) -> Dict[str, str]:
        return self._collect_from_connections(self.team_conns(team), cmd)

    def teams_collect(self, teams: List[str], cmd: str) -> Dict[str, str]:
        normalized = {normalize_team(t) for t in teams}
        with self._lock:
            conns = [c for c in self._conns.values() if (c.team or "A").upper() in normalized]
        return self._collect_from_connections(conns, cmd)


class ScheduleRunner:
    """
    Runs one background task at a time.

    The actual task logic is owned by higher-level modules such as
    latency_control.py or experiment_control.py.
    """
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._mode_lock = threading.Lock()
        self._mode: Optional[str] = None

    def _set_mode(self, mode: Optional[str]) -> None:
        with self._mode_lock:
            self._mode = mode

    def current_mode(self) -> Optional[str]:
        with self._mode_lock:
            return self._mode

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_task(self, mode: str, target: Callable[[threading.Event], None], thread_name: str) -> None:
        self._stop_evt.clear()
        self._set_mode(mode)

        def _wrapped() -> None:
            try:
                target(self._stop_evt)
            finally:
                self._set_mode(None)

        self._thread = threading.Thread(target=_wrapped, name=thread_name, daemon=True)
        self._thread.start()

    def stop(self) -> Optional[str]:
        previous_mode = self.current_mode()
        if not self.running():
            print("[tcp_client] Schedule is not running.")
            return previous_mode
        self._stop_evt.set()
        self._thread.join(timeout=2.0)
        return previous_mode
