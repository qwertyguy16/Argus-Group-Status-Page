// ── Auto-refresh logic ────────────────────────────────────────────────────────
(function () {
  const CHECK_INTERVAL_MS = (window.__CHECK_INTERVAL__ || 30) * 1000;
  const REFRESH_BUFFER_MS = 2000; // extra 2 s after server check completes

  const timestampEl = document.getElementById('last-updated');
  const countdownEl = document.getElementById('countdown');

  let nextRefresh = Date.now() + CHECK_INTERVAL_MS + REFRESH_BUFFER_MS;

  // Countdown ticker
  function tickCountdown() {
    const remaining = Math.max(0, Math.ceil((nextRefresh - Date.now()) / 1000));
    if (countdownEl) countdownEl.textContent = `Refreshing in ${remaining}s`;
    if (remaining <= 0) {
      window.location.reload();
    }
  }

  setInterval(tickCountdown, 1000);
  tickCountdown();

  // Human-readable "last checked" time
  function updateTimestamp() {
    if (!timestampEl) return;
    const isoStr = timestampEl.dataset.ts;
    if (!isoStr) return;
    const date = new Date(isoStr);
    const now = new Date();
    const diffSec = Math.floor((now - date) / 1000);
    let label;
    if (diffSec < 5)       label = 'just now';
    else if (diffSec < 60) label = `${diffSec}s ago`;
    else                   label = `${Math.floor(diffSec / 60)}m ago`;
    timestampEl.textContent = `Last checked ${label}`;
  }

  setInterval(updateTimestamp, 5000);
  updateTimestamp();
})();
