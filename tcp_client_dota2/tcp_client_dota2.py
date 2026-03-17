from __future__ import annotations

import argparse
import socket
from typing import List, Tuple

from client_core.core import ConnectionPool, ScheduleRunner, parse_mmss_or_seconds, parse_server_item
from experiment_management.experiment_control import ExperimentController
from input_management.key_mouse_input import InputController
from latency_management.latency_control import LatencyController
from obs_management.obs_control import OBSController
from server_management.servers_control import ServerController

"""
tcp_client_dota2.py

Author: Quang Minh Pham

Description: MAIN file
Main entry point and controller of the TCP client system.

Responsibilities:
- Parse CLI arguments (server IPs, ports, etc.)
- Initialize ConnectionPool and ScheduleRunner
- Establish initial connections to servers
- Provide interactive command-line interface
- Dispatch user commands to corresponding modules:
    - servers_control
    - latency_control
    - experiment_control
    - obs_control
    - key_mouse_input

Supported Commands:
- Server management:
    servers, connect to, delete, move, swap
- Latency control:
    set, start_latency_loop, stop
- Experiment:
    start_exp
- OBS:
    obs ...
- Input:
    input on/off
- Utility:
    ping, quit

Behavior:
- Acts as the central orchestrator
- Delegates actual work to other modules
- Maintains global runtime state

Notes:
- This file should remain lightweight
- All heavy logic is handled in modules
"""
def run_client(
    servers: List[Tuple[str, int]],
    client_name: str,
    connect_timeout_sec: float,
    default_server_port: int,
) -> int:
    pool = ConnectionPool()
    scheduler = ScheduleRunner()

    server_controller = ServerController(
        pool=pool,
        client_name=client_name,
        connect_timeout_sec=connect_timeout_sec,
        default_server_port=default_server_port,
    )
    latency_controller = LatencyController(pool=pool, scheduler=scheduler)
    obs_controller = OBSController(pool=pool)
    input_controller = InputController(pool=pool)
    experiment_controller = ExperimentController(
        pool=pool,
        scheduler=scheduler,
        obs_controller=obs_controller,
        input_controller=input_controller,
        latency_controller=latency_controller,
        parse_duration=parse_mmss_or_seconds,
    )

    server_controller.connect_servers(servers)

    if not pool.labels():
        print("[tcp_client] No servers connected. Exiting.")
        return 1

    print("\n[tcp_client] Commands (latency commands apply to ALL connected servers unless noted):")
    print("  servers                           -> list connected servers (with Team A/B + numbers)")
    print("  connect to <ip1>, <ip2>, ...      -> connect/reconnect to new servers")
    print("  delete <number>                   -> delete server + renumber")
    print("  move <number> to A|B              -> move a server to another team")
    print("  swap <n1> <n2>                    -> swap teams of two servers (example: swap 2 7)")
    print("")
    print("  set <ms>                          -> send SET_LATENCY <ms> to ALL servers")
    print("  start_latency_loop                -> prompt duration + 4 latency values, then loop forever (ALL servers)")
    print("  start_latency_loop <mm:ss|sec> <lat1> <lat2> <lat3> <lat4>")
    print("                                    -> same as above, but no prompts")
    print("  start_exp <duration> <array_no> <team> [team...]")
    print("                                    -> obs all on + input on + apply latency array to listed teams in order")
    print("                                    -> example: start_exp 5 2 A")
    print("                                    -> example: start_exp 300 1 A B")
    print("  stop                              -> stop current loop; if experiment is running, also OBS_OFF_ALL + INPUT_OFF")
    print("  ping                              -> send PING to ALL servers")
    print("  obs <ip> on [match]               -> start OBS recording on ONE server (server host == <ip>)")
    print("  obs <ip> off                      -> stop OBS recording on ONE server (server host == <ip>)")
    print("  obs all on [match]                -> send OBS_ON_ALL to ALL servers")
    print("  obs all off                       -> send OBS_OFF_ALL to ALL servers")
    print("  input on/off                      -> send INPUT_ON / INPUT_OFF to ALL servers")
    print("  quit                              -> stop schedule/experiment, cleanup if needed, then send QUIT\n")

    experiment_controller.print_arrays()

    while True:
        try:
            user = input("client> ").strip()
        except EOFError:
            user = "quit"

        if not user:
            continue

        parts = user.split()
        cmd = parts[0].lower()

        if cmd == "servers":
            server_controller.print_servers()
            continue

        if cmd == "connect":
            server_controller.handle_connect_command(user)
            continue

        if cmd == "delete":
            server_controller.handle_delete_command(parts)
            continue

        if cmd == "move":
            server_controller.handle_move_command(parts)
            continue

        if cmd == "swap":
            server_controller.handle_swap_command(parts)
            continue

        if cmd == "set":
            latency_controller.handle_set_command(parts)
            continue

        if cmd == "start":
            print("[tcp_client] 'start' is deprecated. Use 'start_latency_loop'.")
            cmd = "start_latency_loop"

        if cmd == "start_latency_loop":
            latency_controller.handle_start_latency_loop_command(parts)
            continue

        if cmd == "start_exp":
            experiment_controller.handle_start_exp_command(parts)
            continue

        if cmd == "stop":
            scheduler.stop()
            if not experiment_controller.cleanup_after_stop():
                latency_controller.stop_all()
            continue

        if cmd == "ping":
            from client_core.core import print_results  # local import keeps main top smaller
            res = pool.broadcast_collect("PING")
            print_results("[tcp_server:", res)
            continue

        if cmd == "obs":
            obs_controller.handle_obs_command(parts)
            continue

        if cmd == "input":
            input_controller.handle_input_command(parts)
            continue

        if cmd == "quit":
            scheduler.stop()
            experiment_controller.cleanup_after_stop()

            from client_core.core import print_results  # local import keeps main top smaller
            res = pool.broadcast_collect("QUIT")
            print_results("[tcp_server:", res)
            pool.close_all()
            return 0

        print("[tcp_client] Unknown command. Use: servers/connect/set/start_latency_loop/start_exp/stop/ping/obs/input/quit")



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
            servers.append(parse_server_item(item, default_port=args.server_port))
        except Exception as e:
            raise SystemExit(f"Invalid --server entry '{item}': {e}")

    return run_client(
        servers=servers,
        client_name=args.client_name,
        connect_timeout_sec=args.connect_timeout,
        default_server_port=args.server_port,
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
