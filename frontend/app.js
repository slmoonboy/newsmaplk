/* ═══════════════════════════════════════════════════════════════
   LankaPulse — Main Application (MapLibre + WebSocket + UI)
   ═══════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ─── State ────────────────────────────────────────────────
  const state = {
    events: [],
    markers: new Map(),
    selectedEvent: null,
    ws: null,
    map: null,
    activeFilter: 'all',
  };

  // ─── Sri Lanka Bounds ─────────────────────────────────────
  const SL_CENTER = [80.7718, 7.8731];
  const SL_ZOOM = 7.8;
  const SL_BOUNDS = [[79.3, 5.7], [82.1, 10.0]];

  // ─── DOM Refs ─────────────────────────────────────────────
  const $ = (s) => document.querySelector(s);
  const tooltip = $('#map-tooltip');
  const drawer = $('#drawer');
  const overlay = $('#drawer-overlay');
  const emptyState = $('#empty-state');

  // ─── Initialize Map ───────────────────────────────────────
  function initMap() {
    state.map = new maplibregl.Map({
      container: 'map',
      style: {
        version: 8,
        name: 'LankaPulse Monochrome',
        sources: {
          'osm-tiles': {
            type: 'raster',
            tiles: [
              'https://a.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}@2x.png',
              'https://b.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}@2x.png',
            ],
            tileSize: 256,
            attribution: '© CARTO © OSM',
          },
        },
        layers: [{
          id: 'base-tiles',
          type: 'raster',
          source: 'osm-tiles',
          paint: { 'raster-saturation': -1, 'raster-contrast': 0.15, 'raster-brightness-max': 0.45 },
        }],
        glyphs: 'https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf',
      },
      center: SL_CENTER,
      zoom: SL_ZOOM,
      maxBounds: SL_BOUNDS,
      minZoom: 6.5,
      maxZoom: 14,
    });

    state.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');

    state.map.on('load', () => {
      loadDistrictBorders();
      fetchEvents();
      connectWebSocket();
      startClock();
    });
  }

  // ─── Load Sri Lanka District GeoJSON Borders ──────────────
  function loadDistrictBorders() {
    // Use a simplified Sri Lanka outline if no GeoJSON is available
    fetch('/static/sri-lanka-districts.geojson')
      .then(r => { if (r.ok) return r.json(); throw new Error('no geojson'); })
      .then(geojson => {
        state.map.addSource('districts', { type: 'geojson', data: geojson });
        state.map.addLayer({
          id: 'district-borders',
          type: 'line',
          source: 'districts',
          paint: {
            'line-color': '#27272A',
            'line-width': 1,
            'line-opacity': 0.8,
          },
        });
        state.map.addLayer({
          id: 'district-fill',
          type: 'fill',
          source: 'districts',
          paint: {
            'fill-color': '#09090B',
            'fill-opacity': 0.1,
          },
        });
      })
      .catch(() => { /* District borders optional */ });
  }

  // ─── Fetch Events via REST ────────────────────────────────
  async function fetchEvents() {
    try {
      const resp = await fetch('/api/events?hours=24&active_only=true');
      const data = await resp.json();
      if (data.events) {
        state.events = data.events;
        renderAllMarkers();
        updateStats();
      }
    } catch (e) {
      console.warn('Failed to fetch events:', e);
    }
  }

  // ─── WebSocket Connection ─────────────────────────────────
  function connectWebSocket() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/ws/live`;

    try {
      state.ws = new WebSocket(url);

      state.ws.onopen = () => console.log('[WS] Connected');

      state.ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          handleWSMessage(msg);
        } catch (e) { /* ignore */ }
      };

      state.ws.onclose = () => {
        console.log('[WS] Disconnected, reconnecting in 5s...');
        setTimeout(connectWebSocket, 5000);
      };

      state.ws.onerror = () => state.ws.close();

      // Ping keepalive
      setInterval(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          state.ws.send('ping');
        }
      }, 25000);
    } catch (e) {
      setTimeout(connectWebSocket, 5000);
    }
  }

  function handleWSMessage(msg) {
    if (msg.type === 'initial_state' && msg.events) {
      state.events = msg.events;
      renderAllMarkers();
      updateStats();
    } else if (msg.type === 'event_update' && msg.event) {
      upsertEvent(msg.event);
    }
  }

  function upsertEvent(event) {
    const idx = state.events.findIndex(e => e.id === event.id);
    if (idx >= 0) {
      state.events[idx] = event;
    } else {
      state.events.unshift(event);
    }
    renderMarker(event);
    updateStats();

    // If this event is currently open in the drawer, refresh it
    if (state.selectedEvent && state.selectedEvent.id === event.id) {
      state.selectedEvent = event;
      renderDrawer(event);
    }
  }

  // ─── Map Markers ──────────────────────────────────────────
  function renderAllMarkers() {
    // Clear existing
    state.markers.forEach(m => m.remove());
    state.markers.clear();

    state.events.forEach(e => renderMarker(e));

    // Toggle empty state
    emptyState.style.display = state.events.length === 0 ? 'flex' : 'none';
  }

  function renderMarker(event) {
    // Remove old marker if exists
    if (state.markers.has(event.id)) {
      state.markers.get(event.id).remove();
    }

    const el = document.createElement('div');
    el.className = 'marker-container';

    const now = Date.now();
    const reported = new Date(event.first_reported_at).getTime();
    const minutesAgo = (now - reported) / 60000;
    if (minutesAgo > 60) el.classList.add('old');

    el.innerHTML = `
      <div class="marker-pulse"></div>
      <div class="marker-pin"></div>
    `;

    // Hover tooltip
    el.addEventListener('mouseenter', (e) => showTooltip(e, event));
    el.addEventListener('mouseleave', hideTooltip);
    el.addEventListener('click', () => openDrawer(event));

    const marker = new maplibregl.Marker({ element: el, anchor: 'center' })
      .setLngLat([event.longitude, event.latitude])
      .addTo(state.map);

    state.markers.set(event.id, marker);
  }

  // ─── Tooltip ──────────────────────────────────────────────
  function showTooltip(mouseEvent, event) {
    $('#tooltip-location').textContent = `${event.town}, ${event.district}`;
    $('#tooltip-title').textContent = event.title_en;
    $('#tooltip-meta').textContent = `${event.source_count} source${event.source_count !== 1 ? 's' : ''} • ${event.category}`;

    tooltip.style.left = mouseEvent.pageX + 16 + 'px';
    tooltip.style.top = mouseEvent.pageY - 20 + 'px';
    tooltip.classList.add('visible');
  }

  function hideTooltip() {
    tooltip.classList.remove('visible');
  }

  // ─── Event Drawer ─────────────────────────────────────────
  function openDrawer(event) {
    state.selectedEvent = event;
    renderDrawer(event);
    drawer.classList.add('open');
    overlay.classList.add('open');

    // Pan map to event
    state.map.flyTo({
      center: [event.longitude, event.latitude],
      zoom: Math.max(state.map.getZoom(), 10),
      duration: 800,
    });
  }

  function closeDrawer() {
    drawer.classList.remove('open');
    overlay.classList.remove('open');
    state.selectedEvent = null;
  }

  function renderDrawer(event) {
    // Location & Coords
    $('#drawer-location').textContent = `${event.town}, ${event.district} District`;
    $('#drawer-coords').textContent = `${event.latitude.toFixed(4)}° N, ${event.longitude.toFixed(4)}° E`;

    // Titles
    $('#drawer-title').textContent = event.title_en;
    $('#drawer-title-si').textContent = event.title_si || '';

    // Category
    const catBadge = $('#drawer-category');
    catBadge.setAttribute('data-cat', event.category);
    catBadge.textContent = event.category;

    // Time ago
    const mins = Math.floor((Date.now() - new Date(event.first_reported_at).getTime()) / 60000);
    let timeText = mins < 60 ? `${mins}m ago` : `${Math.floor(mins/60)}h ${mins%60}m ago`;
    $('#drawer-time-ago').textContent = timeText;

    // What happened
    $('#drawer-what').textContent = event.what_happened;

    // Timeline
    const timeline = $('#drawer-timeline');
    timeline.innerHTML = '';
    (event.how_it_happened || []).forEach(step => {
      const li = document.createElement('li');
      li.className = 'timeline-item';
      li.innerHTML = `<div class="timeline-dot"></div><div>${escHtml(step)}</div>`;
      timeline.appendChild(li);
    });

    // Verification meter
    const reports = event.media_reports || [];
    const sourceCount = reports.length || event.source_count || 0;
    const verified = sourceCount >= 2;
    const pct = Math.min(sourceCount * 25, 100);

    const badge = $('#verify-badge');
    badge.textContent = verified ? 'VERIFIED' : 'PENDING';
    badge.className = `verify-badge ${verified ? 'verified' : 'unverified'}`;

    $('#verify-bar-fill').style.width = pct + '%';

    const sourceNames = [...new Set(reports.map(r => r.outlet_name))];
    $('#verify-sources').innerHTML = verified
      ? `Verified across <strong>${sourceNames.length} network${sourceNames.length !== 1 ? 's' : ''}</strong>: ${sourceNames.join(', ')}`
      : `Awaiting corroboration from additional sources...`;

    // Media feed
    renderMediaFeed(reports);
  }

  // ─── Media Feed (Right Column) ────────────────────────────
  function renderMediaFeed(reports, filter) {
    filter = filter || state.activeFilter;
    const scroll = $('#feed-scroll');
    scroll.innerHTML = '';

    let filtered = reports;
    if (filter !== 'all') {
      filtered = reports.filter(r => r.platform === filter);
    }

    if (filtered.length === 0) {
      scroll.innerHTML = `
        <div style="text-align:center;padding:40px 20px;color:var(--text-dim);font-size:13px;">
          No ${filter === 'all' ? '' : filter.replace('_', ' ').toLowerCase() + ' '}reports yet
        </div>`;
      return;
    }

    filtered.forEach(report => {
      const card = document.createElement('div');
      card.className = 'media-card';

      const initials = report.outlet_name.split(' ').map(w => w[0]).join('').substring(0, 2).toUpperCase();
      const timeStr = report.published_at
        ? new Date(report.published_at).toLocaleString('en-LK', { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'short' })
        : '';

      card.innerHTML = `
        <div class="media-card-header">
          <div class="media-card-source">
            <div class="source-avatar">${initials}</div>
            <div>
              <div class="source-name">${escHtml(report.outlet_name)}</div>
              ${report.author_handle ? `<div class="source-handle">${escHtml(report.author_handle)}</div>` : ''}
            </div>
          </div>
          <div class="media-card-meta">
            <span class="lang-badge">${report.language}</span>
            <span class="platform-badge">${report.platform.replace('_', ' ')}</span>
          </div>
        </div>
        <div class="media-card-quote">${escHtml(report.headline_or_quote)}</div>
        ${timeStr ? `<div class="media-card-time">${timeStr}</div>` : ''}
        ${report.url ? `<a class="media-card-link" href="${escHtml(report.url)}" target="_blank" rel="noopener">Open Original →</a>` : ''}
      `;

      scroll.appendChild(card);
    });
  }

  // ─── Stats ────────────────────────────────────────────────
  function updateStats() {
    $('#stat-events-24h').textContent = state.events.length;
    const districts = new Set(state.events.map(e => e.district));
    $('#stat-districts').textContent = districts.size;
  }

  function startClock() {
    const update = () => {
      const now = new Date();
      $('#stat-time').textContent = now.toLocaleTimeString('en-LK', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    };
    update();
    setInterval(update, 1000);
  }

  // ─── Utilities ────────────────────────────────────────────
  function escHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  // ─── Event Listeners ─────────────────────────────────────
  function bindEvents() {
    // Close drawer
    $('#drawer-close').addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });

    // Feed filter tabs
    $('#feed-tabs').addEventListener('click', (e) => {
      const tab = e.target.closest('.feed-tab');
      if (!tab) return;
      document.querySelectorAll('.feed-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      state.activeFilter = tab.dataset.filter;
      if (state.selectedEvent) {
        renderMediaFeed(state.selectedEvent.media_reports || []);
      }
    });
  }

  // ─── Boot ─────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    bindEvents();
    initMap();
  });

})();
