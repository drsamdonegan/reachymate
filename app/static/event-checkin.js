(() => {
  const element = (id) => document.getElementById(id);
  let currentScan = null;
  let refreshing = false;
  let revision = 0;
  const labels = {
    unverified: 'Ticket scanned. Complete check-in in Luma; attendance is not verified here yet.',
    awaiting_luma: 'Ticket found. Complete check-in in Luma, then verify below.',
    checked_in: 'Luma confirms check-in for all valid tickets shown below.',
    partially_checked_in: 'Some tickets are checked in. Review each ticket in Luma before admitting the group.',
    not_approved: 'Registration or ticket is not approved. Ask event staff for help.',
  };
  function render(data) {
    currentScan = data.current?.scan_id || null;
    element('event-scanner-status').textContent = data.scanning
      ? `Scanning · ${data.camera_source} · ${Math.ceil(data.remaining_seconds / 60)} min left` : 'Scanner off';
    element('event-scan-start').disabled = !data.event || data.scanning;
    element('event-scan-stop').disabled = !data.scanning;
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
    element('event-verify').disabled = !currentScan || !data.verification_available;
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
