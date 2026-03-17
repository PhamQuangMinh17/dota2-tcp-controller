from __future__ import annotations

from typing import Dict, List

from client_core.core import ConnectionPool, ScheduleRunner, normalize_team
from input_management.key_mouse_input import InputController
from latency_management.latency_control import LatencyController
from obs_management.obs_control import OBSController

"""
experiment_control.py

Author: Quang Minh Pham

Description:
This module handles all experiment-related logic triggered by the "start_exp" command.

Responsibilities:
- Define latency arrays used for experiments
- Parse and validate experiment inputs
- Start experiment workflow:
    - Turn ON OBS recording (OBS_ON_ALL)
    - Turn ON input automation (INPUT_ON)
    - Apply latency sequences to selected teams
- Control experiment execution flow:
    - Sequential latency application
    - Team switching (A → B → loop)
- Stop experiment workflow:
    - Turn OFF OBS (OBS_OFF_ALL)
    - Turn OFF input (INPUT_OFF)
    - Stop latency injection (STOP)

Behavior:
- Latency values are applied sequentially from predefined arrays
- Each latency value is held for a fixed duration
- If multiple teams are provided, execution alternates between teams
- When one team is active, the other team has no latency

Notes:
- Runs on top of ScheduleRunner from core.py
- Designed for controlled latency experiments in DOTA2
"""
# Edit these later however you want.
# For now: 5 sample latency arrays with random positive integers between 10 and 100.
EXPERIMENT_LATENCY_ARRAYS: Dict[int, List[int]] = {
    1: [18, 41, 67, 24, 93, 56, 32],
    2: [12, 58, 27, 84, 36, 73, 19],
    3: [95, 21, 47, 63, 10, 88, 52],
    4: [31, 76, 14, 65, 99, 40, 22],
    5: [57, 11, 83, 29, 71, 44, 97],
}


class ExperimentController:
    """
    Everything related to the experiment command:
      - experiment latency arrays
      - start_exp <duration> <array_no> <team> [team...]
      - cleanup after stop/quit (OBS off, input off, STOP)
    """
    def __init__(
        self,
        pool: ConnectionPool,
        scheduler: ScheduleRunner,
        obs_controller: OBSController,
        input_controller: InputController,
        latency_controller: LatencyController,
        parse_duration,
    ):
        self.pool = pool
        self.scheduler = scheduler
        self.obs_controller = obs_controller
        self.input_controller = input_controller
        self.latency_controller = latency_controller
        self.parse_duration = parse_duration
        self.cleanup_needed = False

    def print_arrays(self) -> None:
        print("[tcp_client] Experiment latency arrays:")
        for arr_no, values in sorted(EXPERIMENT_LATENCY_ARRAYS.items()):
            print(f"  Array {arr_no}: {values}")
        print("")

    def _has_team_members(self, team: str) -> bool:
        return len(self.pool.team_conns(team)) > 0

    def handle_start_exp_command(self, parts: List[str]) -> None:
        if len(parts) < 4:
            print("[tcp_client] Usage: start_exp <duration> <array_no> <team> [team...]")
            return

        if self.scheduler.running():
            print("[tcp_client] Another schedule/experiment is already running. Type 'stop' first.")
            return

        try:
            duration_sec = self.parse_duration(parts[1])
            if duration_sec <= 0:
                raise ValueError("duration must be > 0")
            array_no = int(parts[2])
            latency_values = EXPERIMENT_LATENCY_ARRAYS[array_no]
            team_sequence = [normalize_team(t) for t in parts[3:]]
        except KeyError:
            print(f"[tcp_client] Invalid array number {parts[2]}. Available arrays: {sorted(EXPERIMENT_LATENCY_ARRAYS.keys())}")
            return
        except Exception as e:
            print(f"[tcp_client] Invalid start_exp args: {e}")
            return

        missing_teams = [team for team in team_sequence if not self._has_team_members(team)]
        if missing_teams:
            missing_display = ", ".join(missing_teams)
            print(f"[tcp_client] Cannot start_exp: no connected servers in team(s): {missing_display}")
            return

        print("[tcp_client] Starting experiment setup: OBS_ON_ALL + INPUT_ON")
        self.obs_controller.obs_all_on()
        self.input_controller.turn_on()

        self.start_experiment(duration_sec, latency_values, team_sequence, loop=True)
        self.cleanup_needed = True

    def start_experiment(self, duration_sec: float, latency_values: List[int], teams: List[str], loop: bool = True) -> None:
        if duration_sec <= 0:
            raise ValueError("duration must be > 0")
        if not latency_values:
            raise ValueError("latency_values is empty")
        if not teams:
            raise ValueError("at least one team must be specified")
        if self.scheduler.running():
            print("[tcp_client] Schedule already running. Type 'stop' first.")
            return

        normalized_teams = [normalize_team(t) for t in teams]

        if not self.pool.labels():
            print("[tcp_client] No connected servers. Cannot start experiment.")
            return

        def _worker(stop_evt) -> None:
            print("[tcp_client] Experiment mode started.")
            print(f"[tcp_client] Teams order: {normalized_teams}")
            print(f"[tcp_client] Duration per latency value: {duration_sec} sec")
            print(f"[tcp_client] Latency sequence: {latency_values}")
            print("[tcp_client] Type 'stop' to stop experiment.\n")
            try:
                while not stop_evt.is_set():
                    for active_team in normalized_teams:
                        if stop_evt.is_set():
                            break

                        inactive_teams = self.pool.other_teams(active_team)
                        if inactive_teams:
                            stop_res = self.pool.teams_collect(inactive_teams, "STOP")
                            for lbl, resp in stop_res.items():
                                print(f"[tcp_client] ({lbl}) STOP (inactive team) -> {resp}")

                        for lat_ms in latency_values:
                            if stop_evt.is_set():
                                break

                            res = self.pool.team_collect(active_team, f"SET_LATENCY {lat_ms}")
                            if not res:
                                print(f"[tcp_client] No connected servers in Team {active_team}.")
                            else:
                                for lbl, resp in res.items():
                                    print(f"[tcp_client] ({lbl}) [Team {active_team}] SET_LATENCY {lat_ms} -> {resp}")

                            if stop_evt.wait(duration_sec):
                                break

                    if not loop:
                        break
            except Exception as e:
                print(f"[tcp_client] Experiment thread error: {e}")
            finally:
                print("[tcp_client] Experiment stopped.\n")

        self.scheduler.start_task(
            mode="exp",
            target=_worker,
            thread_name="experiment-runner",
        )

    def cleanup_after_stop(self) -> bool:
        if not self.cleanup_needed:
            return False

        print("[tcp_client] Cleaning up experiment resources...")
        self.obs_controller.obs_all_off()
        self.input_controller.turn_off()
        self.latency_controller.stop_all()
        self.cleanup_needed = False
        return True
