from __future__ import annotations

from typing import List, Tuple

"""
servers_control.py

Author: Quang Minh Pham

Description:
This module manages all server-related operations.

Responsibilities:
- Handle "servers" command:
    - Display all connected servers
    - Show server numbers and team assignments
- Handle "connect to <ip...>" command:
    - Connect or reconnect to new servers
- Handle "delete <number>" command:
    - Remove server from pool and renumber
- Handle "move <number> to A|B":
    - Reassign server to another team
- Handle "swap <n1> <n2>":
    - Swap team assignments between two servers

Behavior:
- Servers are assigned numbers (1–10)
- Team assignment:
    - 1–5 → Team A
    - 6–10 → Team B
- Supports dynamic changes during runtime

Notes:
- Works directly with ConnectionPool from core.py
- Does not affect latency or experiment logic directly
"""
from client_core.core import (
    MAX_SERVERS,
    ConnectionPool,
    ServerConnection,
    normalize_team,
    parse_server_item,
    team_for_number,
)


class ServerController:
    """
    Everything related to connected-server roster management:
      - servers
      - connect to <ip1>, <ip2>, ...
      - delete <number>
      - move <number> to A|B
      - swap <n1> <n2>
    """
    def __init__(
        self,
        pool: ConnectionPool,
        client_name: str,
        connect_timeout_sec: float,
        default_server_port: int,
    ):
        self.pool = pool
        self.client_name = client_name
        self.connect_timeout_sec = connect_timeout_sec
        self.default_server_port = default_server_port

    def parse_connect_targets(self, text: str) -> List[Tuple[str, int]]:
        """
        Accepts:
          connect to 192.168.0.10, 192.168.0.11
          connect to 192.168.0.10 192.168.0.11
          connect to 192.168.0.10:5000, 192.168.0.11:5001
        """
        cleaned = text.replace(",", " ")
        items = [x.strip() for x in cleaned.split() if x.strip()]
        if not items:
            raise ValueError("no server IPs provided")
        return [parse_server_item(item, self.default_server_port) for item in items]

    def connect_servers(self, server_items: List[Tuple[str, int]]) -> None:
        if not server_items:
            print("[tcp_client] No servers to connect.")
            return

        print("[tcp_client] Connecting to servers:")
        for host, port in server_items:
            label = f"{host}:{port}"

            if self.pool.has_label(label):
                print(f"  ⚠️  {label}  (already connected)")
                continue

            next_number = self.pool.next_available_number()
            if next_number is None:
                print(f"  ❌ {label}  (too many servers: max {MAX_SERVERS})")
                continue

            try:
                conn = ServerConnection(
                    host=host,
                    port=port,
                    client_name=self.client_name,
                    connect_timeout_sec=self.connect_timeout_sec,
                )
                conn.connect()
                conn.number = next_number
                conn.team = team_for_number(next_number)

                self.pool.add(conn)
                print(f"  ✅ {label}  (# {conn.number}, Team {conn.team})")
            except Exception as e:
                print(f"  ❌ {label}  ({e})")

    def handle_connect_command(self, user_text: str) -> None:
        split3 = user_text.split(None, 2)
        if len(split3) < 3 or split3[1].lower() != "to":
            print("[tcp_client] Usage: connect to <ip1>, <ip2>, ...")
            return
        try:
            targets = self.parse_connect_targets(split3[2])
        except Exception as e:
            print(f"[tcp_client] connect: invalid targets ({e})")
            return
        self.connect_servers(targets)
        self.print_servers()

    def renumber_compact(self) -> None:
        """
        Reassign numbers 1..N (in ascending order of current number).
        Teams are NOT changed.
        """
        conns = self.pool.conns()
        conns.sort(key=lambda c: (c.number if c.number is not None else 10**9, c.label))
        for i, c in enumerate(conns, start=1):
            c.number = i

    def print_servers(self) -> None:
        conns = self.pool.conns()
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
        print("  connect to <ip1>, <ip2>, ...  -> connect/reconnect to more servers")
        print("  delete <number>               -> remove server + renumber 1..N")
        print("  move <number> to A|B          -> move one server to another team")
        print("  swap <n1> <n2>                -> swap teams of two servers (like swap 2 7)")
        print("")

    def delete_number(self, number: int) -> bool:
        conn = self.pool.get_by_number(number)
        if conn is None:
            return False
        self.pool.remove_label(conn.label)
        self.renumber_compact()
        return True

    def move_number_to_team(self, number: int, team: str) -> bool:
        t = normalize_team(team)
        conn = self.pool.get_by_number(number)
        if conn is None:
            return False
        conn.team = t
        return True

    def swap_numbers(self, n1: int, n2: int) -> bool:
        c1 = self.pool.get_by_number(n1)
        c2 = self.pool.get_by_number(n2)
        if c1 is None or c2 is None:
            return False
        c1.team, c2.team = c2.team, c1.team
        return True

    def handle_delete_command(self, parts: List[str]) -> None:
        if len(parts) != 2:
            print("[tcp_client] Usage: delete <number>")
            return
        try:
            n = int(parts[1])
        except ValueError:
            print("[tcp_client] delete: <number> must be an integer")
            return
        ok = self.delete_number(n)
        if not ok:
            print(f"[tcp_client] No server with number {n}. Type 'servers' to view.")
        else:
            print(f"[tcp_client] Deleted server #{n} and renumbered.")
            self.print_servers()

    def handle_move_command(self, parts: List[str]) -> None:
        if len(parts) != 4 or parts[2].lower() != "to":
            print("[tcp_client] Usage: move <number> to A|B")
            return
        try:
            n = int(parts[1])
            team = normalize_team(parts[3])
        except Exception as e:
            print(f"[tcp_client] move: invalid args ({e})")
            return
        ok = self.move_number_to_team(n, team)
        if not ok:
            print(f"[tcp_client] No server with number {n}. Type 'servers' to view.")
        else:
            print(f"[tcp_client] Moved server #{n} to Team {team}.")
            self.print_servers()

    def handle_swap_command(self, parts: List[str]) -> None:
        if len(parts) == 4 and parts[2].lower() == "and":
            n1_s, n2_s = parts[1], parts[3]
        elif len(parts) == 3:
            n1_s, n2_s = parts[1], parts[2]
        else:
            print("[tcp_client] Usage: swap <n1> <n2>   (or: swap <n1> and <n2>)")
            return
        try:
            n1 = int(n1_s)
            n2 = int(n2_s)
        except ValueError:
            print("[tcp_client] swap: numbers must be integers")
            return
        ok = self.swap_numbers(n1, n2)
        if not ok:
            print("[tcp_client] swap: one (or both) numbers not found. Type 'servers' to view.")
        else:
            print(f"[tcp_client] Swapped teams of server #{n1} and server #{n2}.")
            self.print_servers()
