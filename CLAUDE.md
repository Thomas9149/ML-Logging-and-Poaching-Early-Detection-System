# Forest Watch – project context

Hackathon project (Junction, Helsinki/Espoo, November 2026). Thomas and a teammate.
Thomas prefers plain-language explanations without unexplained acronyms.

## The idea
Acoustic monitoring for illegal logging and poaching. Microphones in a forest
listen continuously; a machine learning model running on the device itself
recognises chainsaws, tree falls and gunshots, then sends notifications that
rangers can subscribe to.

Pitch structure the team is using:
1. Problem: illegal logging/poaching happens deep in forests and goes
   undetected. Rangers are spread thin, and satellite images show cleared
   forest days or weeks later. (A 2012 UN Environment Programme and Interpol report
   estimated illegal logging at 30–100 billion US dollars a year, 15–30% of
   the global timber trade.)
2. Measure: microphones (plus location of each sensor).
3. Local AI: YAMNet sound classifier runs on the device – no internet needed
   in the forest; only small alerts are sent.
4. Mitigate: notifications to rangers within seconds, saved logs as evidence,
   hotspot maps over time. In a real deployment: long-range low-power radio
   (e.g. LoRa) to a base station, feeding into tools like EarthRanger.
Key pitch line: "Catch the chainsaw in minutes, not see the cleared forest
from space weeks later."

## Hardware
- Arduino UNO Q: two halves – a Debian Linux computer (Qualcomm, quad-core,
  runs Python) and an STM32 microcontroller (runs Arduino C++ sketches with
  setup()/loop()). They talk via Arduino's "Bridge" library.
  All current code runs on the Linux side (Python). The sketch side is unused
  so far (planned later: alert light/buzzer/servo).
- Microphone: Arduino USB camera TPX00246 with built-in microphone. Shows up as
  "Web Camera: USB Audio (hw:1,0), ALSA (2 in, 0 out)". The two channels are
  identical (one microphone copied twice) → treat as ONE sensor, use
  `--channels 1` (or `--channels 2` and only use sensor 0).
  Observed: level sits around -15 dBFS even in quiet → likely automatic gain
  control in the webcam. Can check with `amixer -c 1` / `alsamixer -c 1`.
- A RØDE Wireless PRO (two wireless transmitters, USB-C receiver) was
  discussed as an alternative giving two separate sensors, but they are using
  the webcam.
- The UNO Q has one USB-C port, used for power and data. A USB-C hub with power
  pass-through is needed to power the board and attach the camera at once.

## Board access (current setup)
- Board name: DETECTIONSYSTEM, user `arduino`, password set in Arduino App Lab
  first-time setup. SSH: `ssh arduino@DETECTIONSYSTEM.local` or by IP.
- On an iPhone hotspot, devices get 172.20.10.2–14; the phone is 172.20.10.1.
  `.local` names and App Lab's network discovery are unreliable on the hotspot;
  use the IP.
- App Lab only finds the board reliably over USB to the laptop. On wall power it
  must find it over Wi-Fi (often fails) – SSH/VS Code Remote-SSH is the main
  workflow instead.
- Wi-Fi drops have been a problem (iPhone hotspot at a crowded venue). Applied /
  suggested fixes on the board:
  `sudo nmcli connection modify "<hotspot>" connection.autoconnect yes connection.autoconnect-retries 0 802-11-wireless.powersave 2`
  plus a root crontab watchdog:
  `* * * * * ping -c1 -W3 172.20.10.1 >/dev/null || nmcli connection up "<hotspot>"`
  Other options: Android hotspot, Mac Internet Sharing (iPhone via USB → Mac
  shares over Wi-Fi), run long jobs inside `tmux`.
- Project folder on the board: `/home/arduino/forest-watch` (next to
  `ArduinoApps/`, which App Lab owns – leave it alone).
- Currently moving development to the Mac first (Apple Silicon, needs
  Python ≥ 3.10, e.g. `brew install python@3.12`), then deploying to the board
  with git.

## Setup
On the Mac the project lives in `~/Documents/Arduino/forest/` with its own
`.venv/` (Python 3.13), `models/` and `requirements.txt` all inside it; run
scripts from that folder. `pip install -r requirements.txt` installs the
exact package versions.

Mac:
```
cd ~/forest-watch
python3.12 -m venv .venv && source .venv/bin/activate
pip install numpy scipy soxr sounddevice ai-edge-litert
```
Board (Debian):
```
sudo apt install -y python3-venv libportaudio2 tmux
cd ~/forest-watch && python3 -m venv .venv && source .venv/bin/activate
pip install numpy scipy soxr sounddevice ai-edge-litert
```
Model (both):
```
mkdir -p models
curl -L -o models/yamnet.tflite https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite
curl -L -o models/yamnet_class_map.csv https://raw.githubusercontent.com/tensorflow/models/master/research/audioset/yamnet/yamnet_class_map.csv
```
On macOS, Terminal/VS Code needs microphone permission (System Settings →
Privacy & Security → Microphone) or recordings are silent.
`.gitignore`: `.venv/ logs/ recordings/ frames/ __pycache__/`

## Code – pipeline
```
microphone → audio_capture.py → classifier.py → incidents.py → notifications
             (16 kHz frames)    (YAMNet scores)  (probabilities)  (screen/log/ntfy)
```

### audio_capture.py (stage 1)
- Reads a USB microphone via `sounddevice` (or a WAV file with `--input`).
- Keeps channels separate (each channel = a "sensor"), resamples to 16 kHz with
  `soxr` streaming resampler, float32 in [-1, 1].
- Cuts into YAMNet-sized frames: 15,600 samples (0.975 s), hop 7,800 (50%
  overlap) → a frame about every 0.49 s; ~0.5–1 s latency.
- Each frame is an `AudioFrame(sensor_id, frame_index, start_time, sample_rate,
  rms_dbfs, peak, samples)`. Timestamps come from one shared sample counter,
  so sensors line up exactly (needed later for "which sensor heard it first").
- Handlers receive frames: default prints a loudness meter; `--save-frames DIR`,
  `--save-wav DIR` (training data), `--list-devices`, `--device "<name part>"`,
  `--channels N`.
- Exposes `run_live(device, handlers, recorder_dir, max_channels)`,
  `run_file(path, handlers, recorder_dir, realtime)`, `find_device(name)`.

### classifier.py (stage 2)
- `YamnetClassifier(model_dir)` loads `models/yamnet.tflite` with
  `ai_edge_litert` (falls back to tflite_runtime / tensorflow.lite), finds the
  521-class output automatically, reshapes input to 15,600 samples.
- `classify(samples, top_k=3)` → (top YAMNet labels, category scores).
- YAMNet is multi-label: independent 0–1 score per class (not summing to 1).
- Category = max score over grouped YAMNet class indices:
  - chainsaw: 341 Chainsaw, 415 Sawing, 418 Power tool (added for max sensitivity)
  - chopping: 432 Chop, 431 Wood (axes)
  - tree_fall: 480 Creak, 434 Crack, 464 Breaking, 463 Smash/crash, 454 Thump/thud
  - gunshot: 421 Gunshot/gunfire, 420 Explosion
  - vehicle: 294 Vehicle, 300 Motor vehicle, 310 Truck, 320 Motorcycle, 343 Heavy engine
- YAMNet has no "tree falling" class; tree_fall is built from these components
  and is mainly useful as confirmation after chainsawing.
- Future option: use YAMNet's 1,024-number embeddings + own small classifier
  trained on own clips (needs the Kaggle YAMNet variant that outputs embeddings).

### chainsaw_test.py (simple test tool)
- Chainsaw only. One line per frame: `background  chainsaw 0.04` or
  `CHAINSAW  chainsaw 0.72  <<< ALERT` (threshold 0.3, `--threshold`).
- `python chainsaw_test.py --device "Web Camera" --channels 1`

### detect.py (earlier per-frame detector, kept for reference)
- Per-category hit rules (e.g. chainsaw 2 of last 3 frames, gunshot/tree_fall
  1 frame), levels URGENT/CONFIRMED/WARNING/INFO, `--quiet`, `--top N`,
  `--min-score`. Superseded by incidents.py + run.py.

### incidents.py (stage 3 – incident engine)
- Input: `Observation(time, sensor_id, category, score)` for frames where a
  category scored ≥ 0.3. Also `engine.tick(t)` every frame so probabilities
  fade when nothing is heard.
- Evidence fades with age and combines via noisy-OR:
  P = 1 − Π(1 − weight · score · e^(−age/decay)).
- Notifications (`Notification(type, level, probability, time, sensors,
  message, evidence)`):
  - CHAINSAWING (HIGH): MAX SENSITIVITY (demo setting) – ONE frame with
    chainsaw score ≥ 0.1 triggers (weight 1.0, on 0.1, off 0.05, decay 20 s,
    window 60 s). Repeats every 10 s while the saw is still heard
    (`chainsawing_repeat_s`, run.py `--chainsaw-repeat N`, 0 = once per
    episode); re-arms for a fresh episode after ~35–60 s of silence. Earlier: weight 0.35/on 0.8/off 0.3 (~7 frames), then 0.6 (~4).
  - LOGGING (URGENT): tree fall within 10 min of a CHAINSAWING notification;
    P = cutting memory (fades over ~5 min) × min(1, fall score / 0.6) ≥ 0.5.
    Tree fall without prior chainsawing sends nothing.
  - GUNSHOT (HIGH): each distinct shot with score ≥ 0.3. Observations < 1.5 s
    apart are one shot (frames overlap).
  - POACHING (URGENT): ≥ 2 shots within 10 min, noisy-OR of 0.6·score ≥ 0.6,
    10 min cooldown.
- All thresholds in `DEFAULTS` at the top of the file. Hand-tuned, not trained;
  logs could later be used to fit them.
- Subscribing: `engine.subscribe(callback, types={"POACHING"})` (None = all).
- Outputs: `console_printer`, `JsonlLog(path)`, `NtfyNotifier(prefix)` which
  posts in a background thread to ntfy topics `<prefix>-chainsawing`,
  `-logging`, `-gunshot`, `-poaching`, and `<prefix>-all`. Rangers subscribe
  in the ntfy phone app to the topics they want. Uses `certifi`'s
  certificates (python.org's macOS Python has none → HTTPS posts failed).
- LOGGING memory starts from the LAST chainsaw frame of a confirmed episode
  (so a long sawing session still counts); one LOGGING per 60 s (one per tree).
- `--demo --speed 10` plays the story at 10x real time (0 = instant);
  `--quiet` hides explanations of ignored events.
- `python incidents.py --demo [--ntfy-prefix forestwatch-team42]` runs a
  scripted story (lone tree creak ignored → CHAINSAWING → LOGGING → GUNSHOT →
  GUNSHOT + POACHING). `--replay logs/observations.jsonl` replays a session.

### run.py (full live pipeline)
- Mic → YAMNet → observations (chainsaw, gunshot, tree_fall ≥ `--obs-threshold`
  0.3; chainsaw ≥ `--chainsaw-threshold` 0.1) → engine → console + `logs/notifications.jsonl` (+ ntfy if
  `--ntfy-prefix`). Observations logged to `logs/observations.jsonl`.
- `--show-frames` prints per-frame scores and live P(chainsawing).
  `--explain` prints why tree falls/shots did not trigger. `--input x.wav`
  tests with a recording.
- A tree_fall observation is skipped when the chainsaw score in the same
  frame is higher (a running saw can light up crack/breaking classes).
- `python run.py --device "Web Camera" --channels 1 --ntfy-prefix forestwatch-team42 --show-frames`

### phone_interface/ (ranger phone app)
- Web app (plain HTML/CSS/JavaScript, no build step) in `phone_interface/app/`,
  opened in iPhone Safari and added to the Home Screen so it runs full screen.
  Its own `.venv` (Python 3.13) only runs `serve.py`, which serves the files on
  the local network and prints a QR code (`qrcode` package).
  `python serve.py --prefix forestwatch-team42` → QR presets the topic.
- Subscribes directly to the ntfy topics `<prefix>-chainsawing/-logging/
  -gunshot/-poaching` over a WebSocket (`wss://ntfy.sh/<topics>/ws?since=…`),
  catching up on missed messages (last message id saved; first time: 12 h).
  Parses type from the topic, P from the title (`P=98%`), sensors from the body.
- Alerts only arrive while the app is open (no lock-screen push – rangers use
  the ntfy app for that). Banner + beep (Web Audio, needs one tap to unlock;
  iPhone silent switch mutes it). "Keep screen on" toggle uses Wake Lock.
- Tabs: Alerts (latest alert + timestamped event log with type filters),
  Forest (draggable/pinch-zoom SVG map: meadow at the top, forest below,
  THREE mock microphones – Mic 1 in the meadow, Mic 2 and Mic 3 in the forest –
  forming a dashed coverage triangle; `MICS` in map.js), Settings (per-type subscriptions, topic
  prefix, server, sound, local test alerts + "Play demo story", clear log).
  `#forest` / `#settings` in the URL open that tab.
- Forest map details: markers sit at MOCK locations inside the microphone
  triangle, made up from the event id (logging placed near the chainsawing,
  poaching near the gunshots). Tapping an event shows a MOCK triangulation:
  arrival delay at each mic = distance / 343 m/s; a circle grows from each mic
  (in order of arrival) with radius = its distance, the circles meet at the
  source, plus an accuracy circle (wider for low P). Mic labels show
  "heard first" / "+1.83 s"; the details sheet lists the same. The real version
  would compute the position from real arrival times (needs 3 sensors with a
  shared clock) – the Arduino currently has one microphone (sensor 0).
- Grouping: alerts of the same type < 30 s apart (`GROUP_GAP_MS` in app.js)
  are one group – every alert is still received and stored, but the log shows
  one row "Chainsawing ×5 · since 10:01:02 · peak P", the map one marker, and
  the banner updates its "×N" count (still beeps each time).
- Settings and the event log live in the phone's browser storage.
- `make_icons.py` redraws the Home Screen icons (standard library only).
- Tested in headless Chrome at iPhone size against real ntfy.sh messages from
  `incidents.py --demo`; not yet on a real iPhone.

## Testing status
- Pipeline tested end to end with a stand-in model of the same input/output
  shape (the real YAMNet download was not possible in the environment where
  the code was written). The first run with the real model happens on the
  Mac/board.
- run.py loads the real YAMNet on the Mac and runs end to end on a WAV file
  (November 2026); not yet tested with real chainsaw/gunshot audio.
- Incident engine scenario tests passed (see incidents.py --demo); ntfy request
  format verified against a local test server and against ntfy.sh (November
  2026: incidents.py --demo → ntfy.sh → phone app received all 5 alerts).

## Next steps / ideas
- Verify the real YAMNet on the Mac: chainsaw video vs speech at same volume.
- Tune thresholds with real recordings (`--save-wav`, replay logs).
- Arduino sketch side: light/buzzer/servo triggered on notifications via Bridge.
- Optional: second microphone for two sensors / direction ("which heard it
  first" – needs shared clock; one board with two mics avoids clock sync).
- Dashboard/map for the demo; QR code for judges to subscribe to the ntfy topic.
- Demo plan: play chainsaw clip → CHAINSAWING (phones buzz) → tree-fall clip →
  LOGGING; gunshot clips → GUNSHOT → POACHING.
