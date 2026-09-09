/* Masseberegning - frontend
 *
 * Leaflet map + Kartverket base layers, polygon drawing, and rendering of the
 * cut/fill result returned by the Python backend.
 */

/* Leaflet.draw 1.0.4 predates Leaflet 1.9 and trips over a removed touch
 * helper. Neutralising it keeps polygon drawing working. */
if (window.L && L.Draw && L.Draw.Polyline) {
  L.Draw.Polyline.prototype._onTouch = L.Util.falseFn;
}

const nf = (d = 0) => new Intl.NumberFormat('nb-NO', {
  minimumFractionDigits: d, maximumFractionDigits: d,
});
const fmt = (v, d = 0) => (v === null || v === undefined || !isFinite(v)) ? '–' : nf(d).format(v);

/* Volumes span a huge range; pick decimals so the number stays readable. */
function vol(v) {
  const a = Math.abs(v);
  if (a >= 100000) return fmt(v, 0);
  if (a >= 1000) return fmt(v, 0);
  return fmt(v, 1);
}

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ map */

const map = L.map('map', { zoomControl: true, preferCanvas: true })
  .setView([62.5, 10.5], 5);

/* maxNativeZoom matters here. Kartverket's cache has no tiles beyond zoom 18
 * and answers deeper requests with HTTP 400, so without it the basemap goes
 * blank the moment fitBounds zooms past 18 on a small polygon. With it set,
 * Leaflet upscales the deepest real tile instead of asking for one that does
 * not exist. */
const baseLayers = {
  'Kartverket topo': L.tileLayer(
    'https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png',
    { maxZoom: 22, maxNativeZoom: 18, attribution: '&copy; Kartverket' }),
  'Kartverket gråtone': L.tileLayer(
    'https://cache.kartverket.no/v1/wmts/1.0.0/topograatone/default/webmercator/{z}/{y}/{x}.png',
    { maxZoom: 22, maxNativeZoom: 18, attribution: '&copy; Kartverket' }),
  'Flyfoto': L.tileLayer(
    'https://opencache.statkart.no/gatekeeper/gk/gk.open_nib_web_mercator_wmts_v2' +
    '?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=Nibcache_web_mercator_v2' +
    '&STYLE=default&TILEMATRIXSET=default028mm&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}' +
    '&FORMAT=image/jpgpng',
    { maxZoom: 22, maxNativeZoom: 20, attribution: '&copy; Kartverket / Norge i bilder' }),
  'OpenStreetMap': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 22, maxNativeZoom: 19, attribution: '&copy; OpenStreetMap' }),
};
baseLayers['Kartverket topo'].addTo(map);
const layerControl = L.control.layers(baseLayers, {}, { position: 'topright' }).addTo(map);

const drawnItems = new L.FeatureGroup().addTo(map);

const drawer = new L.Draw.Polygon(map, {
  allowIntersection: false,
  showArea: false,
  shapeOptions: { color: '#0f766e', weight: 2, fillOpacity: 0.12 },
});

let cutfillLayer = null;
let hillshadeLayer = null;
let pickingLevel = false;

/* --------------------------------------------------------------- state */

const state = { polygon: null, result: null };

function setPolygon(latlngs) {
  drawnItems.clearLayers();
  const poly = L.polygon(latlngs, {
    color: '#0f766e', weight: 2, fillOpacity: 0.12,
  }).addTo(drawnItems);
  state.polygon = poly;

  const area = L.GeometryUtil.geodesicArea(poly.getLatLngs()[0]);
  $('area-readout').classList.remove('empty');
  $('area-readout').innerHTML =
    `<strong>${fmt(area, 0)} m²</strong> &nbsp;(${fmt(area / 1000000, 3)} km² / ${fmt(area / 1000, 1)} daa)`;
  $('btn-calc').disabled = false;
  // Cap the zoom: a small polygon would otherwise fill the view at zoom 21,
  // well past any basemap's real tiles, leaving a blurry upscale.
  map.fitBounds(poly.getBounds(), { padding: [40, 40], maxZoom: 18 });
}

function clearAll() {
  drawnItems.clearLayers();
  state.polygon = null;
  state.result = null;
  if (cutfillLayer) { map.removeLayer(cutfillLayer); cutfillLayer = null; }
  if (hillshadeLayer) { map.removeLayer(hillshadeLayer); hillshadeLayer = null; }
  $('btn-calc').disabled = true;
  $('results').hidden = true;
  $('legend').hidden = true;
  $('status').hidden = true;
  $('area-readout').classList.add('empty');
  $('area-readout').innerHTML = '<span class="readout-label">Ingen flate valgt</span>';
}

/* --------------------------------------------------------------- draw */

$('btn-draw').addEventListener('click', () => {
  drawnItems.clearLayers();
  $('square-opts').hidden = true;
  drawer.enable();
  $('btn-draw').classList.add('active');
});

map.on(L.Draw.Event.CREATED, (e) => {
  $('btn-draw').classList.remove('active');
  setPolygon(e.layer.getLatLngs()[0]);
});
map.on(L.Draw.Event.DRAWSTOP, () => $('btn-draw').classList.remove('active'));

$('btn-square').addEventListener('click', () => {
  const opts = $('square-opts');
  if (opts.hidden) { opts.hidden = false; return; }
  const side = Math.max(20, Number($('square-size').value) || 1000);
  const c = map.getCenter();
  const dLat = (side / 2) / 111320;
  const dLon = (side / 2) / (111320 * Math.cos(c.lat * Math.PI / 180));
  setPolygon([
    [c.lat - dLat, c.lng - dLon], [c.lat - dLat, c.lng + dLon],
    [c.lat + dLat, c.lng + dLon], [c.lat + dLat, c.lng - dLon],
  ]);
});

$('btn-clear').addEventListener('click', clearAll);

/* ------------------------------------------------------------- search */

/* Accepts decimal pairs ("61.1153, 10.4662") and the DMS form Google Maps
 * shows ("61°06'55.1\"N 10°27'58.6\"E"). */
function parseCoords(text) {
  const s = text.trim();

  const dec = s.match(/^\s*(-?\d{1,3}(?:[.,]\d+)?)\s*[,;\s]\s*(-?\d{1,3}(?:[.,]\d+)?)\s*$/);
  if (dec) {
    const a = parseFloat(dec[1].replace(',', '.'));
    const b = parseFloat(dec[2].replace(',', '.'));
    if (Math.abs(a) <= 90 && Math.abs(b) <= 180) return { lat: a, lon: b };
  }

  const dms = /(\d{1,3})[°\s]+(\d{1,2})['′\s]+([\d.]+)["″\s]*([NSEWØVnsewøv])/g;
  const found = [];
  let m;
  while ((m = dms.exec(s)) !== null) {
    let v = Number(m[1]) + Number(m[2]) / 60 + Number(m[3]) / 3600;
    const hemi = m[4].toUpperCase();
    if (hemi === 'S' || hemi === 'W' || hemi === 'V') v = -v;
    found.push({ v, hemi });
  }
  if (found.length === 2) {
    const lat = found.find(f => 'NS'.includes(f.hemi)) || found[0];
    const lon = found.find(f => 'EWØV'.includes(f.hemi)) || found[1];
    return { lat: lat.v, lon: lon.v };
  }
  return null;
}

function jumpTo(lat, lon, zoom = 16) {
  map.setView([lat, lon], zoom);
  L.circleMarker([lat, lon], { radius: 6, color: '#0f766e', weight: 2, fillOpacity: .6 })
    .addTo(map).bindTooltip(`${fmt(lat, 5)}, ${fmt(lon, 5)}`).openTooltip();
  $('search-results').hidden = true;
  $('square-opts').hidden = false;
}

let searchTimer = null;
$('search').addEventListener('input', (e) => {
  const q = e.target.value.trim();
  clearTimeout(searchTimer);

  const coords = parseCoords(q);
  const box = $('search-results');
  if (coords) {
    box.innerHTML = `<button type="button"><span class="sr-name">Gå til ${fmt(coords.lat, 5)}, ${fmt(coords.lon, 5)}</span>
      <span class="sr-meta">Koordinat</span></button>`;
    box.hidden = false;
    box.querySelector('button').onclick = () => jumpTo(coords.lat, coords.lon);
    return;
  }
  if (q.length < 3) { box.hidden = true; return; }

  searchTimer = setTimeout(async () => {
    try {
      const r = await MB.api(`/api/sok?q=${encodeURIComponent(q)}`);
      const data = await r.json();
      if (!data.treff || !data.treff.length) { box.hidden = true; return; }
      box.innerHTML = '';
      data.treff.forEach(t => {
        const b = document.createElement('button');
        b.type = 'button';
        b.innerHTML = `<span class="sr-name">${t.navn}</span>
          <span class="sr-meta">${t.type}${t.kommune ? ' · ' + t.kommune : ''}${t.fylke ? ', ' + t.fylke : ''}</span>`;
        b.onclick = () => jumpTo(t.lat, t.lon);
        box.appendChild(b);
      });
      box.hidden = false;
    } catch { box.hidden = true; }
  }, 280);
});

document.addEventListener('click', (e) => {
  if (!e.target.closest('.search-row')) $('search-results').hidden = true;
});

/* ------------------------------------------------------------ controls */

$('soil-depth').addEventListener('input', (e) => {
  $('soil-out').textContent = fmt(Number(e.target.value), 1) + ' m';
});

document.querySelectorAll('input[name=mode]').forEach(r => {
  r.addEventListener('change', () => {
    $('fixed-opts').hidden = document.querySelector('input[name=mode]:checked').value !== 'fast';
  });
});

$('btn-pick').addEventListener('click', () => {
  pickingLevel = true;
  $('btn-pick').textContent = 'Klikk i kartet …';
  map.getContainer().style.cursor = 'crosshair';
});

map.on('click', async (e) => {
  if (!pickingLevel) return;
  pickingLevel = false;
  map.getContainer().style.cursor = '';
  $('btn-pick').textContent = 'Hent høyde ved klikk i kart';
  try {
    const r = await MB.api(`/api/hoyde?lon=${e.latlng.lng}&lat=${e.latlng.lat}`);
    const d = await r.json();
    if (d.z === null || d.z === undefined) {
      status('Ingen høydedata i dette punktet.', 'warn');
    } else {
      $('fixed-level').value = d.z.toFixed(1);
      status(`Terrenghøyde i punktet: ${fmt(d.z, 1)} moh.`, 'info');
    }
  } catch {
    status('Kunne ikke hente høyde.', 'error');
  }
});

function status(msg, kind = 'info') {
  const el = $('status');
  el.className = 'status ' + kind;
  el.textContent = msg;
  el.hidden = false;
}

/* ---------------------------------------------------------- calculate */

$('btn-calc').addEventListener('click', async () => {
  if (!state.polygon) return;

  const ring = state.polygon.getLatLngs()[0].map(p => [p.lng, p.lat]);
  const mode = document.querySelector('input[name=mode]:checked').value;

  const body = {
    polygon: ring,
    mode,
    fixed_level: mode === 'fast' ? Number($('fixed-level').value) : null,
    soil_depth: Number($('soil-depth').value),
    swell_soil: Number($('swell-soil').value) / 100,
    swell_rock: Number($('swell-rock').value) / 100,
    shrinkage: Number($('shrinkage').value) / 100,
    truck_capacity: Number($('truck').value),
    resolution: $('resolution').value ? Number($('resolution').value) : null,
  };

  $('btn-calc').disabled = true;
  $('btn-calc').textContent = 'Beregner …';
  status('Henter høydedata fra Kartverket og beregner volum …', 'info');

  try {
    const r = await MB.api('/api/beregn', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || 'Beregningen feilet.');
    state.result = data;
    renderResult(data);
    $('status').hidden = true;
  } catch (err) {
    status(err.message, 'error');
    $('results').hidden = true;
  } finally {
    $('btn-calc').disabled = false;
    $('btn-calc').textContent = 'Beregn masser';
  }
});

/* ------------------------------------------------------------ results */

function renderResult(d) {
  // --- map overlays
  if (cutfillLayer) map.removeLayer(cutfillLayer);
  if (hillshadeLayer) map.removeLayer(hillshadeLayer);

  /* MB.url, not MB.api: these are consumed by <img src> and <a download>,
   * neither of which can carry an Authorization header. The job id is the
   * credential instead - see the note at the top of storage.py. */
  hillshadeLayer = L.imageOverlay(MB.url(d.overlay.hillshade_url), d.overlay.hillshade_bounds,
    { opacity: 0.55, interactive: false });
  cutfillLayer = L.imageOverlay(MB.url(d.overlay.cutfill_url), d.overlay.bounds,
    { opacity: $('opacity').value / 100, interactive: false });

  if ($('toggle-shade').checked) hillshadeLayer.addTo(map);
  cutfillLayer.addTo(map);
  state.polygon.bringToFront();

  $('legend').hidden = false;
  $('legend-min').textContent = '−' + fmt(d.overlay.vmax, 1) + ' m';
  $('legend-max').textContent = '+' + fmt(d.overlay.vmax, 1) + ' m';

  // --- headline
  const b = d.balance;
  const net = b.net_bank_m3;
  const exporting = net > 0.5;
  const importing = net < -0.5;
  const cls = exporting ? 'export' : importing ? 'import' : 'neutral';
  const loose = exporting ? b.export_loose_m3 : b.import_loose_m3;
  const word = exporting ? 'ut av området' : importing ? 'inn til området' : 'i balanse';

  const headline = exporting || importing
    ? `<div class="big ${cls}">${vol(Math.abs(net))}<span class="unit">m³ fast</span></div>
       <div class="sub">masseoverskudd ${word} · tilsvarer <strong>${vol(loose)} m³ løst</strong>
       (~${fmt(b.truck_loads, 0)} lastebillass à ${fmt(d.params.truck_capacity, 0)} m³)</div>`
    : `<div class="big neutral">Massebalanse<span class="unit"></span></div>
       <div class="sub">skjæring og fylling går opp i opp – ingen transport inn eller ut</div>`;

  const cov = d.coverage < 0.995
    ? `<div class="status warn" style="margin:12px 0 0">Kartverket har høydedata for
       ${fmt(d.coverage * 100, 1)} % av flaten. Volumene gjelder kun det dekkede arealet.</div>`
    : '';

  const modeName = { balansert: 'Balansert', laveste: 'Laveste punkt', fast: 'Fast kote' }[d.mode];

  $('results').innerHTML = `
    <h2>Resultat</h2>
    <div class="headline">${headline}</div>
    ${cov}

    <div class="kpis">
      <div class="kpi"><div class="k">Ferdig planum</div>
        <div class="v">${fmt(d.level, 2)} <span class="u">moh</span></div></div>
      <div class="kpi"><div class="k">Areal</div>
        <div class="v">${fmt(d.area_m2 / 1000, 1)} <span class="u">daa</span></div></div>
      <div class="kpi cut"><div class="k">Skjæring</div>
        <div class="v">${vol(d.cut.total_bank_m3)} <span class="u">m³</span></div></div>
      <div class="kpi fill"><div class="k">Fylling</div>
        <div class="v">${vol(d.fill.void_m3)} <span class="u">m³</span></div></div>
    </div>

    <div class="res-block">
      <h3>Masser i skjæring</h3>
      <table class="tbl">
        <tr><th>Materiale</th><th>Fast m³</th><th>Løst m³</th></tr>
        <tr class="sub"><td>Løsmasse <span class="dim">(0–${fmt(d.params.soil_depth, 1)} m)</span></td>
          <td>${vol(d.cut.soil_bank_m3)}</td>
          <td>${vol(d.cut.soil_bank_m3 * (1 + d.params.swell_soil))}</td></tr>
        <tr class="sub"><td>Fjell <span class="dim">(dypere enn ${fmt(d.params.soil_depth, 1)} m)</span></td>
          <td>${vol(d.cut.rock_bank_m3)}</td>
          <td>${vol(d.cut.rock_bank_m3 * (1 + d.params.swell_rock))}</td></tr>
        <tr class="total"><td>Sum skjæring</td>
          <td>${vol(d.cut.total_bank_m3)}</td>
          <td>${vol(d.cut.soil_bank_m3 * (1 + d.params.swell_soil)
                    + d.cut.rock_bank_m3 * (1 + d.params.swell_rock))}</td></tr>
      </table>
    </div>

    <div class="res-block">
      <h3>Massebalanse</h3>
      <table class="tbl">
        <tr><td>Skjæring, fast volum</td><td>${vol(d.cut.total_bank_m3)} m³</td></tr>
        <tr><td>Fylling, geometrisk volum</td><td>${vol(d.fill.void_m3)} m³</td></tr>
        <tr><td>Massebehov fylling <span class="dim">inkl. ${fmt(d.params.shrinkage * 100, 0)} % svinn</span></td>
          <td>${vol(d.fill.bank_needed_m3)} m³</td></tr>
        <tr class="total"><td>${exporting ? 'Overskudd' : importing ? 'Underskudd' : 'Massebalanse'} (fast)</td>
          <td>${vol(Math.abs(net))} m³</td></tr>
      </table>
    </div>

    <div class="res-block">
      <h3>Terreng og geometri</h3>
      <table class="tbl">
        <tr><td>Modus</td><td>${modeName}</td></tr>
        <tr><td>Terreng lavest / høyest</td>
          <td>${fmt(d.terrain.min, 1)} – ${fmt(d.terrain.max, 1)} moh</td></tr>
        <tr><td>Middelhøyde</td><td>${fmt(d.terrain.mean, 2)} moh</td></tr>
        <tr><td>Maks skjæringsdybde</td><td>${fmt(d.cut.max_depth_m, 2)} m</td></tr>
        <tr><td>Maks fyllingshøyde</td><td>${fmt(d.fill.max_depth_m, 2)} m</td></tr>
        <tr><td>Skjæringsareal / fyllingsareal</td>
          <td>${fmt(d.cut.area_m2 / 1000, 1)} / ${fmt(d.fill.area_m2 / 1000, 1)} daa</td></tr>
        <tr><td>Rutenett</td><td>${fmt(d.resolution_m, 2)} m · ${fmt(d.cells, 0)} celler</td></tr>
      </table>
    </div>

    <div class="res-block">
      <h3>Netto masse mot kotehøyde</h3>
      ${sensitivityChart(d)}
      <p class="hint">Kurven viser netto masseoverskudd for ulike planumshøyder.
      Nullpunktet er den balanserte kotehøyden, ${fmt(d.balanced_level, 2)} moh.</p>
    </div>

    <div class="res-block">
      <h3>Eksport</h3>
      <div class="dl-row">
        <a href="${MB.url(d.downloads.csv)}" download>Resultat (CSV)</a>
        <a href="${MB.url(d.downloads.geotiff)}" download>Dybdekart (GeoTIFF)</a>
      </div>
    </div>

    <div class="caveat">
      <strong>Forbehold.</strong> Beregningen bygger på Kartverkets terrengmodell og gir
      geometrisk volum mellom dagens terreng og et horisontalt planum. Den sier ingenting om
      hva massene faktisk består av – fordelingen løsmasse/fjell er ditt anslag, ikke måledata.
      Grunnundersøkelse er nødvendig før prising. Modellen tar heller ikke hensyn til
      skråningsutslag, byggegroper, fall for drenering, vegetasjonsavtaking eller eksisterende
      konstruksjoner.
    </div>
  `;
  $('results').hidden = false;
  $('results').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* Small hand-rolled SVG chart - net volume as a function of target level. */
function sensitivityChart(d) {
  const pts = d.sensitivity;
  if (!pts || pts.length < 2) return '';

  const W = 340, H = 150, PAD_L = 46, PAD_R = 8, PAD_T = 10, PAD_B = 26;
  const levels = pts.map(p => p.level);
  const nets = pts.map(p => p.net_m3);
  const xMin = Math.min(...levels), xMax = Math.max(...levels);
  const yMax = Math.max(...nets.map(Math.abs)) || 1;

  const sx = v => PAD_L + (v - xMin) / (xMax - xMin) * (W - PAD_L - PAD_R);
  const sy = v => PAD_T + (1 - (v + yMax) / (2 * yMax)) * (H - PAD_T - PAD_B);

  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.level).toFixed(1)},${sy(p.net_m3).toFixed(1)}`).join('');
  const y0 = sy(0);
  const mx = sx(Math.min(Math.max(d.level, xMin), xMax));

  const short = v => {
    const a = Math.abs(v);
    if (a >= 1e6) return fmt(v / 1e6, 1) + ' mill';
    if (a >= 1e3) return fmt(v / 1e3, 0) + ' k';
    return fmt(v, 0);
  };

  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="axis" x1="${PAD_L}" y1="${PAD_T}" x2="${PAD_L}" y2="${H - PAD_B}"/>
    <line class="zero" x1="${PAD_L}" y1="${y0}" x2="${W - PAD_R}" y2="${y0}"/>
    <path class="curve" d="${path}"/>
    <line class="marker" x1="${mx}" y1="${PAD_T}" x2="${mx}" y2="${H - PAD_B}"/>
    <circle cx="${mx}" cy="${y0}" r="3.5" fill="#1a1d21"/>
    <text x="${PAD_L - 5}" y="${PAD_T + 8}" text-anchor="end">+${short(yMax)}</text>
    <text x="${PAD_L - 5}" y="${y0 + 3}" text-anchor="end">0</text>
    <text x="${PAD_L - 5}" y="${H - PAD_B}" text-anchor="end">−${short(yMax)}</text>
    <text x="${PAD_L}" y="${H - 8}" text-anchor="start">${fmt(xMin, 0)} moh</text>
    <text x="${W - PAD_R}" y="${H - 8}" text-anchor="end">${fmt(xMax, 0)} moh</text>
  </svg>`;
}

/* ------------------------------------------------------- legend controls */

$('opacity').addEventListener('input', (e) => {
  if (cutfillLayer) cutfillLayer.setOpacity(e.target.value / 100);
});
$('toggle-shade').addEventListener('change', (e) => {
  if (!hillshadeLayer) return;
  if (e.target.checked) {
    hillshadeLayer.addTo(map);
    if (cutfillLayer) cutfillLayer.bringToFront();
  } else {
    map.removeLayer(hillshadeLayer);
  }
});
$('legend-close').addEventListener('click', () => { $('legend').hidden = true; });

/* ---------------------------------------------------------------- account */

/* auth.js fires this once membership is confirmed. Until then the gate covers
 * the app, so there is nothing to show. */
document.addEventListener('mb:ready', (e) => {
  const me = e.detail;

  /* Local single-user mode has no accounts, roles or invitations, so the whole
   * chip would be noise - and "Logg ut" would do nothing. Leave the UI exactly
   * as it was before access control existed. */
  if (me.local_single_user) return;

  $('account').hidden = false;
  $('account-email').textContent = me.email;
  if (me.role === 'superadmin') {
    $('account-badge').hidden = false;
    $('account-admin').hidden = false;
  }
});

$('account-signout').addEventListener('click', () => MB.signOut());
