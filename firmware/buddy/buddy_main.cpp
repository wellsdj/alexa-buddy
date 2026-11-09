// Buddy desk unit firmware for ESP32-S3-DevKitC-1 + INMP441 mic + MAX98357A amp.
//
// The board is the ears, mouth and alarm clock; the thinking happens on the Buddy server
// (your laptop). It streams the microphone to the server over Wi-Fi, plays back whatever
// the server says, and keeps a copy of your alarms so they ring even if the server is off.
//
// Arduino IDE: install "esp32 by Espressif" 3.x, plus the libraries WebSockets (Markus
// Sattler), WiFiManager (tzapu) and ArduinoJson (Benoit Blanchon). Board: "ESP32S3 Dev Module".
// See buddy/README.md for wiring and setup.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <SD.h>
#include <SPI.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <driver/i2s_std.h>
#include <freertos/stream_buffer.h>
#include <math.h>
#include <sys/time.h>

#include "config.h"

#define FW_VERSION "1.5.0"
#define SAMPLE_RATE 16000
#define MIC_FRAMES 320               // 20 ms per I2S read
#define SEND_BYTES 1280              // 40 ms of 16-bit audio per WebSocket frame
#define MIC_BUFFER_BYTES (16 * 1024) // 0.5 s
#define SPK_BUFFER_BYTES (96 * 1024) // 3 s of speech, 1.5 s of music; the server stays ~0.6 s ahead
#define SPK_BUFFER_MIN (48 * 1024)  // if memory is short
#define MAX_FIRES 64

// ------------------------------------------------------------------ state
static i2s_chan_handle_t rxChan = nullptr, txChan = nullptr;
static StreamBufferHandle_t micStream, spkStream;
static WebSocketsClient ws;
static Preferences prefs;

enum Kind : uint8_t { K_ALARM = 0, K_TIMER = 1, K_REMINDER = 2 };
struct Fire { char id[8]; uint32_t at; uint8_t kind; };
static Fire fires[MAX_FIRES];
static int fireCount = 0;
static Fire firedRecently[16];
static int firedIdx = 0;

static volatile int acceptSeq = -1;
static volatile uint32_t seqBytes = 0;
static volatile bool streamEnded = false;
static volatile bool flushSpeaker = false;
static volatile uint32_t lastStreamMs = 0;  // when sound last arrived from the server (an alarm sound, say)
static volatile uint32_t spkRate = SAMPLE_RATE;   // speaker rate now: 16 kHz speech, 32 kHz music
static volatile uint32_t wantRate = SAMPLE_RATE;  // rate the next stream needs
static volatile bool playbackDone = false;
static volatile uint8_t volumeLevel = 6;
static volatile int micShift = MIC_SHIFT_DEFAULT;
static volatile bool micMuted = false;
static volatile float speakLevel = 0.0f;
static volatile uint32_t droppedSpk = 0, droppedMic = 0;

struct Note { uint16_t freq; uint16_t ms; };
static portMUX_TYPE toneMux = portMUX_INITIALIZER_UNLOCKED;
static Note toneQueue[24];
static volatile int toneLen = 0;
enum RingMode : uint8_t { RING_NONE, RING_ALARM, RING_TIMER };
static volatile RingMode ringMode = RING_NONE;
static char ringId[8] = "";
static uint8_t ringKind = K_ALARM;
static uint32_t ringStartMs = 0;

// ---- lesson recorder state
static SPIClass sdSpi(FSPI);
static bool sdOk = false;
static StreamBufferHandle_t recStream = nullptr;
static File recFile;
static volatile bool recording = false;
static volatile uint8_t recCmd = 0;          // 1 = start, 2 = stop (handled by recTask, which owns the SD card while recording)
static uint32_t recEpoch = 0, recNum = 0, recStartMs = 0;
static volatile uint32_t recBytes = 0, droppedRec = 0;
static volatile uint32_t recErrorUntil = 0;  // quick red flicker when a recording can't start or the card fails
static volatile uint8_t holdStage = 0;       // while the button is held: 2 = release to record, 3 = release to mute
static volatile bool uploading = false;      // a recording is on its way to the Mac
static volatile bool uploadSdBusy = false;   // the upload task is reading the SD card
static volatile bool uploadAbort = false;
static volatile bool canUpload = false;      // set by loop(): connected to Buddy and idle
static uint32_t pendingNum[8], pendingStartMs[8];  // recordings made with no clock yet (booted away from Wi-Fi)

static String cfgHost, cfgToken, resolvedHost;
static uint16_t cfgPort = DEFAULT_SERVER_PORT, resolvedPort = DEFAULT_SERVER_PORT;
static bool wsConnected = false;
static uint32_t lastConnected = 0, lastConnectAttempt = 0;
static String serverState = "offline";
static bool shouldSaveConfig = false;

// ------------------------------------------------------------------ small helpers
static bool timeValid() { return time(nullptr) > 1700000000; }

static void sendJson(JsonDocument &doc) {
  if (!wsConnected) return;
  String out;
  serializeJson(doc, out);
  ws.sendTXT(out);
}

// Empty the speaker buffer and wait until the play task has actually done it, so audio that
// arrives right after (a new reply) is not thrown away with the old.
static void flushSpeakerNow() {
  flushSpeaker = true;
  uint32_t start = millis();
  while (flushSpeaker && millis() - start < 150) delay(1);
}

static void queueNotes(const Note *notes, int n, bool replace) {
  portENTER_CRITICAL(&toneMux);
  if (replace) toneLen = 0;
  for (int i = 0; i < n && toneLen < 24; i++) toneQueue[toneLen++] = notes[i];
  portEXIT_CRITICAL(&toneMux);
}

static void earcon(const char *name) {
  static const Note wake[] = {{660, 90}, {0, 10}, {990, 140}};
  static const Note end[] = {{880, 70}, {0, 10}, {660, 100}};
  static const Note nospeech[] = {{520, 120}, {0, 10}, {390, 180}};
  static const Note error[] = {{220, 120}, {0, 60}, {220, 120}};
  static const Note chime[] = {{988, 140}, {0, 20}, {1319, 340}, {0, 300}, {988, 140}, {0, 20}, {1319, 340}};
  static const Note hello[] = {{523, 90}, {659, 90}, {784, 160}};
  if (!strcmp(name, "wake")) queueNotes(wake, 3, true);
  else if (!strcmp(name, "end")) queueNotes(end, 3, true);
  else if (!strcmp(name, "nospeech")) queueNotes(nospeech, 3, true);
  else if (!strcmp(name, "error")) queueNotes(error, 3, true);
  else if (!strcmp(name, "chime")) queueNotes(chime, 7, true);
  else if (!strcmp(name, "hello")) queueNotes(hello, 3, true);
}

// Volume 0-10 on a perceptual curve: 10 = full, each step ~3 dB.
static float speechGain() {
  uint8_t v = volumeLevel;
  if (v == 0) return 0.0f;
  return powf(10.0f, (v - 10) * 3.0f / 20.0f);
}

// ------------------------------------------------------------------ audio tasks
static void micTask(void *) {
  static int32_t raw[MIC_FRAMES * 2];
  static int16_t out[MIC_FRAMES];
  float hpX = 0, hpY = 0;
  int channel = -1;  // auto-detect which slot the INMP441 is on
  uint64_t sums[2] = {0, 0};
  int blocks = 0;
  for (;;) {
    size_t got = 0;
    if (i2s_channel_read(rxChan, raw, sizeof(raw), &got, portMAX_DELAY) != ESP_OK) continue;
    int frames = got / 8;
    if (channel < 0) {
      for (int i = 0; i < frames; i++) {
        sums[0] += abs(raw[2 * i] >> 16);
        sums[1] += abs(raw[2 * i + 1] >> 16);
      }
      if (++blocks >= 50) {
        channel = sums[1] > sums[0] ? 1 : 0;
        Serial.printf("[mic] using %s channel (L=%llu R=%llu)\n", channel ? "right" : "left", sums[0], sums[1]);
      }
    }
    int c = channel < 0 ? 0 : channel;
    int shift = micShift;
    for (int i = 0; i < frames; i++) {
      float x = (float)(raw[2 * i + c] >> shift);
      float y = x - hpX + 0.995f * hpY;  // remove the mic's DC offset
      hpX = x;
      hpY = y;
      out[i] = (int16_t)constrain((int32_t)y, -32768, 32767);
    }
    if (recording) {  // a lesson: straight to the SD card, not to the server
      if (xStreamBufferSend(recStream, out, frames * 2, 0) < (size_t)frames * 2) droppedRec++;
    } else if (!micMuted && xStreamBufferSend(micStream, out, frames * 2, 0) < (size_t)frames * 2) {
      droppedMic++;
    }
  }
}

static int renderTone(int16_t *out, int maxSamples, float &gainOut) {
  static float phase = 0;
  static uint32_t pos = 0;
  Note n;
  portENTER_CRITICAL(&toneMux);
  bool have = toneLen > 0;
  if (have) n = toneQueue[0];
  portEXIT_CRITICAL(&toneMux);
  if (!have) {
    pos = 0;
    return 0;
  }
  const uint32_t rate = spkRate;
  uint32_t total = (uint32_t)n.ms * rate / 1000;
  int count = min<int>(maxSamples, total - pos);
  const uint32_t attack = rate / 250, release = rate / 40;
  for (int i = 0; i < count; i++, pos++) {
    float env = 1.0f;
    if (pos < attack) env = (float)pos / attack;
    if (total - pos < release) env = min(env, (float)(total - pos) / release);
    float s = 0;
    if (n.freq) {
      s = sinf(phase) * 0.8f + sinf(phase * 2.0f) * 0.12f;  // a touch of warmth
      phase += 2.0f * PI * n.freq / rate;
      if (phase > 2.0f * PI) phase -= 2.0f * PI;
    }
    out[i] = (int16_t)(s * env * 26000.0f);
  }
  if (pos >= total) {
    pos = 0;
    portENTER_CRITICAL(&toneMux);
    for (int i = 1; i < toneLen; i++) toneQueue[i - 1] = toneQueue[i];
    if (toneLen > 0) toneLen--;
    portEXIT_CRITICAL(&toneMux);
  }
  if (ringMode != RING_NONE) {
    // Alarms start gentle and get louder over 30 s, and never go quieter than half volume.
    float ramp = min(1.0f, 0.3f + (millis() - ringStartMs) / 30000.0f * 0.7f);
    gainOut = max(0.5f, speechGain()) * ramp;
  } else {
    gainOut = max(0.2f, speechGain());
  }
  return count;
}

static void playTask(void *) {
  static int16_t mono[256];
  static int16_t stereo[512];
  bool playing = false;
  for (;;) {
    if (wantRate != spkRate) {
      // A stream at another rate (music vs speech): switch the speaker clock between buffers.
      i2s_channel_disable(txChan);
      i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(wantRate);
      i2s_channel_reconfig_std_clock(txChan, &clk);
      i2s_channel_enable(txChan);
      spkRate = wantRate;
    }
    if (flushSpeaker) {
      xStreamBufferReset(spkStream);
      flushSpeaker = false;
      playing = false;
    }
    // While your own alarm sound is arriving from the server, it replaces the beeps.
    if (ringMode != RING_NONE && toneLen == 0 && millis() - lastStreamMs > 2500) {
      static const Note alarmPattern[] = {{784, 160}, {0, 40}, {988, 160}, {0, 40}, {1175, 300}, {0, 700}};
      static const Note timerPattern[] = {{1047, 110}, {0, 90}, {1047, 110}, {0, 90}, {1047, 110}, {0, 90}, {1047, 110}, {0, 600}};
      if (ringMode == RING_ALARM) queueNotes(alarmPattern, 6, false);
      else queueNotes(timerPattern, 8, false);
    }
    float gain = 1.0f;
    int n = renderTone(mono, 256, gain);
    bool tone = n > 0;
    if (!tone) {
      n = xStreamBufferReceive(spkStream, mono, sizeof(mono), pdMS_TO_TICKS(20)) / 2;
      gain = speechGain();
      if (ringMode != RING_NONE) {  // an alarm sound: at least half volume, getting louder like the beeps
        float ramp = min(1.0f, 0.3f + (millis() - ringStartMs) / 30000.0f * 0.7f);
        gain = max(0.5f, gain) * ramp;
      }
      if (n == 0) {
        if (playing && streamEnded && xStreamBufferIsEmpty(spkStream)) {
          playing = false;
          streamEnded = false;
          playbackDone = true;
        }
        speakLevel *= 0.8f;
        continue;
      }
      playing = true;
    }
    int peak = 0;
    for (int i = 0; i < n; i++) {
      int32_t s = (int32_t)(mono[i] * gain);
      s = constrain(s, -32768, 32767);
      stereo[2 * i] = stereo[2 * i + 1] = (int16_t)s;
      peak = max(peak, abs((int)s));
    }
    speakLevel = speakLevel * 0.7f + (peak / 32768.0f) * 0.3f;
    size_t written;
    i2s_channel_write(txChan, stereo, n * 4, &written, portMAX_DELAY);
  }
}

static void setupI2S() {
  // Microphone on I2S0: 32-bit stereo frames, we pick whichever slot the mic is on.
  i2s_chan_config_t rxCfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  rxCfg.dma_desc_num = 6;
  rxCfg.dma_frame_num = MIC_FRAMES;
  ESP_ERROR_CHECK(i2s_new_channel(&rxCfg, nullptr, &rxChan));
  i2s_std_config_t rxStd = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
      .gpio_cfg = {
          .mclk = I2S_GPIO_UNUSED,
          .bclk = (gpio_num_t)MIC_SCK_PIN,
          .ws = (gpio_num_t)MIC_WS_PIN,
          .dout = I2S_GPIO_UNUSED,
          .din = (gpio_num_t)MIC_SD_PIN,
          .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
      },
  };
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(rxChan, &rxStd));
  ESP_ERROR_CHECK(i2s_channel_enable(rxChan));

  // Amplifier on I2S1: 16-bit stereo, the same sample on both slots.
  i2s_chan_config_t txCfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  txCfg.dma_desc_num = 8;
  txCfg.dma_frame_num = 256;
  txCfg.auto_clear = true;  // silence instead of stutter if we ever run dry
  ESP_ERROR_CHECK(i2s_new_channel(&txCfg, &txChan, nullptr));
  i2s_std_config_t txStd = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
      .gpio_cfg = {
          .mclk = I2S_GPIO_UNUSED,
          .bclk = (gpio_num_t)AMP_BCLK_PIN,
          .ws = (gpio_num_t)AMP_LRC_PIN,
          .dout = (gpio_num_t)AMP_DIN_PIN,
          .din = I2S_GPIO_UNUSED,
          .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
      },
  };
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(txChan, &txStd));
  ESP_ERROR_CHECK(i2s_channel_enable(txChan));
}

// ------------------------------------------------------------------ status LED
static void led(uint8_t r, uint8_t g, uint8_t b) {
#if STATUS_LED_PIN >= 0
  static uint32_t last = 0xFFFFFFFF;
  uint32_t v = ((uint32_t)r << 16) | ((uint32_t)g << 8) | b;
  if (v == last) return;
  last = v;
  rgbLedWrite(STATUS_LED_PIN, (r * LED_BRIGHTNESS) / 255, (g * LED_BRIGHTNESS) / 255, (b * LED_BRIGHTNESS) / 255);
#if defined(STATUS_LED_PIN_ALT) && STATUS_LED_PIN_ALT >= 0
  rgbLedWrite(STATUS_LED_PIN_ALT, (r * LED_BRIGHTNESS) / 255, (g * LED_BRIGHTNESS) / 255, (b * LED_BRIGHTNESS) / 255);
#endif
#endif
}

static void updateLed() {
  uint32_t t = millis();
  float breath = (sinf(t / 1000.0f * 2.0f * PI * 0.6f) + 1.0f) * 0.5f;  // 0..1, ~0.6 Hz
  if (holdStage == 2) {  // button held for 2 s: let go to start / stop a lesson recording
    led(200, 0, 200);
  } else if (holdStage == 3) {  // held for 4 s: let go to mute
    led(180, 0, 0);
  } else if (t < recErrorUntil) {  // couldn't record (no card, card full)
    led(((t / 120) % 2) ? 255 : 0, 0, 0);
  } else if (ringMode != RING_NONE) {
    bool on = (t / 250) % 2;
    led(on ? 255 : 40, on ? 150 : 20, 0);
  } else if (recording) {  // recording a lesson: a slow red pulse
    led((uint8_t)(70 + 185 * breath), 0, 0);
  } else if (uploading) {  // sending a lesson home: teal
    led(0, (uint8_t)(60 + 120 * breath), (uint8_t)(60 + 120 * breath));
  } else if (micMuted) {
    led(180, 0, 0);
  } else if (WiFi.status() != WL_CONNECTED || !wsConnected) {
    led(((t / 1000) % 3 == 0) ? 90 : 0, 0, 0);  // dim red blink: can't reach the server
  } else if (serverState == "listening") {
    led(0, 90, 255);
  } else if (serverState == "thinking") {
    uint8_t v = 60 + (uint8_t)(breath * 195);
    led(v * 0.55f, 0, v);
  } else if (serverState == "speaking") {
    uint8_t v = 40 + (uint8_t)min(215.0f, speakLevel * 900.0f);  // pulses with the voice
    led(0, v, v * 0.45f);
  } else {
    led(0, 0, 0);
  }
}

// ------------------------------------------------------------------ LED strip behind the grille
// Same moods as the little light, as animations along the strip, fading smoothly between them.
static uint16_t stripCount = STRIP_DEFAULT_LEDS;
static uint8_t stripBrightness = 70;  // 0-255, set from the web page
#if STRIP_PIN >= 0
static rmt_data_t stripBits[STRIP_MAX_LEDS * 24];
static float stripNow[STRIP_MAX_LEDS][3];  // what's showing (0..1), eased towards the target
static bool stripReady = false;

static void stripInit() {
  stripReady = rmtInit(STRIP_PIN, RMT_TX_MODE, RMT_MEM_NUM_BLOCKS_1, 10000000);  // 0.1 us ticks
  Serial.printf("[strip] %s on GPIO %d, %u LEDs\n", stripReady ? "ready" : "FAILED", STRIP_PIN, stripCount);
}

static void stripShow() {
  if (!stripReady) return;
  size_t i = 0;
  float scale = stripBrightness / 255.0f;
  for (uint16_t n = 0; n < stripCount; n++) {
    // WS2812 wants green, red, blue, most significant bit first. Squaring keeps fades smooth to the eye.
    float g = stripNow[n][1], r = stripNow[n][0], b = stripNow[n][2];
    uint8_t grb[3] = {(uint8_t)(g * g * scale * 255), (uint8_t)(r * r * scale * 255), (uint8_t)(b * b * scale * 255)};
    for (uint8_t c = 0; c < 3; c++) {
      for (int8_t bit = 7; bit >= 0; bit--) {
        bool one = grb[c] & (1 << bit);
        stripBits[i].level0 = 1;
        stripBits[i].duration0 = one ? 8 : 4;  // high 0.8 us for a 1, 0.4 us for a 0
        stripBits[i].level1 = 0;
        stripBits[i].duration1 = one ? 4 : 8;
        i++;
      }
    }
  }
  rmtWrite(STRIP_PIN, stripBits, i, 20);
}

static void stripUpdate() {
  static uint32_t last = 0;
  uint32_t t = millis();
  if (t - last < 25) return;  // 40 frames a second
  last = t;
  uint16_t n = stripCount;
  float mid = (n - 1) / 2.0f;
  float want[STRIP_MAX_LEDS][3];
  float sec = t / 1000.0f;
  for (uint16_t k = 0; k < n; k++) {
    float r = 0, g = 0, b = 0;
    if (holdStage == 2) {  // button held: release to start / stop a lesson recording
      r = 0.8f; b = 0.8f;
    } else if (holdStage == 3) {  // held longer: release to mute
      r = 0.6f;
    } else if (t < recErrorUntil) {  // couldn't record
      r = ((t / 120) % 2) ? 0.8f : 0;
    } else if (ringMode != RING_NONE) {  // alarm: amber, pulsing
      float v = 0.35f + 0.65f * ((sinf(sec * 2 * PI * 2.0f) + 1) * 0.5f);
      r = v; g = v * 0.45f;
    } else if (recording) {  // recording a lesson: a quiet red pulse
      r = 0.12f + 0.2f * ((sinf(sec * 2 * PI * 0.5f) + 1) * 0.5f);
    } else if (uploading) {  // sending a lesson home: teal, breathing
      float v = 0.25f + 0.3f * ((sinf(sec * 2 * PI * 0.6f + k * 0.2f) + 1) * 0.5f);
      g = v; b = v;
    } else if (micMuted) {  // muted: a quiet red
      r = 0.35f;
    } else if (WiFi.status() != WL_CONNECTED || !wsConnected) {  // can't reach Buddy: slow red blink
      r = (fabsf(k - mid) < 1.0f && (t / 1000) % 3 == 0) ? 0.5f : 0;
    } else if (serverState == "listening") {  // listening: blue, gently breathing
      float v = 0.75f + 0.25f * sinf(sec * 2 * PI * 0.7f + k * 0.25f);
      r = 0.05f * v; g = 0.35f * v; b = v;
    } else if (serverState == "thinking") {  // thinking: purple glow with a bright dot sweeping
      float pos = (sinf(sec * 2 * PI * 0.45f) + 1) * 0.5f * (n - 1);
      float d = fabsf(k - pos);
      float v = 0.22f + 0.78f * max(0.0f, 1.0f - d / 2.5f);
      r = 0.6f * v; b = v;
    } else if (serverState == "speaking") {  // speaking: green, opening out from the middle with the voice
      float reach = min(1.0f, 0.15f + speakLevel * 6.0f) * (mid + 1);
      float v = (fabsf(k - mid) <= reach) ? 1.0f : 0.12f;
      g = v; b = v * 0.45f;
    }
    want[k][0] = r; want[k][1] = g; want[k][2] = b;
  }
  for (uint16_t k = 0; k < n; k++)
    for (uint8_t c = 0; c < 3; c++) stripNow[k][c] += (want[k][c] - stripNow[k][c]) * 0.25f;  // smooth
  stripShow();
}

static void stripSet(int count, int brightness) {
  uint16_t old = stripCount;
  if (count >= 1) stripCount = min(count, STRIP_MAX_LEDS);
  if (brightness >= 0) stripBrightness = min(brightness, 255);
  if (stripCount < old) {  // switch off the LEDs no longer used
    uint16_t keep = stripCount;
    stripCount = old;
    for (uint16_t k = keep; k < old; k++) stripNow[k][0] = stripNow[k][1] = stripNow[k][2] = 0;
    stripShow();
    stripCount = keep;
  }
  prefs.putUShort("stripLeds", stripCount);
  prefs.putUChar("stripBri", stripBrightness);
}
#else
static void stripInit() {}
static void stripUpdate() {}
static void stripSet(int, int) {}
#endif

// ------------------------------------------------------------------ alarms kept on the device
static void saveFires() {
  prefs.putBytes("fires", fires, sizeof(Fire) * fireCount);
  prefs.putInt("fireN", fireCount);
}

static void loadFires() {
  fireCount = constrain(prefs.getInt("fireN", 0), 0, MAX_FIRES);
  if (fireCount) prefs.getBytes("fires", fires, sizeof(Fire) * fireCount);
  Serial.printf("[alarms] %d stored fire times\n", fireCount);
}

static bool alreadyFired(const Fire &f) {
  for (auto &r : firedRecently)
    if (r.at == f.at && !strncmp(r.id, f.id, sizeof(r.id))) return true;
  return false;
}

static void stopRinging(const char *reason) {
  if (ringMode == RING_NONE) return;
  ringMode = RING_NONE;
  portENTER_CRITICAL(&toneMux);
  toneLen = 0;
  portEXIT_CRITICAL(&toneMux);
  JsonDocument doc;
  doc["type"] = "alarm_stopped";
  doc["id"] = ringId;
  doc["reason"] = reason;
  sendJson(doc);
  Serial.printf("[alarms] stopped (%s)\n", reason);
}

static void checkAlarms() {
  static uint32_t lastCheck = 0;
  if (millis() - lastCheck < 250) return;
  lastCheck = millis();
  if (ringMode != RING_NONE) {
    uint32_t limit = ringKind == K_TIMER ? 3 * 60000 : 30000;  // alarms give up after 30 s (missed)
    if (millis() - ringStartMs > limit) stopRinging("timeout");
  }
  if (!timeValid()) return;
  uint32_t now = (uint32_t)time(nullptr);
  for (int i = 0; i < fireCount; i++) {
    Fire &f = fires[i];
    if (now < f.at || now - f.at > 90 || alreadyFired(f)) continue;
    firedRecently[firedIdx] = f;
    firedIdx = (firedIdx + 1) % 16;
    Serial.printf("[alarms] firing %s kind=%d\n", f.id, f.kind);
    JsonDocument doc;
    doc["type"] = "alarm_fired";
    doc["id"] = f.id;
    doc["kind"] = f.kind == K_TIMER ? "timer" : f.kind == K_REMINDER ? "reminder" : "alarm";
    sendJson(doc);
    if (f.kind == K_REMINDER) {
      earcon("chime");  // the server then speaks the reminder text
    } else {
      flushSpeaker = true;
      acceptSeq = -1;
      strncpy(ringId, f.id, sizeof(ringId));
      ringKind = f.kind;
      ringStartMs = millis();
      ringMode = f.kind == K_TIMER ? RING_TIMER : RING_ALARM;
    }
  }
}

// ------------------------------------------------------------------ server messages
static void handleJson(uint8_t *payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char *type = doc["type"] | "";

  if (doc["server_time"].is<uint32_t>() && !timeValid()) {
    struct timeval tv = {(time_t)doc["server_time"].as<uint32_t>(), 0};
    settimeofday(&tv, nullptr);  // NTP may be blocked; the server's clock is good enough
    Serial.println("[time] set from server");
  }
  if (!strcmp(type, "hello_ack")) {
    if (doc["volume"].is<int>()) volumeLevel = constrain(doc["volume"].as<int>(), 0, 10);
    if (doc["mic_shift"].is<int>()) micShift = constrain(doc["mic_shift"].as<int>(), 8, 18);
    if (doc["strip_leds"].is<int>()) stripSet(doc["strip_leds"].as<int>(), doc["strip_brightness"] | -1);
    // No tune here: the server plays the "I'm on" sound once, when the unit is switched on.
  } else if (!strcmp(type, "state")) {
    serverState = doc["state"] | "idle";
  } else if (!strcmp(type, "earcon")) {
    earcon(doc["name"] | "");
  } else if (!strcmp(type, "audio_start")) {
    acceptSeq = -1;
    if (doc["ring"] | false) {
      portENTER_CRITICAL(&toneMux);
      toneLen = 0;  // your alarm sound takes over from the beeps
      portEXIT_CRITICAL(&toneMux);
    }
    uint32_t r = doc["rate"] | SAMPLE_RATE;
    if (r == 16000 || r == 22050 || r == 24000 || r == 32000 || r == 44100) wantRate = r;
    flushSpeakerNow();
    acceptSeq = doc["seq"] | -1;
    seqBytes = 0;
    streamEnded = false;
  } else if (!strcmp(type, "audio_end")) {
    if ((doc["seq"] | -2) == acceptSeq) {
      if (seqBytes == 0) {  // nothing arrived (e.g. no voice available): done right away
        JsonDocument d;
        d["type"] = "playback_done";
        d["seq"] = acceptSeq;
        sendJson(d);
      } else {
        streamEnded = true;
      }
    }
  } else if (!strcmp(type, "stop_audio")) {
    acceptSeq = -1;
    flushSpeaker = true;
  } else if (!strcmp(type, "volume")) {
    volumeLevel = constrain(doc["level"] | 6, 0, 10);
    prefs.putUChar("volume", volumeLevel);
  } else if (!strcmp(type, "strip")) {
    stripSet(doc["leds"] | -1, doc["brightness"] | -1);
  } else if (!strcmp(type, "mic_gain")) {
    micShift = constrain(doc["shift"] | MIC_SHIFT_DEFAULT, 8, 18);
    prefs.putUChar("micShift", micShift);
  } else if (!strcmp(type, "alarms")) {
    JsonArray arr = doc["fires"].as<JsonArray>();
    int n = 0;
    for (JsonObject f : arr) {
      if (n >= MAX_FIRES) break;
      strlcpy(fires[n].id, f["id"] | "", sizeof(fires[n].id));
      fires[n].at = f["at"] | 0;
      const char *k = f["kind"] | "alarm";
      fires[n].kind = !strcmp(k, "timer") ? K_TIMER : !strcmp(k, "reminder") ? K_REMINDER : K_ALARM;
      n++;
    }
    fireCount = n;
    saveFires();
    Serial.printf("[alarms] schedule updated: %d fire times\n", n);
  } else if (!strcmp(type, "alarm_stop")) {
    stopRinging(doc["reason"] | (doc["quiet"] | false ? "snooze" : "voice"));
  }
}

static void sendHello() {
  JsonDocument doc;
  doc["type"] = "hello";
  doc["fw"] = FW_VERSION;
  doc["name"] = DEVICE_NAME;
  doc["mac"] = WiFi.macAddress();
  doc["rssi"] = WiFi.RSSI();
  doc["heap"] = ESP.getFreeHeap();
  doc["muted"] = micMuted;
  doc["uptime"] = millis() / 1000;  // lets the server tell switching on from a Wi-Fi reconnect
  doc["sd"] = sdOk;
  doc["recording"] = (bool)recording;
  sendJson(doc);
  if (micMuted || recording) {
    JsonDocument m;
    m["type"] = "audio_mode";
    m["on"] = false;
    sendJson(m);
  }
}

static void wsEvent(WStype_t type, uint8_t *payload, size_t len) {
  switch (type) {
    case WStype_CONNECTED:
      wsConnected = true;
      lastConnected = millis();
      serverState = "idle";
      Serial.printf("[ws] connected to %s:%u\n", resolvedHost.c_str(), resolvedPort);
      sendHello();
      break;
    case WStype_DISCONNECTED:
      if (wsConnected) Serial.println("[ws] disconnected");
      wsConnected = false;
      lastConnected = millis();
      serverState = "offline";
      acceptSeq = -1;
      flushSpeaker = true;
      break;
    case WStype_BIN:
      if (len > 1 && payload[0] == acceptSeq) {
        size_t bytes = (len - 1) & ~1u;
        seqBytes += bytes;
        // All or nothing: a half-written piece would shift every later sample by a byte,
        // which turns the voice into loud static until the next answer.
        lastStreamMs = millis();
        if (xStreamBufferSpacesAvailable(spkStream) >= bytes) xStreamBufferSend(spkStream, payload + 1, bytes, 0);
        else droppedSpk++;
      }
      break;
    case WStype_TEXT:
      handleJson(payload, len);
      break;
    default:
      break;
  }
}

// ------------------------------------------------------------------ lesson recorder
// Hold the button 2-4 s and let go: recording starts onto the micro SD card (16 kHz mono WAV, about 2 MB a minute).
// Do it again to stop. Away from Wi-Fi nothing needs the network. Back at home, finished recordings are sent to the
// Buddy server (which checks they're real lessons) and deleted from the card once it has them.
static void put16(uint8_t *p, uint16_t v) { memcpy(p, &v, 2); }
static void put32(uint8_t *p, uint32_t v) { memcpy(p, &v, 4); }

static void wavHeader(uint8_t *h, uint32_t dataBytes) {
  memcpy(h, "RIFF", 4);
  put32(h + 4, 36 + dataBytes);
  memcpy(h + 8, "WAVEfmt ", 8);
  put32(h + 16, 16);
  put16(h + 20, 1);  // PCM
  put16(h + 22, 1);  // mono
  put32(h + 24, SAMPLE_RATE);
  put32(h + 28, SAMPLE_RATE * 2);
  put16(h + 32, 2);
  put16(h + 34, 16);
  memcpy(h + 36, "data", 4);
  put32(h + 40, dataBytes);
}

static String recName(uint32_t epoch, uint32_t num, const char *ext) {
  char b[48];
  snprintf(b, sizeof(b), "/rec/%lu_%lu.%s", (unsigned long)epoch, (unsigned long)num, ext);
  return String(b);
}

// A recording cut off by the battery dying has no sizes in its header yet: fix it up so it still plays.
static void repairPartials() {
  File dir = SD.open("/rec");
  if (!dir) return;
  String broken[8];
  int n = 0;
  for (File f = dir.openNextFile(); f && n < 8; f = dir.openNextFile()) {
    String name = f.name();
    if (!f.isDirectory() && name.endsWith(".part")) broken[n++] = name;
    f.close();
  }
  dir.close();
  for (int i = 0; i < n; i++) {
    String path = "/rec/" + broken[i];
    File f = SD.open(path, "r+");
    if (!f) continue;
    uint32_t data = f.size() > 44 ? (f.size() - 44) & ~1u : 0;
    if (data < SAMPLE_RATE * 2) {  // under 1 s: nothing worth keeping
      f.close();
      SD.remove(path);
      continue;
    }
    uint8_t h[44];
    wavHeader(h, data);
    f.seek(0);
    f.write(h, 44);
    f.close();
    String fixed = path.substring(0, path.length() - 5) + ".wav";
    SD.rename(path, fixed);
    Serial.printf("[rec] repaired %s\n", fixed.c_str());
  }
}

static void sdBegin() {
  sdSpi.begin(SD_SCK_PIN, SD_MISO_PIN, SD_MOSI_PIN, SD_CS_PIN);
  sdOk = SD.begin(SD_CS_PIN, sdSpi, 20000000) || SD.begin(SD_CS_PIN, sdSpi, 8000000);
  if (sdOk) {
    SD.mkdir("/rec");
    repairPartials();
    Serial.printf("[sd] card ready, %llu MB\n", SD.cardSize() / (1024ULL * 1024ULL));
  } else {
    Serial.println("[sd] no card found (is the reader wired up and the card in?)");
  }
}

static void startRec() {
  uploadAbort = true;  // the SD card is needed for recording: pause any upload first
  for (int i = 0; i < 60 && uploadSdBusy; i++) vTaskDelay(pdMS_TO_TICKS(50));
  if (uploadSdBusy || !sdOk || !recStream) {
    recErrorUntil = millis() + 2500;
    return;
  }
  String path = recName(recEpoch, recNum, "part");
  recFile = SD.open(path, "w+");
  if (!recFile) {
    Serial.printf("[rec] couldn't create %s\n", path.c_str());
    recErrorUntil = millis() + 2500;
    return;
  }
  uint8_t h[44];
  wavHeader(h, 0);
  recFile.write(h, 44);
  xStreamBufferReset(recStream);
  recBytes = 0;
  droppedRec = 0;
  recStartMs = millis();
  recording = true;
  Serial.printf("[rec] recording to %s\n", path.c_str());
}

static void stopRec(bool failed) {
  recording = false;
  vTaskDelay(pdMS_TO_TICKS(60));  // let the mic task finish its last piece
  static int16_t rest[1024];
  size_t got;
  while (!failed && (got = xStreamBufferReceive(recStream, rest, sizeof(rest), 0)) > 0) {
    for (size_t i = 0; i < got / 2; i++) rest[i] = (int16_t)constrain((int32_t)(rest[i] * REC_GAIN), -32768, 32767);
    recFile.write((uint8_t *)rest, got & ~1u);
    recBytes += got & ~1u;
  }
  uint8_t h[44];
  wavHeader(h, recBytes);
  recFile.seek(0);
  recFile.write(h, 44);
  recFile.close();
  String part = recName(recEpoch, recNum, "part"), wav = recName(recEpoch, recNum, "wav");
  if (recBytes < (uint32_t)SAMPLE_RATE * 2 * 2) {  // under 2 s: a slip of the finger
    SD.remove(part);
    Serial.println("[rec] too short, discarded");
    return;
  }
  SD.rename(part, wav);
  Serial.printf("[rec] saved %s (%lu s, %lu dropped pieces)\n", wav.c_str(), (unsigned long)(recBytes / (SAMPLE_RATE * 2)),
                (unsigned long)droppedRec);
  if (failed) recErrorUntil = millis() + 4000;
}

static void recTask(void *) {
  static int16_t chunk[2048];  // 4 KB
  uint32_t lastFlush = 0;
  for (;;) {
    uint8_t cmd = recCmd;
    if (cmd) {
      recCmd = 0;
      if (cmd == 1 && !recording) startRec();
      else if (cmd == 2 && recording) stopRec(false);
    }
    if (!recording) {
      vTaskDelay(pdMS_TO_TICKS(30));
      continue;
    }
    size_t got = xStreamBufferReceive(recStream, chunk, sizeof(chunk), pdMS_TO_TICKS(40));
    got &= ~1u;
    if (!got) continue;
    for (size_t i = 0; i < got / 2; i++) chunk[i] = (int16_t)constrain((int32_t)(chunk[i] * REC_GAIN), -32768, 32767);
    if (recFile.write((uint8_t *)chunk, got) != got) {  // card full, or taken out
      Serial.println("[rec] write failed, stopping");
      stopRec(true);
      continue;
    }
    recBytes += got;
    if (millis() - lastFlush > 2000) {  // so little is lost if the power goes
      recFile.flush();
      lastFlush = millis();
    }
  }
}

static void toggleRecording() {
  if (!sdOk) {
    recErrorUntil = millis() + 2500;
    Serial.println("[rec] no SD card");
    return;
  }
  if (recording) {
    recCmd = 2;
    return;
  }
  prefs.putUInt("recN", prefs.getUInt("recN", 0) + 1);
  recNum = prefs.getUInt("recN", 1);
  recEpoch = timeValid() ? (uint32_t)time(nullptr) : 0;
  if (!recEpoch) {  // no clock yet: remember when it started, work out the date once online
    static int slot = 0;
    pendingNum[slot % 8] = recNum;
    pendingStartMs[slot % 8] = millis();
    slot++;
  }
  recCmd = 1;
}

// Sends one finished recording to the Buddy server. Returns true if the server has it.
static bool uploadFile(const String &name, uint32_t size) {
  uint32_t num = 0, epoch = (uint32_t)atol(name.c_str());
  int us = name.indexOf('_');
  if (us > 0) num = (uint32_t)atol(name.c_str() + us + 1);
  uint32_t dur = size > 44 ? (size - 44) / (SAMPLE_RATE * 2) : 0;
  time_t now = time(nullptr);
  time_t at = epoch;
  if (!at) {  // made without a clock: date it from when this run started it, else from now
    at = now - dur;
    for (int i = 0; i < 8; i++)
      if (pendingNum[i] == num && pendingNum[i]) at = now - (millis() - pendingStartMs[i]) / 1000;
  }
  WiFiClient c;
  c.setTimeout(8);
  if (!c.connect(resolvedHost.c_str(), resolvedPort, 5000)) return false;
  String path = "/study/api/device/recording?recorded_at=" + String((long)at);
  String hdr = "POST " + path + " HTTP/1.1\r\nHost: " + resolvedHost + ":" + String(resolvedPort) +
               "\r\nContent-Type: audio/wav\r\nContent-Length: " + String(size) + "\r\nConnection: close\r\n";
  if (cfgToken.length()) hdr += "X-Buddy-Token: " + cfgToken + "\r\n";
  c.print(hdr + "\r\n");
  File f = SD.open("/rec/" + name, FILE_READ);
  if (!f) return false;
  static uint8_t buf[4096];
  uint32_t sent = 0;
  bool ok = true;
  while (sent < size) {
    if (uploadAbort || !c.connected()) { ok = false; break; }
    int n = f.read(buf, min<uint32_t>(sizeof(buf), size - sent));
    if (n <= 0) { ok = false; break; }
    int off = 0;
    while (off < n && c.connected() && !uploadAbort) {
      int w = c.write(buf + off, n - off);
      if (w > 0) off += w;
      else vTaskDelay(pdMS_TO_TICKS(5));
    }
    if (off < n) { ok = false; break; }
    sent += n;
  }
  f.close();
  uploadSdBusy = false;  // the SD card is free again; the rest is just waiting for the answer
  if (!ok) { c.stop(); return false; }
  uint32_t until = millis() + 60000;
  while (!c.available() && c.connected() && millis() < until) vTaskDelay(pdMS_TO_TICKS(100));
  String status = c.readStringUntil('\n');
  c.stop();
  Serial.printf("[upload] %s -> %s", name.c_str(), status.c_str());
  return status.indexOf(" 200") > 0;
}

static void uploadTask(void *) {
  uint32_t retryAt = 0;
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(4000));
    if (!sdOk || recording || recCmd || !canUpload || ringMode != RING_NONE || millis() < retryAt || !timeValid()) continue;
    String name;
    uint32_t size = 0;
    uploadAbort = false;
    uploadSdBusy = true;
    File dir = SD.open("/rec");
    if (dir) {
      for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
        String n = f.name();
        if (!f.isDirectory() && n.endsWith(".wav")) {
          name = n;
          size = f.size();
          f.close();
          break;
        }
        f.close();
      }
      dir.close();
    }
    if (!name.length()) {
      uploadSdBusy = false;
      continue;
    }
    if (size < 44 + (uint32_t)REC_MIN_UPLOAD_S * SAMPLE_RATE * 2) {  // an accidental hold, not a lesson
      SD.remove("/rec/" + name);
      uploadSdBusy = false;
      continue;
    }
    uploading = true;
    bool done = uploadFile(name, size);
    uploadSdBusy = false;
    uploading = false;
    if (done) {
      SD.remove("/rec/" + name);
      retryAt = 0;
    } else if (!uploadAbort) {
      retryAt = millis() + 60000;  // the server said no (or went away): try again in a minute
    }
  }
}

// ------------------------------------------------------------------ connecting
static bool resolveServer() {
  if (cfgHost.length() && cfgHost != "auto") {
    resolvedHost = cfgHost;
    resolvedPort = cfgPort;
    return true;
  }
  Serial.println("[mdns] looking for the Buddy server...");
  int n = MDNS.queryService("buddy", "tcp");
  if (n <= 0) {
    Serial.println("[mdns] not found (is the server running on the same Wi-Fi?)");
    return false;
  }
  resolvedHost = MDNS.address(0).toString();
  resolvedPort = MDNS.port(0);
  Serial.printf("[mdns] found server at %s:%u\n", resolvedHost.c_str(), resolvedPort);
  return true;
}

static void startWebSocket() {
  lastConnectAttempt = millis();
  if (!resolveServer()) return;
  String path = "/ws?role=device&name=" DEVICE_NAME;
  if (cfgToken.length()) path += "&token=" + cfgToken;
  ws.disconnect();
  ws.begin(resolvedHost, resolvedPort, path);
  ws.onEvent(wsEvent);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 4000, 2);
}

static void maintainConnection() {
  static uint32_t wifiLostAt = 0;
  static bool wasUp = WiFi.status() == WL_CONNECTED;  // already online at start-up: nothing to redo
  bool up = WiFi.status() == WL_CONNECTED;
  if (up && !wasUp) {  // just got onto Wi-Fi (booted away from home, or it dropped): start the clock and name lookup again
    configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
    MDNS.end();
    MDNS.begin(DEVICE_NAME);
    resolvedHost = "";
    lastConnectAttempt = 0;
    Serial.printf("[wifi] online, IP %s\n", WiFi.localIP().toString().c_str());
  }
  wasUp = up;
  if (WiFi.status() != WL_CONNECTED) {
    if (!wifiLostAt) wifiLostAt = millis();
    if (millis() - wifiLostAt > 20000) {
      WiFi.reconnect();
      wifiLostAt = millis();
    }
    return;
  }
  wifiLostAt = 0;
  // Nothing found yet, or no connection for 30 s (the laptop may have a new IP): look again.
  bool autoFind = !cfgHost.length() || cfgHost == "auto";
  bool noTarget = resolvedHost.length() == 0;
  bool stale = autoFind && !wsConnected && millis() - lastConnected > 30000;
  if ((noTarget || stale) && millis() - lastConnectAttempt > (noTarget ? 10000 : 30000)) startWebSocket();
}

static void pumpMic() {
  static uint8_t buf[SEND_BYTES];
  static bool wasRecording = false;
  if (recording != wasRecording) {  // tell the server its microphone is off while a lesson is recorded
    wasRecording = recording;
    JsonDocument doc;
    doc["type"] = "audio_mode";
    doc["on"] = !recording && !micMuted;
    sendJson(doc);
  }
  while (xStreamBufferBytesAvailable(micStream) >= SEND_BYTES) {
    xStreamBufferReceive(micStream, buf, SEND_BYTES, 0);
    if (wsConnected && !micMuted && !recording) ws.sendBIN(buf, SEND_BYTES);
  }
}

static void setMuted(bool muted) {
  micMuted = muted;
  prefs.putBool("muted", muted);
  JsonDocument doc;
  doc["type"] = "audio_mode";
  doc["on"] = !muted;
  sendJson(doc);
  Serial.printf("[mic] %s\n", muted ? "muted" : "unmuted");
}

static void handleButton() {
  static uint32_t downAt = 0;
  bool down = digitalRead(BUTTON_PIN) == LOW;
  uint32_t now = millis();
  if (down && !downAt) downAt = now;
  if (down && downAt) {
    uint32_t held = now - downAt;
    holdStage = held >= 4000 ? 3 : held >= 2000 ? 2 : 0;  // the light shows what letting go will do
    if (held > 10000) {
      Serial.println("[wifi] forgetting Wi-Fi, reopening setup hotspot");
      WiFiManager wm;
      wm.resetSettings();
      delay(300);
      ESP.restart();
    }
  }
  if (!down && downAt) {
    uint32_t held = now - downAt;
    downAt = 0;
    holdStage = 0;
    if (held < 40) return;  // bounce
    if (held >= 4000) {  // 4-10 s: mute / unmute
      setMuted(!micMuted);
      return;
    }
    if (held >= 2000) {  // 2-4 s: start / stop recording a lesson
      toggleRecording();
      return;
    }
    if (ringMode != RING_NONE) {
      stopRinging("button");
    } else if (recording) {
      // a tap while recording a lesson does nothing (hold 2 s to stop)
    } else {
      JsonDocument doc;
      doc["type"] = "button";
      sendJson(doc);
    }
  }
}

static void sendStats() {
  static uint32_t last = 0;
  if (millis() - last < 30000 || !wsConnected) return;
  last = millis();
  JsonDocument doc;
  doc["type"] = "stats";
  doc["rssi"] = WiFi.RSSI();
  doc["heap"] = ESP.getFreeHeap();
  doc["min_heap"] = ESP.getMinFreeHeap();
  doc["uptime"] = millis() / 1000;
  doc["dropped_spk"] = droppedSpk;
  doc["dropped_mic"] = droppedMic;
  doc["muted"] = micMuted;
  doc["alarms"] = fireCount;
  doc["sd"] = sdOk;
  doc["recording"] = (bool)recording;
  doc["rec_seconds"] = recording ? (millis() - recStartMs) / 1000 : 0;
  sendJson(doc);
}

// ------------------------------------------------------------------ setup / loop
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[buddy] firmware " FW_VERSION);
  pinMode(BUTTON_PIN, INPUT_PULLUP);
  led(40, 40, 40);

  prefs.begin("buddy", false);
  cfgHost = prefs.getString("host", DEFAULT_SERVER_HOST);
  cfgPort = prefs.getUShort("port", DEFAULT_SERVER_PORT);
  cfgToken = prefs.getString("token", "");
  volumeLevel = prefs.getUChar("volume", 6);
  micShift = prefs.getUChar("micShift", MIC_SHIFT_DEFAULT);
  micMuted = prefs.getBool("muted", false);
  stripCount = min((int)prefs.getUShort("stripLeds", STRIP_DEFAULT_LEDS), STRIP_MAX_LEDS);
  stripBrightness = prefs.getUChar("stripBri", 70);
  stripInit();
  loadFires();

  micStream = xStreamBufferCreate(MIC_BUFFER_BYTES, SEND_BYTES);
  spkStream = xStreamBufferCreate(SPK_BUFFER_BYTES, 1);
  if (spkStream && ESP.getFreeHeap() < 200000) {  // Wi-Fi starts after this and needs plenty of room
    vStreamBufferDelete(spkStream);
    spkStream = nullptr;
  }
  if (!spkStream) spkStream = xStreamBufferCreate(SPK_BUFFER_MIN, 1);
  Serial.printf("[audio] speaker buffer %u KB, free heap %u KB\n",
                (unsigned)(xStreamBufferSpacesAvailable(spkStream) / 1024), (unsigned)(ESP.getFreeHeap() / 1024));
  setupI2S();
  sdBegin();
  if (sdOk) {
    size_t recBuf = ESP.getFreeHeap() > 220000 ? 24 * 1024 : 12 * 1024;  // about 0.7 s of microphone
    recStream = xStreamBufferCreate(recBuf, 1);
  }
  xTaskCreatePinnedToCore(micTask, "mic", 6144, nullptr, 6, nullptr, 0);
  xTaskCreatePinnedToCore(playTask, "play", 6144, nullptr, 6, nullptr, 1);
  if (sdOk) {
    xTaskCreatePinnedToCore(recTask, "rec", 6144, nullptr, 3, nullptr, 1);
    xTaskCreatePinnedToCore(uploadTask, "upload", 8192, nullptr, 1, nullptr, 1);
  }

  // Wi-Fi: first boot (or after a 10 s button hold) opens the "Buddy-Setup" hotspot.
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // modem sleep adds audio latency and dropouts
  WiFiManager wm;
  char portStr[8];
  snprintf(portStr, sizeof(portStr), "%u", cfgPort);
  WiFiManagerParameter pHost("host", "Buddy server address (leave as auto)", cfgHost.c_str(), 63);
  WiFiManagerParameter pPort("port", "Server port", portStr, 6);
  WiFiManagerParameter pToken("token", "Access token (only if you set one)", cfgToken.c_str(), 63);
  wm.addParameter(&pHost);
  wm.addParameter(&pPort);
  wm.addParameter(&pToken);
  wm.setSaveConfigCallback([]() { shouldSaveConfig = true; });
  wm.setAPCallback([](WiFiManager *) { led(0, 120, 120); Serial.println("[wifi] setup hotspot open: " SETUP_AP_NAME); });
  wm.setConfigPortalTimeout(300);
  wm.setConnectTimeout(25);
  wm.setTitle("Buddy setup");
  // Away from home (school) the saved network isn't there. If one was saved, don't open the setup hotspot
  // or give up: carry on without Wi-Fi so lessons can be recorded, and join it when it appears.
  bool hadWifi = wm.getWiFiIsSaved();
  if (hadWifi) {
    wm.setEnableConfigPortal(false);
    wm.setConnectTimeout(12);
  }
  if (!wm.autoConnect(SETUP_AP_NAME, SETUP_AP_PASSWORD) && !hadWifi) {
    Serial.println("[wifi] no network configured in time; restarting");
    delay(1000);
    ESP.restart();
  }
  if (shouldSaveConfig) {
    cfgHost = String(pHost.getValue());
    cfgHost.trim();
    if (!cfgHost.length()) cfgHost = "auto";
    cfgPort = (uint16_t)atoi(pPort.getValue());
    if (!cfgPort) cfgPort = DEFAULT_SERVER_PORT;
    cfgToken = String(pToken.getValue());
    cfgToken.trim();
    prefs.putString("host", cfgHost);
    prefs.putUShort("port", cfgPort);
    prefs.putString("token", cfgToken);
  }
  WiFi.setAutoReconnect(true);
  if (WiFi.status() == WL_CONNECTED)
    Serial.printf("[wifi] connected, IP %s, RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  else
    Serial.println("[wifi] not in range: working offline (lessons can still be recorded)");

  configTime(0, 0, "pool.ntp.org", "time.google.com", "time.cloudflare.com");
  MDNS.begin(DEVICE_NAME);
  startWebSocket();
}

void loop() {
  ws.loop();
  pumpMic();
  if (playbackDone) {
    playbackDone = false;
    JsonDocument doc;
    doc["type"] = "playback_done";
    doc["seq"] = acceptSeq;
    sendJson(doc);
  }
  canUpload = wsConnected && serverState == "idle";
  handleButton();
  checkAlarms();
  maintainConnection();
  sendStats();
  updateLed();
  stripUpdate();
  delay(1);
}
