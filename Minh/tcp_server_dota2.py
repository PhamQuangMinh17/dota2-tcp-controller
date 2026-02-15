from __future__ import annotations

import argparse
import socket
import threading
from typing import Optional

from latency_change_divert import DivertConfig, DivertLatencyController, require_admin
# NEW IMPORTS
from obs_control import start_obs, stop_obs, start_obs_all, stop_obs_all, register_obs_ip
import subprocess
import psutil
import time
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
EXE_PATH = os.path.join(BASE_DIR, "app.exe")
PID_FILE = os.path.join(BASE_DIR, "app.pid")

def start_app():
    if os.path.exists(PID_FILE):
        print("App is already running")
        return

    p = subprocess.Popen(
        [EXE_PATH],
        cwd=BASE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    with open(PID_FILE, "w") as f:
        f.write(str(p.pid))

    print("turned on app.exe")

def stop_app():
    if not os.path.exists(PID_FILE):
        print("App is not running")
        return

    with open(PID_FILE) as f:
        pid = int(f.read())

    try:
        psutil.Process(pid).terminate()
        print("turned off app.exe")
    except psutil.NoSuchProcess:
        print("App was already shut down")

    os.remove(PID_FILE)



def _send_line(sock_file, text: str) -> None:
    sock_file.write((text + "\n").encode("utf-8"))
    sock_file.flush()


def _recv_line(sock_file) -> Optional[str]:
    raw = sock_file.readline()
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace").strip()


def _parse_set_latency(cmd: str) -> Optional[int]:
    """
    Accepts:
      SET_LATENCY 100
      SET 100
    """
    parts = cmd.strip().split()
    if len(parts) != 2:
        return None
    if parts[0].upper() not in ("SET_LATENCY", "SET"):
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def run_server(
    bind_ip: str,
    port: int,
    filter_expr: str,
    stop_on_zero: bool,
) -> int:
    # WinDivert requires admin privileges.
    require_admin(auto_elevate=True)

    controller = DivertLatencyController(
        DivertConfig(
            filter_expr=filter_expr,
            windivert_dll="",   # expects WinDivert.dll next to this script unless a path is provided
            stop_on_zero=stop_on_zero,
        )
    )

    print(f"[tcp_server] Listening on {bind_ip}:{port}")
    print(f"[tcp_server] Filter: {filter_expr}")
    print("[tcp_server] Waiting for TCP client connection...")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((bind_ip, port))
        srv.listen(1)

        conn, addr = srv.accept()
        with conn:
            print(f"[tcp_server] Client connected from {addr}")
            # Register this client's IP in OBS config so "OBS_ON_ALL"/"OBS_OFF_ALL"
            # can control OBS for all connected game machines.
            server_ip=conn.getsockname()[0]
            register_obs_ip(server_ip)
            sock_file = conn.makefile("rwb")

            # Expect a HELLO from client (controller)
            hello = _recv_line(sock_file)
            if hello is None:
                print("[tcp_server] Client disconnected before HELLO.")
                return 1
            print(f"[tcp_client] {hello}")

            print("[tcp_server] Ready. Waiting for commands...")

            while True:
                line = _recv_line(sock_file)
                if line is None:
                    print("[tcp_server] Client disconnected.")
                    controller.stop()
                    return 0

                cmd = line.strip()
                if not cmd:
                    continue

                cmd_upper = cmd.upper()
                parts = cmd.split()
                cmd_name = parts[0].upper()

                lat = _parse_set_latency(cmd)
                if lat is not None:
                    try:
                        controller.set_latency(lat)
                        _send_line(sock_file, f"OK latency set to {lat} ms")
                        print(f"[tcp_server] Applied latency={lat} ms")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to set latency: {e}")
                        print(f"[tcp_server] Failed to apply latency: {e}")
                    continue

                if cmd_name == "STOP":
                    controller.stop()
                    _send_line(sock_file, "OK latency injector stopped")
                    print("[tcp_server] Stopped latency injector")
                    continue
                # NEW: OBS CONTROL (expects: OBS_ON <ip> [match_id])
                if cmd_name == "OBS_ON":
                    if len(parts) < 2:
                        _send_line(sock_file, "ERR usage: OBS_ON <ip> [match_id]")
                        continue
                    ip = parts[1]
                    match_id = parts[2] if len(parts) >= 3 else None
                    try:
                        start_obs(ip, match_id)
                        _send_line(sock_file, f"OK OBS started for {ip}")
                        print(f"[tcp_server] OBS started for {ip}")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to start OBS for {ip}: {e}")
                        print(f"[tcp_server] Failed to start OBS for {ip}: {e}")
                    continue

                # NEW: OBS CONTROL (expects: OBS_ON_ALL [match_id])
                if cmd_name == "OBS_ON_ALL":
                    match_id = parts[1] if len(parts) >= 2 else None
                    try:
                        start_obs_all(match_id)
                        _send_line(sock_file, "OK OBS started for all IPs")
                        print("[tcp_server] OBS started for all IPs")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to start OBS for all IPs: {e}")
                        print(f"[tcp_server] Failed to start OBS for all IPs: {e}")
                    continue

                # NEW: OBS CONTROL (expects: OBS_OFF <ip>)
                if cmd_name == "OBS_OFF":
                    if len(parts) < 2:
                        _send_line(sock_file, "ERR usage: OBS_OFF <ip>")
                        continue
                    ip = parts[1]
                    try:
                        stop_obs(ip)
                        _send_line(sock_file, f"OK OBS stopped for {ip}")
                        print(f"[tcp_server] OBS stopped for {ip}")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to stop OBS for {ip}: {e}")
                        print(f"[tcp_server] Failed to stop OBS for {ip}: {e}")
                    continue

                # NEW: OBS CONTROL (expects: OBS_OFF_ALL)
                if cmd_name == "OBS_OFF_ALL":
                    try:
                        stop_obs_all()
                        _send_line(sock_file, "OK OBS stopped for all IPs")
                        print("[tcp_server] OBS stopped for all IPs")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to stop OBS for all IPs: {e}")
                        print(f"[tcp_server] Failed to stop OBS for all IPs: {e}")
                    continue

                # NEW: INPUT CONTROL repurposed to control Minh/app.py activity tracker
                if cmd_name == "INPUT_ON":
                    try:
                        start_app()
                        _send_line(sock_file, "OK app started")
                        print("[tcp_server] App started via INPUT_ON")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to start app: {e}")
                        print(f"[tcp_server] Failed to start app: {e}")
                    continue

                if cmd_name == "INPUT_OFF":
                    try:
                        stop_app()
                        _send_line(sock_file, "OK app stopped")
                        print("[tcp_server] App stopped via INPUT_OFF")
                    except Exception as e:
                        _send_line(sock_file, f"ERR failed to stop app: {e}")
                        print(f"[tcp_server] Failed to stop app: {e}")
                    continue

                if cmd_name == "PING":
                    _send_line(sock_file, "OK PONG")
                    continue

                if cmd_name == "QUIT":
                    _send_line(sock_file, "OK bye")
                    print("[tcp_server] Quit requested. Stopping injector and closing.")
                    controller.stop()
                    return 0

                _send_line(sock_file, f"ERR unknown command: {cmd}")


def _cli() -> int:
    p = argparse.ArgumentParser(description="DOTA2 TCP server (controlled node): listens, receives commands, applies WinDivert latency.")
    p.add_argument("--bind", default="0.0.0.0", help="Bind IP for listening")
    p.add_argument("--port", type=int, default=5000, help="Listen port")

    p.add_argument("--filter", default="udp", help="WinDivert filter expression")
    p.add_argument("--stop-on-zero", action="store_true", help="Stop injector when latency=0")

    args = p.parse_args()
    return run_server(
        bind_ip=args.bind,
        port=args.port,
        filter_expr=args.filter,
        stop_on_zero=args.stop_on_zero,
    )


if __name__ == "__main__":
    raise SystemExit(_cli())
