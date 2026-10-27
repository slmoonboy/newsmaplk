/* ═══════════════════════════════════════════════════════════════
   LankaPulse — frontend application
   MapLibre GL + WebSocket + REST. No build step, no framework.
   ═══════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  // ─── Constants ────────────────────────────────────────────
  const SL_CENTER = [80.7718, 7.8731];
  const SL_ZOOM = 7.0;
  const SL_ZOOM_NARROW = 6.6;
  // Generous bounds: keep the user near Sri Lanka without forcing a zoom level
  // (a tight maxBounds makes MapLibre zoom in differently per window size).
  const SL_BOUNDS = [[73.5, 2.5], [88.0, 13.5]];
  const OPENFREEMAP_STYLE = 'https://tiles.openfreemap.org/styles/dark';
  const RASTER_STYLE = {
    version: 8,
    sources: {
      osm: {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        attribution: '© OpenStreetMap contributors',
      },
    },
    layers: [{
      id: 'osm',
      type: 'raster',
      source: 'osm',
      paint: {
        'raster-opacity': 0.55,
        'raster-saturation': -1,
        'raster-contrast': 0.1,
        'raster-brightness-max': 0.55,
      },
    }],
  };
  const CATEGORY_COLORS = {
    ACCIDENT: '#EF4444',
    WEATHER: '#3B82F6',
    POLITICS: '#A855F7',
    CRIME: '#F97316',
    ECONOMY: '#22C55E',
    INFRASTRUCTURE: '#06B6D4',
    OTHER: '#9CA3AF',
  };

  // ─── State ────────────────────────────────────────────────
  const state = {
    events: new Map(),
    markers: new Map(),
    selectedId: null,
    map: null,
    ws: null,
    retry: 0,
    reconnectTimer: null,
    everConnected: false,
    filter: 'all',
    mediaFilter: 'all',
    hours: 24,
    stats: null,
    lastFetch: 0,
    booted: false,
    styleLoaded: false,
  };

  const $ = (selector) => document.querySelector(selector);

  // Exposed for debugging / embedding; harmless in production.
  window.__lankapulse = state;

  const drawer = $('#drawer');
  const overlay = $('#drawer-overlay');
  const tooltip = $('#map-tooltip');
  const emptyState = $('#empty-state');
  const panel = $('#feed-panel');
  const panelOpenButton = $('#panel-open');

  // ─── Boot ─────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', () => {
    bindEvents();
    restorePanelState();
    initMap();
    // The feed and WebSocket must not depend on WebGL/tile loading.
    bootOnce();
    refreshStats();
    setInterval(updateClock, 1000);
    setInterval(refreshRelativeTimes, 30000);
    setInterval(refreshStats, 60000);
    setInterval(wsPing, 25000);
    // If the WebSocket is blocked (proxy, CDN), fall back to polling.
    setInterval(() => {
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) fetchEvents();
    }, 60000);
    updateClock();
  });

  // ─── Map ──────────────────────────────────────────────────
  function initMap() {
    state.map = new maplibregl.Map({
      container: 'map',
      style: OPENFREEMAP_STYLE,
      center: SL_CENTER,
      zoom: window.innerWidth <= 560 ? SL_ZOOM_NARROW : SL_ZOOM,
      maxBounds: SL_BOUNDS,
      minZoom: 6.4,
      maxZoom: 14,
      attributionControl: false,
    });

    state.map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    state.map.addControl(new maplibregl.AttributionControl({
      compact: true,
      customAttribution: 'Boundaries © geoBoundaries (ODbL) · Feeds: public RSS',
    }), 'bottom-right');

    state.map.on('style.load', onStyleLoad);
    state.map.on('load', bootOnce);
    state.map.on('movestart', hideTooltip);
    state.map.on('click', closeDrawer);
    state.map.on('error', (event) => {
      if (event && event.error) console.warn('[map]', event.error.message || event.error);
    });

    // If the style CDN is unreachable, fall back to OSM raster tiles.
    window.setTimeout(() => {
      if (!state.styleLoaded && state.map && !state.map.isStyleLoaded()) {
        console.warn('[map] style timeout, using raster fallback');
        state.map.setStyle(RASTER_STYLE);
      }
    }, 7000);
  }

  function onStyleLoad() {
    state.styleLoaded = true;
    loadDistrictBorders();
  }

  function bootOnce() {
    updateMapPadding();
    if (state.booted) return;
    state.booted = true;
    fetchEvents();
    connectWebSocket();
  }

  // Keep Sri Lanka centred in the part of the map not covered by the feed.
  function updateMapPadding(extraBottom = 0) {
    if (!state.map) return;
    const panelVisible = !panel.hidden && window.innerWidth > 900;
    state.map.setPadding({
      top: 52,
      bottom: extraBottom,
      left: panelVisible ? 348 : 0,
      right: 0,
    });
  }

  function loadDistrictBorders() {
    if (!state.map || state.map.getSource('districts')) return;

    fetch('/static/sri-lanka-districts.geojson')
      .then((response) => {
        if (!response.ok) throw new Error('boundaries unavailable');
        return response.json();
      })
      .then((geojson) => {
        if (state.map.getSource('districts')) return;
        state.map.addSource('districts', { type: 'geojson', data: geojson });
        state.map.addLayer({
          id: 'district-fill',
          type: 'fill',
          source: 'districts',
          paint: { 'fill-color': '#09090B', 'fill-opacity': 0.28 },
        });
        state.map.addLayer({
          id: 'district-borders',
          type: 'line',
          source: 'districts',
          paint: {
            'line-color': '#52525B',
            'line-width': 0.8,
            'line-opacity': 0.55,
          },
        });
      })
      .catch((error) => console.warn('[districts]', error.message));
  }

  // ─── REST ─────────────────────────────────────────────────
  async function fetchEvents() {
    try {
      const response = await fetch(`/api/events?hours=${state.hours}&active_only=true&limit=200`);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      setEvents(data.events || []);
      state.lastFetch = Date.now();
    } catch (error) {
      console.warn('Failed to fetch events:', error);
    }
  }

  async function refreshStats() {
    try {
      const response = await fetch('/api/stats');
      if (!response.ok) return;
      state.stats = await response.json();
      updateStats();
    } catch (_) {
      /* keep derived stats */
    }
  }

  function setEvents(events) {
    state.events = new Map((events || []).map((event) => [event.id, event]));
    renderAllMarkers();
    renderFeed();
    updateStats();
  }

  function upsertEvent(event) {
    if (!event || typeof event.id !== 'number') return;
    state.events.set(event.id, event);
    renderMarker(event);
    renderFeed();
    updateStats();
    if (state.selectedId === event.id) renderDrawer(event);
  }

  // ─── WebSocket ────────────────────────────────────────────
  function connectWebSocket() {
    const protocol = location.protocol === 'https:' ? 'wss' : 'ws';
    let socket;
    try {
      socket = new WebSocket(`${protocol}://${location.host}/ws/live`);
    } catch (error) {
      scheduleReconnect();
      return;
    }
    state.ws = socket;

    socket.onopen = () => {
      const wasConnected = state.everConnected;
      state.everConnected = true;
      state.retry = 0;
      setConnection(true);
      // Catch up on anything missed while disconnected.
      if (wasConnected) fetchEvents();
    };

    socket.onmessage = (message) => {
      let payload;
      try {
        payload = JSON.parse(message.data);
      } catch (_) {
        return;
      }
      handleMessage(payload);
    };

    socket.onclose = () => {
      setConnection(false);
      scheduleReconnect();
    };

    socket.onerror = () => {
      try { socket.close(); } catch (_) { /* noop */ }
    };
  }

  function scheduleReconnect() {
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    const delay = Math.min(30000, 1000 * Math.pow(2, state.retry++));
    state.reconnectTimer = setTimeout(connectWebSocket, delay);
  }

  function wsPing() {
    if (state.ws && state.ws.readyState === WebSocket.OPEN) {
      try { state.ws.send('ping'); } catch (_) { /* noop */ }
    }
  }

  function handleMessage(message) {
    if (message.type === 'initial_state') {
      setEvents(message.events || []);
    } else if (message.type === 'event_update' && message.event) {
      upsertEvent(message.event);
    }
  }

  function setConnection(connected) {
    const indicator = $('#conn-indicator');
    const label = $('#conn-state');
    const dot = $('#live-dot');
    indicator.classList.toggle('offline', !connected);
    label.textContent = connected ? 'LIVE' : 'OFFLINE';
    dot.classList.toggle('offline', !connected);
  }

  // ─── Markers ──────────────────────────────────────────────
  function renderAllMarkers() {
    state.markers.forEach((marker) => marker.remove());
    state.markers.clear();
    state.events.forEach((event) => renderMarker(event));
    emptyState.hidden = state.events.size > 0;
  }

  function renderMarker(event) {
    if (!state.map) return;

    const existing = state.markers.get(event.id);
    if (existing) existing.remove();

    const element = document.createElement('div');
    element.className = 'marker-container';
    element.style.setProperty('--cat', CATEGORY_COLORS[event.category] || CATEGORY_COLORS.OTHER);
    element.setAttribute('role', 'button');
    element.setAttribute('tabindex', '0');
    element.setAttribute('aria-label', `${event.title_en} — ${event.town}, ${event.district}`);

    const pulse = document.createElement('div');
    pulse.className = 'marker-pulse';
    const pin = document.createElement('div');
    pin.className = 'marker-pin';
    element.append(pulse, pin);

    const reportedAt = new Date(event.first_reported_at || Date.now()).getTime();
    if (Number.isFinite(reportedAt) && Date.now() - reportedAt > 60 * 60 * 1000) {
      element.classList.add('old');
    }
    if (state.selectedId === event.id) element.classList.add('selected');

    element.addEventListener('mouseenter', (mouseEvent) => showTooltip(mouseEvent, event));
    element.addEventListener('mouseleave', hideTooltip);
    element.addEventListener('focus', () => showTooltip({ clientX: window.innerWidth / 2, clientY: 120 }, event));
    element.addEventListener('blur', hideTooltip);
    element.addEventListener('click', (mouseEvent) => {
      mouseEvent.stopPropagation();
      openDrawer(event.id);
    });
    element.addEventListener('keydown', (keyboardEvent) => {
      if (keyboardEvent.key === 'Enter' || keyboardEvent.key === ' ') {
        keyboardEvent.preventDefault();
        openDrawer(event.id);
      }
    });

    const marker = new maplibregl.Marker({ element, anchor: 'center' })
      .setLngLat([Number(event.longitude), Number(event.latitude)])
      .addTo(state.map);

    state.markers.set(event.id, marker);
  }

  function highlightMarker(eventId) {
    state.markers.forEach((marker, id) => {
      const element = marker.getElement();
      if (element) element.classList.toggle('selected', id === eventId);
    });
  }

  // ─── Tooltip ──────────────────────────────────────────────
  function showTooltip(mouseEvent, event) {
    $('#tooltip-location').textContent = `${event.town}, ${event.district}`;
    $('#tooltip-title').textContent = event.title_en;
    const sourceLabel = `${event.source_count || 0} source${event.source_count === 1 ? '' : 's'}`;
    $('#tooltip-meta').textContent = `${sourceLabel} · ${event.category}`;

    const left = Math.min((mouseEvent.clientX || 0) + 16, window.innerWidth - 330);
    const top = Math.max((mouseEvent.clientY || 0) - 24, 64);
    tooltip.style.left = `${Math.max(12, left)}px`;
    tooltip.style.top = `${top}px`;
    tooltip.classList.add('visible');
  }

  function hideTooltip() {
    tooltip.classList.remove('visible');
  }

  // ─── Live feed panel ──────────────────────────────────────
  function visibleEvents() {
    const list = Array.from(state.events.values()).sort(
      (a, b) => new Date(b.first_reported_at || 0) - new Date(a.first_reported_at || 0)
    );
    if (state.filter === 'all') return list;
    return list.filter((event) => event.category === state.filter);
  }

  function renderFeed() {
    const list = $('#feed-list');
    if (!list) return;
    list.replaceChildren();

    const events = visibleEvents();
    const total = state.events.size;
    $('#feed-sub').textContent = events.length === total
      ? `${total} event${total === 1 ? '' : 's'}`
      : `${events.length} of ${total} events`;

    if (!events.length) {
      const message = document.createElement('div');
      message.className = 'feed-empty';
      message.textContent = total
        ? 'No events match this filter.'
        : 'No events yet. New pins appear automatically.';
      list.append(message);
      return;
    }

    const fragment = document.createDocumentFragment();
    events.forEach((event) => fragment.append(eventCard(event)));
    list.append(fragment);
  }

  function eventCard(event) {
    const card = document.createElement('button');
    card.type = 'button';
    card.className = 'event-card';
    card.style.setProperty('--cat', CATEGORY_COLORS[event.category] || CATEGORY_COLORS.OTHER);
    card.addEventListener('click', () => openDrawer(event.id));

    const top = document.createElement('div');
    top.className = 'event-card-top';

    const dot = document.createElement('span');
    dot.className = 'cat-dot';

    const category = document.createElement('span');
    category.className = 'event-cat';
    category.textContent = event.category || 'OTHER';

    const time = document.createElement('span');
    time.className = 'event-time';
    time.dataset.ts = event.first_reported_at || '';
    time.textContent = timeAgo(event.first_reported_at);

    top.append(dot, category, time);

    const title = document.createElement('div');
    title.className = 'event-card-title';
    title.textContent = event.title_en || '';

    const location = document.createElement('div');
    location.className = 'event-card-loc';
    location.textContent = `${event.town || 'Unknown'}, ${event.district || ''}`.trim();

    const meta = document.createElement('div');
    meta.className = 'event-card-meta';
    const verified = event.is_verified ? 'Verified' : 'Unconfirmed';
    meta.textContent = `${event.source_count || 0} source${event.source_count === 1 ? '' : 's'} · ${verified}`;

    card.append(top, title, location, meta);
    return card;
  }

  // ─── Drawer ───────────────────────────────────────────────
  function openDrawer(eventId) {
    const cached = state.events.get(eventId);
    if (!cached) return;

    state.selectedId = eventId;
    hideTooltip();
    renderDrawer(cached);
    renderMediaFeed(cached.media_reports || []);
    drawer.classList.add('open');
    overlay.classList.add('open');
    drawer.setAttribute('aria-hidden', 'false');
    highlightMarker(eventId);

    if (state.map) {
      const panelVisible = !panel.hidden && window.innerWidth > 900;
      const drawerPad = window.innerWidth <= 900
        ? Math.round(window.innerHeight * 0.7)
        : Math.min(Math.round(window.innerHeight * 0.6), 640);
      state.map.setPadding({
        top: 52,
        bottom: drawerPad,
        left: panelVisible ? 348 : 0,
        right: 0,
      });
      state.map.flyTo({
        center: [Number(cached.longitude), Number(cached.latitude)],
        zoom: Math.max(state.map.getZoom(), 10),
        duration: 700,
      });
    }

    // Pull the full record (all media reports) in the background.
    fetch(`/api/events/${eventId}`)
      .then((response) => (response.ok ? response.json() : null))
      .then((full) => {
        if (!full || state.selectedId !== eventId) return;
        state.events.set(full.id, full);
        renderDrawer(full);
        renderMediaFeed(full.media_reports || []);
        renderFeed();
      })
      .catch(() => { /* cached copy is fine */ });
  }

  function closeDrawer() {
    if (!drawer.classList.contains('open')) return;
    drawer.classList.remove('open');
    overlay.classList.remove('open');
    drawer.setAttribute('aria-hidden', 'true');
    state.selectedId = null;
    highlightMarker(null);
    updateMapPadding();
  }

  function renderDrawer(event) {
    const location = `${event.town || 'Unknown'}, ${event.district || 'Sri Lanka'}`;
    $('#drawer-location').textContent = event.district ? `${location} District` : location;

    const lat = Number(event.latitude);
    const lng = Number(event.longitude);
    $('#drawer-coords').textContent = Number.isFinite(lat) && Number.isFinite(lng)
      ? `${lat.toFixed(4)}° N, ${lng.toFixed(4)}° E`
      : '';

    const precision = event.location_precision || 'exact';
    const precisionBadge = $('#drawer-precision');
    if (precision === 'exact') {
      precisionBadge.hidden = true;
    } else {
      precisionBadge.hidden = false;
      precisionBadge.textContent = {
        approximate: 'Approximate location',
        district: 'District centre',
        country: 'Country level',
      }[precision] || 'Approximate location';
    }

    $('#drawer-title').textContent = event.title_en || '';
    setOptionalText($('#drawer-title-si'), event.title_si, 'si');
    setOptionalText($('#drawer-title-ta'), event.title_ta, 'ta');

    const category = $('#drawer-category');
    category.setAttribute('data-cat', event.category || 'OTHER');
    category.textContent = event.category || 'OTHER';

    $('#drawer-time-ago').textContent = timeAgo(event.first_reported_at);
    $('#drawer-time-ago').dataset.ts = event.first_reported_at || '';

    renderProvenance(event);
    $('#drawer-what').textContent = event.what_happened || 'No summary available.';

    const timeline = $('#drawer-timeline');
    timeline.replaceChildren();
    const steps = Array.isArray(event.how_it_happened) ? event.how_it_happened : [];
    if (!steps.length) {
      const item = document.createElement('li');
      item.className = 'timeline-item muted';
      item.textContent = 'No timeline available.';
      timeline.append(item);
    } else {
      steps.forEach((step) => {
        const item = document.createElement('li');
        item.className = 'timeline-item';
        const dot = document.createElement('div');
        dot.className = 'timeline-dot';
        const text = document.createElement('div');
        text.textContent = String(step);
        item.append(dot, text);
        timeline.append(item);
      });
    }

    renderVerification(event);
  }

  function setOptionalText(element, value, language) {
    const text = (value || '').trim();
    element.hidden = !text;
    element.textContent = text;
    if (language === 'si') element.lang = 'si';
    if (language === 'ta') element.lang = 'ta';
  }

  function renderProvenance(event) {
    const element = $('#drawer-provenance');
    element.replaceChildren();
    const extractor = (event.extractor || '').trim();
    if (!extractor) {
      element.hidden = true;
      return;
    }
    element.hidden = false;

    const extractorLabel = extractor === 'heuristic'
      ? 'Rule-based summary'
      : extractor === 'demo'
        ? 'Demo data'
        : `Summary by ${extractor}`;
    const extractorSpan = document.createElement('span');
    extractorSpan.textContent = extractorLabel;
    element.append(extractorSpan);

    const providers = Array.isArray(event.grounding_providers) ? event.grounding_providers : [];
    const labels = providers
      .filter((provider) => provider !== 'primary')
      .map((provider) => ({
        google_news: 'Google News',
        wikipedia: 'Wikipedia',
        gdelt: 'GDELT',
        demo: 'demo',
      }[provider] || provider));
    if (labels.length) {
      const separator = document.createElement('span');
      separator.className = 'provenance-sep';
      separator.textContent = '·';
      const context = document.createElement('span');
      context.textContent = `Context: ${labels.join(', ')}`;
      element.append(separator, context);
    }
  }

  function renderVerification(event) {
    const reports = Array.isArray(event.media_reports) ? event.media_reports : [];
    const outlets = Array.from(new Set(
      reports.map((report) => (report.outlet_name || '').trim()).filter(Boolean)
    ));
    const verified = outlets.length >= 2;
    const percent = Math.min(100, outlets.length * 33);

    const badge = $('#verify-badge');
    badge.textContent = verified ? 'VERIFIED' : 'PENDING';
    badge.className = `verify-badge ${verified ? 'verified' : 'unverified'}`;
    $('#verify-bar-fill').style.width = `${percent}%`;

    const container = $('#verify-sources');
    container.replaceChildren();
    if (verified) {
      container.append(document.createTextNode('Confirmed by '));
      const strong = document.createElement('strong');
      strong.textContent = `${outlets.length} outlets`;
      container.append(strong, document.createTextNode(`: ${outlets.slice(0, 6).join(', ')}`));
      if (outlets.length > 6) {
        container.append(document.createTextNode(` and ${outlets.length - 6} more`));
      }
    } else {
      container.textContent = 'Only one outlet so far — treat this report as unconfirmed.';
    }
  }

  // ─── Media feed ───────────────────────────────────────────
  function renderMediaFeed(reports) {
    const container = $('#feed-scroll');
    container.replaceChildren();

    const list = Array.isArray(reports) ? reports : [];
    const filtered = state.mediaFilter === 'all'
      ? list
      : state.mediaFilter === 'WEB'
        ? list.filter((report) => report.platform === 'WEB' || report.platform === 'RSS')
        : list.filter((report) => report.platform === state.mediaFilter);

    if (!filtered.length) {
      const message = document.createElement('div');
      message.className = 'feed-scroll-empty';
      message.textContent = list.length
        ? 'No reports of this type for this event.'
        : 'No media reports linked yet.';
      container.append(message);
      return;
    }

    filtered.forEach((report) => container.append(mediaCard(report)));
  }

  function mediaCard(report) {
    const card = document.createElement('article');
    card.className = 'media-card';

    const header = document.createElement('div');
    header.className = 'media-card-header';

    const source = document.createElement('div');
    source.className = 'media-card-source';
    const avatar = document.createElement('div');
    avatar.className = 'source-avatar';
    avatar.textContent = initials(report.outlet_name);
    const sourceText = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'source-name';
    name.textContent = report.outlet_name || 'Unknown outlet';
    sourceText.append(name);
    if (report.author_handle) {
      const handle = document.createElement('div');
      handle.className = 'source-handle';
      handle.textContent = report.author_handle;
      sourceText.append(handle);
    }
    source.append(avatar, sourceText);

    const meta = document.createElement('div');
    meta.className = 'media-card-meta';
    const language = document.createElement('span');
    language.className = 'lang-badge';
    language.textContent = (report.language || 'EN').toUpperCase();
    const platform = document.createElement('span');
    platform.className = 'platform-badge';
    platform.textContent = platformLabel(report.platform);
    meta.append(language, platform);

    header.append(source, meta);

    const quote = document.createElement('div');
    quote.className = 'media-card-quote';
    quote.textContent = report.headline_or_quote || '';

    card.append(header, quote);

    const published = formatDateTime(report.published_at);
    if (published) {
      const time = document.createElement('div');
      time.className = 'media-card-time';
      time.textContent = `${published}${report.provider ? ` · ${providerLabel(report.provider)}` : ''}`;
      card.append(time);
    }

    const href = safeUrl(report.url);
    if (href) {
      const link = document.createElement('a');
      link.className = 'media-card-link';
      link.href = href;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = 'Open original →';
      card.append(link);
    }

    return card;
  }

  // ─── Stats / clock ────────────────────────────────────────
  function updateStats() {
    const events = Array.from(state.events.values());
    const cutoff = Date.now() - 24 * 60 * 60 * 1000;
    const recent = events.filter((event) => {
      const time = new Date(event.first_reported_at || 0).getTime();
      return Number.isFinite(time) && time >= cutoff;
    });
    const fallbackDistricts = new Set(recent.map((event) => event.district).filter(Boolean));

    $('#stat-events-24h').textContent = state.stats
      ? state.stats.events_24h
      : recent.length;
    $('#stat-districts').textContent = state.stats
      ? state.stats.districts_active
      : fallbackDistricts.size;
  }

  function updateClock() {
    const now = new Date();
    $('#stat-time').textContent = now.toLocaleTimeString('en-LK', {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hour12: false,
    });
  }

  function refreshRelativeTimes() {
    document.querySelectorAll('[data-ts]').forEach((element) => {
      element.textContent = timeAgo(element.dataset.ts);
    });
  }

  // ─── Panel helpers ────────────────────────────────────────
  function restorePanelState() {
    const stored = localStorage.getItem('lp_panel');
    const narrow = window.matchMedia('(max-width: 900px)').matches;
    const open = narrow ? stored === 'open' : stored !== 'closed';
    setPanel(open, false);
  }

  function setPanel(open, persist = true) {
    panel.hidden = !open;
    panelOpenButton.hidden = open;
    if (persist) localStorage.setItem('lp_panel', open ? 'open' : 'closed');
    if (!drawer.classList.contains('open')) updateMapPadding();
  }

  // ─── Utilities ────────────────────────────────────────────
  function initials(name) {
    return (name || '?')
      .split(/\s+/)
      .map((word) => word[0])
      .join('')
      .slice(0, 2)
      .toUpperCase();
  }

  function platformLabel(platform) {
    return {
      WEB: 'Web',
      RSS: 'Web',
      X_POST: '𝕏 Post',
      YOUTUBE: 'YouTube',
    }[platform] || platform || 'Web';
  }

  function providerLabel(provider) {
    return {
      rss: 'RSS',
      google_news: 'Google News',
      wikipedia: 'Wikipedia',
      gdelt: 'GDELT',
      llm: 'AI',
      heuristic: 'Rule-based',
      demo: 'Demo',
      primary: 'Original',
    }[provider] || provider;
  }

  function safeUrl(value) {
    if (!value) return '';
    try {
      const url = new URL(value, window.location.origin);
      if (url.protocol === 'http:' || url.protocol === 'https:') return url.href;
    } catch (_) {
      /* invalid */
    }
    return '';
  }

  function formatDateTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return '';
    return date.toLocaleString('en-LK', {
      day: 'numeric',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function timeAgo(value) {
    if (!value) return '';
    const time = new Date(value).getTime();
    if (!Number.isFinite(time)) return '';
    const minutes = Math.max(0, Math.floor((Date.now() - time) / 60000));
    if (minutes < 1) return 'now';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ${minutes % 60}m ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  }

  // ─── Events ───────────────────────────────────────────────
  function bindEvents() {
    $('#drawer-close').addEventListener('click', closeDrawer);
    overlay.addEventListener('click', closeDrawer);

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeDrawer();
        hideTooltip();
      }
    });

    $('#category-chips').addEventListener('click', (event) => {
      const chip = event.target.closest('.chip');
      if (!chip) return;
      state.filter = chip.dataset.cat || 'all';
      document.querySelectorAll('#category-chips .chip').forEach((button) => {
        button.classList.toggle('active', button === chip);
      });
      renderFeed();
    });

    $('#window-select').addEventListener('change', (event) => {
      state.hours = Number(event.target.value) || 0;
      fetchEvents();
    });

    $('#feed-tabs').addEventListener('click', (event) => {
      const tab = event.target.closest('.feed-tab');
      if (!tab) return;
      state.mediaFilter = tab.dataset.filter || 'all';
      document.querySelectorAll('#feed-tabs .feed-tab').forEach((button) => {
        button.classList.toggle('active', button === tab);
      });
      const selected = state.selectedId ? state.events.get(state.selectedId) : null;
      renderMediaFeed(selected ? selected.media_reports || [] : []);
    });

    $('#panel-collapse').addEventListener('click', () => setPanel(false));
    panelOpenButton.addEventListener('click', () => setPanel(true));

    window.addEventListener('online', () => {
      fetchEvents();
      if (!state.ws || state.ws.readyState > WebSocket.OPEN) connectWebSocket();
    });

    let resizeTimer = null;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (!drawer.classList.contains('open')) updateMapPadding();
      }, 200);
    });

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && Date.now() - state.lastFetch > 60000) {
        fetchEvents();
        refreshStats();
      }
    });
  }
})();
