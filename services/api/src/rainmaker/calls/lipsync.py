"""Wav2Lip: moving her mouth to the audio she is actually saying.

WHY THIS EXISTS RATHER THAN A HOSTED PROVIDER. The face was a still, lit by the output level and
honest about not lip-syncing, and the argument for that is in `avatar.py` and still true as far
as it goes. It is also not what a talking head is. This runs a real lip-sync model on the local
GPU, on the portrait, driven by the same Kokoro audio the browser is about to play.

WHY IT IS FAST ENOUGH TO BE IN A CONVERSATION, which MuseTalk is not on this hardware:

  * THE FACE NEVER CHANGES. A video pipeline runs face detection per frame; there is one
    photograph here, so the crop is computed once at import and reused forever. That removes
    s3fd — a 90MB model and most of the per-frame cost — from the loop entirely.
  * IT RUNS PER CLAUSE, NOT PER REPLY. Synthesis already streams clause by clause, so a clip is
    one to three seconds of audio, which is 25 to 75 frames — one batched forward pass.
  * ONLY THE MOUTH IS GENERATED. Wav2Lip works on a 96x96 crop; the rest of the portrait is the
    original pixels. What travels to the browser is a small patch, not a frame of video.

NO LIBROSA. Wav2Lip's reference implementation pulls in librosa for one mel spectrogram, and the
version that matches its constants is old enough to fight with everything else installed. The mel
here is the same computation — preemphasis, STFT, mel filterbank, dB, normalise — written against
numpy and scipy, and checked against the constants below rather than against an import.

THE LICENCE IS NOT APACHE. Wav2Lip's weights are released for research and personal use, not for
commercial use. That is a genuine difference from everything else in this repository and it is
why this is an opt-in extra with a fetch step rather than something a clone downloads: see
`scripts/fetch-lipsync.py` and the note in the README.
"""

from __future__ import annotations

import io
import logging
import os
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger("rainmaker.calls.lipsync")

MODEL_DIR = Path(os.environ.get("RAINMAKER_MODELS", Path(__file__).resolve().parents[3] / "models"))
CHECKPOINT = MODEL_DIR / "wav2lip_gan.pth"

#: The portrait that gets a moving mouth. The console's copy is the source of truth; this is the
#: same file, read server-side.
PORTRAIT_FILE = Path(
    os.environ.get(
        "RAINMAKER_PORTRAIT_FILE",
        Path(__file__).resolve().parents[5] / "apps" / "console" / "public" / "agent" / "liv.jpg",
    )
)

#: Wav2Lip's constants. Not tunable — they are what the checkpoint was trained against, and the
#: mel it sees has to be the mel it learned on or the mouth moves to the wrong sounds.
SAMPLE_RATE = 16_000
N_FFT = 800
HOP = 200
WIN = 800
N_MELS = 80
F_MIN, F_MAX = 55.0, 7600.0
PREEMPHASIS = 0.97
REF_LEVEL_DB, MIN_LEVEL_DB, MAX_ABS = 20.0, -100.0, 4.0

#: Video frames per second, and the mel width one frame sees. Wav2Lip is trained at 25fps with a
#: 16-frame mel window; both are part of the checkpoint, not preferences.
FPS = 25
MEL_STEP = 16

#: The generated patch. Larger looks better and is not what the model produces.
FACE_SIZE = 96


# ───────────────────────────────────────────────────────────── the model
def _build_model() -> Any:
    """The Wav2Lip generator, reconstructed to match the checkpoint's state dict exactly.

    Written out rather than imported because the reference repository is a clone-and-patch
    affair — old torch, old librosa, a `pip install` that fights with the rest of this
    environment. The shapes below were read straight out of the checkpoint.
    """
    import torch
    from torch import nn

    class Conv2d(nn.Module):
        def __init__(self, cin, cout, kernel_size, stride, padding, residual=False):
            super().__init__()
            self.conv_block = nn.Sequential(
                nn.Conv2d(cin, cout, kernel_size, stride, padding), nn.BatchNorm2d(cout)
            )
            self.act = nn.ReLU()
            self.residual = residual

        def forward(self, x):
            out = self.conv_block(x)
            if self.residual:
                out = out + x
            return self.act(out)

    class Conv2dTranspose(nn.Module):
        def __init__(self, cin, cout, kernel_size, stride, padding, output_padding=0):
            super().__init__()
            self.conv_block = nn.Sequential(
                nn.ConvTranspose2d(cin, cout, kernel_size, stride, padding, output_padding),
                nn.BatchNorm2d(cout),
            )
            self.act = nn.ReLU()

        def forward(self, x):
            return self.act(self.conv_block(x))

    class Wav2Lip(nn.Module):
        def __init__(self):
            super().__init__()
            self.face_encoder_blocks = nn.ModuleList([
                nn.Sequential(Conv2d(6, 16, 7, 1, 3)),
                nn.Sequential(Conv2d(16, 32, 3, 2, 1),
                              Conv2d(32, 32, 3, 1, 1, residual=True),
                              Conv2d(32, 32, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2d(32, 64, 3, 2, 1),
                              Conv2d(64, 64, 3, 1, 1, residual=True),
                              Conv2d(64, 64, 3, 1, 1, residual=True),
                              Conv2d(64, 64, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2d(64, 128, 3, 2, 1),
                              Conv2d(128, 128, 3, 1, 1, residual=True),
                              Conv2d(128, 128, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2d(128, 256, 3, 2, 1),
                              Conv2d(256, 256, 3, 1, 1, residual=True),
                              Conv2d(256, 256, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2d(256, 512, 3, 2, 1),
                              Conv2d(512, 512, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2d(512, 512, 3, 1, 0), Conv2d(512, 512, 1, 1, 0)),
            ])
            self.audio_encoder = nn.Sequential(
                Conv2d(1, 32, 3, 1, 1),
                Conv2d(32, 32, 3, 1, 1, residual=True),
                Conv2d(32, 32, 3, 1, 1, residual=True),
                Conv2d(32, 64, 3, (3, 1), 1),
                Conv2d(64, 64, 3, 1, 1, residual=True),
                Conv2d(64, 64, 3, 1, 1, residual=True),
                Conv2d(64, 128, 3, 3, 1),
                Conv2d(128, 128, 3, 1, 1, residual=True),
                Conv2d(128, 128, 3, 1, 1, residual=True),
                Conv2d(128, 256, 3, (3, 2), 1),
                Conv2d(256, 256, 3, 1, 1, residual=True),
                Conv2d(256, 512, 3, 1, 0),
                Conv2d(512, 512, 1, 1, 0),
            )
            self.face_decoder_blocks = nn.ModuleList([
                nn.Sequential(Conv2d(512, 512, 1, 1, 0)),
                nn.Sequential(Conv2dTranspose(1024, 512, 3, 1, 0),
                              Conv2d(512, 512, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2dTranspose(1024, 512, 3, 2, 1, output_padding=1),
                              Conv2d(512, 512, 3, 1, 1, residual=True),
                              Conv2d(512, 512, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2dTranspose(768, 384, 3, 2, 1, output_padding=1),
                              Conv2d(384, 384, 3, 1, 1, residual=True),
                              Conv2d(384, 384, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2dTranspose(512, 256, 3, 2, 1, output_padding=1),
                              Conv2d(256, 256, 3, 1, 1, residual=True),
                              Conv2d(256, 256, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2dTranspose(320, 128, 3, 2, 1, output_padding=1),
                              Conv2d(128, 128, 3, 1, 1, residual=True),
                              Conv2d(128, 128, 3, 1, 1, residual=True)),
                nn.Sequential(Conv2dTranspose(160, 64, 3, 2, 1, output_padding=1),
                              Conv2d(64, 64, 3, 1, 1, residual=True),
                              Conv2d(64, 64, 3, 1, 1, residual=True)),
            ])
            self.output_block = nn.Sequential(
                Conv2d(80, 32, 3, 1, 1), nn.Conv2d(32, 3, 1, 1, 0), nn.Sigmoid()
            )

        def forward(self, audio_sequences, face_sequences):
            audio_embedding = self.audio_encoder(audio_sequences)

            feats = []
            x = face_sequences
            for block in self.face_encoder_blocks:
                x = block(x)
                feats.append(x)

            x = audio_embedding
            for block in self.face_decoder_blocks:
                x = block(x)
                # The skip connection. A mismatch here is silent and produces a smeared mouth,
                # so it is asserted rather than trusted.
                skip = feats.pop()
                if x.shape[2:] != skip.shape[2:]:
                    raise RuntimeError(f"decoder {tuple(x.shape)} vs skip {tuple(skip.shape)}")
                x = torch.cat((x, skip), dim=1)
            return self.output_block(x)

    return Wav2Lip()


# ───────────────────────────────────────────────────────────── the mel
@lru_cache(maxsize=1)
def _mel_basis() -> np.ndarray:
    """Slaney-style mel filterbank, matching what Wav2Lip trained against."""
    def hz_to_mel(hz):
        return 2595.0 * np.log10(1.0 + hz / 700.0)

    def mel_to_hz(mel):
        return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)

    n_freqs = N_FFT // 2 + 1
    fft_freqs = np.linspace(0, SAMPLE_RATE / 2, n_freqs)
    mel_points = np.linspace(hz_to_mel(F_MIN), hz_to_mel(F_MAX), N_MELS + 2)
    hz_points = mel_to_hz(mel_points)

    basis = np.zeros((N_MELS, n_freqs), dtype=np.float32)
    for i in range(N_MELS):
        left, centre, right = hz_points[i], hz_points[i + 1], hz_points[i + 2]
        rising = (fft_freqs - left) / max(centre - left, 1e-9)
        falling = (right - fft_freqs) / max(right - centre, 1e-9)
        basis[i] = np.maximum(0.0, np.minimum(rising, falling))
        # Slaney normalisation: equal AREA per filter, not equal peak. librosa's default, and
        # getting it wrong scales the whole spectrogram and shifts what the model hears.
        basis[i] *= 2.0 / max(right - left, 1e-9)
    return basis


def melspectrogram(wav: np.ndarray) -> np.ndarray:
    """Wav2Lip's mel, without librosa. Returns (80, frames), normalised to [-4, 4]."""
    from scipy.signal import get_window

    if wav.size == 0:
        return np.zeros((N_MELS, 0), dtype=np.float32)
    emphasised = np.append(wav[0], wav[1:] - PREEMPHASIS * wav[:-1]).astype(np.float32)

    # Centred STFT with reflect padding, which is what librosa does by default and what the
    # frame count downstream assumes.
    padded = np.pad(emphasised, N_FFT // 2, mode="reflect")
    window = get_window("hann", WIN, fftbins=True).astype(np.float32)
    frames = 1 + (len(padded) - N_FFT) // HOP
    if frames <= 0:
        return np.zeros((N_MELS, 0), dtype=np.float32)

    strided = np.lib.stride_tricks.as_strided(
        padded,
        shape=(frames, N_FFT),
        strides=(padded.strides[0] * HOP, padded.strides[0]),
    )
    spectrum = np.abs(np.fft.rfft(strided * window, n=N_FFT, axis=1)).T.astype(np.float32)

    mel = _mel_basis() @ spectrum
    db = 20.0 * np.log10(np.maximum(1e-5, mel)) - REF_LEVEL_DB
    normalised = np.clip(
        (2 * MAX_ABS) * ((db - MIN_LEVEL_DB) / -MIN_LEVEL_DB) - MAX_ABS, -MAX_ABS, MAX_ABS
    )
    return normalised.astype(np.float32)


def _resample_to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    if rate == SAMPLE_RATE:
        return samples.astype(np.float32)
    # Linear interpolation. Kokoro's 24kHz to 16kHz is a 2:3 ratio on a signal already band
    # limited well below Nyquist, so a polyphase filter buys nothing a mel spectrogram can see.
    duration = len(samples) / rate
    target = int(round(duration * SAMPLE_RATE))
    if target <= 1:
        return np.zeros(0, dtype=np.float32)
    source_x = np.linspace(0.0, duration, len(samples), endpoint=False)
    target_x = np.linspace(0.0, duration, target, endpoint=False)
    return np.interp(target_x, source_x, samples).astype(np.float32)


# ───────────────────────────────────────────────────────────── the face
@dataclass(slots=True)
class FaceBox:
    """Where the mouth region sits in the portrait.

    FIXED, AND THAT IS THE WHOLE PERFORMANCE ARGUMENT. A video pipeline detects a face per frame
    because the face moves; there is one photograph here, so this is measured once and the 90MB
    s3fd detector never enters the process.
    """

    left: int
    top: int
    right: int
    bottom: int

    @property
    def size(self) -> tuple[int, int]:
        return self.right - self.left, self.bottom - self.top


#: Measured against `liv.jpg` (512x512): the lower half of the face, which is what Wav2Lip
#: regenerates. Expressed as fractions so a replacement portrait of another size still lands.
FACE_FRACTIONS = (0.30, 0.36, 0.86, 0.92)


def face_box(width: int, height: int) -> FaceBox:
    left, top, right, bottom = FACE_FRACTIONS
    return FaceBox(
        int(left * width), int(top * height), int(right * width), int(bottom * height)
    )


class LipSync:
    """Generates mouth patches for a clip of audio. One instance, loaded once, shared."""

    def __init__(self, checkpoint: Path = CHECKPOINT, portrait: Path = PORTRAIT_FILE):
        self.checkpoint = checkpoint
        self.portrait = portrait
        self._model: Any = None
        self._device = "cpu"
        self._base: np.ndarray | None = None
        self._box: FaceBox | None = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        from importlib.util import find_spec

        return (
            self.checkpoint.exists()
            and self.portrait.exists()
            and find_spec("torch") is not None
            and find_spec("cv2") is not None
        )

    @property
    def ready(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None or not self.available:
            return
        with self._lock:
            if self._model is not None:
                return
            import cv2
            import torch

            state = torch.load(self.checkpoint, map_location="cpu", weights_only=False)
            weights = state["state_dict"] if "state_dict" in state else state
            # The published checkpoint was saved from a DataParallel wrapper.
            weights = {k.replace("module.", "", 1): v for k, v in weights.items()}

            model = _build_model()
            model.load_state_dict(weights)
            model.eval()
            self._device = "cuda" if torch.cuda.is_available() else "cpu"
            self._model = model.to(self._device)

            image = cv2.imread(str(self.portrait))
            if image is None:
                raise RuntimeError(f"could not read the portrait at {self.portrait}")
            self._base = image
            self._box = face_box(image.shape[1], image.shape[0])
            self._warm()
            log.info("lip-sync ready on %s, face box %s", self._device, self._box)

    def _warm(self) -> None:
        """One throwaway pass, so a live call does not pay for cold CUDA kernels.

        Measured: the first generation took 1225ms for 875ms of audio — slower than realtime —
        and every one after it ran at 17x. That is entirely kernel compilation, and paying it
        on the prospect's opening line is the worst available place for it.
        """
        try:
            silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
            self.frames_for(silence, SAMPLE_RATE)
        except Exception:  # noqa: BLE001 — warming is best effort
            log.debug("lip-sync warm-up failed; the first clip will be slower", exc_info=True)

    def frames_for(self, samples: np.ndarray, rate: int) -> list[np.ndarray]:
        """Mouth patches for one clip of audio, at `FPS`, as BGR crops.

        Returns the PATCH rather than a whole frame: the console holds the portrait already, so
        sending the untouched 90% of it 25 times a second would be most of the bandwidth for
        none of the information.
        """
        if self._model is None:
            self.load()
        if self._model is None or self._base is None or self._box is None:
            return []

        import cv2
        import torch

        mel = melspectrogram(_resample_to_16k(samples, rate))
        if mel.shape[1] < MEL_STEP:
            return []

        # One mel window per video frame, stepping at the audio rate that corresponds to 25fps.
        duration = len(samples) / rate
        wanted = max(1, int(round(duration * FPS)))
        per_frame = mel.shape[1] / max(duration * FPS, 1)

        # PADDED AT THE END, OR THE LAST FRAMES DO NOT EXIST. A window is sixteen mel columns
        # wide, so the final fifteen columns cannot start one and the clip comes up short —
        # measured at 71 frames for a three-second clause instead of 75. The visible symptom is
        # her mouth freezing for the last sixth of a second of every clause, which reads as the
        # model giving up. Padded with the floor value rather than by repeating: the floor is
        # silence, and a clause ending in a closed mouth is what a clause ending sounds like.
        padded = np.pad(
            mel, ((0, 0), (0, MEL_STEP)), mode="constant", constant_values=-MAX_ABS
        )

        chunks = []
        for index in range(wanted):
            start = int(index * per_frame)
            if start + MEL_STEP > padded.shape[1]:
                break
            chunks.append(padded[:, start : start + MEL_STEP])
        if not chunks:
            return []

        crop = cv2.resize(
            self._base[self._box.top : self._box.bottom, self._box.left : self._box.right],
            (FACE_SIZE, FACE_SIZE),
        )
        # The model's two inputs are the same crop twice: once masked (what to fill in) and once
        # whole (what the face looks like). The lower half is what it regenerates.
        masked = crop.copy()
        masked[FACE_SIZE // 2 :] = 0
        stacked = np.concatenate((masked, crop), axis=2) / 255.0

        faces = np.repeat(stacked[None], len(chunks), axis=0)
        mels = np.asarray(chunks)[:, None]

        with torch.no_grad():
            face_t = torch.from_numpy(faces.transpose(0, 3, 1, 2)).float().to(self._device)
            mel_t = torch.from_numpy(mels).float().to(self._device)
            generated = self._model(mel_t, face_t)
            out = (generated.cpu().numpy().transpose(0, 2, 3, 1) * 255.0).astype(np.uint8)

        width, height = self._box.size
        return [cv2.resize(frame, (width, height)) for frame in out]

    def frames_for_wav(self, wav: bytes) -> list[np.ndarray]:
        """Mouth patches for a synthesised clip, straight from its WAV bytes."""
        import wave as wave_module

        if not wav:
            return []
        with wave_module.open(io.BytesIO(wav)) as handle:
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return self.frames_for(samples, rate)

    @staticmethod
    def encode(frames: list[np.ndarray], quality: int = 80) -> list[str]:
        """Patches as base64 JPEGs, at the model's own resolution.

        NATIVE 96x96, NOT UPSCALED HERE. Wav2Lip generates at 96 and enlarging server-side adds
        nothing but bytes — the browser scales it to the same pixels either way, and a clip's
        worth of frames goes from a megabyte to about two hundred kilobytes.
        """
        import base64

        import cv2

        out = []
        for frame in frames:
            small = cv2.resize(frame, (FACE_SIZE, FACE_SIZE), interpolation=cv2.INTER_AREA)
            ok, buffer = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if ok:
                out.append(base64.b64encode(buffer.tobytes()).decode())
        return out

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "ready": self.ready,
            "device": self._device,
            "fps": FPS,
            "checkpoint": self.checkpoint.name,
            "box": list(FACE_FRACTIONS),
        }
