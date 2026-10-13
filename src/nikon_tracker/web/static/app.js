// Frontend controller for NikonUSA Refurbished Tracker

let currentStatus = {};
let allDeals = [];
let watchlistData = { items: [], presets: [] };

function showToast(msg) {
  const el = document.getElementById("ui-toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => el.classList.add("hidden"), 4000);
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

// --- Tab Navigation ---
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const target = btn.getAttribute("data-tab");
    document.getElementById(`tab-${target}`).classList.add("active");
    if (target === "watchlist") loadWatchlist();
    if (target === "alerts") loadAlerts();
    if (target === "settings") loadSettings();
  });
});

// --- Status & Deals Loading ---
async function loadStatus() {
  try {
    const res = await fetch("/api/status");
    currentStatus = await res.json();

    document.getElementById("kpi-threshold-label").textContent = Math.round(
      currentStatus.global_discount_threshold_pct || 50
    );
    document.getElementById("kpi-sub50-count").textContent = currentStatus.sub_50_msrp_count ?? 0;
    document.getElementById("kpi-instock-count").textContent = currentStatus.in_stock_count ?? 0;
    document.getElementById("kpi-total-skus").textContent = `/ ${currentStatus.total_tracked_skus ?? 0} SKUs`;
    document.getElementById("kpi-extrasale-count").textContent = currentStatus.extra_sale_count ?? 0;

    const lastPollStr = currentStatus.last_poll_at
      ? `Last poll: ${fmtRelativeTime(currentStatus.last_poll_at)} (${currentStatus.last_poll_duration_ms || 0}ms)`
      : "Scanning NikonUSA...";
    document.getElementById("kpi-last-poll").textContent = lastPollStr;

    const dot = document.getElementById("status-dot");
    const text = document.getElementById("status-text");
    const pauseBtn = document.getElementById("btn-pause-resume");

    dot.className = "status-dot";
    if (currentStatus.is_polling_now) {
      dot.classList.add("polling");
      text.textContent = "Scanning NikonUSA...";
    } else if (currentStatus.is_paused) {
      dot.classList.add("paused");
      text.textContent = "Paused";
      pauseBtn.textContent = "▶ Resume";
    } else {
      text.textContent = `Every ${currentStatus.poll_interval_seconds || 60}s • ${fmtRelativeTime(currentStatus.last_poll_at)}`;
      pauseBtn.textContent = "⏸ Pause";
    }
  } catch (err) {
    console.error("Failed to fetch status:", err);
  }
}

async function loadDeals() {
  const q = document.getElementById("filter-search").value.trim();
  const category = document.getElementById("filter-category").value;
  const sortBy = document.getElementById("filter-sort").value;
  const sub50Only = document.getElementById("filter-sub50").checked;
  const inStockOnly = document.getElementById("filter-instock").checked;
  const extraSaleOnly = document.getElementById("filter-extrasale").checked;

  const params = new URLSearchParams({
    sub_50_only: sub50Only,
    in_stock_only: inStockOnly,
    extra_sale_only: extraSaleOnly,
    category: category,
    sort_by: sortBy,
  });
  if (q) params.set("q", q);

  try {
    const res = await fetch(`/api/deals?${params.toString()}`);
    const data = await res.json();
    allDeals = data.items || [];

    // Populate/update category dropdown while preserving current selection
    const catSelect = document.getElementById("filter-category");
    if (Array.isArray(data.categories)) {
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

    renderDealsTable(allDeals, data.threshold_pct || 50);
  } catch (err) {
    console.error("Error loading deals:", err);
  }
}

function renderDealsTable(items, thresholdPct) {
  const tbody = document.getElementById("deals-tbody");
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">No refurbished items match your current filters. Try unchecking filters or clicking <strong>⚡ Check Now</strong>.</td></tr>`;
    return;
  }

  tbody.innerHTML = items
    .map((item) => {
      const isHot = item.discount_pct_vs_msrp !== null && item.discount_pct_vs_msrp >= thresholdPct;
      const rowClass = isHot && item.is_on_stock ? "row-sub50" : "";

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

      const regStrike =
        item.is_extra_refurb_sale
          ? `<div class="price-strike">Reg Refurb: ${fmtCurrency(item.refurb_reg_dollars)} (-${item.discount_pct_vs_refurb_reg}%)</div>`
          : "";

      const promoHtml = item.promo_message
        ? `<div class="promo-pill">🏷️ ${escapeHtml(item.promo_message)}</div>`
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
        savingsBadge = `
          <div>
            <span class="${badgeCls}">${fire}${item.discount_pct_vs_msrp.toFixed(1)}% OFF MSRP</span>
          </div>
          <div class="product-meta">
            <span><strong>${item.pct_of_msrp.toFixed(1)}%</strong> of MSRP</span>
            <span>• Save ${fmtCurrency(item.savings_vs_msrp_dollars)}</span>
          </div>
        `;
      }

      const thumb = item.image_url
        ? `<img src="${escapeHtml(item.image_url)}" alt="" class="product-thumb" loading="lazy" />`
        : `<div class="product-thumb"></div>`;

      return `
        <tr class="${rowClass}">
          <td>
            <div class="product-cell">
              ${thumb}
              <div>
                <a href="${escapeHtml(item.product_url)}" target="_blank" rel="noopener" class="product-title">
                  ${escapeHtml(item.name)}
                </a>
                <div class="product-meta">
                  <span>SKU: <strong>${escapeHtml(item.sku)}</strong></span>
                  <span>• ${escapeHtml(item.category)}</span>
                  ${item.lowest_seen_cents ? `<span>• Low: ${fmtCurrency(item.lowest_seen_cents / 100)}</span>` : ""}
                </div>
                ${promoHtml}
              </div>
            </div>
          </td>
          <td>${stockBadge}</td>
          <td>
            <div class="price-main">${fmtCurrency(item.effective_price_dollars)}</div>
            ${regStrike}
          </td>
          <td>
            <div style="font-weight:700;">
              ${fmtCurrency(item.msrp_dollars)}
              <button class="btn-icon" style="font-size:13px;" title="Edit MSRP manually" onclick="promptManualMsrp('${escapeHtml(item.sku)}', ${item.msrp_dollars || 0})">✏️</button>
            </div>
            <div class="product-meta">${escapeHtml(sourceLabel)}</div>
          </td>
          <td>${savingsBadge}</td>
          <td>
            <div style="display:flex; gap:6px; flex-wrap:wrap;">
              <button class="btn btn-secondary btn-sm" onclick="openHistoryModal('${escapeJsAttr(item.sku)}')">📈 History</button>
              <button class="btn btn-secondary btn-sm" onclick="quickWatchSku('${escapeJsAttr(item.sku)}', '${escapeJsAttr(item.slug)}', '${escapeJsAttr(item.product_key)}', '${escapeJsAttr(item.name)}')">🎯 Watch</button>
              <a href="${escapeHtml(item.product_url)}" target="_blank" rel="noopener" class="btn btn-primary btn-sm">Buy &rarr;</a>
            </div>
          </td>
        </tr>
      `;
    })
    .join("");
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

// --- Manual MSRP Override ---
async function promptManualMsrp(sku, currentMsrp) {
  const input = prompt(`Enter official Brand-New MSRP ($ USD) for SKU ${sku}:`, currentMsrp || "");
  if (!input) return;
  const val = parseFloat(input);
  if (isNaN(val) || val <= 0) {
    alert("Please enter a valid positive dollar amount.");
    return;
  }
  await fetch(`/api/deals/${encodeURIComponent(sku)}/msrp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ msrp_dollars: val }),
  });
  showToast(`Updated MSRP for ${sku} to ${fmtCurrency(val)}`);
  loadDeals();
  loadStatus();
}

// --- Watchlist & 266-Item Catalog Browser ---
async function loadWatchlist() {
  const res = await fetch("/api/watchlist");
  watchlistData = await res.json();
  document.getElementById("kpi-watchlist-count").textContent = (watchlistData.items || []).length;

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
  const items = watchlistData.items || [];
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

      const priceHtml = live ? `<strong>${fmtCurrency(live.effective_price_dollars)}</strong>` : "—";
      const msrpHtml = live
        ? `${fmtCurrency(live.msrp_dollars)} ${live.discount_pct_vs_msrp ? `<span class="badge badge-discount">${live.discount_pct_vs_msrp}% off</span>` : ""}`
        : "—";

      const rules = [];
      if (entry.max_price_dollars) rules.push(`&le; ${fmtCurrency(entry.max_price_dollars)}`);
      if (entry.min_discount_pct_msrp) rules.push(`&ge; ${entry.min_discount_pct_msrp}% off MSRP`);
      if (entry.notify_on_restock) rules.push("Restock Alert");
      if (entry.auto_open_browser) rules.push("Auto-Open URL");

      const prodUrl = live
        ? live.product_url
        : `https://www.nikonusa.com/p/${entry.slug}/${entry.product_key}/overview`;

      return `
        <tr>
          <td>
            <a href="${escapeHtml(prodUrl)}" target="_blank" rel="noopener" class="product-title">${escapeHtml(entry.label)}</a>
            <div class="product-meta">SKU: <strong>${escapeHtml(entry.sku)}</strong> • Slug: ${escapeHtml(entry.slug)}</div>
          </td>
          <td>${statusHtml}</td>
          <td>${priceHtml}</td>
          <td>${msrpHtml}</td>
          <td><div class="product-meta">${rules.join(" • ")}</div></td>
          <td>
            <button class="btn btn-danger btn-sm" onclick="removeWatchlist('${escapeHtml(entry.sku)}')">Remove</button>
          </td>
        </tr>
      `;
    })
    .join("");
}

function renderCatalogPicker() {
  const presetsEl = document.getElementById("presets-container");
  if (!presetsEl) return;

  const q = (document.getElementById("catalog-search")?.value || "").trim().toLowerCase();
  const cat = document.getElementById("catalog-category")?.value || "ALL";
  const stockFilter = document.getElementById("catalog-stock-filter")?.value || "ALL";

  const watchedSkus = new Set((watchlistData.items || []).map((i) => String(i.sku).toUpperCase()));
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
      const isWatched = watchedSkus.has(skuUpper);
      const prodUrl = `https://www.nikonusa.com/p/${p.slug}/${p.product_key}/overview`;

      const stockHtml = p.is_on_stock
        ? `<span class="badge badge-instock">✅ In Stock (${p.qty || "Yes"})</span>`
        : `<span class="badge badge-oos">Out of Stock</span>`;

      const priceHtml = p.refurb_dollars ? `<strong>${fmtCurrency(p.refurb_dollars)}</strong>` : "—";

      let msrpHtml = `<span class="muted">—</span>`;
      if (p.msrp_dollars) {
        const isSub50 = p.discount_pct_vs_msrp && p.discount_pct_vs_msrp >= 50.0;
        const discBadge = p.discount_pct_vs_msrp
          ? `<span class="badge ${isSub50 ? "badge-sub50" : "badge-discount"}">${isSub50 ? "🔥 " : ""}${p.discount_pct_vs_msrp}% off</span>`
          : "";
        msrpHtml = `${fmtCurrency(p.msrp_dollars)} ${discBadge}`;
      }

      const actionBtn = isWatched
        ? `<span class="badge badge-instock">✅ Watching</span>`
        : `<button type="button" class="btn btn-primary btn-sm" onclick="addPresetToWatchlist('${escapeJsAttr(p.sku)}', '${escapeJsAttr(p.slug)}', '${escapeJsAttr(p.product_key)}', '${escapeJsAttr(p.label)}')">🎯 + Watch</button>`;

      return `
        <tr>
          <td>
            <a href="${escapeHtml(prodUrl)}" target="_blank" rel="noopener" class="product-title">${escapeHtml(p.label)}</a>
            <div class="product-meta">SKU: <strong>${escapeHtml(p.sku)}</strong> • <code>/p/${escapeHtml(p.slug)}/${escapeHtml(p.product_key)}</code></div>
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

["catalog-search", "catalog-category", "catalog-stock-filter"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener(id === "catalog-search" ? "input" : "change", renderCatalogPicker);
  }
});

async function seedFullCatalogToDeals() {
  showToast("Loading all 266 Nikon refurbished & out-of-stock SKUs into Deals table...");
  const res = await fetch("/api/seed-catalog", { method: "POST" });
  const data = await res.json();
  // Uncheck "In Stock Only" so the user immediately sees all 266 OOS & In-Stock items
  const inStockCb = document.getElementById("filter-instock");
  if (inStockCb) inStockCb.checked = false;
  showToast(`Loaded ${data.total_catalog_count} total SKUs (${data.seeded_count} newly added)! Showing all OOS & In-Stock items.`);
  await loadStatus();
  await loadDeals();
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
    await loadDeals();
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
      auto_open_browser: false,
    }),
  });
  showToast(`Added ${label} to Watchlist!`);
  loadWatchlist();
  loadDeals();
  loadStatus();
}

async function quickWatchSku(sku, slug, productKey, label) {
  await addPresetToWatchlist(sku, slug, productKey, label);
}

async function removeWatchlist(sku) {
  await fetch(`/api/watchlist/${encodeURIComponent(sku)}`, { method: "DELETE" });
  showToast(`Removed ${sku} from Watchlist`);
  loadWatchlist();
}

document.getElementById("watchlist-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const urlOrSku = document.getElementById("wl-url-sku").value.trim();
  const label = document.getElementById("wl-label").value.trim();
  const maxPriceRaw = document.getElementById("wl-max-price").value;
  const minDiscRaw = document.getElementById("wl-min-discount").value;
  const restock = document.getElementById("wl-restock").checked;
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
      auto_open_browser: autoOpen,
    }),
  });

  if (!res.ok) {
    const err = await res.json();
    alert(err.detail || "Could not add watchlist entry");
    return;
  }

  document.getElementById("wl-url-sku").value = "";
  document.getElementById("wl-label").value = "";
  document.getElementById("wl-max-price").value = "";
  showToast("Watchlist rule saved!");
  loadWatchlist();
  loadDeals();
});

// --- Alerts Log ---
async function loadAlerts() {
  const res = await fetch("/api/alerts");
  const data = await res.json();
  const tbody = document.getElementById("alerts-tbody");
  const items = data.items || [];
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-cell">No alerts triggered yet. Click <strong>🔔 Test Alert</strong> in the top bar to test your notification channels!</td></tr>`;
    return;
  }

  tbody.innerHTML = items
    .map(
      (a) => `
      <tr>
        <td class="muted">${new Date(a.triggered_at).toLocaleString()}</td>
        <td><span class="badge badge-discount">${escapeHtml(a.event_type)}</span></td>
        <td>
          <strong>${escapeHtml(a.name)}</strong>
          <div class="product-meta">SKU: ${escapeHtml(a.sku)} • ${escapeHtml(a.message)}</div>
        </td>
        <td>
          <strong>${fmtCurrency(a.effective_price_dollars)}</strong>
          ${a.msrp_dollars ? `<div class="product-meta">MSRP: ${fmtCurrency(a.msrp_dollars)} (${a.discount_pct_vs_msrp}% off)</div>` : ""}
        </td>
        <td>${(a.channels_sent || []).map((c) => `<span class="badge badge-instock">${escapeHtml(c)}</span>`).join(" ") || "logged"}</td>
        <td><a href="${escapeHtml(a.product_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm">Open &rarr;</a></td>
      </tr>
    `
    )
    .join("");
}

// --- Settings ---
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
  const payload = {
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

  await fetch("/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  showToast("Settings saved!");
  loadStatus();
  loadDeals();
});

// --- History Modal ---
async function openHistoryModal(sku) {
  const modal = document.getElementById("history-modal");
  const title = document.getElementById("modal-title");
  const body = document.getElementById("modal-body");
  modal.classList.remove("hidden");
  title.textContent = `Loading History for ${sku}...`;
  body.innerHTML = `<p class="muted">Loading...</p>`;

  const res = await fetch(`/api/history/${encodeURIComponent(sku)}`);
  const data = await res.json();
  const v = data.variant;
  const pts = data.history || [];

  title.textContent = v ? `${v.name} [${sku}] — Price & Stock History` : `SKU ${sku} History`;

  let chartSvg = "";
  if (pts.length >= 1) {
    const prices = pts.map((p) => p.effective_price_dollars);
    const minP = Math.min(...prices) * 0.95;
    const maxP = Math.max(...prices, v?.msrp_dollars || 0) * 1.05 || 100;
    const w = 680;
    const h = 180;
    const coords = pts.map((p, idx) => {
      const x = pts.length === 1 ? w / 2 : 30 + (idx / (pts.length - 1)) * (w - 60);
      const y = h - 25 - ((p.effective_price_dollars - minP) / Math.max(1, maxP - minP)) * (h - 50);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });

    chartSvg = `
      <svg viewBox="0 0 ${w} ${h}" style="width:100%; background:#0b0e14; border:1px solid var(--border); border-radius:8px; margin-bottom:14px;">
        <polyline fill="none" stroke="#f6d300" stroke-width="2.5" points="${coords.join(" ")}" />
        ${coords
          .map((c) => {
            const [cx, cy] = c.split(",");
            return `<circle cx="${cx}" cy="${cy}" r="4" fill="#10b981" />`;
          })
          .join("")}
      </svg>
    `;
  }

  const rowsHtml = pts
    .slice()
    .reverse()
    .map(
      (p) => `
      <tr>
        <td class="muted">${new Date(p.recorded_at).toLocaleString()}</td>
        <td><strong>${fmtCurrency(p.effective_price_dollars)}</strong></td>
        <td>${fmtCurrency(p.refurb_reg_dollars)}</td>
        <td>${fmtCurrency(p.msrp_dollars)}</td>
        <td>${p.is_on_stock ? `<span class="badge badge-instock">${p.available_quantity} in stock</span>` : `<span class="badge badge-oos">OOS (0)</span>`}</td>
      </tr>
    `
    )
    .join("");

  body.innerHTML = `
    ${chartSvg}
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
        <tbody>${rowsHtml}</tbody>
      </table>
    </div>
  `;
}

document.getElementById("btn-close-modal").addEventListener("click", () => {
  document.getElementById("history-modal").classList.add("hidden");
});
document.getElementById("modal-close-backdrop").addEventListener("click", () => {
  document.getElementById("history-modal").classList.add("hidden");
});

// --- Topbar Controls & Filters ---
document.getElementById("btn-check-now").addEventListener("click", async () => {
  const btn = document.getElementById("btn-check-now");
  btn.disabled = true;
  btn.textContent = "⏳ Scanning...";
  try {
    const res = await fetch("/api/check-now", { method: "POST" });
    const data = await res.json();
    showToast(
      `Scan complete! Updated ${data.updated_count} SKUs (${(data.triggered_alerts || []).length} new alerts).`
    );
    await loadStatus();
    await loadDeals();
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ Check Now";
  }
});

document.getElementById("btn-pause-resume").addEventListener("click", async () => {
  const endpoint = currentStatus.is_paused ? "/api/resume" : "/api/pause";
  await fetch(endpoint, { method: "POST" });
  await loadStatus();
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

["filter-search", "filter-category", "filter-sort", "filter-sub50", "filter-instock", "filter-extrasale"].forEach(
  (id) => {
    const el = document.getElementById(id);
    el.addEventListener(id === "filter-search" ? "input" : "change", () => loadDeals());
  }
);

document.getElementById("kpi-sub50-card").addEventListener("click", () => {
  const cb = document.getElementById("filter-sub50");
  cb.checked = !cb.checked;
  loadDeals();
});

document.getElementById("kpi-extrasale-card").addEventListener("click", () => {
  const cb = document.getElementById("filter-extrasale");
  cb.checked = !cb.checked;
  loadDeals();
});

// Initial boot & auto-refresh
(async function init() {
  await loadStatus();
  await loadDeals();
  await loadWatchlist();
  setInterval(async () => {
    await loadStatus();
    if (document.getElementById("tab-deals").classList.contains("active")) {
      await loadDeals();
    }
  }, 5000);
})();
