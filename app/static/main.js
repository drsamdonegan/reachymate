const healthTableBody = document.querySelector("#health-table tbody");
const healthSummary = document.querySelector("#health-summary");
const eventsList = document.querySelector("#events-list");
const telemetryFeed = document.querySelector("#telemetry-feed");
const cameraFeed = document.querySelector("#camera-feed");
const cameraStatus = document.querySelector("#camera-status");

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
  healthSummary.textContent = `Conversation: ${payload.conversation_ready ? "ready" : "not ready"} · Access: ${payload.access_ready ? "ready" : "shadow only"}`;
  healthTableBody.innerHTML = "";
  Object.entries(payload.components).forEach(([component, status]) => {
    const row = document.createElement("tr");
    const statusClass =
      status === "HEALTHY" ? "status-ok" : status === "DEGRADED" ? "status-warn" : "status-bad";
    row.innerHTML = `<td>${component}</td><td class="${statusClass}">${status}</td>`;
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
  try {
    const response = await fetch(`/camera/snapshot?ts=${Date.now()}`);
    if (!response.ok) {
      throw new Error("camera unavailable");
    }
    const blob = await response.blob();
    cameraFeed.src = URL.createObjectURL(blob);
    cameraStatus.textContent = "Live";
  } catch (error) {
    cameraStatus.textContent = "Camera unavailable";
  }
}

function addFeedEntry(text) {
  const item = document.createElement("div");
  item.className = "feed-entry";
  item.textContent = text;
  telemetryFeed.prepend(item);
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

document.querySelector("#start-voice-test").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const status = document.querySelector("#voice-test-status");
  button.disabled = true;
  status.textContent = "Connecting to Roo…";
  try {
    const result = await fetchJson("/api/admin/test-conversation", { method: "POST" });
    status.textContent = result.running
      ? "Roo is listening. Speak normally; you can interrupt it."
      : "Realtime did not start.";
  } catch (error) {
    status.textContent = `Voice test failed: ${String(error)}`;
  } finally {
    button.disabled = false;
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
setInterval(refreshCamera, 2000);
connectTelemetry();
