"""Record voluntary member enrollment without starting the concierge runtime."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from pathlib import Path

import httpx

from app.config import get_settings
from app.services.vision.camera import encode_frame_as_jpeg
from app.services.vision.embedding_guard import zeroize
from app.vision_stack import authorized_vision_settings, build_vision_stack


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enroll a consenting member from files or the Reachy camera")
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--slack-user-id", required=True)
    parser.add_argument("--slack-display-name", required=True)
    parser.add_argument("--company")
    parser.add_argument("--operator", required=True)
    parser.add_argument("--notice-version", required=True)
    parser.add_argument("--notice-file", type=Path, required=True)
    parser.add_argument(
        "--confirm-biometric-consent",
        action="store_true",
        help="Member personally gave recorded, voluntary consent",
    )
    parser.add_argument("--greet-by-name", action="store_true")
    parser.add_argument("--reinstate", action="store_true")
    parser.add_argument("--from-reachy-camera", action="store_true")
    parser.add_argument(
        "--member-lookup-url", help="B1 member verification endpoint; requires a matching active member"
    )
    parser.add_argument("images", nargs="*")
    args = parser.parse_args()
    if bool(args.images) == args.from_reachy_camera:
        parser.error("Choose image files or --from-reachy-camera")
    if not args.confirm_biometric_consent:
        parser.error("Enrollment requires --confirm-biometric-consent")
    return args


async def verify_member(settings, args):
    url = args.member_lookup_url or settings.mlai_backend_url.rstrip("/") + "/api/v1/concierge/members/"
    async with httpx.AsyncClient(timeout=5.0, headers={"X-API-Key": settings.mlai_backend_api_key}) as client:
        response = await client.get(url, params={"slack_user_id": args.slack_user_id})
        response.raise_for_status()
        member = response.json()
    if (
        not isinstance(member, dict)
        or member.get("slack_user_id") != args.slack_user_id
        or member.get("active") is not True
    ):
        raise ValueError("Backend could not verify an active member; enrollment stopped")
    if member.get("full_name") != args.full_name:
        raise ValueError("Full name does not match the verified member record")
    return member


async def capture_samples(camera) -> list[dict[str, str]]:
    samples = []
    for pose in (
        "front",
        "left (up to 25 degrees)",
        "right (up to 25 degrees)",
        "slightly up",
        "slightly down",
    ):
        await asyncio.to_thread(input, f"Pose {pose}; press Enter when ready: ")
        for index in range(3):
            frame = await asyncio.to_thread(camera.get_frame)
            if frame is None:
                raise ValueError("Camera frame unavailable; no enrollment was written")
            try:
                samples.append(
                    {
                        "label": f"{pose.split()[0]}-{index + 1}",
                        "image_b64": base64.b64encode(encode_frame_as_jpeg(frame)).decode(),
                    }
                )
            finally:
                zeroize(frame)
            await asyncio.sleep(0.15)
    return samples


async def _run() -> None:
    args = parse_args()
    async with authorized_vision_settings(get_settings()) as settings:
        await _run_authorized(args, settings)


async def _run_authorized(args, settings) -> None:
    await verify_member(settings, args)
    notice_sha256 = hashlib.sha256(args.notice_file.read_bytes()).hexdigest()
    stack = build_vision_stack(settings, for_enrollment=True, from_reachy_camera=args.from_reachy_camera)
    samples = []
    try:
        await stack.start()
        samples = (
            await capture_samples(stack.camera)
            if args.from_reachy_camera
            else [
                {"label": Path(path).stem, "image_b64": base64.b64encode(Path(path).read_bytes()).decode()}
                for path in args.images
            ]
        )
        worker = asyncio.create_task(
            asyncio.to_thread(
                stack.enrollment.enroll,
                full_name=args.full_name,
                slack_user_id=args.slack_user_id,
                slack_display_name=args.slack_display_name,
                company=args.company,
                biometric_consent=True,
                samples=samples,
                operator_id=args.operator,
                notice_version=args.notice_version,
                notice_sha256=notice_sha256,
                greet_by_name=args.greet_by_name,
                reinstate=args.reinstate,
                capture_source="reachy_camera" if args.from_reachy_camera else "file",
            )
        )
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            await worker
            raise
        stack.session.commit()
        print(result)
    except BaseException:
        stack.session.rollback()
        raise
    finally:
        samples.clear()
        await stack.stop()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
