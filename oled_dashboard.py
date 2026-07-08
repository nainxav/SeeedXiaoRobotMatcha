#!/usr/bin/env python3
"""
Pi Klipper Buttons OLED Dashboard
A compact dashboard for 128x64 OLED display on Orange Pi Zero 2 W
Compatible with Allwinner H616 SoC
"""

import time
import threading
import queue
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass

from oled_display import OLEDDisplay
from klipper_interface import KlipperInterface

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@dataclass
class DashboardState:
    """Dashboard state container"""
    current_state: str = 'idle'
    navigation_mode: bool = False
    memory_mode: bool = False
    navigation_option: str = ''
    memory_slot: int = 1
    bowl1_duration: int = 10
    bowl1_pattern: int = 1
    bowl2_duration: int = 10
    bowl2_pattern: int = 1
    memory_slots: Dict[str, Dict[str, Any]] = None
    operation_progress: Dict[int, Dict[str, int]] = None
    selected_option: Optional[str] = None  # Track which option is selected/editing (e.g., 'bowl1_duration', 'bowl1_pattern', etc.)
    edit_mode: bool = False  # True when in edit mode (rotating to adjust)
    
    def __post_init__(self):
        if self.memory_slots is None:
            self.memory_slots = {
                'slot1': {'empty': True, 'data': ''},
                'slot2': {'empty': True, 'data': ''},
                'slot3': {'empty': True, 'data': ''}
            }
        if self.operation_progress is None:
            self.operation_progress = {
                1: {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0},
                2: {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            }

class OLEDDashboard:
    """Main dashboard controller for OLED display"""
    
    def __init__(self, config_file: str = 'dashboard_config.json', display_controller=None):
        self.config = self._load_config(config_file)
        self.state = DashboardState()
        # Start with no selection; cursor appears only after user navigates
        self.state.navigation_option = ''
        self.state.selected_option = None
        self.state.edit_mode = False
        
        # Get display configuration
        display_config = self.config.get('display', {})
        i2c_address = display_config.get('i2c_address', '0x3C')
        # Convert string address to int if needed
        if isinstance(i2c_address, str):
            i2c_address = int(i2c_address, 16) if i2c_address.startswith('0x') else int(i2c_address)
        i2c_port = display_config.get('i2c_port', 2)
        width = display_config.get('width', 128)
        height = display_config.get('height', 64)
        self.refresh_rate = max(0.1, float(display_config.get('refresh_rate', 0.25)))
        
        # Initialize display with configurable port and address. When a
        # display_controller is supplied (e.g. the Seeed XIAO serial backend),
        # the OLED is driven through it instead of the local I2C bus.
        self.display = OLEDDisplay(
            width=width,
            height=height,
            i2c_address=i2c_address,
            i2c_port=i2c_port,
            controller=display_controller
        )
        self.klipper = KlipperInterface(self.config.get('klipper', {}))
        
        # Message queue for thread-safe communication - WITH SIZE LIMIT
        # Prevents unbounded memory growth under heavy message load
        self._message_queue_max = 100  # Max messages before dropping old ones
        self.message_queue = queue.Queue(maxsize=self._message_queue_max)
        
        # Display update thread
        self.display_thread = None
        self.message_thread = None
        self.monitoring_thread = None  # For periodic stats logging
        self.running = False
        self._display_lock = threading.Lock()  # Prevent concurrent display updates
        
        # Monitoring stats
        self._stats = {
            'start_time': time.time(),
            'frames_rendered': 0,
            'frames_skipped': 0,
            'state_file_reads': 0,
            'state_file_errors': 0,
            'messages_processed': 0,
            'messages_dropped': 0,
            'last_stats_log': 0
        }
        self._stats_log_interval = 300  # Log stats every 5 minutes
        
        # Pattern abbreviations mapping (single-letter)
        # 1 -> U, 2 -> M, 3 -> Z
        self.pattern_names = {
            1: 'U',
            2: 'M', 
            3: 'Z'
        }
        
        # Blink timer for selected arrow
        self.blink_timer = 0.0
        self.blink_interval = 0.5  # Blink every 0.5 seconds
        
        # Emergency stop cooldown - prevents display glitches during firmware restart
        self._emergency_stop_cooldown = 0.0  # Timestamp when cooldown ends
        self._emergency_stop_duration = 6.0  # Seconds to blank display during restart
        self._was_in_cooldown = False  # Track if we were in cooldown last frame
        
        # Navigation options mapping (in display order)
        self.nav_options = [
            'bowl1_duration',
            'bowl1_pattern',
            'bowl2_duration',
            'bowl2_pattern'
        ]
        self.nav_option_display_names = {
            'bowl1_duration': 'Bowl 1 Duration',
            'bowl1_pattern': 'Bowl 1 Pattern',
            'bowl2_duration': 'Bowl 2 Duration',
            'bowl2_pattern': 'Bowl 2 Pattern',
            'memory': 'Memory'
        }
        
        # Clock cache to avoid jitter; only update when epoch second changes
        self.clock_offset_seconds = 7 * 3600  # GMT+7 default
        self._clock_last_epoch_sec = None
        self._clock_cached_text = ""
        # Timebase for blink and second alignment
        self._last_update_time = time.time()
        
        # State file path for reading queue information
        self.state_file_path = "/tmp/klipper_buttons_state.json"
        self._last_state_file_check = 0
        self._state_file_check_interval = 0.5  # Check every 0.5 seconds
        self._cached_queue_display = ""
        self._last_ws_state_update = time.time()
        self._last_state_file_sync = 0.0
        self._state_file_fallback_delay = 0.2  # Give WebSocket a short chance before falling back
        self._state_file_sync_interval = 0.1  # Minimum 100ms between file reads
        
        # State file resilience
        self._state_file_retry_count = 0
        self._state_file_retry_max = 3
        self._state_file_retry_delay = 0.05  # 50ms between retries
        
        logger.info("OLED Dashboard initialized")
    
    def _load_config(self, config_file: str) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        default_config = {
            'klipper': {
                'host': 'localhost',
                'port': 7125  # Moonraker WebSocket port (default is 7125, not 8080)
            },
            'display': {
                'i2c_address': '0x3C',
                'i2c_port': 2,
                'width': 128,
                'height': 64,
                'refresh_rate': 0.5
            }
        }
        
        try:
            with open(config_file, 'r') as f:
                config = json.load(f)
            logger.info(f"Configuration loaded from {config_file}")
            return config
        except FileNotFoundError:
            logger.warning(f"Config file {config_file} not found, using defaults")
            return default_config
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            return default_config
    
    def start(self):
        """Start the dashboard"""
        logger.info("Starting OLED Dashboard...")
        
        # Initialize display
        if not self.display.initialize():
            logger.error("Failed to initialize OLED display")
            return False
        
        # Start Klipper interface
        if not self.klipper.start():
            logger.error("Failed to start Klipper interface")
            return False
        
        # Register callback to receive messages directly from Klipper interface
        self.klipper.add_message_callback(self._on_klipper_message)
        
        # Start display update thread
        self.running = True
        self.display_thread = threading.Thread(target=self._display_loop, daemon=True)
        self.display_thread.start()
        
        # Start message processing thread
        self.message_thread = threading.Thread(target=self._process_messages, daemon=True)
        self.message_thread.start()
        
        # Start monitoring thread for periodic stats logging
        self.monitoring_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
        self.monitoring_thread.start()
        
        logger.info("OLED Dashboard started successfully")
        return True
    
    def stop(self):
        """Stop the dashboard"""
        logger.info("Stopping OLED Dashboard...")
        
        # Log final stats before shutdown
        self._log_stats(force=True)
        
        self.running = False
        
        if self.display_thread and self.display_thread.is_alive():
            self.display_thread.join(timeout=2)
        
        if self.message_thread and self.message_thread.is_alive():
            self.message_thread.join(timeout=2)
        
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=2)
        
        self.klipper.stop()
        self.display.cleanup()
        
        logger.info("OLED Dashboard stopped")
    
    def _display_loop(self):
        """Main display update loop"""
        while self.running:
            try:
                self._sync_state_from_file_if_needed()
                now = time.time()
                # Update blink timer by elapsed time since last update
                elapsed = max(0.0, now - self._last_update_time)
                self._last_update_time = now
                self.blink_timer += elapsed
                if self.blink_timer >= self.blink_interval * 2:
                    self.blink_timer -= self.blink_interval * 2

                # Render the frame
                self._update_display()

                # Sleep for configured refresh interval
                time.sleep(self.refresh_rate)
            except Exception as e:
                logger.error(f"Error in display loop: {e}")
                time.sleep(1)
    
    def _update_display(self):
        """Update the OLED display with current state - thread-safe"""
        # Use lock to prevent concurrent display updates (prevents "display under display" glitch)
        if not self._display_lock.acquire(blocking=False):
            # Skip this update if another one is in progress
            self._stats['frames_skipped'] += 1
            return
        
        self._stats['frames_rendered'] += 1
        
        try:
            # Check if in emergency stop cooldown (firmware restarting)
            in_cooldown = time.time() < self._emergency_stop_cooldown
            
            if in_cooldown:
                # Mark that we're in cooldown
                self._was_in_cooldown = True
                # Clear and show simple "Restarting..." message during cooldown
                self.display.clear()
                remaining = int(self._emergency_stop_cooldown - time.time())
                self.display.draw_centered_text(24, "RESTARTING...", size=1)
                self.display.draw_centered_text(40, f"Please wait {remaining}s", size=1)
                self.display.update()
                return
            
            # Check if we just exited cooldown - force full display reinit
            if self._was_in_cooldown:
                self._was_in_cooldown = False
                logger.info("Exiting emergency stop cooldown - reinitializing display")
                # CRITICAL: Clear all progress data to prevent stale progress bars
                for bowl_num in [1, 2]:
                    self.state.operation_progress[bowl_num] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
                # Reset state to idle
                self.state.current_state = 'idle'
                # Hard clear the display memory directly to remove any artifacts
                self.display.hard_clear()
                time.sleep(0.1)
                # Do another hard clear to be sure
                self.display.hard_clear()
                time.sleep(0.1)
            
            # Clear display for normal rendering
            self.display.clear()
            
            # Read queue information from state file periodically
            self._update_queue_info()
            
            # Choose display layout based on current state
            if self.state.memory_mode:
                self._draw_memory_screen()
            elif self.state.current_state == 'whisking':
                self._draw_operation_screen()
            else:
                self._draw_main_screen()
            
            # Draw queue information in top right corner (on all screens)
            if self._cached_queue_display:
                self.display.draw_queue_info(self._cached_queue_display, size=1, margin=2)
            
            # Update display
            self.display.update()
            
        except Exception as e:
            logger.error(f"Error updating display: {e}")
        finally:
            self._display_lock.release()
    
    def _draw_main_screen(self):
        """Draw the main status screen with arrow navigation, progress bars, and countdown timers"""
        # Header with state indicator
        state_symbol = self._get_state_symbol(self.state.current_state)
        self.display.draw_text(0, 0, f"{state_symbol} {self.state.current_state.upper()}", size=1)
        
        # Line positions for bowl options
        line_y_positions = {
            'bowl1_duration': 12,
            'bowl1_pattern': 12,
            'bowl2_duration': 24,
            'bowl2_pattern': 24
        }
        
        current_nav_option = None
        if self.state.navigation_mode or self.state.edit_mode or self.state.memory_mode:
            current_nav_option = self.state.selected_option
            if not current_nav_option and self.state.navigation_option:
                nav_map = {
                    'Bowl 1 Duration': 'bowl1_duration',
                    'Bowl 1 Pattern': 'bowl1_pattern',
                    'Bowl 2 Duration': 'bowl2_duration',
                    'Bowl 2 Pattern': 'bowl2_pattern',
                    'Memory': 'memory'
                }
                current_nav_option = nav_map.get(self.state.navigation_option, None)
        
        # Draw Bowl 1 line with progress bar
        self._draw_bowl_line(1, 12, current_nav_option, self.state.edit_mode, self.state.bowl1_duration, 
                            self.state.bowl1_pattern, self.state.operation_progress[1])
        
        # Draw Bowl 2 line with progress bar
        self._draw_bowl_line(2, 24, current_nav_option, self.state.edit_mode, self.state.bowl2_duration,
                            self.state.bowl2_pattern, self.state.operation_progress[2])
        
        # Memory slots status
        memory_status = ""
        for i in range(1, 4):
            slot_key = f'slot{i}'
            if self.state.memory_slots[slot_key]['empty']:
                memory_status += "E"
            else:
                memory_status += "F"
            if i < 3:
                memory_status += " "
        
        self.display.draw_text(0, 36, f"M: {memory_status}", size=1)
        # Draw navigation cursor for Memory option
        memory_selected = current_nav_option == 'memory'
        if memory_selected:
            show_cursor = True
            if self.state.edit_mode:
                show_cursor = self.blink_timer < self.blink_interval
            if show_cursor:
                _, mem_h = self.display.get_text_size("M:", size=1)
                tri_h = max(6, mem_h)
                tri_w = max(6, tri_h // 2)
                tri_y = 36 + max(0, (mem_h - tri_h) // 2) + 6
                self.display.draw_filled_triangle(0, tri_y, tri_w, tri_h, direction='right', color=1)
        
        # Current time (cached, GMT+7); only changes once per second to avoid jitter
        current_time = self._get_cached_time_str()
        self.display.draw_text(0, 48, current_time, size=1)
        
        # Connection status
        conn_status = "ON" if self.klipper.is_connected() else "OFF"
        self.display.draw_text(90, 48, conn_status, size=1)
    
    def _format_mm_ss(self, seconds: int) -> str:
        """Format seconds as mm:ss"""
        minutes = max(0, seconds) // 60
        secs = max(0, seconds) % 60
        return f"{minutes:02d}:{secs:02d}"

    def _get_local_time_str(self, offset_hours: int = 0) -> str:
        """Get current time string HH:MM with a fixed UTC offset using monotonic seconds.
        Uses time.time() so seconds advance smoothly regardless of refresh jitter.
        """
        try:
            ts = int(time.time()) + int(offset_hours * 3600)
            dt = datetime.utcfromtimestamp(ts)
            return dt.strftime("%H:%M")
        except Exception:
            # Fallback to system local time if anything goes wrong
            return datetime.now().strftime("%H:%M")

    def _get_cached_time_str(self) -> str:
        """Return cached HH:MM for GMT+7; update only when integer epoch second changes."""
        try:
            sec = int(time.time()) + self.clock_offset_seconds
            if sec != self._clock_last_epoch_sec:
                self._clock_last_epoch_sec = sec
                dt = datetime.utcfromtimestamp(sec)
                self._clock_cached_text = dt.strftime("%H:%M")
            return self._clock_cached_text or datetime.now().strftime("%H:%M")
        except Exception:
            return datetime.now().strftime("%H:%M")
    
    def _draw_bowl_line(self, bowl_num: int, y_pos: int, current_nav_option: Optional[str], edit_mode_active: bool,
                       duration: int, pattern: int, progress: Dict[str, int]):
        """Draw a bowl line per requested style: A/B on left, centered mm:ss, arrows, progress and active square."""
        # Determine if this bowl is active (has progress)
        is_active = progress.get('total', 0) > 0 and progress.get('percent', 0) < 100

        # Calculate progress percentage and fill width
        progress_percent = progress.get('percent', 0) / 100.0
        bar_x = 0
        bar_y = y_pos
        bar_width = 128
        bar_height = 10
        fill_width = int(bar_width * progress_percent) if is_active else 0

        # Draw progress fill first (behind)
        if is_active and fill_width > 0:
            self.display.draw_rectangle(bar_x, bar_y, fill_width, bar_height, filled=True, color=1)

        # Prepare time string for height alignment and later drawing
        remaining = progress.get('time_left', 0) if is_active else duration
        time_text = self._format_mm_ss(max(0, int(remaining)))
        _, time_h = self.display.get_text_size(time_text, size=1)

        # Labels A/B on the left, with filled square when active
        label = 'A' if bowl_num == 1 else 'B'
        label_x = 0
        label_box_w = 10
        label_box_h = 10
        # Align the label box vertically with the text row by centering in the text height
        label_y = y_pos + max(0, (time_h - label_box_h) // 2) + 3
        # Center the letter in the box regardless of font
        letter_w, letter_h = self.display.get_text_size(label, size=1)
        letter_x = label_x + max(1, (label_box_w - letter_w) // 2) + 1
        letter_y = label_y + max(0, (label_box_h - letter_h) // 2) - 3
        if is_active:
            # Filled white square behind label, then draw label in black
            self.display.draw_rectangle(label_x, label_y, label_box_w, label_box_h, filled=True, color=1)
            self.display.draw_text(letter_x, letter_y, label, size=1, color=0)
        else:
            # Filled white box with black letter (inverted colors)
            self.display.draw_rectangle(label_x, label_y, label_box_w, label_box_h, filled=True, color=1)
            self.display.draw_text(letter_x, letter_y, label, size=1, color=0)

        # Arrow visibility: always visible; blink when selected option matches
        arrow_option = f'bowl{bowl_num}_duration'
        pattern_option = f'bowl{bowl_num}_pattern'
        duration_selected = bool(current_nav_option and current_nav_option == arrow_option)
        pattern_selected = bool(current_nav_option and current_nav_option == pattern_option)
        blink_on = (self.blink_timer < self.blink_interval)

        # Left arrow near the centered time (we draw now, but position depends on text width later)
        # We'll compute its final x after formatting the time string

        # Time string in mm:ss, center it
        time_w, _ = self.display.get_text_size(time_text, size=1)
        time_x = (128 - time_w) // 2
        # Draw time text (invert overlapped portion if progress fills under it)
        self.display.draw_text(time_x, y_pos, time_text, size=1, color=1)
        if is_active and fill_width > time_x:
            overlap_start = max(time_x, 0)
            overlap_end = min(time_x + time_w, fill_width)
            if overlap_end > overlap_start:
                self.display.draw_text_inverted_region(time_x, y_pos, time_text, size=1,
                                                       invert_start=overlap_start, invert_end=overlap_end)

        # Duration cursor left to the time text; only show for current navigation option
        dur_cursor_x = max(12, time_x - 8)
        # Vertically center the cursor with the text row
        tri_h = max(6, time_h)
        tri_w = max(6, tri_h // 2)
        dur_cursor_y = y_pos + max(0, (time_h - tri_h) // 2) + 6
        if duration_selected:
            show_cursor = True
            if edit_mode_active:
                # Edit mode: show dash cursor (blinking)
                show_cursor = blink_on
                if show_cursor:
                    self._draw_edit_cursor(dur_cursor_x, dur_cursor_y, tri_w)
            else:
                # Navigation mode: show triangle cursor
                self.display.draw_filled_triangle(dur_cursor_x, dur_cursor_y, tri_w, tri_h, direction='right', color=1)

        # Pattern abbreviation on far right, with cursor just before it
        pattern_name = self.pattern_names.get(pattern, 'UNK')
        pat_w, _ = self.display.get_text_size(pattern_name, size=1)
        pat_x = 128 - pat_w
        # Pattern cursor only show for current navigation option
        pat_cursor_x = max(label_box_w + 2, pat_x - 8)
        pat_cursor_y = y_pos + max(0, (time_h - tri_h) // 2) + 6
        if pattern_selected:
            show_cursor = True
            if edit_mode_active:
                # Edit mode: show dash cursor (blinking)
                show_cursor = blink_on
                if show_cursor:
                    self._draw_edit_cursor(pat_cursor_x, pat_cursor_y, tri_w)
            else:
                # Navigation mode: show triangle cursor
                self.display.draw_filled_triangle(pat_cursor_x, pat_cursor_y, tri_w, tri_h, direction='right', color=1)
        # Draw pattern text (also invert if overlapped by progress)
        self.display.draw_text(pat_x, y_pos, pattern_name, size=1, color=1)
        if is_active and fill_width > pat_x:
            overlap_start = max(pat_x, 0)
            overlap_end = min(pat_x + pat_w, fill_width)
            if overlap_end > overlap_start:
                self.display.draw_text_inverted_region(pat_x, y_pos, pattern_name, size=1,
                                                       invert_start=overlap_start, invert_end=overlap_end)
    
    def _draw_edit_cursor(self, x: int, y: int, width: int = 6):
        """Draw a horizontal dash cursor for edit mode (indicates value can be changed)"""
        # Draw a horizontal line (dash) to indicate edit mode
        dash_y = y + 3  # Center vertically
        self.display.draw_rectangle(x, dash_y, width, 2, filled=True)
    
    def _draw_arrow(self, x: int, y: int):
        """Draw a right-pointing arrow (>)"""
        self.display.draw_text(x, y, ">", size=1, color=1)
    
    def _draw_operation_screen(self):
        """Draw the operation progress screen"""
        # Find active bowl
        active_bowl = None
        for bowl_num in [1, 2]:
            if self.state.operation_progress[bowl_num]['total'] > 0:
                active_bowl = bowl_num
                break
        
        if not active_bowl:
            self._draw_main_screen()
            return
        
        progress = self.state.operation_progress[active_bowl]
        
        # Operation header
        pattern_name = self.pattern_names.get(
            getattr(self.state, f'bowl{active_bowl}_pattern'), 'UNK'
        )
        self.display.draw_text(0, 0, f"BOWL {active_bowl} {pattern_name}", size=1)
        
        # Progress bar
        bar_width = 120
        bar_height = 8
        bar_x = 4
        bar_y = 16
        
        # Draw progress bar background
        self.display.draw_rectangle(bar_x, bar_y, bar_width, bar_height, filled=False)
        
        # Draw progress fill
        fill_width = int((progress['percent'] / 100) * bar_width)
        if fill_width > 0:
            self.display.draw_rectangle(bar_x, bar_y, fill_width, bar_height, filled=True)
        
        # Progress text
        progress_text = f"{progress['elapsed']}s/{progress['total']}s {progress['percent']}%"
        self.display.draw_text(0, 28, progress_text, size=1)
        
        # Pattern visualization (simple animation)
        self._draw_pattern_animation(active_bowl, progress['percent'])
    
    def _draw_navigation_screen(self):
        """Draw the navigation mode screen"""
        self.display.draw_text(0, 0, "NAVIGATION MODE", size=1)
        self.display.draw_text(0, 12, f"Option: {self.state.navigation_option}", size=1)
        
        # Show current settings being modified
        if 'bowl1' in self.state.navigation_option.lower():
            pattern = self.pattern_names.get(self.state.bowl1_pattern, 'UNK')
            self.display.draw_text(0, 24, f"B1: {self.state.bowl1_duration}s {pattern}", size=1)
        elif 'bowl2' in self.state.navigation_option.lower():
            pattern = self.pattern_names.get(self.state.bowl2_pattern, 'UNK')
            self.display.draw_text(0, 24, f"B2: {self.state.bowl2_duration}s {pattern}", size=1)
        
        # Navigation instructions
        self.display.draw_text(0, 48, "Use buttons to adjust", size=1)
    
    def _draw_memory_screen(self):
        """Draw the memory mode screen"""
        # Check if this is the "Quit to Dashboard" option (slot 4)
        if self.state.memory_slot == 4:
            self.display.draw_text(0, 0, "QUIT TO DASHBOARD", size=1)
            self.display.draw_text(0, 16, "Click to exit", size=1)
            self.display.draw_text(0, 28, "without changes", size=1)
            return
        
        # Regular memory slot display (slots 1-3)
        self.display.draw_text(0, 0, f"MEMORY SLOT {self.state.memory_slot}", size=1)
        
        slot_key = f'slot{self.state.memory_slot}'
        slot_data = self.state.memory_slots[slot_key]
        
        if slot_data['empty']:
            self.display.draw_text(0, 16, "EMPTY", size=1)
            self.display.draw_text(0, 28, "Press to save", size=1)
        else:
            self.display.draw_text(0, 16, "OCCUPIED", size=1)
            # Show memory data (truncated)
            data_preview = slot_data['data'][:20] + "..." if len(slot_data['data']) > 20 else slot_data['data']
            self.display.draw_text(0, 28, data_preview, size=1)
            self.display.draw_text(0, 40, "Press to load/erase", size=1)
    
    def _draw_pattern_animation(self, bowl: int, progress: int):
        """Draw a simple pattern animation"""
        pattern = getattr(self.state, f'bowl{bowl}_pattern')
        
        # Simple pattern visualization based on progress
        center_x, center_y = 64, 50
        radius = 8
        
        if pattern == 1:  # Standard - vertical line
            self.display.draw_line(center_x, center_y - radius, center_x, center_y + radius)
        elif pattern == 2:  # Circular - circle
            self.display.draw_circle(center_x, center_y, radius, filled=False)
        elif pattern == 3:  # Figure-8 - two circles
            self.display.draw_circle(center_x - radius//2, center_y, radius//2, filled=False)
            self.display.draw_circle(center_x + radius//2, center_y, radius//2, filled=False)
    
    def _get_state_symbol(self, state: str) -> str:
        """Get symbol for current state"""
        symbols = {
            'idle': '●',
            'whisking': '◐',
            'cleaning': '◑',
            'error': '✗'
        }
        return symbols.get(state, '?')
    
    def _update_queue_info(self):
        """Read queue information from state file and cache it (fallback if WebSocket message not received)"""
        try:
            current_time = time.time()
            # Only check state file periodically to avoid excessive file I/O
            # Also, only use state file as fallback if we haven't received a WebSocket message recently
            if current_time - self._last_state_file_check < self._state_file_check_interval:
                return
            
            # If we received a WebSocket queue message recently (within last 2 seconds), don't overwrite it
            if hasattr(self, '_last_queue_websocket_update'):
                if current_time - self._last_queue_websocket_update < 2.0:
                    return  # Use WebSocket value, don't overwrite with state file
            
            self._last_state_file_check = current_time
            
            state_data = self._load_state_file_json()
            if state_data:
                queue_info = state_data.get("queue", {})
                queue_display = queue_info.get("display", "")
                
                # Cache the queue display string (only if not set by WebSocket recently)
                if not hasattr(self, '_last_queue_websocket_update') or \
                   current_time - self._last_queue_websocket_update >= 2.0:
                    self._cached_queue_display = queue_display
            else:
                # State file unavailable, clear queue display (only if not set by WebSocket)
                if not hasattr(self, '_last_queue_websocket_update') or \
                   current_time - self._last_queue_websocket_update >= 2.0:
                    self._cached_queue_display = ""
                
        except Exception as e:
            logger.debug(f"Error updating queue info: {e}")
    
    def _mark_ws_state_update(self):
        self._last_ws_state_update = time.time()
    
    def _load_state_file_json(self) -> Optional[Dict[str, Any]]:
        """Load state file with retry logic for resilience"""
        self._stats['state_file_reads'] += 1
        
        for attempt in range(self._state_file_retry_max):
            try:
                with open(self.state_file_path, 'r') as f:
                    content = f.read()
                    if not content.strip():
                        # Empty file - wait and retry
                        if attempt < self._state_file_retry_max - 1:
                            time.sleep(self._state_file_retry_delay)
                            continue
                        return None
                    
                    result = json.loads(content)
                    # Reset retry count on success
                    self._state_file_retry_count = 0
                    return result
                    
            except FileNotFoundError:
                return None
            except json.JSONDecodeError as e:
                self._stats['state_file_errors'] += 1
                # File might be mid-write, retry
                if attempt < self._state_file_retry_max - 1:
                    time.sleep(self._state_file_retry_delay)
                    continue
                logger.debug(f"Invalid JSON in state file after {self._state_file_retry_max} attempts: {e}")
                return None
            except Exception as e:
                self._stats['state_file_errors'] += 1
                if attempt < self._state_file_retry_max - 1:
                    time.sleep(self._state_file_retry_delay)
                    continue
                logger.debug(f"Error reading state file: {e}")
                return None
        
        return None
    
    def _sync_state_from_file_if_needed(self):
        now = time.time()
        # ALWAYS read from state file in addition to WebSocket
        # - State file: Contains button presses, duration changes, navigation state
        #   (written by klipper_buttons.py)
        # - WebSocket: Contains machine status, operation progress, M118 messages
        #   (received from Moonraker)
        # Both sources are important - state file for UI state, WebSocket for machine state
        if now - self._last_state_file_sync < self._state_file_sync_interval:
            return
        state_data = self._load_state_file_json()
        if state_data:
            self._apply_state_file_state(state_data)
            self._last_state_file_sync = now
    
    def _apply_state_file_state(self, state_data: Dict[str, Any]):
        try:
            new_state = state_data.get("current_state", self.state.current_state)
            
            # If transitioning to idle, clear all progress tracking
            if new_state == "idle" and self.state.current_state != "idle":
                logger.debug("State transition to idle - clearing progress")
                for bowl_num in [1, 2]:
                    self.state.operation_progress[bowl_num] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            
            self.state.current_state = new_state
            self.state.operation_progress = self.state.operation_progress or {}
            self.state.bowl1_duration = state_data.get("bowl1_duration", self.state.bowl1_duration)
            self.state.bowl1_pattern = state_data.get("bowl1_pattern", self.state.bowl1_pattern)
            self.state.bowl2_duration = state_data.get("bowl2_duration", self.state.bowl2_duration)
            self.state.bowl2_pattern = state_data.get("bowl2_pattern", self.state.bowl2_pattern)
            nav_mode = state_data.get("nav_mode")
            if nav_mode is None:
                nav_mode = state_data.get("navigation_mode")
            nav_mode_bool = bool(nav_mode) if nav_mode is not None else self.state.navigation_mode
            self.state.navigation_mode = nav_mode_bool
            memory_mode = state_data.get("memory_mode")
            if memory_mode is None:
                memory_mode = state_data.get("memoryMode")
            memory_mode_bool = bool(memory_mode) if memory_mode is not None else self.state.memory_mode
            self.state.memory_mode = memory_mode_bool
            
            # Read edit_mode from state file - this is the authoritative source
            edit_mode = state_data.get("edit_mode")
            edit_mode_bool = bool(edit_mode) if edit_mode is not None else False
            self.state.edit_mode = edit_mode_bool
            
            # If memory mode is false, reset memory-related state
            if not memory_mode_bool:
                self.state.memory_slot = 1
            
            # REMOVED: Don't reset edit_mode here - trust the state file!
            # The state file from klipper_buttons.py is the authoritative source
            
            nav_option_key = state_data.get("navigation_option")
            # Clear navigation option if it's "Memory" but we're not in memory mode
            if nav_option_key == "Memory" and not memory_mode_bool:
                nav_option_key = ""
            # Use edit_mode_bool (from state file) for the check, not self.state.edit_mode
            if nav_option_key and (nav_mode_bool or edit_mode_bool or memory_mode_bool):
                self.state.selected_option = nav_option_key
                self.state.navigation_option = self.nav_option_display_names.get(nav_option_key, nav_option_key)
            elif not (nav_mode_bool or edit_mode_bool or memory_mode_bool):
                self.state.selected_option = None
                self.state.navigation_option = ''
            # Only update memory_slot from state file if memory_mode is true
            if memory_mode_bool:
                self.state.memory_slot = state_data.get("current_memory_slot", self.state.memory_slot)
            
            # Update memory slots
            memory_slots = state_data.get("memory_slots")
            if isinstance(memory_slots, dict):
                for idx in range(1, 4):
                    slot_key = f"slot{idx}"
                    slot_state = memory_slots.get(slot_key)
                    if not slot_state:
                        continue
                    empty = slot_state.get("isEmpty", True)
                    self.state.memory_slots[slot_key]['empty'] = empty
                    if empty:
                        self.state.memory_slots[slot_key]['data'] = ""
                    else:
                        summary = slot_state.get("name") or ""
                        if not summary:
                            b1 = slot_state.get("bowl1Duration", self.state.bowl1_duration)
                            p1 = slot_state.get("bowl1Pattern", self.state.bowl1_pattern)
                            b2 = slot_state.get("bowl2Duration", self.state.bowl2_duration)
                            p2 = slot_state.get("bowl2Pattern", self.state.bowl2_pattern)
                            summary = f"B1:{b1}s P{p1} | B2:{b2}s P{p2}"
                        self.state.memory_slots[slot_key]['data'] = summary
            
            # Operation progress
            current_operation = state_data.get("current_operation", {})
            bowl = current_operation.get("bowl")
            if bowl in [1, 2]:
                elapsed = int(current_operation.get("elapsed", 0))
                total = int(current_operation.get("total", 0))
                percent = int(current_operation.get("percent", 0))
                time_left = max(0, total - elapsed)
                # Only update if there's actual progress data
                if total > 0:
                    self.state.operation_progress[bowl] = {
                        'elapsed': elapsed,
                        'total': total,
                        'percent': percent,
                        'time_left': time_left
                    }
                    if percent < 100:
                        self.state.current_state = 'whisking'
                else:
                    # Total is 0, clear this bowl's progress
                    self.state.operation_progress[bowl] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            else:
                # No active bowl operation - clear all progress
                # This ensures progress bars are removed after emergency stop
                for bowl_num in [1, 2]:
                    self.state.operation_progress[bowl_num] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            effective_queue = state_data.get("queue", {})
            if effective_queue and not hasattr(self, '_last_queue_websocket_update'):
                self._cached_queue_display = effective_queue.get("display", self._cached_queue_display)
        except Exception as e:
            logger.debug(f"Error applying state file data: {e}")
    
    def _on_klipper_message(self, message: str):
        """Callback function called when Klipper interface receives a message from Moonraker WebSocket"""
        try:
            # The KlipperInterface callback should already send extracted M118 content,
            # but we check for M118 prefix as a safety measure in case raw messages come through
            if 'M118 ' in message:
                m118_content = message.split('M118 ', 1)[1].strip()
                logger.debug(f"Extracted M118 content from callback: {m118_content[:50]}")
                # Process immediately
                self._handle_message(m118_content)
                self._stats['messages_processed'] += 1
            elif message.startswith('UI_') or message.startswith('MACHINE_'):
                # Process UI messages directly (already extracted by KlipperInterface)
                logger.debug(f"Processing UI message from callback: {message[:50]}")
                self._handle_message(message)
                self._stats['messages_processed'] += 1
            elif message.strip():
                # Other messages - log for debugging
                logger.debug(f"Received other message from callback: {message[:50]}")
                # Add to queue for processing - with overflow protection
                try:
                    self.message_queue.put_nowait(message)
                except queue.Full:
                    # Queue is full - drop oldest message and add new one
                    self._stats['messages_dropped'] += 1
                    try:
                        self.message_queue.get_nowait()  # Remove oldest
                        self.message_queue.put_nowait(message)  # Add new
                    except queue.Empty:
                        pass  # Race condition, ignore
        except Exception as e:
            logger.error(f"Error in Klipper message callback: {e}")
            import traceback
            traceback.print_exc()
    
    def _process_messages(self):
        """Process messages from Klipper interface in a separate thread"""
        logger.info("Message processing thread started")
        while self.running:
            try:
                # Check for new messages from Klipper WebSocket (backup method)
                klipper_messages = self.klipper.get_messages()
                for message in klipper_messages:
                    # Extract M118 content if present
                    if 'M118 ' in message:
                        m118_content = message.split('M118 ', 1)[1].strip()
                        self._handle_message(m118_content)
                    elif message.startswith('UI_') or message.startswith('MACHINE_'):
                        self._handle_message(message)
                    else:
                        # Add to queue for processing
                        self.message_queue.put(message)
                
                # Process messages from queue (non-blocking)
                try:
                    message = self.message_queue.get(timeout=0.1)
                    logger.debug(f"Processing message from queue: {message}")
                    self._handle_message(message)
                except queue.Empty:
                    # No messages in queue, continue checking Klipper
                    pass
                
                # Small delay to prevent busy waiting
                time.sleep(0.05)  # 50ms delay
                
            except Exception as e:
                logger.error(f"Error processing messages: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(0.1)
    
    def _handle_message(self, message: str):
        """Handle incoming messages from Klipper - prioritize UI_STATUS messages"""
        try:
            # Log all received messages for debugging
            logger.debug(f"Received message: {message[:100]}")  # Log first 100 chars
            
            # Parse UI status messages (highest priority - these update all parameters)
            if message.startswith('UI_STATUS '):
                self._mark_ws_state_update()
                self._handle_status_message(message)
            elif message.startswith('UI_PROGRESS '):
                self._mark_ws_state_update()
                self._handle_progress_message(message)
            elif message.startswith('UI_OPERATION_START '):
                self._mark_ws_state_update()
                self._handle_operation_start(message)
            elif message.startswith('UI_OPERATION_COMPLETE '):
                self._mark_ws_state_update()
                self._handle_operation_complete(message)
            elif message.startswith('UI_MEMORY_'):
                self._mark_ws_state_update()
                self._handle_memory_message(message)
            elif message.startswith('UI_QUEUE '):
                self._mark_ws_state_update()
                self._handle_queue_message(message)
            elif 'MACHINE_IDLE' in message or 'Machine is now IDLE' in message:
                self.state.current_state = 'idle'
                # Reset progress when idle
                for bowl_num in [1, 2]:
                    if self.state.operation_progress[bowl_num]['percent'] >= 100:
                        self.state.operation_progress[bowl_num] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            elif 'Emergency stop' in message or 'firmware restarting' in message:
                self.state.current_state = 'idle'
                # CRITICAL: Clear all progress tracking immediately on emergency stop
                # This prevents stale progress bars from being displayed
                for bowl_num in [1, 2]:
                    self.state.operation_progress[bowl_num] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
                logger.info("Emergency stop - cleared all progress tracking")
                # Set cooldown to prevent display glitches during firmware restart
                self._emergency_stop_cooldown = time.time() + self._emergency_stop_duration
                logger.info(f"Emergency stop detected - display cooldown for {self._emergency_stop_duration}s")
            elif 'cleaning' in message.lower() or 'clean' in message.lower():
                if 'Moving to clean' in message or 'clean position' in message:
                    self.state.current_state = 'cleaning'
            elif 'whisking' in message.lower() or 'Starting whisking' in message.lower():
                self.state.current_state = 'whisking'
            else:
                # Log unhandled messages for debugging
                logger.debug(f"Unhandled message type: {message[:50]}")
                
        except Exception as e:
            logger.error(f"Error handling message '{message[:50]}...': {e}")
            import traceback
            traceback.print_exc()
    
    def _handle_status_message(self, message: str):
        """Handle UI_STATUS messages - process all parameters in the message"""
        logger.debug(f"Handling status message: {message}")
        # Parse parameter=value pairs
        # Message format: "UI_STATUS PARAM1=VALUE1 PARAM2=VALUE2 ..."
        parts = message[10:].split()  # Remove 'UI_STATUS '
        
        # Process all parameters in this message
        for part in parts:
            if '=' in part:
                try:
                    param, value = part.split('=', 1)
                    # Log all parameter updates for debugging
                    logger.debug(f"UI_STATUS: {param} = {value}")
                    # Update the parameter
                    self._update_state_parameter(param, value)
                except Exception as e:
                    logger.error(f"Error parsing parameter '{part}': {e}")
    
    def _update_state_parameter(self, param: str, value: str):
        """Update a specific state parameter"""
        try:
            if param == 'CURRENT_STATE':
                # Update state from Klipper
                self.state.current_state = value.lower()
                # If state is idle, check if we should reset progress
                if value.lower() == 'idle':
                    # Only reset if no active operations
                    for bowl_num in [1, 2]:
                        if self.state.operation_progress[bowl_num]['percent'] >= 100:
                            self.state.operation_progress[bowl_num] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            elif param == 'MACHINE_STATE':
                # Machine state from Klipper (idle_timeout state)
                # This can be used to verify machine status
                if value.lower() == 'idle':
                    if self.state.current_state == 'idle':
                        # Already idle, no change needed
                        pass
            elif param == 'NAVIGATION_MODE':
                self.state.navigation_mode = value.lower() == 'true'
                if not self.state.navigation_mode:
                    self.state.edit_mode = False
                    self.state.navigation_option = ''
                    self.state.selected_option = None
            elif param == 'MEMORY_MODE':
                self.state.memory_mode = value.lower() == 'true'
                if not self.state.memory_mode:
                    # Reset all memory and navigation state when exiting memory mode
                    self.state.edit_mode = False
                    self.state.navigation_mode = False
                    self.state.navigation_option = ''
                    self.state.selected_option = None
                    self.state.memory_slot = 1  # Reset to slot 1
            elif param == 'NAVIGATION_OPTION':
                old_option = self.state.navigation_option
                self.state.navigation_option = value
                # Map navigation option to internal option name
                nav_map = {
                    'Bowl 1 Duration': 'bowl1_duration',
                    'Bowl 1 Pattern': 'bowl1_pattern',
                    'Bowl 2 Duration': 'bowl2_duration',
                    'Bowl 2 Pattern': 'bowl2_pattern',
                    'Memory': 'memory'
                }
                self.state.selected_option = nav_map.get(value, None)
                # Log navigation option change for debugging
                if old_option != value:
                    logger.info(f"Navigation option changed: {old_option} -> {value} (selected: {self.state.selected_option})")
                    self.state.edit_mode = False
            elif param == 'MEMORY_SLOT':
                self.state.memory_slot = int(value)
            elif param == 'BOWL1_DURATION':
                # If we're navigating and this option matches, we're in edit mode
                if self.state.navigation_mode and self.state.navigation_option == 'Bowl 1 Duration':
                    self.state.edit_mode = True
                self.state.bowl1_duration = int(value)
            elif param == 'BOWL1_PATTERN':
                # If we're navigating and this option matches, we're in edit mode
                if self.state.navigation_mode and self.state.navigation_option == 'Bowl 1 Pattern':
                    self.state.edit_mode = True
                self.state.bowl1_pattern = int(value)
            elif param == 'BOWL2_DURATION':
                # If we're navigating and this option matches, we're in edit mode
                if self.state.navigation_mode and self.state.navigation_option == 'Bowl 2 Duration':
                    self.state.edit_mode = True
                self.state.bowl2_duration = int(value)
            elif param == 'BOWL2_PATTERN':
                # If we're navigating and this option matches, we're in edit mode
                if self.state.navigation_mode and self.state.navigation_option == 'Bowl 2 Pattern':
                    self.state.edit_mode = True
                self.state.bowl2_pattern = int(value)
            elif param.startswith('MEMORY') and '_EMPTY' in param:
                slot_num = param[6:7]  # Extract slot number
                slot_key = f'slot{slot_num}'
                self.state.memory_slots[slot_key]['empty'] = value.lower() == 'true'
            elif param.startswith('MEMORY') and '_DATA' in param:
                slot_num = param[6:7]  # Extract slot number
                slot_key = f'slot{slot_num}'
                self.state.memory_slots[slot_key]['data'] = value
                
        except (ValueError, KeyError) as e:
            logger.error(f"Error updating parameter {param}={value}: {e}")
    
    def _handle_progress_message(self, message: str):
        """Handle UI_PROGRESS messages"""
        # Parse: UI_PROGRESS BOWL=1 ELAPSED=5 TOTAL=15 PERCENT=33
        parts = message[12:].split()  # Remove 'UI_PROGRESS '
        
        bowl = None
        elapsed = 0
        total = 0
        percent = 0
        
        for part in parts:
            if '=' in part:
                key, value = part.split('=', 1)
                if key == 'BOWL':
                    bowl = int(value)
                elif key == 'ELAPSED':
                    elapsed = int(value)
                elif key == 'TOTAL':
                    total = int(value)
                elif key == 'PERCENT':
                    percent = int(value)
        
        if bowl and bowl in [1, 2]:
            # Calculate time left
            time_left = max(0, total - elapsed)
            self.state.operation_progress[bowl] = {
                'elapsed': elapsed,
                'total': total,
                'percent': percent,
                'time_left': time_left
            }
            # Set current state to whisking if operation is active
            if percent < 100 and total > 0:
                self.state.current_state = 'whisking'
            elif percent >= 100:
                # Operation complete, will be handled by completion message
                pass
    
    def _handle_operation_start(self, message: str):
        """Handle UI_OPERATION_START messages"""
        # Parse: UI_OPERATION_START BOWL=1 DURATION=15 PATTERN=2 PATTERN_NAME=Circular
        parts = message[19:].split()  # Remove 'UI_OPERATION_START '
        
        bowl = None
        duration = 0
        pattern = 0
        
        for part in parts:
            if '=' in part:
                key, value = part.split('=', 1)
                if key == 'BOWL':
                    bowl = int(value)
                elif key == 'DURATION':
                    duration = int(value)
                elif key == 'PATTERN':
                    pattern = int(value)
        
        if bowl and bowl in [1, 2]:
            # Set current state to whisking
            self.state.current_state = 'whisking'
            # Initialize progress tracking
            self.state.operation_progress[bowl] = {
                'elapsed': 0,
                'total': duration,
                'percent': 0,
                'time_left': duration
            }
            # Update bowl pattern
            if bowl == 1:
                self.state.bowl1_pattern = pattern
            else:
                self.state.bowl2_pattern = pattern
        
        logger.info(f"Operation started: Bowl {bowl}, Duration {duration}s, Pattern {pattern}")
    
    def _handle_operation_complete(self, message: str):
        """Handle UI_OPERATION_COMPLETE messages"""
        # Parse bowl number
        parts = message[20:].split()  # Remove 'UI_OPERATION_COMPLETE '
        bowl = None
        
        for part in parts:
            if '=' in part:
                key, value = part.split('=', 1)
                if key == 'BOWL':
                    bowl = int(value)
                    break
        
        if bowl and bowl in [1, 2]:
            # Reset progress for completed bowl
            self.state.operation_progress[bowl] = {'elapsed': 0, 'total': 0, 'percent': 0, 'time_left': 0}
            # Set state back to idle after operation completes
            # Check if other bowl is still active
            other_bowl = 2 if bowl == 1 else 1
            if self.state.operation_progress[other_bowl]['total'] > 0 and self.state.operation_progress[other_bowl]['percent'] < 100:
                # Other bowl is still active, keep state as whisking
                pass
            else:
                # No active operations, set to idle
                self.state.current_state = 'idle'
        
        logger.info(f"Operation completed: Bowl {bowl}")
    
    def _handle_memory_message(self, message: str):
        """Handle memory-related messages"""
        logger.info(f"Memory event: {message}")
        
        def exit_to_dashboard():
            self.state.memory_mode = False
            self.state.navigation_mode = False
            self.state.edit_mode = False
            self.state.navigation_option = ''
            self.state.selected_option = None
            self.state.memory_slot = 1  # Reset to slot 1 for next time
        
        # Handle memory mode exit
        if 'UI_MEMORY_MODE_EXIT' in message:
            exit_to_dashboard()
        # Handle memory saved
        elif 'UI_MEMORY_SAVED' in message:
            # Parse slot number
            parts = message.split()
            for part in parts:
                if 'SLOT=' in part:
                    slot = int(part.split('=')[1])
                    self.state.memory_slot = slot
                    slot_key = f'slot{slot}'
                    self.state.memory_slots[slot_key]['empty'] = False
            exit_to_dashboard()
        # Handle memory loaded
        elif 'UI_MEMORY_LOADED' in message:
            # Parse slot number
            parts = message.split()
            for part in parts:
                if 'SLOT=' in part:
                    slot = int(part.split('=')[1])
                    self.state.memory_slot = slot
            exit_to_dashboard()
        # Handle memory erased
        elif 'UI_MEMORY_ERASED' in message:
            # Parse slot number
            parts = message.split()
            for part in parts:
                if 'SLOT=' in part:
                    slot = int(part.split('=')[1])
                    slot_key = f'slot{slot}'
                    self.state.memory_slots[slot_key]['empty'] = True
                    self.state.memory_slots[slot_key]['data'] = ''
            exit_to_dashboard()
        
        # Memory state updates are also handled by status messages
    
    def _handle_queue_message(self, message: str):
        """Handle UI_QUEUE messages from Moonraker WebSocket"""
        try:
            # Parse: UI_QUEUE COUNT=2 DISPLAY="B1 B2" IS_EMPTY=False
            parts = message[9:].split()  # Remove 'UI_QUEUE '
            
            queue_count = 0
            queue_display = ""
            is_empty = True
            
            for part in parts:
                if '=' in part:
                    key, value = part.split('=', 1)
                    if key == 'COUNT':
                        queue_count = int(value)
                    elif key == 'DISPLAY':
                        # Remove quotes if present
                        queue_display = value.strip('"\'')
                    elif key == 'IS_EMPTY':
                        is_empty = value.lower() == 'true'
            
            # Update cached queue display (from WebSocket - takes priority)
            self._cached_queue_display = queue_display
            self._last_queue_websocket_update = time.time()  # Track when we received WebSocket update
            
            logger.debug(f"Queue update received via WebSocket: count={queue_count}, display='{queue_display}', is_empty={is_empty}")
            
        except Exception as e:
            logger.error(f"Error handling queue message '{message}': {e}")
            import traceback
            traceback.print_exc()
    
    def _monitoring_loop(self):
        """Background thread for periodic stats logging and health monitoring"""
        logger.info("Monitoring thread started")
        
        while self.running:
            try:
                # Log stats periodically
                self._log_stats()
                
                # Sleep for a bit before next check
                time.sleep(30)  # Check every 30 seconds
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(5)
        
        logger.info("Monitoring thread stopped")
    
    def _log_stats(self, force: bool = False):
        """Log dashboard statistics for monitoring"""
        now = time.time()
        
        # Only log if enough time has passed (or forced)
        if not force and now - self._stats['last_stats_log'] < self._stats_log_interval:
            return
        
        self._stats['last_stats_log'] = now
        
        # Calculate uptime
        uptime_seconds = int(now - self._stats['start_time'])
        uptime_hours = round(uptime_seconds / 3600, 2)
        
        # Calculate rates
        total_frames = self._stats['frames_rendered'] + self._stats['frames_skipped']
        frame_success_rate = round(
            self._stats['frames_rendered'] / max(1, total_frames) * 100, 2
        )
        
        state_file_success_rate = round(
            (self._stats['state_file_reads'] - self._stats['state_file_errors']) / 
            max(1, self._stats['state_file_reads']) * 100, 2
        )
        
        # Get display stats
        display_stats = self.display.get_stats() if hasattr(self.display, 'get_stats') else {}
        
        # Get queue size
        queue_size = self.message_queue.qsize()
        
        # Build log message
        log_msg = (
            f"[STATS] Uptime: {uptime_hours}h | "
            f"Frames: {self._stats['frames_rendered']} ({frame_success_rate}% success) | "
            f"Skipped: {self._stats['frames_skipped']} | "
            f"StateFile: {self._stats['state_file_reads']} reads, {self._stats['state_file_errors']} errors ({state_file_success_rate}% success) | "
            f"Messages: {self._stats['messages_processed']} processed, {self._stats['messages_dropped']} dropped | "
            f"Queue: {queue_size}/{self._message_queue_max}"
        )
        
        # Add display stats if available
        if display_stats:
            log_msg += (
                f" | I2C: {display_stats.get('updates_total', 0)} updates, "
                f"{display_stats.get('updates_failed', 0)} failed, "
                f"{display_stats.get('i2c_reinits', 0)} reinits | "
                f"Cache: {display_stats.get('cache_hit_rate', 0)}% hit rate"
            )
        
        logger.info(log_msg)
        
        # Log warning if there are issues
        if self._stats['state_file_errors'] > 10:
            logger.warning(f"High state file error count: {self._stats['state_file_errors']}")
        
        if self._stats['messages_dropped'] > 0:
            logger.warning(f"Messages have been dropped due to queue overflow: {self._stats['messages_dropped']}")
        
        if display_stats.get('i2c_reinits', 0) > 0:
            logger.warning(f"I2C display has been reinitialized {display_stats.get('i2c_reinits', 0)} times")
    
    def get_stats(self) -> dict:
        """Get all dashboard statistics"""
        now = time.time()
        uptime = now - self._stats['start_time']
        
        return {
            **self._stats,
            'uptime_seconds': int(uptime),
            'uptime_hours': round(uptime / 3600, 2),
            'queue_size': self.message_queue.qsize(),
            'queue_max': self._message_queue_max,
            'display_stats': self.display.get_stats() if hasattr(self.display, 'get_stats') else {},
            'klipper_connected': self.klipper.is_connected()
        }

def main():
    """Main entry point"""
    dashboard = OLEDDashboard()
    
    try:
        if dashboard.start():
            logger.info("Dashboard running. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
        else:
            logger.error("Failed to start dashboard")
            return 1
            
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
    finally:
        dashboard.stop()
    
    return 0

if __name__ == '__main__':
    exit(main())
