from __future__ import annotations
from pathlib import Path
import time
from threading import Lock, Timer

try:
    from obswebsocket import obsws, requests as obs_requests
    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False
    print("[WARNING] obs-websocket-py not installed")


class SingleRecordingManager:
    """Manages recording for a single game instance"""
    
    def __init__(self, ip_address, matches_folder=".", obs_host="localhost", obs_port=4455, obs_password="", stop_delay=10):
        """
        Args:
            ip_address: IP of the game client (for folder naming and identification)
            matches_folder: Base folder for recordings (used for organization only if remote_recording_path is set)
            obs_host: IP address where OBS is running (can be different from game IP!)
            obs_port: OBS WebSocket port
            obs_password: OBS WebSocket password
            stop_delay: Seconds to wait before stopping recording after game ends
            remote_recording_path: Base path on the remote machine where OBS should save recordings.
                                  If None, uses OBS default recording path.
                                  Example: "/Users/username/Movies/dota2-recordings" (Mac)
                                          or "D:/Recordings/dota2" (Windows)
        """
        self.ip_address = ip_address
        self.matches_folder = Path(matches_folder) / ip_address.replace(":", "_").replace(".", "_")
        self.in_progress = False
        self.current_match_id = None
        self.stop_delay = stop_delay
        self.stop_timer = None
        
        # OBS WebSocket connection
        self.obs_client = None
        self.obs_connected = False
        self.obs_host = obs_host
        self.obs_port = obs_port
        
        if OBS_AVAILABLE:
            try:
                print(f"[OBS][Game: {ip_address}] Connecting to OBS at {obs_host}:{obs_port}...")
                print(f"[OBS][Game: {ip_address}] Password: {'***' + obs_password[-3:] if obs_password else '(empty)'}")
                self.obs_client = obsws(obs_host, obs_port, obs_password)
                self.obs_client.connect()
                self.obs_connected = True
                print(f"[OBS][Game: {ip_address}] ✅ Connected to OBS at {obs_host}:{obs_port}!")
            except ConnectionRefusedError:
                print(f"[OBS][Game: {ip_address}] ❌ Connection REFUSED at {obs_host}:{obs_port}")
                print(f"[OBS][Game: {ip_address}] This means:")
                print(f"  → The host {obs_host} is reachable")
                print(f"  → But nothing is listening on port {obs_port}")
                print(f"  → OBS is probably not running or WebSocket is disabled")
            except TimeoutError:
                print(f"[OBS][Game: {ip_address}] ❌ Connection TIMEOUT to {obs_host}:{obs_port}")
                print(f"[OBS][Game: {ip_address}] This means:")
                print(f"  → Cannot reach {obs_host}")
                print(f"  → Check network connection or firewall")
            except Exception as e:
                print(f"[OBS][Game: {ip_address}] ❌ Failed to connect to OBS at {obs_host}:{obs_port}")
                print(f"[OBS][Game: {ip_address}] Error type: {type(e).__name__}")
                print(f"[OBS][Game: {ip_address}] Error message: {e}")
                print(f"[OBS] Checklist:")
                print(f"  1. Is OBS running on {obs_host}?")
                print(f"  2. OBS → Settings (or Tools) → WebSocket Server → Enable ")
        else:
            print(f"[OBS][Game: {ip_address}] obs-websocket-py not available, simulated mode")
    
    def get_unique_match_path(self, match_id):
        base_path = self.matches_folder / str(match_id)
        if not base_path.exists():
            return base_path

        index = 1
        while True:
            new_path = self.matches_folder / f"{match_id}_{index}"
            if not new_path.exists():
                return new_path
            index += 1

    def start_recording(self, match_id):
        """Start OBS recording for a match"""
        if self.in_progress:
            print(f"[RECORDING][{self.ip_address}] Already recording")
            return
            
        try:
           
            
            # Create local folder for sync file (metadata only)
            local_match_path = self.get_unique_match_path(match_id)
            print(f"[RECORDING][{self.ip_address}] Creating local metadata folder: {local_match_path}")
            local_match_path.mkdir(parents=True, exist_ok=True)

            # Create sync file with recording info
            sync_file = local_match_path / "sync.txt"
            sync_file.write_text(
                f"Game IP: {self.ip_address}\n"
                f"OBS Host: {self.obs_host}:{self.obs_port}\n"
                f"Recording started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Match ID: {match_id}\n"
            )

            print(f"[RECORDING][{self.ip_address}] *** STARTING for Match {match_id} ***")

            # Set OBS recording path (if remote path specified)
            if self.obs_connected:
                try:
                    # Try to set recording directory
                    
                    
                    # Verify the path was set
                    try:
                        response = self.obs_client.call(obs_requests.GetRecordDirectory())
                        actual_path = response.getRecordDirectory()
                        print(f"[OBS][{self.ip_address}] 📁 OBS confirmed path: {actual_path}")
                    except Exception as e:
                        print(f"[OBS][{self.ip_address}] ⚠️ Could not verify recording path: {e}")
                        
                except Exception as e:
                    print(f"[OBS][{self.ip_address}] ⚠️ Could not set recording path: {e}")
                    print(f"[OBS][{self.ip_address}] Videos will save to OBS default location")
                    print(f"[OBS][{self.ip_address}] Check: OBS Settings → Output → Recording Path")

            # Start recording
            if self.obs_connected:
                try:
                    self.obs_client.call(obs_requests.StartRecord())
                    print(f"[OBS][{self.ip_address}] ✅ Recording started!")
                except Exception as e:
                    print(f"[OBS][{self.ip_address}] ❌ Failed to start: {e}")
            else:
                print(f"[RECORDING][{self.ip_address}] 📝 Simulated start (OBS not connected)")

            self.in_progress = True
            self.current_match_id = match_id

        except Exception as e:
            print(f"[ERROR][{self.ip_address}] Failed to start recording: {e}")
            import traceback
            traceback.print_exc()
    
    def stop_recording(self, immediate=False):
        """Stop OBS recording
        
        Args:
            immediate: If True, stops immediately. If False, waits for stop_delay seconds.
        """
        if not self.in_progress:
            return
        
        # Cancel any pending stop timer
        if self.stop_timer:
            self.stop_timer.cancel()
            self.stop_timer = None
        
        if not immediate and self.stop_delay > 0:
            # Schedule delayed stop
            print(f"[RECORDING][{self.ip_address}] ⏳ Scheduling stop in {self.stop_delay} seconds...")
            self.stop_timer = Timer(self.stop_delay, self._do_stop_recording)
            self.stop_timer.start()
        else:
            # Stop immediately
            self._do_stop_recording()
    
    def _do_stop_recording(self):
        """Internal method to actually stop the recording"""
        if not self.in_progress:
            return
            
        print(f"[RECORDING][{self.ip_address}] *** STOPPING for Match {self.current_match_id} ***")
        
        if self.obs_connected:
            try:
                self.obs_client.call(obs_requests.StopRecord())
                print(f"[OBS][{self.ip_address}] ✅ Recording stopped!")
            except Exception as e:
                print(f"[OBS][{self.ip_address}] ❌ Failed to stop: {e}")
        else:
            print(f"[RECORDING][{self.ip_address}] 📝 Simulated stop")
        
        self.in_progress = False
        self.current_match_id = None
        self.stop_timer = None
    
    def disconnect(self):
        """Disconnect from OBS"""
        # Cancel any pending stop timer
        if self.stop_timer:
            self.stop_timer.cancel()
            self.stop_timer = None
            
        if self.obs_connected and self.obs_client:
            try:
                self.obs_client.disconnect()
                print(f"[OBS][{self.ip_address}] Disconnected from {self.obs_host}")
            except:
                pass


class MultiIPRecordingManager:
    """Manages recordings for multiple game instances (IPs)"""
    
    def __init__(self, matches_folder=".", obs_configs=None, stop_delay=10):
        """
        Initialize multi-IP recording manager
        
        Args:
            matches_folder: Base folder for all recordings (local metadata)
            obs_configs: Dict mapping game IPs to OBS configs
                        Example: {
                            "192.168.0.231": {
                                "host": "192.168.0.231",
                                "port": 4455,
                                "password": "123456",
                                "recording_path": "/Users/username/Movies/dota2-recordings"  # Mac path
                            },
                            "192.168.0.227": {
                                "host": "192.168.0.227",
                                "port": 4455,
                                "password": "123456",
                                "recording_path": "D:/Recordings/dota2"  # Windows path
                            }
                        }
                        "host" = where OBS is running
                        "recording_path" = where to save videos on that machine (optional)
            stop_delay: Seconds to wait before stopping recording after game ends (default: 10)
        """
        self.matches_folder = Path(matches_folder)
        self.matches_folder.mkdir(parents=True, exist_ok=True)
        
        self.managers = {}  # IP -> SingleRecordingManager
        self.lock = Lock()  # Thread safety for manager creation
        self.obs_configs = obs_configs or {}
        self.stop_delay = stop_delay
        self.default_obs_config = {
            "host": "localhost",
            "port": 4455,
            "password": "",
            "recording_path": None  # Use OBS default
        }
        
        print(f"[MULTI-IP] Initialized with base folder: {self.matches_folder}")
        print(f"[MULTI-IP] Stop delay: {stop_delay} seconds after game ends")
    
    def get_manager(self, ip_address):
        """Get or create a recording manager for a specific IP"""
        with self.lock:
            if ip_address not in self.managers:
                # Get OBS config for this IP
                obs_config = self.obs_configs.get(ip_address, self.default_obs_config)
                
                obs_host = obs_config.get("host", "localhost")
                obs_port = obs_config.get("port", 4455)
                obs_password = obs_config.get("password", "")
                recording_path = obs_config.get("recording_path", None)
                
                print(f"[MULTI-IP] Creating manager for game at {ip_address}")
                print(f"[MULTI-IP]   → OBS location: {obs_host}:{obs_port}")
                if recording_path:
                    print(f"[MULTI-IP]   → Recording path: {recording_path}")
                else:
                    print(f"[MULTI-IP]   → Recording path: OBS default location")
                
                self.managers[ip_address] = SingleRecordingManager(
                    ip_address=ip_address,
                    matches_folder=str(self.matches_folder),
                    obs_host=obs_host,  # ✅ NOW PASSING THE CORRECT HOST!
                    obs_port=obs_port,
                    obs_password=obs_password,
                    stop_delay=self.stop_delay,
                )
            
            return self.managers[ip_address]
    
    def handle_game_state(self, ip_address, data):
        """Process game state for a specific IP address"""
        if not data:
            return
        
        manager = self.get_manager(ip_address)
        
        try:
            map_data = data.get("map", {})
            if not map_data:
                manager.stop_recording(immediate=True)
                return
                
            game_state = map_data.get("game_state")
            match_id = map_data.get("matchid")
            
            # No game state - stop if recording
            if not game_state:
                if manager.in_progress:
                    manager.stop_recording(immediate=True)
                return
            
            # Skip early states
            if game_state in [
                "DOTA_GAMERULES_STATE_INIT",
                "DOTA_GAMERULES_STATE_WAIT_FOR_PLAYERS_TO_LOAD",
            ]:
                return
            
            # Start recording when game starts
            if match_id and not manager.in_progress:
                if game_state in [
                    "DOTA_GAMERULES_STATE_HERO_SELECTION",
                    "DOTA_GAMERULES_STATE_STRATEGY_TIME",
                    "DOTA_GAMERULES_STATE_PRE_GAME",
                    "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS",
                ]:
                    manager.start_recording(match_id)
            elif not match_id and manager.in_progress:
                manager.stop_recording(immediate=True)
            
            # Stop recording on game end (with delay)
            if manager.in_progress and game_state in [
                "DOTA_GAMERULES_STATE_POST_GAME",
                "DOTA_GAMERULES_STATE_DISCONNECT",
            ]:
                print(f"[MULTI-IP][{ip_address}] Game ending, stopping recording in {self.stop_delay}s...")
                manager.stop_recording(immediate=False)  # Use delay
                
        except Exception as e:
            print(f"[ERROR][{ip_address}] Error handling game state: {e}")
            import traceback
            traceback.print_exc()
            
            if manager.in_progress:
                manager.stop_recording(immediate=True)
    
    def get_status(self):
        """Get recording status for all IPs"""
        status = {}
        for ip, manager in self.managers.items():
            status[ip] = {
                "recording": manager.in_progress,
                "match_id": manager.current_match_id,
                "obs_connected": manager.obs_connected,
                "obs_host": manager.obs_host,
                "obs_port": manager.obs_port
            }
        return status
    
    def stop_all(self):
        """Stop all recordings immediately"""
        print("[MULTI-IP] Stopping all recordings...")
        for ip, manager in self.managers.items():
            if manager.in_progress:
                print(f"[MULTI-IP] Stopping recording for {ip}")
                manager.stop_recording(immediate=True)
    
    def disconnect_all(self):
        """Disconnect all OBS connections"""
        print("[MULTI-IP] Disconnecting all OBS connections...")
        for manager in self.managers.values():
            manager.disconnect()


# ==================== SIMPLE API FOR TCP SERVER ====================
#
# This section exposes two functions that `tcp_server_dota2.py` can import:
#   - start_obs(ip_address: str, match_id: str | None = None)
#   - stop_obs(ip_address: str, immediate: bool = True)
#
# They internally use a global MultiIPRecordingManager and an IP → OBS config map.

# Mapping of known game IPs to OBS connection info.
# - Key   = "game IP" (the IP you will send from the client, e.g. 192.168.0.231)
# - Value = dict with OBS connection info for that game.
OBS_IP_CONFIGS: dict[str, dict] = {}


# Global manager instance used by start_obs/stop_obs
_GLOBAL_RECORDER = MultiIPRecordingManager(
    matches_folder="./recordings",
    obs_configs=OBS_IP_CONFIGS,
    stop_delay=10,
)


def register_obs_ip(ip_address: str) -> None:
    """
    Ensure an IP is present in OBS_IP_CONFIGS and the global recorder config.
    By default we assume OBS WebSocket is running on the same host as ip_address.
    """
    if ip_address not in OBS_IP_CONFIGS:
        OBS_IP_CONFIGS[ip_address] = {
            "host": ip_address,
            "port": 4455,
            "password": "123456",
            "recording_path": None,
        }
        _GLOBAL_RECORDER.obs_configs[ip_address] = OBS_IP_CONFIGS[ip_address]


def start_obs(ip_address: str, match_id: str | None = None) -> None:
    """
    Start OBS recording for the given game IP.

    Called by tcp_server via a command like:  OBS_ON <ip> [match_id]
    """
    # Make sure this IP is tracked so "obs all on/off" can include it.
    register_obs_ip(ip_address)

    if not match_id:
        match_id = "manual"

    manager = _GLOBAL_RECORDER.get_manager(ip_address)
    manager.start_recording(match_id)


def stop_obs(ip_address: str, immediate: bool = True) -> None:
    """
    Stop OBS recording for the given game IP.

    Called by tcp_server via a command like:  OBS_OFF <ip>
    """
    register_obs_ip(ip_address)
    manager = _GLOBAL_RECORDER.get_manager(ip_address)
    manager.stop_recording(immediate=immediate)


def start_obs_all(match_id: str | None = None) -> None:
    """
    Start OBS recording for all configured IPs in OBS_IP_CONFIGS.
    """
    print(OBS_IP_CONFIGS)
    for ip in OBS_IP_CONFIGS.keys():
        start_obs(ip, match_id)


def stop_obs_all(immediate: bool = True) -> None:
    """
    Stop OBS recording for all configured IPs in OBS_IP_CONFIGS.
    """
    for ip in OBS_IP_CONFIGS.keys():
        stop_obs(ip, immediate=immediate)
