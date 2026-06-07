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
FAV_THRESHOLD_FOOT = 1.80
MIN_ODDS_RISE_FOOT = 35
MAX_MINUTE_FOOT    = 75
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
FAV_THRESHOLD_BASK = 1.60
MIN_POINTS_BEHIND  = 8
BASKETBALL_LEAGUES = {
    12:  "🏀 NBA",
    120: "🏀 Євроліга",
    117: "🏀 NCAA",
}

# ── ТЕНІС ─────────────────────────────────────────────────────────────────
FAV_THRESHOLD_TEN = 1.60
TENNIS_LEAGUES    = {
    1: "🎾 ATP",
    2: "🎾 WTA",
    3: "🎾 Grand Slam",
    4: "🎾 Challenger",
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
}

leagues_enabled = {
    "football":   {lid: True for lid in FOOTBALL_LEAGUES},
    "basketball": {lid: True for lid in BASKETBALL_LEAGUES},
    "tennis":     {lid: True for lid in TENNIS_LEAGUES},
}

user_state = {"menu": None}

api_requests = {
    "football":   {"used": 0, "limit": 100},
    "basketball": {"used": 0, "limit": 100},
    "tennis":     {"used": 0, "limit": 100},
}

stats = {
    "signals_total": 0,
    "scans_total":   0,
    "started_at":    now_kyiv().strftime("%H:%M %d.%m.%Y"),
    "last_signal":   None,
    "by_sport": {"⚽ Футбол": 0, "🏀 Баскетбол": 0, "🎾 Теніс": 0},
}

# ── КЛАВІАТУРИ ────────────────────────────────────────────────────────────
def main_keyboard():
    f = "✅" if sports_enabled["football"]   else "❌"
    b = "✅" if sports_enabled["basketball"] else "❌"
    t = "✅" if sports_enabled["tennis"]     else "❌"
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}],
            [{"text": f"{f} Футбол"}, {"text": f"{b} Баскетбол"}, {"text": f"{t} Теніс"}],
            [{"text": "⚙️ Ліги футбол"}, {"text": "⚙️ Ліги баскет"}, {"text": "⚙️ Ліги теніс"}],
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
    else:
        leagues = TENNIS_LEAGUES

    rows = []
    items = list(leagues.items())
    for i in range(0, len(items), 2):
        row = []
        for lid, name in items[i:i+2]:
            icon = "✅" if leagues_enabled[sport][lid] else "❌"
            row.append({"text": f"{icon} {name}"})
        rows.append(row)
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
def track_request(sport):
    api_requests[sport]["used"] += 1

def requests_left(sport):
    r = api_requests[sport]
    return r["limit"] - r["used"]

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
        if menu in ["football_leagues", "basketball_leagues", "tennis_leagues"]:
            sport = menu.replace("_leagues", "")

            if text == "🔙 назад":
                user_state["menu"] = None
                await send_msg(session, "🏠 Головне меню")
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
            else:
                leagues = TENNIS_LEAGUES

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

# ── СТАТИСТИКА ────────────────────────────────────────────────────────────
async def send_stat(session):
    f_leagues = [n for lid, n in FOOTBALL_LEAGUES.items()   if leagues_enabled["football"][lid]]
    b_leagues = [n for lid, n in BASKETBALL_LEAGUES.items() if leagues_enabled["basketball"][lid]]
    t_leagues = [n for lid, n in TENNIS_LEAGUES.items()     if leagues_enabled["tennis"][lid]]

    fl = requests_left("football")
    bl = requests_left("basketball")
    tl = requests_left("tennis")

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
        f"  🎾 Теніс: {stats['by_sport']['🎾 Теніс']} — {'✅' if sports_enabled['tennis'] else '❌'}\n",
        "📡 *Залишок запитів (сьогодні):*",
        f"  ⚽ Футбол: {fl}/100 {'⚠️' if fl < 20 else ''}",
        f"  🏀 Баскетбол: {bl}/100 {'⚠️' if bl < 20 else ''}",
        f"  🎾 Теніс: {tl}/100 {'⚠️' if tl < 20 else ''}\n",
        "📋 *Активні ліги:*",
        f"  ⚽ {', '.join(f_leagues) if f_leagues else 'немає'}",
        f"  🏀 {', '.join(b_leagues) if b_leagues else 'немає'}",
        f"  🎾 {', '.join(t_leagues) if t_leagues else 'немає'}",
    ]
    if stats["last_signal"]:
        lines.append(f"\n📌 Останній: {stats['last_signal']}")
    await send_msg(session, "\n".join(lines))

def add_signal(sport_key, description):
    stats["signals_total"] += 1
    stats["last_signal"] = f"{description} ({now_kyiv().strftime('%H:%M')})"
    stats["by_sport"][sport_key] = stats["by_sport"].get(sport_key, 0) + 1

# ── ФУТБОЛ API ────────────────────────────────────────────────────────────
async def fetch_football_live(session, league_id):
    url = f"https://v3.football.api-sports.io/fixtures?live=all&league={league_id}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            return (await r.json()).get("response", [])
    except:
        return []

async def fetch_prematch_odds(session, fixture_id):
    if fixture_id in pre_odds:
        return pre_odds[fixture_id]
    url = f"https://v3.football.api-sports.io/odds?fixture={fixture_id}&bookmaker=6"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            data = (await r.json()).get("response", [])
            if data:
                for bet in data[0].get("bookmakers", [{}])[0].get("bets", []):
                    if bet.get("name") == "Match Winner":
                        for v in bet.get("values", []):
                            if v.get("value") == "Home":
                                odd = float(v.get("odd", 0))
                                pre_odds[fixture_id] = odd
                                return odd
    except:
        pass
    return None

# ── БАСКЕТБОЛ API ─────────────────────────────────────────────────────────
async def fetch_basketball_live(session, league_id):
    url = f"https://v1.basketball.api-sports.io/games?league={league_id}&live=all"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("basketball")
            return (await r.json()).get("response", [])
    except:
        return []

# ── ТЕНІС API ─────────────────────────────────────────────────────────────
async def fetch_tennis_live(session, league_id):
    url = f"https://v1.tennis.api-sports.io/games?league={league_id}&live=all"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("tennis")
            return (await r.json()).get("response", [])
    except:
        return []

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
    for league_id, league_name in FOOTBALL_LEAGUES.items():
        if not leagues_enabled["football"][league_id]:
            continue
        fixtures = await fetch_football_live(session, league_id)
        await asyncio.sleep(1)
        for fix in fixtures:
            fid     = fix["fixture"]["id"]
            minute  = fix["fixture"]["status"].get("elapsed") or 0
            home    = fix["teams"]["home"]["name"]
            away    = fix["teams"]["away"]["name"]
            score_h = fix["goals"].get("home") or 0
            score_a = fix["goals"].get("away") or 0

            if minute > MAX_MINUTE_FOOT:
                continue

            pre_odd = await fetch_prematch_odds(session, fid)
            if not pre_odd or pre_odd >= FAV_THRESHOLD_FOOT:
                continue
            if score_h >= score_a:
                continue

            live_odd = pre_odd
            for bet_block in fix.get("odds", []):
                for v in bet_block.get("values", []):
                    if v.get("value") == "Home":
                        try: live_odd = float(v["odd"])
                        except: pass

            rise = round(((live_odd - pre_odd) / pre_odd) * 100)
            if rise < MIN_ODDS_RISE_FOOT:
                continue

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
    for league_id, league_name in BASKETBALL_LEAGUES.items():
        if not leagues_enabled["basketball"][league_id]:
            continue
        games = await fetch_basketball_live(session, league_id)
        await asyncio.sleep(1)
        for game in games:
            gid     = game.get("id")
            home    = game.get("teams", {}).get("home", {}).get("name", "")
            away    = game.get("teams", {}).get("away", {}).get("name", "")
            score_h = game.get("scores", {}).get("home", {}).get("total") or 0
            score_a = game.get("scores", {}).get("away", {}).get("total") or 0
            quarter = game.get("status", {}).get("short", "")

            if quarter not in ["Q2", "Q3"]:
                continue

            diff = score_h - score_a
            if diff > -MIN_POINTS_BEHIND:
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
                f"📊 Різниця: {abs(diff)} очок\n"
                f"💪 {strength(abs(diff), 15, 10)}"
            )
            await send_msg(session, msg)
            print(f"  🏀 СИГНАЛ: {home} {score_h}:{score_a} {away} чв.{quarter}")

# ── СКАНУВАННЯ ТЕНІС ──────────────────────────────────────────────────────
async def scan_tennis(session):
    if not sports_enabled["tennis"]:
        return
    for league_id, league_name in TENNIS_LEAGUES.items():
        if not leagues_enabled["tennis"][league_id]:
            continue
        games = await fetch_tennis_live(session, league_id)
        await asyncio.sleep(1)
        for game in games:
            gid    = game.get("id")
            home   = game.get("players", {}).get("home", {}).get("name", "")
            away   = game.get("players", {}).get("away", {}).get("name", "")
            sets_h = game.get("scores", {}).get("home", {}).get("sets") or 0
            sets_a = game.get("scores", {}).get("away", {}).get("sets") or 0

            if not (sets_h == 0 and sets_a == 1):
                continue

            key = f"ten_{gid}_0_1"
            if key in notified:
                continue
            notified.add(key)
            add_signal("🎾 Теніс", f"{home} {sets_h}:{sets_a} {away}")

            msg = (
                f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ СЕТ*\n\n"
                f"🎾 {league_name}\n"
                f"*{home}* {sets_h}:{sets_a} *{away}*\n"
                f"📍 Фаворит програв перший сет\n"
                f"💡 Перевір live коефіцієнт на букмекері"
            )
            await send_msg(session, msg)
            print(f"  🎾 СИГНАЛ: {home} {sets_h}:{sets_a} {away}")

# ── ГОЛОВНИЙ СКАН ─────────────────────────────────────────────────────────
async def scan(session):
    if not is_running:
        return
    stats["scans_total"] += 1
    print(f"[{now_kyiv().strftime('%H:%M:%S')}] Скан #{stats['scans_total']}...")
    await scan_football(session)
    await scan_basketball(session)
    await scan_tennis(session)
    print(f"  Сигналів всього: {stats['signals_total']}")

# ── MAIN ──────────────────────────────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  FavTracker Bot — Футбол + Баскетбол + Теніс")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        await send_msg(session,
            "✅ *FavTracker запущено\\!*\n\n"
            "Відстежую:\n"
            "⚽ Футбол \\(MLS, Бразилія, Аргентина та ін\\.\\)\n"
            "🏀 Баскетбол \\(NBA, Євроліга\\)\n"
            "🎾 Теніс \\(ATP, WTA, Grand Slam\\)\n\n"
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

        async def scan_loop():
            while True:
                try:
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
