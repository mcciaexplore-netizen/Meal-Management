export function scheduleScanResultReset({ flow, active, clearMarker, onReset, onBlocked }) {
  const result = flow.result;
  const request = flow.request;
  if (!result || !request || flow.inFlight) return () => {};
  const timer = setTimeout(() => {
    if (!active() || flow.result !== result || flow.request !== request || flow.inFlight) return;
    let cleared = false;
    try { cleared = clearMarker(); } catch {}
    if (!cleared) {
      onBlocked();
      return;
    }
    flow.reset();
    onReset();
  }, 2000);
  return () => clearTimeout(timer);
}
