(() => {
  const element = (id) => document.getElementById(id);
  let currentScan = null;
  let refreshing = false;
  let revision = 0;
  let handoffPending = false;
  const labels = {
    unverified: 'Ticket scanned. Complete check-in in Luma; attendance is not verified here yet.',
    awaiting_luma: 'Ticket found. Complete check-in in Luma, then verify below.',
    checked_in: 'Luma confirms check-in for all valid tickets shown below.',
    partially_checked_in: 'Some tickets are checked in. Review each ticket in Luma before admitting the group.',
    not_approved: 'Registration or ticket is not approved. Ask event staff for help.',
  };
  function render(data) {
    const express = data.express || {};
    const external = !!express.active;
    currentScan = data.current?.scan_id || null;
    element('event-scanner-status').textContent = external ? 'Camera reserved for Luma' : data.scanning
      ? `Scanning · ${data.camera_source} · ${Math.ceil(data.remaining_seconds / 60)} min left` : 'Scanner off';
    element('event-scan-start').disabled = !data.event || data.scanning || external;
    element('event-scan-stop').disabled = !data.scanning;
    element('event-next').disabled = external;
    element('event-select-submit').disabled = external;
    element('luma-event-id').disabled = external;
    element('event-qr-code').disabled = external;
    element('event-express-start').disabled = !data.event || handoffPending;
    element('event-express-start').textContent = external ? 'Prepare and open Luma again' : 'Open automatic Luma check-in';
    element('event-express-finish').hidden = !external;
    const reopen = element('event-express-reopen');
    reopen.hidden = !external || !express.camera_released || !express.url;
    if (!reopen.hidden) reopen.href = express.url;
    else reopen.removeAttribute('href');
    element('event-express-status').textContent = external
      ? express.camera_released
        ? 'Reachy has released the camera and paused chat. In Luma, confirm Reachy Mini Camera and Express Mode. Check-in results appear there.'
        : 'A Luma handoff is reserved or was recovered after restart. Prepare and open Luma again, or close its scanner and return the camera.'
      : 'Opening Luma pauses Reachy’s voice and camera features. Hold each guest’s QR code in front of Reachy; Luma records valid single-ticket arrivals automatically in Express Mode.';
    if (document.activeElement !== element('luma-event-id')) element('luma-event-id').value = data.event?.id || '';
    element('luma-event-name').textContent = data.event ? `${data.event.name} · ${data.event.id}` : '';
    element('event-verification-notice').textContent = data.verification_available
      ? 'Luma’s signed-in page records attendance. Verify reads it back; only a Luma timestamp confirms check-in.'
      : 'Luma API key is not configured. You can complete check-in in Luma, but this dashboard cannot yet verify the result. Configure LUMA_API_KEY privately to enable verification.';
    element('event-scan-result').textContent = data.current
      ? `${data.current.name}: ${labels[data.current.status] || 'Awaiting verification.'}`
      : data.scanning ? 'Show one ticket QR code at a time.' : 'Ready when you start the scanner.';
    element('event-checkin-error').textContent = data.error || '';
    const list = element('event-tickets');
    list.replaceChildren();
    for (const ticket of data.current?.tickets || []) {
      const li = document.createElement('li');
      li.textContent = `${ticket.name} · ${!ticket.valid ? 'Invalid ticket' : ticket.checked_in_at ? `Checked in: ${new Date(ticket.checked_in_at).toLocaleString()}` : 'Not checked in'}`;
      list.append(li);
    }
    const link = element('event-luma-checkin');
    link.hidden = !data.current?.checkin_url || data.current.status === 'not_approved';
    if (!link.hidden) link.href = data.current.checkin_url;
    else link.removeAttribute('href');
    element('event-verify').disabled = !currentScan || !data.verification_available || external;
  }
  async function request(action = '', body) {
    if (action) revision += 1;
    const requestRevision = revision;
    const response = await fetch(`/api/admin/event-checkin${action ? `/${action}` : ''}`, {
      method: action ? 'POST' : 'GET', cache: 'no-store',
      headers: body ? {'Content-Type': 'application/json'} : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not process the ticket.');
    if (requestRevision === revision) render(data);
    return data;
  }
  async function action(name, body) {
    try { await request(name, body); }
    catch (error) { element('event-checkin-error').textContent = error.message; }
  }
  element('event-select-form').addEventListener('submit', (event) => {
    event.preventDefault();
    action('event', {event_id: element('luma-event-id').value.trim()});
  });
  element('event-qr-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const input = element('event-qr-code');
    const value = input.value.trim();
    input.value = '';
    action('scan', {qr_code: value});
  });
  element('event-scan-start').addEventListener('click', () => action('start'));
  element('event-scan-stop').addEventListener('click', () => action('stop'));
  element('event-next').addEventListener('click', () => action('next'));
  element('event-verify').addEventListener('click', () => action('verify', {scan_id: currentScan}));
  element('event-express-start').addEventListener('click', async () => {
    if (handoffPending) return;
    // Open on the user's click, before the asynchronous camera handoff, to avoid popup blocking.
    const scanner = window.open('about:blank', '_blank');
    if (!scanner) {
      element('event-express-error').textContent = 'Allow this dashboard to open a Luma tab, then try again. Reachy has not changed modes.';
      return;
    }
    scanner.opener = null;
    handoffPending = true;
    element('event-express-start').disabled = true;
    element('event-express-error').textContent = '';
    try {
      const data = await request('express/start');
      if (!data.express?.camera_released || !data.express.url) throw new Error('Camera handoff is not ready.');
      scanner.location.replace(data.express.url);
    } catch (error) {
      scanner.close();
      element('event-express-error').textContent = error.message;
      await refresh();
    } finally {
      handoffPending = false;
      await refresh();
    }
  });
  element('event-express-finish').addEventListener('click', async () => {
    element('event-express-error').textContent = '';
    try { await request('express/finish', {scanner_closed: true}); }
    catch (error) { element('event-express-error').textContent = error.message; }
  });
  // Read-only status polling never records attendance or automatically opens third-party pages.
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try { await request(); }
    catch { element('event-checkin-error').textContent = 'Event service unavailable. Open the Reachy launcher.'; }
    finally { refreshing = false; }
  }
  refresh();
  setInterval(refresh, 2000);
})();
