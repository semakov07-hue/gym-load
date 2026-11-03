#!/usr/bin/env python3
"""Один замер для запуска в GitHub Actions. Токен берётся из секрета, а не из файла."""
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

URL = "https://mobifitness.ru/api/v8/clubs/5809.json"
JSON_PATH = "currentLoad"
CSV_FILE = Path("data/occupancy.csv")
# Часовой пояс зала: время в таблице записывается по нему
GYM_TZ = os.environ.get("GYM_TZ", "Asia/Yekaterinburg")

HEADERS = {
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


def main():
    try:
        r = requests.get(URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        count = int(extract(r.json(), JSON_PATH))
    except Exception as e:
        print(f"ошибка: {e}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(ZoneInfo(GYM_TZ)).replace(tzinfo=None)
    CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    new_file = not CSV_FILE.exists()
    with CSV_FILE.open("a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["timestamp", "count"])
        w.writerow([now.isoformat(timespec="seconds"), count])
    print(f"{now:%F %T} людей: {count}")


if __name__ == "__main__":
    main()
