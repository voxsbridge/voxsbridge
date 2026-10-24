/* VoxsBridge session guard: fail closed on confirmed revocation. */
(() => {
  if (location.pathname === '/login' || location.pathname === '/signup') return;
  let revoked = false;
  async function check() {
    if (revoked) return;
    try {
      const response = await fetch('/api/session/status', {credentials:'same-origin',cache:'no-store'});
      if (response.status !== 401 && response.status !== 403) return;
      revoked = true;
      // Stop any local WebRTC legs exposed by the dialer, then disconnect SDK.
      try { window.VoxsBridge?.hangup?.(); } catch (_) {}
      try { window.VoxsBridge?.disconnect?.(); } catch (_) {}
      window.dispatchEvent(new CustomEvent('voxsbridge:session-revoked'));
      location.replace('/login?reason=session-revoked');
    } catch (_) { /* network interruption is not proof of revoked login */ }
  }
  setInterval(check, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  check();
})();
