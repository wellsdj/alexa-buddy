// Build and run: g++ -O2 -std=c++17 -Ifirmware/buddy firmware/tests/aec_test.cpp -o /tmp/aec && /tmp/aec 1100
// Host test: simulated speaker -> room -> mic, checks the canceller removes Buddy's voice
// and leaves a person's voice alone.
#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <vector>
#include "aec.h"
static AecPipeline P;
static int16_t refMem[AEC_REF_LEN], histMem[AEC_HIST];

double rnd() { return rand() / (double)RAND_MAX * 2 - 1; }
int main(int argc, char **argv) {
  int delay = argc > 1 ? atoi(argv[1]) : 1100;  // samples from writing to hearing
  int SR = 16000, total = SR * 12;
  // Buddy "speech": noise shaped into syllables, 0..8 s; a person talks 9..11 s (also 5..6 s over Buddy)
  std::vector<float> buddy(total, 0), person(total, 0);
  double lp = 0, lp2 = 0;
  for (int i = 0; i < total; i++) {
    double env = (i < 8 * SR) ? (0.5 + 0.5 * sin(i * 2 * M_PI * 4 / SR)) : 0;
    lp = 0.9 * lp + 0.1 * rnd();
    buddy[i] = env * lp * 30000;
    double penv = ((i > 9 * SR && i < 11 * SR) || (i > 5 * SR && i < 6 * SR)) ? 0.6 + 0.4 * sin(i * 2 * M_PI * 3 / SR) : 0;
    lp2 = 0.8 * lp2 + 0.2 * rnd();
    person[i] = penv * lp2 * 8000;
  }
  // room: decaying echo path, 0.4 loud, 15 ms long
  std::vector<float> h(240);
  for (int k = 0; k < 240; k++) h[k] = 0.4 * exp(-k / 40.0) * rnd();
  h[0] = 0.4;
  std::vector<float> mic(total, 0);
  for (int i = 0; i < total; i++) {
    double s = person[i] + rnd() * 30;
    for (int k = 0; k < 240; k++) { int j = i - delay - k; if (j >= 0) s += h[k] * buddy[j]; }
    mic[i] = s;
  }
  // run: the speaker side is 768 samples ahead (its queue), blocks of 256
  P.attach(refMem, histMem);
  std::vector<int16_t> out(total);
  int16_t blk[256];
  for (int i = 0; i < 768; i += 256) { int16_t z[256] = {0}; P.pushRef(z, 256); }
  for (int b = 0; b < total; b += 256) {
    for (int i = 0; i < 256; i++) blk[i] = (int16_t)fmax(-32768, fmin(32767, buddy[b + 768 + i < total ? b + 768 + i : 0] * (b + 768 + i < total)));
    P.pushRef(blk, 256);
    for (int i = 0; i < 256; i++) blk[i] = (int16_t)fmax(-32768, fmin(32767, mic[b + i]));
    P.processMic(blk, 256);
    for (int i = 0; i < 256; i++) out[b + i] = blk[i];
    P.delayTask();
  }
  auto e = [&](const std::vector<float> &x, int a, int b) { double s = 0; for (int i = a; i < b; i++) s += x[i] * x[i]; return s; };
  auto eo = [&](int a, int b) { double s = 0; for (int i = a; i < b; i++) s += (double)out[i] * out[i]; return s; };
  printf("measured lag %d (true: echo %d behind writing, minus 768 queue = %d)\n", P.lag, delay, delay - 768);
  printf("echo removed 2-5 s: %.1f dB\n", 10 * log10(e(mic, 2 * SR, 5 * SR) / eo(2 * SR, 5 * SR)));
  printf("echo removed 6.5-8 s: %.1f dB\n", 10 * log10(e(mic, 6.5 * SR, 8 * SR) / eo(6.5 * SR, 8 * SR)));
  // person's voice while Buddy talked (5-6 s): compare output with the person alone
  double err = 0, pe = 0;
  for (int i = 5 * SR; i < 6 * SR; i++) { err += (out[i] - person[i]) * (double)(out[i] - person[i]); pe += person[i] * (double)person[i]; }
  printf("person over Buddy (5-6 s): leftover echo is %.1f dB below their voice\n", 10 * log10(pe / err));
  printf("person alone (9-11 s) untouched: %s\n", fabs(10 * log10(e(mic, 9 * SR, 11 * SR) / eo(9 * SR, 11 * SR))) < 0.1 ? "yes" : "NO");
  printf("resets %u\n", P.ec.resets);
}
