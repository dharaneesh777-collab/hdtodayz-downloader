/**
 * HDTodayz Video Downloader - Direct Browser Download Controller
 */

// Application State
const state = {
  currentMedia: null,
  currentSeasonEpisodes: [],
  selectedEpisodes: new Map(),
  selectedQuality: 'best',
  selectedAudio: 'eng',
  selectedSub: 'eng',
  selectedThreads: 16,
};

// DOM Elements Cache
const el = {
  urlInput: document.getElementById('url-input'),
  resolveBtn: document.getElementById('resolve-btn'),
  mediaInspector: document.getElementById('media-inspector'),
  mediaBackdrop: document.getElementById('media-backdrop'),
  mediaPoster: document.getElementById('media-poster'),
  mediaTitle: document.getElementById('media-title'),
  mediaYear: document.getElementById('media-year'),
  badgeType: document.getElementById('badge-type'),
  badgeRating: document.getElementById('badge-rating'),
  badgeRuntime: document.getElementById('badge-runtime'),
  badgeGenres: document.getElementById('badge-genres'),
  mediaOverview: document.getElementById('media-overview'),
  tvControls: document.getElementById('tv-controls'),
  seasonSelect: document.getElementById('season-select'),
  episodesGrid: document.getElementById('episodes-grid'),
  selectedEpCount: document.getElementById('selected-ep-count'),
  btnMasterDownload: document.getElementById('btn-master-download'),
  btnMasterText: document.getElementById('btn-master-text'),
  trendingMoviesGrid: document.getElementById('trending-movies-grid'),
  trendingTvGrid: document.getElementById('trending-tv-grid'),
  searchResultsSection: document.getElementById('search-results-section'),
  searchResultsGrid: document.getElementById('search-results-grid'),
  catalogSearchInput: document.getElementById('catalog-search-input'),
  videoModal: document.getElementById('video-modal'),
  modalVideoPlayer: document.getElementById('modal-video-player'),
  modalVideoTitle: document.getElementById('modal-video-title'),
  modalBtnDownload: document.getElementById('modal-btn-download'),
  modalBtnPlayLocal: document.getElementById('modal-btn-play-local'),
};

// Initial Setup
document.addEventListener('DOMContentLoaded', () => {
  loadTrendingCatalog();
});

// Tab Navigation
function switchTab(tabId) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));

  const targetPane = document.getElementById(tabId);
  const targetBtn = document.getElementById('btn-' + tabId);
  if (targetPane) targetPane.classList.add('active');
  if (targetBtn) targetBtn.classList.add('active');

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// Toast Notifications
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
    (type === 'success' 
      ? '<polyline points="20 6 9 17 4 12"></polyline>' 
      : '<circle cx="12" cy="12" r="10"></circle><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>') +
    '</svg><span>' + message + '</span>';
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

// Quick Sample Loader
function loadSample(value) {
  if (el.urlInput) {
    el.urlInput.value = value;
    handleResolve();
  }
}

// Resolve HDTodayz Link or Search Query
async function handleResolve() {
  const input = el.urlInput.value.trim();
  if (!input) {
    showToast('Please enter an HDTodayz URL or movie title', 'warning');
    return;
  }

  el.resolveBtn.disabled = true;
  el.resolveBtn.innerHTML = '<svg class="animate-spin" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="2" x2="12" y2="6"></line><line x1="12" y1="18" x2="12" y2="22"></line><line x1="4.93" y1="4.93" x2="7.76" y2="7.76"></line><line x1="16.24" y1="16.24" x2="19.07" y2="19.07"></line><line x1="2" y1="12" x2="6" y2="12"></line><line x1="18" y1="12" x2="22" y2="12"></line><line x1="4.93" y1="19.07" x2="7.76" y2="16.24"></line><line x1="16.24" y1="7.76" x2="19.07" y2="4.93"></line></svg> Analyzing...';

  try {
    const res = await fetch('/api/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: input }),
    });

    const data = await res.json();
    if (!res.ok || !data.success) {
      throw new Error(data.detail || 'Failed to resolve stream information.');
    }

    state.currentMedia = data.media;
    renderMediaInspector(data.media, data.stream_info);
    showToast('Ready to download: ' + data.media.title, 'success');

  } catch (err) {
    showToast(err.message, 'danger');
  } finally {
    el.resolveBtn.disabled = false;
    el.resolveBtn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg> Analyze Video';
  }
}

// Render Media Inspector
function renderMediaInspector(media, streamInfo) {
  el.mediaInspector.style.display = 'block';
  el.mediaBackdrop.style.backgroundImage = media.backdrop_url ? 'url("' + media.backdrop_url + '")' : 'none';
  el.mediaPoster.src = media.poster_url || 'data:image/svg+xml,<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 200 300\' fill=\'%23222\'><text x=\'50%\' y=\'50%\' fill=\'%23666\' text-anchor=\'middle\'>No Image</text></svg>';
  el.mediaTitle.textContent = media.title;
  el.mediaYear.textContent = media.year ? '(' + media.year + ')' : '';
  el.badgeType.textContent = (media.media_type || 'movie').toUpperCase();
  el.badgeRating.textContent = '⭐ ' + media.rating;
  el.badgeRuntime.textContent = media.runtime_minutes ? media.runtime_minutes + ' min' : (media.media_type === 'tv' ? 'TV Series' : 'N/A');
  el.badgeGenres.textContent = media.genres ? media.genres.join(', ') : 'Featured';
  el.mediaOverview.textContent = media.overview || 'No overview description available.';

  if (media.media_type === 'tv' && media.seasons && media.seasons.length > 0) {
    el.tvControls.style.display = 'block';
    el.seasonSelect.innerHTML = media.seasons.map(s => 
      '<option value="' + s.season_number + '">' + s.name + ' (' + s.episode_count + ' eps)</option>'
    ).join('');

    handleSeasonChange();
  } else {
    el.tvControls.style.display = 'none';
    updateTvActionButtons();
  }

  el.mediaInspector.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// Handle Season Change (Fetch episodes)
async function handleSeasonChange() {
  if (!state.currentMedia || state.currentMedia.media_type !== 'tv') return;
  const sNum = el.seasonSelect.value;
  if (el.episodesGrid) {
    el.episodesGrid.innerHTML = '<div style="color:var(--text-muted); padding:1rem;">Loading episodes for Season ' + sNum + '...</div>';
  }
  state.selectedEpisodes.clear();
  updateTvActionButtons();

  try {
    const res = await fetch('/api/tv/' + state.currentMedia.tmdb_id + '/season/' + sNum);
    const data = await res.json();
    if (data.success && data.episodes) {
      state.currentSeasonEpisodes = data.episodes;
      if (state.currentSeasonEpisodes.length > 0) {
        state.selectedEpisodes.set(state.currentSeasonEpisodes[0].episode_number, state.currentSeasonEpisodes[0]);
      }
      renderEpisodesGrid();
    }
  } catch (err) {
    if (el.episodesGrid) {
      el.episodesGrid.innerHTML = '<div style="color:var(--danger); padding:1rem;">Failed to load episodes.</div>';
    }
  }
}

// Render Interactive Episode Grid with 1-Click Download & Play
function renderEpisodesGrid() {
  if (!el.episodesGrid) return;
  if (!state.currentSeasonEpisodes || state.currentSeasonEpisodes.length === 0) {
    el.episodesGrid.innerHTML = '<div style="color:var(--text-muted); padding:1rem;">No episodes available for this season.</div>';
    updateTvActionButtons();
    return;
  }

  el.episodesGrid.innerHTML = state.currentSeasonEpisodes.map(ep => {
    const isChecked = state.selectedEpisodes.has(ep.episode_number);
    const epNumPadded = String(ep.episode_number).padStart(2, '0');
    const safeTitle = (ep.name || 'Episode ' + ep.episode_number).replace(/'/g, "\\'");
    return '<div class="episode-card ' + (isChecked ? 'selected' : '') + '" onclick="toggleEpisode(' + ep.episode_number + ')">' +
      '<input type="checkbox" class="ep-checkbox" id="chk-ep-' + ep.episode_number + '" ' + (isChecked ? 'checked' : '') + ' onclick="event.stopPropagation(); toggleEpisode(' + ep.episode_number + ');">' +
      '<span class="ep-badge">E' + epNumPadded + '</span>' +
      '<div class="ep-info">' +
        '<span class="ep-title" title="' + safeTitle + '">' + (ep.name || 'Episode ' + ep.episode_number) + '</span>' +
      '</div>' +
      '<div class="ep-actions" onclick="event.stopPropagation();">' +
        '<button class="btn-ep-action btn-ep-download" title="Download Episode directly to this device" onclick="triggerDirectDownloadEpisode(' + ep.episode_number + ', \'' + safeTitle + '\')">' +
          '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg> Download' +
        '</button>' +
        '<button class="btn-ep-action btn-ep-play" title="Watch Episode online" onclick="openEpisodeStreamInModal(' + ep.episode_number + ', \'' + safeTitle + '\')">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg> Watch' +
        '</button>' +
      '</div>' +
    '</div>';
  }).join('');

  updateTvActionButtons();
}

function toggleEpisode(epNum) {
  const ep = state.currentSeasonEpisodes.find(e => e.episode_number === epNum);
  if (!ep) return;

  if (state.selectedEpisodes.has(epNum)) {
    state.selectedEpisodes.delete(epNum);
  } else {
    state.selectedEpisodes.set(epNum, ep);
  }

  const chk = document.getElementById('chk-ep-' + epNum);
  const card = chk ? chk.closest('.episode-card') : null;
  const isNowSelected = state.selectedEpisodes.has(epNum);

  if (chk) chk.checked = isNowSelected;
  if (card) {
    if (isNowSelected) card.classList.add('selected');
    else card.classList.remove('selected');
  }

  updateTvActionButtons();
}

function selectAllEpisodes() {
  if (!state.currentSeasonEpisodes) return;
  state.currentSeasonEpisodes.forEach(ep => {
    state.selectedEpisodes.set(ep.episode_number, ep);
  });
  renderEpisodesGrid();
}

function deselectAllEpisodes() {
  state.selectedEpisodes.clear();
  renderEpisodesGrid();
}

function updateTvActionButtons() {
  const count = state.selectedEpisodes.size;
  const total = state.currentSeasonEpisodes ? state.currentSeasonEpisodes.length : 0;

  if (el.selectedEpCount) {
    el.selectedEpCount.textContent = count + ' / ' + total + ' selected';
  }

  if (state.currentMedia && state.currentMedia.media_type === 'tv') {
    if (count === 0) {
      if (el.btnMasterText) el.btnMasterText.textContent = 'Select an episode to download';
      if (el.btnMasterDownload) {
        el.btnMasterDownload.disabled = true;
        el.btnMasterDownload.style.opacity = '0.5';
      }
    } else if (count === 1) {
      const singleEp = Array.from(state.selectedEpisodes.values())[0];
      if (el.btnMasterText) el.btnMasterText.textContent = 'Download Ep ' + singleEp.episode_number + ' to Device (MP4)';
      if (el.btnMasterDownload) {
        el.btnMasterDownload.disabled = false;
        el.btnMasterDownload.style.opacity = '1';
      }
    } else {
      if (el.btnMasterText) el.btnMasterText.textContent = 'Download Selected (' + count + ' Episodes)';
      if (el.btnMasterDownload) {
        el.btnMasterDownload.disabled = false;
        el.btnMasterDownload.style.opacity = '1';
      }
    }
  } else {
    if (el.btnMasterText) el.btnMasterText.textContent = 'Download to This Device (MP4)';
    if (el.btnMasterDownload) {
      el.btnMasterDownload.disabled = false;
      el.btnMasterDownload.style.opacity = '1';
    }
  }
}

// Quality, Audio, & Subtitle Selectors
function selectQuality(btn) {
  document.querySelectorAll('#quality-options .select-chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  state.selectedQuality = btn.dataset.quality;
}

function selectAudio(btn) {
  document.querySelectorAll('#audio-options .select-chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  state.selectedAudio = btn.dataset.lang;
}

function selectSubtitle(btn) {
  document.querySelectorAll('#sub-options .select-chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  state.selectedSub = btn.dataset.sub || 'eng';
}

function selectThreads(btn) {
  document.querySelectorAll('#thread-options .select-chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  state.selectedThreads = parseInt(btn.dataset.threads) || 16;
}

// =============================================================================
// Direct In-Browser (Chrome) Download Engine
// =============================================================================

function triggerBrowserDownload(url) {
  // Isolated hidden iframe ensures multiple simultaneous downloads in Chrome without canceling each other
  const iframe = document.createElement('iframe');
  iframe.style.display = 'none';
  iframe.src = url;
  document.body.appendChild(iframe);
  setTimeout(() => {
    try { iframe.remove(); } catch (e) {}
  }, 300000);
}

function buildDownloadStreamUrl(options = {}) {
  if (!state.currentMedia) return null;
  const media = state.currentMedia;
  const params = new URLSearchParams({
    tmdb_id: media.tmdb_id,
    media_type: media.media_type || 'movie',
    title: media.title || 'video',
    year: media.year || '',
    quality: state.selectedQuality || 'best',
    audio_lang: state.selectedAudio || 'eng',
    sub_lang: state.selectedSub || 'eng',
    subtitles_enabled: (state.selectedSub || 'eng').toLowerCase() !== 'none',
    threads: state.selectedThreads || 16,
  });
  if (options.previewSeconds) {
    params.set('preview_seconds', options.previewSeconds);
  }
  if (options.season) params.set('season', options.season);
  if (options.episode) params.set('episode', options.episode);
  if (options.episodeTitle) params.set('episode_title', options.episodeTitle);
  return '/api/download/stream?' + params.toString();
}

function triggerDirectDownload(previewSeconds = null) {
  if (!state.currentMedia) {
    showToast('Please resolve a movie or series first', 'warning');
    return;
  }
  const media = state.currentMedia;

  if (media.media_type === 'tv') {
    const selectedCount = state.selectedEpisodes.size;
    if (selectedCount === 0) {
      showToast('Please select at least 1 episode to download', 'warning');
      return;
    }
    const season = parseInt(el.seasonSelect.value) || 1;
    const sortedEpisodes = Array.from(state.selectedEpisodes.values()).sort(
      (a, b) => a.episode_number - b.episode_number
    );

    if (sortedEpisodes.length === 1 || previewSeconds) {
      const ep = sortedEpisodes[0];
      const url = buildDownloadStreamUrl({
        season,
        episode: ep.episode_number,
        episodeTitle: ep.name || ('Episode ' + ep.episode_number),
        previewSeconds,
      });
      triggerBrowserDownload(url);
      const label = previewSeconds ? ('30s Sample of Ep ' + ep.episode_number) : ('Ep ' + ep.episode_number);
      showToast('📥 Chrome download started for ' + label + '! Saving to your device...', 'success');
    } else {
      showToast('📥 Initiating ' + sortedEpisodes.length + ' simultaneous downloads in Chrome...', 'success');
      sortedEpisodes.forEach((ep, idx) => {
        setTimeout(() => {
          const url = buildDownloadStreamUrl({
            season,
            episode: ep.episode_number,
            episodeTitle: ep.name || ('Episode ' + ep.episode_number),
          });
          triggerBrowserDownload(url);
        }, idx * 150);
      });
    }
  } else {
    const url = buildDownloadStreamUrl({ previewSeconds });
    triggerBrowserDownload(url);
    const label = previewSeconds ? '30s sample preview' : 'full MP4 movie';
    showToast('📥 Chrome download started for ' + media.title + ' (' + label + ')! Saving directly to this device...', 'success');
  }
}

function triggerDirectDownloadEpisode(epNum, epTitle) {
  if (!state.currentMedia) return;
  const season = parseInt(el.seasonSelect.value) || 1;
  const url = buildDownloadStreamUrl({
    season,
    episode: epNum,
    episodeTitle: epTitle,
  });
  triggerBrowserDownload(url);
  showToast('📥 Chrome downloading S' + season + 'E' + String(epNum).padStart(2, '0') + ': ' + (epTitle || ('Episode ' + epNum)) + '!', 'success');
}

// =============================================================================
// In-Browser Stream Player Modal
// =============================================================================

async function openStreamInModal() {
  if (!state.currentMedia) return;
  const media = state.currentMedia;
  let s = 1;
  let e = 1;
  if (media.media_type === 'tv') {
    s = parseInt(el.seasonSelect.value) || 1;
    if (state.selectedEpisodes.size > 0) {
      e = Array.from(state.selectedEpisodes.values())[0].episode_number;
    }
  }
  await playStreamModal(media.tmdb_id, media.media_type, s, e, media.title + (media.media_type === 'tv' ? ' S' + s + 'E' + e : ''));
}

async function openEpisodeStreamInModal(epNum, epTitle) {
  if (!state.currentMedia) return;
  const season = parseInt(el.seasonSelect.value) || 1;
  await playStreamModal(state.currentMedia.tmdb_id, 'tv', season, epNum, state.currentMedia.title + ' - S' + season + 'E' + String(epNum).padStart(2, '0') + ': ' + epTitle);
}

async function playStreamModal(tmdbId, mediaType, season, episode, title) {
  showToast('Loading stream for ' + title + '...', 'info');
  try {
    const res = await fetch('/api/stream-info?tmdb_id=' + tmdbId + '&media_type=' + mediaType + '&season=' + season + '&episode=' + episode);
    const data = await res.json();
    if (!data.success || !data.stream_info || !data.stream_info.master_playlist_url) {
      throw new Error('Stream unavailable for online playback.');
    }

    el.modalVideoTitle.textContent = title;
    el.modalVideoPlayer.src = data.stream_info.master_playlist_url;
    el.videoModal.classList.add('show');

    if (el.modalBtnDownload) {
      el.modalBtnDownload.onclick = (ev) => {
        ev.preventDefault();
        const dlUrl = buildDownloadStreamUrl({
          season,
          episode,
          episodeTitle: title,
        });
        triggerBrowserDownload(dlUrl);
        showToast('📥 Chrome download initiated from player!', 'success');
      };
    }

    el.modalVideoPlayer.play().catch(() => {});
  } catch (err) {
    showToast(err.message, 'danger');
  }
}

function closeModalPlayer() {
  if (el.modalVideoPlayer) {
    el.modalVideoPlayer.pause();
    el.modalVideoPlayer.src = '';
  }
  if (el.videoModal) {
    el.videoModal.classList.remove('show');
  }
}

window.addEventListener('click', (e) => {
  if (e.target === el.videoModal) {
    closeModalPlayer();
  }
});

// =============================================================================
// Catalog Browsing & Search
// =============================================================================

async function loadTrendingCatalog() {
  try {
    const res = await fetch('/api/trending');
    const data = await res.json();
    if (data.success && data.catalog) {
      renderCatalogGrid(el.trendingMoviesGrid, data.catalog.movies || []);
      renderCatalogGrid(el.trendingTvGrid, data.catalog.tv_series || []);
    }
  } catch (err) {
    console.error('Failed to load trending catalog:', err);
  }
}

function renderCatalogGrid(container, items) {
  if (!container) return;
  if (!items || items.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted); padding:1rem;">No items available.</div>';
    return;
  }

  container.innerHTML = items.map(item => 
    '<div class="media-card" onclick="selectCatalogItem(\'' + item.id + '\', \'' + item.media_type + '\', \'' + (item.title || item.name || '').replace(/'/g, "\\'") + '\')">' +
      '<div class="card-poster-wrap">' +
        '<img class="card-poster" src="' + (item.poster_url || 'data:image/svg+xml,<svg xmlns=\'http://www.w3.org/2000/svg\' viewBox=\'0 0 200 300\' fill=\'%23222\'/>') + '" alt="' + (item.title || item.name) + '" loading="lazy">' +
        '<div class="card-rating">★ ' + (item.rating || 'N/A') + '</div>' +
      '</div>' +
      '<div class="card-info">' +
        '<h4 class="card-title">' + (item.title || item.name) + '</h4>' +
        '<div class="card-meta">' +
          '<span>' + (item.media_type || 'movie').toUpperCase() + '</span>' +
          '<span>' + (item.year || '') + '</span>' +
        '</div>' +
      '</div>' +
    '</div>'
  ).join('');
}

function selectCatalogItem(tmdbId, mediaType, title) {
  if (el.urlInput) {
    el.urlInput.value = title;
  }
  switchTab('tab-downloader');
  handleResolve();
}

async function executeCatalogSearch() {
  const query = el.catalogSearchInput ? el.catalogSearchInput.value.trim() : '';
  if (!query) {
    showToast('Please enter a search term', 'warning');
    return;
  }

  showToast('Searching HDTodayz for "' + query + '"...', 'info');
  try {
    const res = await fetch('/api/search?q=' + encodeURIComponent(query));
    const data = await res.json();
    if (data.success && data.results) {
      if (el.searchResultsSection && el.searchResultsGrid) {
        el.searchResultsSection.style.display = 'block';
        renderCatalogGrid(el.searchResultsGrid, data.results);
        el.searchResultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }
  } catch (err) {
    showToast('Search failed.', 'danger');
  }
}

function clearSearchResults() {
  if (el.searchResultsSection) {
    el.searchResultsSection.style.display = 'none';
  }
  if (el.catalogSearchInput) {
    el.catalogSearchInput.value = '';
  }
}
