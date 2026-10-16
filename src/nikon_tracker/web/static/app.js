// Frontend controller for NikonUSA Refurbished Tracker v1.0

let currentStatus = {};
let allDeals = [];
let watchlistData = { items: [], presets: [] };
let watchedSkusSet = new Set();
let recentlyChangedSkusSet = new Set();
let lastRenderedPollAt = null;
let lastFilterSignature = "";
let activeMsrpEditSku = null;

function debounce(fn, delayMs = 120) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), delayMs);
  };
}

function showToast(msg, type = "info") {
  const el = document.getElementById("ui-toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle("toast-error", type === "error");
  el.classList.remove("hidden");
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => el.classList.add("hidden"), 4200);
}

function fmtCurrency(val) {
  if (val === null || val === undefined) return "—";
  return "$" + Number(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtRelativeTime(isoStr) {
  if (!isoStr) return "Never";
  const diffSec = Math.max(0, Math.round((Date.now() - new Date(isoStr).getTime()) / 1000));
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `${mins}m ago`;
  return new Date(isoStr).toLocaleTimeString();
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeJsAttr(str) {
  if (!str) return "";
  return escapeHtml(String(str).replace(/\\/g, "\\\\").replace(/'/g, "\\'"));
}

// --- Tab Navigation ---
function switchToTab(target) {
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const isTarget = b.getAttribute("data-tab") === target;
    b.classList.toggle("active", isTarget);
    b.setAttribute("aria-selected", isTarget ? "true" : "false");
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `tab-${target}`);
  });
  if (target === "watchlist") loadWatchlist();
  if (target === "alerts") loadAlerts();
  if (target === "settings") loadSettings();
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    switchToTab(btn.getAttribute("data-tab"));
  });
});

// --- Filter State & KPI Highlight Sync ---
function syncActiveFilterHighlights() {
  const sub50Only = document.getElementById("filter-sub50")?.checked || false;
  const inStockOnly = document.getElementById("filter-instock")?.checked || false;
  const extraSaleOnly = document.getElementById("filter-extrasale")?.checked || false;
  const atlOnly = document.getElementById("filter-atl")?.checked || false;
  const category = document.getElementById("filter-category")?.value || "ALL";
  const mount = document.getElementById("filter-mount")?.value || "ALL";
  const q = (document.getElementById("filter-search")?.value || "").trim();
  const sortBy = document.getElementById("filter-sort")?.value || "discount_desc";

  document.getElementById("kpi-sub50-card")?.classList.toggle("active-filter", sub50Only);
  document.getElementById("kpi-instock-card")?.classList.toggle("active-filter", inStockOnly);
  document.getElementById("kpi-extrasale-card")?.classList.toggle("active-filter", extraSaleOnly);

  document.querySelectorAll("#quick-category-pills .cat-pill").forEach((pill) => {
    pill.classList.toggle("active", pill.getAttribute("data-cat") === category);
  });

  document.querySelectorAll("#live-deals-table th.sortable").forEach((th) => {
    const colSort = th.getAttribute("data-sort");
    const isMatch =
      colSort === sortBy ||
      (colSort === "velocity_desc" && (sortBy === "velocity_desc" || sortBy === "stock_desc")) ||
      (colSort === "price_asc" && (sortBy === "price_asc" || sortBy === "price_desc")) ||
      (colSort === "discount_desc" && (sortBy === "discount_desc" || sortBy === "savings_desc"));
    th.classList.toggle("active-sort", isMatch);
  });

  const isNonDefault =
    q !== "" ||
    category !== "ALL" ||
    mount !== "ALL" ||
    sub50Only !== false ||
    atlOnly !== false ||
    inStockOnly !== true ||
    extraSaleOnly !== false ||
    sortBy !== "discount_desc";

  const resetBtn = document.getElementById("btn-reset-filters");
  if (resetBtn) {
    resetBtn.classList.toggle("hidden", !isNonDefault);
  }
}

function updatePollerStatusPillText() {
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-text");
  const pauseBtn = document.getElementById("btn-pause-resume");
  if (!dot || !text || !pauseBtn) return;

  dot.className = "status-dot";
  if (currentStatus.is_polling_now) {
    dot.classList.add("polling");
    text.textContent = "Scanning NikonUSA...";
  } else if (currentStatus.is_paused) {
    dot.classList.add("paused");
    text.textContent = "Paused";
    pauseBtn.textContent = "▶ Resume";
  } else {
    pauseBtn.textContent = "⏸ Pause";
    let nextStr = `Every ${currentStatus.poll_interval_seconds || 60}s`;
    if (currentStatus.next_poll_at) {
      const remSec = Math.max(0, Math.round((new Date(currentStatus.next_poll_at).getTime() - Date.now()) / 1000));
      nextStr = `Next in ${remSec}s`;
    }
    text.textContent = `${nextStr} • ${fmtRelativeTime(currentStatus.last_poll_at)}`;
  }
}

function applyStatus(statusData) {
  if (!statusData || typeof statusData !== "object") return;
  currentStatus = statusData;

  const threshEl = document.getElementById("kpi-threshold-label");
  if (threshEl) threshEl.textContent = Math.round(currentStatus.global_discount_threshold_pct || 50);

  document.getElementById("kpi-sub50-count").textContent = currentStatus.sub_50_msrp_count ?? 0;
  document.getElementById("kpi-instock-count").textContent = currentStatus.in_stock_count ?? 0;
  document.getElementById("kpi-total-skus").textContent = `/ ${currentStatus.total_tracked_skus ?? 0} SKUs`;
  document.getElementById("kpi-extrasale-count").textContent = currentStatus.extra_sale_count ?? 0;

  const atlSummary = document.getElementById("kpi-atl-summary");
  if (atlSummary) {
    atlSummary.textContent = currentStatus.atl_count > 0 ? `• ${currentStatus.atl_count} at ATL` : "";
  }

  const dealsBadge = document.getElementById("tab-badge-deals");
  if (dealsBadge) dealsBadge.textContent = currentStatus.in_stock_count ?? 0;

  const lastPollStr = currentStatus.last_poll_at
    ? `Last poll: ${fmtRelativeTime(currentStatus.last_poll_at)} (${currentStatus.last_poll_duration_ms || 0}ms)`
    : "Scanning NikonUSA...";
  document.getElementById("kpi-last-poll").textContent = lastPollStr;

  // Mute button state
  const muteBtn = document.getElementById("btn-mute-desktop");
  if (muteBtn) {
    const isMuted = !!currentStatus.mute_desktop;
    muteBtn.textContent = isMuted ? "🔕 Muted" : "🔔 Alerts On";
    muteBtn.classList.toggle("btn-muted", isMuted);
  }

  // Flash sale event banner
  const flashBanner = document.getElementById("flash-sale-banner");
  const flashText = document.getElementById("flash-sale-text");
  if (flashBanner && flashText) {
    if (currentStatus.flash_sale_active && currentStatus.extra_sale_count >= 3) {
      flashText.innerHTML = `<strong>NikonUSA Refurbished Flash Sale Active!</strong> ${currentStatus.extra_sale_count} in-stock items have extra promotional markdowns right now (up to <strong>${currentStatus.max_extra_sale_pct || 10}% extra off</strong> regular refurb price).`;
      flashBanner.classList.remove("hidden");
    } else {
      flashBanner.classList.add("hidden");
    }
  }

  // Error banner handling
  const errBanner = document.getElementById("error-banner");
  const errText = document.getElementById("error-banner-text");
  if (errBanner && errText) {
    if (currentStatus.last_error) {
      errText.textContent = `⚠️ Last NikonUSA poll encountered an error: ${currentStatus.last_error}`;
      errBanner.classList.remove("hidden");
    } else {
      errBanner.classList.add("hidden");
    }
  }

  updatePollerStatusPillText();
}

// --- Status & Deals Loading ---
async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    applyStatus(data);
  } catch (err) {
    console.error("Failed to fetch status:", err);
  }
}

function getFilterQueryParams() {
  const q = document.getElementById("filter-search")?.value.trim() || "";
  const category = document.getElementById("filter-category")?.value || "ALL";
  const mount = document.getElementById("filter-mount")?.value || "ALL";
  const sortBy = document.getElementById("filter-sort")?.value || "discount_desc";
  const sub50Only = document.getElementById("filter-sub50")?.checked || false;
  const atlOnly = document.getElementById("filter-atl")?.checked || false;
  const inStockOnly = document.getElementById("filter-instock")?.checked || false;
  const extraSaleOnly = document.getElementById("filter-extrasale")?.checked || false;

  const params = new URLSearchParams({
    sub_50_only: sub50Only,
    atl_only: atlOnly,
    in_stock_only: inStockOnly,
    extra_sale_only: extraSaleOnly,
    category: category,
    mount: mount,
    sort_by: sortBy,
  });
  if (q) params.set("q", q);
  return params;
}

function getFilterSignature() {
  return getFilterQueryParams().toString();
}

async function loadDeals(force = false) {
  syncActiveFilterHighlights();
  if (activeMsrpEditSku && !force) {
    return;
  }

  const sig = getFilterSignature();
  const pollStamp = currentStatus.last_poll_at || "";
  if (!force && sig === lastFilterSignature && pollStamp === lastRenderedPollAt && lastRenderedPollAt !== null) {
    return;
  }

  const params = getFilterQueryParams();

  try {
    const res = await fetch(`/api/deals?${params.toString()}`);
    const data = await res.json();
    allDeals = data.items || [];
    lastFilterSignature = sig;
    lastRenderedPollAt = pollStamp;

    // Populate/update category dropdown while preserving current selection
    const catSelect = document.getElementById("filter-category");
    if (catSelect && Array.isArray(data.categories)) {
      const existingCats = new Set(Array.from(catSelect.options).map((o) => o.value));
      data.categories.forEach((cat) => {
        if (!existingCats.has(cat)) {
          const opt = document.createElement("option");
          opt.value = cat;
          opt.textContent = cat;
          catSelect.appendChild(opt);
        }
      });
    }

    const totalTracked = data.total_count ?? currentStatus.total_tracked_skus ?? allDeals.length;
    const countEl = document.getElementById("deals-results-count");
    if (countEl) {
      countEl.innerHTML = `Showing <strong>${allDeals.length}</strong> of <strong>${totalTracked}</strong> tracked SKUs`;
    }

    renderDealsTable(allDeals, data.threshold_pct || 50);
  } catch (err) {
    console.error("Error loading deals:", err);
  }
}

function renderDealsTable(items, thresholdPct) {
  const tbody = document.getElementById("deals-tbody");
  if (!items.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="6" class="empty-cell">
          No refurbished items match your current filters.
          Try clicking <strong>↺ Reset Filters</strong>, unchecking <strong>✅ In Stock Only</strong>, or clicking <strong>📥 Load All 266 SKUs</strong>.
        </td>
      </tr>`;
    return;
  }

  tbody.innerHTML = items
    .map((item) => {
      const skuUpper = String(item.sku || "").toUpperCase();
      const isWatched = watchedSkusSet.has(skuUpper);
      const isHot = item.discount_pct_vs_msrp !== null && item.discount_pct_vs_msrp >= thresholdPct;
      const isChanged = recentlyChangedSkusSet.has(skuUpper);
      const classes = [];
      if (isHot && item.is_on_stock) classes.push("row-sub50");
      else if (!item.is_on_stock) classes.push("row-oos");
      if (isChanged) classes.push("row-just-updated");
      const rowClass = classes.join(" ");

      let stockBadge = `<span class="badge badge-oos">Out of Stock</span>`;
      if (item.is_on_stock) {
        if (item.available_quantity > 0 && item.available_quantity <= 3) {
          stockBadge = `<span class="badge badge-lowstock">🔥 Only ${item.available_quantity} left!</span>`;
        } else if (item.available_quantity > 0) {
          stockBadge = `<span class="badge badge-instock">✅ ${item.available_quantity} in stock</span>`;
        } else {
          stockBadge = `<span class="badge badge-instock">✅ In Stock</span>`;
        }
      }

      const vel = item.velocity || null;
      let velocityHtml = "";
      if (vel) {
        const parts = [];
        if (vel.units_sold_24h > 0) {
          parts.push(`📉 ${vel.units_sold_24h} sold (24h) • ${vel.burn_rate_per_hour}/hr`);
        }
        if (vel.units_restocked_24h > 0) {
          parts.push(`📈 +${vel.units_restocked_24h} restocked`);
        }
        if (parts.length) {
          velocityHtml += `<div class="velocity-meta">${parts.join(" • ")}</div>`;
        }
        if (item.is_on_stock && (vel.sellout_risk === "critical" || vel.sellout_risk === "high" || vel.sellout_risk === "moderate")) {
          velocityHtml += `<div><span class="sellout-pill sellout-${vel.sellout_risk}" title="Predicted time until stock reaches 0 at current burn rate">${escapeHtml(vel.sellout_forecast)}</span></div>`;
        } else if (item.is_on_stock && vel.units_sold_24h > 0 && vel.sellout_forecast) {
          velocityHtml += `<div class="velocity-meta">⏳ ${escapeHtml(vel.sellout_forecast)}</div>`;
        }
      }

      const regStrike =
        item.is_extra_refurb_sale
          ? `<div class="price-strike">Reg Refurb: ${fmtCurrency(item.refurb_reg_dollars)} (-${item.discount_pct_vs_refurb_reg}%)</div>`
          : "";

      const atlBadge =
        item.is_all_time_low && item.is_on_stock
          ? `<span class="badge-atl" title="Lowest price ever recorded for this SKU">🏆 All-Time Low</span>`
          : "";

      const promoHtml =
        item.promo_message || atlBadge
          ? `<div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-top:4px;">
               ${atlBadge}
               ${item.promo_message ? `<span class="promo-pill" style="margin-top:0;">🏷️ ${escapeHtml(item.promo_message)}</span>` : ""}
             </div>`
          : "";

      const msrpSourceLabels = {
        new_catalog: "Official New SKU " + item.new_sku,
        refurb_base: "Nikon Base Price",
        manual: "Manual Override",
        unknown: "Unresolved",
      };
      const sourceLabel = msrpSourceLabels[item.msrp_source] || item.msrp_source;

      let savingsBadge = `<span class="muted">MSRP Unknown</span>`;
      if (item.discount_pct_vs_msrp !== null) {
        const badgeCls = isHot ? "badge badge-sub50" : "badge badge-discount";
        const fire = isHot ? "🔥 " : "";
        const meterWidth = Math.min(100, Math.max(5, Math.round(item.discount_pct_vs_msrp)));
        const meterCls = isHot ? "discount-meter-fill hot" : "discount-meter-fill";
        savingsBadge = `
          <div>
            <span class="${badgeCls}">${fire}${item.discount_pct_vs_msrp.toFixed(1)}% OFF MSRP</span>
          </div>
          <div class="discount-meter" title="${item.discount_pct_vs_msrp.toFixed(1)}% off official New MSRP">
            <div class="${meterCls}" style="width: ${meterWidth}%;"></div>
          </div>
          <div class="product-meta">
            <span><strong>${item.pct_of_msrp.toFixed(1)}%</strong> of MSRP</span>
            <span>• Save <strong>${fmtCurrency(item.savings_vs_msrp_dollars)}</strong></span>
          </div>
        `;
      }

      const thumb = item.image_url
        ? `<img src="${escapeHtml(item.image_url)}" alt="" class="product-thumb" loading="lazy" onerror="this.outerHTML='<div class=&quot;product-thumb-placeholder&quot;>N</div>'" />`
        : `<div class="product-thumb-placeholder" aria-hidden="true">N</div>`;

      const watchBtn = isWatched
        ? `<button class="btn btn-watching btn-sm" title="Already in your Watchlist — click to view rules" onclick="switchToTab('watchlist')">✅ Watching</button>`
        : `<button class="btn btn-secondary btn-sm" title="Add to Watchlist for instant restock & price alerts" onclick="quickWatchSku('${escapeJsAttr(item.sku)}', '${escapeJsAttr(item.slug)}', '${escapeJsAttr(item.product_key)}', '${escapeJsAttr(item.name)}')">🎯 Watch</button>`;

      return `
        <tr class="${rowClass}" data-sku="${escapeHtml(skuUpper)}">
          <td>
            <div class="product-cell">
              ${thumb}
              <div>
                <a href="${escapeHtml(item.product_url)}" target="_blank" rel="noopener" class="product-title">
                  ${escapeHtml(item.name)}
                </a>
                <div class="product-meta">
                  <span class="sku-pill">${escapeHtml(item.sku)}</span>
                  ${item.mount_system ? `<span class="mount-pill">${escapeHtml(item.mount_system)}</span>` : ""}
                  <span>${escapeHtml(item.category)}</span>
                  ${item.lowest_seen_cents ? `<span>• Low: ${fmtCurrency(item.lowest_seen_cents / 100)}</span>` : ""}
                </div>
                ${promoHtml}
              </div>
            </div>
          </td>
          <td>${stockBadge}${velocityHtml}</td>
          <td>
            <div class="price-main">${fmtCurrency(item.effective_price_dollars)}</div>
            ${regStrike}
          </td>
          <td id="msrp-cell-${escapeHtml(skuUpper)}">
            <div class="price-msrp">
              <span>${fmtCurrency(item.msrp_dollars)}</span>
              <button type="button" class="btn-icon" style="font-size:12px;" title="Edit MSRP inline" onclick="openInlineMsrpEditor('${escapeJsAttr(skuUpper)}', ${item.msrp_dollars || 0})">✏️</button>
            </div>
            <div class="product-meta">${escapeHtml(sourceLabel)}</div>
          </td>
          <td>${savingsBadge}</td>
          <td>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
              <button class="btn btn-secondary btn-sm" title="View price & stock history chart" onclick="openHistoryModal('${escapeJsAttr(item.sku)}')">📈 History</button>
              ${watchBtn}
              <a href="${escapeHtml(item.product_url)}" target="_blank" rel="noopener" class="btn btn-primary btn-sm">Buy &rarr;</a>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

// --- Inline Manual MSRP Popover Editor (No window.prompt / window.alert) ---
function openInlineMsrpEditor(sku, currentMsrp) {
  const skuUpper = String(sku || "").toUpperCase();
  const cell = document.getElementById(`msrp-cell-${skuUpper}`);
  if (!cell) return;
  activeMsrpEditSku = skuUpper;
  const initVal = currentMsrp && Number(currentMsrp) > 0 ? Number(currentMsrp).toFixed(2) : "";

  cell.innerHTML = `
    <div class="msrp-inline-editor">
      <input type="number" step="0.01" min="1" class="msrp-input" id="msrp-input-${escapeHtml(skuUpper)}" value="${escapeHtml(initVal)}" placeholder="MSRP $" aria-label="Edit MSRP for ${escapeHtml(skuUpper)}" />
      <button type="button" class="btn btn-primary btn-sm" style="padding:3px 7px;" title="Save MSRP (Enter)" onclick="saveInlineMsrp('${escapeJsAttr(skuUpper)}')">✓</button>
      <button type="button" class="btn btn-secondary btn-sm" style="padding:3px 7px;" title="Cancel (Escape)" onclick="cancelInlineMsrp()">✕</button>
    </div>
    <div class="product-meta">Press Enter to save</div>
  `;

  const inputEl = document.getElementById(`msrp-input-${skuUpper}`);
  if (inputEl) {
    inputEl.focus();
    inputEl.select();
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        saveInlineMsrp(skuUpper);
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancelInlineMsrp();
      }
    });
  }
}

function cancelInlineMsrp() {
  activeMsrpEditSku = null;
  loadDeals(true);
}

async function saveInlineMsrp(sku) {
  const skuUpper = String(sku || "").toUpperCase();
  const inputEl = document.getElementById(`msrp-input-${skuUpper}`);
  const val = inputEl ? parseFloat(inputEl.value) : NaN;
  if (isNaN(val) || val <= 0) {
    showToast("Please enter a valid positive dollar amount for MSRP.", "error");
    return;
  }
  activeMsrpEditSku = null;
  const res = await fetch(`/api/deals/${encodeURIComponent(skuUpper)}/msrp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msrp_dollars: val }),
  });
  if (!res.ok) {
    showToast(`Failed to update MSRP for ${skuUpper}`, "error");
    return;
  }
  showToast(`Updated MSRP for ${skuUpper} to ${fmtCurrency(val)}`);
  await loadStatus();
  await loadDeals(true);
}

// --- Watchlist & 266-Item Catalog Browser ---
async function loadWatchlist() {
  const res = await fetch("/api/watchlist");
  watchlistData = await res.json();
  const items = watchlistData.items || [];
  watchedSkusSet = new Set(items.map((i) => String(i.sku || "").toUpperCase()));

  document.getElementById("kpi-watchlist-count").textContent = items.length;
  const wlBadge = document.getElementById("tab-badge-watchlist");
  if (wlBadge) wlBadge.textContent = items.length;

  // Populate catalog category dropdown once
  const catSel = document.getElementById("catalog-category");
  if (catSel && catSel.options.length <= 1 && Array.isArray(watchlistData.preset_categories)) {
    watchlistData.preset_categories.forEach((cat) => {
      const count = (watchlistData.presets || []).filter((p) => p.category === cat).length;
      const opt = document.createElement("option");
      opt.value = cat;
      opt.textContent = `${cat} (${count})`;
      catSel.appendChild(opt);
    });
  }

  renderCatalogPicker();

  // Render active watchlist table
  const tbody = document.getElementById("watchlist-tbody");
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">Your watchlist is empty. Click <strong>🎯 + Watch</strong> on any of the 266 items below or paste a URL on the right!</td></tr>`;
    return;
  }

  tbody.innerHTML = items
    .map((entry) => {
      const live = entry.live_variant;
      const statusHtml = live
        ? live.is_on_stock
          ? `<span class="badge badge-instock">✅ In Stock (${live.available_quantity})</span>`
          : `<span class="badge badge-oos">Out of Stock (0)</span>`
        : `<span class="muted">Pending scan</span>`;

      const priceHtml = live ? `<strong class="price-main" style="font-size:16px;">${fmtCurrency(live.effective_price_dollars)}</strong>` : "—";
      const msrpHtml = live
        ? `<div class="price-msrp">${fmtCurrency(live.msrp_dollars)} ${live.discount_pct_vs_msrp ? `<span class="badge badge-discount">${live.discount_pct_vs_msrp}% off</span>` : ""}</div>`
        : "—";

      const rules = [];
      if (entry.max_price_dollars) rules.push(`&le; ${fmtCurrency(entry.max_price_dollars)}`);
      if (entry.min_discount_pct_msrp) rules.push(`&ge; ${entry.min_discount_pct_msrp}% off MSRP`);
      if (entry.notify_on_restock) rules.push("Restock Alert");
      if (entry.notify_on_price_drop !== false) rules.push("Price Drop Alert");
      if (entry.auto_open_browser) rules.push("Auto-Open URL");

      const prodUrl = live
        ? live.product_url
        : `https://www.nikonusa.com/p/${entry.slug}/${entry.product_key}/overview`;

      return `
        <tr>
          <td>
            <a href="${escapeHtml(prodUrl)}" target="_blank" rel="noopener" class="product-title">${escapeHtml(entry.label)}</a>
            <div class="product-meta">
              <span class="sku-pill">${escapeHtml(entry.sku)}</span>
              <span>Slug: ${escapeHtml(entry.slug)}</span>
            </div>
          </td>
          <td>${statusHtml}</td>
          <td>${priceHtml}</td>
          <td>${msrpHtml}</td>
          <td><div class="product-meta">${rules.join(" • ")}</div></td>
          <td>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
              <button type="button" class="btn btn-secondary btn-sm" title="Edit rule thresholds" onclick="editWatchlistRule('${escapeJsAttr(entry.sku)}')">✏️ Edit</button>
              <button type="button" class="btn btn-secondary btn-sm" title="View price & stock history" onclick="openHistoryModal('${escapeJsAttr(entry.sku)}')">📈</button>
              <button type="button" class="btn btn-danger btn-sm" title="Remove from Watchlist" onclick="removeWatchlist('${escapeJsAttr(entry.sku)}')">Remove</button>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
}

function editWatchlistRule(sku) {
  const skuUpper = String(sku || "").toUpperCase();
  const entry = (watchlistData.items || []).find((i) => String(i.sku || "").toUpperCase() === skuUpper);
  if (!entry) return;

  document.getElementById("wl-url-sku").value = entry.sku;
  document.getElementById("wl-label").value = entry.label || "";
  document.getElementById("wl-max-price").value = entry.max_price_dollars ?? "";
  document.getElementById("wl-min-discount").value = entry.min_discount_pct_msrp ?? 50;
  document.getElementById("wl-restock").checked = !!entry.notify_on_restock;
  const priceDropEl = document.getElementById("wl-price-drop");
  if (priceDropEl) priceDropEl.checked = entry.notify_on_price_drop !== false;
  document.getElementById("wl-auto-open").checked = !!entry.auto_open_browser;

  const titleEl = document.getElementById("wl-form-title");
  if (titleEl) titleEl.textContent = `✏️ Editing Watchlist Rule: ${entry.sku}`;
  const submitBtn = document.getElementById("btn-submit-wl");
  if (submitBtn) submitBtn.textContent = `💾 Update Rule (${entry.sku})`;
  const cancelBtn = document.getElementById("btn-cancel-wl-edit");
  if (cancelBtn) cancelBtn.classList.remove("hidden");

  const cardEl = document.getElementById("watchlist-form-card");
  if (cardEl) cardEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
  document.getElementById("wl-max-price")?.focus();
}

function resetWatchlistForm() {
  document.getElementById("wl-url-sku").value = "";
  document.getElementById("wl-label").value = "";
  document.getElementById("wl-max-price").value = "";
  document.getElementById("wl-min-discount").value = "50";
  document.getElementById("wl-restock").checked = true;
  const priceDropEl = document.getElementById("wl-price-drop");
  if (priceDropEl) priceDropEl.checked = true;
  document.getElementById("wl-auto-open").checked = false;

  const titleEl = document.getElementById("wl-form-title");
  if (titleEl) titleEl.textContent = "Add or Edit Watchlist Rule";
  const submitBtn = document.getElementById("btn-submit-wl");
  if (submitBtn) submitBtn.textContent = "➕ Save Watchlist Rule";
  const cancelBtn = document.getElementById("btn-cancel-wl-edit");
  if (cancelBtn) cancelBtn.classList.add("hidden");
}

document.getElementById("btn-cancel-wl-edit")?.addEventListener("click", resetWatchlistForm);

function renderCatalogPicker() {
  const presetsEl = document.getElementById("presets-container");
  if (!presetsEl) return;

  const q = (document.getElementById("catalog-search")?.value || "").trim().toLowerCase();
  const cat = document.getElementById("catalog-category")?.value || "ALL";
  const stockFilter = document.getElementById("catalog-stock-filter")?.value || "ALL";

  const allPresets = watchlistData.presets || [];

  const filtered = allPresets.filter((p) => {
    if (cat !== "ALL" && p.category !== cat) return false;
    if (stockFilter === "OOS" && p.is_on_stock) return false;
    if (stockFilter === "INSTOCK" && !p.is_on_stock) return false;
    if (stockFilter === "SUB50" && (!p.discount_pct_vs_msrp || p.discount_pct_vs_msrp < 50.0)) return false;
    if (q) {
      const hay = `${p.label} ${p.sku} ${p.slug} ${p.category || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const badgeEl = document.getElementById("catalog-count-badge");
  if (badgeEl) badgeEl.textContent = `${filtered.length} / ${allPresets.length}`;

  if (!filtered.length) {
    presetsEl.innerHTML = `<tr><td colspan="6" class="empty-cell">No catalog items match your search.</td></tr>`;
    return;
  }

  presetsEl.innerHTML = filtered
    .map((p) => {
      const skuUpper = String(p.sku).toUpperCase();
      const isWatched = watchedSkusSet.has(skuUpper);
      const prodUrl = `https://www.nikonusa.com/p/${p.slug}/${p.product_key}/overview`;

      const stockHtml = p.is_on_stock
        ? `<span class="badge badge-instock">✅ In Stock (${p.qty || "Yes"})</span>`
        : `<span class="badge badge-oos">Out of Stock</span>`;

      const priceHtml = p.refurb_dollars ? `<strong style="font-variant-numeric: tabular-nums;">${fmtCurrency(p.refurb_dollars)}</strong>` : "—";

      let msrpHtml = `<span class="muted">—</span>`;
      if (p.msrp_dollars) {
        const isSub50 = p.discount_pct_vs_msrp && p.discount_pct_vs_msrp >= 50.0;
        const discBadge = p.discount_pct_vs_msrp
          ? `<span class="badge ${isSub50 ? "badge-sub50" : "badge-discount"}">${isSub50 ? "🔥 " : ""}${p.discount_pct_vs_msrp}% off</span>`
          : "";
        msrpHtml = `<span style="font-variant-numeric: tabular-nums;">${fmtCurrency(p.msrp_dollars)}</span> ${discBadge}`;
      }

      const actionBtn = isWatched
        ? `<button type="button" class="btn btn-watching btn-sm" onclick="editWatchlistRule('${escapeJsAttr(p.sku)}')">✅ Watching</button>`
        : `<button type="button" class="btn btn-primary btn-sm" onclick="addPresetToWatchlist('${escapeJsAttr(p.sku)}', '${escapeJsAttr(p.slug)}', '${escapeJsAttr(p.product_key)}', '${escapeJsAttr(p.label)}')">🎯 + Watch</button>`;

      return `
        <tr>
          <td>
            <a href="${escapeHtml(prodUrl)}" target="_blank" rel="noopener" class="product-title">${escapeHtml(p.label)}</a>
            <div class="product-meta">
              <span class="sku-pill">${escapeHtml(p.sku)}</span>
              <code>/p/${escapeHtml(p.slug)}/${escapeHtml(p.product_key)}</code>
            </div>
          </td>
          <td><span class="product-meta">${escapeHtml(p.category || "Refurbished")}</span></td>
          <td>${stockHtml}</td>
          <td>${priceHtml}</td>
          <td>${msrpHtml}</td>
          <td>${actionBtn}</td>
        </tr>
      `;
    })
    .join("");
}

const debouncedCatalogRender = debounce(renderCatalogPicker, 120);
document.getElementById("catalog-search")?.addEventListener("input", debouncedCatalogRender);
document.getElementById("catalog-category")?.addEventListener("change", renderCatalogPicker);
document.getElementById("catalog-stock-filter")?.addEventListener("change", renderCatalogPicker);

async function seedFullCatalogToDeals() {
  showToast("Loading all 266 Nikon refurbished & out-of-stock SKUs into Deals table...");
  const res = await fetch("/api/seed-catalog", { method: "POST" });
  const data = await res.json();
  const inStockCb = document.getElementById("filter-instock");
  if (inStockCb) inStockCb.checked = false;
  showToast(`Loaded ${data.total_catalog_count} total SKUs (${data.seeded_count} newly added)! Showing all OOS & In-Stock items.`);
  await loadStatus();
  await loadDeals(true);
  await loadWatchlist();
}

document.getElementById("btn-seed-catalog")?.addEventListener("click", seedFullCatalogToDeals);
document.getElementById("btn-seed-deals")?.addEventListener("click", seedFullCatalogToDeals);

document.getElementById("btn-discover-oos")?.addEventListener("click", async () => {
  const btn = document.getElementById("btn-discover-oos");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "⏳ Probing Nikon Sitemap...";
  }
  showToast("Probing NikonUSA sitemap for all hidden & out-of-stock refurbished pages (takes ~10-15s)...");
  try {
    const res = await fetch("/api/discover-oos", { method: "POST" });
    const data = await res.json();
    showToast(`Live sitemap probe complete! Synced ${data.discovered_count} refurbished SKUs.`);
    await loadStatus();
    await loadDeals(true);
    await loadWatchlist();
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🛰️ Live-Probe Nikon Sitemap";
    }
  }
});

async function addPresetToWatchlist(sku, slug, productKey, label) {
  showToast(`Adding ${label} (${sku}) and probing NikonUSA...`);
  await fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sku,
      slug,
      product_key: productKey,
      label,
      min_discount_pct_msrp: 50.0,
      notify_on_restock: true,
      notify_on_price_drop: true,
      auto_open_browser: false,
    }),
  });
  showToast(`Added ${label} to Watchlist!`);
  await loadWatchlist();
  await loadDeals(true);
  await loadStatus();
}

async function quickWatchSku(sku, slug, productKey, label) {
  await addPresetToWatchlist(sku, slug, productKey, label);
}

async function removeWatchlist(sku) {
  await fetch(`/api/watchlist/${encodeURIComponent(sku)}`, { method: "DELETE" });
  showToast(`Removed ${sku} from Watchlist`);
  await loadWatchlist();
  await loadDeals(true);
}

document.getElementById("watchlist-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const urlOrSku = document.getElementById("wl-url-sku").value.trim();
  const label = document.getElementById("wl-label").value.trim();
  const maxPriceRaw = document.getElementById("wl-max-price").value;
  const minDiscRaw = document.getElementById("wl-min-discount").value;
  const restock = document.getElementById("wl-restock").checked;
  const priceDropEl = document.getElementById("wl-price-drop");
  const priceDrop = priceDropEl ? priceDropEl.checked : true;
  const autoOpen = document.getElementById("wl-auto-open").checked;

  const res = await fetch("/api/watchlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      url_or_sku: urlOrSku,
      label: label,
      max_price_dollars: maxPriceRaw ? parseFloat(maxPriceRaw) : null,
      min_discount_pct_msrp: minDiscRaw ? parseFloat(minDiscRaw) : 50.0,
      notify_on_restock: restock,
      notify_on_price_drop: priceDrop,
      auto_open_browser: autoOpen,
    }),
  });

  if (!res.ok) {
    let detail = "Could not add watchlist entry";
    try {
      const err = await res.json();
      if (err.detail) detail = err.detail;
    } catch (_) {}
    showToast(detail, "error");
    return;
  }

  resetWatchlistForm();
  showToast("Watchlist rule saved!");
  await loadWatchlist();
  await loadDeals(true);
});

// --- Alerts Log ---
async function loadAlerts() {
  const res = await fetch("/api/alerts");
  const data = await res.json();
  const tbody = document.getElementById("alerts-tbody");
  const items = data.items || [];

  const alertBadge = document.getElementById("tab-badge-alerts");
  if (alertBadge) alertBadge.textContent = items.length;

  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">No alerts triggered yet. Click <strong>🔔 Test Alert</strong> in the top bar to test your notification channels!</td></tr>`;
    return;
  }

  tbody.innerHTML = items
    .map(
      (a) => `
      <tr>
        <td class="muted" style="font-variant-numeric: tabular-nums;">${new Date(a.triggered_at).toLocaleString()}</td>
        <td><span class="badge badge-discount">${escapeHtml(a.event_type)}</span></td>
        <td>
          <strong>${escapeHtml(a.name)}</strong>
          <div class="product-meta">
            <span class="sku-pill">${escapeHtml(a.sku)}</span>
            <span>${escapeHtml(a.message)}</span>
          </div>
        </td>
        <td>
          <strong style="font-variant-numeric: tabular-nums;">${fmtCurrency(a.effective_price_dollars)}</strong>
          ${a.msrp_dollars ? `<div class="product-meta">MSRP: ${fmtCurrency(a.msrp_dollars)} (${a.discount_pct_vs_msrp}% off)</div>` : ""}
        </td>
        <td>${(a.channels_sent || []).map((c) => `<span class="badge badge-instock">${escapeHtml(c)}</span>`).join(" ") || "logged"}</td>
        <td><a href="${escapeHtml(a.product_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm">Open &rarr;</a></td>
      </tr>
    `
    )
    .join("");
}

// --- Settings, Per-Channel Test & Backup/Restore ---
function getSettingsFormPayload() {
  return {
    poll_interval_seconds: parseInt(document.getElementById("set-poll-interval").value, 10) || 60,
    global_discount_threshold_pct: parseFloat(document.getElementById("set-discount-threshold").value) || 50.0,
    alert_cooldown_minutes: parseInt(document.getElementById("set-cooldown").value, 10) || 360,
    alert_on_any_extra_refurb_sale: document.getElementById("set-extra-sale-alert").checked,
    desktop_notifications_enabled: document.getElementById("set-desktop-enabled").checked,
    desktop_sound_enabled: document.getElementById("set-desktop-sound").checked,
    auto_open_browser_on_sub50: document.getElementById("set-auto-open-sub50").checked,
    ntfy_enabled: document.getElementById("set-ntfy-enabled").checked,
    ntfy_server_url: document.getElementById("set-ntfy-server").value.trim() || "https://ntfy.sh",
    ntfy_topic: document.getElementById("set-ntfy-topic").value.trim(),
    ntfy_token: document.getElementById("set-ntfy-token").value.trim(),
    discord_enabled: document.getElementById("set-discord-enabled").checked,
    discord_webhook_url: document.getElementById("set-discord-url").value.trim(),
    telegram_enabled: document.getElementById("set-telegram-enabled").checked,
    telegram_bot_token: document.getElementById("set-telegram-token").value.trim(),
    telegram_chat_id: document.getElementById("set-telegram-chat").value.trim(),
    pushover_enabled: document.getElementById("set-pushover-enabled").checked,
    pushover_user_key: document.getElementById("set-pushover-user").value.trim(),
    pushover_api_token: document.getElementById("set-pushover-token").value.trim(),
  };
}

async function loadSettings() {
  const res = await fetch("/api/settings");
  const s = await res.json();
  document.getElementById("set-poll-interval").value = s.poll_interval_seconds ?? 60;
  document.getElementById("set-discount-threshold").value = s.global_discount_threshold_pct ?? 50;
  document.getElementById("set-cooldown").value = s.alert_cooldown_minutes ?? 360;
  document.getElementById("set-extra-sale-alert").checked = !!s.alert_on_any_extra_refurb_sale;

  document.getElementById("set-desktop-enabled").checked = !!s.desktop_notifications_enabled;
  document.getElementById("set-desktop-sound").checked = !!s.desktop_sound_enabled;
  document.getElementById("set-auto-open-sub50").checked = !!s.auto_open_browser_on_sub50;

  document.getElementById("set-ntfy-enabled").checked = !!s.ntfy_enabled;
  document.getElementById("set-ntfy-server").value = s.ntfy_server_url || "https://ntfy.sh";
  document.getElementById("set-ntfy-topic").value = s.ntfy_topic || "";
  document.getElementById("set-ntfy-token").value = s.ntfy_token || "";

  document.getElementById("set-discord-enabled").checked = !!s.discord_enabled;
  document.getElementById("set-discord-url").value = s.discord_webhook_url || "";

  document.getElementById("set-telegram-enabled").checked = !!s.telegram_enabled;
  document.getElementById("set-telegram-token").value = s.telegram_bot_token || "";
  document.getElementById("set-telegram-chat").value = s.telegram_chat_id || "";

  document.getElementById("set-pushover-enabled").checked = !!s.pushover_enabled;
  document.getElementById("set-pushover-user").value = s.pushover_user_key || "";
  document.getElementById("set-pushover-token").value = s.pushover_api_token || "";
}

document.getElementById("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const payload = getSettingsFormPayload();

  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const saveMsg = document.getElementById("settings-save-msg");
  if (saveMsg) {
    saveMsg.textContent = `✅ Saved at ${new Date().toLocaleTimeString()}`;
    clearTimeout(window._saveMsgTimer);
    window._saveMsgTimer = setTimeout(() => {
      saveMsg.textContent = "";
    }, 5000);
  }
  showToast("Settings saved!");
  await loadStatus();
  await loadDeals(true);
});

document.querySelectorAll(".btn-test-channel").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const ch = btn.getAttribute("data-channel") || "";
    if (!ch) return;
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = "⏳ Testing...";
    try {
      const res = await fetch(`/api/test-alert?channel=${encodeURIComponent(ch)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(getSettingsFormPayload()),
      });
      if (!res.ok) {
        let errDetail = `Channel test failed (${res.status})`;
        try {
          const err = await res.json();
          if (err.detail) errDetail = err.detail;
        } catch (_) {}
        showToast(errDetail, "error");
        return;
      }
      const data = await res.json();
      const sent = data.channels_sent || [];
      if (sent.length > 0) {
        showToast(`✅ Test alert delivered via ${sent.join(", ")}!`);
      } else {
        showToast(`⚠️ Channel '${ch}' did not dispatch — check credentials/fields above.`, "warning");
      }
      await loadAlerts();
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  });
});

document.getElementById("btn-export-backup")?.addEventListener("click", () => {
  showToast("Downloading JSON configuration backup...");
  window.location.href = "/api/backup";
});

document.getElementById("input-import-backup")?.addEventListener("change", async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (_) {
      showToast("Invalid JSON backup file.", "error");
      return;
    }
    const res = await fetch("/api/backup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed),
    });
    if (!res.ok) {
      let detail = "Failed to import backup";
      try {
        const err = await res.json();
        if (err.detail) detail = err.detail;
      } catch (_) {}
      showToast(detail, "error");
      return;
    }
    const summary = await res.json();
    showToast(
      `Restored ${summary.watchlist_restored || 0} watchlist rules, ${summary.settings_restored || 0} settings, and ${summary.manual_msrps_restored || 0} MSRP overrides!`
    );
    await loadStatus();
    await loadSettings();
    await loadWatchlist();
    await loadDeals(true);
    await loadAlerts();
  } finally {
    e.target.value = "";
  }
});

// --- History Modal (Day-by-Day Price & Stock Velocity Rollup) ---
let _lastHistoryData = null;
let _historyViewMode = "daily"; // "daily" | "intraday"

async function openHistoryModal(sku) {
  const modal = document.getElementById("history-modal");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");
  modal.classList.remove("hidden");
  title.textContent = `Loading History for ${sku}...`;
  body.innerHTML = `<p class="muted">Loading...</p>`;

  const res = await fetch(`/api/history/${encodeURIComponent(sku)}`);
  _lastHistoryData = await res.json();
  _historyViewMode = "daily";
  renderHistoryModalContent();
}

function toggleHistoryViewMode(mode) {
  _historyViewMode = mode;
  renderHistoryModalContent();
}

function renderHistoryModalContent() {
  if (!_lastHistoryData) return;
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");
  const sku = _lastHistoryData.sku;
  const v = _lastHistoryData.variant;
  const vel = _lastHistoryData.velocity || {};
  const daily = _lastHistoryData.daily_history || [];
  const pts = _lastHistoryData.history || [];

  title.textContent = v
    ? `${v.name} [${sku}] — Daily Price & Stock Velocity`
    : `SKU ${sku} — Daily Price & Stock Velocity`;

  const forecastColor =
    vel.sellout_risk === "critical"
      ? "var(--red)"
      : vel.sellout_risk === "high"
      ? "var(--amber)"
      : vel.sellout_risk === "moderate"
      ? "#38bdf8"
      : "var(--text)";

  const velocityLabel =
    vel.units_sold_24h > 0
      ? `-${vel.units_sold_24h} sold (${vel.burn_rate_per_hour}/hr)`
      : vel.units_restocked_24h > 0
      ? `+${vel.units_restocked_24h} restocked`
      : "0 sold (24h)";

  const statsHtml = v
    ? `
    <div class="history-stats-grid">
      <div class="history-stat-box">
        <div class="history-stat-label">Current Refurb Price</div>
        <div class="history-stat-val" style="color: var(--nikon-yellow);">${fmtCurrency(v.effective_price_dollars)}</div>
      </div>
      <div class="history-stat-box">
        <div class="history-stat-label">Official New MSRP</div>
        <div class="history-stat-val">${fmtCurrency(v.msrp_dollars)} ${v.discount_pct_vs_msrp ? `<small class="muted" style="font-size:12px;">(-${v.discount_pct_vs_msrp}%)</small>` : ""}</div>
      </div>
      <div class="history-stat-box">
        <div class="history-stat-label">All-Time Low Seen</div>
        <div class="history-stat-val" style="color: var(--green);">${v.lowest_seen_cents ? fmtCurrency(v.lowest_seen_cents / 100) : fmtCurrency(v.effective_price_dollars)}</div>
      </div>
      <div class="history-stat-box">
        <div class="history-stat-label">24h Unit Velocity</div>
        <div class="history-stat-val" style="font-size:15px;">${escapeHtml(velocityLabel)}</div>
      </div>
      <div class="history-stat-box">
        <div class="history-stat-label">Sellout Forecast</div>
        <div class="history-stat-val" style="font-size:15px; color: ${forecastColor};">${escapeHtml(vel.sellout_forecast || (v.is_on_stock ? "Stable stock" : "Out of Stock"))}</div>
      </div>
    </div>
  `
    : "";

  let chartSvg = "";
  let chartPointMeta = [];
  if (daily.length >= 1) {
    const prices = daily.map((d) => d.effective_price_dollars);
    const qtys = daily.map((d) => d.close_quantity ?? d.available_quantity ?? 0);
    const maxQty = Math.max(1, ...qtys, ...daily.map((d) => d.high_quantity || 0));

    const target50 = v?.msrp_dollars ? v.msrp_dollars * 0.5 : null;
    const minCandidates = target50 ? [...prices, target50] : prices;
    const minP = Math.min(...minCandidates) * 0.94;
    const maxP = Math.max(...prices, v?.msrp_dollars ? v.msrp_dollars * 0.85 : 0) * 1.05 || 100;

    const w = 720;
    const h = 235;
    // Top panel (Price): y from 18 to 115
    const priceToY = (val) => 115 - ((val - minP) / Math.max(1, maxP - minP)) * 95;
    // Bottom panel (Stock Qty bars): y from 148 to 212
    const stockBottomY = 212;
    const stockMaxH = 58;

    const getX = (idx) => (daily.length === 1 ? w / 2 : 42 + (idx / (daily.length - 1)) * (w - 84));

    const coords = daily.map((d, idx) => {
      const x = getX(idx);
      const y = priceToY(d.effective_price_dollars);
      chartPointMeta.push({ x, y, d });
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });

    let targetLineSvg = "";
    if (target50) {
      const ty = priceToY(target50).toFixed(1);
      targetLineSvg = `
        <line x1="24" y1="${ty}" x2="${w - 24}" y2="${ty}" stroke="#10b981" stroke-width="1.5" stroke-dasharray="5,5" opacity="0.7" />
        <text x="${w - 28}" y="${Math.max(14, ty - 5)}" fill="#10b981" font-size="10.5" font-weight="700" text-anchor="end">
          50% MSRP Target (${fmtCurrency(target50)})
        </text>
      `;
    }

    const barWidth = Math.min(28, Math.max(6, Math.floor((w - 90) / Math.max(2, daily.length * 1.6))));
    const stockBarsSvg = daily
      .map((d, idx) => {
        const cx = getX(idx);
        const qty = d.close_quantity ?? d.available_quantity ?? 0;
        const bh = qty > 0 ? Math.max(4, (qty / maxQty) * stockMaxH) : 2;
        const by = stockBottomY - bh;
        const barColor =
          qty === 0
            ? "#475569"
            : d.units_restocked > d.units_sold_est
            ? "#10b981"
            : d.units_sold_est > 0
            ? "#f59e0b"
            : "#38bdf8";
        const showLabel = daily.length <= 10 || idx === 0 || idx === daily.length - 1 || idx % Math.ceil(daily.length / 7) === 0;
        const shortDate = String(d.date || "").slice(5);
        return `
          <rect x="${(cx - barWidth / 2).toFixed(1)}" y="${by.toFixed(1)}" width="${barWidth}" height="${bh.toFixed(1)}" rx="2" fill="${barColor}" opacity="0.88">
            <title>${d.date}: ${qty} in stock (Sold: ${d.units_sold_est || 0}, Restocked: ${d.units_restocked || 0})</title>
          </rect>
          <text x="${cx.toFixed(1)}" y="${(by - 4).toFixed(1)}" fill="#cbd5e1" font-size="9.5" text-anchor="middle">${qty}</text>
          ${showLabel ? `<text x="${cx.toFixed(1)}" y="227" fill="#94a3b8" font-size="9.5" text-anchor="middle">${shortDate}</text>` : ""}
        `;
      })
      .join("");

    chartSvg = `
      <div class="chart-hud-bar" id="chart-hud-bar">
        <span>🖱️ Hover over the chart to inspect daily price, stock level, and unit burn velocity</span>
      </div>
      <svg id="history-chart-svg" viewBox="0 0 ${w} ${h}" style="width:100%; background:#0b0e14; border:1px solid var(--border); border-radius:8px; margin-bottom:12px; cursor:crosshair;">
        <text x="26" y="15" fill="#94a3b8" font-size="10" font-weight="700">DAILY REFURB PRICE ($ USD)</text>
        ${targetLineSvg}
        <line id="svg-crosshair-line" x1="0" y1="16" x2="0" y2="${h - 14}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="3,3" visibility="hidden" />
        <polyline fill="none" stroke="#f6d300" stroke-width="2.5" points="${coords.join(" ")}" />
        ${coords
          .map((c, idx) => {
            const [cx, cy] = c.split(",");
            const d = daily[idx];
            return `<circle cx="${cx}" cy="${cy}" r="4" fill="#f6d300"><title>${d.date}: ${fmtCurrency(d.effective_price_dollars)}</title></circle>`;
          })
          .join("")}
        <circle id="svg-crosshair-dot" cx="0" cy="0" r="6.5" fill="#f6d300" stroke="#0b0e14" stroke-width="2" visibility="hidden" />
        <line x1="20" y1="130" x2="${w - 20}" y2="130" stroke="#1e293b" stroke-width="1" />
        <text x="26" y="144" fill="#94a3b8" font-size="10" font-weight="700">DAILY WAREHOUSE STOCK QTY &amp; BURN VELOCITY</text>
        ${stockBarsSvg}
      </svg>
    `;
  }

  const toggleBarHtml = `
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; flex-wrap:wrap; gap:8px;">
      <div class="quick-cats">
        <button type="button" class="cat-pill ${_historyViewMode === "daily" ? "active" : ""}" onclick="toggleHistoryViewMode('daily')">
          📅 Day-by-Day Rollup &amp; Stock Velocity (${daily.length}d)
        </button>
        <button type="button" class="cat-pill ${_historyViewMode === "intraday" ? "active" : ""}" onclick="toggleHistoryViewMode('intraday')">
          ⏱️ Raw Intraday Log (${pts.length} events)
        </button>
      </div>
      <span class="muted" style="font-size:11.5px;">Tracks daily open/close units, units sold, and restock velocity</span>
    </div>
  `;

  let tableHtml = "";
  if (_historyViewMode === "daily") {
    const dailyRows = daily
      .slice()
      .reverse()
      .map((d, idx) => {
        const isLatest = idx === 0;
        const priceRange =
          d.low_price_dollars !== d.high_price_dollars
            ? `<div class="product-meta">Range: ${fmtCurrency(d.low_price_dollars)} – ${fmtCurrency(d.high_price_dollars)}</div>`
            : "";

        const openQ = d.open_quantity ?? d.close_quantity ?? 0;
        const closeQ = d.close_quantity ?? d.available_quantity ?? 0;
        const stockTransition =
          openQ !== closeQ
            ? `<strong>${openQ} &rarr; ${closeQ}</strong> in stock`
            : closeQ > 0
            ? `<span class="badge badge-instock">${closeQ} in stock</span>`
            : `<span class="badge badge-oos">OOS (0)</span>`;
        const peakLowMeta =
          d.high_quantity !== d.low_quantity
            ? `<div class="product-meta">Peak: ${d.high_quantity} • Low: ${d.low_quantity}</div>`
            : "";

        const velBadges = [];
        if (d.units_sold_est > 0) {
          velBadges.push(`<span class="badge badge-lowstock">📉 ${d.units_sold_est} sold</span>`);
        }
        if (d.units_restocked > 0) {
          velBadges.push(`<span class="badge badge-instock">📈 +${d.units_restocked} restocked</span>`);
        }
        if (!velBadges.length) {
          velBadges.push(`<span class="muted">No stock change (0)</span>`);
        }
        const netStr =
          d.net_quantity_change !== 0
            ? `<span class="product-meta" style="margin-left:6px;">(Net: ${d.net_quantity_change > 0 ? "+" : ""}${d.net_quantity_change})</span>`
            : "";

        return `
          <tr>
            <td style="font-variant-numeric: tabular-nums;">
              <strong>${escapeHtml(d.date)}</strong>
              ${isLatest ? `<span class="sku-pill" style="margin-left:6px; color:var(--nikon-yellow);">Latest</span>` : ""}
            </td>
            <td style="font-variant-numeric: tabular-nums;">
              <strong>${fmtCurrency(d.effective_price_dollars)}</strong>
              ${priceRange}
            </td>
            <td style="font-variant-numeric: tabular-nums;">${fmtCurrency(d.msrp_dollars)}</td>
            <td style="font-variant-numeric: tabular-nums;">
              ${stockTransition}
              ${peakLowMeta}
            </td>
            <td style="font-variant-numeric: tabular-nums;">
              ${velBadges.join(" ")} ${netStr}
            </td>
          </tr>
        `;
      })
      .join("");

    tableHtml = `
      <div class="table-wrapper">
        <table class="deals-table">
          <thead>
            <tr>
              <th>Date (UTC)</th>
              <th>Refurb Price (Close)</th>
              <th>New MSRP</th>
              <th>Stock (Open &rarr; Close)</th>
              <th>Daily Unit Velocity</th>
            </tr>
          </thead>
          <tbody>${dailyRows || `<tr><td colspan="5" class="empty-cell">No daily history recorded yet.</td></tr>`}</tbody>
        </table>
      </div>
    `;
  } else {
    const intradayRows = pts
      .slice()
      .reverse()
      .map(
        (p) => `
        <tr>
          <td class="muted" style="font-variant-numeric: tabular-nums;">${new Date(p.recorded_at).toLocaleString()}</td>
          <td><strong style="font-variant-numeric: tabular-nums;">${fmtCurrency(p.effective_price_dollars)}</strong></td>
          <td style="font-variant-numeric: tabular-nums;">${fmtCurrency(p.refurb_reg_dollars)}</td>
          <td style="font-variant-numeric: tabular-nums;">${fmtCurrency(p.msrp_dollars)}</td>
          <td>${p.is_on_stock ? `<span class="badge badge-instock">${p.available_quantity} in stock</span>` : `<span class="badge badge-oos">OOS (0)</span>`}</td>
        </tr>
      `
      )
      .join("");

    tableHtml = `
      <div class="table-wrapper">
        <table class="deals-table">
          <thead>
            <tr>
              <th>Recorded At</th>
              <th>Refurb Sale Price</th>
              <th>Reg Refurb Price</th>
              <th>New MSRP</th>
              <th>Stock</th>
            </tr>
          </thead>
          <tbody>${intradayRows || `<tr><td colspan="5" class="empty-cell">No history snapshots recorded yet.</td></tr>`}</tbody>
        </table>
      </div>
    `;
  }

  body.innerHTML = `
    ${statsHtml}
    ${chartSvg}
    ${toggleBarHtml}
    ${tableHtml}
  `;

  // Bind interactive SVG crosshair hover readout
  const svgEl = document.getElementById("history-chart-svg");
  const hudEl = document.getElementById("chart-hud-bar");
  const chLine = document.getElementById("svg-crosshair-line");
  const chDot = document.getElementById("svg-crosshair-dot");
  if (svgEl && hudEl && chLine && chDot && chartPointMeta.length > 0) {
    svgEl.addEventListener("mousemove", (evt) => {
      const rect = svgEl.getBoundingClientRect();
      if (!rect.width) return;
      const svgX = ((evt.clientX - rect.left) / rect.width) * 720;
      let nearest = chartPointMeta[0];
      let minDist = Math.abs(svgX - nearest.x);
      for (let i = 1; i < chartPointMeta.length; i++) {
        const dist = Math.abs(svgX - chartPointMeta[i].x);
        if (dist < minDist) {
          minDist = dist;
          nearest = chartPointMeta[i];
        }
      }
      chLine.setAttribute("x1", nearest.x.toFixed(1));
      chLine.setAttribute("x2", nearest.x.toFixed(1));
      chLine.setAttribute("visibility", "visible");
      chDot.setAttribute("cx", nearest.x.toFixed(1));
      chDot.setAttribute("cy", nearest.y.toFixed(1));
      chDot.setAttribute("visibility", "visible");

      const d = nearest.d;
      const openQ = d.open_quantity ?? d.close_quantity ?? 0;
      const closeQ = d.close_quantity ?? d.available_quantity ?? 0;
      const discText =
        d.msrp_dollars && d.msrp_dollars > d.effective_price_dollars
          ? ` (-${(((d.msrp_dollars - d.effective_price_dollars) / d.msrp_dollars) * 100).toFixed(1)}% vs MSRP)`
          : "";
      hudEl.innerHTML = `
        <span>📅 <strong>${escapeHtml(d.date)}</strong></span>
        <span>💰 Price: <strong style="color:var(--nikon-yellow);">${fmtCurrency(d.effective_price_dollars)}</strong>${escapeHtml(discText)}</span>
        <span>📦 Stock: <strong>${openQ} &rarr; ${closeQ}</strong></span>
        <span>📉 Sold: <strong>${d.units_sold_est || 0}</strong> • 📈 Restocked: <strong>+${d.units_restocked || 0}</strong></span>
      `;
    });

    svgEl.addEventListener("mouseleave", () => {
      chLine.setAttribute("visibility", "hidden");
      chDot.setAttribute("visibility", "hidden");
      hudEl.innerHTML = `<span>🖱️ Hover over the chart to inspect daily price, stock level, and unit burn velocity</span>`;
    });
  }
}

function closeHistoryModal() {
  document.getElementById("history-modal")?.classList.add("hidden");
}

document.getElementById("btn-close-modal").addEventListener("click", closeHistoryModal);
document.getElementById("modal-close-backdrop").addEventListener("click", closeHistoryModal);

// --- Topbar Controls & Filters ---
async function triggerManualCheck() {
  const btn = document.getElementById("btn-check-now");
  btn.disabled = true;
  btn.textContent = "⏳ Scanning...";
  try {
    const res = await fetch("/api/check-now", { method: "POST" });
    const data = await res.json();
    if (Array.isArray(data.changed_skus)) {
      data.changed_skus.forEach((s) => recentlyChangedSkusSet.add(String(s).toUpperCase()));
      setTimeout(() => recentlyChangedSkusSet.clear(), 8000);
    }
    showToast(
      `Scan complete! Updated ${data.updated_count} SKUs (${(data.triggered_alerts || []).length} new alerts).`
    );
    await loadStatus();
    await loadDeals(true);
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ Check Now";
  }
}

document.getElementById("btn-check-now").addEventListener("click", triggerManualCheck);
document.getElementById("btn-retry-error")?.addEventListener("click", triggerManualCheck);

document.getElementById("btn-pause-resume").addEventListener("click", async () => {
  const endpoint = currentStatus.is_paused ? "/api/resume" : "/api/pause";
  await fetch(endpoint, { method: "POST" });
  await loadStatus();
});

document.getElementById("btn-mute-desktop")?.addEventListener("click", async () => {
  const nextMuted = !currentStatus.mute_desktop;
  const res = await fetch("/api/mute-desktop", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ muted: nextMuted }),
  });
  if (res.ok) {
    const st = await res.json();
    applyStatus(st);
    showToast(nextMuted ? "🔕 Desktop toast & chime notifications muted." : "🔔 Desktop toast & chime notifications unmuted.");
  }
});

document.getElementById("btn-filter-flash-sale")?.addEventListener("click", () => {
  switchToTab("deals");
  const cb = document.getElementById("filter-extrasale");
  if (cb) cb.checked = true;
  loadDeals(true);
});

document.getElementById("btn-export-csv")?.addEventListener("click", () => {
  const params = getFilterQueryParams();
  showToast("Downloading filtered deals CSV...");
  window.location.href = `/api/deals/export.csv?${params.toString()}`;
});

document.getElementById("btn-test-alert").addEventListener("click", async () => {
  showToast("Sending test alert...");
  const res = await fetch("/api/test-alert", { method: "POST" });
  const data = await res.json();
  const sent = (data.channels_sent || []).join(", ") || "alert log (enable channels in Settings)";
  showToast(`Test alert dispatched via: ${sent}`);
  loadAlerts();
});

document.getElementById("btn-refresh-alerts").addEventListener("click", loadAlerts);

const debouncedLoadDeals = debounce(() => loadDeals(true), 120);
document.getElementById("filter-search").addEventListener("input", debouncedLoadDeals);

[
  "filter-category",
  "filter-mount",
  "filter-sort",
  "filter-sub50",
  "filter-instock",
  "filter-extrasale",
  "filter-atl",
].forEach((id) => {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener("change", () => loadDeals(true));
  }
});

// Quick category pills
document.querySelectorAll("#quick-category-pills .cat-pill").forEach((pill) => {
  pill.addEventListener("click", () => {
    const cat = pill.getAttribute("data-cat") || "ALL";
    const select = document.getElementById("filter-category");
    if (select) {
      if (!Array.from(select.options).some((o) => o.value === cat)) {
        const opt = document.createElement("option");
        opt.value = cat;
        opt.textContent = cat;
        select.appendChild(opt);
      }
      select.value = cat;
    }
    loadDeals(true);
  });
});

// Clickable table column headers for sorting
document.querySelectorAll("#live-deals-table th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const colSort = th.getAttribute("data-sort");
    const sortSel = document.getElementById("filter-sort");
    if (!sortSel || !colSort) return;
    if (colSort === "price_asc") {
      sortSel.value = sortSel.value === "price_asc" ? "price_desc" : "price_asc";
    } else if (colSort === "discount_desc") {
      sortSel.value = sortSel.value === "discount_desc" ? "savings_desc" : "discount_desc";
    } else if (colSort === "velocity_desc") {
      sortSel.value = sortSel.value === "velocity_desc" ? "stock_desc" : "velocity_desc";
    } else {
      sortSel.value = colSort;
    }
    loadDeals(true);
  });
});

// Reset Filters button
document.getElementById("btn-reset-filters")?.addEventListener("click", () => {
  document.getElementById("filter-search").value = "";
  document.getElementById("filter-category").value = "ALL";
  const mountEl = document.getElementById("filter-mount");
  if (mountEl) mountEl.value = "ALL";
  document.getElementById("filter-sort").value = "discount_desc";
  document.getElementById("filter-sub50").checked = false;
  document.getElementById("filter-instock").checked = true;
  document.getElementById("filter-extrasale").checked = false;
  const atlEl = document.getElementById("filter-atl");
  if (atlEl) atlEl.checked = false;
  loadDeals(true);
});

// Interactive KPI Cards (click + keyboard Enter/Space)
function bindKpiCardAction(cardId, actionFn) {
  const card = document.getElementById(cardId);
  if (!card) return;
  card.addEventListener("click", actionFn);
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      actionFn();
    }
  });
}

bindKpiCardAction("kpi-sub50-card", () => {
  switchToTab("deals");
  const cb = document.getElementById("filter-sub50");
  cb.checked = !cb.checked;
  loadDeals(true);
});

bindKpiCardAction("kpi-instock-card", () => {
  switchToTab("deals");
  const cb = document.getElementById("filter-instock");
  cb.checked = !cb.checked;
  loadDeals(true);
});

bindKpiCardAction("kpi-extrasale-card", () => {
  switchToTab("deals");
  const cb = document.getElementById("filter-extrasale");
  cb.checked = !cb.checked;
  loadDeals(true);
});

bindKpiCardAction("kpi-watchlist-card", () => {
  switchToTab("watchlist");
});

// Global Keyboard Shortcuts: '/' to focus search, 'Escape' to close modal or clear search
document.addEventListener("keydown", (e) => {
  const modal = document.getElementById("history-modal");
  if (e.key === "Escape") {
    if (activeMsrpEditSku) {
      cancelInlineMsrpEditor();
      return;
    }
    if (modal && !modal.classList.contains("hidden")) {
      closeHistoryModal();
      return;
    }
    if (document.activeElement && document.activeElement.tagName === "INPUT" && document.activeElement.type === "search") {
      document.activeElement.value = "";
      document.activeElement.dispatchEvent(new Event("input"));
      document.activeElement.blur();
    }
    return;
  }

  const tag = document.activeElement ? document.activeElement.tagName : "";
  if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
    e.preventDefault();
    if (document.getElementById("tab-watchlist")?.classList.contains("active")) {
      const catInput = document.getElementById("catalog-search");
      catInput?.focus();
      catInput?.select();
    } else {
      switchToTab("deals");
      const searchInput = document.getElementById("filter-search");
      searchInput?.focus();
      searchInput?.select();
    }
  }
});

// --- Real-Time SSE Event Stream & Countdown Ticker ---
let _eventSource = null;

function connectEventStream() {
  if (typeof EventSource === "undefined") return;
  try {
    if (_eventSource) {
      _eventSource.close();
    }
    const es = new EventSource("/api/events");
    _eventSource = es;

    es.onopen = () => {
      const dot = document.getElementById("status-dot");
      if (dot) {
        dot.classList.remove("paused");
      }
    };

    es.onmessage = async (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.status) {
          applyStatus(msg.status);
        }
        if (msg.event === "poll_started") {
          currentStatus.is_polling_now = true;
          updatePollerStatusPillText();
        } else if (msg.event === "poll_completed") {
          if (Array.isArray(msg.changed_skus) && msg.changed_skus.length > 0) {
            msg.changed_skus.forEach((s) => recentlyChangedSkusSet.add(String(s).toUpperCase()));
            setTimeout(() => recentlyChangedSkusSet.clear(), 8000);
          }
          if (document.getElementById("tab-deals")?.classList.contains("active")) {
            await loadDeals(Boolean(msg.changed_skus && msg.changed_skus.length > 0));
          }
          if (Array.isArray(msg.triggered_alerts) && msg.triggered_alerts.length > 0) {
            showToast(`🔔 ${msg.triggered_alerts.length} new refurbished deal alerts triggered!`);
            await loadAlerts();
          }
        }
      } catch (_) {}
    };

    es.onerror = () => {
      es.close();
      setTimeout(connectEventStream, 5000);
    };
  } catch (_) {}
}

// Initial boot, live SSE connection, 1s countdown ticker & fallback auto-refresh
(async function init() {
  await loadStatus();
  await loadWatchlist();
  await loadDeals(true);
  await loadAlerts();
  connectEventStream();

  // Smooth 1-second countdown ticker for Next Poll
  setInterval(updatePollerStatusPillText, 1000);

  // Fallback periodic sync every 10s
  setInterval(async () => {
    await loadStatus();
    if (document.getElementById("tab-deals").classList.contains("active")) {
      await loadDeals(false);
    }
  }, 10000);
})();
