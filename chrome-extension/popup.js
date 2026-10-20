document.addEventListener('DOMContentLoaded', async () => {
  try {
    await chrome.tabs.create({url: 'http://127.0.0.1:38471/', active: true});
    window.close();
  } catch (error) {
    document.body.textContent = `Could not open Media Downloader: ${error.message}`;
  }
});
