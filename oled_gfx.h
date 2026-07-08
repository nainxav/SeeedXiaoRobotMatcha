/*
 * oled_gfx.h — framebuffer text/graphics helper for the XIAO front panel.
 *
 * Wraps Adafruit_SSD1306 (128x64, software SPI) behind a small drawing API
 * used to reproduce the manual_images dashboard layout.
 *
 * Libraries: Adafruit GFX, Adafruit SSD1306
 */
#pragma once

#include <SPI.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#ifndef OLED_GFX_WIDTH
#define OLED_GFX_WIDTH 128
#endif
#ifndef OLED_GFX_HEIGHT
#define OLED_GFX_HEIGHT 64
#endif

static Adafruit_SSD1306* gfx = nullptr;
static bool gfxReady = false;

// Software-SPI OLED init. rst may be -1 if the panel's RES is tied to 3V3.
inline bool oledBegin(int8_t mosi, int8_t sck, int8_t dc, int8_t cs, int8_t rst) {
  if (gfx == nullptr) {
    gfx = new Adafruit_SSD1306(OLED_GFX_WIDTH, OLED_GFX_HEIGHT, mosi, sck, dc, rst, cs);
  }
  gfxReady = gfx->begin(SSD1306_SWITCHCAPVCC);
  if (gfxReady) {
    gfx->clearDisplay();
    gfx->display();
  }
  return gfxReady;
}

// Clear the buffer, run the caller's draw routine, then push to the panel.
inline void oledDrawFrame(void (*draw)()) {
  if (!gfxReady || gfx == nullptr) return;
  gfx->clearDisplay();
  gfx->setTextSize(1);
  gfx->setTextColor(SSD1306_WHITE);
  draw();
  gfx->display();
}

// --- text ---------------------------------------------------------
inline void fbText(int16_t x, int16_t y, const char* text) {
  if (gfx == nullptr) return;
  gfx->setTextColor(SSD1306_WHITE);
  gfx->setCursor(x, y);
  gfx->print(text);
}

// Text in an explicit color (SSD1306_BLACK for labels on a filled box).
inline void fbTextC(int16_t x, int16_t y, const char* text, uint16_t color) {
  if (gfx == nullptr) return;
  gfx->setTextColor(color);
  gfx->setCursor(x, y);
  gfx->print(text);
}

inline void fbTextRight(int16_t y, const char* text) {
  if (gfx == nullptr) return;
  int16_t bx, by;
  uint16_t bw, bh;
  gfx->getTextBounds(text, 0, y, &bx, &by, &bw, &bh);
  int16_t x = (int16_t)OLED_GFX_WIDTH - (int16_t)bw;
  if (x < 0) x = 0;
  gfx->setTextColor(SSD1306_WHITE);
  gfx->setCursor(x, y);
  gfx->print(text);
}

inline void fbTextCenter(int16_t y, const char* text) {
  if (gfx == nullptr) return;
  int16_t bx, by;
  uint16_t bw, bh;
  gfx->getTextBounds(text, 0, y, &bx, &by, &bw, &bh);
  int16_t x = ((int16_t)OLED_GFX_WIDTH - (int16_t)bw) / 2;
  if (x < 0) x = 0;
  gfx->setTextColor(SSD1306_WHITE);
  gfx->setCursor(x, y);
  gfx->print(text);
}

// --- shapes -------------------------------------------------------
inline void fbFillRect(int16_t x, int16_t y, int16_t w, int16_t h) {
  if (gfx) gfx->fillRect(x, y, w, h, SSD1306_WHITE);
}
inline void fbDrawRect(int16_t x, int16_t y, int16_t w, int16_t h) {
  if (gfx) gfx->drawRect(x, y, w, h, SSD1306_WHITE);
}
inline void fbFillCircle(int16_t x, int16_t y, int16_t r) {
  if (gfx) gfx->fillCircle(x, y, r, SSD1306_WHITE);
}
inline void fbDrawCircle(int16_t x, int16_t y, int16_t r) {
  if (gfx) gfx->drawCircle(x, y, r, SSD1306_WHITE);
}
// Right-pointing filled triangle (navigation cursor).
inline void fbTriRight(int16_t x, int16_t y, int16_t w, int16_t h) {
  if (gfx) gfx->fillTriangle(x, y, x, y + h, x + w, y + h / 2, SSD1306_WHITE);
}

// Horizontal progress bar (0-100%). Geometry matches the manual: full-screen
// whisking bar at x=4, y=30, width=120, height=8.
inline void fbProgressBar(int pct) {
  if (gfx == nullptr) return;
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  const int16_t x = 4, y = 30, w = 120, h = 8;
  gfx->drawRect(x, y, w, h, SSD1306_WHITE);
  int fill = (w * pct) / 100;
  if (fill > 0) gfx->fillRect(x, y, fill, h, SSD1306_WHITE);
}
