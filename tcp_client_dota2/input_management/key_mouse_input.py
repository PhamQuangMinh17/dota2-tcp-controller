from __future__ import annotations

from typing import List

from client_core.core import ConnectionPool, print_results

"""
key_mouse_input.py

Author: Quang Minh Pham

Description:
This module handles input automation commands.

Responsibilities:
- Handle "input on" command:
    - Start input automation (app.exe on server)
- Handle "input off" command:
    - Stop input automation (terminate app.exe on server)
- Broadcast input commands to all connected servers

Behavior:
- INPUT_ON triggers app.exe execution on server
- INPUT_OFF stops app.exe on server
- Used mainly during experiments to simulate user activity

Notes:
- This module only sends commands
- Actual input handling is implemented on server-side
"""
class InputController:
    """
    Everything related to keyboard / mouse trigger control:
      - input on
      - input off
    """
    def __init__(self, pool: ConnectionPool):
        self.pool = pool

    def turn_on(self) -> None:
        res = self.pool.broadcast_collect("INPUT_ON")
        print_results("[tcp_server:", res)

    def turn_off(self) -> None:
        res = self.pool.broadcast_collect("INPUT_OFF")
        print_results("[tcp_server:", res)

    def handle_input_command(self, parts: List[str]) -> None:
        if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
            print("[tcp_client] Usage: input on|off")
            return
        sub = parts[1].lower()
        if sub == "on":
            self.turn_on()
        else:
            self.turn_off()
