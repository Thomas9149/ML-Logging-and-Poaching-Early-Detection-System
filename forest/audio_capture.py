#!/usr/bin/env python3
"""
Stage 1 - microphone -> YAMNet-ready audio frames.

What YAMNet expects
-------------------
  * mono audio (one channel at a time)
  * 16,000 samples per second
  * float32 values between -1.0 and 1.0
  * the TensorFlow Lite version takes exactly 15,600 samples (0.975 s) per call

What this script does
---------------------
  1. Reads audio from a USB microphone (e.g. the RODE Wireless PRO receiver)
     or from a WAV file (handy for testing on a laptop without hardware).
  2. Keeps each channel separate - with the Wireless PRO, each transmitter is
     its own channel, so each one acts as a separate "forest sensor".
  3. Resamples to 16 kHz and converts to float32 in [-1, 1].
  4. Cuts the stream into overlapping 0.975 s frames (50% overlap).
  5. Passes every frame to handle_frame() as an AudioFrame object.
     Stage 2 replaces handle_frame() with the YAMNet classifier.

Every frame carries a timestamp worked out from a single shared sample
counter, so frames from different sensors line up exactly - which is what
the later "which sensor heard it first" step needs.

Usage examples
--------------
  python3 audio_capture.py --list-devices
  python3 audio_capture.py --device "Wireless PRO"
  python3 audio_capture.py --device "Wireless PRO" --save-wav recordings/
  python3 audio_capture.py --input test.wav --save-frames frames/

Install (Uno Q / Debian):
  sudo apt install libportaudio2
  pip install numpy scipy soxr sounddevice
"""
import argparse
import json
import queue
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

TARGET_SR = 16_000      # YAMNet sample rate
FRAME_LEN = 15_600      # 0.975 s - YAMNet TFLite input size
HOP_LEN = 7_800         # 50% overlap, so a short bang is never split badly


# --------------------------------------------------------------------------
# Data structure handed to the model
# --------------------------------------------------------------------------
@dataclass
class AudioFrame:
    sensor_id: int          # which channel / transmitter heard it
    frame_index: int        # running count per sensor
    start_time: float       # Unix time of the first sample in the frame
    sample_rate: int        # always 16000
    rms_dbfs: float         # loudness, 0 = maximum, -90 = near silence
    peak: float             # largest absolute sample value (0..1)
    samples: np.ndarray     # float32, shape (15600,), values in [-1, 1]

    def metadata(self):
        d = asdict(self)
        d.pop("samples")
        return d


def loudness_dbfs(x):
    rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))
    return round(20 * np.log10(max(rms, 1e-9)), 1)


# --------------------------------------------------------------------------
# Resampling (device rate -> 16 kHz), streaming so blocks join smoothly
# --------------------------------------------------------------------------
class Resampler:
    def __init__(self, in_rate, n_channels):
        self.in_rate = int(in_rate)
        self.passthrough = self.in_rate == TARGET_SR
        self.stream = None
        if not self.passthrough:
            try:
                import soxr
                self.stream = soxr.ResampleStream(
                    self.in_rate, TARGET_SR, n_channels, dtype="float32")
            except ImportError:
                print("soxr not installed - falling back to scipy "
                      "(small glitches at block edges).", file=sys.stderr)

    def process(self, block, last=False):
        """block: float32 array, shape (n_samples, n_channels)."""
        if self.passthrough:
            return block
        if self.stream is not None:
            return self.stream.resample_chunk(block, last=last)
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(self.in_rate, TARGET_SR)
        return resample_poly(block, TARGET_SR // g, self.in_rate // g,
                             axis=0).astype(np.float32)


# --------------------------------------------------------------------------
# Framing: continuous 16 kHz stream -> overlapping 0.975 s frames
# --------------------------------------------------------------------------
class Framer:
    def __init__(self, n_channels, stream_start_time):
        self.n_channels = n_channels
        self.t0 = stream_start_time
        self.buffer = np.zeros((0, n_channels), dtype=np.float32)
        self.consumed = 0          # samples already dropped from the buffer
        self.frame_index = 0

    def push(self, block_16k):
        self.buffer = np.concatenate([self.buffer, block_16k], axis=0)
        while len(self.buffer) >= FRAME_LEN:
            start_time = self.t0 + self.consumed / TARGET_SR
            for ch in range(self.n_channels):
                x = np.clip(self.buffer[:FRAME_LEN, ch], -1.0, 1.0).copy()
                yield AudioFrame(
                    sensor_id=ch,
                    frame_index=self.frame_index,
                    start_time=round(start_time, 4),
                    sample_rate=TARGET_SR,
                    rms_dbfs=loudness_dbfs(x),
                    peak=round(float(np.max(np.abs(x))), 4),
                    samples=x,
                )
            self.frame_index += 1
            self.buffer = self.buffer[HOP_LEN:]
            self.consumed += HOP_LEN


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------
class FrameSaver:
    """Saves each frame as .npy plus one line of metadata in frames.jsonl."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.log = open(self.folder / "frames.jsonl", "a")

    def __call__(self, frame):
        name = f"s{frame.sensor_id}_{frame.frame_index:06d}.npy"
        np.save(self.folder / name, frame.samples)
        meta = frame.metadata() | {"file": name}
        self.log.write(json.dumps(meta) + "\n")
        self.log.flush()


class WavRecorder:
    """Writes one continuous 16 kHz WAV per sensor - for collecting training data."""
    def __init__(self, folder, n_channels):
        import wave
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        self.files = []
        for ch in range(n_channels):
            w = wave.open(str(self.folder / f"{stamp}_sensor{ch}.wav"), "wb")
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TARGET_SR)
            self.files.append(w)

    def write(self, block_16k):
        pcm = (np.clip(block_16k, -1, 1) * 32767).astype("<i2")
        for ch, w in enumerate(self.files):
            w.writeframes(pcm[:, ch].tobytes())

    def close(self):
        for w in self.files:
            w.close()


def print_frame(frame):
    """Default handler: a live loudness meter per sensor.
    Stage 2 swaps this for: label, confidence = classify(frame.samples)"""
    bar = "#" * max(0, int((frame.rms_dbfs + 60) / 2))
    print(f"t={frame.start_time:.2f}  sensor {frame.sensor_id}  "
          f"frame {frame.frame_index:5d}  {frame.rms_dbfs:6.1f} dBFS  {bar}")


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------
def to_float32(block):
    """Any integer or float PCM -> float32 in [-1, 1]."""
    if block.dtype == np.int16:
        return block.astype(np.float32) / 32768.0
    if block.dtype == np.int32:
        return block.astype(np.float32) / 2147483648.0
    if block.dtype == np.uint8:
        return (block.astype(np.float32) - 128) / 128.0
    return block.astype(np.float32)


def run_file(path, handlers, recorder_dir, realtime):
    from scipy.io import wavfile
    rate, data = wavfile.read(path)
    data = to_float32(data)
    if data.ndim == 1:
        data = data[:, None]
    n_ch = data.shape[1]
    print(f"File: {path}  {rate} Hz  {n_ch} channel(s)  "
          f"{len(data) / rate:.1f} s", file=sys.stderr)

    resampler = Resampler(rate, n_ch)
    framer = Framer(n_ch, stream_start_time=time.time())
    recorder = WavRecorder(recorder_dir, n_ch) if recorder_dir else None
    block = int(rate * 0.1)
    n_frames = 0
    for i in range(0, len(data), block):
        last = i + block >= len(data)
        out = resampler.process(data[i:i + block], last=last)
        if recorder:
            recorder.write(out)
        for frame in framer.push(out):
            n_frames += 1
            for h in handlers:
                h(frame)
        if realtime:
            time.sleep(0.1)
    if recorder:
        recorder.close()
    print(f"Done: {n_frames} frames produced.", file=sys.stderr)


def run_live(device, handlers, recorder_dir, max_channels):
    try:
        import sounddevice as sd
    except OSError:
        sys.exit("PortAudio missing - run: sudo apt install libportaudio2")

    info = sd.query_devices(device, "input")
    rate = int(info["default_samplerate"])
    n_ch = min(int(info["max_input_channels"]), max_channels)
    print(f"Device: {info['name']}  {rate} Hz  {n_ch} channel(s)",
          file=sys.stderr)

    q = queue.Queue()
    overflows = 0

    def callback(indata, frames, time_info, status):
        nonlocal overflows
        if status.input_overflow:
            overflows += 1
        q.put(indata.copy())

    resampler = Resampler(rate, n_ch)
    recorder = WavRecorder(recorder_dir, n_ch) if recorder_dir else None
    framer = None
    with sd.InputStream(device=info["index"], channels=n_ch, samplerate=rate,
                        dtype="float32", blocksize=int(rate * 0.1),
                        callback=callback):
        print("Listening... Ctrl+C to stop.", file=sys.stderr)
        try:
            while True:
                block = q.get()
                if framer is None:   # timestamp anchored to first audio block
                    framer = Framer(n_ch, time.time() - len(block) / rate)
                out = resampler.process(block)
                if recorder:
                    recorder.write(out)
                for frame in framer.push(out):
                    for h in handlers:
                        h(frame)
        except KeyboardInterrupt:
            pass
    if recorder:
        recorder.close()
    if overflows:
        print(f"Warning: {overflows} buffer overflows (audio dropped). "
              f"The processing loop is too slow.", file=sys.stderr)


def find_device(name_part):
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if name_part.lower() in d["name"].lower() and d["max_input_channels"] > 0:
            return i
    sys.exit(f"No input device matching '{name_part}'. Try --list-devices.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--list-devices", action="store_true")
    p.add_argument("--device", help="part of the input device name, e.g. 'Wireless PRO'")
    p.add_argument("--input", help="WAV file to use instead of a microphone")
    p.add_argument("--realtime", action="store_true",
                   help="with --input, play the file at real speed")
    p.add_argument("--channels", type=int, default=2,
                   help="maximum channels/sensors to use (default 2)")
    p.add_argument("--save-frames", metavar="DIR",
                   help="save every frame as .npy + frames.jsonl")
    p.add_argument("--save-wav", metavar="DIR",
                   help="record continuous 16 kHz WAV per sensor (training data)")
    p.add_argument("--quiet", action="store_true", help="don't print the meter")
    a = p.parse_args()

    if a.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    handlers = []
    if not a.quiet:
        handlers.append(print_frame)
    if a.save_frames:
        handlers.append(FrameSaver(a.save_frames))

    if a.input:
        run_file(a.input, handlers, a.save_wav, a.realtime)
    else:
        device = find_device(a.device) if a.device else None
        run_live(device, handlers, a.save_wav, a.channels)


if __name__ == "__main__":
    main()
