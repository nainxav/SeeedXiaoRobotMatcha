/*
 * Matcha Robot — Seeed Studio XIAO (RP2040 / RP2350) front panel
 *
 * Smart peripheral over USB serial (native USB CDC @ 115200):
 *   - sends semantic JSON events for the buttons, e.g.
 *       {"evt":"button","action":"start_bowl1"}
 *   - receives JSON state / commands from the host and renders the OLED UI
 *     on-device (idle / whisking / cleaning / memory screens).
 *
 * Pin map (XIAO silkscreen):
 *   Buttons   D0-D3 (start_bowl1, start_bowl2, stop, clean)
 *   OLED SPI  D10=MOSI(DIN), D9=SCK(CLK), D4=DC, D5=CS, D6=RES
 *             (SSD1306 128x64, software SPI)
 * All button inputs use INPUT_PULLUP: idle = HIGH, pressed/active = LOW.
 *
 * Board:    "Seeed XIAO RP2040" / "Seeed XIAO RP2350" (Arduino-Pico core)
 * Libraries: Adafruit GFX, Adafruit SSD1306
 */

// ---- XIAO silkscreen -> RP2040/RP2350 GPIO fallback ---------------
// Some board packages (or a wrong board selection) don't define the D0..D10
// silkscreen aliases. Fall back to the raw GPIO numbers, which the
// Arduino-Pico core (Earle Philhower) accepts directly.
#ifndef D0
#define D0  26  // A0
#define D1  27  // A1
#define D2  28  // A2
#define D3  29  // A3
#define D4   6  // SDA
#define D5   7  // SCL
#define D6   0  // TX
#define D7   1  // RX
#define D8   2  // SCK
#define D9   4  // MISO
#define D10  3  // MOSI
#endif

#include "oled_gfx.h"

// Buttons (active LOW, INPUT_PULLUP)
constexpr uint8_t PIN_BTN_BOWL1 = D0;
constexpr uint8_t PIN_BTN_BOWL2 = D1;
constexpr uint8_t PIN_BTN_STOP  = D2;
constexpr uint8_t PIN_BTN_CLEAN = D3;

// SSD1306 OLED (software SPI)
constexpr int8_t PIN_OLED_MOSI = D10;  // OLED DIN / SDA / D1
constexpr int8_t PIN_OLED_SCK  = D9;   // OLED CLK / SCK / D0
constexpr int8_t PIN_OLED_DC   = D4;   // OLED DC
constexpr int8_t PIN_OLED_CS   = D5;   // OLED CS
constexpr int8_t PIN_OLED_RST  = D6;   // OLED RES

constexpr unsigned long BAUD = 115200;
constexpr unsigned long HOST_TIMEOUT_MS = 5000;
constexpr unsigned long POLL_MS = 5;
constexpr unsigned long DISPLAY_MS = 100;
constexpr unsigned long DEBOUNCE_MS = 50;

constexpr uint16_t RX_MAX = 512;
constexpr uint8_t STR_MAX = 24;
constexpr uint8_t QUEUE_MAX = 8;

struct UiState {
  char current_state[STR_MAX] = "idle";
  char operation_status[STR_MAX] = "Idle";
  char navigation_option[STR_MAX] = "";
  char queue_display[QUEUE_MAX] = "";
  char status_banner[STR_MAX] = "";
  char clock[8] = "";          // "HH:MM" from host (optional)
  int bowl1_duration = 10;     // seconds
  int bowl2_duration = 10;
  int bowl1_pattern = 1;
  int bowl2_pattern = 1;
  bool nav_mode = false;
  bool edit_mode = false;
  bool memory_mode = false;
  int current_memory_slot = 1;
  bool mem_empty[3] = {true, true, true};  // slots 1..3 for "M: E E F"
  int slot_b1_dur = 0;         // current-slot preview (occupied memory)
  int slot_b1_pat = 0;
  int slot_b2_dur = 0;
  int slot_b2_pat = 0;
  int op_bowl = 0;
  int op_total = 0;
  int op_elapsed = 0;
  int op_percent = 0;
  int op_pattern = 0;
  unsigned long banner_until_ms = 0;
} state;

char rxBuf[RX_MAX];
uint16_t rxLen = 0;
unsigned long lastHostMs = 0;
unsigned long lastDisplayMs = 0;
unsigned long lastReadyMs = 0;
bool wasHostActive = false;
bool host_connected = false;

int btnLast[4] = {HIGH, HIGH, HIGH, HIGH};
unsigned long btnLastMs[4] = {0, 0, 0, 0};

// Full pattern names (used on the full whisking screen).
const char* patternName(int id) {
  switch (id) {
    case 1: return "Standard";
    case 2: return "Circular";
    case 3: return "Figure-8";
    default: return "Unknown";
  }
}

// Single-letter pattern codes (used on the bowl lines, per manual_images).
const char* patternCode(int id) {
  switch (id) {
    case 1: return "U";
    case 2: return "M";
    case 3: return "Z";
    default: return "?";
  }
}

// Format a seconds value as mm:ss.
void fmtMMSS(int seconds, char* out, size_t n) {
  if (seconds < 0) seconds = 0;
  snprintf(out, n, "%02d:%02d", seconds / 60, seconds % 60);
}

bool streq(const char* a, const char* b) {
  return strcmp(a, b) == 0;
}

bool startsWith(const char* s, const char* prefix) {
  return strncmp(s, prefix, strlen(prefix)) == 0;
}

void sendEvent(const char* evt, const char* action) {
  Serial.print(F("{\"evt\":\""));
  Serial.print(evt);
  Serial.print(F("\",\"action\":\""));
  Serial.print(action);
  Serial.println(F("\"}"));
}

void sendSys(const char* action) {
  Serial.print(F("{\"type\":\"sys\",\"action\":\""));
  Serial.print(action);
  Serial.println(F("\"}"));
}

void announceReady() {
  Serial.println(F("{\"type\":\"sys\",\"action\":\"ready\",\"version\":1}"));
}

bool hostActive() {
  return lastHostMs != 0 && (millis() - lastHostMs) < HOST_TIMEOUT_MS;
}

void showBanner(const char* text) {
  strncpy(state.status_banner, text, STR_MAX - 1);
  state.status_banner[STR_MAX - 1] = '\0';
  state.banner_until_ms = millis() + 2500;
}

void resetSystem(const char* reason) {
  strcpy(state.current_state, "idle");
  strcpy(state.operation_status, "Idle");
  state.nav_mode = false;
  state.memory_mode = false;
  state.current_memory_slot = 1;
  state.op_bowl = 0;
  state.op_total = 0;
  state.op_elapsed = 0;
  state.op_percent = 0;
  state.op_pattern = 0;

  if (streq(reason, "host_startup")) showBanner("HOST ONLINE");
  else if (streq(reason, "host_lost")) showBanner("HOST LOST");
  else if (streq(reason, "klipper_restart")) showBanner("KLIPPER RST");
  else if (streq(reason, "estop")) showBanner("E-STOP RST");
  else showBanner("SYS RESET");
}

bool extractStringField(const char* src, const char* key, char* out, uint8_t outLen) {
  char pat[20];
  snprintf(pat, sizeof(pat), "\"%s\":\"", key);
  const char* p = strstr(src, pat);
  if (!p) return false;
  p += strlen(pat);
  const char* end = strchr(p, '"');
  if (!end) return false;
  uint8_t n = end - p;
  if (n >= outLen) n = outLen - 1;
  strncpy(out, p, n);
  out[n] = '\0';
  return true;
}

bool extractIntField(const char* src, const char* key, int& out) {
  char pat[20];
  snprintf(pat, sizeof(pat), "\"%s\":", key);
  const char* p = strstr(src, pat);
  if (!p) return false;
  p += strlen(pat);
  while (*p == ' ') p++;
  out = atoi(p);
  return true;
}

bool extractBoolField(const char* src, const char* key, bool& out) {
  char pat[20];
  snprintf(pat, sizeof(pat), "\"%s\":", key);
  const char* p = strstr(src, pat);
  if (!p) return false;
  p += strlen(pat);
  while (*p == ' ') p++;
  if (strncmp(p, "true", 4) == 0) {
    out = true;
    return true;
  }
  if (strncmp(p, "false", 5) == 0) {
    out = false;
    return true;
  }
  return false;
}

void applyHostState(const char* line) {
  char tmp[STR_MAX];
  int v = 0;

  if (extractStringField(line, "current_state", state.current_state, STR_MAX)) {}
  if (extractStringField(line, "operation_status", state.operation_status, STR_MAX)) {}
  if (extractIntField(line, "bowl1_duration", v)) state.bowl1_duration = v;
  if (extractIntField(line, "bowl2_duration", v)) state.bowl2_duration = v;
  if (extractIntField(line, "bowl1_pattern", v)) state.bowl1_pattern = v;
  if (extractIntField(line, "bowl2_pattern", v)) state.bowl2_pattern = v;
  bool b = false;
  if (extractBoolField(line, "nav_mode", b)) state.nav_mode = b;
  if (extractBoolField(line, "edit_mode", b)) state.edit_mode = b;
  if (extractBoolField(line, "memory_mode", b)) state.memory_mode = b;
  if (extractIntField(line, "current_memory_slot", v)) state.current_memory_slot = v;
  if (extractStringField(line, "navigation_option", state.navigation_option, STR_MAX)) {}
  if (extractStringField(line, "clock", state.clock, sizeof(state.clock))) {}
  if (extractStringField(line, "status_banner", state.status_banner, STR_MAX)) {
    if (state.status_banner[0] != '\0') state.banner_until_ms = millis() + 2500;
  }

  const char* q = strstr(line, "\"queue\"");
  if (q) {
    if (extractStringField(q, "display", tmp, QUEUE_MAX)) {
      strncpy(state.queue_display, tmp, QUEUE_MAX - 1);
      state.queue_display[QUEUE_MAX - 1] = '\0';
    } else {
      state.queue_display[0] = '\0';
    }
  }

  const char* ms = strstr(line, "\"memory_slots\"");
  if (ms) {
    // Empty/full status for all three slots (drives "M: E E F").
    for (int i = 1; i <= 3; i++) {
      char slotKey[8];
      snprintf(slotKey, sizeof(slotKey), "slot%d", i);
      const char* sk = strstr(ms, slotKey);
      if (sk) {
        bool empty = true;
        if (extractBoolField(sk, "isEmpty", empty)) state.mem_empty[i - 1] = empty;
      }
    }
    // Preview values for the currently browsed slot (occupied memory screen).
    char slotKey[8];
    snprintf(slotKey, sizeof(slotKey), "slot%d", state.current_memory_slot);
    const char* sk = strstr(ms, slotKey);
    if (sk) {
      if (extractIntField(sk, "bowl1Duration", v)) state.slot_b1_dur = v;
      if (extractIntField(sk, "bowl1Pattern", v)) state.slot_b1_pat = v;
      if (extractIntField(sk, "bowl2Duration", v)) state.slot_b2_dur = v;
      if (extractIntField(sk, "bowl2Pattern", v)) state.slot_b2_pat = v;
    }
  }

  const char* op = strstr(line, "\"current_operation\"");
  if (op) {
    if (extractIntField(op, "bowl", v)) state.op_bowl = v;
    if (extractIntField(op, "total", v)) state.op_total = v;
    if (extractIntField(op, "elapsed", v)) state.op_elapsed = v;
    if (extractIntField(op, "percent", v)) state.op_percent = v;
    if (extractIntField(op, "pattern", v)) state.op_pattern = v;
  } else {
    state.op_bowl = 0;
    state.op_total = 0;
    state.op_elapsed = 0;
    state.op_percent = 0;
    state.op_pattern = 0;
  }

}

void handleCmd(const char* line) {
  char action[STR_MAX];
  char reason[STR_MAX];
  if (!extractStringField(line, "action", action, STR_MAX)) return;
  if (!extractStringField(line, "reason", reason, STR_MAX)) reason[0] = '\0';
  if (reason[0] == '\0') strcpy(reason, action);

  if (streq(action, "ping")) {
    sendSys("pong");
    lastHostMs = millis();
    return;
  }

  if (streq(action, "host_startup")) {
    if (!host_connected) {
      resetSystem("host_startup");
    } else {
      showBanner("HOST ONLINE");
    }
    sendSys("reset_done");
    host_connected = true;
    lastHostMs = millis();
    return;
  }

  if (streq(action, "system_reset") || streq(action, "klipper_restart") ||
      streq(action, "host_shutdown") || streq(action, "estop")) {
    resetSystem(streq(action, "system_reset") ? reason : action);
    sendSys("reset_done");
    host_connected = true;
    lastHostMs = millis();
  }
}

void ingestLine(const char* line) {
  if (strstr(line, "\"type\":\"cmd\"")) {
    handleCmd(line);
    return;
  }
  if (strstr(line, "\"type\":\"state\"") || strstr(line, "\"current_state\"")) {
    applyHostState(line);
    host_connected = true;
    lastHostMs = millis();
  }
}

void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rxBuf[rxLen] = '\0';
        ingestLine(rxBuf);
        rxLen = 0;
      }
    } else if (rxLen < RX_MAX - 1) {
      rxBuf[rxLen++] = c;
    } else {
      rxLen = 0;
    }
  }
}

void pollButtons() {
  const uint8_t pins[4] = {PIN_BTN_BOWL1, PIN_BTN_BOWL2, PIN_BTN_STOP, PIN_BTN_CLEAN};
  const char* actions[4] = {"start_bowl1", "start_bowl2", "stop", "clean"};
  unsigned long now = millis();

  for (int i = 0; i < 4; i++) {
    int cur = digitalRead(pins[i]);
    if (cur == LOW && btnLast[i] == HIGH && (now - btnLastMs[i] > DEBOUNCE_MS)) {
      sendEvent("button", actions[i]);
      btnLastMs[i] = now;
    }
    btnLast[i] = cur;
  }
}

// --- Dashboard screens (layout from manual_images/*.svg) ---

void toUpperCopy(char* dst, const char* src, uint8_t cap) {
  uint8_t i = 0;
  for (; i + 1 < cap && src[i]; i++) {
    char c = src[i];
    if (c >= 'a' && c <= 'z') c = c - 32;
    dst[i] = c;
  }
  dst[i] = '\0';
}

bool isWhiskingState() {
  return startsWith(state.current_state, "whisking");
}

void drawQueueOverlay() {
  if (state.queue_display[0] != '\0') fbTextRight(0, state.queue_display);
}

// One bowl row: [A] label box, centered mm:ss, single-letter pattern code,
// and the navigation/edit cursor when this bowl's field is selected.
void drawBowlLine(int bowlNum, int boxTop, int durSec, int pattern) {
  fbFillRect(0, boxTop, 10, 10);
  char lbl[2] = { (char)(bowlNum == 1 ? 'A' : 'B'), 0 };
  fbTextC(2, boxTop + 1, lbl, SSD1306_BLACK);

  char t[8];
  fmtMMSS(durSec, t, sizeof(t));
  fbText(49, boxTop + 1, t);                       // centered mm:ss
  fbText(120, boxTop + 1, patternCode(pattern));   // pattern code (right)

  char durOpt[16], patOpt[16];
  snprintf(durOpt, sizeof(durOpt), "bowl%d_duration", bowlNum);
  snprintf(patOpt, sizeof(patOpt), "bowl%d_pattern", bowlNum);
  if (state.nav_mode || state.edit_mode) {
    if (streq(state.navigation_option, durOpt)) {
      if (state.edit_mode) fbFillRect(42, boxTop + 4, 6, 2);  // dash (edit)
      else fbTriRight(42, boxTop + 2, 6, 6);                  // triangle (nav)
    }
    if (streq(state.navigation_option, patOpt)) {
      if (state.edit_mode) fbFillRect(108, boxTop + 4, 6, 2);
      else fbTriRight(108, boxTop + 2, 6, 6);
    }
  }
}

// Idle / navigation / edit all use this 5-row main screen (manual 01/04/05/06/13).
void drawMainScreen() {
  fbFillCircle(3, 3, 3);
  char st[STR_MAX];
  toUpperCopy(st, state.current_state, STR_MAX);
  fbText(9, 0, st);

  drawBowlLine(1, 12, state.bowl1_duration, state.bowl1_pattern);
  drawBowlLine(2, 24, state.bowl2_duration, state.bowl2_pattern);

  char mem[16];
  snprintf(mem, sizeof(mem), "M: %c %c %c",
           state.mem_empty[0] ? 'E' : 'F',
           state.mem_empty[1] ? 'E' : 'F',
           state.mem_empty[2] ? 'E' : 'F');
  int memX = 2;
  if ((state.nav_mode || state.edit_mode) && streq(state.navigation_option, "memory")) {
    fbTriRight(0, 38, 6, 6);
    memX = 9;
  }
  fbText(memX, 37, mem);

  fbText(2, 50, state.clock[0] ? state.clock : "--:--");
  const char* conn = hostActive() ? "ON" : "OFF";
  int connX = (int)OLED_GFX_WIDTH - (int)strlen(conn) * 6 - 2;
  fbText(connX, 50, conn);
}

// Full whisking screen (manual 15/16/17).
void drawWhiskingScreen() {
  char title[STR_MAX];
  if (state.operation_status[0] && !streq(state.operation_status, "Idle")) {
    toUpperCopy(title, state.operation_status, STR_MAX);
  } else if (state.op_bowl > 0) {
    snprintf(title, sizeof(title), "WHISKING BOWL %d", state.op_bowl);
  } else if (streq(state.current_state, "whisking_bowl1")) {
    strcpy(title, "WHISKING BOWL 1");
  } else if (streq(state.current_state, "whisking_bowl2")) {
    strcpy(title, "WHISKING BOWL 2");
  } else {
    strcpy(title, "WHISKING");
  }
  fbText(2, 2, title);

  char line[STR_MAX];
  snprintf(line, sizeof(line), "Time: %d/%ds", state.op_elapsed, state.op_total);
  fbText(2, 15, line);
  fbProgressBar(state.op_percent);
  snprintf(line, sizeof(line), "%d%%", state.op_percent);
  fbText(2, 44, line);
  fbText(60, 44, patternName(state.op_pattern > 0 ? state.op_pattern : 1));
}

// Cleaning screen (manual 10).
void drawCleaningScreen() {
  fbFillCircle(3, 4, 3);
  char title[STR_MAX];
  if (strstr(state.operation_status, "Cleaning") || strstr(state.operation_status, "cleaning")) {
    toUpperCopy(title, state.operation_status, STR_MAX);
  } else {
    strcpy(title, "CLEANING");
  }
  fbText(9, 1, title);
  fbText(2, 20, "Moving to");
  fbText(2, 34, "clean position");
}

// Memory browse screen (manual 07/08/09). Slot 4 = Quit to dashboard.
void drawMemoryScreen() {
  if (state.current_memory_slot == 4) {
    fbText(2, 1, "QUIT TO DASHBOARD");
    fbText(2, 18, "Click to exit");
    fbText(2, 30, "without changes");
    return;
  }

  char h[STR_MAX];
  snprintf(h, sizeof(h), "MEMORY SLOT %d", state.current_memory_slot);
  fbText(2, 1, h);

  int idx = (state.current_memory_slot >= 1 && state.current_memory_slot <= 3)
              ? state.current_memory_slot - 1 : 0;
  if (state.mem_empty[idx]) {
    fbText(2, 16, "EMPTY");
    fbText(2, 30, "Press to save");
    fbText(2, 50, "Rotate to change slot");
  } else {
    fbText(2, 16, "OCCUPIED");
    char p[40];
    snprintf(p, sizeof(p), "B1:%ds P%d | B2:%ds P%d",
             state.slot_b1_dur, state.slot_b1_pat, state.slot_b2_dur, state.slot_b2_pat);
    fbText(2, 28, p);
    fbText(2, 40, "Press to load/erase");
    fbText(2, 52, "Hold to erase");
  }
}

// System banner (manual 11 restarting / host online / host lost, etc).
void drawBannerScreen() {
  fbTextCenter(16, "SYSTEM");
  fbTextCenter(32, state.status_banner);
}

void drawFrame() {
  if (state.status_banner[0] != '\0') {
    drawBannerScreen();
    return;
  }

  if (isWhiskingState()) {
    drawWhiskingScreen();
  } else if (streq(state.current_state, "cleaning")) {
    drawCleaningScreen();
  } else if (state.memory_mode) {
    drawMemoryScreen();
  } else {
    drawMainScreen();  // idle + navigation + edit
  }

  drawQueueOverlay();
}

void renderDisplay() {
  if (state.banner_until_ms > 0 && (long)(millis() - state.banner_until_ms) > 0) {
    state.status_banner[0] = '\0';
    state.banner_until_ms = 0;
  }

  oledDrawFrame(drawFrame);
}

void setup() {
  Serial.begin(BAUD);

  pinMode(PIN_BTN_BOWL1, INPUT_PULLUP);
  pinMode(PIN_BTN_BOWL2, INPUT_PULLUP);
  pinMode(PIN_BTN_STOP, INPUT_PULLUP);
  pinMode(PIN_BTN_CLEAN, INPUT_PULLUP);

  oledBegin(PIN_OLED_MOSI, PIN_OLED_SCK, PIN_OLED_DC, PIN_OLED_CS, PIN_OLED_RST);

  announceReady();
}

void loop() {
  pollButtons();
  pollSerial();

  bool active = hostActive();
  if (wasHostActive && !active) {
    resetSystem("host_lost");
    host_connected = false;
  }
  wasHostActive = active;

  unsigned long now = millis();
  if (!host_connected && now - lastReadyMs >= 3000) {
    announceReady();
    lastReadyMs = now;
  }
  if (now - lastDisplayMs >= DISPLAY_MS) {
    renderDisplay();
    lastDisplayMs = now;
  }

  delay(POLL_MS);
}
