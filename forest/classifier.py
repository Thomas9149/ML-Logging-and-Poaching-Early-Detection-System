"""
Stage 2 - YAMNet sound classifier.

Takes one AudioFrame's samples (15,600 float32 values at 16 kHz) and returns
  * the top YAMNet labels (out of 521 sound classes), and
  * a score for each of OUR categories: chainsaw, gunshot, vehicle.

Files needed in models/ (see README steps):
  models/yamnet.tflite
  models/yamnet_class_map.csv

Install:  pip install ai-edge-litert
"""
import csv
from pathlib import Path

import numpy as np

# YAMNet classes grouped into the categories we care about.
# Numbers are row indices in yamnet_class_map.csv.
CATEGORIES = {
    "chainsaw":  [341, 415, 418],                # Chainsaw, Sawing, Power tool
    "chopping":  [432, 431],                     # Chop, Wood (axes)
    "tree_fall": [480, 434, 464, 463, 454],      # Creak, Crack, Breaking,
                                                 # Smash/crash, Thump/thud
    "gunshot":   [421, 420],                     # Gunshot/gunfire, Explosion
    "vehicle":   [294, 300, 310, 320, 343],      # Vehicle, Motor vehicle, Truck,
                                                 # Motorcycle, Heavy engine
}


def _load_interpreter(model_path):
    """Use whichever TensorFlow Lite runtime is installed."""
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:
        try:
            from tflite_runtime.interpreter import Interpreter
        except ImportError:
            from tensorflow.lite import Interpreter
    return Interpreter(model_path=str(model_path), num_threads=4)


class YamnetClassifier:
    def __init__(self, model_dir="models"):
        model_dir = Path(model_dir)
        self.labels = self._load_labels(model_dir / "yamnet_class_map.csv")
        self.interp = _load_interpreter(model_dir / "yamnet.tflite")

        inp = self.interp.get_input_details()[0]
        self.input_index = inp["index"]
        self.input_shape = tuple(inp["shape"])
        # Some YAMNet builds accept any length; fix it to one frame.
        if int(np.prod(self.input_shape)) != 15600:
            shape = [15600] if len(self.input_shape) == 1 else [1, 15600]
            self.interp.resize_tensor_input(self.input_index, shape)
            self.input_shape = tuple(shape)
        self.interp.allocate_tensors()

        # Find the output that holds the 521 class scores.
        self.scores_index = None
        for out in self.interp.get_output_details():
            if out["shape"][-1] == len(self.labels):
                self.scores_index = out["index"]
                break
        if self.scores_index is None:
            raise RuntimeError("Could not find a 521-class output in the model.")

    @staticmethod
    def _load_labels(path):
        with open(path, newline="") as f:
            return [row["display_name"] for row in csv.DictReader(f)]

    def scores(self, samples):
        """samples: float32 array of 15,600 values in [-1, 1] -> 521 scores (0..1)."""
        x = np.asarray(samples, dtype=np.float32).reshape(self.input_shape)
        self.interp.set_tensor(self.input_index, x)
        self.interp.invoke()
        out = self.interp.get_tensor(self.scores_index)
        return out.reshape(-1, len(self.labels)).max(axis=0)

    def classify(self, samples, top_k=3):
        s = self.scores(samples)
        top = [(self.labels[i], float(s[i])) for i in np.argsort(s)[::-1][:top_k]]
        cats = {name: float(s[idx].max()) for name, idx in CATEGORIES.items()}
        return top, cats


if __name__ == "__main__":
    # Quick self-test: classify a WAV file frame by frame.
    import sys
    from scipy.io import wavfile
    if len(sys.argv) < 2:
        sys.exit("usage: python classifier.py some_16khz_mono.wav")
    clf = YamnetClassifier()
    rate, data = wavfile.read(sys.argv[1])
    if data.ndim > 1:
        data = data[:, 0]
    data = data.astype(np.float32) / (32768.0 if data.dtype == np.int16 else 1.0)
    if rate != 16000:
        sys.exit(f"File is {rate} Hz - run it through audio_capture.py "
                 f"--input ... --save-wav out/ first to convert to 16 kHz.")
    for start in range(0, len(data) - 15600 + 1, 7800):
        top, cats = clf.classify(data[start:start + 15600])
        print(f"{start / 16000:6.2f}s  " +
              "  ".join(f"{n} {p:.2f}" for n, p in top))
