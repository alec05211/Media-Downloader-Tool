const APP_ORIGIN = 'http://127.0.0.1:38471';
const NATIVE_HOST = 'com.media_downloader.launcher';

function appIsReady() {
  return fetch(`${APP_ORIGIN}/api/app-status`, {cache: 'no-store'})
    .then(response => response.ok ? response.json() : null)
    .then(status => status?.app === 'media-downloader')
    .catch(() => false);
}

function startApp() {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(NATIVE_HOST, {action: 'start'}, response => {
      const error = chrome.runtime.lastError;
      if (error) {
        reject(new Error(error.message));
      } else if (!response?.ok) {
        reject(new Error(response?.error || 'The launcher did not start the application.'));
      } else {
        resolve();
      }
    });
  });
}

async function waitForApp() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    if (await appIsReady()) return true;
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  return false;
}

document.addEventListener('DOMContentLoaded', async () => {
  try {
    if (!await appIsReady()) await startApp();
    if (!await waitForApp()) throw new Error('The local server did not become ready.');
    await chrome.tabs.create({url: `${APP_ORIGIN}/`, active: true});
    window.close();
  } catch (error) {
    document.body.textContent = `Could not open Media Downloader: ${error.message}`;
  }
});
