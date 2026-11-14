// Echo cancelling: takes Buddy's own voice out of what the microphone hears.
//
// The ESP32 knows exactly what it sends to the speaker (the "reference"). The microphone hears that
// a few tens of milliseconds later, changed by the speaker, the box and the room. An adaptive filter
// (NLMS) learns that change and subtracts its estimate of the echo, leaving mostly the person's voice.
//
//   1. findDelay(): how far behind the reference the echo arrives (cross-correlation).
//   2. process():   per sample, estimate the echo from the recent reference and subtract it.
//
// It only works while Buddy is making sound; the rest of the time the mic passes through untouched,
// so "hey buddy" and normal listening are never affected. If the filter ever makes things louder
// (it went wrong), it resets itself.
#pragma once
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#ifndef AEC_TAPS
#define AEC_TAPS 320  // 20 ms of echo path at 16 kHz
#endif
#define AEC_PRE 32     // taps allowed before the measured delay (it's never exact)

class EchoCanceller {
 public:
  float w[AEC_TAPS];
  float mu = 0.25f;     // how fast it learns
  float power = 0;      // sum of squares of the reference in the filter window
  bool converged = false;
  uint32_t resets = 0;
  float erleDb = 0;     // how much echo it's removing (dB), for the stats

  EchoCanceller() { reset(); }
  void reset() {
    memset(w, 0, sizeof(w));
    converged = false;
    resets++;
  }

  // ref(i) gives the reference sample i places back in time from the newest one in the window,
  // i = 0 .. AEC_TAPS-1. The caller lines the window up using the measured delay.
  // Returns the cleaned mic sample.
  template <typename Ref>
  float step(float mic, Ref ref, float xNew, float xOld, bool adapt) {
    power += xNew * xNew - xOld * xOld;
    if (power < 0) power = 0;
    float y = 0;
    for (int k = 0; k < AEC_TAPS; k++) y += w[k] * ref(k);
    float e = mic - y;
    if (adapt && power > 1e3f) {
      float g = mu * e / (power + 1e4f);
      for (int k = 0; k < AEC_TAPS; k++) w[k] += g * ref(k);
    }
    return e;
  }
};

// Cross-correlation: the lag (0..maxLag) at which the mic best matches the reference.
// mic[0..n-1]; ref is indexed so ref[j] is the reference sample that was sent j samples before
// mic[0]'s matching "now" (ref must hold n + maxLag samples). Returns -1 if there's no clear match.
static inline int aecFindDelay(const float *mic, const float *ref, int n, int maxLag, float *strength) {
  // Normalised so a loud passage doesn't win just by being loud.
  double micE = 0;
  for (int i = 0; i < n; i++) micE += (double)mic[i] * mic[i];
  int best = -1;
  double bestScore = 0, sum = 0;
  int count = 0;
  for (int lag = 0; lag <= maxLag; lag++) {
    double c = 0, refE = 0;
    for (int i = 0; i < n; i++) {
      float r = ref[i + lag];
      c += (double)mic[i] * r;
      refE += (double)r * r;
    }
    if (refE <= 0 || micE <= 0) continue;
    double score = fabs(c) / sqrt(refE * micE);
    sum += score;
    count++;
    if (score > bestScore) {
      bestScore = score;
      best = lag;
    }
  }
  double mean = count ? sum / count : 0;
  if (strength) *strength = (float)bestScore;
  // A real echo stands out well above the typical lag.
  if (bestScore < 0.15 || bestScore < 3 * mean) return -1;
  return best;
}

// ---------------------------------------------------------------- the whole thing
// The speaker side calls pushRef() with every sample it plays (silence too, so the timeline never
// skips). The mic side calls processMic() on each block. delayTask() (low priority) measures the
// delay whenever Buddy talks, so a change (another speaker, a new box) is picked up by itself.
#define AEC_REF_LEN 16384   // 1 s of what was played
#define AEC_HIST 8192       // the last 0.5 s heard
#define AEC_WIN 4096        // samples compared when measuring the delay
#define AEC_MAX_LAG 3200    // up to 200 ms between playing and hearing

struct AecPipeline {
  int16_t ref[AEC_REF_LEN];
  volatile uint32_t refCount = 0;   // samples played so far
  int16_t hist[AEC_HIST];
  volatile uint32_t micCount = 0;   // samples heard so far
  int32_t snap = 0;                 // refCount - micCount, measured once both are running
  bool snapSet = false;
  volatile int lag = -1;            // measured delay (samples), -1 = not known yet
  volatile bool wantDelay = false;
  uint32_t loudSince = 0;           // micCount when the current stretch of Buddy talking started
  uint32_t lastMeasured = 0;
  uint32_t lastLoud = 0;            // micCount when Buddy was last making sound
  bool enabled = true;
  EchoCanceller ec;
  float tmp[512];
  float X[512 + AEC_TAPS];
  bool talkingOver = false;         // someone talking over Buddy right now
  float inE = 0, outE = 0;          // energy in and out this block, for ERLE and the safety check

  void pushRef(const int16_t *s, int n) {
    uint32_t c = refCount;
    for (int i = 0; i < n; i++) ref[(c + i) % AEC_REF_LEN] = s[i];
    refCount = c + n;
  }

  inline float refAt(int64_t r) const {
    if (r < 0 || r >= (int64_t)refCount || (int64_t)refCount - r >= AEC_REF_LEN) return 0;
    return ref[(uint32_t)r % AEC_REF_LEN];
  }

  // Cleans io[0..n-1] in place (when it can).
  void processMic(int16_t *io, int n) {  // n <= 512
    if (!snapSet && refCount > 0) {
      snap = (int32_t)(refCount - micCount) - n;  // this block lines up with the newest block played
      snapSet = true;
    }
    uint32_t m0 = micCount;
    for (int i = 0; i < n; i++) hist[(m0 + i) % AEC_HIST] = io[i];
    micCount = m0 + n;
    if (!enabled || !snapSet) return;
    // Is Buddy making sound around now? Look at the played sound lined up with this block.
    int L = lag;
    int64_t base = (int64_t)m0 + snap - (L < 0 ? 0 : L);
    double refE = 0;
    for (int i = 0; i < n; i++) {
      float r = refAt(base + i);
      refE += (double)r * r;
    }
    bool talking = refE / n > 2.0e4;  // above about -44 dBFS
    if (!talking) {
      if (micCount - lastLoud > 4800) loudSince = micCount;  // quiet for 0.3 s: that stretch is over
      return;
    }
    lastLoud = micCount;
    // Measure the delay at the start of each stretch of talking (after 0.3 s of it) and every 10 s.
    if (micCount - loudSince > AEC_WIN && (L < 0 || micCount - lastMeasured > 160000)) {
      wantDelay = true;
      lastMeasured = micCount;
    }
    if (L < 0) return;  // not measured yet: leave the sound alone
    int64_t r0 = base + AEC_PRE;
    // The played sound this block needs, in one straight line (fast to read): X[i + AEC_TAPS - k] is
    // the sample k places before the one lined up with mic sample i.
    int64_t x0 = r0 - AEC_TAPS;
    for (int j = 0; j < n + AEC_TAPS; j++) X[j] = refAt(x0 + j);
    float p = 0;
    for (int j = 0; j < AEC_TAPS; j++) p += X[j] * X[j];
    // First a look without learning: if a lot is left over once it's working, someone is talking
    // over Buddy, and learning from that would teach it to cancel their voice. So it only learns when
    // what's left is mostly echo.
    double ie = 0, oe = 0;
    ec.power = p;
    for (int i = 0; i < n; i++) {
      const float *xi = X + i + AEC_TAPS;
      float d = io[i];
      float e = ec.step(d, [xi](int k) { return xi[-k]; }, xi[0], X[i], false);
      ie += (double)d * d;
      oe += (double)e * e;
      tmp[i] = e;
    }
    bool overTalk = ec.erleDb > 10.f && oe > ie * 0.1;
    if (!overTalk) {
      ec.power = p;
      oe = 0;
      for (int i = 0; i < n; i++) {
        const float *xi = X + i + AEC_TAPS;
        float e = ec.step(io[i], [xi](int k) { return xi[-k]; }, xi[0], X[i], true);
        oe += (double)e * e;
        tmp[i] = e;
      }
    }
    for (int i = 0; i < n; i++) io[i] = (int16_t)fmaxf(-32768.f, fminf(32767.f, tmp[i]));
    talkingOver = overTalk;
    inE = ie;
    outE = oe;
    if (oe > ie * 2.0 && ie > 1e6) {  // made it worse: it learnt something wrong, start again
      ec.reset();
      lag = -1;
      for (int i = 0; i < n; i++) io[i] = (int16_t)hist[(m0 + i) % AEC_HIST];
    } else if (ie > 0 && oe > 0 && !overTalk) {
      ec.erleDb = ec.erleDb * 0.95f + 0.05f * 10.f * log10f((float)(ie / oe));
    }
  }

  // Low priority: measure the delay when asked. Uses its own copies, so it doesn't hold anything up.
  bool delayTask() {
    if (!wantDelay) return false;
    wantDelay = false;
    float *micBuf = (float *)malloc(AEC_WIN * sizeof(float));
    float *refBuf = (float *)malloc((AEC_WIN + AEC_MAX_LAG) * sizeof(float));
    if (!micBuf || !refBuf) {
      free(micBuf);
      free(refBuf);
      return false;
    }
    uint32_t end = micCount;
    uint32_t m0 = end - AEC_WIN;
    for (int i = 0; i < AEC_WIN; i++) micBuf[i] = hist[(m0 + i) % AEC_HIST];
    // refBuf[j] lines up with micBuf[i] at lag L when j = i + (AEC_MAX_LAG - L)
    int64_t r0 = (int64_t)m0 + snap - AEC_MAX_LAG;
    for (int j = 0; j < AEC_WIN + AEC_MAX_LAG; j++) refBuf[j] = refAt(r0 + j);
    float strength = 0;
    int best = aecFindDelay(micBuf, refBuf, AEC_WIN, AEC_MAX_LAG, &strength);
#ifdef AEC_DEBUG
    printf("delay check: best %d strength %.3f\n", best, strength);
#endif
    if (best >= 0) {
      int L = AEC_MAX_LAG - best;
      if (L != lag) {
        if (lag >= 0 && abs(L - lag) > 16) ec.reset();  // a real change: relearn
        lag = L;
      }
    }
    free(micBuf);
    free(refBuf);
    return best >= 0;
  }

  // Start a fresh timeline (after music at another rate, or switching it back on).
  void restart() {
    snapSet = false;
    lag = -1;
    ec.reset();
  }
};
