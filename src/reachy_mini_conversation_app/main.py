"""Entrypoint for ReachyConvoMate."""

import argparse
import asyncio
import os
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Dict, List, Optional

import gradio as gr
from fastapi import FastAPI
from fastrtc import Stream
from gradio.utils import get_space
from reachy_mini import ReachyMini, ReachyMiniApp

from reachy_mini_conversation_app.utils import (
    handle_vision_stuff,
    parse_args,
    setup_logger,
)

DASHBOARD_URL = "http://127.0.0.1:7860/"
DAEMON_URL = "http://127.0.0.1:8000/"
DAEMON_START_COMMAND = "reachy-mini-daemon --localhost-only --log-level INFO"
MACOS_DAEMON_START_COMMAND = "reachy-mini-daemon --no-localhost-only --log-level INFO"
DASHBOARD_START_COMMAND = "reachy-convo-dashboard"
MODULE_DASHBOARD_START_COMMAND = "python -m reachy_mini_conversation_app.main"
CAMERA_PERMISSION_HINT = (
    "macOS denied camera access before OpenCV initialization. Grant camera access to the host app "
    "(for example Terminal, iTerm, or Codex) in System Settings > Privacy & Security > Camera, "
    "then relaunch the dashboard."
)


def _recommended_daemon_command() -> str:
    """Return the recommended daemon command for the current platform."""
    return MACOS_DAEMON_START_COMMAND if sys.platform == "darwin" else DAEMON_START_COMMAND


def _should_force_network_dashboard_connection() -> bool:
    """Return True when the dashboard should bypass localhost-only Zenoh on this platform."""
    if sys.platform != "darwin":
        return False

    try:
        sdk_version = version("reachy_mini")
    except PackageNotFoundError:
        return True

    try:
        major_str, minor_str, *_ = sdk_version.split(".")
        major = int(major_str)
        minor = int(minor_str)
    except (TypeError, ValueError):
        return True

    # Reachy Mini 1.6 switched the local client transport to localhost WebSockets.
    # For newer SDKs, forcing network mode breaks local dashboard startup on macOS.
    return (major, minor) < (1, 6)


def update_chatbot(chatbot: List[Dict[str, Any]], response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Update the chatbot with AdditionalOutputs."""
    chatbot.append(response)
    return chatbot


def _status_field(status: Any, field: str, default: Any = None) -> Any:
    """Read daemon status fields across dict-based and model-based SDK versions."""
    if isinstance(status, dict):
        return status.get(field, default)
    return getattr(status, field, default)


def _build_connection_help(args: argparse.Namespace, custom_dashboard_mode: bool) -> str:
    """Build a focused error message for missing Reachy daemon connectivity."""
    launch_command = "reachy-convo-mate --gradio" if args.gradio else "reachy-convo-mate"
    if custom_dashboard_mode:
        launch_hint = (
            f"Then launch the custom dashboard with `{DASHBOARD_START_COMMAND}` "
            f"(or `{MODULE_DASHBOARD_START_COMMAND}`)."
        )
    else:
        launch_hint = f"Then relaunch the app with `{launch_command}`."

    return "\n".join(
        [
            "Could not connect to a Reachy Mini daemon.",
            f"Start the local daemon first: `{_recommended_daemon_command()}`",
            launch_hint,
            f"Verify the daemon responds on {DAEMON_URL}.",
            "If the daemon dashboard on :8000 is up but the app still cannot connect on macOS, retry the daemon with "
            f"`{MACOS_DAEMON_START_COMMAND}`.",
            "If the daemon cannot find the robot, retry with `--log-level DEBUG` or `--serialport <device>`.",
        ]
    )


def _build_camera_help() -> str:
    """Build a focused error message for local camera initialization failures."""
    return "\n".join(
        [
            "Reachy Mini connected, but the local camera backend could not be opened.",
            CAMERA_PERMISSION_HINT,
            f"If you only want realtime speech first, relaunch with `{DASHBOARD_START_COMMAND} --no-camera`.",
        ]
    )


def standalone_main() -> None:
    """Run the standalone console or Gradio app entrypoint."""
    args, _ = parse_args()
    run(args)


def main() -> None:
    """Backward-compatible alias for the standalone entrypoint."""
    standalone_main()


def run(
    args: argparse.Namespace,
    robot: ReachyMini = None,
    app_stop_event: Optional[threading.Event] = None,
    settings_app: Optional[FastAPI] = None,
    instance_path: Optional[str] = None,
) -> None:
    """Run the Reachy Mini conversation app."""
    # Putting these dependencies here makes the dashboard faster to load when the conversation app is installed
    from reachy_mini_conversation_app.audio.head_wobbler import HeadWobbler
    from reachy_mini_conversation_app.console import LocalStream
    from reachy_mini_conversation_app.moves import MovementManager
    from reachy_mini_conversation_app.openai_realtime import OpenaiRealtimeHandler
    from reachy_mini_conversation_app.tools.core_tools import ToolDependencies

    logger = setup_logger(args.debug)
    logger.info("Starting ReachyConvoMate")

    custom_dashboard_mode = settings_app is not None
    if custom_dashboard_mode and args.gradio:
        logger.warning(
            "Ignoring --gradio because the dashboard entrypoint already serves the custom dashboard at %s.",
            DASHBOARD_URL,
        )
        args.gradio = False

    if args.no_camera and args.head_tracker is not None:
        logger.warning("Head tracking is not activated due to --no-camera.")

    if robot is None:
        # Initialize robot with appropriate backend
        # TODO: Implement dynamic robot connection detection
        # Automatically detect and connect to available Reachy Mini robot(s!)
        # Priority checks (in order):
        #   1. Reachy Lite connected directly to the host
        #   2. Reachy Mini daemon running on localhost (same device)
        #   3. Reachy Mini daemon on local network (same subnet)

        try:
            if args.wireless_version and not args.on_device:
                logger.info("Using WebRTC backend for fully remote wireless version")
                robot = ReachyMini(media_backend="webrtc", localhost_only=False)
            elif args.wireless_version and args.on_device:
                logger.info("Using GStreamer backend for on-device wireless version")
                robot = ReachyMini(media_backend="gstreamer")
            else:
                logger.info("Using default backend for lite version")
                robot = ReachyMini(media_backend="default")
        except ConnectionError as exc:
            raise ConnectionError(_build_connection_help(args, custom_dashboard_mode)) from exc

    # Check if running in simulation mode without --gradio
    robot_status = robot.client.get_status()
    if _status_field(robot_status, "simulation_enabled", False) and not args.gradio:
        logger.error(
            "Simulation mode requires Gradio interface. Please use --gradio flag when running in simulation mode.",
        )
        robot.client.disconnect()
        sys.exit(1)

    camera_worker, _, vision_manager = handle_vision_stuff(args, robot)

    movement_manager = MovementManager(
        current_robot=robot,
        camera_worker=camera_worker,
    )

    head_wobbler = HeadWobbler(set_speech_offsets=movement_manager.set_speech_offsets)

    deps = ToolDependencies(
        reachy_mini=robot,
        movement_manager=movement_manager,
        camera_worker=camera_worker,
        vision_manager=vision_manager,
        head_wobbler=head_wobbler,
    )
    current_file_path = os.path.dirname(os.path.abspath(__file__))
    logger.debug(f"Current file absolute path: {current_file_path}")
    chatbot = gr.Chatbot(
        type="messages",
        resizable=True,
        avatar_images=(
            os.path.join(current_file_path, "images", "user_avatar.png"),
            os.path.join(current_file_path, "images", "reachymini_avatar.png"),
        ),
    )
    logger.debug(f"Chatbot avatar images: {chatbot.avatar_images}")

    handler = OpenaiRealtimeHandler(deps, gradio_mode=args.gradio, instance_path=instance_path)

    stream_manager: gr.Blocks | LocalStream | None = None

    if args.gradio:
        api_key_textbox = gr.Textbox(
            label="OPENAI API Key",
            type="password",
            value=os.getenv("OPENAI_API_KEY") if not get_space() else "",
        )

        from reachy_mini_conversation_app.gradio_personality import PersonalityUI

        personality_ui = PersonalityUI()
        personality_ui.create_components()

        stream = Stream(
            handler=handler,
            mode="send-receive",
            modality="audio",
            additional_inputs=[
                chatbot,
                api_key_textbox,
                *personality_ui.additional_inputs_ordered(),
            ],
            additional_outputs=[chatbot],
            additional_outputs_handler=update_chatbot,
            ui_args={"title": "ReachyConvoMate"},
        )
        stream_manager = stream.ui
        if not settings_app:
            app = FastAPI()
        else:
            app = settings_app

        personality_ui.wire_events(handler, stream_manager)

        app = gr.mount_gradio_app(app, stream.ui, path="/")
    else:
        # In headless mode, wire settings_app + instance_path to console LocalStream
        stream_manager = LocalStream(
            handler,
            robot,
            settings_app=settings_app,
            instance_path=instance_path,
        )

    # Each async service → its own thread/loop
    movement_manager.start()
    head_wobbler.start()
    if camera_worker:
        camera_worker.start()
    if vision_manager:
        vision_manager.start()

    def poll_stop_event() -> None:
        """Poll the stop event to allow graceful shutdown."""
        if app_stop_event is not None:
            app_stop_event.wait()

        logger.info("App stop event detected, shutting down...")
        try:
            stream_manager.close()
        except Exception as e:
            logger.error(f"Error while closing stream manager: {e}")

    if app_stop_event:
        threading.Thread(target=poll_stop_event, daemon=True).start()

    try:
        stream_manager.launch()
    except KeyboardInterrupt:
        logger.info("Keyboard interruption in main thread... closing server.")
    finally:
        movement_manager.stop()
        head_wobbler.stop()
        if camera_worker:
            camera_worker.stop()
        if vision_manager:
            vision_manager.stop()

        # Ensure media is explicitly closed before disconnecting
        try:
            robot.media.close()
        except Exception as e:
            logger.debug(f"Error closing media during shutdown: {e}")

        # prevent connection to keep alive some threads
        robot.client.disconnect()
        time.sleep(1)
        logger.info("Shutdown complete.")


class ReachyConvoMate(ReachyMiniApp):  # type: ignore[misc]
    """Reachy Mini Apps entry point for ReachyConvoMate."""

    custom_app_url = "http://0.0.0.0:7860/"
    dont_start_webserver = False

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        """Run ReachyConvoMate."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        args, _ = parse_args()

        # is_wireless = reachy_mini.client.get_status()["wireless_version"]
        # args.head_tracker = None if is_wireless else "mediapipe"

        instance_path = self._get_instance_path().parent
        run(
            args,
            robot=reachy_mini,
            app_stop_event=stop_event,
            settings_app=self.settings_app,
            instance_path=instance_path,
        )


def dashboard_main() -> None:
    """Run the custom dashboard entrypoint through ReachyMiniApp."""
    args, _ = parse_args()
    app = ReachyConvoMate()
    if _should_force_network_dashboard_connection() and app.daemon_on_localhost:
        app.logger.info(
            "Forcing dashboard network discovery on macOS to avoid localhost-only Zenoh connection issues."
        )
        app.daemon_on_localhost = False
    if args.no_camera and not args.wireless_version:
        app.media_backend = "default_no_video"
    try:
        app.wrapped_run()
    except ConnectionError as exc:
        raise ConnectionError(_build_connection_help(args, custom_dashboard_mode=True)) from exc
    except RuntimeError as exc:
        if "Camera not found" in str(exc) or "Failed to open camera" in str(exc):
            raise RuntimeError(_build_camera_help()) from exc
        raise
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    dashboard_main()
