#!/usr/bin/env python3
"""Insider & superbelegger tracker.

  python main.py run        dashboard + automatische sync (aanbevolen)
  python main.py sync       eenmalig alles ophalen
  python main.py serve      alleen het dashboard
  python main.py test-alert stuur een test-melding
  python main.py export     statische site maken (GitHub Pages)
"""
import argparse
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tracker import alerts, sec  # noqa: E402
from tracker.db import DB  # noqa: E402
from tracker.server import serve  # noqa: E402
from tracker.sync import Syncer  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str) -> dict:
    if not os.path.exists(path):
        example = os.path.join(HERE, "config.example.json")
        sys.exit(f"Geen {path} gevonden. Kopieer {example} naar config.json en vul je naam + e-mail in.")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    # environment overrides (handig voor Docker)
    env = {"TRACKER_USER_AGENT": ("user_agent",), "TELEGRAM_BOT_TOKEN": ("notify", "telegram_bot_token"),
           "TELEGRAM_CHAT_ID": ("notify", "telegram_chat_id"), "NTFY_TOPIC": ("notify", "ntfy_topic"),
           "DASHBOARD_PASSWORD": ("dashboard_password",), "OPENFIGI_API_KEY": ("openfigi", "api_key")}
    for var, keys in env.items():
        if os.environ.get(var):
            d = cfg
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = os.environ[var]
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Insider & superbelegger tracker")
    ap.add_argument("command", choices=["run", "sync", "serve", "test-alert", "export"])
    ap.add_argument("--out", default=os.path.join(HERE, "_site"), help="map voor export")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--no-market", action="store_true", help="sla de marktbrede Form 4-scan over")
    ap.add_argument("--no-funds", action="store_true", help="sla 13F-fondsen over")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config(a.config)

    if a.command == "test-alert":
        alerts.send(cfg, "Test-alert Insider Tracker", "Als je dit ziet werken je meldingen.")
        return

    db_path = cfg.get("db_path", "data/tracker.db")
    if not os.path.isabs(db_path):
        db_path = os.path.join(HERE, db_path)
    db = DB(db_path)
    if a.command == "export":   # reads only the local database, no SEC access needed
        from tracker.export import export
        print(export(db, cfg, a.out))
        return
    try:
        client = sec.SecClient(cfg.get("user_agent", ""))
    except sec.SecError as e:
        sys.exit(str(e))
    syncer = Syncer(db, client, cfg)
    syncer.seed()

    if a.command == "sync":
        print(syncer.run(market=not a.no_market, funds=not a.no_funds))
        return

    if a.command == "run":
        interval = max(10, int(cfg.get("sync_interval_minutes", 60))) * 60

        def loop():
            while True:
                syncer.run(market=not a.no_market, funds=not a.no_funds)
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True).start()

    try:
        serve(db, syncer, cfg)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
