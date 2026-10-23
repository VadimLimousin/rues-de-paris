(function () {
  "use strict";

  // Must mirror scripts/fetch_streets.py exactly.
  var TYPE_WORDS = new Set([
    "rue", "avenue", "boulevard", "place", "quai", "impasse", "allee",
    "passage", "pont", "square", "cours", "villa", "cite", "sentier",
    "chemin", "esplanade", "promenade", "parvis", "cour", "hameau",
    "viaduc", "degre", "galerie", "terrasse", "voie",
  ]);
  var LINK_WORDS = new Set(["de", "du", "des", "la", "le", "les", "l", "d", "au", "aux", "un", "une"]);

  function foldAccents(s) {
    return s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
  }

  function normalize(text) {
    text = text.replace(/œ/g, "oe").replace(/Œ/g, "OE").replace(/æ/g, "ae").replace(/Æ/g, "AE");
    text = foldAccents(text).toLowerCase();
    text = text.replace(/[-'’.,]/g, " ");
    text = text.replace(/\s+/g, " ").trim();
    return text;
  }

  function stripPrefixTokens(tokens) {
    tokens = tokens.slice();
    if (tokens[0] === "rond" && tokens[1] === "point") {
      tokens = tokens.slice(2);
    } else if (tokens.length && TYPE_WORDS.has(tokens[0])) {
      tokens = tokens.slice(1);
    }
    while (tokens.length && LINK_WORDS.has(tokens[0])) {
      tokens = tokens.slice(1);
    }
    return tokens;
  }

  function computeCoreKey(input) {
    var n = normalize(input);
    var tokens = n ? n.split(" ") : [];
    return stripPrefixTokens(tokens).join(" ");
  }

  function computeFullKey(input) {
    return normalize(input);
  }

  // Arrondissements 1-14 use the official RATP Métro line colors
  // (same number = same line). 15-20 are proposed colors chosen to
  // avoid duplicating any of the 14, or each other.
  var PALETTE = [
    "#FFCE00", "#0064B0", "#9F9825", "#C04191", "#F28E42",
    "#83C491", "#F3A4BA", "#CEADD2", "#D5C900", "#E3B32A",
    "#8D5E2A", "#00814F", "#98D4E2", "#662483", "#C1272D",
    "#00A88F", "#4C5FAE", "#5C7A99", "#E0704A", "#7A9B76",
  ];
  var FALLBACK_COLOR = "#777777";

  var TIER_LABELS = {
    1: "Monuments",
    2: "Connues",
    3: "Averties",
    4: "Carte complète",
  };
  var tierBaseLabels = {}; // set once by setupTierOptions; applyWonTierStyling re-renders from this

  // ---------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------

  var ALL_STREETS = window.PARIS_STREETS || [];
  var VIEWBOX = window.PARIS_VIEWBOX || { minX: 0, minY: 0, width: 1000, height: 1000 };

  var state = {
    tier: 2,
    active: [],
    found: new Set(),
    coreIndex: new Map(),
    fullIndex: new Map(),
    pathEls: new Map(),
    districts: new Map(), // arrondissement -> { total, found, fillEl, countEl, rowEl }
    view: null, // current SVG viewBox {x, y, w, h} — see resetView()
    ended: false,
  };

  var el = {
    tierSelect: document.getElementById("tierSelect"),
    counter: document.getElementById("counter"),
    giveupBtn: document.getElementById("giveupBtn"),
    mapWrap: document.getElementById("map-wrap"),
    map: document.getElementById("map"),
    zoomInBtn: document.getElementById("zoomInBtn"),
    zoomOutBtn: document.getElementById("zoomOutBtn"),
    resetViewBtn: document.getElementById("resetViewBtn"),
    input: document.getElementById("guessInput"),
    districtGrid: document.getElementById("districtGrid"),
    foundList: document.getElementById("foundList"),
    endPanel: document.getElementById("endPanel"),
    endTitle: document.getElementById("endTitle"),
    endSubtitle: document.getElementById("endSubtitle"),
    missedList: document.getElementById("missedList"),
    closeEndBtn: document.getElementById("closeEndBtn"),
    helpBtn: document.getElementById("helpBtn"),
    introPanel: document.getElementById("introPanel"),
    closeIntroBtn: document.getElementById("closeIntroBtn"),
  };

  var SVG_NS = "http://www.w3.org/2000/svg";

  function colorFor(arr) {
    if (!arr || arr < 1) return FALLBACK_COLOR;
    return PALETTE[(arr - 1) % PALETTE.length];
  }

  // ---------------------------------------------------------------------
  // Setup: tier option labels with real counts
  // ---------------------------------------------------------------------

  function setupTierOptions() {
    var counts = { 1: 0, 2: 0, 3: 0, 4: 0 };
    ALL_STREETS.forEach(function (s) {
      if (s.synthetic) return; // landmark points (e.g. Tour Eiffel) aren't classic-mode streets
      counts[s.tier] = (counts[s.tier] || 0) + 1;
    });
    var cumulative = 0;
    [1, 2, 3, 4].forEach(function (t) {
      cumulative += counts[t] || 0;
      tierBaseLabels[t] = TIER_LABELS[t] + " (" + cumulative.toLocaleString("fr-FR") + ")";
    });
    applyWonTierStyling();
  }

  // ---------------------------------------------------------------------
  // Map rendering
  // ---------------------------------------------------------------------

  function renderBackgroundLayers() {
    var parks = window.PARIS_PARKS || [];
    parks.forEach(function (d) {
      if (!d) return;
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "map-park");
      el.map.appendChild(path);
    });

    var water = window.PARIS_WATER || [];
    water.forEach(function (d) {
      if (!d) return;
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", d);
      path.setAttribute("class", "map-water");
      el.map.appendChild(path);
    });

    if (window.PARIS_BOUNDARY) {
      var boundary = document.createElementNS(SVG_NS, "path");
      boundary.setAttribute("d", window.PARIS_BOUNDARY);
      boundary.setAttribute("class", "map-boundary");
      el.map.appendChild(boundary);
    }
  }

  var VIEW_PAD = 20;
  var BASE_VIEW = {
    x: VIEWBOX.minX - VIEW_PAD,
    y: VIEWBOX.minY - VIEW_PAD,
    w: VIEWBOX.width + VIEW_PAD * 2,
    h: VIEWBOX.height + VIEW_PAD * 2,
  };
  var MAX_ZOOM = 25; // how far past the full-map view the user can zoom in
  var MIN_VIEW_W = BASE_VIEW.w / MAX_ZOOM;

  function createStreetPathEl(street, className) {
    var path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", street.pathD);
    path.setAttribute("class", className);
    path.dataset.id = street.id;
    return path;
  }

  function stylePathAsFound(pathEl, street) {
    pathEl.classList.remove("unfound");
    pathEl.classList.add("found");
    pathEl.style.stroke = colorFor(street.arrondissement);
  }

  function renderMap() {
    while (el.map.firstChild) el.map.removeChild(el.map.firstChild);
    el.map.setAttribute("preserveAspectRatio", "xMidYMid meet");

    renderBackgroundLayers();

    state.pathEls.clear();
    state.active.forEach(function (street) {
      var path = createStreetPathEl(street, "street unfound");
      el.map.appendChild(path);
      state.pathEls.set(street.id, path);
      // Idempotent w.r.t. progress, so re-entering from Challenge mode
      // (shared <svg>) doesn't wipe a classic game already in progress.
      if (state.found.has(street.id)) stylePathAsFound(path, street);
    });

    resetView();
  }

  // ---------------------------------------------------------------------
  // Pan & zoom (mouse wheel, drag, touch pinch/drag, and the on-screen
  // +/-/reset buttons) — plain SVG viewBox manipulation, no library.
  // ---------------------------------------------------------------------

  function applyViewBox() {
    var v = state.view;
    el.map.setAttribute("viewBox", v.x + " " + v.y + " " + v.w + " " + v.h);
    // No-op in classic mode (no #placeMarkersLayer exists yet); keeps
    // challenge-mode place markers a constant apparent size while
    // zooming instead of ballooning with the shrinking viewBox.
    updatePlaceMarkerVisuals();
  }

  function resetView() {
    state.view = { x: BASE_VIEW.x, y: BASE_VIEW.y, w: BASE_VIEW.w, h: BASE_VIEW.h };
    applyViewBox();
  }

  function clampView() {
    var v = state.view;
    v.w = Math.min(Math.max(v.w, MIN_VIEW_W), BASE_VIEW.w);
    v.h = v.w * (BASE_VIEW.h / BASE_VIEW.w);
    // Allow panning a bit past the map's own edges (roomy but not
    // infinite), scaling the slack with the base map size.
    var slackX = BASE_VIEW.w * 0.4;
    var slackY = BASE_VIEW.h * 0.4;
    var minX = BASE_VIEW.x - slackX;
    var maxX = BASE_VIEW.x + BASE_VIEW.w + slackX - v.w;
    var minY = BASE_VIEW.y - slackY;
    var maxY = BASE_VIEW.y + BASE_VIEW.h + slackY - v.h;
    v.x = Math.min(Math.max(v.x, Math.min(minX, maxX)), Math.max(minX, maxX));
    v.y = Math.min(Math.max(v.y, Math.min(minY, maxY)), Math.max(minY, maxY));
  }

  // Convert a pointer's pixel position (viewport coords) to SVG
  // user-space coordinates under the current viewBox.
  function pointerToSvg(clientX, clientY) {
    var rect = el.map.getBoundingClientRect();
    return {
      x: state.view.x + ((clientX - rect.left) / rect.width) * state.view.w,
      y: state.view.y + ((clientY - rect.top) / rect.height) * state.view.h,
    };
  }

  function zoomAt(factor, svgX, svgY) {
    var v = state.view;
    var newW = v.w * factor;
    newW = Math.min(Math.max(newW, MIN_VIEW_W), BASE_VIEW.w);
    var ratio = newW / v.w;
    v.x = svgX - (svgX - v.x) * ratio;
    v.y = svgY - (svgY - v.y) * ratio;
    v.w = newW;
    v.h = v.w * (BASE_VIEW.h / BASE_VIEW.w);
    clampView();
    applyViewBox();
  }

  function zoomAtCenter(factor) {
    var v = state.view;
    zoomAt(factor, v.x + v.w / 2, v.y + v.h / 2);
  }

  el.mapWrap.addEventListener(
    "wheel",
    function (e) {
      e.preventDefault();
      var p = pointerToSvg(e.clientX, e.clientY);
      zoomAt(e.deltaY < 0 ? 0.88 : 1 / 0.88, p.x, p.y);
    },
    { passive: false }
  );

  el.zoomInBtn.addEventListener("click", function () { zoomAtCenter(0.7); });
  el.zoomOutBtn.addEventListener("click", function () { zoomAtCenter(1 / 0.7); });
  el.resetViewBtn.addEventListener("click", resetView);

  // Mouse drag to pan.
  var drag = null;
  el.mapWrap.addEventListener("mousedown", function (e) {
    if (e.button !== 0) return;
    drag = { lastX: e.clientX, lastY: e.clientY };
    el.mapWrap.classList.add("dragging");
  });
  window.addEventListener("mousemove", function (e) {
    if (!drag) return;
    var rect = el.map.getBoundingClientRect();
    var v = state.view;
    v.x -= ((e.clientX - drag.lastX) / rect.width) * v.w;
    v.y -= ((e.clientY - drag.lastY) / rect.height) * v.h;
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;
    clampView();
    applyViewBox();
  });
  window.addEventListener("mouseup", function () {
    drag = null;
    el.mapWrap.classList.remove("dragging");
  });

  // Touch: one finger pans, two fingers pinch-zoom.
  function touchDist(touches) {
    var dx = touches[0].clientX - touches[1].clientX;
    var dy = touches[0].clientY - touches[1].clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }
  function touchMidpoint(touches) {
    return {
      x: (touches[0].clientX + touches[1].clientX) / 2,
      y: (touches[0].clientY + touches[1].clientY) / 2,
    };
  }

  var touchState = null;
  el.mapWrap.addEventListener(
    "touchstart",
    function (e) {
      if (e.touches.length === 1) {
        touchState = { mode: "pan", lastX: e.touches[0].clientX, lastY: e.touches[0].clientY };
      } else if (e.touches.length === 2) {
        touchState = { mode: "pinch", startDist: touchDist(e.touches) };
      }
    },
    { passive: true }
  );
  el.mapWrap.addEventListener(
    "touchmove",
    function (e) {
      if (!touchState) return;
      e.preventDefault();
      if (touchState.mode === "pan" && e.touches.length === 1) {
        var rect = el.map.getBoundingClientRect();
        var v = state.view;
        var t = e.touches[0];
        v.x -= ((t.clientX - touchState.lastX) / rect.width) * v.w;
        v.y -= ((t.clientY - touchState.lastY) / rect.height) * v.h;
        touchState.lastX = t.clientX;
        touchState.lastY = t.clientY;
        clampView();
        applyViewBox();
      } else if (touchState.mode === "pinch" && e.touches.length === 2) {
        var dist = touchDist(e.touches);
        var mid = touchMidpoint(e.touches);
        var p = pointerToSvg(mid.x, mid.y);
        zoomAt(touchState.startDist / dist, p.x, p.y);
        touchState.startDist = dist;
      }
    },
    { passive: false }
  );
  el.mapWrap.addEventListener("touchend", function (e) {
    if (e.touches.length === 0) touchState = null;
  });

  // ---------------------------------------------------------------------
  // Per-arrondissement progress bars
  // ---------------------------------------------------------------------

  function renderDistrictProgress() {
    state.districts.clear();
    el.districtGrid.innerHTML = "";

    var totals = new Map();
    state.active.forEach(function (street) {
      var arr = street.arrondissement;
      if (!arr) return;
      totals.set(arr, (totals.get(arr) || 0) + 1);
    });

    for (var arr = 1; arr <= 20; arr++) {
      var total = totals.get(arr) || 0;
      var cell = document.createElement("div");
      cell.className = "district-cell" + (total === 0 ? " empty" : "");
      cell.style.setProperty("--fill-color", colorFor(arr));
      cell.style.setProperty("--pct", "0%");

      var number = document.createElement("span");
      number.className = "arr-number";
      number.textContent = String(arr);

      var count = document.createElement("span");
      count.className = "arr-count";
      count.textContent = total === 0 ? "—" : "0/" + total;

      cell.appendChild(number);
      cell.appendChild(count);
      el.districtGrid.appendChild(cell);

      state.districts.set(arr, { total: total, found: 0, countEl: count, cellEl: cell });
    }
  }

  function bumpDistrictProgress(arr) {
    var d = state.districts.get(arr);
    if (!d || d.total === 0) return;
    d.found += 1;
    d.cellEl.style.setProperty("--pct", Math.round((d.found / d.total) * 100) + "%");
    d.countEl.textContent = d.found + "/" + d.total;
    if (d.found === d.total) d.cellEl.classList.add("complete");
  }

  // ---------------------------------------------------------------------
  // Indexes (built fresh per active tier, so ambiguity is judged within
  // the currently playable set, not the full 3,435-street corpus)
  // ---------------------------------------------------------------------

  // Pure: builds a {coreIndex, fullIndex} pair from any street list.
  // Classic mode indexes state.active (tier-filtered); challenge mode
  // indexes the full corpus (see challenge section below) — a valid
  // route can legitimately pass through an obscure tier-4 street.
  function buildIndexesFor(streetList) {
    var coreIndex = new Map();
    var fullIndex = new Map();
    streetList.forEach(function (street) {
      if (!coreIndex.has(street.coreKey)) coreIndex.set(street.coreKey, []);
      coreIndex.get(street.coreKey).push(street);
      fullIndex.set(street.fullKey, street);
    });
    // Within an ambiguous group (several streets sharing one lenient
    // core name, e.g. "Pont/Avenue/Villa des Arts" all reducing to
    // "arts"), claim the most famous one first: sort by tier so a bare
    // "arts" resolves to the most iconic unfound match, and repeating
    // the same guess moves on to the next one.
    coreIndex.forEach(function (bucket) {
      if (bucket.length > 1) bucket.sort(function (a, b) { return a.tier - b.tier; });
    });
    return { coreIndex: coreIndex, fullIndex: fullIndex };
  }

  function buildIndexes() {
    var idx = buildIndexesFor(state.active);
    state.coreIndex = idx.coreIndex;
    state.fullIndex = idx.fullIndex;
  }

  // Returns the list of streets a guess should mark as found, against
  // the given index pair (classic passes state.*, challenge mode passes
  // its own). A bare ambiguous core name (e.g. "arts", matching "Pont/
  // Villa/Impasse/Passage/Avenue des Arts") validates every street in
  // the group at once; typing a specific full name (with its type word)
  // always targets just that one street.
  function resolveGuess(rawInput, fullIndex, coreIndex) {
    var fullKey = computeFullKey(rawInput);
    if (fullIndex.has(fullKey)) return [fullIndex.get(fullKey)];
    var coreKey = computeCoreKey(rawInput);
    if (!coreKey) return [];
    var bucket = coreIndex.get(coreKey);
    if (!bucket || bucket.length === 0) return [];
    return bucket;
  }

  // ---------------------------------------------------------------------
  // Game flow
  // ---------------------------------------------------------------------

  function updateCounter() {
    el.counter.textContent = state.found.size + " / " + state.active.length;
  }

  function addFoundListItem(street) {
    var row = document.createElement("div");
    row.className = "found-item";
    var swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = colorFor(street.arrondissement);
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = street.displayName;
    var arr = document.createElement("span");
    arr.className = "arr";
    arr.textContent = street.arrondissement ? street.arrondissement + "e" : "";
    row.appendChild(swatch);
    row.appendChild(name);
    row.appendChild(arr);
    el.foundList.insertBefore(row, el.foundList.firstChild);
  }

  function pulsePath(pathEl) {
    pathEl.classList.remove("pulse");
    void pathEl.offsetWidth; // restart animation
    pathEl.classList.add("pulse");
  }

  function handleMatch(street) {
    var pathEl = state.pathEls.get(street.id);
    if (state.found.has(street.id)) {
      // Already found: a no-op that leaves the input alone. Without
      // this, an already-picked street (e.g. "Rue Le Brun") could
      // re-trigger mid-keystroke while typing a longer, unrelated name
      // that happens to pass through the same letters (typing toward
      // "Boulevard Brune" hits the exact core "brun" along the way),
      // wiping out what was being typed.
      if (pathEl) pulsePath(pathEl);
      return;
    }
    state.found.add(street.id);
    if (pathEl) {
      stylePathAsFound(pathEl, street);
      pulsePath(pathEl);
    }
    addFoundListItem(street);
    updateCounter();
    bumpDistrictProgress(street.arrondissement);
    el.input.value = "";
    saveClassicProgress();

    if (state.found.size === state.active.length) {
      endGame(true);
    }
  }

  function shakeInputEl(inputEl) {
    inputEl.classList.remove("shake");
    void inputEl.offsetWidth;
    inputEl.classList.add("shake");
  }

  // ---- Classic-mode persistence (localStorage) ----
  //
  // An in-progress game survives a reload; giving up clears it instead,
  // so a reload after giving up starts fresh rather than resuming a lost
  // game. Winning a tier marks it (and every lower tier, since each is a
  // strict subset of the one above it) permanently "won", independent of
  // the resumable in-progress record.

  var CLASSIC_PROGRESS_KEY = "r2p.classic.progress";
  var CLASSIC_WON_TIERS_KEY = "r2p.classic.wonTiers";

  function loadClassicProgress() {
    try {
      var raw = localStorage.getItem(CLASSIC_PROGRESS_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function saveClassicProgress() {
    try {
      localStorage.setItem(CLASSIC_PROGRESS_KEY, JSON.stringify({
        tier: state.tier,
        foundIds: Array.from(state.found),
      }));
    } catch (e) { /* no persistence available; game still works, just won't resume */ }
  }

  function clearClassicProgress() {
    try {
      localStorage.removeItem(CLASSIC_PROGRESS_KEY);
    } catch (e) { /* ignore */ }
  }

  function loadWonTiers() {
    try {
      var raw = localStorage.getItem(CLASSIC_WON_TIERS_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch (e) {
      return new Set();
    }
  }

  function saveWonTiers(wonTiers) {
    try {
      localStorage.setItem(CLASSIC_WON_TIERS_KEY, JSON.stringify(Array.from(wonTiers)));
    } catch (e) { /* ignore */ }
  }

  // Struck through via a combining character baked into the text itself
  // (not CSS text-decoration) — <option> styling is unreliable on Safari
  // and ignored entirely by mobile OS pickers (iOS/Android render their
  // own native UI), so this renders identically everywhere. Also avoids
  // a checkmark prefix, which would be redundant with (and easy to
  // confuse for) the OS's own checkmark next to the currently-selected
  // option in the open dropdown.
  function strikethrough(text) {
    return text.split("").map(function (ch) { return ch + "\u0336"; }).join("");
  }

  function applyWonTierStyling() {
    var wonTiers = loadWonTiers();
    [1, 2, 3, 4].forEach(function (t) {
      var opt = el.tierSelect.querySelector('option[value="' + t + '"]');
      if (!opt || !tierBaseLabels[t]) return;
      var won = wonTiers.has(t);
      opt.textContent = won ? strikethrough(tierBaseLabels[t]) : tierBaseLabels[t];
      opt.classList.toggle("won", won);
    });
  }

  function markTierWon(tier) {
    var wonTiers = loadWonTiers();
    for (var t = 1; t <= tier; t++) wonTiers.add(t);
    saveWonTiers(wonTiers);
    applyWonTierStyling();
  }

  function endGame(won) {
    if (state.ended) return;
    state.ended = true;
    el.input.disabled = true;

    var missedByArr = new Map();
    state.active.forEach(function (street) {
      if (state.found.has(street.id)) return;
      var pathEl = state.pathEls.get(street.id);
      if (pathEl) {
        pathEl.classList.remove("unfound");
        pathEl.classList.add("revealed");
      }
      var key = street.arrondissement || 0;
      if (!missedByArr.has(key)) missedByArr.set(key, []);
      missedByArr.get(key).push(street.displayName);
    });

    el.missedList.innerHTML = "";
    if (won) {
      el.endTitle.textContent = "Bravo !";
      el.endSubtitle.textContent = "Toutes les rues ont été trouvées.";
      markTierWon(state.tier);
    } else {
      el.endTitle.textContent = "Partie terminée";
      el.endSubtitle.textContent =
        state.found.size + " / " + state.active.length + " rues trouvées.";
      var arrNums = Array.from(missedByArr.keys()).sort(function (a, b) { return a - b; });
      arrNums.forEach(function (arrNum) {
        var h3 = document.createElement("h3");
        h3.textContent = arrNum ? arrNum + "e arrondissement" : "Non classé";
        el.missedList.appendChild(h3);
        var p = document.createElement("p");
        p.style.margin = "0";
        p.textContent = missedByArr.get(arrNum).sort().join(", ");
        el.missedList.appendChild(p);
      });
      clearClassicProgress();
    }
    el.endPanel.hidden = false;
  }

  // `foundIds`, when given, resumes a saved game instead of starting
  // empty (see the boot call at the bottom of this file).
  function resetGame(tier, foundIds) {
    state.tier = tier;
    state.active = ALL_STREETS.filter(function (s) { return s.tier <= tier && !s.synthetic; });
    state.found = new Set(foundIds || []);
    state.ended = false;
    buildIndexes();
    renderMap();
    renderDistrictProgress();
    el.foundList.innerHTML = "";
    var byId = new Map(state.active.map(function (s) { return [s.id, s]; }));
    state.found.forEach(function (id) {
      var street = byId.get(id);
      if (!street) return;
      bumpDistrictProgress(street.arrondissement);
      addFoundListItem(street);
    });
    updateCounter();
    el.endPanel.hidden = true;
    el.input.disabled = false;
    el.input.value = "";
    el.input.focus();
    saveClassicProgress();
    if (state.found.size > 0 && state.found.size === state.active.length) {
      endGame(true);
    }
  }

  // ---------------------------------------------------------------------
  // Events
  // ---------------------------------------------------------------------

  el.input.addEventListener("input", function () {
    if (state.ended) return;
    el.input.classList.remove("shake");
    var value = el.input.value;
    if (!value.trim()) return;
    var matches = resolveGuess(value, state.fullIndex, state.coreIndex);
    matches.forEach(handleMatch);
  });

  el.input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !state.ended) {
      var value = el.input.value;
      if (value.trim() && resolveGuess(value, state.fullIndex, state.coreIndex).length === 0) shakeInputEl(el.input);
    }
  });

  el.tierSelect.addEventListener("change", function () {
    var newTier = parseInt(el.tierSelect.value, 10);
    if (state.found.size > 0 && !state.ended) {
      var ok = window.confirm("Changer de difficulté réinitialise la partie en cours. Continuer ?");
      if (!ok) {
        el.tierSelect.value = String(state.tier);
        return;
      }
    }
    resetGame(newTier);
  });

  el.giveupBtn.addEventListener("click", function () {
    if (appMode === "challenge") {
      challengeGiveUp();
      return;
    }
    if (state.ended) return;
    var ok = window.confirm("Abandonner et révéler les rues manquantes ?");
    if (ok) endGame(false);
  });

  el.closeEndBtn.addEventListener("click", function () {
    el.endPanel.hidden = true;
  });

  // ---- First-visit "how to play" intro ----

  var INTRO_SEEN_KEY = "r2p.introSeen";

  function showIntro() {
    el.introPanel.hidden = false;
  }

  function closeIntro() {
    el.introPanel.hidden = true;
    try {
      localStorage.setItem(INTRO_SEEN_KEY, "1");
    } catch (e) { /* no persistence available; intro just reappears next visit */ }
  }

  el.helpBtn.addEventListener("click", showIntro);
  el.closeIntroBtn.addEventListener("click", closeIntro);

  // Clicking the dark backdrop (not the card itself) closes it too.
  el.introPanel.addEventListener("click", function (e) {
    if (e.target === el.introPanel) closeIntro();
  });

  // ---------------------------------------------------------------------
  // Daily Challenge mode
  //
  // Connect two well-known places by naming real, adjacent streets
  // between them. Lives entirely alongside classic mode: its own
  // challengeState, its own indexes (built over the FULL corpus — a
  // valid route can pass through an obscure tier-4 street), and it
  // shares the same physical <svg id="map"> element, re-rendered from
  // whichever mode's state is active (see setMode below).
  // ---------------------------------------------------------------------

  var ALL_PLACES = window.PARIS_PLACES || [];
  var ADJACENCY = window.PARIS_ADJACENCY || {};
  var STREETS_BY_ID = new Map(ALL_STREETS.map(function (s) { return [s.id, s]; }));

  var appMode = "classic";

  var elC = {
    challengeBtn: document.getElementById("challengeBtn"),
    tierField: document.getElementById("tierField"),
    counterStat: document.getElementById("counterStat"),
    challengeTimerStat: document.getElementById("challengeTimerStat"),
    challengeTimer: document.getElementById("challengeTimer"),
    challengeMapOptions: document.getElementById("challengeMapOptions"),
    showAllStreetsToggle: document.getElementById("showAllStreetsToggle"),
    sidebar: document.getElementById("sidebar"),
    challengeSidebar: document.getElementById("challenge-sidebar"),
    districtGrid: document.getElementById("districtGrid"),
    routeSwatchA: document.getElementById("routeSwatchA"),
    routePlaceA: document.getElementById("routePlaceA"),
    routeSwatchB: document.getElementById("routeSwatchB"),
    routePlaceB: document.getElementById("routePlaceB"),
    challengeStatus: document.getElementById("challengeStatus"),
    challengeInput: document.getElementById("challengeInput"),
    challengeFoundList: document.getElementById("challengeFoundList"),
    confettiLayer: document.getElementById("confettiLayer"),
  };

  var challengeState = {
    date: null,
    anchors: null,
    places: null,
    found: new Set(),
    order: [],
    fullIndex: new Map(),
    coreIndex: new Map(),
    pathEls: new Map(),
    ufParent: new Map(),
    ufRank: new Map(),
    showAllStreets: false,
    timerStart: null,
    timerInterval: null,
    elapsedMs: null,
    solved: false,
    ended: false,
    attemptStarted: false, // has beginChallengeAttempt/showAlreadySolvedStatus run for `date` yet
  };

  // ---- Daily seed ----

  function todayDateStr() {
    var d = new Date();
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  function hashString(s) {
    var hash = 5381;
    for (var i = 0; i < s.length; i++) {
      hash = ((hash * 33) ^ s.charCodeAt(i)) >>> 0;
    }
    return hash >>> 0;
  }

  function makeRng(seed) {
    var s = seed >>> 0;
    return function () {
      s = (Math.imul(s, 1103515245) + 12345) >>> 0;
      return s / 4294967296;
    };
  }

  function placeDistance(a, b) {
    var dx = a.x - b.x, dy = a.y - b.y;
    return Math.sqrt(dx * dx + dy * dy);
  }

  // Picks today's two places deterministically from the date string, so
  // every device landing on the same calendar date gets the same puzzle.
  // Rejects trivial pairs (same place/arrondissement, too close, or a
  // route that's absurdly short/long).
  function pickDailyPlaces(dateStr) {
    var n = ALL_PLACES.length;
    if (n < 2) return null;
    var rng = makeRng(hashString(dateStr));
    var cityDiagonal = Math.sqrt(VIEWBOX.width * VIEWBOX.width + VIEWBOX.height * VIEWBOX.height);
    var minDist = cityDiagonal * 0.35;

    for (var attempt = 0; attempt < 200; attempt++) {
      var i = Math.floor(rng() * n);
      var j = Math.floor(rng() * n);
      if (i === j) continue;
      var a = ALL_PLACES[i], b = ALL_PLACES[j];
      if (a.arrondissement === b.arrondissement) continue;
      if (placeDistance(a, b) < minDist) continue;
      var path = bfsPath(a.anchorStreetId, b.anchorStreetId);
      if (!path || path.length < 4 || path.length > 20) continue;
      return [a, b];
    }

    // Fallback (shouldn't normally trigger): the single farthest-apart
    // valid pair in the whole pool, deterministic rather than random.
    var best = null, bestDist = -1;
    for (var x = 0; x < n; x++) {
      for (var y = x + 1; y < n; y++) {
        var d = placeDistance(ALL_PLACES[x], ALL_PLACES[y]);
        if (d > bestDist) { bestDist = d; best = [ALL_PLACES[x], ALL_PLACES[y]]; }
      }
    }
    return best;
  }

  // ---- Union-Find (incremental connectivity while playing) ----

  function ufFind(parent, id) {
    if (!parent.has(id)) parent.set(id, id);
    var root = id;
    while (parent.get(root) !== root) root = parent.get(root);
    while (parent.get(id) !== root) {
      var next = parent.get(id);
      parent.set(id, root);
      id = next;
    }
    return root;
  }

  function ufUnion(parent, rank, a, b) {
    var ra = ufFind(parent, a), rb = ufFind(parent, b);
    if (ra === rb) return;
    var rka = rank.get(ra) || 0, rkb = rank.get(rb) || 0;
    if (rka < rkb) {
      parent.set(ra, rb);
    } else if (rka > rkb) {
      parent.set(rb, ra);
    } else {
      parent.set(rb, ra);
      rank.set(ra, rka + 1);
    }
  }

  // ---- BFS (daily-seed validation, give-up reveal, and — restricted to
  // an `allowed` node set — finding the actual working path within
  // whatever streets the player has found) ----

  function bfsPath(startId, endId, allowed) {
    if (startId === endId) return [startId];
    var visited = new Set([startId]);
    var parent = new Map();
    var queue = [startId];
    var head = 0;
    while (head < queue.length) {
      var cur = queue[head++];
      var neighbors = ADJACENCY[cur] || [];
      for (var k = 0; k < neighbors.length; k++) {
        var next = neighbors[k];
        if (allowed && !allowed.has(next)) continue;
        if (visited.has(next)) continue;
        visited.add(next);
        parent.set(next, cur);
        if (next === endId) {
          var path = [endId];
          var node = endId;
          while (parent.has(node)) { node = parent.get(node); path.push(node); }
          return path.reverse();
        }
        queue.push(next);
      }
    }
    return null;
  }

  // ---- localStorage persistence ----

  var SHOW_ALL_STREETS_KEY = "r2p.challenge.showAllStreets";

  function loadShowAllStreetsPref() {
    try {
      return localStorage.getItem(SHOW_ALL_STREETS_KEY) === "1";
    } catch (e) {
      return false;
    }
  }

  function saveShowAllStreetsPref(value) {
    try {
      localStorage.setItem(SHOW_ALL_STREETS_KEY, value ? "1" : "0");
    } catch (e) { /* localStorage unavailable — preference just won't persist */ }
  }

  function loadChallengeRecord(dateStr) {
    try {
      var raw = localStorage.getItem("r2p.challenge." + dateStr);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function saveChallengeRecord(dateStr, record) {
    try {
      localStorage.setItem("r2p.challenge." + dateStr, JSON.stringify(record));
    } catch (e) { /* no persistence available; feature degrades to always-fresh */ }
  }

  // Resets every day for free: it's just "is *today's* record solved",
  // and today's date-keyed record naturally doesn't exist yet on a new day.
  function isTodaysChallengeSolved() {
    var record = loadChallengeRecord(todayDateStr());
    return !!(record && record.solved);
  }

  function refreshChallengeBtnLabel() {
    var isChallenge = appMode === "challenge";
    var solved = isTodaysChallengeSolved();
    elC.challengeBtn.classList.toggle("solved", solved);
    elC.challengeBtn.textContent = isChallenge
      ? "Mode classique"
      : (solved ? "✓ Défi du jour" : "Défi du jour");
  }

  // ---- Timer ----

  function formatElapsed(ms) {
    var totalSeconds = Math.floor(ms / 1000);
    var m = Math.floor(totalSeconds / 60);
    var s = totalSeconds % 60;
    return (m < 10 ? "0" + m : m) + ":" + (s < 10 ? "0" + s : s);
  }

  function startChallengeTimer() {
    challengeState.timerStart = Date.now();
    elC.challengeTimer.textContent = "00:00";
    challengeState.timerInterval = setInterval(function () {
      elC.challengeTimer.textContent = formatElapsed(Date.now() - challengeState.timerStart);
    }, 250);
  }

  function stopChallengeTimer() {
    if (challengeState.timerInterval) clearInterval(challengeState.timerInterval);
    challengeState.timerInterval = null;
    challengeState.elapsedMs = challengeState.timerStart !== null ? Date.now() - challengeState.timerStart : 0;
    elC.challengeTimer.textContent = formatElapsed(challengeState.elapsedMs);
  }

  // ---- Rendering ----

  var MARKER_BASE_RADIUS = 7;
  var MARKER_BASE_FONT_SIZE = 16;
  // The marker's circle/text live in the same zoomable SVG user-space as
  // the streets, so without correction they'd balloon as the viewBox
  // shrinks while zooming in. Ratio 1 = the full base map view; below
  // MARKER_FADE_START the label starts fading (it's no longer needed —
  // you're zoomed in to read the streets around the point, not the
  // point's name), fully gone by MARKER_FADE_END.
  var MARKER_FADE_START_RATIO = 0.55;
  var MARKER_FADE_END_RATIO = 0.15;

  function markerLabelOpacity(ratio) {
    if (ratio >= MARKER_FADE_START_RATIO) return 1;
    if (ratio <= MARKER_FADE_END_RATIO) return 0;
    return (ratio - MARKER_FADE_END_RATIO) / (MARKER_FADE_START_RATIO - MARKER_FADE_END_RATIO);
  }

  function updatePlaceMarkerVisuals() {
    var g = document.getElementById("placeMarkersLayer");
    if (!g) return;
    var ratio = state.view.w / BASE_VIEW.w;
    var r = MARKER_BASE_RADIUS * ratio;
    var fontSize = MARKER_BASE_FONT_SIZE * ratio;
    var opacity = markerLabelOpacity(ratio);
    g.querySelectorAll(".place-marker").forEach(function (circle) {
      circle.setAttribute("r", r);
    });
    g.querySelectorAll(".place-marker-label").forEach(function (text) {
      text.setAttribute("font-size", fontSize);
      text.style.opacity = opacity;
    });
  }

  function renderPlaceMarkers() {
    var g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("id", "placeMarkersLayer");
    challengeState.places.forEach(function (place) {
      var circle = document.createElementNS(SVG_NS, "circle");
      circle.setAttribute("cx", place.x);
      circle.setAttribute("cy", place.y);
      circle.setAttribute("r", MARKER_BASE_RADIUS);
      circle.setAttribute("class", "place-marker");
      circle.style.fill = colorFor(place.arrondissement);
      var text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", place.x + 11);
      text.setAttribute("y", place.y + 4);
      text.setAttribute("font-size", MARKER_BASE_FONT_SIZE);
      text.setAttribute("class", "place-marker-label");
      text.textContent = place.displayName;
      g.appendChild(circle);
      g.appendChild(text);
    });
    el.map.appendChild(g);
  }

  // Derives the map purely from challengeState — safe to call repeatedly
  // (mode re-entry, or the "show all streets" toggle) without losing
  // progress. keepView=true (the toggle flip) avoids jarring the
  // player's pan/zoom for what's just a display option.
  function renderChallengeMap(keepView) {
    while (el.map.firstChild) el.map.removeChild(el.map.firstChild);
    el.map.setAttribute("preserveAspectRatio", "xMidYMid meet");
    renderBackgroundLayers();

    challengeState.pathEls.clear();
    if (challengeState.showAllStreets) {
      ALL_STREETS.forEach(function (street) {
        var path = createStreetPathEl(street, "street unfound");
        var title = document.createElementNS(SVG_NS, "title");
        title.textContent = street.displayName;
        path.appendChild(title);
        el.map.appendChild(path);
        challengeState.pathEls.set(street.id, path);
      });
    }
    challengeState.found.forEach(function (id) {
      var street = STREETS_BY_ID.get(id);
      if (!street) return;
      var pathEl = challengeState.pathEls.get(id);
      if (!pathEl) {
        pathEl = createStreetPathEl(street, "street unfound");
        el.map.appendChild(pathEl);
        challengeState.pathEls.set(id, pathEl);
      }
      stylePathAsFound(pathEl, street);
    });

    renderPlaceMarkers();

    // Re-entering a finished attempt (e.g. toggling "show all streets"
    // after solving/giving up) must redraw the same end-state overlay —
    // gold route + dashed best path — since the DOM was just wiped.
    if (challengeState.ended) {
      highlightChallengeRoute();
      revealBestPath();
    }

    if (keepView) applyViewBox();
    else resetView();
  }

  function pulseOrCreatePath(street) {
    var pathEl = challengeState.pathEls.get(street.id);
    if (!pathEl) {
      pathEl = createStreetPathEl(street, "street unfound");
      var markersLayer = document.getElementById("placeMarkersLayer");
      el.map.insertBefore(pathEl, markersLayer);
      challengeState.pathEls.set(street.id, pathEl);
    }
    return pathEl;
  }

  function addChallengeFoundListItem(street) {
    var row = document.createElement("div");
    row.className = "found-item";
    var swatch = document.createElement("span");
    swatch.className = "swatch";
    swatch.style.background = colorFor(street.arrondissement);
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = street.displayName;
    var arr = document.createElement("span");
    arr.className = "arr";
    arr.textContent = street.arrondissement ? street.arrondissement + "e" : "";
    row.appendChild(swatch);
    row.appendChild(name);
    row.appendChild(arr);
    elC.challengeFoundList.insertBefore(row, elC.challengeFoundList.firstChild); // newest first, like classic mode
  }

  var CHALLENGE_GOLD = "#d4af37";

  // Marks, in gold, only the streets that actually form a working chain
  // between the two anchors — found via BFS restricted to the streets
  // the player named (challengeState.found). A player can find streets
  // that never contribute to the connection (dead ends, wrong guesses);
  // those stay in their normal per-arrondissement "found" color instead
  // of being highlighted as if they were part of the route.
  function highlightChallengeRoute() {
    var path = bfsPath(challengeState.anchors[0], challengeState.anchors[1], challengeState.found) || [];
    path.forEach(function (id) {
      var pathEl = challengeState.pathEls.get(id);
      if (!pathEl) return;
      pathEl.classList.add("found-route");
      pathEl.style.stroke = CHALLENGE_GOLD;
    });
  }

  // Overlays the real shortest path between the two anchors, styled
  // exactly like the give-up reveal (dashed), skipping any street the
  // player already found (already shown in gold) — so the two routes
  // are visible side by side for comparison.
  function revealBestPath() {
    var path = bfsPath(challengeState.anchors[0], challengeState.anchors[1]) || [];
    path.forEach(function (id) {
      if (challengeState.found.has(id)) return;
      var street = STREETS_BY_ID.get(id);
      if (!street) return;
      var pathEl = pulseOrCreatePath(street);
      pathEl.classList.remove("unfound");
      pathEl.classList.add("revealed");
    });
    return path;
  }

  function fireConfetti() {
    for (var i = 0; i < 32; i++) {
      (function () {
        var piece = document.createElement("span");
        piece.className = "confetti-piece";
        piece.style.left = Math.round(Math.random() * 100) + "vw";
        piece.style.background = PALETTE[Math.floor(Math.random() * PALETTE.length)];
        piece.style.animationDuration = (1.2 + Math.random() * 0.8) + "s";
        piece.style.animationDelay = (Math.random() * 0.3) + "s";
        piece.addEventListener("animationend", function () {
          if (piece.parentNode) piece.parentNode.removeChild(piece);
        });
        elC.confettiLayer.appendChild(piece);
      })();
    }
  }

  function handleChallengeMatch(street) {
    var pathEl = pulseOrCreatePath(street);
    if (challengeState.found.has(street.id)) {
      // Same rationale as classic's fix: must never touch the input's
      // value, or an already-found street could hijack a longer
      // in-progress guess.
      pulsePath(pathEl);
      return;
    }
    challengeState.found.add(street.id);
    challengeState.order.push(street.id);
    stylePathAsFound(pathEl, street);
    pulsePath(pathEl);
    addChallengeFoundListItem(street);
    elC.challengeInput.value = "";

    (ADJACENCY[street.id] || []).forEach(function (neighborId) {
      if (challengeState.found.has(neighborId)) {
        ufUnion(challengeState.ufParent, challengeState.ufRank, street.id, neighborId);
      }
    });

    var a = challengeState.anchors[0], b = challengeState.anchors[1];
    if (ufFind(challengeState.ufParent, a) === ufFind(challengeState.ufParent, b)) {
      challengeSolve();
    }
  }

  function stopChallengeAttempt() {
    stopChallengeTimer();
    challengeState.ended = true;
    elC.challengeInput.disabled = true;
  }

  function challengeSolve() {
    if (challengeState.ended) return;
    challengeState.solved = true;
    stopChallengeAttempt();

    var record = loadChallengeRecord(challengeState.date) || { solved: false, bestMs: null, attempts: 0 };
    record.attempts = (record.attempts || 0) + 1;
    if (!record.solved || challengeState.elapsedMs < record.bestMs) record.bestMs = challengeState.elapsedMs;
    record.solved = true;
    saveChallengeRecord(challengeState.date, record);
    refreshChallengeBtnLabel();

    highlightChallengeRoute();
    revealBestPath();
    fireConfetti();

    elC.challengeStatus.innerHTML = "";
    var p = document.createElement("p");
    p.style.margin = "0";
    p.textContent = "Défi terminé en " + formatElapsed(challengeState.elapsedMs) + " !";
    elC.challengeStatus.appendChild(p);
    elC.challengeStatus.hidden = false;
  }

  function challengeGiveUp() {
    if (challengeState.ended) return;
    var ok = window.confirm("Abandonner et révéler un itinéraire possible ?");
    if (!ok) return;
    stopChallengeAttempt();

    highlightChallengeRoute();
    var path = revealBestPath();

    elC.challengeStatus.innerHTML = "";
    var p = document.createElement("p");
    p.style.margin = "0";
    p.textContent = path.length
      ? "Partie abandonnée. Itinéraire possible révélé en pointillés."
      : "Partie abandonnée. Aucun itinéraire trouvé.";
    elC.challengeStatus.appendChild(p);
    elC.challengeStatus.hidden = false;
  }

  function beginChallengeAttempt() {
    // The two endpoints are given, not guessed — naming them yourself
    // would be counter-intuitive since they're already shown (with
    // names and colors) in the route header above the input.
    challengeState.found = new Set(challengeState.anchors);
    challengeState.order = [];
    challengeState.ufParent = new Map();
    challengeState.ufRank = new Map();
    challengeState.timerStart = null;
    challengeState.elapsedMs = null;
    challengeState.solved = false;
    challengeState.ended = false;

    elC.challengeStatus.hidden = true;
    elC.challengeFoundList.innerHTML = "";
    elC.challengeInput.disabled = false;
    elC.challengeInput.value = "";
    elC.challengeTimer.textContent = "00:00";

    renderChallengeMap(false);
    elC.challengeInput.focus();
  }

  function showAlreadySolvedStatus(record) {
    elC.challengeStatus.innerHTML = "";
    var p = document.createElement("p");
    p.style.margin = "0";
    p.textContent = "Déjà résolu aujourd'hui en " + formatElapsed(record.bestMs) + ".";
    var btn = document.createElement("button");
    btn.className = "btn-secondary";
    btn.textContent = "Rejouer pour le plaisir";
    btn.addEventListener("click", function () { beginChallengeAttempt(); });
    elC.challengeStatus.appendChild(p);
    elC.challengeStatus.appendChild(btn);
    elC.challengeStatus.hidden = false;

    elC.challengeInput.disabled = true;
    elC.challengeFoundList.innerHTML = "";
    challengeState.found = new Set();
    renderChallengeMap(false);
  }

  function enterChallengeMode() {
    var today = todayDateStr();
    var isFreshDay = challengeState.date !== today;
    if (isFreshDay) {
      challengeState.date = today;
      var pair = pickDailyPlaces(today) || [ALL_PLACES[0], ALL_PLACES[1]];
      challengeState.places = pair;
      challengeState.anchors = pair.map(function (p) { return p.anchorStreetId; });
      var idx = buildIndexesFor(ALL_STREETS);
      challengeState.coreIndex = idx.coreIndex;
      challengeState.fullIndex = idx.fullIndex;
      challengeState.showAllStreets = loadShowAllStreetsPref();
      elC.showAllStreetsToggle.checked = challengeState.showAllStreets;
      challengeState.attemptStarted = false;
    }

    elC.routeSwatchA.style.background = colorFor(challengeState.places[0].arrondissement);
    elC.routePlaceA.textContent = challengeState.places[0].displayName;
    elC.routeSwatchB.style.background = colorFor(challengeState.places[1].arrondissement);
    elC.routePlaceB.textContent = challengeState.places[1].displayName;

    if (challengeState.attemptStarted) {
      // Returning to challenge mode mid-session (e.g. from Classic) —
      // redraw the shared <svg> from the existing state (found streets,
      // timer, ended/solved flags all untouched), don't restart.
      renderChallengeMap(false);
      return;
    }
    challengeState.attemptStarted = true;

    var record = loadChallengeRecord(today);
    if (record && record.solved) {
      showAlreadySolvedStatus(record);
    } else {
      beginChallengeAttempt();
    }
  }

  function setMode(mode) {
    appMode = mode;
    var isChallenge = mode === "challenge";
    refreshChallengeBtnLabel();
    elC.tierField.hidden = isChallenge;
    elC.counterStat.hidden = isChallenge;
    elC.challengeTimerStat.hidden = !isChallenge;
    elC.challengeMapOptions.hidden = !isChallenge;
    elC.challengeSidebar.hidden = !isChallenge;
    elC.sidebar.hidden = isChallenge;
    elC.districtGrid.hidden = isChallenge;

    if (isChallenge) enterChallengeMode();
    else renderMap();
  }

  elC.challengeBtn.addEventListener("click", function () {
    setMode(appMode === "challenge" ? "classic" : "challenge");
  });

  elC.challengeInput.addEventListener("input", function () {
    if (challengeState.ended) return;
    if (challengeState.timerStart === null) startChallengeTimer();
    elC.challengeInput.classList.remove("shake");
    var value = elC.challengeInput.value;
    if (!value.trim()) return;
    var matches = resolveGuess(value, challengeState.fullIndex, challengeState.coreIndex);
    matches.forEach(handleChallengeMatch);
  });

  elC.challengeInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !challengeState.ended) {
      var value = elC.challengeInput.value;
      if (value.trim() && resolveGuess(value, challengeState.fullIndex, challengeState.coreIndex).length === 0) {
        shakeInputEl(elC.challengeInput);
      }
    }
  });

  elC.showAllStreetsToggle.addEventListener("change", function () {
    challengeState.showAllStreets = elC.showAllStreetsToggle.checked;
    saveShowAllStreetsPref(challengeState.showAllStreets);
    renderChallengeMap(true);
  });

  // ---------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------

  setupTierOptions();
  var savedProgress = loadClassicProgress();
  if (savedProgress) {
    el.tierSelect.value = String(savedProgress.tier);
    resetGame(savedProgress.tier, savedProgress.foundIds);
  } else {
    resetGame(state.tier);
  }
  refreshChallengeBtnLabel();

  var introAlreadySeen;
  try {
    introAlreadySeen = localStorage.getItem(INTRO_SEEN_KEY) === "1";
  } catch (e) {
    introAlreadySeen = false;
  }
  if (!introAlreadySeen) showIntro();
})();
