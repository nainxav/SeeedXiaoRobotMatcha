#!/usr/bin/env python3
"""
OLED Display Controller for 128x64 SSD1306 OLED
Handles low-level display operations and graphics primitives
Compatible with Orange Pi Zero 2 W (Allwinner H616)
"""

import time
import logging
from typing import Tuple, Optional, Dict
from PIL import Image, ImageDraw, ImageFont
from ssd1306_smbus import SSD1306_I2C

logger = logging.getLogger(__name__)

class OLEDDisplay:
    """OLED Display controller for 128x64 OLED display (SSD1309/SSD1306)"""
    
    # Orange Pi Zero 2 W: OLED is on I2C bus 1 at address 0x3C
    def __init__(self, width: int = 128, height: int = 64, i2c_address: int = 0x3C, i2c_port: int = 2,
                 controller=None):
        self.width = width
        self.height = height
        self.i2c_address = i2c_address
        self.i2c_port = i2c_port
        
        # Optional pre-built controller (e.g. SSD1306_Serial for a Seeed XIAO
        # peripheral). When provided, initialize() uses it instead of opening
        # the local I2C bus. Must expose show_image(image) and fill(color).
        self._injected_controller = controller
        
        # Display objects
        self.controller = None
        self.image = None
        self.draw = None
        
        # Fonts
        self.fonts = {}
        self._load_fonts()
        
        # Initialize flag
        self.initialized = False
        
        # PERFORMANCE FIX: Cached temp image for text measurement (prevents memory leak)
        # Instead of creating new Image objects on every get_text_size() call
        self._text_measure_image = Image.new('1', (1, 1))
        self._text_measure_draw = ImageDraw.Draw(self._text_measure_image)
        self._text_size_cache: Dict[str, Tuple[int, int]] = {}  # Cache text sizes
        self._text_size_cache_max = 100  # Max cache entries
        
        # I2C error recovery tracking
        self._i2c_error_count = 0
        self._i2c_error_max = 5  # Errors before reinit attempt
        self._last_i2c_error_time = 0
        self._i2c_error_reset_interval = 60  # Reset error count after 60s of no errors
        
        # Monitoring stats
        self.stats = {
            'updates_total': 0,
            'updates_failed': 0,
            'i2c_reinits': 0,
            'text_cache_hits': 0,
            'text_cache_misses': 0,
            'start_time': time.time()
        }
        
        logger.info(f"OLED Display initialized for {width}x{height} display on I2C port {i2c_port}, address 0x{i2c_address:02X}")
    
    def _load_fonts(self):
        """Load fonts for different text sizes"""
        try:
            # Start with defaults
            self.fonts['small'] = ImageFont.load_default()
            self.fonts['medium'] = ImageFont.load_default()
            self.fonts['large'] = ImageFont.load_default()

            # Preferred: Pixel Operator (monospace, crisp at small sizes)
            pixel_operator_candidates = [
                "/usr/share/fonts/truetype/pixel_operator/PixelOperator.ttf",
                "/usr/share/fonts/truetype/pixel-operator/PixelOperator.ttf",
                "/usr/share/fonts/truetype/pixeloperator/PixelOperator.ttf",
                "/usr/local/share/fonts/PixelOperator.ttf",
                "/usr/share/fonts/TTF/PixelOperator.ttf"
            ]

            loaded_pixel = False
            for path in pixel_operator_candidates:
                try:
                    
                    self.fonts['small'] = ImageFont.truetype(path, 10)
                    self.fonts['medium'] = ImageFont.truetype(path, 12)
                    self.fonts['large'] = ImageFont.truetype(path, 14)
                    loaded_pixel = True
                    loaded_pixel = True
                    logger.info(f"Loaded Pixel Operator font from {path}")
                    break
                except OSError:
                    continue

            if not loaded_pixel:
                # Fallback to DejaVu Sans if Pixel Operator not found
                try:
                    self.fonts['small'] = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
                    self.fonts['medium'] = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
                    self.fonts['large'] = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
                    logger.info("Loaded DejaVu Sans fonts (fallback)")
                except OSError:
                    logger.warning("Could not load DejaVu fonts; using PIL default bitmap font")

        except Exception as e:
            logger.error(f"Error loading fonts: {e}")
            # Fallback to default font
            self.fonts = {
                'small': ImageFont.load_default(),
                'medium': ImageFont.load_default(),
                'large': ImageFont.load_default()
            }
    
    def initialize(self) -> bool:
        """Initialize the OLED display using the injected controller or SMBus SSD1306"""
        try:
            if self._injected_controller is not None:
                self.controller = self._injected_controller
            else:
                self.controller = SSD1306_I2C(self.width, self.height, self.i2c_port, self.i2c_address)
            self.image = Image.new('1', (self.width, self.height), 0)
            self.draw = ImageDraw.Draw(self.image)
            self.clear()
            self.update()
            self.initialized = True
            logger.info("OLED display initialized successfully using SMBus SSD1306")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize OLED display: {e}")
            return False
    
    def clear(self):
        """Clear the display buffer"""
        if self.draw:
            self.draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)
    
    def hard_clear(self):
        """Force clear both buffer and physical display memory directly.
        
        Use this after display glitches or connection issues to ensure
        no stale data remains in the display's internal memory.
        """
        # Clear PIL image buffer
        if self.draw:
            self.draw.rectangle((0, 0, self.width, self.height), outline=0, fill=0)
        
        # Clear physical display memory directly via controller
        if self.controller:
            try:
                self.controller.fill(0)
            except Exception as e:
                logger.error(f"Error during hard clear: {e}")
    
    def update(self):
        """Update the physical display with buffer contents - with I2C error recovery"""
        if self.controller and self.image:
            self.stats['updates_total'] += 1
            try:
                self.controller.show_image(self.image)
                
                # Reset error count on success (if enough time has passed)
                if self._i2c_error_count > 0:
                    if time.time() - self._last_i2c_error_time > self._i2c_error_reset_interval:
                        self._i2c_error_count = 0
                        logger.info("I2C error count reset after successful period")
                        
            except Exception as e:
                self.stats['updates_failed'] += 1
                self._i2c_error_count += 1
                self._last_i2c_error_time = time.time()
                logger.error(f"Error updating display (error #{self._i2c_error_count}): {e}")
                
                # Attempt recovery if too many errors
                if self._i2c_error_count >= self._i2c_error_max:
                    logger.warning(f"I2C error threshold reached ({self._i2c_error_max}), attempting display reinit...")
                    self._attempt_i2c_recovery()
    
    def _attempt_i2c_recovery(self):
        """Attempt to recover from I2C errors by reinitializing the display"""
        try:
            self.stats['i2c_reinits'] += 1
            logger.info("Attempting I2C display recovery...")
            
            # Close existing controller if possible
            if self.controller:
                try:
                    self.controller.fill(0)  # Try to clear
                except:
                    pass  # Ignore errors during cleanup
            
            # Small delay before reinit
            time.sleep(0.5)
            
            # Reinitialize display. An injected controller (e.g. the XIAO serial
            # backend) manages its own transport, so just reuse it.
            if self._injected_controller is not None:
                self.controller = self._injected_controller
            else:
                self.controller = SSD1306_I2C(self.width, self.height, self.i2c_port, self.i2c_address)
            self.image = Image.new('1', (self.width, self.height), 0)
            self.draw = ImageDraw.Draw(self.image)
            
            # Reset error count
            self._i2c_error_count = 0
            
            logger.info("I2C display recovery successful")
            
        except Exception as e:
            logger.error(f"I2C display recovery failed: {e}")
            # Don't reset error count - will try again after more errors
    
    def draw_text(self, x: int, y: int, text: str, size: int = 1, color: int = 1):
        """Draw text on the display
        
        Args:
            x, y: Position coordinates
            text: Text to draw
            size: Font size (1=small, 2=medium, 3=large)
            color: 0=black, 1=white
        """
        if not self.draw:
            return
        
        try:
            # Select font based on size
            font_key = 'small' if size == 1 else 'medium' if size == 2 else 'large'
            font = self.fonts.get(font_key, self.fonts['small'])
            
            self.draw.text((x, y), text, font=font, fill=color)
            
        except Exception as e:
            logger.error(f"Error drawing text '{text}' at ({x}, {y}): {e}")
    
    def draw_text_inverted_region(self, x: int, y: int, text: str, size: int = 1,
                                  invert_start: int = 0, invert_end: int = 0):
        """Draw text with an inverted color region
        
        Args:
            x, y: Position coordinates
            text: Text to draw
            size: Font size
            invert_start: Start pixel position to invert (absolute x coordinate)
            invert_end: End pixel position to invert (absolute x coordinate)
        """
        if not self.draw:
            return
        
        try:
            # Get text dimensions
            text_width, text_height = self.get_text_size(text, size)
            
            # If inversion region overlaps text, invert that portion
            if invert_end > invert_start and invert_end > x and invert_start < (x + text_width):
                # Calculate overlap region
                region_x = max(x, invert_start)
                region_y = y
                region_w = min(x + text_width, invert_end) - region_x
                region_h = text_height
                
                if region_w > 0 and region_h > 0:
                    # For 1-bit images, use more efficient pixel access
                    # Access pixels directly using load()
                    pixels = self.image.load()
                    
                    # Invert the region pixels
                    for py in range(region_y, min(region_y + region_h, self.image.height)):
                        for px in range(region_x, min(region_x + region_w, self.image.width)):
                            # Invert: 0 becomes 1, 1 becomes 0 (XOR with 1)
                            current_pixel = pixels[px, py]
                            pixels[px, py] = 1 - current_pixel
                    
        except Exception as e:
            logger.error(f"Error drawing inverted text '{text}' at ({x}, {y}): {e}")
    
    def draw_line(self, x1: int, y1: int, x2: int, y2: int, color: int = 1):
        """Draw a line on the display"""
        if not self.draw:
            return
        
        try:
            self.draw.line([(x1, y1), (x2, y2)], fill=color)
        except Exception as e:
            logger.error(f"Error drawing line from ({x1}, {y1}) to ({x2}, {y2}): {e}")
    
    def draw_rectangle(self, x: int, y: int, width: int, height: int, 
                      filled: bool = False, color: int = 1):
        """Draw a rectangle on the display"""
        if not self.draw:
            return
        
        try:
            if filled:
                self.draw.rectangle([(x, y), (x + width, y + height)], fill=color)
            else:
                self.draw.rectangle([(x, y), (x + width, y + height)], outline=color)
        except Exception as e:
            logger.error(f"Error drawing rectangle at ({x}, {y}) size {width}x{height}: {e}")
    
    def draw_circle(self, x: int, y: int, radius: int, filled: bool = False, color: int = 1):
        """Draw a circle on the display"""
        if not self.draw:
            return
        
        try:
            if filled:
                self.draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], fill=color)
            else:
                self.draw.ellipse([(x - radius, y - radius), (x + radius, y + radius)], outline=color)
        except Exception as e:
            logger.error(f"Error drawing circle at ({x}, {y}) radius {radius}: {e}")
    
    def draw_filled_triangle(self, x: int, y: int, width: int, height: int, direction: str = 'right', color: int = 1):
        """Draw a filled triangle (used for arrow indicators)
        
        Args:
            x, y: Top-left of bounding box
            width, height: Size of triangle
            direction: 'right', 'left', 'up', 'down'
            color: 0=black, 1=white
        """
        if not self.draw:
            return
        
        try:
            if direction == 'right':
                points = [(x, y), (x, y + height), (x + width, y + height // 2)]
            elif direction == 'left':
                points = [(x + width, y), (x + width, y + height), (x, y + height // 2)]
            elif direction == 'up':
                points = [(x, y + height), (x + width, y + height), (x + width // 2, y)]
            else:  # down
                points = [(x, y), (x + width, y), (x + width // 2, y + height)]
            self.draw.polygon(points, fill=color)
        except Exception as e:
            logger.error(f"Error drawing filled triangle at ({x}, {y}) size {width}x{height}: {e}")
    
    def draw_pixel(self, x: int, y: int, color: int = 1):
        """Draw a single pixel"""
        if not self.draw:
            return
        
        try:
            self.draw.point((x, y), fill=color)
        except Exception as e:
            logger.error(f"Error drawing pixel at ({x}, {y}): {e}")
    
    def draw_progress_bar(self, x: int, y: int, width: int, height: int, 
                         progress: float, color: int = 1):
        """Draw a progress bar
        
        Args:
            x, y: Position
            width, height: Size
            progress: Progress value (0.0 to 1.0)
            color: Color (0=black, 1=white)
        """
        if not self.draw:
            return
        
        try:
            # Draw background
            self.draw_rectangle(x, y, width, height, filled=False, color=color)
            
            # Draw progress fill
            if progress > 0:
                fill_width = int(width * progress)
                if fill_width > 0:
                    self.draw_rectangle(x, y, fill_width, height, filled=True, color=color)
                    
        except Exception as e:
            logger.error(f"Error drawing progress bar: {e}")
    
    def draw_bitmap(self, x: int, y: int, bitmap_data: list, width: int, height: int):
        """Draw a bitmap from data array
        
        Args:
            x, y: Position
            bitmap_data: List of bytes representing bitmap
            width, height: Bitmap dimensions
        """
        if not self.draw:
            return
        
        try:
            # Convert bitmap data to image
            bitmap_image = Image.frombytes('1', (width, height), bytes(bitmap_data))
            self.image.paste(bitmap_image, (x, y))
        except Exception as e:
            logger.error(f"Error drawing bitmap: {e}")
    
    def get_text_size(self, text: str, size: int = 1) -> Tuple[int, int]:
        """Get the size of text when rendered - OPTIMIZED with caching
        
        Returns:
            Tuple of (width, height) in pixels
        """
        try:
            # Check cache first (key = text + size)
            cache_key = f"{size}:{text}"
            if cache_key in self._text_size_cache:
                self.stats['text_cache_hits'] += 1
                return self._text_size_cache[cache_key]
            
            self.stats['text_cache_misses'] += 1
            
            font_key = 'small' if size == 1 else 'medium' if size == 2 else 'large'
            font = self.fonts.get(font_key, self.fonts['small'])
            
            # PERFORMANCE FIX: Use cached draw object instead of creating new ones
            # This prevents memory leak from creating thousands of temp images
            bbox = self._text_measure_draw.textbbox((0, 0), text, font=font)
            result = (bbox[2] - bbox[0], bbox[3] - bbox[1])
            
            # Cache the result (with LRU-style eviction)
            if len(self._text_size_cache) >= self._text_size_cache_max:
                # Remove oldest entries (first 20%)
                keys_to_remove = list(self._text_size_cache.keys())[:self._text_size_cache_max // 5]
                for key in keys_to_remove:
                    del self._text_size_cache[key]
            
            self._text_size_cache[cache_key] = result
            return result
            
        except Exception as e:
            logger.error(f"Error getting text size: {e}")
            return 0, 0
    
    def draw_centered_text(self, y: int, text: str, size: int = 1, color: int = 1):
        """Draw text centered horizontally"""
        text_width, text_height = self.get_text_size(text, size)
        x = (self.width - text_width) // 2
        self.draw_text(x, y, text, size, color)
    
    def draw_scrolling_text(self, y: int, text: str, size: int = 1, 
                           scroll_offset: int = 0, color: int = 1):
        """Draw text with horizontal scrolling
        
        Args:
            y: Vertical position
            text: Text to draw
            size: Font size
            scroll_offset: Horizontal scroll offset in pixels
            color: Color
        """
        text_width, text_height = self.get_text_size(text, size)
        
        # If text fits, draw normally
        if text_width <= self.width:
            self.draw_text(0, y, text, size, color)
            return
        
        # Calculate visible portion
        visible_start = scroll_offset % text_width
        visible_end = min(visible_start + self.width, text_width)
        
        # Draw visible portion
        visible_text = text[visible_start:visible_end] if visible_start < len(text) else ""
        self.draw_text(0, y, visible_text, size, color)
    
    def draw_status_indicator(self, x: int, y: int, status: str, size: int = 1):
        """Draw a status indicator with symbol and text
        
        Args:
            x, y: Position
            status: Status string ('idle', 'whisking', 'cleaning', 'error')
            size: Font size
        """
        symbols = {
            'idle': '●',
            'whisking': '◐',
            'cleaning': '◑',
            'error': '✗',
            'connected': '●',
            'disconnected': '○'
        }
        
        symbol = symbols.get(status.lower(), '?')
        self.draw_text(x, y, f"{symbol} {status.upper()}", size)
    
    def draw_battery_indicator(self, x: int, y: int, level: float, size: int = 1):
        """Draw a battery level indicator
        
        Args:
            x, y: Position
            level: Battery level (0.0 to 1.0)
            size: Font size
        """
        # Battery outline
        battery_width = 20
        battery_height = 10
        
        # Main battery body
        self.draw_rectangle(x, y, battery_width, battery_height, filled=False)
        
        # Battery terminal
        self.draw_rectangle(x + battery_width, y + 2, 2, battery_height - 4, filled=True)
        
        # Battery level
        if level > 0:
            fill_width = int((battery_width - 2) * level)
            if fill_width > 0:
                self.draw_rectangle(x + 1, y + 1, fill_width, battery_height - 2, filled=True)
    
    def draw_queue_info(self, queue_display: str, size: int = 1, margin: int = 2):
        """Draw queue information in the top right corner of the display
        
        Args:
            queue_display: Queue display string (e.g., "B1 B2 C" or "B1 B2 +2")
            size: Font size (default: 1 for small)
            margin: Margin from right edge in pixels (default: 2)
        """
        if not queue_display or queue_display == "":
            return  # Don't draw anything if queue is empty
        
        try:
            # Get text size to calculate position
            text_width, text_height = self.get_text_size(queue_display, size)
            
            # Calculate x position for top right (right-aligned with margin)
            x_pos = self.width - text_width - margin
            
            # Draw queue info at top right (y = 0 or small offset)
            self.draw_text(x_pos, 0, queue_display, size, color=1)
            
        except Exception as e:
            logger.error(f"Error drawing queue info '{queue_display}': {e}")
    
    def cleanup(self):
        """Clean up display resources"""
        try:
            if self.controller:
                self.clear()
                self.update()
            logger.info("OLED display cleaned up")
        except Exception as e:
            logger.error(f"Error during display cleanup: {e}")
    
    def is_available(self) -> bool:
        """Check if OLED display is available"""
        return self.initialized
    
    def get_dimensions(self) -> Tuple[int, int]:
        """Get display dimensions"""
        return self.width, self.height
    
    def get_stats(self) -> dict:
        """Get display statistics for monitoring"""
        uptime = time.time() - self.stats['start_time']
        return {
            **self.stats,
            'uptime_seconds': int(uptime),
            'uptime_hours': round(uptime / 3600, 2),
            'i2c_error_count': self._i2c_error_count,
            'text_cache_size': len(self._text_size_cache),
            'update_success_rate': round(
                (self.stats['updates_total'] - self.stats['updates_failed']) / 
                max(1, self.stats['updates_total']) * 100, 2
            ),
            'cache_hit_rate': round(
                self.stats['text_cache_hits'] / 
                max(1, self.stats['text_cache_hits'] + self.stats['text_cache_misses']) * 100, 2
            )
        }
    
    def clear_text_cache(self):
        """Clear the text size cache (useful if fonts change)"""
        self._text_size_cache.clear()
        logger.info("Text size cache cleared")

# Test function for development
def test_display():
    """Test function for OLED display"""
    display = OLEDDisplay()
    
    if not display.initialize():
        print("Failed to initialize display")
        return
    
    try:
        # Test basic drawing functions
        display.clear()
        display.draw_text(0, 0, "OLED Test", size=2)
        display.draw_text(0, 16, "Line Test", size=1)
        display.draw_line(0, 24, 127, 24)
        
        # Test progress bar
        display.draw_progress_bar(10, 30, 100, 8, 0.6)
        
        # Test circle
        display.draw_circle(64, 50, 8, filled=False)
        
        display.update()
        
        print("Display test completed")
        time.sleep(2)
        
    except Exception as e:
        print(f"Test error: {e}")
    finally:
        display.cleanup()

if __name__ == '__main__':
    test_display()
