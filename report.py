#!/usr/bin/env python3
"""Отчёты в Telegram по загрузке зала.

  python report.py morning   - утренний прогноз на сегодня
  python report.py weekly    - итоги за 7 дней + лучшие окна на неделю + картинка

Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  - доступ к боту (секреты GitHub)
  GYM_TZ    - часовой пояс зала (по умолчанию Asia/Yekaterinburg)
  GYM_LAT, GYM_LON - координаты для погоды (по умолчанию Екатеринбург)
  FREE_HOURS - ваши свободные часы, формат "дни:часы,дни:часы",
               дни 0=пн ... 6=вс, часы включительно. Пример:
               "0-4:18-22,5-6:10-16" = пн-пт с 18:00 до 23:00, сб-вс с 10:00 до 17:00
  DRY_RUN   - если задано, сообщения печатаются, а не отправляются
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

CSV_FILE = Path("data/occupancy.csv")
GYM_TZ = os.environ.get("GYM_TZ", "Asia/Yekaterinburg")
LAT = float(os.environ.get("GYM_LAT", "56.84"))
LON = float(os.environ.get("GYM_LON", "60.61"))
FREE_HOURS = os.environ.get("FREE_HOURS", "").strip()
DRY_RUN = bool(os.environ.get("DRY_RUN"))

DAYS_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ---------- Telegram ----------
def send(text, photo=None, caption=""):
    if DRY_RUN:
        print("--- TELEGRAM ---")
        print(text)
        if photo:
            print(f"[картинка: {photo}] {caption}")
        return
    bot = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not bot or not chat:
        print("Нет секретов TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)
    base = f"https://api.telegram.org/bot{bot}"
    r = requests.post(f"{base}/sendMessage", data={"chat_id": chat, "text": text}, timeout=20)
    r.raise_for_status()
    if photo:
        with open(photo, "rb") as f:
            r = requests.post(
                f"{base}/sendPhoto",
                data={"chat_id": chat, "caption": caption[:1000]},
                files={"photo": f},
                timeout=40,
            )
        r.raise_for_status()


# ---------- Данные ----------
def load():
    df = pd.read_csv(CSV_FILE, parse_dates=["timestamp"])
    df["date"] = df["timestamp"].dt.normalize()
    df["dow"] = df["timestamp"].dt.dayofweek
    df["hour"] = df["timestamp"].dt.hour
    # среднее внутри каждого часа конкретного дня: замеры идут неравномерно
    hourly = df.groupby(["date", "dow", "hour"], as_index=False)["count"].mean()
    return df, hourly


def open_hours(prof):
    """Часы, когда зал явно работает: средняя загрузка не меньше 10% от пика (и не меньше 3)."""
    if prof.empty:
        return set()
    thr = max(3, 0.1 * prof.max())
    return {int(h) for h, v in prof.items() if v >= thr}


def day_profile(hourly, dow, today):
    h = hourly[hourly["date"] < today]
    sub = h[h["dow"] == dow]
    if sub["date"].nunique() >= 2:
        return sub.groupby("hour")["count"].mean(), ""
    return h.groupby("hour")["count"].mean(), " (пока по всем дням недели: данных мало)"


def parse_free(s):
    if not s:
        return None
    try:
        res = {d: set() for d in range(7)}
        for part in s.split(","):
            days, hrs = part.strip().split(":")
            d1, d2 = (map(int, days.split("-")) if "-" in days else (int(days), int(days)))
            h1, h2 = (map(int, hrs.split("-")) if "-" in hrs else (int(hrs), int(hrs)))
            for d in range(d1, d2 + 1):
                res[d] |= set(range(h1, h2 + 1))
        return res
    except Exception:
        print("FREE_HOURS не разобран, игнорирую", file=sys.stderr)
        return None


def best_windows(prof, allowed, k=2, width=2):
    cands = []
    for h in sorted(allowed):
        hrs = range(h, h + width)
        if all(x in allowed and x in prof.index for x in hrs):
            cands.append((sum(prof[x] for x in hrs) / width, h))
    cands.sort()
    chosen = []
    for val, h in cands:
        if all(abs(h - c) >= width for _, c in chosen):
            chosen.append((val, h))
        if len(chosen) == k:
            break
    return chosen


def fmt(h, width=2):
    return f"{h:02d}:00–{(h + width) % 24:02d}:00"


# ---------- Погода и праздники ----------
def weather_today():
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": LAT, "longitude": LON, "timezone": GYM_TZ, "forecast_days": 1,
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        },
        timeout=15,
    )
    r.raise_for_status()
    d = r.json()["daily"]
    return d["temperature_2m_min"][0], d["temperature_2m_max"][0], d["precipitation_sum"][0]


def weather_history(start, end):
    r = requests.get(
        "https://archive-api.open-meteo.com/v1/archive",
        params={
            "latitude": LAT, "longitude": LON, "timezone": GYM_TZ,
            "start_date": start.strftime("%Y-%m-%d"), "end_date": end.strftime("%Y-%m-%d"),
            "daily": "temperature_2m_mean,precipitation_sum",
        },
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()["daily"]
    w = pd.DataFrame({"temp": d["temperature_2m_mean"], "precip": d["precipitation_sum"]},
                     index=pd.to_datetime(d["time"]))
    return w.dropna()


def insights(hourly, today, ohours):
    """Что влияет на загрузку. Возвращает список строк (пусто, если данных мало)."""
    out = []
    try:
        h = hourly[(hourly["date"] < today) & (hourly["hour"].isin(ohours))]
        cnt = h.groupby("date")["hour"].nunique()
        full = cnt[cnt >= max(3, int(0.6 * len(ohours)))].index
        daily = h[h["date"].isin(full)].groupby("date")["count"].mean().to_frame("load")
        if len(daily) < 14:
            return out
        daily["dow"] = daily.index.dayofweek
        daily["ratio"] = daily["load"] / daily.groupby("dow")["load"].transform("mean")
    except Exception:
        return out

    try:
        w = weather_history(daily.index.min(), daily.index.max())
        d = daily.join(w, how="inner")
        if len(d) >= 14:
            rainy, dry = d[d["precip"] >= 1], d[d["precip"] < 1]
            if len(rainy) >= 3 and len(dry) >= 3:
                diff = (rainy["ratio"].mean() / dry["ratio"].mean() - 1) * 100
                if abs(diff) >= 5:
                    word = "меньше" if diff < 0 else "больше"
                    out.append(f"• В дни с осадками людей в среднем на {abs(diff):.0f}% {word} обычного ({len(rainy)} таких дн.)")
            r = d["temp"].corr(d["ratio"])
            if pd.notna(r) and abs(r) >= 0.3:
                out.append("• Чем теплее на улице, тем людей " + ("больше" if r > 0 else "меньше")
                           + f" (связь {'заметная' if abs(r) >= 0.5 else 'слабая'})")
    except Exception as e:
        print(f"погода недоступна: {e}", file=sys.stderr)

    try:
        import holidays
        hol = holidays.country_holidays("RU")
        daily["hol"] = [(t.date() in hol) and t.dayofweek < 5 for t in daily.index]
        a = daily[daily["hol"]]
        b = daily[(~daily["hol"]) & (daily["dow"] < 5)]
        if len(a) >= 2 and len(b) >= 3:
            diff = (a["ratio"].mean() / b["ratio"].mean() - 1) * 100
            if abs(diff) >= 5:
                word = "меньше" if diff < 0 else "больше"
                out.append(f"• В праздничные будни людей на {abs(diff):.0f}% {word}, чем в обычные")
    except Exception as e:
        print(f"праздники недоступны: {e}", file=sys.stderr)
    return out


# ---------- Отчёты ----------
def morning(df, hourly, now):
    today, dow = pd.Timestamp(now.date()), now.weekday()
    prof, note = day_profile(hourly, dow, today)
    ohours = open_hours(prof)
    ndays = df["date"].nunique()
    if not ohours:
        send(f"Доброе утро! Пока данных мало ({len(df)} замеров за {ndays} дн.), прогноз появится позже.")
        return

    lines = [f"Доброе утро! Сегодня {DAYS_FULL[dow]}.", "", f"Тише всего обычно{note}:"]
    for val, h in best_windows(prof, ohours, k=2):
        lines.append(f"• {fmt(h)}: около {val:.0f} чел.")

    free = parse_free(FREE_HOURS)
    if free and free[dow]:
        mine = best_windows(prof, ohours & free[dow], k=1)
        if mine:
            lines += ["", f"Из ваших свободных часов лучше всего {fmt(mine[0][1])} (~{mine[0][0]:.0f} чел.)"]
        else:
            lines += ["", "В ваши свободные часы зал, судя по данным, закрыт или данных мало."]

    peak = int(prof[list(ohours)].idxmax())
    lines += ["", f"Самое людное время: {peak:02d}:00 (~{prof[peak]:.0f} чел.)"]

    try:
        tmin, tmax, rain = weather_today()
        lines += ["", f"Погода: от {tmin:.0f}° до {tmax:.0f}°, осадки {rain:.1f} мм"]
    except Exception as e:
        print(f"прогноз погоды недоступен: {e}", file=sys.stderr)

    lines += ["", f"Основано на {ndays} дн. данных."]
    send("\n".join(lines))


def heatmap(hourly, path="heatmap.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    p = hourly.pivot_table(index="dow", columns="hour", values="count", aggfunc="mean")
    p = p.reindex(index=range(7), columns=range(24))
    fig, ax = plt.subplots(figsize=(11, 3.8))
    im = ax.imshow(p.values, aspect="auto", cmap="RdYlGn_r")
    ax.set_yticks(range(7))
    ax.set_yticklabels(DAYS_SHORT)
    ax.set_xticks(range(24))
    ax.set_xlabel("Час")
    ax.set_title("Средняя загрузка зала (зелёный = свободно)")
    fig.colorbar(im, label="человек")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def weekly(df, hourly, now):
    today = pd.Timestamp(now.date())
    start, prev_start = today - timedelta(days=6), today - timedelta(days=13)
    cur = hourly[(hourly["date"] >= start) & (hourly["date"] <= today)]
    prev = hourly[(hourly["date"] >= prev_start) & (hourly["date"] < start)]
    overall = hourly.groupby("hour")["count"].mean()
    ohours = open_hours(overall)
    if not ohours or cur.empty:
        send(f"Недельный отчёт: пока мало данных ({len(df)} замеров). Подождём ещё немного.")
        return

    def avg_open(x):
        y = x[x["hour"].isin(ohours)]
        return y["count"].mean() if not y.empty else None

    a, b = avg_open(cur), avg_open(prev)
    lines = [f"Итоги за 7 дней ({start:%d.%m}–{today:%d.%m})", ""]
    line = f"Средняя загрузка в рабочие часы: {a:.0f} чел."
    if b:
        line += f" ({(a - b) / b * 100:+.0f}% к предыдущей неделе)"
    lines.append(line)

    p = cur[cur["hour"].isin(ohours)].groupby(["dow", "hour"])["count"].mean()
    if not p.empty:
        (d1, h1), v1 = p.idxmax(), p.max()
        lines.append(f"Самый людный час: {DAYS_SHORT[d1]} {h1:02d}:00 (~{v1:.0f} чел.)")

    free = parse_free(FREE_HOURS)
    lines += ["", "Лучшие окна на неделю" + (" (в ваши свободные часы):" if free else ":")]
    for d in range(7):
        prof, _ = day_profile(hourly, d, today)
        allowed = open_hours(prof)
        if free:
            allowed &= free[d]
        best = best_windows(prof, allowed, k=1)
        if best:
            lines.append(f"• {DAYS_SHORT[d]} {fmt(best[0][1])}: ~{best[0][0]:.0f} чел.")
        else:
            lines.append(f"• {DAYS_SHORT[d]}: нет подходящих окон")

    ins = insights(hourly, today, ohours)
    if ins:
        lines += ["", "Что влияет на загрузку:"] + ins
        lines.append("(это наблюдения по вашим данным, не гарантия)")

    lines += ["", f"Основано на {df['date'].nunique()} дн. данных."]
    send("\n".join(lines), photo=heatmap(hourly), caption="Средняя загрузка по дням недели и часам")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode not in ("morning", "weekly"):
        print("режим: morning или weekly", file=sys.stderr)
        sys.exit(2)
    if not CSV_FILE.exists():
        send("Пока нет данных для отчёта.")
        return
    df, hourly = load()
    now = datetime.now(ZoneInfo(GYM_TZ)).replace(tzinfo=None)
    (morning if mode == "morning" else weekly)(df, hourly, now)


if __name__ == "__main__":
    main()
