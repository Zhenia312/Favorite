import os
import asyncio
import aiohttp
from datetime import datetime

# ── КОНФІГ ────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API_KEY    = os.environ.get("API_KEY", "")

FAVORITE_THRESHOLD = 1.80   # коеф нижче цього = фаворит
MIN_ODDS_RISE      = 35     # % зростання коефу для сигналу
MAX_MINUTE         = 75     # не брати голи після цієї хвилини
POLL_INTERVAL      = 60     # секунд між сканами

LEAGUES = [39, 140, 78, 135, 2]  # АПЛ, Ла Ліга, Бундесліга, Серія А, ЛЧ
LEAGUE_NAMES = {
    39: "🏴󠁧󠁢󠁥󠁮󠁧󠁿 АПЛ",
    140: "🇪🇸 Ла Ліга",
    78: "🇩🇪 Бундесліга",
    135: "🇮🇹 Серія А",
    2: "⭐ Ліга чемпіонів",
}

# ── СТАН ──────────────────────────────────────────────────────────────────
notified = set()   # ID матчів про які вже надіслали сигнал
pre_odds = {}      # {fixture_id: odds до матчу}

# ── TELEGRAM ──────────────────────────────────────────────────────────────
async def send_telegram(session, text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        async with session.post(url, json=payload) as r:
            return await r.json()
    except Exception as e:
        print(f"[TG ERROR] {e}")

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
    """Беремо передматчеві коефіцієнти якщо ще не маємо"""
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
    except Exception as e:
        print(f"[ODDS ERROR] {e}")
    return None

# ── СИЛА СИГНАЛУ ──────────────────────────────────────────────────────────
def signal_strength(rise, minute):
    if rise >= 60 and minute <= 55:
        return "🔥 СИЛЬНИЙ"
    if rise >= 40 and minute <= 65:
        return "✅ ХОРОШИЙ"
    return "⚠️ СЛАБКИЙ"

# ── ГОЛОВНИЙ СКАН ─────────────────────────────────────────────────────────
async def scan(session):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Сканую матчі...")
    signals_found = 0

    for league_id in LEAGUES:
        fixtures = await fetch_live(session, league_id)
        await asyncio.sleep(1)  # пауза між запитами

        for fix in fixtures:
            fid      = fix["fixture"]["id"]
            minute   = fix["fixture"]["status"].get("elapsed") or 0
            home     = fix["teams"]["home"]["name"]
            away     = fix["teams"]["away"]["name"]
            score_h  = fix["goals"].get("home") or 0
            score_a  = fix["goals"].get("away") or 0
            league   = LEAGUE_NAMES.get(league_id, "")

            # Пропускаємо пізні голи
            if minute > MAX_MINUTE:
                continue

            # Беремо live коефіцієнт з відповіді
            live_odd = None
            for bet_block in fix.get("odds", []):
                for v in bet_block.get("values", []):
                    if v.get("value") == "Home":
                        try:
                            live_odd = float(v["odd"])
                        except:
                            pass

            # Беремо передматчевий коефіцієнт
            pre_odd = await fetch_prematch_odds(session, fid)

            # Якщо немає live odds — використовуємо pre як базу
            if not pre_odd:
                continue

            is_fav_home = pre_odd < FAVORITE_THRESHOLD
            fav_losing  = is_fav_home and score_h < score_a

            if not fav_losing:
                continue

            # Рахуємо зростання коефу
            current_odd = live_odd or pre_odd
            rise = round(((current_odd - pre_odd) / pre_odd) * 100) if pre_odd else 0

            if rise < MIN_ODDS_RISE:
                continue

            # Унікальний ключ = матч + рахунок (щоб сигналити тільки раз на кожен гол)
            signal_key = f"{fid}_{score_h}_{score_a}"
            if signal_key in notified:
                continue

            notified.add(signal_key)
            signals_found += 1
            strength = signal_strength(rise, minute)

            msg = (
                f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                f"🏆 {league}\n"
                f"⚽ *{home}* {score_h}:{score_a} *{away}*\n"
                f"⏱ Хвилина: {minute}'\n"
                f"📉 Коеф до матчу: `{pre_odd}`\n"
                f"📈 Коеф зараз: `{current_odd}` \\(+{rise}%\\)\n"
                f"💪 Сила сигналу: {strength}\n\n"
                f"👆 Фаворит: *{home}*"
            )
            await send_telegram(session, msg)
            print(f"  → СИГНАЛ: {home} {score_h}:{score_a} {away} | +{rise}% | {strength}")

    if signals_found == 0:
        print("  Сигналів немає")

# ── СТАРТ ─────────────────────────────────────────────────────────────────
async def main():
    print("=" * 50)
    print("  FavTracker Bot запущено")
    print(f"  Ліги: АПЛ, Ла Ліга, Бундесліга, Серія А, ЛЧ")
    print(f"  Фаворит: коеф ≤ {FAVORITE_THRESHOLD}")
    print(f"  Мін. зріст коефу: {MIN_ODDS_RISE}%")
    print(f"  Макс. хвилина: {MAX_MINUTE}'")
    print(f"  Інтервал сканування: {POLL_INTERVAL} сек")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        # Стартове повідомлення
        await send_telegram(session, 
            "✅ *FavTracker запущено!*\n\n"
            "Відстежую матчі:\n"
            "🏴󠁧󠁢󠁥󠁮󠁧󠁿 АПЛ | 🇪🇸 Ла Ліга | 🇩🇪 Бундесліга\n"
            "🇮🇹 Серія А | ⭐ Ліга чемпіонів\n\n"
            f"Сигнал = фаворит програє + коеф зріс на {MIN_ODDS_RISE}%+"
        )

        while True:
            try:
                await scan(session)
            except Exception as e:
                print(f"[SCAN ERROR] {e}")
            await asyncio.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
