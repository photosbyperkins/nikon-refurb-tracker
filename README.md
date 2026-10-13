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

## Features

- 🔥 **Sub-50% of MSRP Deal Detection**: Automatically highlights and alerts when any refurbished camera, kit, or lens drops to $\le 50\%$ of its true brand-new MSRP (configurable threshold).
- 📦 **Exact Warehouse Stock Counts**: Displays NikonUSA's live `availableQuantity` for every SKU.
- 🎯 **Custom Watchlist & 1-Click Presets**: Watch any NikonUSA refurbished URL or SKU with custom max price ($), min `% off MSRP`, and instant restock (`0 -> N`) triggers.
- 🔔 **Fast Multi-Channel Alerts**:
  - **Windows & macOS Native Desktop Notifications** + audible chime + optional auto-open product page in browser.
  - **Mobile Push via [ntfy.sh](https://ntfy.sh)** (free instant push notifications to iOS/Android with one-tap link to NikonUSA).
  - **Discord Rich Webhooks**, **Telegram Bot**, and **Pushover**.
- 🖥️ **Local Web Dashboard + System Tray Icon**: Runs quietly in your Windows system tray or macOS menu bar with a dark-mode local web UI at `http://127.0.0.1:8470`.
- 📈 **SQLite Price & Stock History**: Tracks all historical price drops, lowest-ever seen prices, and stock fluctuations over time.

---

## Quick Start (Windows & macOS)

### 1. Install from Source

Requires **Python 3.10+**.

```bash
git clone https://github.com/photosbyperkins/nikon-refurb-tracker.git
cd nikon-refurb-tracker
pip install -e ".[dev]"
```

### 2. Run a Fast Terminal Scan (`nikon-tracker check`)

```bash
# Scan all in-stock NikonUSA refurbished items and resolve true New MSRPs:
nikon-tracker check

# Show only hot deals priced at <= 50% of official New MSRP:
nikon-tracker check --sub50
```

### 3. Launch the System Tray App & Local Web Dashboard

```bash
nikon-tracker start
```

- Opens the dashboard at **`http://127.0.0.1:8470`** and places a yellow Nikon **`N`** icon in your Windows system tray / macOS menu bar.
- For headless/server mode without a system tray icon:
  ```bash
  nikon-tracker start --headless --no-browser
  ```

---

## Building Standalone Executables (Windows `.exe` / macOS `.app`)

You can package the tracker into a single standalone executable using PyInstaller:

**Windows (PowerShell):**
```powershell
pyinstaller --noconfirm --onefile --windowed --name "NikonRefurbTracker" --add-data "src/nikon_tracker/web/static;nikon_tracker/web/static" src/nikon_tracker/tray.py
```

**macOS (Terminal):**
```bash
pyinstaller --noconfirm --onefile --windowed --name "NikonRefurbTracker" --add-data "src/nikon_tracker/web/static:nikon_tracker/web/static" src/nikon_tracker/tray.py
```

---

## Running Automated Tests

```bash
pytest -v
```

---

## License

MIT License — see [LICENSE](LICENSE). Not affiliated with or endorsed by Nikon Inc.
