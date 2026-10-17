"""Bidirectional local audio stream with optional settings UI.

In headless mode, there is no Gradio UI. If the OpenAI API key is not
available via environment/.env, we expose a minimal settings page via the
Reachy Mini Apps settings server to let non-technical users enter it.

The settings UI is served from this package's ``static/`` folder and offers a
single password field to set ``OPENAI_API_KEY``. Once set, we persist it to the
app instance's ``.env`` file (if available) and proceed to start streaming.
"""

import asyncio
import base64
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import cv2
from fastrtc import AdditionalOutputs, audio_to_float32
from reachy_mini import ReachyMini
from reachy_mini.media.media_manager import MediaBackend
from scipy.signal import resample

from reachy_mini_conversation_app.config import config
from reachy_mini_conversation_app.headless_personality_ui import mount_personality_routes
from reachy_mini_conversation_app.openai_realtime import OpenaiRealtimeHandler

try:
    # FastAPI is provided by the Reachy Mini Apps runtime
    from fastapi import FastAPI, Response
    from fastapi.responses import FileResponse, JSONResponse
    from pydantic import BaseModel
except Exception:  # pragma: no cover - only loaded when settings_app is used
    FastAPI = object  # type: ignore
    FileResponse = object  # type: ignore
    JSONResponse = object  # type: ignore
    BaseModel = object  # type: ignore


logger = logging.getLogger(__name__)


class LocalStream:
    """LocalStream using Reachy Mini's recorder/player."""

    def __init__(
        self,
        handler: OpenaiRealtimeHandler,
        robot: ReachyMini,
        *,
        settings_app: Optional[FastAPI] = None,
        instance_path: Optional[str] = None,
    ):
        """Initialize the stream with an OpenAI realtime handler and pipelines.

        - ``settings_app``: the Reachy Mini Apps FastAPI to attach settings endpoints.
        - ``instance_path``: directory where per-instance ``.env`` should be stored.
        """
        self.handler = handler
        self._robot = robot
        self._stop_event = asyncio.Event()
        self._tasks: List[asyncio.Task[None]] = []
        # Allow the handler to flush the player queue when appropriate.
        self.handler._clear_queue = self.clear_audio_queue
        self._settings_app: Optional[FastAPI] = settings_app
        self._instance_path: Optional[str] = instance_path
        self._settings_initialized = False
        self._asyncio_loop = None

        # Chat history for the settings UI live view
        self._chat_history: List[Dict[str, Any]] = []
        self._chat_lock = threading.Lock()
        self._chat_sequence = 0  # monotonic counter for SSE
        # In-memory store for camera images referenced by chat messages
        self._chat_images: Dict[int, bytes] = {}  # seq -> JPEG bytes
        # Microphone mute flag
        self._muted = False
        # Sleep mode: suppresses all audio in and out
        self._sleeping = False

    # ---- Settings UI (only when API key is missing) ----
    def _read_env_lines(self, env_path: Path) -> list[str]:
        """Load env file contents or a template as a list of lines."""
        inst = env_path.parent
        try:
            if env_path.exists():
                try:
                    return env_path.read_text(encoding="utf-8").splitlines()
                except Exception:
                    return []
            template_text = None
            ex = inst / ".env.example"
            if ex.exists():
                try:
                    template_text = ex.read_text(encoding="utf-8")
                except Exception:
                    template_text = None
            if template_text is None:
                try:
                    cwd_example = Path.cwd() / ".env.example"
                    if cwd_example.exists():
                        template_text = cwd_example.read_text(encoding="utf-8")
                except Exception:
                    template_text = None
            if template_text is None:
                packaged = Path(__file__).parent / ".env.example"
                if packaged.exists():
                    try:
                        template_text = packaged.read_text(encoding="utf-8")
                    except Exception:
                        template_text = None
            return template_text.splitlines() if template_text else []
        except Exception:
            return []

    def _persist_api_key(self, key: str) -> None:
        """Persist API key to environment and instance ``.env`` if possible.

        Behavior:
        - Always sets ``OPENAI_API_KEY`` in process env and in-memory config.
        - Writes/updates ``<instance_path>/.env``:
          * If ``.env`` exists, replaces/append OPENAI_API_KEY line.
          * Else, copies template from ``<instance_path>/.env.example`` when present,
            otherwise falls back to the packaged template
            ``reachy_mini_conversation_app/.env.example``.
          * Ensures the resulting file contains the full template plus the key.
        - Loads the written ``.env`` into the current process environment.
        """
        k = (key or "").strip()
        if not k:
            return
        # Update live process env and config so consumers see it immediately
        try:
            os.environ["OPENAI_API_KEY"] = k
        except Exception:  # best-effort
            pass
        try:
            config.OPENAI_API_KEY = k
        except Exception:
            pass

        if not self._instance_path:
            return
        try:
            inst = Path(self._instance_path)
            env_path = inst / ".env"
            lines = self._read_env_lines(env_path)
            replaced = False
            for i, ln in enumerate(lines):
                if ln.strip().startswith("OPENAI_API_KEY="):
                    lines[i] = f"OPENAI_API_KEY={k}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"OPENAI_API_KEY={k}")
            final_text = "\n".join(lines) + "\n"
            env_path.write_text(final_text, encoding="utf-8")
            logger.info("Persisted OPENAI_API_KEY to %s", env_path)

            # Load the newly written .env into this process to ensure downstream imports see it
            try:
                from dotenv import load_dotenv

                load_dotenv(dotenv_path=str(env_path), override=True)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to persist OPENAI_API_KEY: %s", e)

    def _persist_personality(self, profile: Optional[str]) -> None:
        """Persist the startup personality to the instance .env and config."""
        selection = (profile or "").strip() or None
        try:
            from reachy_mini_conversation_app.config import set_custom_profile

            set_custom_profile(selection)
        except Exception:
            pass

        if not self._instance_path:
            return
        try:
            env_path = Path(self._instance_path) / ".env"
            lines = self._read_env_lines(env_path)
            replaced = False
            for i, ln in enumerate(list(lines)):
                if ln.strip().startswith("REACHY_MINI_CUSTOM_PROFILE="):
                    if selection:
                        lines[i] = f"REACHY_MINI_CUSTOM_PROFILE={selection}"
                    else:
                        lines.pop(i)
                    replaced = True
                    break
            if selection and not replaced:
                lines.append(f"REACHY_MINI_CUSTOM_PROFILE={selection}")
            if selection is None and not env_path.exists():
                return
            final_text = "\n".join(lines) + "\n"
            env_path.write_text(final_text, encoding="utf-8")
            logger.info("Persisted startup personality to %s", env_path)
            try:
                from dotenv import load_dotenv

                load_dotenv(dotenv_path=str(env_path), override=True)
            except Exception:
                pass
        except Exception as e:
            logger.warning("Failed to persist REACHY_MINI_CUSTOM_PROFILE: %s", e)

    def _read_persisted_personality(self) -> Optional[str]:
        """Read persisted startup personality from instance .env (if any)."""
        if not self._instance_path:
            return None
        env_path = Path(self._instance_path) / ".env"
        try:
            if env_path.exists():
                for ln in env_path.read_text(encoding="utf-8").splitlines():
                    if ln.strip().startswith("REACHY_MINI_CUSTOM_PROFILE="):
                        _, _, val = ln.partition("=")
                        v = val.strip()
                        return v or None
        except Exception:
            pass
        return None

    def _init_settings_ui_if_needed(self) -> None:
        """Attach minimal settings UI to the settings app.

        Always mounts the UI when a settings_app is provided so that users
        see a confirmation message even if the API key is already configured.
        """
        if self._settings_initialized:
            return
        if self._settings_app is None:
            return

        static_dir = Path(__file__).parent / "static"
        index_file = static_dir / "index.html"

        # Serve /static/* with no-cache headers so browsers always pick up changes
        @self._settings_app.get("/static/{path:path}")
        def _static_file(path: str) -> Response:
            file_path = (static_dir / path).resolve()
            if not str(file_path).startswith(str(static_dir.resolve())) or not file_path.is_file():
                return Response(status_code=404)
            return FileResponse(
                str(file_path),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
            )

        # ---- Override the base-class GET / so our index.html wins ----
        # FastAPI matches the first registered route for a given path+method,
        # and the SDK base class already registered GET /.  Remove it so our
        # handler (with no-cache headers) takes precedence.
        try:
            self._settings_app.routes[:] = [
                r for r in self._settings_app.routes
                if not (hasattr(r, "path") and r.path == "/" and hasattr(r, "methods") and "GET" in r.methods)
            ]
        except Exception:
            pass

        class ApiKeyPayload(BaseModel):
            openai_api_key: str

        NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"}

        # GET / -> index.html (no-cache to pick up changes immediately)
        @self._settings_app.get("/")
        def _root() -> FileResponse:
            return FileResponse(str(index_file), headers=NO_CACHE)

        # GET /favicon.ico -> optional, avoid noisy 404s on some browsers
        @self._settings_app.get("/favicon.ico")
        def _favicon() -> Response:
            return Response(status_code=204)

        # GET /status -> whether key is set
        @self._settings_app.get("/status")
        def _status() -> JSONResponse:
            has_key = bool(config.OPENAI_API_KEY and str(config.OPENAI_API_KEY).strip())
            return JSONResponse({"has_key": has_key}, headers=NO_CACHE)

        # GET /ready -> whether backend finished loading tools
        @self._settings_app.get("/ready")
        def _ready() -> JSONResponse:
            try:
                mod = sys.modules.get("reachy_mini_conversation_app.tools.core_tools")
                ready = bool(getattr(mod, "_TOOLS_INITIALIZED", False)) if mod else False
            except Exception:
                ready = False
            return JSONResponse({"ready": ready})

        # POST /openai_api_key -> set/persist key
        @self._settings_app.post("/openai_api_key")
        def _set_key(payload: ApiKeyPayload) -> JSONResponse:
            key = (payload.openai_api_key or "").strip()
            if not key:
                return JSONResponse({"ok": False, "error": "empty_key"}, status_code=400)
            self._persist_api_key(key)
            return JSONResponse({"ok": True})

        # POST /validate_api_key -> validate key without persisting it
        @self._settings_app.post("/validate_api_key")
        async def _validate_key(payload: ApiKeyPayload) -> JSONResponse:
            key = (payload.openai_api_key or "").strip()
            if not key:
                return JSONResponse({"valid": False, "error": "empty_key"}, status_code=400)

            # Try to validate by checking if we can fetch the models
            try:
                import httpx

                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get("https://api.openai.com/v1/models", headers=headers)
                    if response.status_code == 200:
                        return JSONResponse({"valid": True})
                    elif response.status_code == 401:
                        return JSONResponse({"valid": False, "error": "invalid_api_key"}, status_code=401)
                    else:
                        return JSONResponse(
                            {"valid": False, "error": "validation_failed"}, status_code=response.status_code
                        )
            except Exception as e:
                logger.warning(f"API key validation failed: {e}")
                return JSONResponse({"valid": False, "error": "validation_error"}, status_code=500)

        # ---- Camera & Chat endpoints for the live dashboard ----

        class BrowserFramePayload(BaseModel):
            image_b64: str
            source_id: str | None = None
            source_label: str | None = None

        # GET /camera/source -> tell the browser whether it should push frames.
        @self._settings_app.get("/camera/source")
        def _camera_source() -> JSONResponse:
            cw = self.handler.deps.camera_worker
            return JSONResponse({
                "available": cw is not None,
                "browser_input": callable(getattr(cw, "set_frame_from_jpeg", None)),
            })

        # POST /camera/browser-frame -> latest browser-captured JPEG.
        @self._settings_app.post("/camera/browser-frame")
        def _camera_browser_frame(payload: BrowserFramePayload) -> JSONResponse:
            cw = self.handler.deps.camera_worker
            setter = getattr(cw, "set_frame_from_jpeg", None)
            if not callable(setter):
                return JSONResponse({"ok": False, "error": "browser_camera_disabled"}, status_code=400)

            raw = (payload.image_b64 or "").strip()
            if "," in raw:
                raw = raw.split(",", 1)[1]
            if not raw:
                return JSONResponse({"ok": False, "error": "empty_frame"}, status_code=400)

            try:
                accepted = setter(
                    base64.b64decode(raw, validate=True),
                    source_id=(payload.source_id or "").strip() or None,
                    source_label=(payload.source_label or "").strip() or None,
                )
            except Exception as e:
                logger.debug("browser camera frame rejected: %s", e)
                return JSONResponse({"ok": False, "error": "invalid_frame"}, status_code=400)
            return JSONResponse({"ok": True, "accepted": bool(accepted)})

        # GET /camera/snapshot -> JPEG image of what Reachy sees right now
        @self._settings_app.get("/camera/snapshot")
        def _camera_snapshot() -> Response:
            cw = self.handler.deps.camera_worker
            if cw is None:
                return Response(content=b"", status_code=503, media_type="text/plain")
            frame = cw.get_latest_frame()
            if frame is None:
                return Response(content=b"", status_code=503, media_type="text/plain")
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if not ok:
                return Response(content=b"", status_code=500, media_type="text/plain")
            return Response(content=buf.tobytes(), media_type="image/jpeg")

        # POST /camera/photo -> save a high-quality snapshot and return base64
        @self._settings_app.post("/camera/photo")
        def _camera_photo() -> JSONResponse:
            cw = self.handler.deps.camera_worker
            if cw is None:
                return JSONResponse({"ok": False, "error": "no_camera"}, status_code=503)
            frame = cw.get_latest_frame()
            if frame is None:
                return JSONResponse({"ok": False, "error": "no_frame"}, status_code=503)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                return JSONResponse({"ok": False, "error": "encode_failed"}, status_code=500)
            jpeg_bytes = buf.tobytes()
            b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
            # Also store in chat history so photos survive page refresh
            with self._chat_lock:
                self._chat_sequence += 1
                seq = self._chat_sequence
                self._chat_images[seq] = jpeg_bytes
                self._chat_history.append({
                    "seq": seq,
                    "role": "user",
                    "content": "Photo captured",
                    "tool": False,
                    "image": f"/chat/image/{seq}",
                })
            return JSONResponse({"ok": True, "image": b64})

        # GET /chat/history -> full chat history
        @self._settings_app.get("/chat/history")
        def _chat_history() -> JSONResponse:
            with self._chat_lock:
                return JSONResponse({"messages": list(self._chat_history), "seq": self._chat_sequence})

        # GET /chat/poll?after=N -> new messages since sequence N
        @self._settings_app.get("/chat/poll")
        def _chat_poll(after: int = 0) -> JSONResponse:
            with self._chat_lock:
                new = [m for m in self._chat_history if m.get("seq", 0) > after]
                return JSONResponse({"messages": new, "seq": self._chat_sequence})

        # GET /chat/image/{seq} -> JPEG image stored from a camera tool result
        @self._settings_app.get("/chat/image/{seq}")
        def _chat_image(seq: int) -> Response:
            data = self._chat_images.get(seq)
            if data is None:
                return Response(status_code=404)
            return Response(content=data, media_type="image/jpeg")

        # ---- Robot control endpoints ----

        @self._settings_app.post("/robot/sleep")
        def _robot_sleep() -> JSONResponse:
            try:
                self._sleeping = True
                self.clear_audio_queue()
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pool.submit(self._robot.goto_sleep).result(timeout=15)
                logger.info("Robot is now sleeping (audio suppressed)")
                return JSONResponse({"ok": True, "sleeping": True})
            except Exception as e:
                logger.warning("robot/sleep failed: %s", e)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        @self._settings_app.post("/robot/wakeup")
        def _robot_wakeup() -> JSONResponse:
            try:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    pool.submit(self._robot.wake_up).result(timeout=15)
                self._sleeping = False
                logger.info("Robot is now awake (audio resumed)")
                return JSONResponse({"ok": True, "sleeping": False})
            except Exception as e:
                logger.warning("robot/wakeup failed: %s", e)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

        @self._settings_app.get("/robot/status")
        def _robot_status() -> JSONResponse:
            return JSONResponse({"sleeping": self._sleeping})

        # ---- Audio mute ----

        @self._settings_app.post("/audio/mute")
        def _audio_mute(muted: bool = True) -> JSONResponse:
            self._muted = muted
            logger.info("Microphone %s", "muted" if muted else "unmuted")
            return JSONResponse({"ok": True, "muted": self._muted})

        @self._settings_app.get("/audio/status")
        def _audio_status() -> JSONResponse:
            return JSONResponse({"muted": self._muted})

        # ---- Text chat ----

        class ChatMessage(BaseModel):
            text: str

        @self._settings_app.post("/chat/send")
        def _chat_send(payload: ChatMessage) -> JSONResponse:
            text = (payload.text or "").strip()
            if not text:
                return JSONResponse({"ok": False, "error": "empty"}, status_code=400)

            loop = self._asyncio_loop
            if loop is None:
                return JSONResponse({"ok": False, "error": "not_ready"}, status_code=503)

            async def _do_send() -> None:
                conn = self.handler.connection
                if conn is None:
                    raise RuntimeError("no_connection")
                await conn.conversation.item.create(
                    item={
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                )
                await conn.response.create()

            try:
                fut = asyncio.run_coroutine_threadsafe(_do_send(), loop)
                fut.result(timeout=10)
            except Exception as e:
                logger.warning("chat/send failed: %s", e)
                return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

            # Store in chat history
            with self._chat_lock:
                self._chat_sequence += 1
                self._chat_history.append({
                    "seq": self._chat_sequence,
                    "role": "user",
                    "content": text,
                    "tool": False,
                })

            return JSONResponse({"ok": True})

        self._settings_initialized = True

    def launch(self) -> None:
        """Start the recorder/player and run the async processing loops.

        If the OpenAI key is missing, expose a tiny settings UI via the
        Reachy Mini settings server to collect it before starting streams.
        """
        self._stop_event.clear()

        # Try to load an existing instance .env first (covers subsequent runs)
        if self._instance_path:
            try:
                from dotenv import load_dotenv

                from reachy_mini_conversation_app.config import set_custom_profile

                env_path = Path(self._instance_path) / ".env"
                if env_path.exists():
                    load_dotenv(dotenv_path=str(env_path), override=True)
                    # Update config with newly loaded values
                    new_key = os.getenv("OPENAI_API_KEY", "").strip()
                    if new_key:
                        try:
                            config.OPENAI_API_KEY = new_key
                        except Exception:
                            pass
                    new_profile = os.getenv("REACHY_MINI_CUSTOM_PROFILE")
                    if new_profile is not None:
                        try:
                            set_custom_profile(new_profile.strip() or None)
                        except Exception:
                            pass
            except Exception:
                pass

        # If key is still missing, try to download one from HuggingFace
        if not (config.OPENAI_API_KEY and str(config.OPENAI_API_KEY).strip()):
            logger.info("OPENAI_API_KEY not set, attempting to download from HuggingFace...")
            try:
                from gradio_client import Client
                client = Client("HuggingFaceM4/gradium_setup", verbose=False)
                key, status = client.predict(api_name="/claim_b_key")
                if key and key.strip():
                    logger.info("Successfully downloaded API key from HuggingFace")
                    # Persist it immediately
                    self._persist_api_key(key)
            except Exception as e:
                logger.warning(f"Failed to download API key from HuggingFace: {e}")

        # Always expose settings UI if a settings app is available
        # (do this AFTER loading/downloading the key so status endpoint sees the right value)
        self._init_settings_ui_if_needed()

        # If key is still missing -> wait until provided via the settings UI
        if not (config.OPENAI_API_KEY and str(config.OPENAI_API_KEY).strip()):
            logger.warning("OPENAI_API_KEY not found. Open the app settings page to enter it.")
            # Poll until the key becomes available (set via the settings UI)
            try:
                while not (config.OPENAI_API_KEY and str(config.OPENAI_API_KEY).strip()):
                    time.sleep(0.2)
            except KeyboardInterrupt:
                logger.info("Interrupted while waiting for API key.")
                return

        # Start media after key is set/available
        self._robot.media.start_recording()
        self._robot.media.start_playing()
        time.sleep(1)  # give some time to the pipelines to start

        async def runner() -> None:
            # Capture loop for cross-thread personality actions
            loop = asyncio.get_running_loop()
            self._asyncio_loop = loop  # type: ignore[assignment]
            # Mount personality routes now that loop and handler are available
            try:
                if self._settings_app is not None:
                    mount_personality_routes(
                        self._settings_app,
                        self.handler,
                        lambda: self._asyncio_loop,
                        persist_personality=self._persist_personality,
                        get_persisted_personality=self._read_persisted_personality,
                    )
            except Exception:
                pass
            self._tasks = [
                asyncio.create_task(self.handler.start_up(), name="openai-handler"),
                asyncio.create_task(self.record_loop(), name="stream-record-loop"),
                asyncio.create_task(self.play_loop(), name="stream-play-loop"),
            ]
            try:
                await asyncio.gather(*self._tasks)
            except asyncio.CancelledError:
                logger.info("Tasks cancelled during shutdown")
            finally:
                # Ensure handler connection is closed
                await self.handler.shutdown()

        asyncio.run(runner())

    def close(self) -> None:
        """Stop the stream and underlying media pipelines.

        This method:
        - Stops audio recording and playback first
        - Sets the stop event to signal async loops to terminate
        - Cancels all pending async tasks (openai-handler, record-loop, play-loop)
        """
        logger.info("Stopping LocalStream...")

        # Stop media pipelines FIRST before cancelling async tasks
        # This ensures clean shutdown before PortAudio cleanup
        try:
            self._robot.media.stop_recording()
        except Exception as e:
            logger.debug(f"Error stopping recording (may already be stopped): {e}")

        try:
            self._robot.media.stop_playing()
        except Exception as e:
            logger.debug(f"Error stopping playback (may already be stopped): {e}")

        # Now signal async loops to stop
        self._stop_event.set()

        # Cancel all running tasks
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def clear_audio_queue(self) -> None:
        """Flush the player's appsrc to drop any queued audio immediately."""
        logger.info("User intervention: flushing player queue")
        if self._robot.media.backend == MediaBackend.GSTREAMER:
            # Directly flush gstreamer audio pipe
            self._robot.media.audio.clear_player()
        elif self._robot.media.backend in (MediaBackend.DEFAULT, MediaBackend.DEFAULT_NO_VIDEO):
            self._robot.media.audio.clear_output_buffer()
        self.handler.output_queue = asyncio.Queue()

    async def record_loop(self) -> None:
        """Read mic frames from the recorder and forward them to the handler."""
        input_sample_rate = self._robot.media.get_input_audio_samplerate()
        logger.debug(f"Audio recording started at {input_sample_rate} Hz")

        while not self._stop_event.is_set():
            audio_frame = self._robot.media.get_audio_sample()
            if audio_frame is not None and not self._muted and not self._sleeping:
                await self.handler.receive((input_sample_rate, audio_frame))
            await asyncio.sleep(0)  # avoid busy loop

    async def play_loop(self) -> None:
        """Fetch outputs from the handler: log text and play audio frames."""
        while not self._stop_event.is_set():
            handler_output = await self.handler.emit()

            if isinstance(handler_output, AdditionalOutputs):
                for msg in handler_output.args:
                    content = msg.get("content", "")
                    role = msg.get("role", "")
                    if isinstance(content, str):
                        logger.info(
                            "role=%s content=%s",
                            role,
                            content if len(content) < 500 else content[:500] + "…",
                        )
                    # Store displayable messages for the chat UI
                    if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                        metadata = msg.get("metadata")
                        is_tool = metadata and metadata.get("title", "").startswith("\U0001f6e0")
                        # Extract camera images from tool results
                        image_seq = None
                        display_content = content
                        if is_tool:
                            try:
                                parsed = json.loads(content)
                                if isinstance(parsed, dict) and "b64_im" in parsed:
                                    image_bytes = base64.b64decode(parsed["b64_im"])
                                    display_content = "Camera snapshot"
                                    image_seq = True  # placeholder, set below
                            except Exception:
                                image_bytes = None  # type: ignore[assignment]
                                image_seq = None
                        with self._chat_lock:
                            self._chat_sequence += 1
                            entry: Dict[str, Any] = {
                                "seq": self._chat_sequence,
                                "role": role,
                                "content": display_content,
                                "tool": bool(is_tool),
                            }
                            if image_seq is not None:
                                entry["image"] = f"/chat/image/{self._chat_sequence}"
                                self._chat_images[self._chat_sequence] = image_bytes  # type: ignore[assignment]
                            self._chat_history.append(entry)
                            # Cap history at 200 messages to avoid unbounded growth
                            if len(self._chat_history) > 200:
                                removed = self._chat_history[:-200]
                                self._chat_history = self._chat_history[-200:]
                                for r in removed:
                                    self._chat_images.pop(r.get("seq", -1), None)

            elif isinstance(handler_output, tuple):
                # Drop outgoing audio while sleeping
                if self._sleeping:
                    continue

                input_sample_rate, audio_data = handler_output
                output_sample_rate = self._robot.media.get_output_audio_samplerate()

                # Reshape if needed
                if audio_data.ndim == 2:
                    # Scipy channels last convention
                    if audio_data.shape[1] > audio_data.shape[0]:
                        audio_data = audio_data.T
                    # Multiple channels -> Mono channel
                    if audio_data.shape[1] > 1:
                        audio_data = audio_data[:, 0]

                # Cast if needed
                audio_frame = audio_to_float32(audio_data)

                # Resample if needed
                if input_sample_rate != output_sample_rate:
                    audio_frame = resample(
                        audio_frame,
                        int(len(audio_frame) * output_sample_rate / input_sample_rate),
                    )

                self._robot.media.push_audio_sample(audio_frame)

            else:
                logger.debug("Ignoring output type=%s", type(handler_output).__name__)

            await asyncio.sleep(0)  # yield to event loop
