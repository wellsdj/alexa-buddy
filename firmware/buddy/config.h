// ---------------------------------------------------------------------------
// Buddy firmware settings. Wi-Fi and the server address are NOT set here: on first
// boot the board opens a Wi-Fi hotspot called "Buddy-Setup" (password: heybuddy)
// and you choose your network in a phone browser.
// ---------------------------------------------------------------------------
#pragma once

// Microphone: INMP441 (I2S). Tie the mic's L/R pin to GND.
#define MIC_SCK_PIN   4   // INMP441 SCK
#define MIC_WS_PIN    5   // INMP441 WS
#define MIC_SD_PIN    6   // INMP441 SD

// Amplifier: MAX98357A (I2S). Leave its SD and GAIN pins unconnected.
#define AMP_BCLK_PIN  15  // MAX98357A BCLK
#define AMP_LRC_PIN   16  // MAX98357A LRC
#define AMP_DIN_PIN   7   // MAX98357A DIN

// The DevKitC-1's BOOT button: tap = talk / stop alarm, hold 3 s = mute mic,
// hold 10 s = forget Wi-Fi and reopen the setup hotspot.
#define BUTTON_PIN    0

// On-board RGB status LED: GPIO 48 on DevKitC-1 v1.0, GPIO 38 on v1.1. Both are driven so
// either board version works (neither pin is used for anything else). -1 to disable.
#define STATUS_LED_PIN 48
#define STATUS_LED_PIN_ALT 38
#define LED_BRIGHTNESS 40  // 0-255; the on-board LED is very bright

// LED strip behind the grille (WS2812B / WS2812E: 5V, GND, DIN). -1 to disable.
// How many LEDs and how bright are set on Buddy's web page (Settings > Light strip).
#define STRIP_PIN 2
#define STRIP_MAX_LEDS 60      // the most it will drive (one metre of 60/m)
#define STRIP_DEFAULT_LEDS 15  // until the web page says otherwise (about 25 cm)

// Lesson recorder: a micro SD card reader (3.3 V SPI type) on the board's own SPI pins.
// Hold the button 2-4 s and let go to start / stop a recording; it uploads by itself when home.
// SD reader pin -> board: CS -> 10, MOSI -> 11, SCK -> 12, MISO -> 13, VCC -> 3V3, GND -> GND.
#define SD_CS_PIN   10
#define SD_MOSI_PIN 11
#define SD_SCK_PIN  12
#define SD_MISO_PIN 13
#define REC_GAIN 2.0f          // lessons are heard from further away than you talking to Buddy
#define REC_MIN_UPLOAD_S 10    // shorter recordings are deleted instead of sent

// Microphone loudness. The INMP441 is quiet, so its 24-bit samples are shifted down by
// this much to 16 bits. Lower = louder. The server can change it live (web page ->
// Settings -> Listening), so you rarely need to touch this.
#define MIC_SHIFT_DEFAULT 14

// Defaults shown in the setup page. "auto" finds the server on your network by itself.
#define DEFAULT_SERVER_HOST "auto"
#define DEFAULT_SERVER_PORT 8000

#define SETUP_AP_NAME     "Buddy-Setup"
#define SETUP_AP_PASSWORD "heybuddy"
#define DEVICE_NAME       "buddy-desk"
