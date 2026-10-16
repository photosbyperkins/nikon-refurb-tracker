# NikonUSA Refurbished Tracker (`nikon-refurb-tracker`)

An open-source, cross-platform (**Windows & macOS**) real-time deal and restock tracker for [NikonUSA Refurbished Cameras & Lenses](https://www.nikonusa.com/refurbished-cameras).

Built to catch **sub-50% of MSRP flash sales** and **high-demand camera/lens restocks** (such as refurbished Nikon Z8, Z9, Zf, Z6III, Z50II, and S-Line NIKKOR Z lenses) within seconds.

---

## Why This Tracker Exists (The "Hidden MSRP" Problem on NikonUSA)

When NikonUSA runs a flash refurbished sale (e.g., *"10% Off Select Refurbished Products"*), their product listing JSON replaces the brand-new price comparison field with the regular refurbished price—showing only `"10% Off"` instead of the true total discount off MSRP. Furthermore, even when no flash sale is running, NikonUSA often compares the refurbished price against the *discounted new street price* rather than the *official brand-new MSRP*.

**How `nikon-refurb-tracker` solves this:**
1. **Direct Next.js JSON Polling**: Queries NikonUSA's `/_next/data/{buildId}/en-US/refurbished-cameras.json?limit=99` endpoint directly (with automatic `buildId` self-healing), scanning the entire refurbished catalog and exact warehouse stock counts in `< 1 second`.
2. **True New-MSRP Resolver**: Automatically maps every refurbished product (`{slug}-refurbished` and `{sku}B`/`{sku}Q`) to its brand-new NikonUSA catalog counterpart (`{slug}` and `{sku}`), extracting and caching the **official Brand-New MSRP** so it can accurately compute **% of MSRP** and immediately flag items priced **$\le 50\%$ of MSRP**.
3. **Out-of-Stock Watchlist Probing**: Directly monitors `/_next/data/{buildId}/en-US/p/{slug}/{sku}/overview.json` for out-of-stock items (like the Nikon Zf `1761Q` or Z8 `1695Q`) that are hidden from Nikon's main refurbished page until the moment inventory drops.

---

## Features (v1.0)

- 🔥 **Sub-50% of MSRP Deal Detection**: Automatically highlights and alerts when any refurbished camera, kit, or lens drops to $\le 50\%$ of its true brand-new MSRP (configurable threshold).
- 🏆 **All-Time Low (ATL) & Flash Sale Event Detection**: Automatically badges items at or below their historical lowest price (`🏆 All-Time Low`) and surfaces a live **NikonUSA Flash Sale Event Banner** when storewide extra refurb markdowns go live.
- 📉 **Warehouse Stock Velocity & Sellout Forecasting**: Tracks live `availableQuantity`, 24-hour unit burn rate (`units/hr`), and estimated hours/days until sellout.
- 📷 **Mount System & Quick-Category Filtering**: Instant filtering by **Z-Mount FX (Full Frame)**, **Z-Mount DX (APS-C)**, **F-Mount (DSLR)**, and **Optics & Accessories**, plus one-click category pills.
- 🔍 **266-SKU Out-of-Stock & Hidden Refurbished Catalog Browser**: Includes 266 curated Nikon refurbished cameras, Z-mount & F-mount lenses, teleconverters, FTZ adapters, flashes, and sport optics discovered via NikonUSA sitemap probing—even when they are out of stock and hidden from Nikon's main `/refurbished-cameras` page.
- 🎯 **Custom Watchlist & Price-Drop Monitoring**: Watch any NikonUSA refurbished URL or pick from the 266-SKU catalog with custom max price ($), min `% off MSRP`, instant restock (`0 -> N`), and **Any Price Drop** triggers.
- 🔔 **Fast Multi-Channel Alerts (with Debounced Audio & Per-Channel Testing)**:
  - **Windows & macOS Native Desktop Notifications** + debounced audible chime (consolidated batch summary on cold start) + topbar mute toggle + optional auto-open product page in browser.
  - **Mobile Push via [ntfy.sh](https://ntfy.sh)**, **Discord Rich Webhooks**, **Telegram Bot**, and **Pushover**, each with an independent **🧪 Test** button in Settings.
- ⚡ **Real-Time SSE Dashboard + Interactive History Charts**: Live Server-Sent Events (`/api/events`) push poll updates, countdown timers, and row-change highlights without page reloads, paired with an interactive dual-panel SVG price/stock crosshair chart, 1-click CSV export, and portable JSON Backup & Restore.

---

## Quick Start (Windows & macOS)

### Option A: 1-Click Standalone Download (No Python Required)

Download the latest precompiled executable from the [**GitHub Releases page**](https://github.com/photosbyperkins/nikon-refurb-tracker/releases/latest):

- **Windows (10/11):** Download **`NikonRefurbTracker-Windows.exe`** and double-click to launch. It starts in your Windows system tray (yellow **`N`** icon) and automatically opens the dashboard at `http://127.0.0.1:8470`.
  - *Note: Because this is a free open-source build without a paid Microsoft certificate, click **More info &rarr; Run anyway** if Windows SmartScreen prompts on first launch.*
- **macOS (Apple Silicon & Intel):** Download **`NikonRefurbTracker-macOS.zip`**, double-click to unzip **`NikonRefurbTracker.app`**, and move it to Applications.
  - *Note: On first launch, **Right-click (or Ctrl+click) `NikonRefurbTracker.app` &rarr; Open** (or run `xattr -cr NikonRefurbTracker.app` in Terminal) to bypass macOS Gatekeeper for unsigned open-source apps.*

---

### Option B: Run from Python Source

Requires **Python 3.10+**.

```bash
git clone https://github.com/photosbyperkins/nikon-refurb-tracker.git
cd nikon-refurb-tracker
pip install -e ".[dev]"
```

#### Run a Fast Terminal Scan or Browse the 266-SKU Catalog

```bash
# Scan all in-stock NikonUSA refurbished items and resolve true New MSRPs:
nikon-tracker check

# Show only hot deals priced at <= 50% of official New MSRP:
nikon-tracker check --sub50

# Browse or search the full 266-item out-of-stock & refurbished catalog:
nikon-tracker catalog --search "Z 8"
nikon-tracker catalog --category "NIKKOR Z"

# Add any SKU or URL to your restock & sub-50% MSRP watchlist:
nikon-tracker watch 1695Q
```

#### Launch the System Tray App & Local Web Dashboard

```bash
nikon-tracker start
```

- Opens the dashboard at **`http://127.0.0.1:8470`** and places a yellow Nikon **`N`** icon in your Windows system tray / macOS menu bar.
- For headless/server mode without a system tray icon:
  ```bash
  nikon-tracker start --headless --no-browser
  ```

---

## Building Standalone Executables Locally (Windows `.exe` / macOS `.app`)

You can also package the tracker locally using PyInstaller:

**Windows (PowerShell):**
```powershell
pyinstaller --noconfirm --onefile --windowed --name "NikonRefurbTracker-Windows" --collect-submodules uvicorn --collect-submodules pystray --add-data "src/nikon_tracker/web/static;nikon_tracker/web/static" --add-data "src/nikon_tracker/data;nikon_tracker/data" src/nikon_tracker/tray.py
```

**macOS (Terminal):**
```bash
pyinstaller --noconfirm --windowed --name "NikonRefurbTracker" --collect-submodules uvicorn --collect-submodules pystray --add-data "src/nikon_tracker/web/static:nikon_tracker/web/static" --add-data "src/nikon_tracker/data:nikon_tracker/data" src/nikon_tracker/tray.py
```

---

## Running Automated Tests

```bash
pytest -v
```

---

## License

MIT License — see [LICENSE](LICENSE). Not affiliated with or endorsed by Nikon Inc.
