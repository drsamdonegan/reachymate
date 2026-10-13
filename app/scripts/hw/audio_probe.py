"""Measure stereo channels in RAM, optionally play a bounded -20 dBFS tone."""

from __future__ import annotations

import time

import numpy as np

from app.scripts.hw.common import parser, write_report
from app.services.system.tcc import permission_status


def channel_metrics(samples, sample_rate: int = 16000):
    values = np.asarray(samples, dtype=np.float32)
    if values.ndim != 2 or values.shape[1] != 2 or not len(values):
        raise ValueError("Expected nonempty stereo samples")
    zero = np.all(values == 0, axis=1)
    longest = run = 0
    for flag in zero:
        run = run + 1 if flag else 0
        longest = max(longest, run)
    varying = bool(np.std(values[:, 0]) > 0 and np.std(values[:, 1]) > 0)
    return {
        "rms": np.sqrt(np.mean(values**2, axis=0)).astype(float).tolist(),
        "correlation": float(np.corrcoef(values.T)[0, 1]) if varying else None,
        "longest_zero_seconds": longest / sample_rate,
        "all_zeros": bool(np.all(zero)),
        "frames": len(values),
    }


def capture_complete(metrics: dict, requested_seconds: float, status_flags: int = 0) -> bool:
    return (
        metrics.get("frames", 0) >= requested_seconds * 16000 * 0.9
        and not metrics.get("all_zeros", True)
        and status_flags == 0
    )


def main():
    cli = parser("audio_probe")
    cli.add_argument("--seconds", type=float, default=10)
    cli.add_argument("--play-tone", action="store_true")
    args = cli.parse_args()
    if not 0 < args.seconds <= 60:
        raise ValueError("Probe duration must be within 60 seconds")
    report = {
        "status": "failed",
        "tcc": permission_status(),
        "requested_seconds": args.seconds,
        "tone_played": False,
        "echo_delay": "requires operator corpus",
    }
    blocks = []
    statuses = []
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        device = next(
            (
                i
                for i, d in enumerate(devices)
                if "Reachy Mini Audio" in d["name"] and d["max_input_channels"] >= 2
            ),
            None,
        )
        if device is None:
            raise RuntimeError("reachy_audio_usb_unavailable")

        def callback(indata, _frames, _time, status):
            blocks.append(indata.copy())
            if status:
                statuses.append(str(status))

        with sd.InputStream(device=device, channels=2, samplerate=16000, blocksize=160, callback=callback):
            if args.play_tone:
                output = next(
                    i
                    for i, d in enumerate(devices)
                    if "Reachy Mini Audio" in d["name"] and d["max_output_channels"] >= 2
                )
                t = np.arange(16000) / 16000
                tone = (0.1 * np.sin(2 * np.pi * (440 * t + 600 * t**2))).astype(np.float32)
                sd.play(np.repeat(tone[:, None], 2, axis=1), samplerate=16000, device=output)
                report["tone_played"] = True
            time.sleep(args.seconds)
        if not blocks:
            raise RuntimeError("no_captured_samples")
        metrics = channel_metrics(np.concatenate(blocks))
        complete = capture_complete(metrics, args.seconds, len(statuses))
        report.update(
            {
                **metrics,
                "audio_status_flags": statuses,
                "capture_complete": complete,
                "captured_seconds": metrics["frames"] / 16000,
                "status": "measured" if complete else "failed",
            }
        )
        if not complete:
            report["fault"] = "incomplete_or_zero_audio"
    except Exception as exc:
        report.update(
            {
                "error": type(exc).__name__,
                "capture_complete": False,
                "audio_status_flags": statuses,
                "frames": sum(len(block) for block in blocks),
            }
        )
    finally:
        blocks.clear()
        write_report(args.output, report)


if __name__ == "__main__":
    main()
