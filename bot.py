import os
import asyncio
import aiohttp
from datetime import datetime

# ── КОНФІГ ────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API_KEY    = os.environ.get("API_KEY", "")

FAVORITE_THRESHOLD = 1.80
MIN_ODDS_RISE      = 35
MAX_MINUTE         = 75
POLL_INTERVAL      = 60

LEAGUES = [39, 140, 78, 135, 2, 253, 71, 32]
LEAGUE_NAMES = {
    39:  "🏴󠁧󠁢󠁥󠁮󠁧󠁿 АПЛ",
    140: "🇪🇸 Ла Ліга",
    78:  "🇩🇪 Бундесліга",
    135: "🇮🇹 Серія А",
    2:   "⭐ Ліга чемпіонів",
    253: "🇺🇸 MLS",
    71:  "🇧🇷 Бразилія Серія А",
    32:  "🌍 Відбір ЧС 2026",
}

# ── СТАН ──────────────────────────────────────────────────────────────────
notified   = set()
pre_odds   = {}
is_running = True
stats = {
    "signals_total": 0,
    "scans_total": 0,
    "started_at": datetime.now().strftime("%H:%M:%S %d.%m.%Y"),
    "last_signal": None,
    "by_league": {},
}
offset = 0  # для Telegram polling

# ── TELEGRAM ВІДПРАВКА ────────────────────────────────────────────────────
async def send_msg(session, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with session.post(url, json=payload) as r:
            return await r.json()
    except Exception as e:
        print(f"[TG ERROR] {e}")

# ── TELEGRAM POLLING (читаємо команди) ────────────────────────────────────
async def get_updates(session):
    global offset
    url = f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 5}
    try:
        async with session.get(url, params=params) as r:
            data = await r.json()
            return data.get("result", [])
    except:
        return []

async def process_commands(session):
    global is_running, offset
    updates = await get_updates(session)
    for upd in updates:
        offset = upd["update_id"] + 1
        msg = upd.get("message", {})
        text = msg.get("text", "").strip().lower()

        if text == "/start":
            if not is_running:
                is_running = True
                await send_msg(session,
                    "▶️ *Сканування запущено!*\n"
                    "Відстежую матчі кожну хвилину.",
                    reply_markup=main_keyboard()
                )
            else:
                await send_msg(session,
                    "✅ Бот вже працює!",
                    reply_markup=main_keyboard()
                )

        elif text == "/stop":
            if is_running:
                is_running = False
                await send_msg(session,
                    "⏹ *Сканування зупинено.*\n"
                    "Надішли /start щоб відновити.",
                    reply_markup=main_keyboard()
                )
            else:
                await send_msg(session,
                    "⏹ Бот вже зупинений.",
                    reply_markup=main_keyboard()
                )

        elif text == "/stat":
            await send_stat(session)

        elif text in ["/help", "/menu"]:
            await send_msg(session,
                "📋 *Команди:*\n\n"
                "/start — запустити сканування\n"
                "/stop — зупинити сканування\n"
                "/stat — статистика сигналів",
                reply_markup=main_keyboard()
            )

# ── КЛАВІАТУРА ────────────────────────────────────────────────────────────
def main_keyboard():
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }

# ── СТАТИСТИКА ────────────────────────────────────────────────────────────
async def send_stat(session):
    lines = [
        "📊 *Статистика FavTracker*\n",
        f"🕐 Запущено: {stats['started_at']}",
        f"🔍 Сканів виконано: {stats['scans_total']}",
        f"🚨 Сигналів всього: {stats['signals_total']}",
        f"⚡ Статус: {'▶️ Активний' if is_running else '⏹ Зупинений'}",
    ]
    if stats["last_signal"]:
        lines.append(f"📌 Останній сигнал: {stats['last_signal']}")

    if stats["by_league"]:
        lines.append("\n🏆 *По лігах:*")
        for league, count in sorted(stats["by_league"].items(), key=lambda x: -x[1]):
            lines.append(f"  {league}: {count}")

    await send_msg(session, "\n".join(lines), reply_markup=main_keyboard())

# ── API-FOOTBALL ──────────────────────────────────────────────────────────
async def fetch_live(session, league_id):
    url = f"https://v3.football.api-sports.io/fixtures?live=all&league={league_id}"
    headers = {"x-apisports-key": API_KEY}
    try:
        async with session.get(url, headers=headers) as r:
            data = await r.json()
            return data.get("response", [])
    except Exception as e:
        print(f"[API ERROR] league={league_id} {e}")
        return []

async def fetch_prematch_odds(session, fixture_id):
    if fixture_id in pre_odds:
        return pre_odds[fixture_id]
    url = f"https://v3.football.api-sports.io/odds?fixture={fixture_id}&bookmaker=6"
    headers = {"x-apisports-key": API_KEY}
    try:
        async with session.get(url, headers=headers) as r:
            data = await r.json()
            resp = data.get("response", [])
            if resp:
                bets = resp[0].get("bookmakers", [{}])[0].get("bets", [])
                for bet in bets:
                    if bet.get("name") == "Match Winner":
                        for v in bet.get("values", []):
                            if v.get("value") == "Home":
                                odd = float(v.get("odd", 0))
                                pre_odds[fixture_id] = odd
                                return odd
    except:
        pass
    return None

def signal_strength(rise, minute):
    if rise >= 60 and minute <= 55:
        return "🔥 СИЛЬНИЙ"
    if rise >= 40 and minute <= 65:
        return "✅ ХОРОШИЙ"
    return "⚠️ СЛАБКИЙ"

# ── СКАНУВАННЯ ────────────────────────────────────────────────────────────
async def scan(session):
    if not is_running:
        return

    stats["scans_total"] += 1
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Скан #{stats['scans_total']}...")

    for league_id in LEAGUES:
        fixtures = await fetch_live(session, league_id)
        await asyncio.sleep(1)

        for fix in fixtures:
            fid     = fix["fixture"]["id"]
            minute  = fix["fixture"]["status"].get("elapsed") or 0
            home    = fix["teams"]["home"]["name"]
            away    = fix["teams"]["away"]["name"]
            score_h = fix["goals"].get("home") or 0
            score_a = fix["goals"].get("away") or 0
            league  = LEAGUE_NAMES.get(league_id, "")

            if minute > MAX_MINUTE:
                continue

            live_odd = None
            for bet_block in fix.get("odds", []):
                for v in bet_block.get("values", []):
                    if v.get("value") == "Home":
                        try:
                            live_odd = float(v["odd"])
                        except:
                            pass

            pre_odd = await fetch_prematch_odds(session, fid)
            if not pre_odd:
                continue

            is_fav_home = pre_odd < FAVORITE_THRESHOLD
            fav_losing  = is_fav_home and score_h < score_a
            if not fav_losing:
                continue

            current_odd = live_odd or pre_odd
            rise = round(((current_odd - pre_odd) / pre_odd) * 100) if pre_odd else 0
            if rise < MIN_ODDS_RISE:
                continue

            signal_key = f"{fid}_{score_h}_{score_a}"
            if signal_key in notified:
                continue

            notified.add(signal_key)
            stats["signals_total"] += 1
            stats["last_signal"] = f"{home} {score_h}:{score_a} {away} ({datetime.now().strftime('%H:%M')})"
            stats["by_league"][league] = stats["by_league"].get(league, 0) + 1

            strength = signal_strength(rise, minute)
            msg = (
                f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                f"🏆 {league}\n"
                f"⚽ *{home}* {score_h}:{score_a} *{away}*\n"
                f"⏱ Хвилина: {minute}'\n"
                f"📉 Коеф до матчу: `{pre_odd}`\n"
                f"📈 Коеф зараз: `{current_odd}` \\(+{rise}%\\)\n"
                f"💪 Сила сигналу: {strength}"
            )
            await send_msg(session, msg, reply_markup=main_keyboard())
            print(f"  → СИГНАЛ: {home} {score_h}:{score_a} {away} | +{rise}% | {strength}")

    print(f"  Сигналів немає" if stats["signals_total"] == 0 else f"  Всього сигналів: {stats['signals_total']}")

# ── MAIN ──────────────────────────────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  FavTracker Bot запущено")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        await send_msg(session,
            "✅ *FavTracker запущено\\!*\n\n"
            "Відстежую матчі:\n"
            "🏴󠁧󠁢󠁥󠁮󠁧󠁿 АПЛ | 🇪🇸 Ла Ліга | 🇩🇪 Бундесліга\n"
            "🇮🇹 Серія А | ⭐ Ліга чемпіонів\n"
            "🇺🇸 MLS | 🇧🇷 Бразилія | 🌍 Відбір ЧС 2026\n\n"
            f"Сигнал \\= фаворит програє \\+ коеф зріс на {MIN_ODDS_RISE}%\\+",
            reply_markup=main_keyboard()
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
                               
