"""Cross-platform System Tray application (Windows & macOS) for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Any

import uvicorn
from PIL import Image, ImageDraw, ImageFont

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.web.app import create_app

logger = logging.getLogger(__name__)


def create_nikon_tray_icon(size: int = 64) -> Image.Image:
    """Render a Nikon-inspired yellow & black 'N' icon using Pillow."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Rounded Nikon yellow background
    draw.rounded_rectangle(
        [(2, 2), (size - 3, size - 3)],
        radius=12,
        fill=(246, 211, 0, 255),
        outline=(20, 20, 20, 255),
        width=3,
    )
    # Draw bold 'N' in the center
    try:
        font = ImageFont.truetype("arialbd.ttf", int(size * 0.62))
    except Exception:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "N", font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(
        ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
        "N",
        fill=(12, 14, 20, 255),
        font=font,
    )
    return img


def run_tray_app(
    host: str = "127.0.0.1",
    port: int = 8470,
    open_browser: bool = True,
) -> None:
    """Launch the FastAPI dashboard + background poller and attach a Windows/macOS system tray icon."""
    # Prevent Uvicorn logging crash in PyInstaller --windowed mode where stdout/stderr are None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    db = TrackerDatabase()
    scheduler = TrackerScheduler(db=db)
    app = create_app(db=db, scheduler=scheduler, start_background_poller=True)

    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", log_config=None
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    dashboard_url = f"http://{host}:{port}"

    if open_browser:
        def _delayed_open() -> None:
            time.sleep(1.2)
            webbrowser.open(dashboard_url)

        threading.Thread(target=_delayed_open, daemon=True).start()

    try:
        import pystray
    except Exception:
        logger.info("pystray not available; running web server in foreground at %s", dashboard_url)
        server_thread.join()
        return

    def _post_bg(path: str, timeout: float = 10.0) -> None:
        def _worker() -> None:
            try:
                req = urllib.request.Request(f"{dashboard_url}{path}", method="POST")
                urllib.request.urlopen(req, timeout=timeout)
            except Exception as exc:
                logger.debug("Tray background request %s failed: %s", path, exc)

        threading.Thread(target=_worker, daemon=True).start()

    def on_open_dashboard(_icon: Any, _item: Any) -> None:
        webbrowser.open(dashboard_url)

    def on_check_now(_icon: Any, _item: Any) -> None:
        _post_bg("/api/check-now", timeout=35.0)

    def on_toggle_pause(_icon: Any, _item: Any) -> None:
        endpoint = "resume" if scheduler.is_paused else "pause"
        _post_bg(f"/api/{endpoint}", timeout=5.0)

    def on_toggle_mute(_icon: Any, _item: Any) -> None:
        _post_bg("/api/mute-desktop", timeout=5.0)

    def on_quit(icon: Any, _item: Any) -> None:
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Nikon Refurb Dashboard", on_open_dashboard, default=True),
        pystray.MenuItem("⚡ Check NikonUSA Now", on_check_now),
        pystray.MenuItem(
            lambda _item: "▶ Resume Polling" if scheduler.is_paused else "⏸ Pause Polling",
            on_toggle_pause,
        ),
        pystray.MenuItem(
            lambda _item: "🔔 Unmute Desktop Alerts"
            if scheduler.dispatcher.mute_desktop
            else "🔕 Mute Desktop Alerts",
            on_toggle_mute,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit NikonRefurbTracker", on_quit),
    )

    icon = pystray.Icon(
        "nikon_refurb_tracker",
        create_nikon_tray_icon(),
        "NikonUSA Refurbished Tracker",
        menu,
    )
    icon.run()


if __name__ == "__main__":
    run_tray_app()
