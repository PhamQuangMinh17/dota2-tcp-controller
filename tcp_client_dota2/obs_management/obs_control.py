from __future__ import annotations

from typing import List, Optional

from client_core.core import ConnectionPool, print_results

"""
obs_control.py

Author: Quang Minh Pham

Description:
This module handles all OBS (Open Broadcaster Software) related commands.

Responsibilities:
- Handle "obs all on/off" commands (broadcast to all servers)
- Handle "obs <ip> on/off" commands (single server control)
- Build and send correct OBS commands to server:
    - OBS_ON
    - OBS_OFF
    - OBS_ON_ALL
    - OBS_OFF_ALL
- Ensure correct target selection (single server vs all servers)

Behavior:
- OBS commands are sent through TCP to servers
- Servers handle actual OBS recording via obs_control module (server-side)

Notes:
- This module only sends commands (client-side)
- No direct OBS interaction happens here
"""
class OBSController:
    """
    Everything related to OBS control:
      - obs <ip> on [match_id]
      - obs <ip> off
      - obs all on [match_id]
      - obs all off
    """
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def obs_all_on(self, match_id: str = "") -> None:
        server_cmd = f"OBS_ON_ALL {match_id}".strip()
        res = self.pool.broadcast_collect(server_cmd)
        print_results("[tcp_server:", res)

    def obs_all_off(self) -> None:
        res = self.pool.broadcast_collect("OBS_OFF_ALL")
        print_results("[tcp_server:", res)

    def obs_one_on(self, ip: str, match_id: str = "") -> None:
        conn = self.pool.get_by_host(ip)
        if conn is None:
            print(f"[tcp_client] No connected server with host '{ip}'. Type 'servers' to see connections.")
            return

        server_cmd = f"OBS_ON {ip} {match_id}".strip()
        try:
            resp = conn.send(server_cmd)
            print(f"[tcp_server:{conn.label}] {resp}")
        except Exception as e:
            print(f"[tcp_client] OBS command failed on {conn.label}: {e}")
            self.pool.remove_label(conn.label)

    def obs_one_off(self, ip: str) -> None:
        conn = self.pool.get_by_host(ip)
        if conn is None:
            print(f"[tcp_client] No connected server with host '{ip}'. Type 'servers' to see connections.")
            return

        server_cmd = f"OBS_OFF {ip}"
        try:
            resp = conn.send(server_cmd)
            print(f"[tcp_server:{conn.label}] {resp}")
        except Exception as e:
            print(f"[tcp_client] OBS command failed on {conn.label}: {e}")
            self.pool.remove_label(conn.label)

    def handle_obs_command(self, parts: List[str]) -> None:
        if len(parts) < 3:
            print("[tcp_client] Usage:")
            print("  obs <ip> on [match_id]")
            print("  obs <ip> off")
            print("  obs all on [match_id]")
            print("  obs all off")
            return

        target = parts[1].lower()
        sub = parts[2].lower()

        if target == "all":
            if sub not in ("on", "off"):
                print("[tcp_client] Usage: obs all on [match_id]  |  obs all off")
                return
            if sub == "on":
                match_id = parts[3] if len(parts) >= 4 else ""
                self.obs_all_on(match_id)
            else:
                self.obs_all_off()
            return

        ip = parts[1]
        if sub not in ("on", "off"):
            print("[tcp_client] Usage: obs <ip> on [match_id]  |  obs <ip> off")
            return

        if sub == "on":
            match_id = parts[3] if len(parts) >= 4 else ""
            self.obs_one_on(ip, match_id)
        else:
            self.obs_one_off(ip)
