/**
 * Converter RB — Client Application
 * Clean, lightweight, secured against XSS and injection.
 */

document.addEventListener('DOMContentLoaded', () => {
  // Navigation Tabs
  const tabUrlBtn = document.getElementById('tab-url-btn');
  const tabFileBtn = document.getElementById('tab-file-btn');
  const panelUrl = document.getElementById('panel-url');
  const panelFile = document.getElementById('panel-file');

  // URL Downloader Elements
  const urlForm = document.getElementById('url-form');
  const urlInput = document.getElementById('url-input');
  const btnPaste = document.getElementById('btn-paste');
  const btnClear = document.getElementById('btn-clear');
  const btnFetch = document.getElementById('btn-fetch');
  const platformDetector = document.getElementById('platform-detector');
  const platformBadge = document.getElementById('detected-platform-badge');
  const urlAlert = document.getElementById('url-alert');
  const urlAlertMsg = document.getElementById('url-alert-message');

  // Result Elements
  const mediaResult = document.getElementById('media-result');
  const mediaThumb = document.getElementById('media-thumb');
  const mediaDuration = document.getElementById('media-duration');
  const mediaPlatformTag = document.getElementById('media-platform-tag');
  const mediaAuthor = document.getElementById('media-author');
  const mediaTitle = document.getElementById('media-title');
  const btnDlMp4 = document.getElementById('btn-dl-mp4');
  const btnDlMp3 = document.getElementById('btn-dl-mp3');
  const btnDlImg = document.getElementById('btn-dl-img');
  const dlProgressBox = document.getElementById('dl-progress-box');
  const dlProgressText = document.getElementById('dl-progress-text');

  // File Converter Elements
  const dropzone = document.getElementById('dropzone');
  const fileInput = document.getElementById('file-input');
  const btnBrowse = document.getElementById('btn-browse');
  const fileInfoBox = document.getElementById('file-info-box');
  const selectedFileName = document.getElementById('selected-file-name');
  const selectedFileSize = document.getElementById('selected-file-size');
  const btnRemoveFile = document.getElementById('btn-remove-file');
  const btnConvert = document.getElementById('btn-convert');
  const fileAlert = document.getElementById('file-alert');
  const fileAlertMsg = document.getElementById('file-alert-message');
  const convertProgressBox = document.getElementById('convert-progress-box');
  const convertProgressText = document.getElementById('convert-progress-text');

  let currentMediaData = null;
  let selectedFile = null;

  // ==========================================
  // Tab Switching
  // ==========================================
  function switchTab(target) {
    if (target === 'url') {
      tabUrlBtn.classList.add('active');
      tabUrlBtn.setAttribute('aria-selected', 'true');
      tabFileBtn.classList.remove('active');
      tabFileBtn.setAttribute('aria-selected', 'false');
      panelUrl.classList.add('active');
      panelFile.classList.remove('active');
    } else {
      tabFileBtn.classList.add('active');
      tabFileBtn.setAttribute('aria-selected', 'true');
      tabUrlBtn.classList.remove('active');
      tabUrlBtn.setAttribute('aria-selected', 'false');
      panelFile.classList.add('active');
      panelUrl.classList.remove('active');
    }
  }

  tabUrlBtn.addEventListener('click', () => switchTab('url'));
  tabFileBtn.addEventListener('click', () => switchTab('file'));

  // ==========================================
  // Platform Detection & Input Helpers
  // ==========================================
  function detectPlatform(rawUrl) {
    if (!rawUrl) return null;
    const url = rawUrl.toLowerCase().trim();

    if (url.includes('youtube.com/shorts/') || url.includes('/shorts/')) {
      return { id: 'youtube', name: 'YouTube Shorts' };
    }
    if (url.includes('youtube.com') || url.includes('youtu.be')) {
      return { id: 'youtube', name: 'YouTube Video' };
    }
    if (url.includes('instagram.com/reel/') || url.includes('instagram.com/reels/')) {
      return { id: 'instagram', name: 'Instagram Reel' };
    }
    if (url.includes('instagram.com/p/') || url.includes('instagram.com')) {
      return { id: 'instagram', name: 'Instagram Post' };
    }
    if (url.includes('threads.net') || url.includes('threads.com')) {
      return { id: 'threads', name: 'Threads Post' };
    }
    return null;
  }

  function updatePlatformBadge() {
    const val = urlInput.value.trim();
    if (val.length > 0) {
      btnClear.classList.remove('hidden');
    } else {
      btnClear.classList.add('hidden');
    }

    const detected = detectPlatform(val);
    if (detected) {
      platformBadge.textContent = detected.name;
      platformBadge.className = `detected-badge ${detected.id}`;
      platformDetector.classList.remove('hidden');
    } else {
      platformDetector.classList.add('hidden');
    }
  }

  urlInput.addEventListener('input', updatePlatformBadge);

  btnClear.addEventListener('click', () => {
    urlInput.value = '';
    updatePlatformBadge();
    hideAlert(urlAlert);
    mediaResult.classList.add('hidden');
    urlInput.focus();
  });

  btnPaste.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        urlInput.value = text.trim();
        updatePlatformBadge();
        hideAlert(urlAlert);
      }
    } catch {
      urlInput.focus();
    }
  });

  // ==========================================
  // URL Downloader Logic
  // ==========================================
  urlForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = urlInput.value.trim();
    if (!url) return;

    hideAlert(urlAlert);
    mediaResult.classList.add('hidden');
    dlProgressBox.classList.add('hidden');
    setButtonLoading(btnFetch, true, 'Menganalisis...');

    try {
      const resp = await fetch('/api/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });

      const json = await resp.json();

      if (!resp.ok || !json.success) {
        throw new Error(json.detail || 'Gagal memproses tautan.');
      }

      currentMediaData = json.data;
      displayMediaResult(json.data);
    } catch (err) {
      showAlert(urlAlert, urlAlertMsg, err.message);
    } finally {
      setButtonLoading(btnFetch, false, 'Analisis Link');
    }
  });

  function displayMediaResult(data) {
    // Safe text assignments (prevents XSS)
    mediaTitle.textContent = data.title || 'Untitled Media';
    mediaAuthor.textContent = `@${data.uploader || 'Creator'}`;
    mediaPlatformTag.textContent = (data.platform || 'Media').toUpperCase();

    if (data.thumbnail) {
      mediaThumb.src = data.thumbnail;
      mediaThumb.alt = data.title || 'Thumbnail';
    } else {
      mediaThumb.src = '';
    }

    if (data.duration && data.duration !== 'N/A') {
      mediaDuration.textContent = data.duration;
      mediaDuration.classList.remove('hidden');
    } else {
      mediaDuration.classList.add('hidden');
    }

    // Format download buttons visibility
    if (data.has_video) {
      btnDlMp4.classList.remove('hidden');
    } else {
      btnDlMp4.classList.add('hidden');
    }

    if (data.has_audio) {
      btnDlMp3.classList.remove('hidden');
    } else {
      btnDlMp3.classList.add('hidden');
    }

    if (data.is_image || (data.image_urls && data.image_urls.length > 0)) {
      btnDlImg.classList.remove('hidden');
    } else {
      btnDlImg.classList.add('hidden');
    }

    mediaResult.classList.remove('hidden');
    mediaResult.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  // Trigger Download Handlers
  btnDlMp4.addEventListener('click', () => triggerDownload('mp4'));
  btnDlMp3.addEventListener('click', () => triggerDownload('mp3'));
  btnDlImg.addEventListener('click', () => triggerDownload('image'));

  async function triggerDownload(format) {
    if (!currentMediaData || !currentMediaData.url) return;

    dlProgressBox.classList.remove('hidden');
    const label = format === 'mp3' ? 'audio MP3 (320kbps)' : (format === 'mp4' ? 'video MP4' : 'gambar');
    dlProgressText.textContent = `Sedang mengunduh dan merender ${label}...`;
    setMediaActionButtonsDisabled(true);

    try {
      const resp = await fetch('/api/download', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          url: currentMediaData.url,
          format: format
        })
      });

      if (!resp.ok) {
        let errMessage = 'Gagal mengunduh file.';
        try {
          const errJson = await resp.json();
          if (errJson.detail) errMessage = errJson.detail;
        } catch {
          // not json
        }
        throw new Error(errMessage);
      }

      // Extract filename from header
      let filename = `download.${format === 'image' ? 'jpg' : format}`;
      const disposition = resp.headers.get('Content-Disposition');
      if (disposition) {
        const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        if (utf8Match && utf8Match[1]) {
          filename = decodeURIComponent(utf8Match[1]);
        } else {
          const regularMatch = disposition.match(/filename="?([^";]+)"?/i);
          if (regularMatch && regularMatch[1]) {
            filename = regularMatch[1];
          }
        }
      }

      // Stream to blob and trigger download
      const blob = await resp.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.style.display = 'none';
      a.href = downloadUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(downloadUrl);
      a.remove();

      dlProgressText.textContent = 'Unduhan selesai! File tersimpan.';
      setTimeout(() => {
        dlProgressBox.classList.add('hidden');
      }, 4000);

    } catch (err) {
      showAlert(urlAlert, urlAlertMsg, err.message);
      dlProgressBox.classList.add('hidden');
    } finally {
      setMediaActionButtonsDisabled(false);
    }
  }

  function setMediaActionButtonsDisabled(disabled) {
    btnDlMp4.disabled = disabled;
    btnDlMp3.disabled = disabled;
    btnDlImg.disabled = disabled;
  }

  // ==========================================
  // File Converter (MP4 ke MP3) Logic
  // ==========================================
  dropzone.addEventListener('click', () => fileInput.click());
  btnBrowse.addEventListener('click', (e) => {
    e.stopPropagation();
    fileInput.click();
  });

  dropzone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });

  dropzone.addEventListener('dragleave', () => {
    dropzone.classList.remove('dragover');
  });

  dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFileSelected(e.dataTransfer.files[0]);
    }
  });

  fileInput.addEventListener('change', () => {
    if (fileInput.files && fileInput.files.length > 0) {
      handleFileSelected(fileInput.files[0]);
    }
  });

  function handleFileSelected(file) {
    hideAlert(fileAlert);

    // Validate MP4 format
    if (!file.name.toLowerCase().endsWith('.mp4') && file.type !== 'video/mp4') {
      showAlert(fileAlert, fileAlertMsg, 'Hanya file dengan format .mp4 yang didukung.');
      return;
    }

    // Size limit: 150 MB
    const maxBytes = 150 * 1024 * 1024;
    if (file.size > maxBytes) {
      showAlert(fileAlert, fileAlertMsg, 'Ukuran file melebihi batas maksimal 150 MB.');
      return;
    }

    selectedFile = file;
    selectedFileName.textContent = file.name;
    selectedFileSize.textContent = formatBytes(file.size);

    dropzone.classList.add('hidden');
    fileInfoBox.classList.remove('hidden');
    btnConvert.disabled = false;
  }

  btnRemoveFile.addEventListener('click', () => {
    selectedFile = null;
    fileInput.value = '';
    dropzone.classList.remove('hidden');
    fileInfoBox.classList.add('hidden');
    btnConvert.disabled = true;
    hideAlert(fileAlert);
    convertProgressBox.classList.add('hidden');
  });

  btnConvert.addEventListener('click', async () => {
    if (!selectedFile) return;

    hideAlert(fileAlert);
    convertProgressBox.classList.remove('hidden');
    setButtonLoading(btnConvert, true, 'Mengonversi...');

    const selectedBitrate = document.querySelector('input[name="bitrate"]:checked')?.value || '192';

    const formData = new FormData();
    formData.append('file', selectedFile);
    formData.append('bitrate', selectedBitrate);

    try {
      const resp = await fetch('/api/convert-file', {
        method: 'POST',
        body: formData
      });

      if (!resp.ok) {
        let errMessage = 'Gagal mengonversi file.';
        try {
          const errJson = await resp.json();
          if (errJson.detail) errMessage = errJson.detail;
        } catch {
          // not json
        }
        throw new Error(errMessage);
      }

      // Extract filename from header
      let filename = selectedFile.name.replace(/\.[^/.]+$/, "") + ".mp3";
      const disposition = resp.headers.get('Content-Disposition');
      if (disposition) {
        const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
        if (utf8Match && utf8Match[1]) {
          filename = decodeURIComponent(utf8Match[1]);
        } else {
          const regularMatch = disposition.match(/filename="?([^";]+)"?/i);
          if (regularMatch && regularMatch[1]) {
            filename = regularMatch[1];
          }
        }
      }

      // Stream to blob and trigger download
      const blob = await resp.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.style.display = 'none';
      a.href = downloadUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      window.URL.revokeObjectURL(downloadUrl);
      a.remove();

      convertProgressText.textContent = 'Konversi berhasil! Audio MP3 telah diunduh.';
      setTimeout(() => {
        convertProgressBox.classList.add('hidden');
      }, 4000);

    } catch (err) {
      showAlert(fileAlert, fileAlertMsg, err.message);
      convertProgressBox.classList.add('hidden');
    } finally {
      setButtonLoading(btnConvert, false, 'Konversi Sekarang ke MP3');
    }
  });

  // ==========================================
  // Helpers
  // ==========================================
  function showAlert(box, msgEl, message) {
    msgEl.textContent = message;
    box.className = 'alert-box error';
    box.classList.remove('hidden');
  }

  function hideAlert(box) {
    box.classList.add('hidden');
  }

  function setButtonLoading(btn, loading, text) {
    const textEl = btn.querySelector('.btn-text');
    const spinner = btn.querySelector('.btn-spinner');
    if (textEl) textEl.textContent = text;
    btn.disabled = loading;
    if (spinner) {
      if (loading) spinner.classList.remove('hidden');
      else spinner.classList.add('hidden');
    }
  }

  function formatBytes(bytes) {
    if (bytes === 0) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
  }
});
