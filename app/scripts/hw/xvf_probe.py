"""Read DoA concurrently; chip recovery passes only with USB and live audio."""

from __future__ import annotations

import time
from contextlib import nullcontext

import numpy as np

from app.scripts.hw.common import parser, summary, write_report
from app.services.audio.xvf import XvfController


def recovery_complete(
    *,
    usb_present: bool,
    audio_present: bool,
    frames: int,
    nonzero_frames: int,
    status_flags: int,
    requested_seconds: float = 1,
) -> bool:
    return (
        usb_present
        and audio_present
        and frames >= requested_seconds * 16000 * 0.9
        and nonzero_frames > 0
        and not status_flags
    )


def main():
    cli = parser("xvf_probe")
    cli.add_argument("--concurrent-audio", action="store_true")
    cli.add_argument(
        "--reboot", action="store_true", help="Reboot while CoreAudio holds the device; verify full recovery"
    )
    cli.add_argument("--samples", type=int, default=50)
    args = cli.parse_args()
    if not 1 <= args.samples <= 600:
        raise ValueError("Samples must be within1..600")
    samples = []
    latency = []
    audio_blocks = audio_frames = audio_nonzero = status_count = 0
    report = {"status": "failed", "concurrent_audio": args.concurrent_audio, "reboot_requested": args.reboot}
    xvf = XvfController()
    try:
        import sounddevice as sd

        def find_device():
            return next(
                (
                    i
                    for i, d in enumerate(sd.query_devices())
                    if "Reachy Mini Audio" in d["name"] and d["max_input_channels"] >= 2
                ),
                None,
            )

        device = find_device()
        if args.concurrent_audio and device is None:
            raise RuntimeError("reachy_audio_usb_unavailable")

        def callback(data, frames, _time, status):
            nonlocal audio_blocks, audio_frames, audio_nonzero, status_count
            audio_blocks += 1
            audio_frames += frames
            audio_nonzero += frames if np.any(data) else 0
            status_count += bool(status)

        xvf.open()
        stream = (
            sd.InputStream(device=device, samplerate=16000, channels=2, blocksize=160, callback=callback)
            if args.concurrent_audio
            else nullcontext()
        )
        with stream:
            for _ in range(args.samples):
                start = time.monotonic()
                result = xvf.read_doa()
                latency.append((time.monotonic() - start) * 1000)
                samples.append(None if result is None else {"theta": result[0], "speech": result[1]})
                time.sleep(0.1)
            if args.reboot:
                start = time.monotonic()
                xvf.reboot()
                report["control_reopen_seconds"] = time.monotonic() - start
                # The old stream may remain open despite USB disappearing.
                # Release it, then discover and open a fresh CoreAudio stream.
        initial_frames = audio_frames
        initial_nonzero = audio_nonzero
        initial_status_count = status_count
        if args.reboot:
            import usb.core

            usb_present = usb.core.find(idVendor=0x38FB, idProduct=0x1001) is not None
            device = find_device()
            audio_present = device is not None
            audio_frames = audio_nonzero = status_count = 0
            if usb_present and audio_present:
                with sd.InputStream(
                    device=device, samplerate=16000, channels=2, blocksize=160, callback=callback
                ):
                    time.sleep(1)
            recovered = recovery_complete(
                usb_present=usb_present,
                audio_present=audio_present,
                frames=audio_frames,
                nonzero_frames=audio_nonzero,
                status_flags=status_count,
            )
            report.update(
                {
                    "usb_present_after_reboot": usb_present,
                    "coreaudio_present_after_reboot": audio_present,
                    "post_reboot_frames": audio_frames,
                    "post_reboot_nonzero_frames": audio_nonzero,
                    "post_reboot_status_flags": status_count,
                    "recovery_complete": recovered,
                }
            )
            if not recovered:
                report["fault"] = "usb_or_coreaudio_vanished_replug_required"
        else:
            recovered = True
        concurrent_complete = not args.concurrent_audio or (
            initial_frames >= args.samples * 0.1 * 16000 * 0.9
            and initial_nonzero > 0
            and initial_status_count == 0
        )
        report.update(
            {
                "registers": xvf.registers,
                "firmware_ready": xvf.last_error is None,
                "concurrent_capture_complete": concurrent_complete,
                "audio_frames": initial_frames,
                "audio_nonzero_frames": initial_nonzero,
                "audio_status_flags": initial_status_count,
                "audio_blocks": audio_blocks,
                "read_latency_ms": summary(latency),
                "samples": samples,
                "status": "measured"
                if recovered and concurrent_complete and all(s is not None for s in samples)
                else "failed",
            }
        )
    except Exception as exc:
        report.update(
            {
                "error": type(exc).__name__,
                "recovery_complete": False if args.reboot else None,
                "registers": xvf.registers,
                "samples": samples,
                "read_latency_ms": summary(latency),
            }
        )
    finally:
        xvf.close()
        write_report(args.output, report)


if __name__ == "__main__":
    main()
