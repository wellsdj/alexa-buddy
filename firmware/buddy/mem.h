// Getting memory on the board. Read firmware/LIMITS.md first.
//
// Every buffer of 4 KB or more comes from here (and is listed in memory_plan.h), never from a plain malloc/new
// or a big array. That way the board log shows where everything went, and the tests can add it all up.
//   memBig(name, bytes)    PSRAM: big, slower, plenty of it. nullptr if there is no PSRAM.
//   memFast(name, bytes)   internal RAM: small, fast, needed by hardware and interrupts. Scarce.
//   memStream(...)         a FreeRTOS stream buffer with its storage in PSRAM (internal fallback size if none).
//   memTemp(bytes)         a short-lived buffer (freed soon after): PSRAM if there is any. Not listed in the log.
#pragma once
#include <Arduino.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/stream_buffer.h>

struct MemEntry {
  const char *name;
  uint32_t bytes;
  bool inPsram;
};
static MemEntry memLedger[24];
static int memLedgerLen = 0;
static uint8_t memFallbacks = 0;  // BIG things that had to go in internal RAM (no PSRAM)

static void memNote(const char *name, size_t bytes, bool inPsram) {
  if (memLedgerLen < (int)(sizeof(memLedger) / sizeof(memLedger[0]))) memLedger[memLedgerLen++] = {name, (uint32_t)bytes, inPsram};
}

static void *memBig(const char *name, size_t bytes) {
  if (!psramFound()) return nullptr;
  void *p = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  if (p) memNote(name, bytes, true);
  return p;
}

static void *memFast(const char *name, size_t bytes) {
  void *p = heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  if (p) memNote(name, bytes, false);
  return p;
}

// PSRAM if possible, otherwise `fallback` bytes of internal RAM (0 = don't fall back). *got says how much it got.
static void *memBigOr(const char *name, size_t bytes, size_t fallback, size_t *got = nullptr) {
  void *p = memBig(name, bytes);
  size_t n = bytes;
  if (!p && fallback) {
    n = fallback;
    p = memFast(name, n);
    if (p) memFallbacks++;
  }
  if (got) *got = p ? n : 0;
  return p;
}

static void *memTemp(size_t bytes) {
  void *p = psramFound() ? heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) : nullptr;
  return p ? p : heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
}

static StreamBufferHandle_t memStream(const char *name, size_t bytes, size_t fallback, size_t trigger) {
  auto *ctl = (StaticStreamBuffer_t *)heap_caps_malloc(sizeof(StaticStreamBuffer_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
  size_t got = 0;
  auto *store = (uint8_t *)memBigOr(name, bytes + 1, fallback + 1, &got);  // a stream buffer needs one spare byte
  if (!ctl || !store) return nullptr;
  return xStreamBufferCreateStatic(got - 1, trigger, store, ctl);
}

static void memReport(Print &out) {
  out.printf("[mem] PSRAM %s: %u KB free of %u KB\n", psramFound() ? "found" : "NOT FOUND",
             (unsigned)(ESP.getFreePsram() / 1024), (unsigned)(ESP.getPsramSize() / 1024));
  for (int i = 0; i < memLedgerLen; i++)
    out.printf("[mem]   %-22s %4u KB  %s\n", memLedger[i].name, (unsigned)((memLedger[i].bytes + 1023) / 1024),
               memLedger[i].inPsram ? "PSRAM" : "internal");
  if (memFallbacks) out.printf("[mem] %u big buffers are in internal RAM because there is no PSRAM\n", memFallbacks);
}
