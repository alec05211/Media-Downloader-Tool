const MENU_ID = 'media-downloader-open';
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
    const localUrl = `http://127.0.0.1:38471/?url=${encodeURIComponent(appUrl)}`;
    await chrome.tabs.create({url: localUrl, active: true});
  });
}
