// Captures the microphone and turns it into 16 kHz mono int16 frames of 20 ms (320 samples),
// the same format the ESP32 sends. Low-pass first so resampling doesn't alias.
class BuddyMic extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.prev = 0;
    this.out = new Int16Array(320);
    this.n = 0;
    // Two cascaded RBJ low-pass biquads at 7 kHz (4th order) before decimation.
    const f = 7000, q = 0.7071;
    const w = 2 * Math.PI * f / sampleRate, cos = Math.cos(w), alpha = Math.sin(w) / (2 * q);
    const a0 = 1 + alpha;
    this.b = [(1 - cos) / 2 / a0, (1 - cos) / a0, (1 - cos) / 2 / a0];
    this.a = [(-2 * cos) / a0, (1 - alpha) / a0];
    this.z = [[0, 0], [0, 0]];
  }
  lp(x, s) {
    const z = this.z[s], b = this.b, a = this.a;
    const y = b[0] * x + z[0];
    z[0] = b[1] * x - a[0] * y + z[1];
    z[1] = b[2] * x - a[1] * y;
    return y;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      const x = this.lp(this.lp(ch[i], 0), 1);
      // Linear interpolation between the previous and current filtered sample.
      while (this.pos <= 1) {
        const v = this.prev + (x - this.prev) * this.pos;
        const s = Math.max(-1, Math.min(1, v));
        this.out[this.n++] = s < 0 ? s * 32768 : s * 32767;
        if (this.n === 320) {
          this.port.postMessage(this.out.buffer, [this.out.buffer]);
          this.out = new Int16Array(320);
          this.n = 0;
        }
        this.pos += this.ratio;
      }
      this.pos -= 1;
      this.prev = x;
    }
    return true;
  }
}
registerProcessor("buddy-mic", BuddyMic);
