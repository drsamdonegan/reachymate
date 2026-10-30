const healthTableBody = document.querySelector("#health-table tbody");
const healthSummary = document.querySelector("#health-summary");
const eventsList = document.querySelector("#events-list");
const telemetryFeed = document.querySelector("#telemetry-feed");
const cameraFeed = document.querySelector("#camera-feed");
const cameraStatus = document.querySelector("#camera-status");
let cameraSourceName = "Camera";
let dashboardStartAttempted = false;
let chatStartPending = false;
let cameraHandedToLuma = false;
let greeterStartAttempted = false;
let greeterGreetingLoaded = false;
let greeterActionError = '';

async function startEventGreeter(useDefault = false) {
  greeterActionError = '';
  const payload = useDefault ? {} : {greeting: document.querySelector('#greeter-greeting').value};
  try {
    await fetchJson('/api/admin/event-greeter/start', {method:'POST',
      ...(useDefault ? {} : {headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})});
    await refreshVoice();
  } catch (error) {
    greeterActionError = error.message;
    document.querySelector('#greeter-status').textContent = greeterActionError;
  }
}
document.querySelector('#greeter-form').addEventListener('submit', event => {
  event.preventDefault(); startEventGreeter();
});
document.querySelector('#greeter-stop').addEventListener('click', async () => {
  try {
    greeterActionError = '';
    await fetchJson('/api/admin/event-greeter/stop', {method:'POST'});
    await refreshVoice();
  } catch (error) {
    greeterActionError = error.message;
    document.querySelector('#greeter-status').textContent = greeterActionError;
  }
});

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `${response.status}`);
  }
  return response.json();
}

async function refreshHealth() {
  const response = await fetch("/healthz/ready");
  const payload = await response.json();
  healthSummary.textContent = `Concierge services: ${payload.conversation_ready ? "ready" : "setup pending"} · Access: ${payload.access_ready ? "ready" : "shadow only"}`;
  healthTableBody.innerHTML = "";
  Object.entries(payload.components).forEach(([component, status]) => {
    const row = document.createElement("tr");
    const statusClass =
      status === "HEALTHY" ? "status-ok" : status === "DEGRADED" ? "status-warn" : "status-bad";
    const label = component === "realtime" ? "voice" : component;
    row.innerHTML = `<td>${label}</td><td class="${statusClass}">${status}</td>`;
    healthTableBody.appendChild(row);
  });
}

async function refreshEvents() {
  const events = await fetchJson("/api/admin/events");
  eventsList.innerHTML = "";
  events.forEach((event) => {
    const card = document.createElement("div");
    card.className = "event-card";
    card.innerHTML = `
      <strong>${event.reason_code}</strong>
      <div>${event.decision} · ${event.booking_status}</div>
      <div class="muted">${event.timestamp}</div>
      <div class="muted">${event.latency_ms ?? "?"} ms</div>
    `;
    eventsList.appendChild(card);
  });
}

async function refreshCamera() {
  if (cameraHandedToLuma) {
    const previous = cameraFeed.src;
    cameraFeed.removeAttribute("src");
    if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);
    cameraStatus.textContent = "Camera handed to Luma · preview is in the Luma scanner";
    return;
  }
  try {
    const response = await fetch(`/camera/snapshot?ts=${Date.now()}`);
    if (!response.ok) {
      throw new Error("Camera off · say Hey Reachy or start a voice test");
    }
    const blob = await response.blob();
    if (cameraHandedToLuma) return;
    const previous = cameraFeed.src;
    cameraFeed.src = URL.createObjectURL(blob);
    if (previous.startsWith("blob:")) URL.revokeObjectURL(previous);
    cameraStatus.textContent = `Live · ${cameraSourceName}`;
  } catch (error) {
    cameraFeed.removeAttribute("src");
    cameraStatus.textContent = error.message;
  }
}

async function refreshVoice() {
  try {
    const diagnostics = await fetchJson("/api/admin/diagnostics/audio");
    cameraSourceName = diagnostics.reachy_media?.camera_source?.name || "Simulation camera";
    const movement = diagnostics.motion;
    document.querySelector("#motion-status").textContent = movement
      ? `Motion: ${movement.speech_active ? "moving with speech" : movement.status}${movement.name ? ` · ${movement.name}` : ""} · ${movement.mode}`
      : "";
    const realtime = diagnostics.voice || diagnostics.realtime;
    const conversation = diagnostics.conversation || {};
    const greeter = diagnostics.greeter || {};
    const greeterUnavailable = greeter.unavailable_reason || (movement?.error
      ? `Movement fault (${movement.error}). Check USB and external power, then reopen the Reachy launcher.` : '');
    if (!greeterGreetingLoaded && greeter.greeting) {
      document.querySelector('#greeter-greeting').value = greeter.greeting;
      greeterGreetingLoaded = true;
    }
    document.querySelector('#greeter-start').disabled = greeter.enabled || !!greeterUnavailable || conversation.camera_owner === 'luma';
    document.querySelector('#greeter-stop').disabled = !greeter.enabled;
    document.querySelector('#greeter-status').textContent = greeter.enabled
      ? `${diagnostics.motion?.speech_motion_enabled === false ? 'Movement paused' : greeter.face_present ? 'Face detected · Following' : 'Looking for arrivals'} · ${greeter.welcomes} welcomes${greeter.error ? ` · ${greeter.error}` : ''}`
      : greeterUnavailable || greeterActionError || 'Greeter off. Start it to watch for faces and welcome arrivals.';
    const autoGreeter = !greeterStartAttempted && greeter.auto_start && !greeter.enabled && !greeterUnavailable && conversation.camera_owner !== 'luma';
    greeterStartAttempted = true;
    cameraHandedToLuma = conversation.camera_owner === "luma" || conversation.pause_reason === "luma_express";
    document.querySelector("#start-voice-test").disabled = cameraHandedToLuma;
    document.querySelector("#motion-mode-notice").textContent = cameraHandedToLuma
      ? "GPT-Live host can look around, dance and play Hugging Face moves. Enable camera sharing above for visual questions; let Reachy settle before scanning a ticket."
      : "Start chat, then ask Roo to look around, dance, nod or wiggle his antennas.";
    document.querySelectorAll('[data-robot-motion]').forEach(button => {
      button.disabled = cameraHandedToLuma && conversation.mode !== 'event_host' && button.dataset.robotMotion !== 'stop';
    });
    document.querySelectorAll('#motion-library-form button, #motion-pose-form button').forEach(button => {
      button.disabled = cameraHandedToLuma && conversation.mode !== 'event_host';
    });
    const autoStart = !dashboardStartAttempted && conversation.dashboard_auto_start;
    dashboardStartAttempted = true;
    const vision = realtime?.vision;
    window.ReachyHostCamera?.update(diagnostics);
    document.querySelector("#model-vision-status").textContent = vision?.enabled
      ? `Model vision: Reachy camera snapshots on request · ${vision.images_sent} sent${vision.last_image_age_seconds == null ? "" : ` · Last capture ${Math.round(vision.last_image_age_seconds)}s ago`}${vision.last_error ? ` · ${vision.last_error}` : ""}`
      : "Model vision: unavailable in this profile.";
    document.querySelector("#model-vision-observation").textContent = vision?.last_observation
      ? `Last camera observation: ${vision.last_observation}` : "";
    const attended = conversation.mode === "attended_chat";
    document.querySelector("#start-voice-test").textContent = conversation.attended_available
      ? "Start chat" : "Start Voice Test";
    document.querySelector("#stop-voice-test").disabled = !realtime?.running && !greeter.enabled;
    document.querySelector("#voice-mode-notice").textContent = conversation.attended_available
      ? `${conversation.dashboard_auto_start ? "Opening this dashboard starts chat. " : ""}Chat streams Reachy's microphone to OpenAI until Stop chat or ${Math.ceil(conversation.max_seconds / 60)} minutes. Closing the tab does not stop chat; use Stop chat.${vision?.enabled ? " Visual questions send a fresh camera snapshot to OpenAI." : ""}`
      : "";
    const status = document.querySelector("#voice-test-status");
    const audioStatus = document.querySelector("#voice-audio-status");
    audioStatus.textContent = realtime?.full_duplex
      ? `Backend: ${realtime.backend_model} · Input frames: ${Number(realtime.input_frames_sent || 0).toLocaleString()} · Output frames: ${Number(realtime.output_frames_received || 0).toLocaleString()} · Speaking overlap: ${Number(realtime.simultaneous_input_output_frames || 0).toLocaleString()} frames`
      : "";
    if (cameraHandedToLuma) {
      status.textContent = conversation.mode === "event_host" && realtime?.running
        ? `GPT-Live · ${realtime.model} connected · Full duplex · Event host · ${Math.ceil(conversation.remaining_seconds / 60)} min left`
        : "Voice paused. Start GPT-Live host above to talk while Luma scans.";
      document.querySelector("#voice-mode-notice").textContent = "Luma owns the camera. Host voice supports greetings, talking motion, looks and dances. Local wakes and automatic chat stay paused.";
      if (!diagnostics.browser_camera?.connected) {
        document.querySelector("#model-vision-status").textContent = "Enable camera sharing above so GPT-Live can see while Luma scans.";
        document.querySelector("#model-vision-observation").textContent = "";
      }
    } else if (!realtime) {
      status.textContent = "OpenAI voice is disabled.";
    } else if (realtime.last_error) {
      status.textContent = `${realtime.api_label || "Voice"} error: ${realtime.last_error}`;
    } else if (!realtime.running) {
      status.textContent = `${realtime.api_label || "Voice"} · ${realtime.model} · ${conversation.attended_available ? "Click Start chat for continuous conversation." : "Say “Hey Reachy” or start a voice test."}`;
    } else {
      const focus = attended
        ? `Attended chat · microphone live · ${Math.ceil(conversation.remaining_seconds / 60)} min left`
        : diagnostics.attention?.face_present
        ? "Face in view"
        : "Move into Reachy's camera view to speak";
      const mode = diagnostics.attention?.degraded_reason ? " · Uncalibrated bench demo" : "";
      const duplex = realtime.full_duplex ? " · Full duplex" : "";
      status.textContent = `${realtime.api_label || "Realtime"} · ${realtime.model} connected${duplex} · ${focus}${mode}`;
    }
    if (greeter.enabled) {
      status.textContent = `Event greeter · ${greeter.face_present ? 'Following a face' : 'Watching for arrivals'} · ${realtime?.running ? 'GPT-Live connected' : 'Voice starts for the next arrival'}`;
      document.querySelector('#voice-mode-notice').textContent = 'Reachy owns his camera. Local detection controls turning and arrival welcomes; visual questions use fresh snapshots. Stop event greeter to finish.';
    }
    if (autoGreeter) await startEventGreeter(true);
    else if (autoStart && !realtime?.running) await startVoiceChat();
  } catch (error) {
    document.querySelector("#voice-test-status").textContent = "Voice service unavailable. Open Start Reachy.command to start the local services.";
    document.querySelector("#voice-audio-status").textContent = "";
  }
}

function addFeedEntry(text) {
  const item = document.createElement("div");
  item.className = "feed-entry";
  item.textContent = text;
  telemetryFeed.prepend(item);
  while (telemetryFeed.children.length > 200) telemetryFeed.lastElementChild.remove();
}

function connectTelemetry() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/telemetry`);
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    addFeedEntry(`${payload.timestamp} · ${payload.kind} · ${JSON.stringify(payload.payload)}`);
  };
  socket.onclose = () => {
    setTimeout(connectTelemetry, 1000);
  };
}

document.querySelector("#refresh-health").addEventListener("click", refreshHealth);
document.querySelector("#refresh-events").addEventListener("click", refreshEvents);
document.querySelector("#clear-feed").addEventListener("click", () => {
  telemetryFeed.innerHTML = "";
});

let movementCatalogue = { dances: [], emotions: [] };
document.querySelector("#robot-request-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = document.querySelector("#motion-result");
  try {
    await fetchJson("/api/admin/conversation/request", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: document.querySelector("#robot-request-text").value.trim()}),
    });
    result.textContent = "Request sent to GPT-Live. Watch motion status for the result.";
  } catch (error) {
    result.textContent = error.message;
  }
});
function refreshMovementOptions() {
  const kind = {dance: 'dances', emotion: 'emotions', recording: 'community'}[document.querySelector("#motion-kind").value];
  const query = document.querySelector("#motion-search").value.trim().toLowerCase();
  const moves = (movementCatalogue[kind] || []).map(value => typeof value === 'string'
    ? {name: value, title: value.replaceAll('_', ' ')} : value);
  const matches = moves.filter(move => `${move.title} ${move.name} ${move.repo || ''}`.toLowerCase().includes(query));
  const select = document.querySelector("#motion-name");
  select.replaceChildren(...matches.slice(0, 200).map((move) => {
    const option = document.createElement("option");
    option.value = move.name;
    option.textContent = `${move.title}${move.repo ? ` — ${move.repo}` : ''}`;
    return option;
  }));
  document.querySelector('#motion-search-status').textContent = `${matches.length} matches${matches.length > 200 ? ' · Showing first 200; narrow your search to find more' : ''}`;
  if (kind === "dances" && matches.some(move => move.name === "groovy_sway_and_roll")) {
    select.value = "groovy_sway_and_roll";
  }
}
async function sendRobotMotion(payload) {
  const result = document.querySelector("#motion-result");
  try {
    const response = await fetchJson("/api/admin/robot/motion", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    const amplitude = response.amplitude_scale < 1 ? ` · ${Math.round(response.amplitude_scale * 100)}% amplitude for reachability` : "";
    result.textContent = `${response.status}${response.movement ? ` · ${response.movement}` : ""}${amplitude}${response.reason ? ` · ${response.reason.replaceAll("_", " ")}` : ""}`;
  } catch (error) {
    result.textContent = `Movement failed: ${error.message}`;
  }
}
document.querySelectorAll("[data-robot-motion]").forEach((button) => {
  button.addEventListener("click", () => sendRobotMotion({
    action: button.dataset.robotMotion,
    ...(button.dataset.direction ? { direction: button.dataset.direction } : {}),
    ...(button.dataset.name ? { name: button.dataset.name } : {}),
  }));
});
document.querySelector("#motion-kind").addEventListener("change", refreshMovementOptions);
document.querySelector("#motion-search").addEventListener("input", refreshMovementOptions);
document.querySelector("#motion-library-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!document.querySelector("#motion-name").value) return;
  await sendRobotMotion({action: document.querySelector("#motion-kind").value,
    name: document.querySelector("#motion-name").value, duration_seconds: 12});
});
document.querySelector("#motion-pose-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {action: "pose"};
  for (const [key, value] of new FormData(event.currentTarget)) {
    if (value !== "") payload[key] = Number(value);
  }
  await sendRobotMotion(payload);
});
function renderMovementCatalogue(catalogue) {
  movementCatalogue = catalogue;
  refreshMovementOptions();
  document.querySelector("#motion-result").textContent = `${catalogue.dances.length} official dances · ${catalogue.emotions.length} expressions · ${catalogue.community_count || 0} community moves · Head, body and both antennas`;
}
document.querySelector('#motion-reload').addEventListener('click', async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try { renderMovementCatalogue(await fetchJson('/api/admin/robot/motions/reload', {method:'POST'})); }
  catch (error) { document.querySelector('#motion-result').textContent = error.message; }
  finally { button.disabled = false; }
});
fetchJson("/api/admin/robot/motions").then(renderMovementCatalogue).catch(() => {
  document.querySelector("#motion-result").textContent = "Movement controls are waiting for the robot runtime.";
});

document.querySelector("#speech-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = document.querySelector("#speech-text").value.trim();
  if (!text) return;
  await fetchJson("/api/admin/test-speech", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
});

document.querySelector("#booking-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = document.querySelector("#booking-user-id").value.trim();
  const output = document.querySelector("#booking-result");
  if (!userId) return;
  try {
    const payload = await fetchJson(`/api/admin/users/${userId}/booking-check`);
    output.textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    output.textContent = String(error);
  }
});

document.querySelector("#simulate-attempt").addEventListener("click", async () => {
  await fetchJson("/api/admin/simulate/attempt", { method: "POST" });
  await refreshEvents();
});

async function startVoiceChat() {
  if (chatStartPending) return;
  dashboardStartAttempted = true;
  chatStartPending = true;
  const button = document.querySelector("#start-voice-test");
  const status = document.querySelector("#voice-test-status");
  button.disabled = true;
  status.textContent = "Connecting to Roo…";
  try {
    const result = await fetchJson("/api/admin/test-conversation", { method: "POST" });
    await refreshVoice();
    status.textContent = result.running
      ? `${result.api_label || "Voice"} connected. Speak normally, including while Roo talks.`
      : "Voice did not start.";
  } catch (error) {
    status.textContent = `Voice test failed: ${String(error)}`;
  } finally {
    button.disabled = false;
    chatStartPending = false;
  }
}
document.querySelector("#start-voice-test").addEventListener("click", startVoiceChat);

document.querySelector("#stop-voice-test").addEventListener("click", async (event) => {
  dashboardStartAttempted = true;
  event.currentTarget.disabled = true;
  try {
    await fetchJson("/api/admin/conversation/stop", {method: "POST"});
    await refreshVoice();
  } catch (error) {
    document.querySelector("#voice-test-status").textContent = `Could not stop chat: ${error.message}`;
    event.currentTarget.disabled = false;
  }
});

fetchJson("/api/admin/biometric-notice").then((notice) => {
  document.querySelector("#biometric-notice").textContent = notice.notice_text;
  const form = document.querySelector("#enrollment-form");
  form.notice_version.value = notice.notice_version;
  form.notice_sha256.value = notice.notice_sha256;
  document.querySelector("#enroll-submit").disabled = false;
}).catch(() => {
  document.querySelector("#biometric-notice").textContent = "The face-check notice is unavailable. Please use staff check-in.";
});

document.querySelector("#enrollment-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const notice = await fetchJson("/api/admin/biometric-notice");
  form.notice_version.value = notice.notice_version;
  form.notice_sha256.value = notice.notice_sha256;
  const files = document.querySelector("#enrollment-files").files;
  const samples = await Promise.all(
    Array.from(files).map(async (file, index) => ({
      label: `sample-${index + 1}`,
      image_b64: (await fileToBase64(file)).split(",")[1],
    })),
  );

  const payload = {
    full_name: form.full_name.value,
    slack_user_id: form.slack_user_id.value,
    slack_display_name: form.slack_display_name.value,
    company: form.company.value,
    biometric_consent: form.biometric_consent.checked,
    greet_by_name: form.greet_by_name.checked,
    operator_id: form.operator_id.value,
    notice_version: form.notice_version.value,
    notice_sha256: form.notice_sha256.value,
    samples,
  };

  const output = document.querySelector("#enrollment-result");
  try {
    const result = await fetchJson("/api/admin/enroll", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    output.textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    output.textContent = String(error);
  }
});

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

refreshHealth();
refreshEvents();
refreshCamera();
refreshVoice();
setInterval(refreshCamera, 2000);
setInterval(refreshVoice, 2000);
setInterval(refreshHealth, 5000);
connectTelemetry();
