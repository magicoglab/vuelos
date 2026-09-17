#!/usr/bin/env python3
"""Vigila precios de vuelos con SerpAPI (Google Flights) y avisa por Telegram."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

import requests
import yaml

BASE_DIR = Path(__file__).resolve().parent
SERPAPI_URL = "https://serpapi.com/search"
CABIN_MAP = {
    "economy": 1,
    "premium_economy": 2,
    "premium": 2,
    "business": 3,
    "first": 4,
}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log(f"[warn] state {path} corrupto, se regenera")
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def fmt(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.0f}"


def check_key(check: dict) -> str:
    if check.get("name"):
        return str(check["name"])
    parts = [str(check["from"]).upper(), str(check["to"]).upper(), str(check["departure_date"])]
    if check.get("return_date"):
        parts.append(str(check["return_date"]))
    return "|".join(parts)


def build_params(api_key: str, check: dict, cfg: dict, defaults: dict) -> dict:
    cabin = str(check.get("cabin", defaults.get("cabin", "economy"))).lower()
    params = {
        "engine": "google_flights",
        "departure_id": str(check["from"]).upper(),
        "arrival_id": str(check["to"]).upper(),
        "outbound_date": str(check["departure_date"]),
        "currency": str(cfg.get("currency", "USD")).upper(),
        "hl": cfg.get("language", "es"),
        "gl": cfg.get("country", "ar"),
        "adults": int(check.get("adults", defaults.get("adults", 1))),
        "api_key": api_key,
    }
    if check.get("return_date"):
        params["type"] = 1
        params["return_date"] = str(check["return_date"])
    else:
        params["type"] = 2
    if cabin in CABIN_MAP:
        params["travel_class"] = CABIN_MAP[cabin]
    return params


def fetch(api_key: str, check: dict, cfg: dict, defaults: dict) -> dict:
    resp = requests.get(
        SERPAPI_URL, params=build_params(api_key, check, cfg, defaults), timeout=60
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    return data


def cheapest(data: dict) -> float | None:
    prices = []
    for key in ("best_flights", "other_flights"):
        for option in data.get(key) or []:
            price = option.get("price")
            if isinstance(price, (int, float)):
                prices.append(float(price))
    insights = data.get("price_insights") or {}
    lowest = insights.get("lowest_price")
    if isinstance(lowest, (int, float)):
        prices.append(float(lowest))
    return min(prices) if prices else None


def should_notify(
    prev: dict, price: float, threshold: float, drop_pct: float, cooldown_hours: float, now: dt.datetime
) -> tuple[bool, str]:
    if price > threshold:
        return False, "por encima del umbral"
    if not prev or prev.get("last_price") is None:
        return True, "primer aviso"
    last_price = float(prev["last_price"])
    if last_price > threshold:
        return True, "cruzo el umbral a la baja"
    if price <= last_price * (1 - drop_pct / 100):
        return True, f"bajo {drop_pct:g}% respecto al ultimo aviso"
    last_notified = prev.get("last_notified_at")
    if last_notified:
        try:
            elapsed = (now - dt.datetime.fromisoformat(last_notified)).total_seconds() / 3600
        except ValueError:
            elapsed = cooldown_hours
        if elapsed >= cooldown_hours:
            return True, f"pasaron {cooldown_hours:g}h desde el ultimo aviso"
    return False, "sin cambios relevantes"


def build_message(check: dict, price: float, prev_price: float | None, data: dict, cfg: dict) -> str:
    currency = str(cfg.get("currency", "USD")).upper()
    route = f"{str(check['from']).upper()} a {str(check['to']).upper()}"
    lines = [
        f"<b>Baja de precio: {route}</b>",
        f"Ida: {check['departure_date']}",
    ]
    if check.get("return_date"):
        lines.append(f"Vuelta: {check['return_date']}")
    lines.append(f"Precio actual: <b>{fmt(price, currency)}</b>")
    lines.append(f"Umbral: {fmt(float(check['threshold']), currency)}")
    if prev_price is not None:
        lines.append(f"Precio anterior: {fmt(float(prev_price), currency)}")
    insights = data.get("price_insights") or {}
    if insights.get("price_level"):
        lines.append(f"Nivel de precio: {insights['price_level']}")
    url = (data.get("search_metadata") or {}).get("google_flights_url")
    if url:
        lines.append(f'<a href="{url}">Ver en Google Flights</a>')
    return "\n".join(lines)


def send_telegram(token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {resp.status_code}: {resp.text[:300]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Vigila precios de vuelos y avisa por Telegram.")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yml"))
    parser.add_argument("--state", default=str(BASE_DIR / "state.json"))
    parser.add_argument("--dry-run", action="store_true", help="No envia Telegram, imprime por consola")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    state_path = Path(args.state)
    if not cfg_path.exists():
        log(f"[error] no existe {cfg_path}")
        return 2

    cfg = load_yaml(cfg_path)
    defaults = cfg.get("defaults") or {}
    checks = cfg.get("checks") or []
    if not checks:
        log("[error] no hay 'checks' definidos en el config")
        return 2

    api_key = os.environ.get("SERPAPI_API_KEY", "")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not args.dry_run and not api_key:
        log("[error] falta SERPAPI_API_KEY")
        return 2
    if not args.dry_run and not (token and chat_id):
        log("[error] faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return 2

    state = load_state(state_path)
    now = dt.datetime.now(dt.timezone.utc)
    currency = str(cfg.get("currency", "USD")).upper()
    failures = 0
    successes = 0

    for check in checks:
        key = check_key(check)
        label = check.get("name") or key
        if "threshold" not in check:
            log(f"[skip] {label}: falta 'threshold'")
            failures += 1
            continue

        try:
            data = fetch(api_key, check, cfg, defaults)
            price = cheapest(data)
            successes += 1
        except Exception as exc:  # noqa: BLE001
            log(f"[error] {label}: {exc}")
            failures += 1
            continue

        prev = state.get(key) or {}
        prev_price = prev.get("last_price")
        threshold = float(check["threshold"])
        drop_pct = float(check.get("renotify_drop_pct", defaults.get("renotify_drop_pct", 5)))
        cooldown = float(check.get("renotify_hours", defaults.get("renotify_hours", 24)))

        if price is None:
            log(f"[sin datos] {label}")
            state[key] = {
                "last_price": prev_price,
                "last_checked_at": now.isoformat(),
                "last_notified_at": prev.get("last_notified_at"),
                "last_notified_price": prev.get("last_notified_price"),
            }
            continue

        log(f"[{label}] {fmt(price, currency)} (umbral {fmt(threshold, currency)})")
        notify, reason = should_notify(prev, price, threshold, drop_pct, cooldown, now)
        if notify:
            message = build_message(check, price, prev_price, data, cfg)
            log(f"  -> aviso ({reason})")
            if args.dry_run:
                log("  [dry-run] " + message.replace("\n", "\n  "))
            else:
                try:
                    send_telegram(token, chat_id, message)
                    prev["last_notified_at"] = now.isoformat()
                    prev["last_notified_price"] = price
                except Exception as exc:  # noqa: BLE001
                    log(f"  [error] Telegram: {exc}")
                    failures += 1
        else:
            log(f"  -> sin aviso ({reason})")

        state[key] = {
            "last_price": price,
            "last_checked_at": now.isoformat(),
            "last_notified_at": prev.get("last_notified_at"),
            "last_notified_price": prev.get("last_notified_price"),
        }

    save_state(state_path, state)
    log(f"Listo: {successes} ok, {failures} con problemas.")
    return 1 if successes == 0 and failures > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
