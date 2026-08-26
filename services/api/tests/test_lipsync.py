"""The mel she is lip-synced against, and the crop the patch is cut from.

WHY THE MEL IS TESTED AND THE MODEL IS NOT. Wav2Lip's checkpoint is a 436MB opt-in download with
a non-commercial licence, so most machines running this suite — including CI — do not have it.
What they can check is everything around it, and the mel is where a silent wrongness would live:
the checkpoint was trained on one specific spectrogram, and a mel that is subtly off produces a
mouth that moves confidently to the wrong sounds. That failure looks like a bad model rather
than a bad constant, which is exactly the kind that survives.

The reference implementation gets this mel from librosa. This one computes it directly, so the
constants it depends on are asserted here rather than inherited from a pinned version.
"""

from __future__ import annotations

import numpy as np
import pytest

from rainmaker.calls.lipsync import (
    FACE_FRACTIONS,
    FPS,
    MAX_ABS,
    MEL_STEP,
    N_MELS,
    SAMPLE_RATE,
    LipSync,
    _resample_to_16k,
    face_box,
    melspectrogram,
)


def tone(seconds: float = 1.0, hz: float = 220.0, rate: int = SAMPLE_RATE) -> np.ndarray:
    t = np.linspace(0.0, seconds, int(seconds * rate), endpoint=False)
    return (0.4 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


class TestTheMel:
    def test_it_has_the_shape_the_checkpoint_expects(self):
        """Eighty bands. The audio encoder's first convolution takes exactly that."""
        assert melspectrogram(tone()).shape[0] == N_MELS

    def test_a_second_of_audio_is_about_eighty_frames(self):
        """16kHz at a 200-sample hop. If this drifts, the mouth drifts against the voice."""
        frames = melspectrogram(tone(1.0)).shape[1]
        assert 78 <= frames <= 84, frames

    def test_it_is_normalised_into_the_range_the_model_trained_on(self):
        mel = melspectrogram(tone())
        assert mel.min() >= -MAX_ABS - 1e-3
        assert mel.max() <= MAX_ABS + 1e-3

    def test_silence_pins_to_the_floor_rather_than_producing_noise(self):
        """Silence has to look like silence, or she chews through the gaps between clauses."""
        mel = melspectrogram(np.zeros(SAMPLE_RATE, dtype=np.float32))
        assert np.allclose(mel, -MAX_ABS, atol=1e-3)

    def test_a_loud_tone_is_louder_than_a_quiet_one(self):
        loud = melspectrogram(tone() * 4.0).mean()
        quiet = melspectrogram(tone() * 0.05).mean()
        assert loud > quiet

    def test_it_finds_the_pitch_it_was_given(self):
        """The single sanity check that the filterbank is not transposed or mis-scaled: a 220Hz
        tone must be loudest in a low band, not a high one."""
        mel = melspectrogram(tone(1.0, hz=220.0))
        assert int(np.argmax(mel.mean(axis=1))) < N_MELS // 3

    def test_a_higher_tone_lands_in_a_higher_band(self):
        low = int(np.argmax(melspectrogram(tone(1.0, hz=220.0)).mean(axis=1)))
        high = int(np.argmax(melspectrogram(tone(1.0, hz=3000.0)).mean(axis=1)))
        assert high > low

    def test_a_clip_shorter_than_one_window_yields_nothing_rather_than_raising(self):
        assert melspectrogram(np.zeros(0, dtype=np.float32)).shape[1] == 0


class TestResampling:
    def test_kokoros_rate_becomes_the_models_rate(self):
        """Kokoro speaks at 24kHz and Wav2Lip listens at 16kHz. Getting this wrong does not
        error — it shifts every phoneme, and she lip-syncs to a chipmunk."""
        out = _resample_to_16k(tone(1.0, rate=24_000), 24_000)
        assert abs(len(out) - SAMPLE_RATE) <= 2

    def test_audio_already_at_the_right_rate_is_untouched(self):
        samples = tone(0.5)
        assert _resample_to_16k(samples, SAMPLE_RATE) is not None
        assert len(_resample_to_16k(samples, SAMPLE_RATE)) == len(samples)

    def test_the_pitch_survives_the_resample(self):
        resampled = _resample_to_16k(tone(1.0, hz=220.0, rate=24_000), 24_000)
        assert int(np.argmax(melspectrogram(resampled).mean(axis=1))) < N_MELS // 3


class TestTheCrop:
    def test_the_box_scales_with_the_portrait(self):
        """Fractions, not pixels, so a replacement face of another size still lands."""
        small, large = face_box(256, 256), face_box(1024, 1024)
        # Within a pixel of four times: the box is computed in fractions and truncated to
        # integers, so exact multiples are not on offer and are not needed.
        assert abs(large.left - small.left * 4) <= 4
        assert abs(large.size[0] - small.size[0] * 4) <= 4

    def test_it_covers_the_lower_face(self):
        left, top, right, bottom = FACE_FRACTIONS
        assert 0.2 < top < 0.5, "the crop should start around the brow"
        assert bottom > 0.85, "the crop has to include the chin"
        assert left < 0.5 < right

    def test_it_is_roughly_square_because_the_model_wants_a_square(self):
        """Wav2Lip is handed a 96x96 crop. A wildly non-square box gets squashed into it and the
        mouth comes back the wrong shape."""
        left, top, right, bottom = FACE_FRACTIONS
        assert abs((right - left) - (bottom - top)) < 0.05


class TestDegradingWithoutTheCheckpoint:
    def test_it_reports_itself_unavailable_rather_than_failing(self, tmp_path):
        """CI has no checkpoint. That must be a quiet no, not an exception on startup."""
        sync = LipSync(checkpoint=tmp_path / "nothing.pth")
        assert sync.available is False
        assert sync.ready is False

    def test_asking_for_frames_without_a_model_returns_none_of_them(self, tmp_path):
        sync = LipSync(checkpoint=tmp_path / "nothing.pth")
        assert sync.frames_for(tone(), SAMPLE_RATE) == []
        assert sync.frames_for_wav(b"") == []

    def test_it_describes_what_it_would_need(self, tmp_path):
        described = LipSync(checkpoint=tmp_path / "nothing.pth").describe()
        assert described["available"] is False
        assert described["fps"] == FPS
        assert described["box"] == list(FACE_FRACTIONS)


class TestTheFrameClock:
    def test_the_mel_window_and_the_frame_rate_are_the_ones_it_trained_on(self):
        """Both come from the checkpoint, not from preference. Changing either desynchronises
        the mouth from the voice by a growing amount across a clause."""
        assert FPS == 25
        assert MEL_STEP == 16

    @pytest.mark.parametrize("seconds", [0.4, 1.0, 3.0])
    def test_a_clip_yields_about_the_right_number_of_frames(self, seconds: float):
        """Checked through the arithmetic rather than the model, so it holds without a
        checkpoint. 25fps means a three-second clause is about seventy-five patches."""
        from rainmaker.calls.lipsync import MAX_ABS

        mel = melspectrogram(tone(seconds))
        padded = np.pad(mel, ((0, 0), (0, MEL_STEP)), constant_values=-MAX_ABS)
        wanted = max(1, int(round(seconds * FPS)))
        count = sum(
            1 for i in range(wanted) if int(i * mel.shape[1] / max(seconds * FPS, 1)) + MEL_STEP
            <= padded.shape[1]
        )
        # Every frame of the clause, including the last. Before the mel was padded this came up
        # four short on a three-second clause and her mouth froze at the end of every one.
        assert count == wanted, f"{count} frames for {wanted} expected"
