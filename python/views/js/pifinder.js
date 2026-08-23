/* Shared helpers for the PiFinder web UI. */

/**
 * Poll /image and show it in the <img id="image"> element.
 *
 * Polling pauses while the tab is hidden and the previous blob URL is
 * revoked so the browser does not keep every frame in memory.
 */
function startScreenPolling(intervalMs, unavailableText) {
  const imageElement = document.getElementById('image');
  const errorElement = document.getElementById('error');
  let previousUrl = null;
  let timer = null;

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(fetchImage, intervalMs);
  }

  function fetchImage() {
    if (document.hidden) {
      return;
    }
    fetch('/image', { cache: 'no-store' })
      .then((response) => {
        if (!response.ok) { throw Error(response.statusText); }
        return response.blob();
      })
      .then((imageBlob) => {
        const url = URL.createObjectURL(imageBlob);
        imageElement.src = url;
        if (previousUrl) { URL.revokeObjectURL(previousUrl); }
        previousUrl = url;
        errorElement.textContent = '';
      })
      .catch((error) => {
        console.log(error);
        errorElement.textContent = unavailableText;
      })
      .finally(schedule);
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) { fetchImage(); }
  });
  fetchImage();
}

/** Send one keypad button to the PiFinder. */
function sendKey(code) {
  return fetch('/key_callback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ button: code }),
  })
    .then((response) => response.json())
    .catch((error) => console.error('Error:', error));
}

/**
 * Poll `url` until it answers 200, then go to `target`.
 * Gives up after `maxMs` and goes to `target` anyway.
 */
function redirectWhenUp(url, target, firstDelayMs, maxMs) {
  const start = Date.now();
  function probe() {
    fetch(url, { cache: 'no-store' })
      .then((response) => {
        if (response.ok) { location.href = target; return; }
        throw Error(response.statusText);
      })
      .catch(() => {
        if (Date.now() - start > maxMs) { location.href = target; return; }
        setTimeout(probe, 2000);
      });
  }
  setTimeout(probe, firstDelayMs);
}
