#!/usr/bin/env python3
"""Один замер для GitHub Actions. Токены берутся из секретов, а не из файла.

Уведомление «Иди сейчас» в Telegram:
  NOTIFY_BELOW = ""   -> писать при каждом замере (режим проверки)
  NOTIFY_BELOW = "40" -> писать только в момент, когда людей стало 40 или меньше
                         (после того как было больше), и только в часы NOTIFY_HOURS
"""
import csv
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

URL = "https://mobifitness.ru/api/v8/clubs/5809.json"
JSON_PATH = "currentLoad"
CSV_FILE = Path("data/occupancy.csv")
GYM_TZ = os.environ.get("GYM_TZ", "Asia/Yekaterinburg")


def headers():
    return {
        "Authorization": "Bearer " + os.environ["FITNESS_TOKEN"],
        "X-CustomOS": "iOS",
        "X-CustomVersion": "5.24.6",
        "User-Agent": "IOS MobifitnessDemo/5.24.6 (production; build:303.20260924.1)",
        "Accept-Language": "ru-RU;q=1",
        "Accept": "*/*",
    }


def extract(data, path):
    for key in path.split("."):
        data = data[int(key)] if isinstance(data, list) else data[key]
    return data


def read_rows():
    """Прошлые замеры: список (datetime, count)."""
    if not CSV_FILE.exists():
        return []
    rows = []
    with CSV_FILE.open() as f:
        for r in csv.DictReader(f):
            try:
                rows.append((datetime.fromisoformat(r["timestamp"]), int(r["count"])))
            except (KeyError, ValueError):
                continue
    return rows


def typical_for(rows, now):
    """Медиана за тот же день недели и час; None, если замеров мало."""
    vals = [c for t, c in rows if t.weekday() == now.weekday() and t.hour == now.hour]
    return statistics.median(vals) if len(vals) >= 3 else None


def parse_hours(s, default=(7, 23)):
    try:
        a, b = s.split("-")
        return int(a), int(b)
    except Exception:
        return default


def should_notify(count, prev, limit, hour, hours):
    if limit == "":
        return True  # режим проверки: каждый замер
    if count > int(limit):
        return False
    if prev is not None and prev <= int(limit):
        return False  # уже было свободно: не повторяем
    return hours[0] <= hour <= hours[1]


def notify(count, now, typical):
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        return
    text = f"Свободно: в зале сейчас {count} чел. ({now:%H:%M})"
    if typical is not None:
        text += f". Обычно в это время ~{typical:.0f}"
    try:
        requests.post(
            f"https://api.telegram.org/bot{bot}/sendMessage",
            data={"chat_id": chat, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"Telegram: ошибка отправки: {e}", file=sys.stderr)


def main():
    try:
        r = requests.get(URL, headers=headers(), timeout=20)
        r.raise_for_status()
        count = int(extract(r.json(), JSON_PATH))
    except Exception as e:
        print(f"ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(ZoneInfo(GYM_TZ)).replace(tzinfo=None)
    rows = read_rows()
    prev = rows[-1][1] if rows else None
    typical = typical_for(rows, now)

    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "count"])
        w.writerow([now.isoformat(timespec="seconds"), count])
    print(f"{now:%F %T} людей: {count}")

    limit = os.environ.get("NOTIFY_BELOW", "").strip()
    hours = parse_hours(os.environ.get("NOTIFY_HOURS", "7-23"))
    if should_notify(count, prev, limit, now.hour, hours):
        notify(count, now, typical)


if __name__ == "__main__":
    main()
