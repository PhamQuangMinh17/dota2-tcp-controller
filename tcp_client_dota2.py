from __future__ import annotations

import argparse
import socket
import threading
import random
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List


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
# Duration / latency helpers
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

    # Fallback: accept seconds
    return float(s)


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
    Convert duration + list of latencies into schedule items for ScheduleRunner.
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be > 0")
    if len(latencies_ms) != 4:
        raise ValueError("latencies_ms must contain exactly 4 values")
    for v in latencies_ms:
        if v < 0:
            raise ValueError("latency must be >= 0")
    return [(lat, float(duration_sec)) for lat in latencies_ms]


def _normalize_team(team: str) -> str:
    t = team.strip().upper()
    if t in ("A", "TEAM_A", "TEAMA", "1"):
        return "A"
    if t in ("B", "TEAM_B", "TEAMB", "2"):
        return "B"
    raise ValueError("team must be A or B")


# ---------------------------
# Legacy schedule parser (kept for compatibility)
# ---------------------------

def parse_schedule(schedule: str) -> List[Tuple[int, float]]:
    """
    Parse schedule string like:
        "0:5,50:5,100:5,200:5"
    into:
        [(0, 5.0), (50, 5.0), (100, 5.0), (200, 5.0)]
    """
    items: List[Tuple[int, float]] = []
    if not schedule.strip():
        return items

    for chunk in schedule.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if ":" not in chunk:
            raise ValueError(f"Invalid schedule item '{chunk}'. Expected 'latency:seconds'.")
        lat_s, sec_s = chunk.split(":", 1)
        lat = int(lat_s.strip())
        sec = float(sec_s.strip())
        if lat < 0 or sec <= 0:
            raise ValueError(f"Invalid schedule values in '{chunk}'.")
        items.append((lat, sec))
    return items


DEFAULT_START_SCHEDULE = "0:5,50:5,100:5,200:5"


# ---------------------------
# EXP3 sequences (Random walk)
# ---------------------------

EXP3_LATENCY_SEQUENCES: List[List[int]] = [
    # Seq 1
    [50, 40, 50, 60, 50, 40, 30, 20, 10, 0, 10, 20, 10, 20, 10, 20, 30, 40, 50, 60, 70, 60, 70, 80, 90, 80, 90, 100, 100, 90, 100],
    # Seq 2
    [50, 60, 70, 80, 90, 100, 90, 80, 70, 80, 90, 80, 70, 60, 50, 40, 30, 40, 30, 40, 30, 40, 50, 40, 30, 20, 10, 0, 0, 10, 0],
    # Seq 3
    [50, 60, 70, 80, 70, 60, 50, 40, 50, 60, 70, 80, 90, 100, 90, 80, 70, 60, 50, 40, 30, 40, 30, 20, 10, 0, 10, 20, 10, 20, 30],
    # Seq 4
    [50, 60, 70, 80, 90, 100, 90, 80, 70, 80, 70, 60, 50, 40, 30, 20, 10, 0, 10, 0, 0, 10, 20, 30, 40, 50, 40, 50, 60, 70, 80],
    # Seq 5
    [50, 60, 50, 40, 30, 40, 50, 40, 30, 20, 10, 0, 0, 10, 20, 30, 40, 30, 40, 50, 60, 70, 80, 70, 80, 90, 80, 90, 80, 90, 100],
    # Seq 6
    [50, 60, 70, 80, 90, 100, 90, 80, 90, 80, 70, 80, 70, 60, 50, 40, 50, 60, 50, 40, 30, 40, 30, 20, 10, 20, 10, 0, 0, 0, 0],
    # Seq 7
    [50, 60, 50, 60, 70, 80, 90, 100, 90, 80, 90, 80, 90, 80, 70, 60, 50, 40, 50, 40, 30, 20, 10, 0, 10, 20, 30, 20, 30, 40, 30],
    # Seq 8
    [50, 40, 30, 20, 30, 40, 30, 20, 10, 0, 10, 20, 30, 40, 50, 60, 50, 60, 50, 60, 70, 80, 90, 100, 90, 100, 100, 90, 100, 100, 100],
    # Seq 9
    [50, 60, 50, 60, 70, 80, 90, 100, 90, 100, 100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 0, 10, 0, 0, 10, 20, 30, 40, 50, 60, 70],
    # Seq 10
    [50, 40, 30, 40, 30, 20, 10, 0, 10, 0, 10, 20, 30, 40, 50, 40, 50, 60, 70, 60, 50, 60, 70, 80, 70, 80, 90, 100, 100, 90, 80],
]


# ---------------------------
# Networking / command channel
# ---------------------------

class CommandChannel:
    """
    Thread-safe request/response channel over one TCP connection.
    Ensures schedule thread and interactive thread won't interleave send/recv.
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

    # metadata for "servers" / teams
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

        # Handshake (server prints it, doesn't respond)
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
      - send to a specific server (by host)
      - thread-safe removal on failure

    Plus:
      - number/team management used by "servers/delete/move/swap"
      - targeted send to a team
      - allocate number for newly connected servers
    """
    def __init__(self):
        self._lock = threading.RLock()
        self._conns: Dict[str, ServerConnection] = {}

    def add(self, conn: ServerConnection) -> None:
        with self._lock:
            self._conns[conn.label] = conn

    def labels(self) -> List[str]:
        with self._lock:
            return sorted(self._conns.keys())

    def conns(self) -> List[ServerConnection]:
        with self._lock:
            return list(self._conns.values())

    def get_by_label(self, label: str) -> Optional[ServerConnection]:
        with self._lock:
            return self._conns.get(label)

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

    def used_numbers(self) -> set:
        with self._lock:
            return {c.number for c in self._conns.values() if c.number is not None}

    def count_team(self, team: str) -> int:
        t = _normalize_team(team)
        with self._lock:
            return sum(1 for c in self._conns.values() if (c.team or "").upper() == t)

    def allocate_number(self, max_n: int = 10) -> Optional[int]:
        used = self.used_numbers()
        for n in range(1, max_n + 1):
            if n not in used:
                return n
        return None

    def broadcast_collect(self, cmd: str) -> Dict[str, str]:
        """
        Send a command to ALL servers.
        Returns a dict: label -> response (or error string).
        If a server disconnects, it is removed.
        """
        with self._lock:
            conns = list(self._conns.values())

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

    def team_collect(self, team: str, cmd: str) -> Dict[str, str]:
        """
        Send a command only to servers in a given team (A or B).
        Returns dict label -> response.
        Removes dead servers on error.
        """
        t = _normalize_team(team)
        with self._lock:
            conns = [c for c in self._conns.values() if (c.team or "").upper() == t]

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

    # --------------------
    # roster helpers
    # --------------------

    def renumber_compact(self) -> None:
        """
        Reassign numbers 1..N (in ascending order of current number).
        Teams are NOT changed.
        """
        with self._lock:
            conns = list(self._conns.values())
            conns.sort(key=lambda c: (c.number if c.number is not None else 10**9, c.label))
            for i, c in enumerate(conns, start=1):
                c.number = i

    def print_servers(self) -> None:
        """
        Pretty print Team A / Team B with server numbers.
        """
        conns = self.conns()
        if not conns:
            print("[tcp_client] No connected servers.")
            return

        def _num(c: ServerConnection) -> int:
            return c.number if c.number is not None else 10**9

        team_a = sorted([c for c in conns if (c.team or "A").upper() == "A"], key=_num)
        team_b = sorted([c for c in conns if (c.team or "B").upper() == "B"], key=_num)
        team_other = sorted([c for c in conns if (c.team or "").upper() not in ("A", "B")], key=_num)

        print("\n[tcp_client] Connected servers by team:")
        print(f"  Team A ({len(team_a)}/5):")
        for c in team_a:
            n = c.number if c.number is not None else "?"
            print(f"    {n:>2}) {c.label}")
        print(f"  Team B ({len(team_b)}/5):")
        for c in team_b:
            n = c.number if c.number is not None else "?"
            print(f"    {n:>2}) {c.label}")
        if team_other:
            print(f"  Other ({len(team_other)}):")
            for c in team_other:
                n = c.number if c.number is not None else "?"
                print(f"    {n:>2}) {c.label}")

        print("\n[tcp_client] Server management commands:")
        print("  connect to <ip1> <ip2> ...      -> connect/reconnect (HOST[:PORT], commas allowed)")
        print("  delete <number>                 -> remove server + renumber 1..N")
        print("  move <number> to A|B            -> move one server to another team")
        print("  swap <n1> <n2>                  -> swap teams of two servers (like swap 2 7)")
        print("")

    def delete_number(self, number: int) -> bool:
        """
        Delete server by its number. Renumbers remaining servers immediately.
        Returns True if deleted, False if not found.
        """
        conn = self.get_by_number(number)
        if conn is None:
            return False
        self.remove_label(conn.label)
        self.renumber_compact()
        return True

    def move_number_to_team(self, number: int, team: str) -> bool:
        """
        Move a server (by number) to Team A or Team B.
        Returns True on success, False if server not found.
        """
        t = _normalize_team(team)
        with self._lock:
            conn = None
            for c in self._conns.values():
                if c.number == number:
                    conn = c
                    break
            if conn is None:
                return False
            conn.team = t
        return True

    def swap_numbers(self, n1: int, n2: int) -> bool:
        """
        Swap teams of two servers identified by their numbers.
        Returns True on success, False if either number not found.
        """
        with self._lock:
            c1 = None
            c2 = None
            for c in self._conns.values():
                if c.number == n1:
                    c1 = c
                elif c.number == n2:
                    c2 = c
            if c1 is None or c2 is None:
                return False
            c1.team, c2.team = c2.team, c1.team
        return True


class ScheduleRunner:
    """
    Runs SET_LATENCY schedule across ALL connected servers in a background thread.
    """
    def __init__(self, pool: ConnectionPool):
        self.pool = pool
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, schedule_items: List[Tuple[int, float]], loop: bool = True) -> None:
        if not schedule_items:
            raise ValueError("Schedule is empty.")
        if self.running():
            print("[tcp_client] Schedule already running. Type 'stop' first.")
            return

        if not self.pool.labels():
            print("[tcp_client] No connected servers. Cannot start schedule.")
            return

        self._stop_evt.clear()

        def _worker():
            print("[tcp_client] Schedule mode started (ALL servers).")
            print("[tcp_client] Schedule items:", schedule_items)
            print("[tcp_client] Type 'stop' to stop schedule.\n")
            try:
                while not self._stop_evt.is_set():
                    for lat_ms, duration_sec in schedule_items:
                        if self._stop_evt.is_set():
                            break
                        res = self.pool.broadcast_collect(f"SET_LATENCY {lat_ms}")
                        for lbl, resp in res.items():
                            print(f"[tcp_client] ({lbl}) SET_LATENCY {lat_ms} -> {resp}")
                        if self._stop_evt.wait(duration_sec):
                            break
                    if not loop:
                        break
            except Exception as e:
                print(f"[tcp_client] Schedule thread error: {e}")
            print("[tcp_client] Schedule stopped.\n")

        self._thread = threading.Thread(target=_worker, name="schedule-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.running():
            return
        self._stop_evt.set()
        self._thread.join(timeout=2.0)


class Exp2Runner:
    """
    Experiment 2 latency runner:
      - Only Team A (team1)
      - Values: 0,20,40,60,80,100 ms
      - Shuffled each 30-min cycle
      - 5 minutes per value
      - Loop forever until stop()
    """
    def __init__(self, pool: ConnectionPool, team: str = "A"):
        self.pool = pool
        self.team = _normalize_team(team)
        self.block_sec = 5 * 60.0

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running():
            print("[tcp_client] start_exp_2 latency runner already running. Type 'stop' first.")
            return

        self._stop_evt.clear()

        def _worker():
            base = [0, 20, 40, 60, 80, 100]
            last_seq: Optional[List[int]] = None
            cycle = 0
            print(f"[tcp_client] EXP2 latency loop started for Team {self.team}.")
            print("[tcp_client] Discrete latencies:", base)
            print("[tcp_client] 5 minutes per value; new shuffle each 30-minute cycle.")
            print("[tcp_client] Type 'stop' to stop exp2.\n")

            try:
                while not self._stop_evt.is_set():
                    cycle += 1

                    seq = base.copy()
                    # Ensure different from last cycle (as requested)
                    for _ in range(20):
                        random.shuffle(seq)
                        if last_seq is None or seq != last_seq:
                            break
                    last_seq = seq

                    print(f"[tcp_client] EXP2 cycle {cycle} shuffle: {seq}")

                    for lat in seq:
                        if self._stop_evt.is_set():
                            break

                        res = self.pool.team_collect(self.team, f"SET_LATENCY {lat}")
                        if not res:
                            print(f"[tcp_client] (Team {self.team}) No servers to apply latency {lat} ms.")
                        else:
                            for lbl, resp in res.items():
                                print(f"[tcp_client] (Team {self.team}) ({lbl}) SET_LATENCY {lat} -> {resp}")

                        # 5 minutes per latency value
                        if self._stop_evt.wait(self.block_sec):
                            break

            except Exception as e:
                print(f"[tcp_client] EXP2 latency thread error: {e}")

            print("[tcp_client] EXP2 latency loop stopped.\n")

        self._thread = threading.Thread(target=_worker, name="exp2-latency", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.running():
            return
        self._stop_evt.set()
        self._thread.join(timeout=2.0)


class Exp3Runner:
    """
    Experiment 3 latency runner (Random Walk sequences):
      - Only Team A (team1)
      - Uses 1 randomly chosen sequence out of 10
      - 1 minute per value
      - Full pass = 30 minutes (we use the first 30 values of the sequence)
      - Loop back to the first value of the same sequence forever until stop()
    """
    def __init__(self, pool: ConnectionPool, team: str = "A"):
        self.pool = pool
        self.team = _normalize_team(team)
        self.block_sec = 60.0  # 1 minute per latency value

        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._seq_id: Optional[int] = None
        self._seq_blocks: Optional[List[int]] = None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running():
            print("[tcp_client] start_exp_3 latency runner already running. Type 'stop' first.")
            return

        # Choose 1 sequence at random (1..10)
        seq_idx = random.randrange(0, len(EXP3_LATENCY_SEQUENCES))
        raw_seq = EXP3_LATENCY_SEQUENCES[seq_idx]

        # Spec says: 30 minutes total. Provided sequences have 31 values; we use first 30.
        seq_blocks = raw_seq[:30] if len(raw_seq) >= 30 else raw_seq.copy()

        self._seq_id = seq_idx + 1
        self._seq_blocks = seq_blocks

        self._stop_evt.clear()

        def _worker():
            assert self._seq_id is not None
            assert self._seq_blocks is not None

            print(f"[tcp_client] EXP3 latency loop started for Team {self.team}.")
            print(f"[tcp_client] Using sequence #{self._seq_id}.")
            print("[tcp_client] 1 minute per value; full pass 30 minutes; loop same sequence.")
            print("[tcp_client] Type 'stop' to stop exp3.\n")

            cycle = 0
            try:
                while not self._stop_evt.is_set():
                    cycle += 1
                    print(f"[tcp_client] EXP3 cycle {cycle} (sequence #{self._seq_id})")

                    for lat in self._seq_blocks:
                        if self._stop_evt.is_set():
                            break

                        res = self.pool.team_collect(self.team, f"SET_LATENCY {lat}")
                        if not res:
                            print(f"[tcp_client] (Team {self.team}) No servers to apply latency {lat} ms.")
                        else:
                            for lbl, resp in res.items():
                                print(f"[tcp_client] (Team {self.team}) ({lbl}) SET_LATENCY {lat} -> {resp}")

                        if self._stop_evt.wait(self.block_sec):
                            break

            except Exception as e:
                print(f"[tcp_client] EXP3 latency thread error: {e}")

            print("[tcp_client] EXP3 latency loop stopped.\n")

        self._thread = threading.Thread(target=_worker, name="exp3-latency", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.running():
            return
        self._stop_evt.set()
        self._thread.join(timeout=2.0)


def _parse_server_item(item: str, default_port: int) -> Tuple[str, int]:
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


def _parse_connect_targets(user_line: str) -> List[str]:
    """
    Parse a line like:
      "connect to 1.2.3.4, 5.6.7.8 9.9.9.9:5001"
    into tokens:
      ["1.2.3.4", "5.6.7.8", "9.9.9.9:5001"]
    """
    s = user_line.strip()

    # remove leading "connect"
    if s.lower().startswith("connect"):
        s = s[len("connect"):].strip()

    # optional "to"
    if s.lower().startswith("to "):
        s = s[3:].strip()

    # commas -> spaces
    s = s.replace(",", " ")
    toks = [t.strip() for t in s.split() if t.strip()]
    return toks


def run_client(
    servers: List[Tuple[str, int]],
    client_name: str,
    connect_timeout_sec: float,
    default_port: int,
) -> int:
    pool = ConnectionPool()

    # Connect to all servers at startup
    print("[tcp_client] Connecting to servers:")
    for host, port in servers:
        label = f"{host}:{port}"
        try:
            conn = ServerConnection(
                host=host,
                port=port,
                client_name=client_name,
                connect_timeout_sec=connect_timeout_sec,
            )
            conn.connect()

            number = pool.allocate_number(max_n=10)
            if number is None:
                conn.close()
                print(f"  ❌ {label}  (too many servers: max 10)")
                continue

            # default team fill (A then B)
            team_a = pool.count_team("A")
            team_b = pool.count_team("B")
            if team_a < 5:
                team = "A"
            elif team_b < 5:
                team = "B"
            else:
                team = "A" if team_a <= team_b else "B"

            conn.number = number
            conn.team = team

            pool.add(conn)
            print(f"  ✅ {label}  (# {conn.number}, Team {conn.team})")
        except Exception as e:
            print(f"  ❌ {label}  ({e})")

    if not pool.labels():
        print("[tcp_client] No servers connected. Exiting.")
        return 1

    scheduler = ScheduleRunner(pool)
    exp2_runner = Exp2Runner(pool, team="A")
    exp3_runner = Exp3Runner(pool, team="A")
    exp_mode: Optional[str] = None  # None | "exp1" | "exp2" | "exp3"

    def _print_result_map(prefix: str, res: Dict[str, str]) -> None:
        for lbl, resp in res.items():
            print(f"{prefix}{lbl}] {resp}")

    def _stop_any_modes() -> None:
        nonlocal exp_mode
        if scheduler.running():
            scheduler.stop()
        if exp2_runner.running():
            exp2_runner.stop()
        if exp3_runner.running():
            exp3_runner.stop()
        exp_mode = None

    print("\n[tcp_client] Commands (latency commands apply to ALL connected servers by default):")
    print("  servers                         -> list connected servers (Team A/B + numbers)")
    print("  connect to <ip1> <ip2> ...       -> connect/reconnect servers (HOST[:PORT], commas allowed)")
    print("  delete <number>                 -> delete server + renumber")
    print("  move <number> to A|B            -> move a server to another team")
    print("  swap <n1> <n2>                  -> swap teams of two servers (example: swap 2 7)")
    print("")
    print("  set <ms>                        -> send SET_LATENCY <ms> to ALL servers")
    print("  start_latency_loop              -> prompt duration + 4 latency values, loop forever (ALL servers)")
    print("  start_latency_loop <mm:ss|sec> <lat1> <lat2> <lat3> <lat4>")
    print("                                  -> same as above, but no prompts")
    print("  start_exp_1                     -> OBS ALL ON + INPUT ON, no latency (STOP injector)")
    print("  start_exp_2                     -> OBS ALL ON + INPUT ON + Team A shuffled discrete latency (0..100)")
    print("  start_exp_3                     -> OBS ALL ON + INPUT ON + Team A random-walk sequence (1-min steps)")
    print("  stop                            -> stop running schedule/exp + STOP latency injector;")
    print("                                    if exp1/exp2/exp3: also OBS ALL OFF + INPUT OFF")
    print("  ping                            -> send PING to ALL servers")
    print("  obs <ip> on [match]             -> start OBS recording on ONE server (server host == <ip>)")
    print("  obs <ip> off                    -> stop OBS recording on ONE server (server host == <ip>)")
    print("  obs all on [match]              -> send OBS_ON_ALL to ALL servers")
    print("  obs all off                     -> send OBS_OFF_ALL to ALL servers")
    print("  input on/off                    -> send INPUT_ON / INPUT_OFF to ALL servers")
    print("  quit                            -> stop schedule + send QUIT to ALL servers\n")

    while True:
        try:
            user = input("client> ").strip()
        except EOFError:
            user = "quit"

        if not user:
            continue

        parts = user.split()
        cmd = parts[0].lower()

        # ----------------
        # server roster UI
        # ----------------
        if cmd == "servers":
            pool.print_servers()
            continue

        # ----------------
        # connect
        # ----------------
        if cmd == "connect":
            targets = _parse_connect_targets(user)
            if not targets:
                print("[tcp_client] Usage: connect to <ip1> <ip2> ... (HOST[:PORT], commas allowed)")
                continue

            print("[tcp_client] Connecting to:")
            for t in targets:
                try:
                    host, port = _parse_server_item(t, default_port=default_port)
                except Exception as e:
                    print(f"  ❌ {t} (invalid: {e})")
                    continue

                label = f"{host}:{port}"

                # If already in pool, verify alive with PING.
                existing = pool.get_by_label(label)
                old_number = None
                old_team = None
                if existing is not None:
                    try:
                        pong = existing.send("PING")
                        print(f"  ✅ {label} (already connected: {pong})")
                        continue
                    except Exception:
                        old_number = existing.number
                        old_team = existing.team
                        print(f"  ⚠️  {label} (was connected but dead). Reconnecting...")
                        pool.remove_label(label)

                # Enforce max 10 servers
                number = pool.allocate_number(max_n=10)
                if number is None:
                    print(f"  ❌ {label} (too many servers: max 10)")
                    continue

                conn = ServerConnection(
                    host=host,
                    port=port,
                    client_name=client_name,
                    connect_timeout_sec=connect_timeout_sec,
                )
                try:
                    conn.connect()
                except Exception as e:
                    print(f"  ❌ {label} (connect failed: {e})")
                    continue

                # number/team assignment (preserve if reconnecting)
                if old_number is not None and 1 <= old_number <= 10 and old_number not in pool.used_numbers():
                    conn.number = old_number
                else:
                    conn.number = number

                if old_team is not None:
                    conn.team = old_team
                else:
                    team_a = pool.count_team("A")
                    team_b = pool.count_team("B")
                    if team_a < 5:
                        conn.team = "A"
                    elif team_b < 5:
                        conn.team = "B"
                    else:
                        conn.team = "A" if team_a <= team_b else "B"

                pool.add(conn)
                print(f"  ✅ {label}  (# {conn.number}, Team {conn.team})")

            pool.print_servers()
            continue

        if cmd == "delete":
            if len(parts) != 2:
                print("[tcp_client] Usage: delete <number>")
                continue
            try:
                n = int(parts[1])
            except ValueError:
                print("[tcp_client] delete: <number> must be an integer")
                continue
            ok = pool.delete_number(n)
            if not ok:
                print(f"[tcp_client] No server with number {n}. Type 'servers' to view.")
            else:
                print(f"[tcp_client] Deleted server #{n} and renumbered.")
                pool.print_servers()
            continue

        if cmd == "move":
            # move <number> to A|B
            if len(parts) != 4 or parts[2].lower() != "to":
                print("[tcp_client] Usage: move <number> to A|B")
                continue
            try:
                n = int(parts[1])
                team = _normalize_team(parts[3])
            except Exception as e:
                print(f"[tcp_client] move: invalid args ({e})")
                continue
            ok = pool.move_number_to_team(n, team)
            if not ok:
                print(f"[tcp_client] No server with number {n}. Type 'servers' to view.")
            else:
                print(f"[tcp_client] Moved server #{n} to Team {team}.")
                pool.print_servers()
            continue

        if cmd == "swap":
            # swap <n1> <n2>  OR  swap <n1> and <n2>
            if len(parts) == 4 and parts[2].lower() == "and":
                n1_s, n2_s = parts[1], parts[3]
            elif len(parts) == 3:
                n1_s, n2_s = parts[1], parts[2]
            else:
                print("[tcp_client] Usage: swap <n1> <n2>   (or: swap <n1> and <n2>)")
                continue
            try:
                n1 = int(n1_s)
                n2 = int(n2_s)
            except ValueError:
                print("[tcp_client] swap: numbers must be integers")
                continue
            ok = pool.swap_numbers(n1, n2)
            if not ok:
                print("[tcp_client] swap: one (or both) numbers not found. Type 'servers' to view.")
            else:
                print(f"[tcp_client] Swapped teams of server #{n1} and server #{n2}.")
                pool.print_servers()
            continue

        # -------------------------
        # Experiment start commands
        # -------------------------
        if cmd == "start_exp_1":
            _stop_any_modes()

            print("[tcp_client] start_exp_1: ensuring NO latency, turning OBS+INPUT ON (ALL servers).")

            res = pool.broadcast_collect("STOP")
            _print_result_map("[tcp_server:", res)

            res = pool.broadcast_collect("OBS_ON_ALL")
            _print_result_map("[tcp_server:", res)

            res = pool.broadcast_collect("INPUT_ON")
            _print_result_map("[tcp_server:", res)

            exp_mode = "exp1"
            print("[tcp_client] EXP1 started. Type 'stop' to end EXP1 (OBS OFF + INPUT OFF).")
            continue

        if cmd == "start_exp_2":
            _stop_any_modes()

            print("[tcp_client] start_exp_2: resetting latency, turning OBS+INPUT ON (ALL servers),")
            print("[tcp_client] then starting Team A shuffled discrete latency schedule (0..100ms, 5min blocks).")

            res = pool.broadcast_collect("STOP")
            _print_result_map("[tcp_server:", res)

            res = pool.broadcast_collect("OBS_ON_ALL")
            _print_result_map("[tcp_server:", res)

            res = pool.broadcast_collect("INPUT_ON")
            _print_result_map("[tcp_server:", res)

            exp2_runner.start()
            exp_mode = "exp2"
            print("[tcp_client] EXP2 started. Type 'stop' to end EXP2 (OBS OFF + INPUT OFF + STOP latency).")
            continue

        if cmd == "start_exp_3":
            _stop_any_modes()

            print("[tcp_client] start_exp_3: resetting latency, turning OBS+INPUT ON (ALL servers),")
            print("[tcp_client] then starting Team A random-walk sequence (1-min steps, 30-min cycles).")

            res = pool.broadcast_collect("STOP")
            _print_result_map("[tcp_server:", res)

            res = pool.broadcast_collect("OBS_ON_ALL")
            _print_result_map("[tcp_server:", res)

            res = pool.broadcast_collect("INPUT_ON")
            _print_result_map("[tcp_server:", res)

            exp3_runner.start()
            exp_mode = "exp3"
            print("[tcp_client] EXP3 started. Type 'stop' to end EXP3 (OBS OFF + INPUT OFF + STOP latency).")
            continue

        # ----------------
        # existing commands
        # ----------------
        if cmd == "set":
            if len(parts) != 2:
                print("[tcp_client] Usage: set <ms>")
                continue
            try:
                ms = int(parts[1])
                if ms < 0:
                    raise ValueError()
            except ValueError:
                print("[tcp_client] Latency must be an integer >= 0")
                continue

            res = pool.broadcast_collect(f"SET_LATENCY {ms}")
            for lbl, resp in res.items():
                print(f"[tcp_server:{lbl}] {resp}")
            continue

        # "start" is deprecated -> redirect to start_latency_loop
        if cmd == "start":
            print("[tcp_client] 'start' is deprecated. Use 'start_latency_loop'.")
            cmd = "start_latency_loop"

        if cmd == "start_latency_loop":
            # Starting manual schedule cancels experiment modes (but does not turn off OBS/INPUT).
            if exp_mode is not None:
                print("[tcp_client] Stopping current experiment mode before starting manual latency loop.")
            _stop_any_modes()

            duration_sec: Optional[float] = None
            latencies: List[int] = []

            if len(parts) == 1:
                duration_sec = _prompt_duration_seconds()
                if duration_sec is None:
                    print("[tcp_client] Canceled.")
                    continue
                for i in range(1, 5):
                    v = _prompt_latency_ms(i)
                    if v is None:
                        print("[tcp_client] Canceled.")
                        duration_sec = None
                        break
                    latencies.append(v)
                if duration_sec is None:
                    continue
            else:
                if len(parts) != 6:
                    print("[tcp_client] Usage: start_latency_loop <mm:ss|sec> <lat1> <lat2> <lat3> <lat4>")
                    continue
                try:
                    duration_sec = parse_mmss_or_seconds(parts[1])
                    if duration_sec <= 0:
                        raise ValueError("duration must be > 0")
                    latencies = [int(x) for x in parts[2:6]]
                    if any(x < 0 for x in latencies):
                        raise ValueError("latency must be >= 0")
                except Exception as e:
                    print(f"[tcp_client] Invalid args: {e}")
                    continue

            schedule_items = build_latency_loop_schedule(duration_sec, latencies)
            scheduler.start(schedule_items, loop=True)
            continue

        if cmd == "stop":
            # stop background work
            scheduler.stop()
            exp2_runner.stop()
            exp3_runner.stop()

            # If we're in exp1/exp2/exp3, also stop OBS and INPUT
            if exp_mode in ("exp1", "exp2", "exp3"):
                print("[tcp_client] stop: ending experiment -> OBS OFF ALL + INPUT OFF")
                res = pool.broadcast_collect("OBS_OFF_ALL")
                _print_result_map("[tcp_server:", res)

                res = pool.broadcast_collect("INPUT_OFF")
                _print_result_map("[tcp_server:", res)

            # Always stop latency injector on all servers
            res = pool.broadcast_collect("STOP")
            _print_result_map("[tcp_server:", res)

            exp_mode = None
            print("[tcp_client] Stopped.\n")
            continue

        if cmd == "ping":
            res = pool.broadcast_collect("PING")
            for lbl, resp in res.items():
                print(f"[tcp_server:{lbl}] {resp}")
            continue

        # OBS CONTROL
        if cmd == "obs":
            if len(parts) < 3:
                print("[tcp_client] Usage:")
                print("  obs <ip> on [match_id]")
                print("  obs <ip> off")
                print("  obs all on [match_id]")
                print("  obs all off")
                continue

            target = parts[1].lower()
            sub = parts[2].lower()

            if target == "all":
                if sub not in ("on", "off"):
                    print("[tcp_client] Usage: obs all on [match_id]  |  obs all off")
                    continue
                if sub == "on":
                    match_id = parts[3] if len(parts) >= 4 else ""
                    server_cmd = f"OBS_ON_ALL {match_id}".strip()
                else:
                    server_cmd = "OBS_OFF_ALL"

                res = pool.broadcast_collect(server_cmd)
                for lbl, resp in res.items():
                    print(f"[tcp_server:{lbl}] {resp}")
                continue

            # Target ONE server (host == ip)
            ip = parts[1]
            if sub not in ("on", "off"):
                print("[tcp_client] Usage: obs <ip> on [match_id]  |  obs <ip> off")
                continue

            conn = pool.get_by_host(ip)
            if conn is None:
                print(f"[tcp_client] No connected server with host '{ip}'. Type 'servers' to see connections.")
                continue

            if sub == "on":
                match_id = parts[3] if len(parts) >= 4 else ""
                server_cmd = f"OBS_ON {ip} {match_id}".strip()
            else:
                server_cmd = f"OBS_OFF {ip}"

            try:
                resp = conn.send(server_cmd)
                print(f"[tcp_server:{conn.label}] {resp}")
            except Exception as e:
                print(f"[tcp_client] OBS command failed on {conn.label}: {e}")
                pool.remove_label(conn.label)
            continue

        # INPUT CONTROL (broadcast)
        if cmd == "input":
            if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
                print("[tcp_client] Usage: input on|off")
                continue
            sub = parts[1].lower()
            server_cmd = "INPUT_ON" if sub == "on" else "INPUT_OFF"

            res = pool.broadcast_collect(server_cmd)
            for lbl, resp in res.items():
                print(f"[tcp_server:{lbl}] {resp}")
            continue

        if cmd == "quit":
            # stop all background work
            if scheduler.running():
                scheduler.stop()
            exp2_runner.stop()
            exp3_runner.stop()

            # If in experiment, try to turn off OBS/input too
            if exp_mode in ("exp1", "exp2", "exp3"):
                pool.broadcast_collect("OBS_OFF_ALL")
                pool.broadcast_collect("INPUT_OFF")

            # Stop injectors & quit servers
            pool.broadcast_collect("STOP")
            res = pool.broadcast_collect("QUIT")
            for lbl, resp in res.items():
                print(f"[tcp_server:{lbl}] {resp}")
            pool.close_all()
            return 0

        print("[tcp_client] Unknown command. Use: servers/connect/delete/move/swap/set/start_latency_loop/start_exp_1/start_exp_2/start_exp_3/stop/ping/obs/input/quit")


def _cli() -> int:
    p = argparse.ArgumentParser(
        description="DOTA2 TCP client (controller): connect to MULTIPLE servers and send commands."
    )
    p.add_argument(
        "--server",
        nargs="+",
        required=True,
        metavar="HOST[:PORT]",
        help="One or more server IPs/hosts. Example: --server 192.168.0.231 192.168.0.227 192.168.0.100",
    )
    p.add_argument(
        "--server-port",
        type=int,
        default=5000,
        help="Default server port (used when HOST doesn't include :PORT)",
    )
    p.add_argument("--client-name", default=socket.gethostname(), help="Name to identify this controller")
    p.add_argument("--connect-timeout", type=float, default=5.0, help="TCP connect timeout seconds")

    args = p.parse_args()

    servers: List[Tuple[str, int]] = []
    for item in args.server:
        try:
            servers.append(_parse_server_item(item, default_port=args.server_port))
        except Exception as e:
            raise SystemExit(f"Invalid --server entry '{item}': {e}")

    return run_client(
        servers=servers,
        client_name=args.client_name,
        connect_timeout_sec=args.connect_timeout,
        default_port=args.server_port,
    )


if __name__ == "__main__":
    raise SystemExit(_cli())