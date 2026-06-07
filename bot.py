import os
import asyncio
import aiohttp
from datetime import datetime

# ── КОНФІГ ────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API_KEY    = os.environ.get("API_KEY", "")

POLL_INTERVAL = 300  # 5 хвилин між сканами (економія запитів)

# ── ФУТБОЛ ────────────────────────────────────────────────────────────────
FOOTBALL_ENABLED   = True
FAV_THRESHOLD_FOOT = 1.80
MIN_ODDS_RISE_FOOT = 35
MAX_MINUTE_FOOT    = 75
FOOTBALL_LEAGUES   = [253, 71, 32, 98, 188, 262, 128, 9, 667]
FOOTBALL_NAMES     = {
    253: "🇺🇸 MLS",
    71:  "🇧🇷 Бразилія",
    32:  "🌍 Відбір ЧС 2026",
    98:  "🇯🇵 J-League",
    188: "🇦🇺 A-League",
    262: "🇲🇽 Ліга МХ",
    128: "🇦🇷 Аргентина",
    9:   "🤝 Товариські (збірні)",
    667: "🤝 Товариські (клуби)",
}

# ── БАСКЕТБОЛ ─────────────────────────────────────────────────────────────
BASKETBALL_ENABLED    = True
FAV_THRESHOLD_BASK    = 1.60
MIN_ODDS_RISE_BASK    = 30
MIN_POINTS_BEHIND     = 8   # фаворит програє мінімум 8 очок
MIN_QUARTER_BASK      = 2   # не раніше 2-ї чверті
MAX_QUARTER_BASK      = 3   # не пізніше 3-ї чверті
BASKETBALL_LEAGUES    = [12, 120, 117]  # NBA, Євроліга, NCAAm
BASKETBALL_NAMES      = {
    12:  "🏀 NBA",
    120: "🏀 Євроліга",
    117: "🏀 NCAA",
}

# ── ТЕНІС ─────────────────────────────────────────────────────────────────
TENNIS_ENABLED     = True
FAV_THRESHOLD_TEN  = 1.60
MIN_ODDS_RISE_TEN  = 40
TENNIS_LEAGUES     = [1, 2, 3, 4]  # ATP, WTA, Grand Slam, Challenger
TENNIS_NAMES       = {
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
stats = {
    "signals_total": 0,
    "scans_total": 0,
    "started_at": datetime.now().strftime("%H:%M %d.%m.%Y"),
    "last_signal": None,
    "by_sport": {"⚽ Футбол": 0, "🏀 Баскетбол": 0, "🎾 Теніс": 0},
}

# ── TELEGRAM ──────────────────────────────────────────────────────────────
async def send_msg(session, text, keyboard=True):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    if keyboard:
        payload["reply_markup"] = main_keyboard()
    try:
        async with session.post(url, json=payload) as r:
            return await r.json()
    except Exception as e:
        print(f"[TG ERROR] {e}")

def main_keyboard():
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }

async def get_updates(session):
    global offset
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    try:
        async with session.get(url, params={"offset": offset, "timeout": 3}) as r:
            data = await r.json()
            return data.get("result", [])
    except:
        return []

async def process_commands(session):
    global is_running, offset
    updates = await get_updates(session)
    for upd in updates:
        offset = upd["update_id"] + 1
        text = upd.get("message", {}).get("text", "").strip().lower()

        if text in ["/start", "▶️ старт"]:
            is_running = True
            await send_msg(session, "▶️ *Сканування запущено!*")

        elif text in ["/stop", "⏹ стоп"]:
            is_running = False
            await send_msg(session, "⏹ *Сканування зупинено.*\nНадішли /start щоб відновити.")

        elif text in ["/stat", "📊 статистика"]:
            await send_stat(session)

        elif text in ["/help", "/menu"]:
            await send_msg(session,
                "📋 *Команди:*\n\n"
                "/start — запустити\n"
                "/stop — зупинити\n"
                "/stat — статистика"
            )

async def send_stat(session):
    lines = [
        "📊 *Статистика FavTracker*\n",
        f"🕐 Запущено: {stats['started_at']}",
        f"🔍 Сканів: {stats['scans_total']}",
        f"🚨 Сигналів: {stats['signals_total']}",
        f"⚡ Статус: {'▶️ Активний' if is_running else '⏹ Зупинений'}",
        f"⏱ Інтервал: {POLL_INTERVAL // 60} хв",
    ]
    if stats["last_signal"]:
        lines.append(f"\n📌 Останній: {stats['last_signal']}")
    lines.append("\n🏆 *По видах спорту:*")
    for sport, count in stats["by_sport"].items():
        lines.append(f"  {sport}: {count}")
    await send_msg(session, "\n".join(lines))

def add_signal(sport_key, description):
    stats["signals_total"] += 1
    stats["last_signal"] = f"{description} ({datetime.now().strftime('%H:%M')})"
    stats["by_sport"][sport_key] = stats["by_sport"].get(sport_key, 0) + 1

# ── ФУТБОЛ API ────────────────────────────────────────────────────────────
async def fetch_football_live(session, league_id):
    url = f"https://v3.football.api-sports.io/fixtures?live=all&league={league_id}"
    try:
        async with session.get(url, headers={"x-apisports-key": API_KEY}) as r:
            return (await r.json()).get("response", [])
    except:
        return []

async def fetch_prematch_odds(session, fixture_id):
    if fixture_id in pre_odds:
        return pre_odds[fixture_id]
    url = f"https://v3.football.api-sports.io/odds?fixture={fixture_id}&bookmaker=6"
    try:
        async with session.get(url, headers={"x-apisports-key": API_KEY}) as r:
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
        async with session.get(url, headers={"x-apisports-key": API_KEY}) as r:
            return (await r.json()).get("response", [])
    except:
        return []

# ── ТЕНІС API ─────────────────────────────────────────────────────────────
async def fetch_tennis_live(session, league_id):
    url = f"https://v1.tennis.api-sports.io/games?league={league_id}&live=all"
    try:
        async with session.get(url, headers={"x-apisports-key": API_KEY}) as r:
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
    if not FOOTBALL_ENABLED:
        return
    for league_id in FOOTBALL_LEAGUES:
        fixtures = await fetch_football_live(session, league_id)
        await asyncio.sleep(1)
        for fix in fixtures:
            fid     = fix["fixture"]["id"]
            minute  = fix["fixture"]["status"].get("elapsed") or 0
            home    = fix["teams"]["home"]["name"]
            away    = fix["teams"]["away"]["name"]
            score_h = fix["goals"].get("home") or 0
            score_a = fix["goals"].get("away") or 0
            league  = FOOTBALL_NAMES.get(league_id, "")

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
                f"⚽ {league}\n"
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
    if not BASKETBALL_ENABLED:
        return
    for league_id in BASKETBALL_LEAGUES:
        games = await fetch_basketball_live(session, league_id)
        await asyncio.sleep(1)
        for game in games:
            gid     = game.get("id")
            home    = game.get("teams", {}).get("home", {}).get("name", "")
            away    = game.get("teams", {}).get("away", {}).get("name", "")
            score_h = game.get("scores", {}).get("home", {}).get("total") or 0
            score_a = game.get("scores", {}).get("away", {}).get("total") or 0
            quarter = game.get("status", {}).get("short", "")
            league  = BASKETBALL_NAMES.get(league_id, "")

            # Тільки 2-3 чверть
            if quarter not in ["Q2", "Q3"]:
                continue

            # Фаворит програє мінімум 8 очок
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
                f"🏀 {league}\n"
                f"*{home}* {score_h}:{score_a} *{away}*\n"
                f"📍 Чверть: {quarter}\n"
                f"📊 Різниця: {abs(diff)} очок\n"
                f"💪 {strength(abs(diff), 15, 10)}"
            )
            await send_msg(session, msg)
            print(f"  🏀 СИГНАЛ: {home} {score_h}:{score_a} {away} чв.{quarter}")

# ── СКАНУВАННЯ ТЕНІС ──────────────────────────────────────────────────────
async def scan_tennis(session):
    if not TENNIS_ENABLED:
        return
    for league_id in TENNIS_LEAGUES:
        games = await fetch_tennis_live(session, league_id)
        await asyncio.sleep(1)
        for game in games:
            gid    = game.get("id")
            home   = game.get("players", {}).get("home", {}).get("name", "")
            away   = game.get("players", {}).get("away", {}).get("name", "")
            sets_h = game.get("scores", {}).get("home", {}).get("sets") or 0
            sets_a = game.get("scores", {}).get("away", {}).get("sets") or 0
            league = TENNIS_NAMES.get(league_id, "")

            # Фаворит програв перший сет (рахунок 0:1)
            if not (sets_h == 0 and sets_a == 1):
                continue

            key = f"ten_{gid}_0_1"
            if key in notified:
                continue
            notified.add(key)
            add_signal("🎾 Теніс", f"{home} {sets_h}:{sets_a} {away}")

            msg = (
                f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ СЕТ*\n\n"
                f"🎾 {league}\n"
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
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Скан #{stats['scans_total']}...")
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

        while True:
            try:
                await process_commands(session)
                await scan(session)
            except Exception as e:
                print(f"[ERROR] {e}")
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
