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
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <WiFiManager.h>
#include <driver/i2s_std.h>
#include <freertos/stream_buffer.h>
#include <math.h>
#include <sys/time.h>

#include "config.h"

#define FW_VERSION "1.0.0"
#define SAMPLE_RATE 16000
#define MIC_FRAMES 320               // 20 ms per I2S read
#define SEND_BYTES 1280              // 40 ms of 16-bit audio per WebSocket frame
#define MIC_BUFFER_BYTES (16 * 1024) // 0.5 s
#define SPK_BUFFER_BYTES (48 * 1024) // 1.5 s; the server stays ~0.6 s ahead
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
    if (!micMuted && xStreamBufferSend(micStream, out, frames * 2, 0) < (size_t)frames * 2) droppedMic++;
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
  uint32_t total = (uint32_t)n.ms * SAMPLE_RATE / 1000;
  int count = min<int>(maxSamples, total - pos);
  const uint32_t attack = SAMPLE_RATE / 250, release = SAMPLE_RATE / 40;
  for (int i = 0; i < count; i++, pos++) {
    float env = 1.0f;
    if (pos < attack) env = (float)pos / attack;
    if (total - pos < release) env = min(env, (float)(total - pos) / release);
    float s = 0;
    if (n.freq) {
      s = sinf(phase) * 0.8f + sinf(phase * 2.0f) * 0.12f;  // a touch of warmth
      phase += 2.0f * PI * n.freq / SAMPLE_RATE;
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
    if (flushSpeaker) {
      xStreamBufferReset(spkStream);
      flushSpeaker = false;
      playing = false;
    }
    if (ringMode != RING_NONE && toneLen == 0) {
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
  if (ringMode != RING_NONE) {
    bool on = (t / 250) % 2;
    led(on ? 255 : 40, on ? 150 : 20, 0);
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
    uint32_t limit = ringKind == K_TIMER ? 3 * 60000 : 10 * 60000;
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
    earcon("hello");
  } else if (!strcmp(type, "state")) {
    serverState = doc["state"] | "idle";
  } else if (!strcmp(type, "earcon")) {
    earcon(doc["name"] | "");
  } else if (!strcmp(type, "audio_start")) {
    acceptSeq = -1;
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
    stopRinging(doc["quiet"] | false ? "snooze" : "voice");
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
  sendJson(doc);
  if (micMuted) {
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
        if (xStreamBufferSend(spkStream, payload + 1, bytes, 0) < bytes) droppedSpk++;
      }
      break;
    case WStype_TEXT:
      handleJson(payload, len);
      break;
    default:
      break;
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
  while (xStreamBufferBytesAvailable(micStream) >= SEND_BYTES) {
    xStreamBufferReceive(micStream, buf, SEND_BYTES, 0);
    if (wsConnected && !micMuted) ws.sendBIN(buf, SEND_BYTES);
  }
}

static void setMuted(bool muted) {
  micMuted = muted;
  prefs.putBool("muted", muted);
  JsonDocument doc;
  doc["type"] = "audio_mode";
  doc["on"] = !muted;
  sendJson(doc);
  earcon(muted ? "nospeech" : "wake");
  Serial.printf("[mic] %s\n", muted ? "muted" : "unmuted");
}

static void handleButton() {
  static uint32_t downAt = 0;
  static bool handledLong = false;
  bool down = digitalRead(BUTTON_PIN) == LOW;
  uint32_t now = millis();
  if (down && !downAt) {
    downAt = now;
    handledLong = false;
  }
  if (down && downAt && !handledLong) {
    if (now - downAt > 10000) {
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
    if (held < 40) return;  // bounce
    if (held >= 3000) {
      setMuted(!micMuted);
      return;
    }
    if (ringMode != RING_NONE) {
      stopRinging("button");
    } else {
      JsonDocument doc;
      doc["type"] = "button";
      sendJson(doc);
      if (!wsConnected) earcon("error");
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
  loadFires();

  micStream = xStreamBufferCreate(MIC_BUFFER_BYTES, SEND_BYTES);
  spkStream = xStreamBufferCreate(SPK_BUFFER_BYTES, 1);
  setupI2S();
  xTaskCreatePinnedToCore(micTask, "mic", 6144, nullptr, 6, nullptr, 0);
  xTaskCreatePinnedToCore(playTask, "play", 6144, nullptr, 6, nullptr, 1);

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
  if (!wm.autoConnect(SETUP_AP_NAME, SETUP_AP_PASSWORD)) {
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
  Serial.printf("[wifi] connected, IP %s, RSSI %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());

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
  handleButton();
  checkAlarms();
  maintainConnection();
  sendStats();
  updateLed();
  delay(1);
}
