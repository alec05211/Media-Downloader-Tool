const MENU_ID = 'media-downloader-open';
const APP_ORIGIN = 'http://127.0.0.1:38471';
const NATIVE_HOST = 'com.media_downloader.launcher';
const SUPPORTED_HOSTS = [
  'youtube.com',
  'youtu.be',
  'tiktok.com',
  'instagram.com',
  'x.com',
  'twitter.com',
  'reddit.com',
  'redd.it',
  'redditmedia.com'
];

if (chrome.runtime?.onInstalled && chrome.contextMenus?.create) {
  chrome.runtime.onInstalled.addListener(() => {
    chrome.contextMenus.create({
      id: MENU_ID,
      title: 'Download with Media Downloader',
      contexts: ['image', 'video', 'audio', 'link']
    });
  });
}

function isHttpUrl(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch (_) {
    return false;
  }
}

function isSupportedProviderUrl(value) {
  if (!isHttpUrl(value)) return false;
  const host = new URL(value).hostname.toLowerCase().replace(/^www\./, '');
  return SUPPORTED_HOSTS.some(domain => host === domain || host.endsWith(`.${domain}`));
}

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

async function openApp(url = '') {
  if (!await appIsReady()) await startApp();
  if (!await waitForApp()) throw new Error('The local server did not become ready.');
  const target = url ? `${APP_ORIGIN}/?url=${encodeURIComponent(url)}` : `${APP_ORIGIN}/`;
  await chrome.tabs.create({url: target, active: true});
  await chrome.action.setBadgeText({text: ''});
  await chrome.action.setTitle({title: 'Open Media Downloader'});
}

async function copyToClipboard(tabId, value) {
  if (!tabId || !isHttpUrl(value)) return;
  try {
    await chrome.scripting.executeScript({
      target: {tabId},
      func: text => navigator.clipboard.writeText(text),
      args: [value]
    });
  } catch (_) {
    // Some protected Chrome pages disallow injected scripts. Launching still works.
  }
}

if (chrome.contextMenus?.onClicked) {
  chrome.contextMenus.onClicked.addListener(async (info, tab) => {
    if (info.menuItemId !== MENU_ID) return;

    const exactMediaUrl = info.srcUrl || info.linkUrl || '';
    const pageUrl = info.pageUrl || (tab && tab.url) || '';
    const linkedUrl = info.linkUrl || '';
    const appUrl = isSupportedProviderUrl(linkedUrl)
      ? linkedUrl
      : isSupportedProviderUrl(pageUrl)
        ? pageUrl
        : exactMediaUrl;

    if (!isHttpUrl(appUrl)) return;

    await copyToClipboard(tab && tab.id, exactMediaUrl || appUrl);
    try {
      await openApp(appUrl);
    } catch (error) {
      console.error('Could not open Media Downloader:', error);
      await chrome.action.setBadgeBackgroundColor({color: '#b42318'});
      await chrome.action.setBadgeText({text: '!'});
      await chrome.action.setTitle({title: `Media Downloader could not start: ${error.message}`});
    }
  });
}
