async function fetchStatus() {
  try {
    const url = new URL("/status", window.location.origin);
    url.searchParams.set("_", Date.now().toString());
    const resp = await fetchWithTimeout(url, {}, 2000);
    if (!resp.ok) throw new Error("status error");
    return await resp.json();
  } catch (e) {
    return { has_key: false, error: true };
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function fetchWithTimeout(url, options = {}, timeoutMs = 2000) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(id);
  }
}

async function waitForStatus(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (true) {
    try {
      const url = new URL("/status", window.location.origin);
      url.searchParams.set("_", Date.now().toString());
      const resp = await fetchWithTimeout(url, {}, 2000);
      if (resp.ok) return await resp.json();
    } catch (e) {}
    if (Date.now() >= deadline) return null;
    await sleep(500);
  }
}

async function waitForPersonalityData(timeoutMs = 15000) {
  const loadingText = document.querySelector("#loading p");
  let attempts = 0;
  const deadline = Date.now() + timeoutMs;
  while (true) {
    attempts += 1;
    try {
      const url = new URL("/personalities", window.location.origin);
      url.searchParams.set("_", Date.now().toString());
      const resp = await fetchWithTimeout(url, {}, 2000);
      if (resp.ok) return await resp.json();
    } catch (e) {}

    if (loadingText) {
      loadingText.textContent = attempts > 8 ? "Starting backend…" : "Loading…";
    }
    if (Date.now() >= deadline) return null;
    await sleep(500);
  }
}

async function validateKey(key) {
  const body = { openai_api_key: key };
  const resp = await fetch("/validate_api_key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || "validation_failed");
  }
  return data;
}

async function saveKey(key) {
  const body = { openai_api_key: key };
  const resp = await fetch("/openai_api_key", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.error || "save_failed");
  }
  return await resp.json();
}

// ---------- Personalities API ----------
async function getPersonalities() {
  const url = new URL("/personalities", window.location.origin);
  url.searchParams.set("_", Date.now().toString());
  const resp = await fetchWithTimeout(url, {}, 2000);
  if (!resp.ok) throw new Error("list_failed");
  return await resp.json();
}

async function loadPersonality(name) {
  const url = new URL("/personalities/load", window.location.origin);
  url.searchParams.set("name", name);
  url.searchParams.set("_", Date.now().toString());
  const resp = await fetchWithTimeout(url, {}, 3000);
  if (!resp.ok) throw new Error("load_failed");
  return await resp.json();
}

async function savePersonality(payload) {
  // Try JSON POST first
  const saveUrl = new URL("/personalities/save", window.location.origin);
  saveUrl.searchParams.set("_", Date.now().toString());
  let resp = await fetchWithTimeout(saveUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }, 5000);
  if (resp.ok) return await resp.json();

  // Fallback to form-encoded POST
  try {
    const form = new URLSearchParams();
    form.set("name", payload.name || "");
    form.set("instructions", payload.instructions || "");
    form.set("tools_text", payload.tools_text || "");
    form.set("voice", payload.voice || "cedar");
    const url = new URL("/personalities/save_raw", window.location.origin);
    url.searchParams.set("_", Date.now().toString());
    resp = await fetchWithTimeout(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form.toString(),
    }, 5000);
    if (resp.ok) return await resp.json();
  } catch {}

  // Fallback to GET (query params)
  try {
    const url = new URL("/personalities/save_raw", window.location.origin);
    url.searchParams.set("name", payload.name || "");
    url.searchParams.set("instructions", payload.instructions || "");
    url.searchParams.set("tools_text", payload.tools_text || "");
    url.searchParams.set("voice", payload.voice || "cedar");
    url.searchParams.set("_", Date.now().toString());
    resp = await fetchWithTimeout(url, { method: "GET" }, 5000);
    if (resp.ok) return await resp.json();
  } catch {}

  const data = await resp.json().catch(() => ({}));
  throw new Error(data.error || "save_failed");
}

async function applyPersonality(name, { persist = false } = {}) {
  // Send as query param to avoid any body parsing issues on the server
  const url = new URL("/personalities/apply", window.location.origin);
  url.searchParams.set("name", name || "");
  if (persist) {
    url.searchParams.set("persist", "1");
  }
  url.searchParams.set("_", Date.now().toString());
  const resp = await fetchWithTimeout(url, { method: "POST" }, 5000);
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.error || "apply_failed");
  }
  return await resp.json();
}

async function getVoices() {
  try {
    const url = new URL("/voices", window.location.origin);
    url.searchParams.set("_", Date.now().toString());
    const resp = await fetchWithTimeout(url, {}, 3000);
    if (!resp.ok) throw new Error("voices_failed");
    return await resp.json();
  } catch (e) {
    return ["cedar"];
  }
}

function show(el, flag) {
  el.classList.toggle("hidden", !flag);
}

async function init() {
  const loading = document.getElementById("loading");
  show(loading, true);
  const statusEl = document.getElementById("status");
  const formPanel = document.getElementById("form-panel");
  const configuredPanel = document.getElementById("configured");
  const personalityPanel = document.getElementById("personality-panel");
  const saveBtn = document.getElementById("save-btn");
  const changeKeyBtn = document.getElementById("change-key-btn");
  const input = document.getElementById("api-key");

  // Personality elements
  const pSelect = document.getElementById("personality-select");
  const pApply = document.getElementById("apply-personality");
  const pPersist = document.getElementById("persist-personality");
  const pNew = document.getElementById("new-personality");
  const pSave = document.getElementById("save-personality");
  const pStartupLabel = document.getElementById("startup-label");
  const pName = document.getElementById("personality-name");
  const pInstr = document.getElementById("instructions-ta");
  const pTools = document.getElementById("tools-ta");
  const pStatus = document.getElementById("personality-status");
  const pVoice = document.getElementById("voice-select");
  const pAvail = document.getElementById("tools-available");

  const AUTO_WITH = {
    dance: ["stop_dance"],
    play_emotion: ["stop_emotion"],
  };

  statusEl.textContent = "Checking configuration...";
  show(formPanel, false);
  show(configuredPanel, false);
  show(personalityPanel, false);

  const st = (await waitForStatus()) || { has_key: false };
  if (st.has_key) {
    statusEl.textContent = "";
    show(configuredPanel, true);
  }

  // Handler for "Change API key" button
  changeKeyBtn.addEventListener("click", () => {
    show(configuredPanel, false);
    show(formPanel, true);
    input.value = "";
    statusEl.textContent = "";
    statusEl.className = "status";
  });

  // Remove error styling when user starts typing
  input.addEventListener("input", () => {
    input.classList.remove("error");
  });

  saveBtn.addEventListener("click", async () => {
    const key = input.value.trim();
    if (!key) {
      statusEl.textContent = "Please enter a valid key.";
      statusEl.className = "status warn";
      input.classList.add("error");
      return;
    }
    statusEl.textContent = "Validating API key...";
    statusEl.className = "status";
    input.classList.remove("error");
    try {
      // First validate the key
      const validation = await validateKey(key);
      if (!validation.valid) {
        statusEl.textContent = "Invalid API key. Please check your key and try again.";
        statusEl.className = "status error";
        input.classList.add("error");
        return;
      }

      // If valid, save it
      statusEl.textContent = "Key valid! Saving...";
      statusEl.className = "status ok";
      await saveKey(key);
      statusEl.textContent = "Saved. Reloading…";
      statusEl.className = "status ok";
      window.location.reload();
    } catch (e) {
      input.classList.add("error");
      if (e.message === "invalid_api_key") {
        statusEl.textContent = "Invalid API key. Please check your key and try again.";
      } else {
        statusEl.textContent = "Failed to validate/save key. Please try again.";
      }
      statusEl.className = "status error";
    }
  });

  if (!st.has_key) {
    statusEl.textContent = "";
    show(formPanel, true);
    show(loading, false);
    return;
  }

  // ===== Camera & Chat panels (visible once key is set) =====
  const visionPanel = document.getElementById("vision-panel");
  const chatPanel = document.getElementById("chat-panel");
  const controlsBar = document.getElementById("controls-bar");
  show(visionPanel, true);
  show(chatPanel, true);
  show(controlsBar, true);
  startCameraFeed();
  startChatPolling();
  initControls();
  initChatInput();

  // Wait until backend routes are ready before rendering personalities UI
  const list = (await waitForPersonalityData()) || { choices: [] };
  statusEl.textContent = "";
  show(formPanel, false);
  if (!list.choices.length) {
    statusEl.textContent = "Personality endpoints not ready yet. Retry shortly.";
    statusEl.className = "status warn";
    show(loading, false);
    return;
  }

  // Initialize personalities UI
  try {
    const choices = Array.isArray(list.choices) ? list.choices : [];
    const DEFAULT_OPTION = choices[0] || "(built-in default)";
    const startupChoice = choices.includes(list.startup) ? list.startup : DEFAULT_OPTION;
    const currentChoice = choices.includes(list.current) ? list.current : startupChoice;

    function setStartupLabel(name) {
      const display = name && name !== DEFAULT_OPTION ? name : "Built-in default";
      pStartupLabel.textContent = `Launch on start: ${display}`;
    }

    // Populate select
    pSelect.innerHTML = "";
    for (const n of choices) {
      const opt = document.createElement("option");
      opt.value = n;
      opt.textContent = n;
      pSelect.appendChild(opt);
    }
    if (choices.length) {
      const preferred = choices.includes(startupChoice) ? startupChoice : currentChoice;
      pSelect.value = preferred;
    }
    const voices = await getVoices();
    pVoice.innerHTML = "";
    for (const v of voices) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v;
      pVoice.appendChild(opt);
    }
    setStartupLabel(startupChoice);

    function renderToolCheckboxes(available, enabled) {
      pAvail.innerHTML = "";
      const enabledSet = new Set(enabled);
      for (const t of available) {
        const wrap = document.createElement("div");
        wrap.className = "chk";
        const id = `tool-${t}`;
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.id = id;
        cb.value = t;
        cb.checked = enabledSet.has(t);
        const lab = document.createElement("label");
        lab.htmlFor = id;
        lab.textContent = t;
        wrap.appendChild(cb);
        wrap.appendChild(lab);
        pAvail.appendChild(wrap);
      }
    }

    function getSelectedTools() {
      const selected = new Set();
      pAvail.querySelectorAll('input[type="checkbox"]').forEach((el) => {
        if (el.checked) selected.add(el.value);
      });
      // Auto-include dependencies
      for (const [main, deps] of Object.entries(AUTO_WITH)) {
        if (selected.has(main)) {
          for (const d of deps) selected.add(d);
        }
      }
      return Array.from(selected);
    }

    function syncToolsTextarea() {
      const selected = getSelectedTools();
      const comments = pTools.value
        .split("\n")
        .filter((ln) => ln.trim().startsWith("#"));
      const body = selected.join("\n");
      pTools.value = (comments.join("\n") + (comments.length ? "\n" : "") + body).trim() + "\n";
    }

    function attachToolHandlers() {
      pAvail.addEventListener("change", (ev) => {
        const target = ev.target;
        if (!(target instanceof HTMLInputElement) || target.type !== "checkbox") return;
        const name = target.value;
        // If a main tool toggled, propagate to deps
        if (AUTO_WITH[name]) {
          for (const dep of AUTO_WITH[name]) {
            const depEl = pAvail.querySelector(`input[value="${dep}"]`);
            if (depEl) depEl.checked = target.checked || depEl.checked;
          }
        }
        syncToolsTextarea();
      });
    }

    async function loadSelected() {
      const selected = pSelect.value;
      const data = await loadPersonality(selected);
      pInstr.value = data.instructions || "";
      pTools.value = data.tools_text || "";
      pVoice.value = data.voice || "cedar";
      // Available tools as checkboxes
      renderToolCheckboxes(data.available_tools, data.enabled_tools);
      attachToolHandlers();
      // Default name field to last segment of selection
      const idx = selected.lastIndexOf("/");
      pName.value = idx >= 0 ? selected.slice(idx + 1) : "";
      pStatus.textContent = `Loaded ${selected}`;
      pStatus.className = "status";
    }

    pSelect.addEventListener("change", loadSelected);
    await loadSelected();
    show(personalityPanel, true);

    // pAvail change handler registered in attachToolHandlers()

    pApply.addEventListener("click", async () => {
      pStatus.textContent = "Applying...";
      pStatus.className = "status";
      try {
        const res = await applyPersonality(pSelect.value);
        if (res.startup) setStartupLabel(res.startup);
        pStatus.textContent = res.status || "Applied.";
        pStatus.className = "status ok";
      } catch (e) {
        pStatus.textContent = `Failed to apply${e.message ? ": " + e.message : ""}`;
        pStatus.className = "status error";
      }
    });

    pPersist.addEventListener("click", async () => {
      pStatus.textContent = "Saving for startup...";
      pStatus.className = "status";
      try {
        const res = await applyPersonality(pSelect.value, { persist: true });
        if (res.startup) setStartupLabel(res.startup);
        pStatus.textContent = res.status || "Saved for startup.";
        pStatus.className = "status ok";
      } catch (e) {
        pStatus.textContent = `Failed to persist${e.message ? ": " + e.message : ""}`;
        pStatus.className = "status error";
      }
    });

    pNew.addEventListener("click", () => {
      pName.value = "";
      pInstr.value = "# Write your instructions here\n# e.g., Keep responses concise and friendly.";
      pTools.value = "# tools enabled for this profile\n";
      // Keep available tools list, clear selection
      pAvail.querySelectorAll('input[type="checkbox"]').forEach((el) => {
        el.checked = false;
      });
      pVoice.value = "cedar";
      pStatus.textContent = "Fill fields and click Save.";
      pStatus.className = "status";
    });

    pSave.addEventListener("click", async () => {
      const name = (pName.value || "").trim();
      if (!name) {
        pStatus.textContent = "Enter a valid name.";
        pStatus.className = "status warn";
        return;
      }
      pStatus.textContent = "Saving...";
      pStatus.className = "status";
      try {
        // Ensure tools.txt reflects checkbox selection and auto-includes
        syncToolsTextarea();
        const res = await savePersonality({
          name,
          instructions: pInstr.value || "",
          tools_text: pTools.value || "",
          voice: pVoice.value || "cedar",
        });
        // Refresh select choices
        pSelect.innerHTML = "";
        for (const n of res.choices) {
          const opt = document.createElement("option");
          opt.value = n;
          opt.textContent = n;
          if (n === res.value) opt.selected = true;
          pSelect.appendChild(opt);
        }
        pStatus.textContent = "Saved.";
        pStatus.className = "status ok";
        // Auto-apply
        try { await applyPersonality(pSelect.value); } catch {}
      } catch (e) {
        pStatus.textContent = "Failed to save.";
        pStatus.className = "status error";
      }
    });
  } catch (e) {
    statusEl.textContent = "UI failed to load. Please refresh.";
    statusEl.className = "status warn";
  } finally {
    // Hide loading when initial setup is done (regardless of key presence)
    show(loading, false);
  }
}

// ===== Camera Feed =====
let cameraInterval = null;
let browserCameraStream = null;
let browserUploadInterval = null;
let browserCameraDeviceId = null;
const browserCameraSourceId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;

async function blobToDataUrl(blob) {
  return await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

function startCameraFeed() {
  const img = document.getElementById("camera-feed");
  const offline = document.getElementById("camera-offline");
  const takeBtn = document.getElementById("take-photo");
  const photoStatus = document.getElementById("photo-status");
  const gallery = document.getElementById("photo-gallery");
  const deviceRow = document.getElementById("camera-device-row");
  const deviceSelect = document.getElementById("camera-device-select");
  if (!img) return;

  let consecutive_fails = 0;
  let browserUploadInFlight = false;

  function cameraPreferenceScore(device) {
    const label = (device.label || "").toLowerCase();
    let score = 0;
    if (/(reachy|pollen)/.test(label)) score += 100;
    if (/(usb|uvc|external)/.test(label)) score += 40;
    if (/(facetime|built-in|continuity|iphone|ipad)/.test(label)) score -= 60;
    return score;
  }

  function preferredCameraDevice(cameras, currentDeviceId) {
    if (!cameras.length) return null;
    const ranked = [...cameras].sort((a, b) => cameraPreferenceScore(b) - cameraPreferenceScore(a));
    const best = ranked[0];
    if (!best || best.deviceId === currentDeviceId) return null;
    return cameraPreferenceScore(best) > 0 ? best : null;
  }

  function stopBrowserCameraUpload() {
    if (browserUploadInterval) {
      clearInterval(browserUploadInterval);
      browserUploadInterval = null;
    }
    if (browserCameraStream) {
      browserCameraStream.getTracks().forEach((track) => track.stop());
      browserCameraStream = null;
    }
  }

  async function populateCameraDevices(selectedId) {
    if (!deviceSelect || !navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return [];
    const devices = await navigator.mediaDevices.enumerateDevices();
    const cameras = devices.filter((device) => device.kind === "videoinput");
    deviceSelect.innerHTML = "";
    cameras.forEach((device, index) => {
      const option = document.createElement("option");
      option.value = device.deviceId;
      option.textContent = device.label || `Camera ${index + 1}`;
      option.selected = device.deviceId === selectedId;
      deviceSelect.appendChild(option);
    });
    if (deviceRow) show(deviceRow, cameras.length > 1);
    return cameras;
  }

  async function startBrowserCameraUpload(deviceId = browserCameraDeviceId) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      offline.textContent = "Browser camera unavailable";
      return;
    }

    stopBrowserCameraUpload();

    try {
      const videoConstraints = {
        width: { ideal: 1280 },
        height: { ideal: 720 },
      };
      if (deviceId) {
        videoConstraints.deviceId = { exact: deviceId };
      }

      browserCameraStream = await navigator.mediaDevices.getUserMedia({
        video: videoConstraints,
        audio: false,
      });

      const video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = browserCameraStream;
      await video.play();

      const track = browserCameraStream.getVideoTracks()[0];
      browserCameraDeviceId = (track && track.getSettings().deviceId) || deviceId || null;
      const cameras = await populateCameraDevices(browserCameraDeviceId);
      const selectedCamera = cameras.find((camera) => camera.deviceId === browserCameraDeviceId);
      const sourceLabel = selectedCamera ? selectedCamera.label : "";
      if (!deviceId) {
        const preferred = preferredCameraDevice(cameras, browserCameraDeviceId);
        if (preferred) {
          browserCameraDeviceId = preferred.deviceId;
          return startBrowserCameraUpload(browserCameraDeviceId);
        }
      }

      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");

      browserUploadInterval = setInterval(async () => {
        if (browserUploadInFlight || !ctx || video.readyState < 2) return;
        browserUploadInFlight = true;
        try {
          const width = video.videoWidth || 640;
          const height = video.videoHeight || 480;
          canvas.width = width;
          canvas.height = height;
          ctx.drawImage(video, 0, 0, width, height);
          const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.78));
          if (!blob) return;
          const image_b64 = await blobToDataUrl(blob);
          await fetchWithTimeout(
            "/camera/browser-frame",
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                image_b64,
                source_id: browserCameraSourceId,
                source_label: sourceLabel,
              }),
            },
            3000,
          );
        } catch {
          // The snapshot poll below will keep the visible state accurate.
        } finally {
          browserUploadInFlight = false;
        }
      }, 500);
    } catch {
      offline.textContent = "Camera permission needed";
      img.style.display = "none";
      offline.style.display = "flex";
    }
  }

  if (deviceSelect) {
    deviceSelect.addEventListener("change", () => {
      browserCameraDeviceId = deviceSelect.value || null;
      startBrowserCameraUpload(browserCameraDeviceId);
    });
  }

  async function refreshFrame() {
    try {
      const resp = await fetchWithTimeout(
        `/camera/snapshot?_=${Date.now()}`,
        {},
        3000,
      );
      if (resp.ok) {
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const old = img.src;
        img.src = url;
        img.style.display = "block";
        offline.style.display = "none";
        if (old && old.startsWith("blob:")) URL.revokeObjectURL(old);
        consecutive_fails = 0;
      } else {
        throw new Error("not ok");
      }
    } catch {
      consecutive_fails++;
      if (consecutive_fails > 3) {
        img.style.display = "none";
        offline.style.display = "flex";
      }
    }
  }

  fetchWithTimeout("/camera/source", {}, 2000)
    .then((resp) => resp.ok ? resp.json() : null)
    .then((data) => {
      if (data && data.browser_input) startBrowserCameraUpload();
    })
    .catch(() => {});

  refreshFrame();
  cameraInterval = setInterval(refreshFrame, 500);

  if (takeBtn) {
    takeBtn.addEventListener("click", async () => {
      photoStatus.textContent = "Capturing...";
      photoStatus.className = "status";
      try {
        const resp = await fetchWithTimeout(
          "/camera/photo",
          { method: "POST" },
          5000,
        );
        const data = await resp.json();
        if (data.ok && data.image) {
          photoStatus.textContent = "Photo captured!";
          photoStatus.className = "status ok";
          const wrapper = document.createElement("div");
          wrapper.className = "photo-thumb";
          const thumb = document.createElement("img");
          thumb.src = `data:image/jpeg;base64,${data.image}`;
          thumb.alt = "Captured photo";
          thumb.addEventListener("click", () => {
            const w = window.open("");
            if (w) {
              const imgEl = w.document.createElement("img");
              imgEl.src = thumb.src;
              imgEl.style.maxWidth = "100%";
              imgEl.style.background = "#000";
              w.document.body.style.margin = "0";
              w.document.body.style.background = "#000";
              w.document.body.appendChild(imgEl);
            }
          });
          const dl = document.createElement("a");
          dl.href = thumb.src;
          dl.download = `reachy_photo_${Date.now()}.jpg`;
          dl.className = "photo-download";
          dl.textContent = "Save";
          wrapper.appendChild(thumb);
          wrapper.appendChild(dl);
          gallery.prepend(wrapper);
        } else {
          photoStatus.textContent = data.error || "Failed";
          photoStatus.className = "status error";
        }
      } catch {
        photoStatus.textContent = "Camera unavailable";
        photoStatus.className = "status error";
      }
    });
  }
}

// ===== Chat Polling =====
let chatPollInterval = null;
let chatSeq = 0;

function startChatPolling() {
  const container = document.getElementById("chat-messages");
  if (!container) return;

  async function poll() {
    try {
      const resp = await fetchWithTimeout(
        `/chat/poll?after=${chatSeq}&_=${Date.now()}`,
        {},
        3000,
      );
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.seq > chatSeq) chatSeq = data.seq;
      if (!data.messages || !data.messages.length) return;

      const empty = container.querySelector(".chat-empty");
      if (empty) empty.remove();

      for (const msg of data.messages) {
        const bubble = document.createElement("div");
        bubble.className = `chat-bubble chat-${msg.role}`;
        if (msg.tool) bubble.classList.add("chat-tool");

        const label = document.createElement("span");
        label.className = "chat-label";
        label.textContent = msg.role === "user" ? "You" : "Reachy";

        bubble.appendChild(label);

        // Render image if present
        if (msg.image) {
          const img = document.createElement("img");
          img.src = msg.image;
          img.alt = "Camera snapshot";
          img.className = "chat-image";
          img.addEventListener("click", () => {
            const w = window.open("");
            if (w) {
              const el = w.document.createElement("img");
              el.src = img.src;
              el.style.maxWidth = "100%";
              el.style.background = "#000";
              w.document.body.style.margin = "0";
              w.document.body.style.background = "#000";
              w.document.body.appendChild(el);
            }
          });
          bubble.appendChild(img);
        }

        const text = document.createElement("span");
        text.className = "chat-text";
        let display = msg.content;
        if (msg.tool && !msg.image && display.length > 200) {
          display = display.slice(0, 200) + "...";
        }
        text.textContent = display;

        bubble.appendChild(text);
        container.appendChild(bubble);
      }
      container.scrollTop = container.scrollHeight;
    } catch {
      // silent
    }
  }

  poll();
  chatPollInterval = setInterval(poll, 1000);
}

// ===== Controls: Sleep / Wake / Mute =====
function initControls() {
  const btnSleep = document.getElementById("btn-sleep");
  const btnWakeup = document.getElementById("btn-wakeup");
  const btnMute = document.getElementById("btn-mute");
  const controlsStatus = document.getElementById("controls-status");
  let isMuted = false;
  let isSleeping = false;

  function updateSleepUI() {
    btnSleep.disabled = isSleeping;
    btnWakeup.disabled = !isSleeping;
    btnMute.disabled = isSleeping;
    const chatInput = document.getElementById("chat-input");
    const chatSend = document.getElementById("chat-send");
    if (chatInput) chatInput.disabled = isSleeping;
    if (chatSend) chatSend.disabled = isSleeping;
    if (isSleeping) {
      controlsStatus.textContent = "Sleeping";
      controlsStatus.className = "status warn";
    }
  }

  btnSleep.addEventListener("click", async () => {
    controlsStatus.textContent = "Going to sleep...";
    controlsStatus.className = "status";
    btnSleep.disabled = true;
    try {
      const resp = await fetchWithTimeout("/robot/sleep", { method: "POST" }, 20000);
      const data = await resp.json().catch(() => ({}));
      if (data.ok) {
        isSleeping = true;
        updateSleepUI();
      } else {
        controlsStatus.textContent = data.error || "Failed";
        controlsStatus.className = "status error";
        btnSleep.disabled = false;
      }
    } catch {
      controlsStatus.textContent = "Request failed";
      controlsStatus.className = "status error";
      btnSleep.disabled = false;
    }
  });

  btnWakeup.addEventListener("click", async () => {
    controlsStatus.textContent = "Waking up...";
    controlsStatus.className = "status";
    btnWakeup.disabled = true;
    try {
      const resp = await fetchWithTimeout("/robot/wakeup", { method: "POST" }, 20000);
      const data = await resp.json().catch(() => ({}));
      if (data.ok) {
        isSleeping = false;
        controlsStatus.textContent = "Awake";
        controlsStatus.className = "status ok";
        updateSleepUI();
      } else {
        controlsStatus.textContent = data.error || "Failed";
        controlsStatus.className = "status error";
        btnWakeup.disabled = false;
      }
    } catch {
      controlsStatus.textContent = "Request failed";
      controlsStatus.className = "status error";
      btnWakeup.disabled = false;
    }
  });

  btnMute.addEventListener("click", async () => {
    isMuted = !isMuted;
    try {
      const url = new URL("/audio/mute", window.location.origin);
      url.searchParams.set("muted", isMuted ? "true" : "false");
      await fetchWithTimeout(url, { method: "POST" }, 3000);
    } catch {
      isMuted = !isMuted; // revert on failure
    }
    btnMute.textContent = isMuted ? "Unmute mic" : "Mute mic";
    btnMute.classList.toggle("btn-muted", isMuted);
  });

  // Sync initial state
  Promise.all([
    fetchWithTimeout("/audio/status", {}, 2000).then((r) => r.json()).catch(() => ({})),
    fetchWithTimeout("/robot/status", {}, 2000).then((r) => r.json()).catch(() => ({})),
  ]).then(([audio, robot]) => {
    isMuted = !!audio.muted;
    btnMute.textContent = isMuted ? "Unmute mic" : "Mute mic";
    btnMute.classList.toggle("btn-muted", isMuted);
    isSleeping = !!robot.sleeping;
    updateSleepUI();
  });
}

// ===== Chat Text Input =====
function initChatInput() {
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("chat-send");
  if (!input || !sendBtn) return;

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendBtn.disabled = true;
    try {
      await fetchWithTimeout(
        "/chat/send",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        },
        10000,
      );
    } catch {
      // message will not appear in chat if send failed
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
}

window.addEventListener("DOMContentLoaded", init);
