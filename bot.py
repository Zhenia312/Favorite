import os
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta

# ── КОНФІГ ────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API_KEY    = os.environ.get("API_KEY", "")

POLL_INTERVAL = 300

KYIV_TZ = timezone(timedelta(hours=3))

def now_kyiv():
    return datetime.now(timezone.utc).astimezone(KYIV_TZ)

# ── ФУТБОЛ ────────────────────────────────────────────────────────────────
FAV_THRESHOLD_FOOT = 2.20
MIN_ODDS_RISE_FOOT = 20
MAX_MINUTE_FOOT    = 85
FOOTBALL_LEAGUES   = {
    253: "🇺🇸 MLS",
    71:  "🇧🇷 Бразилія",
    32:  "🌍 Відбір ЧС 2026",
    98:  "🇯🇵 J-League",
    188: "🇦🇺 A-League",
    262: "🇲🇽 Ліга МХ",
    128: "🇦🇷 Аргентина",
    9:   "🤝 Тов. збірні",
    667: "🤝 Тов. клуби",
}

# ── БАСКЕТБОЛ ─────────────────────────────────────────────────────────────
FAV_THRESHOLD_BASK = 1.90
MIN_POINTS_BEHIND  = 6
BASKETBALL_LEAGUES = {
    12:  "🏀 NBA",
    120: "🏀 Євроліга",
    117: "🏀 NCAA",
}

# ── ТЕНІС ─────────────────────────────────────────────────────────────────
FAV_THRESHOLD_TEN = 2.00
MIN_ODDS_RISE_TEN = 30
TENNIS_LEAGUES    = {
    1: "🎾 ATP",
    2: "🎾 WTA",
    3: "🎾 Grand Slam",
    4: "🎾 Challenger",
}

# ── ХОКЕЙ ─────────────────────────────────────────────────────────────────
FAV_THRESHOLD_HOCK    = 2.00
MIN_ODDS_RISE_HOCK    = 25
MIN_GOALS_BEHIND_HOCK = 1
HOCKEY_LEAGUES        = {
    57:  "🏒 NHL",
    92:  "🏒 KHL",
    96:  "🏒 Ліга чемпіонів",
    112: "🏒 AHL",
}

# ── СТАН ──────────────────────────────────────────────────────────────────
notified   = set()
pre_odds   = {}
is_running = True
offset     = 0

sports_enabled = {
    "football":   True,
    "basketball": True,
    "tennis":     True,
    "hockey":     True,
}

leagues_enabled = {
    "football":   {lid: True for lid in FOOTBALL_LEAGUES},
    "basketball": {lid: True for lid in BASKETBALL_LEAGUES},
    "tennis":     {lid: True for lid in TENNIS_LEAGUES},
    "hockey":     {lid: True for lid in HOCKEY_LEAGUES},
}

# Режим "всі ліги API" — ігнорує фільтр ліг, запитує весь live
all_leagues_mode = {
    "football":   False,
    "basketball": False,
    "tennis":     False,
    "hockey":     False,
}

user_state = {"menu": None}

api_requests = {
    "football":   {"used": 0, "limit": 100},
    "basketball": {"used": 0, "limit": 100},
    "tennis":     {"used": 0, "limit": 100},
    "hockey":     {"used": 0, "limit": 100},
}

stats = {
    "signals_total": 0,
    "scans_total":   0,
    "started_at":    now_kyiv().strftime("%H:%M %d.%m.%Y"),
    "last_signal":   None,
    "by_sport": {
        "⚽ Футбол":    0,
        "🏀 Баскетбол": 0,
        "🎾 Теніс":     0,
        "🏒 Хокей":     0,
    },
}

# ── КЛАВІАТУРИ ────────────────────────────────────────────────────────────
def main_keyboard():
    f = "✅" if sports_enabled["football"]   else "❌"
    b = "✅" if sports_enabled["basketball"] else "❌"
    t = "✅" if sports_enabled["tennis"]     else "❌"
    h = "✅" if sports_enabled["hockey"]     else "❌"
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}, {"text": "🔍 Діагностика"}],
            [{"text": f"{f} Футбол"}, {"text": f"{b} Баскетбол"}],
            [{"text": f"{t} Теніс"}, {"text": f"{h} Хокей"}],
            [{"text": "⚙️ Ліги футбол"}, {"text": "⚙️ Ліги баскет"}],
            [{"text": "⚙️ Ліги теніс"}, {"text": "⚙️ Ліги хокей"}],
            [{"text": "📋 Ліги API"}, {"text": "📅 Розклад"}],
            [{"text": f"⏱ Інтервал: {POLL_INTERVAL // 60} хв"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }

def interval_keyboard():
    return {
        "keyboard": [
            [{"text": "1 хв"}, {"text": "2 хв"}, {"text": "3 хв"}],
            [{"text": "5 хв"}, {"text": "10 хв"}],
            [{"text": "🔙 Назад"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }

def leagues_keyboard(sport):
    if sport == "football":
        leagues = FOOTBALL_LEAGUES
    elif sport == "basketball":
        leagues = BASKETBALL_LEAGUES
    elif sport == "tennis":
        leagues = TENNIS_LEAGUES
    else:
        leagues = HOCKEY_LEAGUES

    all_mode = all_leagues_mode[sport]
    all_icon = "🌍✅" if all_mode else "🌍❌"

    rows = []
    items = list(leagues.items())
    for i in range(0, len(items), 2):
        row = []
        for lid, name in items[i:i+2]:
            icon = "✅" if leagues_enabled[sport][lid] else "❌"
            row.append({"text": f"{icon} {name}"})
        rows.append(row)
    rows.append([{"text": f"{all_icon} Всі ліги API"}])
    rows.append([{"text": "✅ Всі"}, {"text": "❌ Жодної"}])
    rows.append([{"text": "🔙 Назад"}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "persistent": True,
    }

# ── TELEGRAM ──────────────────────────────────────────────────────────────
async def send_msg(session, text, kb=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TG_CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown",
    }
    payload["reply_markup"] = kb if kb else main_keyboard()
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.post(url, json=payload, timeout=timeout) as r:
            return await r.json()
    except Exception as e:
        print(f"[TG ERROR] {e}")

async def get_updates(session):
    global offset
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with session.get(
            url,
            params={"offset": offset, "timeout": 3},
            timeout=timeout
        ) as r:
            data = await r.json()
            return data.get("result", [])
    except:
        return []

# ── ЛІЧИЛЬНИК ЗАПИТІВ ─────────────────────────────────────────────────────
_last_reset_day = now_kyiv().day
_quota_warned   = set()   # спорти, для яких вже надіслано попередження
_quota_paused   = set()   # спорти, для яких скан призупинено через 0 запитів

def reset_counters_if_needed():
    global _last_reset_day
    today = now_kyiv().day
    if today != _last_reset_day:
        for sport in api_requests:
            api_requests[sport]["used"] = 0
        had_paused = list(_quota_paused)
        _quota_warned.clear()
        _quota_paused.clear()
        _last_reset_day = today
        print(f"[{now_kyiv().strftime('%H:%M:%S')}] Лічильники запитів скинуто (новий день)")
        # Повідомлення надсилається асинхронно — тут тільки логуємо
        # (session недоступна в sync контексті, повідомлення в scan_loop)
        if had_paused:
            print(f"[QUOTA] Поновлено спорти: {had_paused}")

def track_request(sport):
    reset_counters_if_needed()
    api_requests[sport]["used"] += 1

def requests_left(sport):
    r = api_requests[sport]
    return max(0, r["limit"] - r["used"])

async def check_quota(session, sport, sport_label):
    """Повертає False якщо запитів не залишилось — скан треба пропустити."""
    left = requests_left(sport)
    if left == 0:
        if sport not in _quota_paused:
            _quota_paused.add(sport)
            now_str = now_kyiv().strftime("%H:%M")
            msg = (
                "🚫 *" + sport_label + ": запити вичерпано!*\n\n"
                "Ліміт 100 запитів на сьогодні витрачено.\n"
                "Скан *" + sport_label + "* призупинено до опівночі.\n"
                "⏰ Поновиться автоматично о 00:00 (Київ).\n"
                "🕐 Зараз: " + now_str
            )
            await send_msg(session, msg)
            print(f"[QUOTA] {sport} — запити вичерпано, скан призупинено")
        return False
    if left <= 20 and sport not in _quota_warned:
        _quota_warned.add(sport)
        msg = (
            "⚠️ *" + sport_label + ": залишилось мало запитів!*\n\n"
            "Залишок: *" + str(left) + "/100*\n"
            "Розглянь збільшення інтервалу або вимкнення спорту."
        )
        await send_msg(session, msg)
        print(f"[QUOTA] {sport} — попередження: залишилось {left} запитів")
    return True

# ── ОБРОБКА КОМАНД ────────────────────────────────────────────────────────
async def process_commands(session):
    global is_running, offset, POLL_INTERVAL
    updates = await get_updates(session)

    for upd in updates:
        offset = upd["update_id"] + 1
        raw  = upd.get("message", {}).get("text", "").strip()
        text = raw.lower()
        menu = user_state["menu"]

        # ── Режим вибору інтервалу ────────────────────────────────────────
        if menu == "set_interval":
            if text == "🔙 назад":
                user_state["menu"] = None
                await send_msg(session, "🏠 Головне меню")
                continue

            interval_map = {
                "1 хв": 1, "2 хв": 2, "3 хв": 3,
                "5 хв": 5, "10 хв": 10,
            }
            if text in interval_map:
                minutes = interval_map[text]
                POLL_INTERVAL = minutes * 60
                user_state["menu"] = None
                await send_msg(session, f"✅ *Інтервал змінено на {minutes} хв*")
            else:
                await send_msg(session, "Натисни одну з кнопок ⬇️", kb=interval_keyboard())
            continue

        # ── Режим вибору ліг ──────────────────────────────────────────────
        if menu in ["football_leagues", "basketball_leagues", "tennis_leagues", "hockey_leagues"]:
            sport = menu.replace("_leagues", "")

            if text == "🔙 назад":
                user_state["menu"] = None
                await send_msg(session, "🏠 Головне меню")
                continue

            # Кнопка "Всі ліги API" — toggle режиму без фільтра
            if "всі ліги api" in text:
                all_leagues_mode[sport] = not all_leagues_mode[sport]
                if all_leagues_mode[sport]:
                    await send_msg(session,
                        f"🌍 *Режим «Всі ліги API» увімкнено*\n"
                        f"Бот сканує всі live матчі що дає API без фільтра по лігах.\n"
                        f"⚠️ Витрачає 1 запит на скан замість {len(leagues_enabled[sport])}",
                        kb=leagues_keyboard(sport)
                    )
                else:
                    await send_msg(session,
                        f"🌍 *Режим «Всі ліги API» вимкнено*\n"
                        f"Повернено фільтр по обраних лігах.",
                        kb=leagues_keyboard(sport)
                    )
                continue

            if text in ["✅ всі", "всі"]:
                for lid in leagues_enabled[sport]:
                    leagues_enabled[sport][lid] = True
                await send_msg(session, "✅ Всі ліги увімкнено", kb=leagues_keyboard(sport))
                continue

            if text in ["❌ жодної", "жодної"]:
                for lid in leagues_enabled[sport]:
                    leagues_enabled[sport][lid] = False
                await send_msg(session, "❌ Всі ліги вимкнено", kb=leagues_keyboard(sport))
                continue

            if sport == "football":
                leagues = FOOTBALL_LEAGUES
            elif sport == "basketball":
                leagues = BASKETBALL_LEAGUES
            elif sport == "tennis":
                leagues = TENNIS_LEAGUES
            else:
                leagues = HOCKEY_LEAGUES

            matched = False
            for lid, name in leagues.items():
                clean_name = name.split(" ", 1)[-1].lower().strip()
                if clean_name in text:
                    leagues_enabled[sport][lid] = not leagues_enabled[sport][lid]
                    icon = "✅" if leagues_enabled[sport][lid] else "❌"
                    await send_msg(session, f"{icon} {name}", kb=leagues_keyboard(sport))
                    matched = True
                    break

            if not matched:
                await send_msg(session, "Натисни кнопку ліги", kb=leagues_keyboard(sport))
            continue

        # ── Головне меню ──────────────────────────────────────────────────
        if text in ["/start", "▶️ старт"]:
            is_running = True
            await send_msg(session, "▶️ *Сканування запущено!*")

        elif text in ["/stop", "⏹ стоп"]:
            is_running = False
            await send_msg(session, "⏹ *Сканування зупинено.*")

        elif text in ["/stat", "📊 статистика"]:
            await send_stat(session)

        elif "діагностика" in text:
            await send_diagnostics(session)

        elif "ліги api" in text:
            await send_leagues_info(session)

        elif "розклад" in text:
            await send_schedule(session)

        elif "інтервал" in text or text == "/interval":
            user_state["menu"] = "set_interval"
            await send_msg(session,
                f"⏱ *Поточний інтервал: {POLL_INTERVAL // 60} хв*\n\nВибери новий:",
                kb=interval_keyboard()
            )

        elif "ліги футбол" in text:
            user_state["menu"] = "football_leagues"
            await send_msg(session, "⚙️ *Ліги футболу:*", kb=leagues_keyboard("football"))

        elif "ліги баскет" in text:
            user_state["menu"] = "basketball_leagues"
            await send_msg(session, "⚙️ *Ліги баскетболу:*", kb=leagues_keyboard("basketball"))

        elif "ліги теніс" in text:
            user_state["menu"] = "tennis_leagues"
            await send_msg(session, "⚙️ *Ліги тенісу:*", kb=leagues_keyboard("tennis"))

        elif "ліги хокей" in text:
            user_state["menu"] = "hockey_leagues"
            await send_msg(session, "⚙️ *Ліги хокею:*", kb=leagues_keyboard("hockey"))

        elif "футбол" in text:
            sports_enabled["football"] = not sports_enabled["football"]
            icon = "✅" if sports_enabled["football"] else "❌"
            await send_msg(session, f"{icon} *Футбол {'увімкнено' if sports_enabled['football'] else 'вимкнено'}*")

        elif "баскетбол" in text:
            sports_enabled["basketball"] = not sports_enabled["basketball"]
            icon = "✅" if sports_enabled["basketball"] else "❌"
            await send_msg(session, f"{icon} *Баскетбол {'увімкнено' if sports_enabled['basketball'] else 'вимкнено'}*")

        elif "теніс" in text:
            sports_enabled["tennis"] = not sports_enabled["tennis"]
            icon = "✅" if sports_enabled["tennis"] else "❌"
            await send_msg(session, f"{icon} *Теніс {'увімкнено' if sports_enabled['tennis'] else 'вимкнено'}*")

        elif "хокей" in text:
            sports_enabled["hockey"] = not sports_enabled["hockey"]
            icon = "✅" if sports_enabled["hockey"] else "❌"
            await send_msg(session, f"{icon} *Хокей {'увімкнено' if sports_enabled['hockey'] else 'вимкнено'}*")


# ── РОЗКЛАД МАТЧІВ ────────────────────────────────────────────────────────
async def send_schedule(session):
    await send_msg(session, "📅 *Збираю розклад на сьогодні...*")

    today = now_kyiv().strftime("%Y-%m-%d")

    async def fetch_fixtures_today(url, sport):
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
                track_request(sport)
                return (await r.json()).get("response", [])
        except Exception:
            return []

    foot_fix  = await fetch_fixtures_today(
        f"https://v3.football.api-sports.io/fixtures?date={today}", "football")
    await asyncio.sleep(1)
    bask_fix  = await fetch_fixtures_today(
        f"https://v1.basketball.api-sports.io/games?date={today}", "basketball")
    await asyncio.sleep(1)
    hock_fix  = await fetch_fixtures_today(
        f"https://v1.hockey.api-sports.io/games?date={today}", "hockey")
    await asyncio.sleep(1)
    ten_fix   = await fetch_fixtures_today(
        f"https://v1.tennis.api-sports.io/games?date={today}", "tennis")

    def by_hour(fixtures, time_key):
        hours = {}
        for f in fixtures:
            try:
                t = f.get(time_key, {})
                if isinstance(t, dict):
                    dt_str = t.get("date") or t.get("time") or ""
                else:
                    dt_str = str(t)
                if "T" in dt_str:
                    utc_hour = int(dt_str[11:13])
                    kyiv_hour = (utc_hour + 3) % 24
                elif ":" in dt_str:
                    kyiv_hour = int(dt_str[:2])
                else:
                    continue
                hours[kyiv_hour] = hours.get(kyiv_hour, 0) + 1
            except Exception:
                continue
        return hours

    foot_hours = by_hour(foot_fix,  "fixture")
    bask_hours = by_hour(bask_fix,  "date")
    hock_hours = by_hour(hock_fix,  "date")
    ten_hours  = by_hour(ten_fix,   "date")

    def fmt_hours(hours_dict):
        if not hours_dict:
            return "  немає матчів"
        lines = []
        for h in sorted(hours_dict.keys()):
            lines.append(f"  {h:02d}:00 — {hours_dict[h]} матч(ів)")
        return "\n".join(lines)

    total = len(foot_fix) + len(bask_fix) + len(hock_fix) + len(ten_fix)

    lines = [
        f"📅 *Розклад матчів на сьогодні* ({today}, Київ)\n",
        f"⚽ Футбол ({len(foot_fix)} матчів):",
        fmt_hours(foot_hours),
        f"\n🏀 Баскетбол ({len(bask_fix)} матчів):",
        fmt_hours(bask_hours),
        f"\n🏒 Хокей ({len(hock_fix)} матчів):",
        fmt_hours(hock_hours),
        f"\n🎾 Теніс ({len(ten_fix)} матчів):",
        fmt_hours(ten_hours),
        f"\n📊 Всього сьогодні: *{total} матчів*",
        f"⚠️ Витрачено 4 запити",
    ]
    await send_msg(session, "\n".join(lines))

# ── ДОСТУПНІ ЛІГИ API ─────────────────────────────────────────────────────
async def send_leagues_info(session):
    await send_msg(session, "📋 *Запитую доступні ліги...*")

    async def fetch_leagues(url, sport):
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
                track_request(sport)
                data = await r.json()
                return data.get("response", [])
        except Exception:
            return []

    foot_data  = await fetch_leagues("https://v3.football.api-sports.io/leagues", "football")
    await asyncio.sleep(1)
    bask_data  = await fetch_leagues("https://v1.basketball.api-sports.io/leagues", "basketball")
    await asyncio.sleep(1)
    hock_data  = await fetch_leagues("https://v1.hockey.api-sports.io/leagues", "hockey")
    await asyncio.sleep(1)
    ten_data   = await fetch_leagues("https://v1.tennis.api-sports.io/leagues", "tennis")

    foot_live  = [l for l in foot_data  if l.get("seasons") and any(s.get("coverage", {}).get("fixtures", {}).get("live") for s in l.get("seasons", []))]
    bask_live  = [l for l in bask_data  if l.get("seasons") and any(s.get("coverage", {}).get("live") for s in l.get("seasons", []))]
    hock_live  = [l for l in hock_data  if l.get("seasons") and any(s.get("coverage", {}).get("live") for s in l.get("seasons", []))]
    ten_live   = [l for l in ten_data   if l.get("seasons") and any(s.get("coverage", {}).get("live") for s in l.get("seasons", []))]

    lines = [
        "📋 *Доступні ліги через ваш API ключ*\n",
        f"⚽ Футбол: *{len(foot_data)}* ліг загалом, з live: *{len(foot_live)}*",
        f"🏀 Баскетбол: *{len(bask_data)}* ліг загалом, з live: *{len(bask_live)}*",
        f"🏒 Хокей: *{len(hock_data)}* ліг загалом, з live: *{len(hock_live)}*",
        f"🎾 Теніс: *{len(ten_data)}* ліг загалом, з live: *{len(ten_live)}*",
        f"\n📊 Всього ліг: *{len(foot_data)+len(bask_data)+len(hock_data)+len(ten_data)}*",
        f"📡 Live ліг: *{len(foot_live)+len(bask_live)+len(hock_live)+len(ten_live)}*",
        f"\n⚠️ Витрачено 4 запити на перевірку",
    ]
    await send_msg(session, "\n".join(lines))

# ── ДІАГНОСТИКА ───────────────────────────────────────────────────────────
async def send_diagnostics(session):
    await send_msg(session, "🔍 *Перевіряю live матчі...*")

    async def count_live(url, sport):
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
                track_request(sport)
                data = await r.json()
                return len(data.get("response", []))
        except Exception:
            return -1

    foot  = await count_live("https://v3.football.api-sports.io/fixtures?live=all", "football")
    await asyncio.sleep(1)
    bask  = await count_live("https://v1.basketball.api-sports.io/games?live=all", "basketball")
    await asyncio.sleep(1)
    hock  = await count_live("https://v1.hockey.api-sports.io/games?live=all", "hockey")
    await asyncio.sleep(1)
    ten   = await count_live("https://v1.tennis.api-sports.io/games?live=all", "tennis")

    def fmt(n):
        if n == -1:
            return "❌ помилка"
        return f"{n} матчів"

    total = sum(x for x in [foot, bask, hock, ten] if x >= 0)

    lines = [
        "🔍 *Діагностика — Live матчі зараз*\n",
        f"⚽ Футбол: {fmt(foot)}",
        f"🏀 Баскетбол: {fmt(bask)}",
        f"🏒 Хокей: {fmt(hock)}",
        f"🎾 Теніс: {fmt(ten)}",
        f"\n📊 Всього в лайві: *{total} матчів*",
        f"\n📡 Запитів витрачено на діагностику: 4",
    ]
    await send_msg(session, "\n".join(lines))

# ── СТАТИСТИКА ────────────────────────────────────────────────────────────
async def send_stat(session):
    f_leagues = [n for lid, n in FOOTBALL_LEAGUES.items()   if leagues_enabled["football"][lid]]
    b_leagues = [n for lid, n in BASKETBALL_LEAGUES.items() if leagues_enabled["basketball"][lid]]
    t_leagues = [n for lid, n in TENNIS_LEAGUES.items()     if leagues_enabled["tennis"][lid]]
    h_leagues = [n for lid, n in HOCKEY_LEAGUES.items()     if leagues_enabled["hockey"][lid]]

    fl = requests_left("football")
    bl = requests_left("basketball")
    tl = requests_left("tennis")
    hl = requests_left("hockey")

    def league_line(sport, leagues_list):
        if all_leagues_mode[sport]:
            return "🌍 Всі ліги API"
        return ', '.join(leagues_list) if leagues_list else 'немає'

    lines = [
        "📊 *Статистика FavTracker*\n",
        f"🕐 Запущено: {stats['started_at']}",
        f"🔍 Сканів: {stats['scans_total']}",
        f"🚨 Сигналів: {stats['signals_total']}",
        f"⚡ Статус: {'▶️ Активний' if is_running else '⏹ Зупинений'}",
        f"⏱ Інтервал: {POLL_INTERVAL // 60} хв\n",
        "🏆 *По видах спорту:*",
        f"  ⚽ Футбол: {stats['by_sport']['⚽ Футбол']} — {'✅' if sports_enabled['football'] else '❌'}",
        f"  🏀 Баскетбол: {stats['by_sport']['🏀 Баскетбол']} — {'✅' if sports_enabled['basketball'] else '❌'}",
        f"  🎾 Теніс: {stats['by_sport']['🎾 Теніс']} — {'✅' if sports_enabled['tennis'] else '❌'}",
        f"  🏒 Хокей: {stats['by_sport']['🏒 Хокей']} — {'✅' if sports_enabled['hockey'] else '❌'}\n",
        "📡 *Залишок запитів (сьогодні):*",
        f"  ⚽ Футбол: {fl}/100 {'⚠️' if fl < 20 else ''}",
        f"  🏀 Баскетбол: {bl}/100 {'⚠️' if bl < 20 else ''}",
        f"  🎾 Теніс: {tl}/100 {'⚠️' if tl < 20 else ''}",
        f"  🏒 Хокей: {hl}/100 {'⚠️' if hl < 20 else ''}\n",
        "📋 *Активні ліги:*",
        f"  ⚽ {league_line('football', f_leagues)}",
        f"  🏀 {league_line('basketball', b_leagues)}",
        f"  🎾 {league_line('tennis', t_leagues)}",
        f"  🏒 {league_line('hockey', h_leagues)}",
    ]
    if stats["last_signal"]:
        lines.append(f"\n📌 Останній: {stats['last_signal']}")
    await send_msg(session, "\n".join(lines))

def add_signal(sport_key, description):
    stats["signals_total"] += 1
    stats["last_signal"] = f"{description} ({now_kyiv().strftime('%H:%M')})"
    stats["by_sport"][sport_key] = stats["by_sport"].get(sport_key, 0) + 1

# ── ФУТБОЛ API ────────────────────────────────────────────────────────────
async def fetch_football_live(session, league_id=None):
    if league_id:
        url = f"https://v3.football.api-sports.io/fixtures?live=all&league={league_id}"
    else:
        url = "https://v3.football.api-sports.io/fixtures?live=all"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            return (await r.json()).get("response", [])
    except Exception:
        return []

async def fetch_prematch_odds_football(session, fixture_id):
    if fixture_id in pre_odds:
        return pre_odds[fixture_id]
    url = f"https://v3.football.api-sports.io/odds?fixture={fixture_id}&bookmaker=6"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            data = (await r.json()).get("response", [])
            print(f"    [ODDS⚽] fixture={fixture_id} response={len(data)} записів")
            if data:
                bookmakers = data[0].get("bookmakers", [])
                print(f"    [ODDS⚽] букмекерів: {len(bookmakers)}")
                if bookmakers:
                    bet_names = [b.get("name") for b in bookmakers[0].get("bets", [])]
                    print(f"    [ODDS⚽] типи ставок: {bet_names}")
                    for bet in bookmakers[0].get("bets", []):
                        if bet.get("name") == "Match Winner":
                            for v in bet.get("values", []):
                                if v.get("value") == "Home":
                                    odd = float(v.get("odd", 0))
                                    pre_odds[fixture_id] = odd
                                    print(f"    [ODDS⚽] ✅ знайдено odd={odd}")
                                    return odd
                    print(f"    [ODDS⚽] ❌ 'Match Winner' не знайдено")
            else:
                print(f"    [ODDS⚽] ❌ порожня відповідь (немає odds для цього матчу)")
    except Exception as e:
        print(f"    [ODDS⚽ ERROR] {e}")
    return None

# ── БАСКЕТБОЛ API ─────────────────────────────────────────────────────────
async def fetch_basketball_live(session, league_id=None):
    if league_id:
        url = f"https://v1.basketball.api-sports.io/games?league={league_id}&live=all"
    else:
        url = "https://v1.basketball.api-sports.io/games?live=all"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("basketball")
            return (await r.json()).get("response", [])
    except Exception:
        return []

# ── ТЕНІС API ─────────────────────────────────────────────────────────────
async def fetch_tennis_live(session, league_id=None):
    if league_id:
        url = f"https://v1.tennis.api-sports.io/games?league={league_id}&live=all"
    else:
        url = "https://v1.tennis.api-sports.io/games?live=all"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("tennis")
            return (await r.json()).get("response", [])
    except Exception:
        return []

async def fetch_prematch_odds_tennis(session, game_id):
    key = f"ten_odds_{game_id}"
    if key in pre_odds:
        return pre_odds[key]
    url = f"https://v1.tennis.api-sports.io/odds?game={game_id}&bookmaker=6"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("tennis")
            data = (await r.json()).get("response", [])
            if data:
                for bet in data[0].get("bookmakers", [{}])[0].get("bets", []):
                    if bet.get("name") == "Winner":
                        for v in bet.get("values", []):
                            if v.get("value") == "Home":
                                odd = float(v.get("odd", 0))
                                pre_odds[key] = odd
                                return odd
    except Exception:
        pass
    return None

# ── ХОКЕЙ API ─────────────────────────────────────────────────────────────
async def fetch_hockey_live(session, league_id=None):
    if league_id:
        url = f"https://v1.hockey.api-sports.io/games?league={league_id}&live=all"
    else:
        url = "https://v1.hockey.api-sports.io/games?live=all"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("hockey")
            return (await r.json()).get("response", [])
    except Exception:
        return []

async def fetch_prematch_odds_hockey(session, game_id):
    key = f"hock_odds_{game_id}"
    if key in pre_odds:
        return pre_odds[key]
    url = f"https://v1.hockey.api-sports.io/odds?game={game_id}&bookmaker=6"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("hockey")
            data = (await r.json()).get("response", [])
            if data:
                for bet in data[0].get("bookmakers", [{}])[0].get("bets", []):
                    if bet.get("name") in ["Match Winner", "Winner"]:
                        for v in bet.get("values", []):
                            if v.get("value") == "Home":
                                odd = float(v.get("odd", 0))
                                pre_odds[key] = odd
                                return odd
    except Exception:
        pass
    return None

# ── СИЛА СИГНАЛУ ──────────────────────────────────────────────────────────
def strength(rise, strong_rise=60, good_rise=40):
    if rise >= strong_rise:
        return "🔥 СИЛЬНИЙ"
    if rise >= good_rise:
        return "✅ ХОРОШИЙ"
    return "⚠️ СЛАБКИЙ"

# ── СКАНУВАННЯ ФУТБОЛ ─────────────────────────────────────────────────────
async def scan_football(session):
    if not sports_enabled["football"]:
        return
    if not await check_quota(session, "football", "⚽ Футбол"):
        return

    if all_leagues_mode["football"]:
        # Один запит — всі live матчі без фільтра
        fixtures = await fetch_football_live(session)
        await asyncio.sleep(1)
        league_map = {}  # збираємо назву ліги з відповіді API
        for fix in fixtures:
            league_id   = fix.get("league", {}).get("id")
            league_name = fix.get("league", {}).get("name", f"Ліга {league_id}")
            league_map[league_id] = league_name
        await _process_football_fixtures(session, fixtures, league_map)
    else:
        for league_id, league_name in FOOTBALL_LEAGUES.items():
            if not leagues_enabled["football"][league_id]:
                continue
            fixtures = await fetch_football_live(session, league_id)
            await asyncio.sleep(1)
            await _process_football_fixtures(session, fixtures, {league_id: league_name})

async def _process_football_fixtures(session, fixtures, league_map):
    print(f"  [⚽ СКАН] Матчів отримано: {len(fixtures)}")
    for fix in fixtures:
        fid        = fix["fixture"]["id"]
        league_id  = fix.get("league", {}).get("id")
        league_name = league_map.get(league_id, fix.get("league", {}).get("name", ""))
        minute     = fix["fixture"]["status"].get("elapsed") or 0
        home       = fix["teams"]["home"]["name"]
        away       = fix["teams"]["away"]["name"]
        score_h    = fix["goals"].get("home") or 0
        score_a    = fix["goals"].get("away") or 0

        print(f"  [⚽] {home} {score_h}:{score_a} {away} | хв={minute} | ліга={league_name}")

        if minute > MAX_MINUTE_FOOT:
            print(f"    → пропуск: хвилина {minute} > {MAX_MINUTE_FOOT}")
            continue

        pre_odd = await fetch_prematch_odds_football(session, fid)
        if not pre_odd:
            print(f"    → пропуск: odds не знайдено")
            continue
        if pre_odd >= FAV_THRESHOLD_FOOT:
            print(f"    → пропуск: pre_odd={pre_odd} >= порогу {FAV_THRESHOLD_FOOT}")
            continue
        # Сигнал 1: фаворит програє по рахунку
        valid_score = (
            (score_h == 0 and score_a == 1) or
            (score_h == 0 and score_a == 2) or
            (score_h == 1 and score_a == 2) or
            (score_h == 0 and score_a == 3) or
            (score_h == 1 and score_a == 3)
        )

        # Сигнал 2: рахунок 0:0 після першого тайму (46-85 хв)
        is_00_second_half = (score_h == 0 and score_a == 0 and 46 <= minute <= MAX_MINUTE_FOOT)

        if not valid_score and not is_00_second_half:
            print(f"    → пропуск: рахунок {score_h}:{score_a} не підходить і не 0:0 у 2-му таймі")
            continue

        live_odd = pre_odd
        for bet_block in fix.get("odds", []):
            for v in bet_block.get("values", []):
                if v.get("value") == "Home":
                    try:
                        live_odd = float(v["odd"])
                    except Exception:
                        pass

        rise = round(((live_odd - pre_odd) / pre_odd) * 100)
        print(f"    → pre_odd={pre_odd} live_odd={live_odd} rise={rise}% (мін={MIN_ODDS_RISE_FOOT}%)")
        if rise < MIN_ODDS_RISE_FOOT:
            print(f"    → пропуск: ріст {rise}% < мінімум {MIN_ODDS_RISE_FOOT}%")
            continue

        if is_00_second_half and not valid_score:
            key = f"foot_{fid}_00_2h"
            if key in notified:
                continue
            notified.add(key)
            add_signal("⚽ Футбол", f"{home} 0:0 {away} (2-й тайм)")
            msg = (
                f"🚨 *СИГНАЛ: ФАВОРИТ НЕ ЗАБИВАЄ*\n\n"
                f"⚽ {league_name}\n"
                f"*{home}* 0:0 *{away}*\n"
                f"⏱ Хвилина: {minute}' (2-й тайм)\n"
                f"📉 Коеф до матчу: `{pre_odd}`\n"
                f"📈 Коеф зараз: `{live_odd}` \\(+{rise}%\\)\n"
                f"💡 Фаворит без голів у другому таймі\n"
                f"💪 {strength(rise)}"
            )
            await send_msg(session, msg)
            print(f"  ⚽ СИГНАЛ 0:0: {home} vs {away} {minute}' +{rise}%")
        else:
            key = f"foot_{fid}_{score_h}_{score_a}"
            if key in notified:
                continue
            notified.add(key)
            add_signal("⚽ Футбол", f"{home} {score_h}:{score_a} {away}")
            msg = (
                f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                f"⚽ {league_name}\n"
                f"*{home}* {score_h}:{score_a} *{away}*\n"
                f"⏱ Хвилина: {minute}'\n"
                f"📉 Коеф до матчу: `{pre_odd}`\n"
                f"📈 Коеф зараз: `{live_odd}` \\(+{rise}%\\)\n"
                f"💪 {strength(rise)}"
            )
            await send_msg(session, msg)
            print(f"  ⚽ СИГНАЛ: {home} {score_h}:{score_a} {away} +{rise}%")

# ── СКАНУВАННЯ БАСКЕТБОЛ ──────────────────────────────────────────────────
async def scan_basketball(session):
    if not sports_enabled["basketball"]:
        return
    if not await check_quota(session, "basketball", "🏀 Баскетбол"):
        return

    if all_leagues_mode["basketball"]:
        games = await fetch_basketball_live(session)
        await asyncio.sleep(1)
        await _process_basketball_games(session, games, None)
    else:
        for league_id, league_name in BASKETBALL_LEAGUES.items():
            if not leagues_enabled["basketball"][league_id]:
                continue
            games = await fetch_basketball_live(session, league_id)
            await asyncio.sleep(1)
            await _process_basketball_games(session, games, league_name)

async def fetch_prematch_odds_basketball(session, game_id):
    key = f"bask_odds_{game_id}"
    if key in pre_odds:
        return pre_odds[key]
    url = f"https://v1.basketball.api-sports.io/odds?game={game_id}&bookmaker=6"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("basketball")
            data = (await r.json()).get("response", [])
            if data:
                for bet in data[0].get("bookmakers", [{}])[0].get("bets", []):
                    if bet.get("name") in ["Home/Away", "Match Winner", "Winner"]:
                        for v in bet.get("values", []):
                            if v.get("value") == "Home":
                                odd = float(v.get("odd", 0))
                                pre_odds[key] = odd
                                return odd
    except Exception:
        pass
    return None

async def _process_basketball_games(session, games, default_league_name):
    print(f"  [🏀 СКАН] Матчів отримано: {len(games)}")
    for game in games:
        gid         = game.get("id")
        league_name = default_league_name or game.get("league", {}).get("name", "")
        home        = game.get("teams", {}).get("home", {}).get("name", "")
        away        = game.get("teams", {}).get("away", {}).get("name", "")
        score_h     = game.get("scores", {}).get("home", {}).get("total") or 0
        score_a     = game.get("scores", {}).get("away", {}).get("total") or 0
        quarter     = game.get("status", {}).get("short", "")

        print(f"  [🏀] {home} {score_h}:{score_a} {away} | чверть={quarter}")

        if quarter not in ["Q2", "Q3", "Q4"]:
            print(f"    → пропуск: чверть {quarter} не підходить")
            continue

        diff = score_h - score_a
        if diff > -MIN_POINTS_BEHIND:
            print(f"    → пропуск: різниця {diff} (потрібно < -{MIN_POINTS_BEHIND})")
            continue

        pre_odd = await fetch_prematch_odds_basketball(session, gid)
        print(f"    → pre_odd={pre_odd}")
        if not pre_odd:
            print(f"    → пропуск: odds не знайдено")
            continue
        if pre_odd >= FAV_THRESHOLD_BASK:
            print(f"    → пропуск: pre_odd={pre_odd} >= порогу {FAV_THRESHOLD_BASK}")
            continue

        key = f"bask_{gid}_{score_h}_{score_a}"
        if key in notified:
            continue
        notified.add(key)
        add_signal("🏀 Баскетбол", f"{home} {score_h}:{score_a} {away}")

        msg = (
            f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
            f"🏀 {league_name}\n"
            f"*{home}* {score_h}:{score_a} *{away}*\n"
            f"📍 Чверть: {quarter}\n"
            f"📉 Коеф до матчу: `{pre_odd}`\n"
            f"📊 Різниця: {abs(diff)} очок\n"
            f"💪 {strength(abs(diff), 15, 10)}"
        )
        await send_msg(session, msg)
        print(f"  🏀 СИГНАЛ: {home} {score_h}:{score_a} {away} чв.{quarter}")

# ── СКАНУВАННЯ ТЕНІС ──────────────────────────────────────────────────────
async def scan_tennis(session):
    if not sports_enabled["tennis"]:
        return
    if not await check_quota(session, "tennis", "🎾 Теніс"):
        return

    if all_leagues_mode["tennis"]:
        games = await fetch_tennis_live(session)
        await asyncio.sleep(1)
        await _process_tennis_games(session, games, None)
    else:
        for league_id, league_name in TENNIS_LEAGUES.items():
            if not leagues_enabled["tennis"][league_id]:
                continue
            games = await fetch_tennis_live(session, league_id)
            await asyncio.sleep(1)
            await _process_tennis_games(session, games, league_name)

async def _process_tennis_games(session, games, default_league_name):
    for game in games:
        gid        = game.get("id")
        league_name = default_league_name or game.get("league", {}).get("name", "")
        home       = game.get("players", {}).get("home", {}).get("name", "")
        away       = game.get("players", {}).get("away", {}).get("name", "")
        sets_h     = game.get("scores", {}).get("home", {}).get("sets") or 0
        sets_a     = game.get("scores", {}).get("away", {}).get("sets") or 0

        if not (sets_h == 0 and sets_a == 1):
            continue

        pre_odd = await fetch_prematch_odds_tennis(session, gid)

        key = f"ten_{gid}_0_1"
        if key in notified:
            continue
        notified.add(key)
        add_signal("🎾 Теніс", f"{home} {sets_h}:{sets_a} {away}")

        if not pre_odd or pre_odd >= FAV_THRESHOLD_TEN:
            continue

        msg = (
            f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ СЕТ*\n\n"
            f"🎾 {league_name}\n"
            f"*{home}* {sets_h}:{sets_a} *{away}*\n"
            f"📍 Фаворит програв перший сет\n"
            f"📉 Коеф до матчу: `{pre_odd}`\n"
            f"💡 Перевір live коефіцієнт на букмекері"
        )
        await send_msg(session, msg)
        print(f"  🎾 СИГНАЛ: {home} {sets_h}:{sets_a} {away}")

# ── СКАНУВАННЯ ХОКЕЙ ──────────────────────────────────────────────────────
async def scan_hockey(session):
    if not sports_enabled["hockey"]:
        return
    if not await check_quota(session, "hockey", "🏒 Хокей"):
        return

    if all_leagues_mode["hockey"]:
        games = await fetch_hockey_live(session)
        await asyncio.sleep(1)
        await _process_hockey_games(session, games, None)
    else:
        for league_id, league_name in HOCKEY_LEAGUES.items():
            if not leagues_enabled["hockey"][league_id]:
                continue
            games = await fetch_hockey_live(session, league_id)
            await asyncio.sleep(1)
            await _process_hockey_games(session, games, league_name)

async def _process_hockey_games(session, games, default_league_name):
    print(f"  [🏒 СКАН] Матчів отримано: {len(games)}")
    for game in games:
        gid         = game.get("id")
        league_name = default_league_name or game.get("league", {}).get("name", "")
        home        = game.get("teams", {}).get("home", {}).get("name", "")
        away        = game.get("teams", {}).get("away", {}).get("name", "")
        score_h     = game.get("scores", {}).get("home") or 0
        score_a     = game.get("scores", {}).get("away") or 0
        period      = game.get("status", {}).get("short", "")

        print(f"  [🏒] {home} {score_h}:{score_a} {away} | період={period}")

        if period not in ["P1", "P2", "P3"]:
            print(f"    → пропуск: період {period} не підходить")
            continue

        pre_odd = await fetch_prematch_odds_hockey(session, gid)
        print(f"    → pre_odd={pre_odd}")
        if not pre_odd:
            print(f"    → пропуск: odds не знайдено")
            continue
        if pre_odd >= FAV_THRESHOLD_HOCK:
            print(f"    → пропуск: pre_odd={pre_odd} >= порогу {FAV_THRESHOLD_HOCK}")
            continue

        diff = score_h - score_a
        if diff > -MIN_GOALS_BEHIND_HOCK:
            print(f"    → пропуск: різниця {diff} (потрібно < -{MIN_GOALS_BEHIND_HOCK})")
            continue

        live_odd = pre_odd
        for bet_block in game.get("odds", []):
            for v in bet_block.get("values", []):
                if v.get("value") == "Home":
                    try:
                        live_odd = float(v["odd"])
                    except Exception:
                        pass

        rise = round(((live_odd - pre_odd) / pre_odd) * 100)
        if rise < MIN_ODDS_RISE_HOCK:
            continue

        key = f"hock_{gid}_{score_h}_{score_a}"
        if key in notified:
            continue
        notified.add(key)
        add_signal("🏒 Хокей", f"{home} {score_h}:{score_a} {away}")

        msg = (
            f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
            f"🏒 {league_name}\n"
            f"*{home}* {score_h}:{score_a} *{away}*\n"
            f"📍 Період: {period}\n"
            f"📉 Коеф до матчу: `{pre_odd}`\n"
            f"📈 Коеф зараз: `{live_odd}` \\(+{rise}%\\)\n"
            f"💪 {strength(rise)}"
        )
        await send_msg(session, msg)
        print(f"  🏒 СИГНАЛ: {home} {score_h}:{score_a} {away} +{rise}%")

# ── ГОЛОВНИЙ СКАН ─────────────────────────────────────────────────────────
async def scan(session):
    if not is_running:
        return
    stats["scans_total"] += 1
    print(f"[{now_kyiv().strftime('%H:%M:%S')}] Скан #{stats['scans_total']}...")
    await scan_football(session)
    await scan_basketball(session)
    await scan_tennis(session)
    await scan_hockey(session)
    print(f"  Сигналів всього: {stats['signals_total']}")

# ── MAIN ──────────────────────────────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  FavTracker Bot — Футбол + Баскетбол + Теніс + Хокей")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        await send_msg(session,
            "✅ *FavTracker запущено\\!*\n\n"
            "Відстежую:\n"
            "⚽ Футбол \\(MLS, Бразилія, Аргентина та ін\\.\\)\n"
            "🏀 Баскетбол \\(NBA, Євроліга\\)\n"
            "🎾 Теніс \\(ATP, WTA, Grand Slam\\)\n"
            "🏒 Хокей \\(NHL, KHL\\)\n\n"
            f"⏱ Скан кожні {POLL_INTERVAL // 60} хвилин"
        )

        async def command_loop():
            while True:
                try:
                    await asyncio.wait_for(process_commands(session), timeout=8)
                except asyncio.TimeoutError:
                    print("[CMD TIMEOUT] пропускаємо")
                except Exception as e:
                    print(f"[CMD ERROR] {e}")
                await asyncio.sleep(2)

        _prev_paused = set()

        async def scan_loop():
            nonlocal _prev_paused
            while True:
                try:
                    # Перевіряємо чи скинувся ліміт і були призупинені спорти
                    reset_counters_if_needed()
                    newly_resumed = _prev_paused - _quota_paused
                    if newly_resumed:
                        sport_labels = {
                            "football": "⚽ Футбол",
                            "basketball": "🏀 Баскетбол",
                            "tennis": "🎾 Теніс",
                            "hockey": "🏒 Хокей",
                        }
                        labels = ", ".join(sport_labels.get(s, s) for s in newly_resumed)
                        msg = (
                            "✅ *Ліміт запитів поновлено!*\n\n"
                            "Новий день — 100 запитів на кожен спорт.\n"
                            "Скан поновлено: " + labels
                        )
                        await send_msg(session, msg)
                    _prev_paused = set(_quota_paused)
                    await asyncio.wait_for(scan(session), timeout=120)
                except asyncio.TimeoutError:
                    print("[SCAN TIMEOUT] скан завис, пропускаємо")
                except Exception as e:
                    print(f"[SCAN ERROR] {e}")
                await asyncio.sleep(POLL_INTERVAL if is_running else 10)

        await asyncio.gather(
            command_loop(),
            scan_loop(),
        )

if __name__ == "__main__":
    asyncio.run(main())
