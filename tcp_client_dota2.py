from __future__ import annotations

import argparse
import socket
import threading
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

    - number/team management helpers used by "servers/delete/move/swap"
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

    def conns_for_team(self, team: str) -> List[ServerConnection]:
        t = _normalize_team(team)
        with self._lock:
            return [c for c in self._conns.values() if (c.team or "").upper() == t]

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
        Send a command to all servers in one team (A or B).
        Returns label -> response. Removes dead connections.
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
        print(f"  Team A (team 1) ({len(team_a)}/5):")
        for c in team_a:
            n = c.number if c.number is not None else "?"
            print(f"    {n:>2}) {c.label}")
        print(f"  Team B (team 2) ({len(team_b)}/5):")
        for c in team_b:
            n = c.number if c.number is not None else "?"
            print(f"    {n:>2}) {c.label}")
        if team_other:
            print(f"  Other ({len(team_other)}):")
            for c in team_other:
                n = c.number if c.number is not None else "?"
                print(f"    {n:>2}) {c.label}")

        print("\n[tcp_client] Server management commands:")
        print("  delete <number>            -> remove server + renumber 1..N")
        print("  move <number> to A|B       -> move one server to another team")
        print("  swap <n1> <n2>             -> swap teams of two servers (like swap 2 7)")
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
    Runs SET_LATENCY schedule across ALL connected servers OR one team.
    """
    def __init__(self, pool: ConnectionPool):
        self.pool = pool
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(
        self,
        schedule_items: List[Tuple[int, float]],
        loop: bool = True,
        team: Optional[str] = None,  # None = ALL, else "A"/"B"
    ) -> None:
        if not schedule_items:
            raise ValueError("Schedule is empty.")
        if self.running():
            print("[tcp_client] Schedule already running. Type 'stop' first.")
            return

        if not self.pool.labels():
            print("[tcp_client] No connected servers. Cannot start schedule.")
            return

        target_desc = "ALL servers"
        team_norm: Optional[str] = None
        if team is not None:
            try:
                team_norm = _normalize_team(team)
            except Exception:
                print("[tcp_client] Invalid team for schedule. Use A or B.")
                return
            if not self.pool.conns_for_team(team_norm):
                print(f"[tcp_client] No connected servers in Team {team_norm}. Cannot start schedule.")
                return
            target_desc = f"Team {team_norm} only"

        self._stop_evt.clear()

        def _worker():
            print(f"[tcp_client] Schedule mode started ({target_desc}).")
            print("[tcp_client] Schedule items:", schedule_items)
            print("[tcp_client] Type 'stop' to stop schedule.\n")
            try:
                while not self._stop_evt.is_set():
                    for lat_ms, duration_sec in schedule_items:
                        if self._stop_evt.is_set():
                            break
                        if team_norm is None:
                            res = self.pool.broadcast_collect(f"SET_LATENCY {lat_ms}")
                        else:
                            res = self.pool.team_collect(team_norm, f"SET_LATENCY {lat_ms}")

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
            print("[tcp_client] Schedule is not running.")
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


def _print_broadcast_results(results: Dict[str, str], prefix: str = "tcp_server") -> None:
    for lbl, resp in results.items():
        print(f"[{prefix}:{lbl}] {resp}")


def run_client(
    servers: List[Tuple[str, int]],
    client_name: str,
    connect_timeout_sec: float,
) -> int:
    pool = ConnectionPool()

    # Connect to all servers at startup
    print("[tcp_client] Connecting to servers:")
    next_number = 1
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

            if next_number > 10:
                # Hard limit requested (1..10)
                conn.close()
                print(f"  ❌ {label}  (too many servers: max 10)")
                continue

            conn.number = next_number
            conn.team = "A" if next_number <= 5 else "B"
            next_number += 1

            pool.add(conn)
            print(f"  ✅ {label}  (# {conn.number}, Team {conn.team})")
        except Exception as e:
            print(f"  ❌ {label}  ({e})")

    if not pool.labels():
        print("[tcp_client] No servers connected. Exiting.")
        return 1

    scheduler = ScheduleRunner(pool)

    # experiment state
    exp_active: Optional[str] = None  # "exp1" | "exp2" | "exp3" | None

    print("\n[tcp_client] Commands (latency commands apply to ALL servers unless stated):")
    print("  servers                 -> list connected servers (with Team A/B + numbers)")
    print("  delete <number>         -> delete server + renumber")
    print("  move <number> to A|B    -> move a server to another team")
    print("  swap <n1> <n2>          -> swap teams of two servers (example: swap 2 7)")
    print("")
    print("  set <ms>                -> send SET_LATENCY <ms> to ALL servers")
    print("  start_latency_loop      -> prompt duration + 4 latency values, then loop forever (ALL servers)")
    print("  start_latency_loop <mm:ss|sec> <lat1> <lat2> <lat3> <lat4>")
    print("                          -> same as above, but no prompts")
    print("")
    print("  start_exp_1             -> OBS ON (all) + INPUT ON (all) + NO latency")
    print("  start_exp_2             -> OBS ON (all) + INPUT ON (all) + Team A latency loop 0/50/100/200 every 5min")
    print("  start_exp_3             -> OBS ON (all) + INPUT ON (all) + Team A latency loop 50/0/100/200 every 5min")
    print("")
    print("  stop                    -> stop schedule + STOP latency (all). If experiment active: OBS OFF (all) + INPUT OFF (all)")
    print("  ping                    -> send PING to ALL servers")
    print("  obs <ip> on [match]     -> start OBS recording on ONE server (server host == <ip>)")
    print("  obs <ip> off            -> stop OBS recording on ONE server (server host == <ip>)")
    print("  obs all on [match]      -> send OBS_ON_ALL to ALL servers")
    print("  obs all off             -> send OBS_OFF_ALL to ALL servers")
    print("  input on/off            -> send INPUT_ON / INPUT_OFF to ALL servers")
    print("  quit                    -> stop schedule + send QUIT to ALL servers\n")

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

        # --------------------------
        # NEW: experiment convenience
        # --------------------------
        if cmd in ("start_exp_1", "start_exp_2", "start_exp_3"):
            if exp_active is not None or scheduler.running():
                print("[tcp_client] Something is already running (experiment or latency loop). Type 'stop' first.")
                continue

            # Ensure no previous latency injector is active anywhere
            print("[tcp_client] Clearing latency injector on ALL servers (STOP) ...")
            _print_broadcast_results(pool.broadcast_collect("STOP"))

            # Start recording + input for ALL
            print("[tcp_client] Starting OBS recording on ALL servers ...")
            _print_broadcast_results(pool.broadcast_collect("OBS_ON_ALL"))

            print("[tcp_client] Turning INPUT ON on ALL servers ...")
            _print_broadcast_results(pool.broadcast_collect("INPUT_ON"))

            if cmd == "start_exp_1":
                # Baseline: no latency
                exp_active = "exp1"
                print("[tcp_client] start_exp_1 started (NO latency). Type 'stop' to end.")
                continue

            # Exp 2 / 3: latency schedule on Team A (team 1) only
            team_target = "A"
            if not pool.conns_for_team(team_target):
                print("[tcp_client] No servers in Team A (team 1). Cannot start exp latency loop.")
                print("[tcp_client] Tip: type 'servers' and use 'move <n> to A' to assign servers to Team A.")
                # roll back to avoid leaving them on in a failed start
                print("[tcp_client] Rolling back: stopping OBS (all) and INPUT (all) ...")
                _print_broadcast_results(pool.broadcast_collect("OBS_OFF_ALL"))
                _print_broadcast_results(pool.broadcast_collect("INPUT_OFF"))
                continue

            interval_sec = 5 * 60  # 5 minutes
            if cmd == "start_exp_2":
                lat_order = [0, 50, 100, 200]
                exp_active = "exp2"
                print("[tcp_client] start_exp_2 started (Team A latency: 0 -> 50 -> 100 -> 200, every 5 min).")
            else:
                lat_order = [50, 0, 100, 200]
                exp_active = "exp3"
                print("[tcp_client] start_exp_3 started (Team A latency: 50 -> 0 -> 100 -> 200, every 5 min).")

            schedule_items = build_latency_loop_schedule(interval_sec, lat_order)
            scheduler.start(schedule_items, loop=True, team=team_target)
            print("[tcp_client] Type 'stop' to end.")
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

            _print_broadcast_results(pool.broadcast_collect(f"SET_LATENCY {ms}"))
            continue

        # CHANGED: "start" is deprecated -> redirect to start_latency_loop prompts
        if cmd == "start":
            print("[tcp_client] 'start' is deprecated. Use 'start_latency_loop'.")
            cmd = "start_latency_loop"

        if cmd == "start_latency_loop":
            # Two modes:
            #   1) start_latency_loop                       -> prompt for duration and 4 latencies
            #   2) start_latency_loop 00:05 0 50 100 200    -> no prompts
            if exp_active is not None:
                print("[tcp_client] An experiment is active. Type 'stop' first.")
                continue

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
            scheduler.start(schedule_items, loop=True, team=None)  # ALL
            continue

        if cmd == "stop":
            # stop schedule thread first
            scheduler.stop()

            # If experiment is active: also stop OBS + INPUT, then stop latency injector.
            if exp_active is not None:
                print("[tcp_client] Stopping experiment: OBS OFF (all) ...")
                _print_broadcast_results(pool.broadcast_collect("OBS_OFF_ALL"))

                print("[tcp_client] Stopping experiment: INPUT OFF (all) ...")
                _print_broadcast_results(pool.broadcast_collect("INPUT_OFF"))

            # Always stop latency injector as previous design
            print("[tcp_client] Stopping latency injector on ALL servers (STOP) ...")
            _print_broadcast_results(pool.broadcast_collect("STOP"))

            if exp_active is not None:
                print(f"[tcp_client] Experiment '{exp_active}' stopped.")
                exp_active = None

            continue

        if cmd == "ping":
            _print_broadcast_results(pool.broadcast_collect("PING"))
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

                _print_broadcast_results(pool.broadcast_collect(server_cmd))
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

            _print_broadcast_results(pool.broadcast_collect(server_cmd))
            continue

        if cmd == "quit":
            # If an experiment is active, behave like "stop" first (so OBS/app doesn't keep running)
            scheduler.stop()
            if exp_active is not None:
                print("[tcp_client] Quitting: stopping active experiment first (OBS OFF + INPUT OFF + STOP) ...")
                _print_broadcast_results(pool.broadcast_collect("OBS_OFF_ALL"))
                _print_broadcast_results(pool.broadcast_collect("INPUT_OFF"))
                _print_broadcast_results(pool.broadcast_collect("STOP"))
                exp_active = None

            _print_broadcast_results(pool.broadcast_collect("QUIT"))
            pool.close_all()
            return 0

        print("[tcp_client] Unknown command. Use: servers/set/start_latency_loop/start_exp_1/start_exp_2/start_exp_3/stop/ping/obs/input/quit")


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
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
