"""Optional local GTCRN after channel selection/AEC. Disabled without an asset."""

from __future__ import annotations

from pathlib import Path

import numpy as np


class GtcrnDenoiser:
    def __init__(self, model_path: str, *, sample_rate: int = 16000, model=None) -> None:
        self.sample_rate = sample_rate
        self.pending = np.empty(0, dtype=np.float32)
        if model is None:
            if not Path(model_path).is_file():
                raise FileNotFoundError("GTCRN asset is not provisioned")
            import sherpa_onnx

            config = sherpa_onnx.OnlineSpeechDenoiserConfig(
                model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
                    gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=model_path),
                    num_threads=1,
                    provider="cpu",
                )
            )
            if not config.validate():
                raise ValueError("GTCRN config validation failed")
            model = sherpa_onnx.OnlineSpeechDenoiser(config)
        if model.sample_rate != sample_rate:
            raise ValueError("GTCRN sample rate must match the local stream")
        self.model = model

    def process(self, chunk: bytes) -> bytes:
        values = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        result = self.model.run(values, self.sample_rate)
        cleaned = np.asarray(result.samples, dtype=np.float32)
        self.pending = np.concatenate((self.pending, cleaned))
        output = np.zeros(len(values), dtype=np.float32)
        take = min(len(output), len(self.pending))
        # Preserve DSP latency rather than forwarding unprocessed input while
        # the streaming denoiser is warming up.
        if take:
            output[-take:] = self.pending[:take]
        self.pending = self.pending[take:]
        if len(self.pending) > self.sample_rate:
            raise RuntimeError("GTCRN exceeded bounded streaming output")
        return (np.clip(output, -1, 1) * 32767).astype(np.int16).tobytes()

    def reset(self) -> None:
        self.model.reset()
        self.pending.fill(0)
        self.pending = np.empty(0, dtype=np.float32)
