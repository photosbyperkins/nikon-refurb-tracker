"""Command-line interface for NikonUSA Refurbished Tracker."""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn
from rich.console import Console
from rich.table import Table

from nikon_tracker.database import TrackerDatabase
from nikon_tracker.models import WatchlistEntry
from nikon_tracker.scheduler import TrackerScheduler
from nikon_tracker.scraper import parse_nikon_url
from nikon_tracker.tray import run_tray_app
from nikon_tracker.web.app import create_app


async def _run_check_cli(sub50_only: bool, show_oos: bool) -> int:
    console = Console()
    db = TrackerDatabase()
    scheduler = TrackerScheduler(db=db)
    scheduler.dispatcher.mute_desktop = True

    console.print(
        "[bold yellow]Scanning NikonUSA.com refurbished catalog & resolving brand-new MSRPs...[/bold yellow]"
    )
    snapshots, alerts = await scheduler.run_once()
    settings = db.get_settings()
    threshold = settings.global_discount_threshold_pct

    variants = db.get_all_variants()
    filtered = []
    for v in variants:
        if not show_oos and not v.is_on_stock:
            continue
        if sub50_only and (
            v.discount_pct_vs_msrp is None or v.discount_pct_vs_msrp < threshold
        ):
            continue
        filtered.append(v)

    filtered.sort(
        key=lambda x: (
            1 if x.is_on_stock else 0,
            x.discount_pct_vs_msrp if x.discount_pct_vs_msrp is not None else -999.0,
        ),
        reverse=True,
    )

    table = Table(
        title=(
            f"NikonUSA Refurbished Deals ({len(filtered)} shown • "
            f"Scan took {scheduler.last_poll_duration_ms}ms)"
        ),
        show_lines=False,
    )
    table.add_column("SKU", style="cyan", no_wrap=True)
    table.add_column("Product", style="bold white")
    table.add_column("Stock", justify="right")
    table.add_column("Refurb Price", justify="right", style="bold yellow")
    table.add_column("Reg Refurb", justify="right", style="dim")
    table.add_column("New MSRP", justify="right")
    table.add_column("% Off MSRP", justify="right")

    for v in filtered:
        stock_str = (
            f"[green]{v.available_quantity} in stock[/green]"
            if v.is_on_stock
            else "[red]OOS (0)[/red]"
        )
        msrp_str = f"${v.msrp_dollars:,.2f}" if v.msrp_dollars else "—"
        if v.discount_pct_vs_msrp is not None:
            if v.discount_pct_vs_msrp >= threshold:
                disc_str = f"[bold green]🔥 {v.discount_pct_vs_msrp:.1f}% OFF ({v.pct_of_msrp:.1f}%)[/bold green]"
            else:
                disc_str = f"[yellow]{v.discount_pct_vs_msrp:.1f}% off[/yellow]"
        else:
            disc_str = "—"

        table.add_row(
            v.sku,
            v.name,
            stock_str,
            f"${v.effective_price_dollars:,.2f}",
            f"${v.refurb_reg_dollars:,.2f}",
            msrp_str,
            disc_str,
        )

    console.print(table)
    if alerts:
        console.print(
            f"[bold green]Triggered {len(alerts)} new alert(s) during this scan![/bold green]"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nikon-tracker",
        description="NikonUSA Refurbished Tracker — Fast <50% MSRP & Restock Alert Engine",
    )
    subparsers = parser.add_subparsers(dest="command")

    # start command
    start_p = subparsers.add_parser(
        "start", help="Start the local web dashboard and system tray / background poller"
    )
    start_p.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    start_p.add_argument("--port", type=int, default=8470, help="Bind port (default: 8470)")
    start_p.add_argument(
        "--headless",
        action="store_true",
        help="Run FastAPI server + background poller in terminal without system tray",
    )
    start_p.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open the web dashboard in the default browser on launch",
    )

    # check command
    check_p = subparsers.add_parser(
        "check", help="Run an immediate one-shot scan of NikonUSA and print deals table"
    )
    check_p.add_argument(
        "--sub50",
        action="store_true",
        help="Only show deals at or below 50%% of official New MSRP",
    )
    check_p.add_argument(
        "--all",
        action="store_true",
        help="Include out-of-stock variants in output table",
    )

    # watch command
    watch_p = subparsers.add_parser(
        "watch", help="Add a NikonUSA refurbished product URL to your watchlist"
    )
    watch_p.add_argument(
        "url",
        help="NikonUSA refurbished URL (e.g. https://www.nikonusa.com/p/z-f-refurbished/1761Q/overview)",
    )
    watch_p.add_argument("--max-price", type=float, default=None, help="Target max USD price")
    watch_p.add_argument(
        "--min-discount", type=float, default=50.0, help="Target min %% off MSRP (default: 50)"
    )

    args = parser.parse_args(argv)

    if args.command == "check":
        return asyncio.run(_run_check_cli(sub50_only=args.sub50, show_oos=args.all))

    if args.command == "watch":
        parsed = parse_nikon_url(args.url)
        if not parsed:
            print("Error: Please provide a valid NikonUSA /p/<slug>/<sku> URL.", file=sys.stderr)
            return 1
        slug, key = parsed[0], parsed[1].upper()
        db = TrackerDatabase()
        entry = db.upsert_watchlist(
            WatchlistEntry(
                sku=key,
                slug=slug,
                product_key=key,
                label=f"Nikon Refurbished {key} ({slug})",
                max_price_dollars=args.max_price,
                min_discount_pct_msrp=args.min_discount,
            )
        )
        print(f"Added {entry.label} [{entry.sku}] to watchlist!")
        return 0

    # Default to "start"
    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 8470)
    headless = getattr(args, "headless", False)
    no_browser = getattr(args, "no_browser", False)

    if headless:
        db = TrackerDatabase()
        scheduler = TrackerScheduler(db=db)
        app = create_app(db=db, scheduler=scheduler, start_background_poller=True)
        print(f"NikonUSA Refurbished Tracker running at http://{host}:{port}")
        uvicorn.run(app, host=host, port=port, log_level="info")
    else:
        run_tray_app(host=host, port=port, open_browser=not no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
