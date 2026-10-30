// Chrome shares the USB camera between its tabs. Only requested stills leave this tab.
(() => {
  const base = '/api/admin/event-checkin/host/camera';
  const start = document.querySelector('#host-camera-start');
  const stop = document.querySelector('#host-camera-stop');
  const status = document.querySelector('#host-camera-status');
  const video = document.querySelector('#host-camera-preview');
  let stream = null, token = null, controller = null, timer = null;
  let hostRunning = false, connecting = false, epoch = 0;
  const isReachy = label => /reachy.*camera/i.test(label || '');

  function buttons() {
    start.disabled = !hostRunning || connecting || Boolean(token);
    stop.disabled = !connecting && !token;
  }

  function disconnect(message = 'Camera sharing off. Luma keeps its own camera stream.') {
    epoch += 1;
    const oldToken = token;
    token = null;
    connecting = false;
    clearTimeout(timer);
    controller?.abort();
    controller = null;
    stream?.getTracks().forEach(track => track.stop());
    stream = null;
    video.srcObject = null;
    video.hidden = true;
    status.textContent = message;
    buttons();
    if (oldToken) fetch(`${base}/stop`, {
      method: 'POST', headers: {Authorization: `Bearer ${oldToken}`}, keepalive: true
    }).catch(() => {}); // Server also expires disconnected browser leases.
  }

  async function request(path, options = {}) {
    const response = await fetch(`${base}${path}`, {
      ...options, signal: controller?.signal, cache: 'no-store',
      headers: {...options.headers, ...(token ? {Authorization: `Bearer ${token}`} : {})}
    });
    if (!response.ok) {
      let detail;
      try { detail = (await response.json()).detail; } catch (_) { /* generic error below */ }
      throw new Error(typeof detail === 'string' ? detail : 'Camera sharing disconnected. Enable it again.');
    }
    return response.json();
  }

  async function capture() {
    const track = stream?.getVideoTracks()[0];
    if (!track || track.readyState !== 'live' || track.muted || video.readyState < 2 || !video.videoWidth) {
      throw new Error('Reachy camera has no live frame. Check USB and try enabling sharing again.');
    }
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 1280 / Math.max(video.videoWidth, video.videoHeight));
    canvas.width = Math.round(video.videoWidth * scale);
    canvas.height = Math.round(video.videoHeight * scale);
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    for (const quality of [0.8, 0.6, 0.4]) {
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', quality));
      if (blob && blob.size <= 512 * 1024) return blob;
    }
    throw new Error('Camera snapshot is too large. Try again.');
  }

  async function poll(currentEpoch) {
    if (!token || currentEpoch !== epoch) return;
    try {
      const pending = await request('/request');
      if (pending.request_id) {
        const image = await capture();
        if (!token || currentEpoch !== epoch) return;
        await request(`/frame/${encodeURIComponent(pending.request_id)}`, {
          method: 'POST', headers: {'Content-Type': 'image/jpeg'}, body: image
        });
        status.textContent = 'Reachy Mini Camera shared · Fresh snapshot supplied for the visual question.';
      }
    } catch (error) {
      if (currentEpoch === epoch) disconnect(error.message);
    } finally {
      if (token && currentEpoch === epoch) timer = setTimeout(() => poll(currentEpoch), 500);
    }
  }

  start.addEventListener('click', async () => {
    if (!hostRunning || connecting || token) return;
    connecting = true;
    const attempt = ++epoch;
    controller = new AbortController();
    buttons();
    status.textContent = 'Allow camera access in Chrome. Checking for Reachy Mini Camera…';
    let candidate;
    try {
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Open this dashboard in Chrome on this computer.');
      const devices = await navigator.mediaDevices.enumerateDevices();
      const reachy = devices.find(device => device.kind === 'videoinput' && isReachy(device.label));
      // Until first permission, Chrome hides device labels. Ask using its selected
      // camera, then verify the actual track before previewing or transmitting pixels.
      if (!reachy && devices.some(device => device.kind === 'videoinput' && device.label)) {
        throw new Error('Reachy Mini Camera is not connected. No other camera will be used.');
      }
      candidate = await navigator.mediaDevices.getUserMedia({
        audio: false, video: reachy ? {deviceId: {exact: reachy.deviceId}} : true
      });
      if (attempt !== epoch || !hostRunning) {
        candidate.getTracks().forEach(track => track.stop());
        return;
      }
      const track = candidate.getVideoTracks()[0];
      if (!isReachy(track?.label)) {
        throw new Error('Chrome selected a different camera. Select Reachy Mini Camera in Chrome’s camera settings and retry.');
      }
      stream = candidate;
      video.srcObject = stream;
      video.hidden = false;
      let playTimeout;
      try {
        await Promise.race([video.play(), new Promise((_, reject) => {
          playTimeout = setTimeout(() => reject(new Error('Chrome could not share Reachy’s camera. Check its camera permission and retry.')), 10000);
        })]);
      } finally { clearTimeout(playTimeout); }
      if (attempt !== epoch) return;
      const result = await request('/start', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({camera_label: track.label})
      });
      if (attempt !== epoch) {
        fetch(`${base}/stop`, {method: 'POST', headers: {Authorization: `Bearer ${result.token}`}}).catch(() => {});
        return;
      }
      token = result.token;
      connecting = false;
      track.addEventListener('ended', () => {
        if (attempt === epoch) disconnect('Reachy camera disconnected. Reconnect USB and enable sharing again.');
      });
      status.textContent = 'Reachy Mini Camera shared · Ask “What can you see?” · Luma can keep scanning in its tab.';
      buttons();
      poll(attempt);
    } catch (error) {
      candidate?.getTracks().forEach(track => track.stop());
      if (attempt === epoch) disconnect(error.message);
    }
  });
  stop.addEventListener('click', () => disconnect());
  window.addEventListener('pagehide', () => disconnect());
  window.ReachyHostCamera = {
    update(diagnostics) {
      hostRunning = diagnostics.conversation?.mode === 'event_host' && diagnostics.voice?.running && diagnostics.voice?.vision?.enabled;
      if (!hostRunning && (token || connecting)) disconnect('Camera sharing stopped with the voice session. Luma is unchanged.');
      buttons();
    }
  };
})();
