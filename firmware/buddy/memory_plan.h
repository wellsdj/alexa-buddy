// Buddy's memory plan: every piece of memory of 4 KB or more that the board program uses, where it lives, and why.
// Read firmware/LIMITS.md first. server/tests/test_firmware_rules.py adds this table up and fails if a tier goes over
// its budget, or if the program uses a big buffer that isn't listed here.
//
// Tiers:
//   FAST   internal RAM, allocated at start-up (task stacks, anything hardware or an interrupt touches)
//   STATIC internal RAM, a fixed array in the program (only for hardware buffers that must be internal)
//   BIG    PSRAM (8 MB, slower), for big buffers and history. If PSRAM is missing, the fallback size is used
//          in internal RAM instead and the board log says so.
//
// Columns: name, tier, bytes, fallback bytes (internal, BIG only), what it's for.
#pragma once

#define MEM_PLAN(X)                                                                                               \
  X(SPEAKER,     BIG,    96 * 1024, 24 * 1024, "speaker reservoir: 3 s of speech from the server")               \
  X(MIC,         BIG,    16 * 1024, 8 * 1024,  "mic audio waiting to go to the server: 0.5 s")                   \
  X(REC,         BIG,    64 * 1024, 12 * 1024, "lesson audio waiting for the SD card: 2 s of slow card writes")   \
  X(AEC_REF,     BIG,    AEC_REF_LEN * 2, AEC_REF_LEN * 2, "echo canceller: the last 1 s Buddy played")           \
  X(AEC_HIST,    BIG,    AEC_HIST * 2, AEC_HIST * 2, "echo canceller: the last 0.5 s the mic heard")               \
  X(AEC_DELAY,   BIG,    (2 * AEC_WIN + AEC_MAX_LAG) * 4, 0, "echo delay measuring, for a moment (skipped if no room)") \
  X(STACK_MIC,   FAST,   6144, 0, "mic task stack")                                                               \
  X(STACK_PLAY,  FAST,   6144, 0, "speaker task stack")                                                           \
  X(STACK_AEC,   FAST,   4096, 0, "echo-delay task stack")                                                        \
  X(STACK_REC,   FAST,   6144, 0, "lesson recorder task stack")                                                   \
  X(STACK_UPLOAD, FAST,  8192, 0, "lesson upload task stack")                                                     \
  X(AEC_WORK,    STATIC, (2 * AEC_TAPS + 2 * 512) * 4, 0, "echo canceller: filter and working arrays, used every 20 ms") \
  X(STRIP_BITS,  STATIC, STRIP_MAX_LEDS * 24 * 4, 0, "LED strip signal (the RMT hardware reads it)")              \
  X(REC_CHUNK,   STATIC, 4096, 0, "one SD card write")                                                            \
  X(UPLOAD_BUF,  STATIC, 4096, 0, "one network write while uploading a lesson")

// Budgets, checked by the tests (from the table) and at run time (by the board and the server).
#define BUDGET_FAST_KB 72          // FAST + STATIC together
#define BUDGET_STATIC_RAM_KB 100   // all fixed internal RAM the build reports ("Global variables use ...")
#define BUDGET_BIG_KB 6144         // of the 8 MB PSRAM: leave 2 MB spare
#define MIN_FREE_INTERNAL_KB 40    // internal RAM that must stay free once connected (Wi-Fi, web socket, SD card)

// MEM_SPEAKER, MEM_SPEAKER_FALLBACK, ... for the code.
#define MEM_PLAN_ENUM(name, tier, bytes, fallback, why) MEM_##name = (bytes), MEM_##name##_FALLBACK = (fallback),
enum : uint32_t { MEM_PLAN(MEM_PLAN_ENUM) };
#undef MEM_PLAN_ENUM
