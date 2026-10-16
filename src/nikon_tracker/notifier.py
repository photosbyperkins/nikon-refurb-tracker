"""Rule evaluation and multi-channel alert dispatcher for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import asyncio
import html
import logging
import subprocess
import sys
import time
import webbrowser

import httpx

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import AlertEvent, TrackerSettings, VariantSnapshot, WatchlistEntry

logger = logging.getLogger(__name__)


def evaluate_variant_alerts(
    previous: VariantSnapshot | None,
    current: VariantSnapshot,
    settings: TrackerSettings,
    watchlist_by_sku: dict[str, WatchlistEntry],
    db: TrackerDatabase | None = None,
) -> list[AlertEvent]:
    """Evaluate a single refurbished variant against global and watchlist alert rules."""
    if not current.is_on_stock:
        return []

    events: list[AlertEvent] = []
    watch = watchlist_by_sku.get(current.sku.upper())
    prev_dollars = previous.effective_price_dollars if previous else None

    msrp_str = f"${current.msrp_dollars:,.2f}" if current.msrp_dollars else "N/A"
    disc_str = (
        f"{current.discount_pct_vs_msrp:.1f}% off MSRP ({current.pct_of_msrp:.1f}% of MSRP)"
        if current.discount_pct_vs_msrp is not None and current.pct_of_msrp is not None
        else "MSRP unknown"
    )
    qty_str = f"{current.available_quantity} in stock" if current.available_quantity > 0 else "In Stock"

    # 1. Watchlist Restock check
    if watch and watch.enabled and watch.notify_on_restock:
        was_out_of_stock = previous is not None and not previous.is_on_stock
        if was_out_of_stock:
            events.append(
                AlertEvent(
                    event_type="WATCHLIST_RESTOCK",
                    sku=current.sku,
                    name=current.name,
                    title=f"🚨 RESTOCK: {current.name} (${current.effective_price_dollars:,.2f})",
                    message=(
                        f"{current.name} [{current.sku}] is BACK IN STOCK on NikonUSA! "
                        f"Price: ${current.effective_price_dollars:,.2f} (MSRP {msrp_str} • {disc_str}) • {qty_str}."
                    ),
                    product_url=current.product_url,
                    image_url=current.image_url,
                    effective_price_dollars=current.effective_price_dollars,
                    msrp_dollars=current.msrp_dollars,
                    discount_pct_vs_msrp=current.discount_pct_vs_msrp,
                    available_quantity=current.available_quantity,
                    previous_price_dollars=prev_dollars,
                    auto_open_browser=watch.auto_open_browser,
                )
            )

    # 2. Watchlist Target Price or Target % Off MSRP check
    watchlist_price_triggered = False
    if watch and watch.enabled:
        hit_price = (
            watch.max_price_dollars is not None
            and current.effective_price_dollars <= watch.max_price_dollars
        )
        hit_pct = (
            watch.min_discount_pct_msrp is not None
            and current.discount_pct_vs_msrp is not None
            and current.discount_pct_vs_msrp >= watch.min_discount_pct_msrp
        )
        if hit_price or hit_pct:
            watchlist_price_triggered = True
            reason = (
                f"<= target ${watch.max_price_dollars:,.2f}"
                if hit_price and watch.max_price_dollars is not None
                else f">= {watch.min_discount_pct_msrp:.0f}% off MSRP"
            )
            events.append(
                AlertEvent(
                    event_type="WATCHLIST_PRICE",
                    sku=current.sku,
                    name=current.name,
                    title=f"🎯 Watchlist Deal: {current.name} @ ${current.effective_price_dollars:,.2f}",
                    message=(
                        f"Watchlisted {current.name} [{current.sku}] hit your rule ({reason})! "
                        f"Now ${current.effective_price_dollars:,.2f} vs {msrp_str} MSRP ({disc_str}) • {qty_str}."
                    ),
                    product_url=current.product_url,
                    image_url=current.image_url,
                    effective_price_dollars=current.effective_price_dollars,
                    msrp_dollars=current.msrp_dollars,
                    discount_pct_vs_msrp=current.discount_pct_vs_msrp,
                    available_quantity=current.available_quantity,
                    previous_price_dollars=prev_dollars,
                    auto_open_browser=watch.auto_open_browser,
                )
            )

    # 3. Global Sub-50% of MSRP (or custom global_discount_threshold_pct) Hot Deal check
    threshold = settings.global_discount_threshold_pct
    if (
        not watchlist_price_triggered
        and current.discount_pct_vs_msrp is not None
        and current.discount_pct_vs_msrp >= threshold
    ):
        events.append(
            AlertEvent(
                event_type="SUB_50_MSRP",
                sku=current.sku,
                name=current.name,
                title=(
                    f"🔥 {current.discount_pct_vs_msrp:.0f}% OFF MSRP: {current.name} "
                    f"(${current.effective_price_dollars:,.2f})"
                ),
                message=(
                    f"{current.name} [{current.sku}] is {current.pct_of_msrp:.1f}% of New MSRP! "
                    f"Refurb Sale: ${current.effective_price_dollars:,.2f} | "
                    f"New MSRP: {msrp_str} (Save ${current.savings_vs_msrp_dollars or 0:,.2f}) • {qty_str}."
                ),
                product_url=current.product_url,
                image_url=current.image_url,
                effective_price_dollars=current.effective_price_dollars,
                msrp_dollars=current.msrp_dollars,
                discount_pct_vs_msrp=current.discount_pct_vs_msrp,
                available_quantity=current.available_quantity,
                previous_price_dollars=prev_dollars,
                auto_open_browser=settings.auto_open_browser_on_sub50
                or bool(watch and watch.auto_open_browser),
            )
        )

    # 4. Extra Refurbished Flash Sale Drop check
    elif settings.alert_on_any_extra_refurb_sale and previous is not None:
        price_drop_ratio = (
            (previous.refurb_sale_cents - current.refurb_sale_cents) / previous.refurb_sale_cents
            if previous.refurb_sale_cents > 0
            else 0.0
        )
        new_flash_promo = current.is_extra_refurb_sale and not previous.is_extra_refurb_sale
        if price_drop_ratio >= 0.05 or new_flash_promo:
            events.append(
                AlertEvent(
                    event_type="FLASH_SALE_DROP",
                    sku=current.sku,
                    name=current.name,
                    title=(
                        f"⚡ Price Drop: {current.name} now ${current.effective_price_dollars:,.2f}"
                    ),
                    message=(
                        f"{current.name} [{current.sku}] dropped from "
                        f"${previous.effective_price_dollars:,.2f} to ${current.effective_price_dollars:,.2f} "
                        f"(MSRP {msrp_str} • {disc_str}) • {qty_str}."
                    ),
                    product_url=current.product_url,
                    image_url=current.image_url,
                    effective_price_dollars=current.effective_price_dollars,
                    msrp_dollars=current.msrp_dollars,
                    discount_pct_vs_msrp=current.discount_pct_vs_msrp,
                    available_quantity=current.available_quantity,
                    previous_price_dollars=prev_dollars,
                    auto_open_browser=bool(watch and watch.auto_open_browser),
                )
            )

    # Deduplicate against recent alerts_log in SQLite
    filtered: list[AlertEvent] = []
    for ev in events:
        if db is not None and db.has_recent_alert(
            ev.sku,
            ev.event_type,
            ev.effective_price_dollars,
            settings.alert_cooldown_minutes,
        ):
            continue
        filtered.append(ev)

    return filtered


class AlertDispatcher:
    """Dispatches AlertEvents across Desktop, ntfy.sh, Discord, Telegram, and Pushover."""

    def __init__(
        self,
        db: TrackerDatabase | None = None,
        sound_debounce_seconds: float = 15.0,
    ) -> None:
        self.db = db
        self.mute_desktop: bool = False
        self.sound_debounce_seconds: float = sound_debounce_seconds
        self._last_sound_at: float = 0.0

    def _should_play_sound(self, settings: TrackerSettings) -> bool:
        """Return True if desktop sound is enabled and hasn't played within sound_debounce_seconds."""
        if not settings.desktop_sound_enabled or self.mute_desktop:
            return False
        now = time.monotonic()
        if self._last_sound_at > 0.0 and (now - self._last_sound_at) < self.sound_debounce_seconds:
            return False
        self._last_sound_at = now
        return True

    async def dispatch_batch(
        self, alerts: list[AlertEvent], settings: TrackerSettings
    ) -> list[AlertEvent]:
        """Dispatch a batch of alerts from a single poll cycle without spamming desktop popups or chimes."""
        if not alerts:
            return []

        if len(alerts) <= 2:
            dispatched: list[AlertEvent] = []
            for ev in alerts:
                dispatched.append(await self.dispatch(ev, settings))
            return dispatched

        # More than 2 alerts in a single poll cycle (e.g., initial launch or sitewide flash sale):
        # Send ONE consolidated desktop notification + ONE debounced chime, while logging all alerts.
        desktop_ok = False
        if settings.desktop_notifications_enabled and not self.mute_desktop:
            top = alerts[0]
            summary_alert = AlertEvent(
                event_type=top.event_type,
                sku=top.sku,
                name=top.name,
                title=f"🔥 {len(alerts)} Nikon Refurbished Deals Found!",
                message=(
                    f"Top deal: {top.name} (${top.effective_price_dollars:,.2f}) "
                    f"+ {len(alerts) - 1} more deals. Open dashboard to view all."
                ),
                product_url=top.product_url,
                effective_price_dollars=top.effective_price_dollars,
                msrp_dollars=top.msrp_dollars,
                discount_pct_vs_msrp=top.discount_pct_vs_msrp,
                available_quantity=top.available_quantity,
            )
            res = await self._send_desktop(summary_alert, settings)
            desktop_ok = res == "desktop"

        browser_opened = False
        dispatched_batch: list[AlertEvent] = []
        for ev in alerts:
            # Dispatch remote webhooks (if enabled) without re-triggering desktop toast per item
            channels_sent: list[str] = []
            if desktop_ok:
                channels_sent.append("desktop")

            tasks: list[asyncio.Task[str | None]] = []
            if settings.ntfy_enabled and settings.ntfy_topic.strip():
                tasks.append(asyncio.create_task(self._send_ntfy(ev, settings)))
            if settings.discord_enabled and settings.discord_webhook_url.strip():
                tasks.append(asyncio.create_task(self._send_discord(ev, settings)))
            if (
                settings.telegram_enabled
                and settings.telegram_bot_token.strip()
                and settings.telegram_chat_id.strip()
            ):
                tasks.append(asyncio.create_task(self._send_telegram(ev, settings)))
            if (
                settings.pushover_enabled
                and settings.pushover_user_key.strip()
                and settings.pushover_api_token.strip()
            ):
                tasks.append(asyncio.create_task(self._send_pushover(ev, settings)))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, str) and r:
                        channels_sent.append(r)

            # Cap auto-opening browser tabs to at most 1 tab per poll batch
            if ev.auto_open_browser and ev.product_url and not browser_opened:
                try:
                    webbrowser.open(ev.product_url)
                    channels_sent.append("browser_open")
                    browser_opened = True
                except Exception as exc:
                    logger.warning("Failed to auto-open browser: %s", exc)

            ev.channels_sent = channels_sent
            if self.db is not None:
                self.db.log_alert(ev)
            dispatched_batch.append(ev)

        return dispatched_batch

    async def dispatch(self, alert: AlertEvent, settings: TrackerSettings) -> AlertEvent:
        """Send an alert across all enabled channels and record it in the database."""
        channels_sent: list[str] = []
        tasks: list[asyncio.Task[str | None]] = []

        if settings.desktop_notifications_enabled and not self.mute_desktop:
            tasks.append(
                asyncio.create_task(self._send_desktop(alert, settings))
            )

        if settings.ntfy_enabled and settings.ntfy_topic.strip():
            tasks.append(
                asyncio.create_task(self._send_ntfy(alert, settings))
            )

        if settings.discord_enabled and settings.discord_webhook_url.strip():
            tasks.append(
                asyncio.create_task(self._send_discord(alert, settings))
            )

        if (
            settings.telegram_enabled
            and settings.telegram_bot_token.strip()
            and settings.telegram_chat_id.strip()
        ):
            tasks.append(
                asyncio.create_task(self._send_telegram(alert, settings))
            )

        if (
            settings.pushover_enabled
            and settings.pushover_user_key.strip()
            and settings.pushover_api_token.strip()
        ):
            tasks.append(
                asyncio.create_task(self._send_pushover(alert, settings))
            )

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, str) and res:
                    channels_sent.append(res)

        if alert.auto_open_browser and alert.product_url:
            try:
                webbrowser.open(alert.product_url)
                channels_sent.append("browser_open")
            except Exception as exc:
                logger.warning("Failed to auto-open browser: %s", exc)

        alert.channels_sent = channels_sent
        if self.db is not None:
            self.db.log_alert(alert)
        return alert

    async def _send_desktop(
        self, alert: AlertEvent, settings: TrackerSettings
    ) -> str | None:
        """Trigger native OS notification and debounced sound on Windows or macOS."""
        play_sound = self._should_play_sound(settings)
        try:
            await asyncio.to_thread(
                self._send_desktop_sync,
                alert.title,
                alert.message,
                play_sound,
            )
            return "desktop"
        except Exception as exc:
            logger.warning("Desktop notification failed: %s", exc)
            return None

    @staticmethod
    def _send_desktop_sync(title: str, message: str, play_sound: bool) -> None:
        if sys.platform == "win32":
            if play_sound:
                try:
                    import winsound

                    winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                except Exception:
                    pass
            # Escape single quotes for PowerShell XML toast notification
            safe_title = (
                title.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("'", "''")
            )
            safe_msg = (
                message.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("'", "''")
            )
            ps_script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                "$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                f"$xml.LoadXml('<toast><visual><binding template=\"ToastText02\"><text id=\"1\">{safe_title}</text><text id=\"2\">{safe_msg}</text></binding></visual></toast>'); "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('NikonUSA Refurbished Tracker').Show($toast);"
            )
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                timeout=6,
                creationflags=creationflags,
            )
        elif sys.platform == "darwin":
            safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
            sound_clause = ' sound name "Glass"' if play_sound else ""
            script = f'display notification "{safe_msg}" with title "{safe_title}"{sound_clause}'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)

    async def _send_ntfy(
        self, alert: AlertEvent, settings: TrackerSettings
    ) -> str | None:
        base = settings.ntfy_server_url.rstrip("/")
        topic = settings.ntfy_topic.strip().lstrip("/")
        url = f"{base}/{topic}"
        headers = {
            "Title": alert.title.encode("ascii", "ignore").decode("ascii") or "NikonUSA Refurb Deal",
            "Priority": "high" if alert.event_type in ("SUB_50_MSRP", "WATCHLIST_RESTOCK") else "default",
            "Tags": "camera,fire",
            "Click": alert.product_url,
            "Actions": f"view, Open on NikonUSA, {alert.product_url}",
        }
        if settings.ntfy_token.strip():
            headers["Authorization"] = f"Bearer {settings.ntfy_token.strip()}"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=alert.message.encode("utf-8"), headers=headers)
                resp.raise_for_status()
            return "ntfy"
        except Exception as exc:
            logger.warning("ntfy notification failed: %s", exc)
            return None

    async def _send_discord(
        self, alert: AlertEvent, settings: TrackerSettings
    ) -> str | None:
        color = 0x10B981 if alert.event_type == "SUB_50_MSRP" else 0xF6D300
        fields = [
            {
                "name": "Refurb Price",
                "value": f"**${alert.effective_price_dollars:,.2f}**",
                "inline": True,
            },
            {
                "name": "Official New MSRP",
                "value": f"${alert.msrp_dollars:,.2f}" if alert.msrp_dollars else "N/A",
                "inline": True,
            },
            {
                "name": "Discount vs MSRP",
                "value": f"**{alert.discount_pct_vs_msrp:.1f}% OFF**"
                if alert.discount_pct_vs_msrp is not None
                else "N/A",
                "inline": True,
            },
            {
                "name": "SKU",
                "value": f"`{alert.sku}`",
                "inline": True,
            },
            {
                "name": "Stock Quantity",
                "value": f"{alert.available_quantity} units",
                "inline": True,
            },
        ]
        embed: dict[str, object] = {
            "title": alert.title,
            "url": alert.product_url,
            "description": alert.message,
            "color": color,
            "fields": fields,
            "footer": {"text": "NikonUSA Refurbished Tracker"},
        }
        if alert.image_url:
            embed["thumbnail"] = {"url": alert.image_url}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    settings.discord_webhook_url.strip(),
                    json={"embeds": [embed]},
                )
                resp.raise_for_status()
            return "discord"
        except Exception as exc:
            logger.warning("Discord webhook notification failed: %s", exc)
            return None

    async def _send_telegram(
        self, alert: AlertEvent, settings: TrackerSettings
    ) -> str | None:
        api_url = f"https://api.telegram.org/bot{settings.telegram_bot_token.strip()}/sendMessage"
        safe_title = html.escape(alert.title)
        safe_msg = html.escape(alert.message)
        safe_url = html.escape(alert.product_url, quote=True)
        text = (
            f"<b>{safe_title}</b>\n\n"
            f"{safe_msg}\n\n"
            f"🔗 <a href=\"{safe_url}\">View on NikonUSA.com</a>"
        )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    api_url,
                    json={
                        "chat_id": settings.telegram_chat_id.strip(),
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": False,
                    },
                )
                resp.raise_for_status()
            return "telegram"
        except Exception as exc:
            logger.warning("Telegram notification failed: %s", exc)
            return None

    async def _send_pushover(
        self, alert: AlertEvent, settings: TrackerSettings
    ) -> str | None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.pushover.net/1/messages.json",
                    data={
                        "token": settings.pushover_api_token.strip(),
                        "user": settings.pushover_user_key.strip(),
                        "title": alert.title,
                        "message": alert.message,
                        "url": alert.product_url,
                        "url_title": "View on NikonUSA",
                        "priority": "1" if alert.event_type == "SUB_50_MSRP" else "0",
                    },
                )
                resp.raise_for_status()
            return "pushover"
        except Exception as exc:
            logger.warning("Pushover notification failed: %s", exc)
            return None
