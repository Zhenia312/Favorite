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

FOOTBALL_LEAGUES = {
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

# ── СТАН ──────────────────────────────────────────────────────────────────
notified   = {}   # key -> timestamp, для очищення старих записів
pre_odds   = {}   # fixture_id -> pre-match odd (кеш)
is_running = True
offset     = 0

sports_enabled = {"football": True}

leagues_enabled = {
    "football": {lid: True for lid in FOOTBALL_LEAGUES},
}

all_leagues_mode = {"football": False}

user_state = {"menu": None}

api_requests = {
    "football": {"used": 0, "limit": 7500},
}

stats = {
    "signals_total":    0,
    "scans_total":      0,
    "started_at":       None,
    "last_signal":      None,
    "signals_football": 0,
}

# ── КЛАВІАТУРИ ────────────────────────────────────────────────────────────
def main_keyboard():
    f = "✅" if sports_enabled["football"] else "❌"
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}, {"text": "🔍 Діагностика"}],
            [{"text": f"{f} Футбол"}],
            [{"text": "⚙️ Ліги футбол"}],
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

def leagues_keyboard():
    all_mode = all_leagues_mode["football"]
    all_icon = "🌍✅" if all_mode else "🌍❌"
    rows = []
    items = list(FOOTBALL_LEAGUES.items())
    for i in range(0, len(items), 2):
        row = []
        for lid, name in items[i:i+2]:
            icon = "✅" if leagues_enabled["football"][lid] else "❌"
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
_quota_warned   = set()
_quota_paused   = set()

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
        print(f"[{now_kyiv().strftime('%H:%M:%S')}] Лічильники скинуто (новий день)")
        if had_paused:
            print(f"[QUOTA] Поновлено: {had_paused}")

def track_request(sport):
    reset_counters_if_needed()
    api_requests[sport]["used"] += 1

def requests_left(sport):
    r = api_requests[sport]
    return max(0, r["limit"] - r["used"])

async def check_quota(session, sport, sport_label):
    left  = requests_left(sport)
    limit = api_requests[sport]["limit"]
    warn_threshold = max(20, int(limit * 0.05))

    if left == 0:
        if sport not in _quota_paused:
            _quota_paused.add(sport)
            now_str = now_kyiv().strftime("%H:%M")
            await send_msg(session,
                f"🚫 *{sport_label}: запити вичерпано!*\n\n"
                f"Ліміт {limit} запитів на сьогодні витрачено.\n"
                f"Скан призупинено до 00:00 (Київ).\n"
                f"🕐 Зараз: {now_str}"
            )
            print(f"[QUOTA] {sport} — вичерпано, призупинено")
        return False

    if left <= warn_threshold and sport not in _quota_warned:
        _quota_warned.add(sport)
        await send_msg(session,
            f"⚠️ *{sport_label}: залишилось мало запитів!*\n\n"
            f"Залишок: *{left}/{limit}*\n"
            f"Розглянь збільшення інтервалу."
        )
        print(f"[QUOTA] {sport} — попередження: залишилось {left}")
    return True

# ── ОЧИЩЕННЯ СТАРИХ ДАНИХ ─────────────────────────────────────────────────
def cleanup_old_data():
    now_ts = now_kyiv().timestamp()
    cutoff = 12 * 3600  # 12 годин
    old_keys = [k for k, ts in notified.items() if now_ts - ts > cutoff]
    for k in old_keys:
        del notified[k]
    # Обмежуємо розмір кешу odds
    if len(pre_odds) > 1000:
        for k in list(pre_odds.keys())[:500]:
            del pre_odds[k]
    if old_keys:
        print(f"[CLEANUP] Видалено {len(old_keys)} старих записів")

# ── ОБРОБКА КОМАНД ────────────────────────────────────────────────────────
async def process_commands(session):
    global is_running, offset, POLL_INTERVAL
    updates = await get_updates(session)

    for upd in updates:
        offset = upd["update_id"] + 1
        raw  = upd.get("message", {}).get("text", "").strip()
        text = raw.lower()
        menu = user_state["menu"]

        # ── Вибір інтервалу ───────────────────────────────────────────────
        if menu == "set_interval":
            if text == "🔙 назад":
                user_state["menu"] = None
                await send_msg(session, "🏠 Головне меню")
                continue
            interval_map = {"1 хв": 1, "2 хв": 2, "3 хв": 3, "5 хв": 5, "10 хв": 10}
            if text in interval_map:
                POLL_INTERVAL = interval_map[text] * 60
                user_state["menu"] = None
                await send_msg(session, f"✅ *Інтервал змінено на {interval_map[text]} хв*")
            else:
                await send_msg(session, "Натисни одну з кнопок ⬇️", kb=interval_keyboard())
            continue

        # ── Вибір ліг ─────────────────────────────────────────────────────
        if menu == "football_leagues":
            if text == "🔙 назад":
                user_state["menu"] = None
                await send_msg(session, "🏠 Головне меню")
                continue

            if "всі ліги api" in text:
                all_leagues_mode["football"] = not all_leagues_mode["football"]
                if all_leagues_mode["football"]:
                    await send_msg(session,
                        "🌍 *Режим «Всі ліги API» увімкнено*\n"
                        "Бот сканує всі live матчі без фільтра.\n"
                        f"⚠️ 1 запит на скан замість {len(leagues_enabled['football'])}",
                        kb=leagues_keyboard()
                    )
                else:
                    await send_msg(session,
                        "🌍 *Режим «Всі ліги API» вимкнено*\n"
                        "Повернено фільтр по обраних лігах.",
                        kb=leagues_keyboard()
                    )
                continue

            if text in ["✅ всі", "всі"]:
                for lid in leagues_enabled["football"]:
                    leagues_enabled["football"][lid] = True
                await send_msg(session, "✅ Всі ліги увімкнено", kb=leagues_keyboard())
                continue

            if text in ["❌ жодної", "жодної"]:
                for lid in leagues_enabled["football"]:
                    leagues_enabled["football"][lid] = False
                await send_msg(session, "❌ Всі ліги вимкнено", kb=leagues_keyboard())
                continue

            matched = False
            for lid, name in FOOTBALL_LEAGUES.items():
                clean_name = name.split(" ", 1)[-1].lower().strip()
                if clean_name in text:
                    leagues_enabled["football"][lid] = not leagues_enabled["football"][lid]
                    icon = "✅" if leagues_enabled["football"][lid] else "❌"
                    await send_msg(session, f"{icon} {name}", kb=leagues_keyboard())
                    matched = True
                    break
            if not matched:
                await send_msg(session, "Натисни кнопку ліги", kb=leagues_keyboard())
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
            await send_msg(session, "⚙️ *Ліги футболу:*", kb=leagues_keyboard())

        elif "футбол" in text:
            sports_enabled["football"] = not sports_enabled["football"]
            icon = "✅" if sports_enabled["football"] else "❌"
            state = "увімкнено" if sports_enabled["football"] else "вимкнено"
            await send_msg(session, f"{icon} *Футбол {state}*")

# ── РОЗКЛАД ───────────────────────────────────────────────────────────────
async def send_schedule(session):
    await send_msg(session, "📅 *Збираю розклад на сьогодні...*")
    today = now_kyiv().strftime("%Y-%m-%d")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(
            f"https://v3.football.api-sports.io/fixtures?date={today}",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            data = await r.json()
            fixtures = data.get("response", [])
            errors   = data.get("errors", {})
            print(f"[РОЗКЛАД] date={today} results={data.get('results',0)} errors={errors}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка запиту: {e}")
        return

    hours = {}
    for fix in fixtures:
        try:
            dt_str = fix.get("fixture", {}).get("date", "")
            if "T" in dt_str:
                utc_hour  = int(dt_str[11:13])
                kyiv_hour = (utc_hour + 3) % 24
                hours[kyiv_hour] = hours.get(kyiv_hour, 0) + 1
        except Exception:
            continue

    hour_lines = (
        "\n".join(f"  {h:02d}:00 — {hours[h]} матч(ів)" for h in sorted(hours))
        if hours else "  немає матчів"
    )
    lines = [
        f"📅 *Розклад футболу* ({today}, Київ)\n",
        f"⚽ Всього матчів: *{len(fixtures)}*\n",
        hour_lines,
        f"\n⚠️ Витрачено 1 запит",
    ]
    if errors:
        lines.append(f"\n❌ Помилка API: {errors}")
    await send_msg(session, "\n".join(lines))

# ── ЛІГИ API ──────────────────────────────────────────────────────────────
async def send_leagues_info(session):
    await send_msg(session, "📋 *Запитую доступні ліги...*")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(
            "https://v3.football.api-sports.io/leagues?current=true",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            data    = await r.json()
            leagues = data.get("response", [])
            errors  = data.get("errors", {})
            results = data.get("results", 0)
            print(f"[ЛІГИ] status={r.status} results={results} errors={errors}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка: {e}")
        return

    live_leagues = [
        l for l in leagues
        if any(
            s.get("coverage", {}).get("fixtures", {}).get("live")
            for s in l.get("seasons", [])
        )
    ]
    lines = [
        "📋 *Доступні ліги через ваш API ключ*\n",
        f"⚽ Всього активних ліг: *{len(leagues)}*",
        f"📡 З підтримкою live: *{len(live_leagues)}*",
        f"\n⚠️ Витрачено 1 запит",
    ]
    if errors:
        lines.append(f"\n❌ Помилка API: {errors}")
    await send_msg(session, "\n".join(lines))

# ── ДІАГНОСТИКА ───────────────────────────────────────────────────────────
async def send_diagnostics(session):
    await send_msg(session, "🔍 *Перевіряю live матчі...*")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(
            "https://v3.football.api-sports.io/fixtures?live=all",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            data     = await r.json()
            fixtures = data.get("response", [])
            errors   = data.get("errors", {})
            results  = data.get("results", 0)
            print(f"[ДІАГНОСТИКА] status={r.status} results={results} errors={errors} len={len(fixtures)}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка: {e}")
        return

    fl    = requests_left("football")
    limit = api_requests["football"]["limit"]
    used  = api_requests["football"]["used"]

    lines = [
        "🔍 *Діагностика — Live матчі зараз*\n",
        f"⚽ Футбол: *{len(fixtures)} матчів* в лайві",
        f"\n📡 Запитів сьогодні: {used}/{limit} (залишок: {fl})",
        f"⚠️ Витрачено 1 запит на діагностику",
    ]
    if errors:
        lines.append(f"\n❌ Помилка API: {errors}")
    await send_msg(session, "\n".join(lines))

# ── СТАТИСТИКА ────────────────────────────────────────────────────────────
async def send_stat(session):
    fl    = requests_left("football")
    limit = api_requests["football"]["limit"]
    used  = api_requests["football"]["used"]

    active_leagues = [n for lid, n in FOOTBALL_LEAGUES.items() if leagues_enabled["football"][lid]]
    league_line = (
        "🌍 Всі ліги API" if all_leagues_mode["football"]
        else (", ".join(active_leagues) if active_leagues else "немає")
    )

    lines = [
        "📊 *Статистика FavTracker*\n",
        f"🕐 Запущено: {stats['started_at']}",
        f"🔍 Сканів: {stats['scans_total']}",
        f"🚨 Сигналів всього: {stats['signals_total']}",
        f"  ⚽ Футбол: {stats['signals_football']}",
        f"⚡ Статус: {'▶️ Активний' if is_running else '⏹ Зупинений'}",
        f"⏱ Інтервал: {POLL_INTERVAL // 60} хв\n",
        f"⚽ Футбол: {'✅' if sports_enabled['football'] else '❌'}\n",
        "📡 *Запити сьогодні:*",
        f"  ⚽ використано {used}/{limit}, залишок {fl} {'⚠️' if fl < max(20, int(limit * 0.05)) else ''}",
        f"\n📋 *Активні ліги:*",
        f"  {league_line}",
    ]
    if stats["last_signal"]:
        lines.append(f"\n📌 Останній: {stats['last_signal']}")
    await send_msg(session, "\n".join(lines))

def add_signal(description):
    stats["signals_total"]    += 1
    stats["signals_football"] += 1
    stats["last_signal"] = f"{description} ({now_kyiv().strftime('%H:%M')})"

# ── ФУТБОЛ API ────────────────────────────────────────────────────────────
async def fetch_football_live(session, league_id=None):
    url = (
        f"https://v3.football.api-sports.io/fixtures?live=all&league={league_id}"
        if league_id else
        "https://v3.football.api-sports.io/fixtures?live=all"
    )
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            raw     = await r.json()
            results = raw.get("response", [])
            errors  = raw.get("errors", {})
            total   = raw.get("results", 0)
            print(f"  [API⚽] status={r.status} results={total} errors={errors} len={len(results)}")
            if errors:
                print(f"  [API⚽] ⚠️ errors: {errors}")
            return results
    except Exception as e:
        print(f"  [API⚽ ERROR] {e}")
        return []

async def fetch_prematch_odds_football(session, fixture_id):
    if fixture_id in pre_odds:
        return pre_odds[fixture_id]

    url = f"https://v3.football.api-sports.io/odds?fixture={fixture_id}&bookmaker=6"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            data       = (await r.json()).get("response", [])
            print(f"    [ODDS⚽] fixture={fixture_id} response={len(data)} записів")
            if not data:
                print(f"    [ODDS⚽] ❌ порожня відповідь")
                return None
            bookmakers = data[0].get("bookmakers", [])
            print(f"    [ODDS⚽] букмекерів: {len(bookmakers)}")
            if not bookmakers:
                print(f"    [ODDS⚽] ❌ немає букмекерів")
                return None
            bets = bookmakers[0].get("bets", [])
            print(f"    [ODDS⚽] типи ставок: {[b.get('name') for b in bets]}")
            for bet in bets:
                if bet.get("name") == "Match Winner":
                    for v in bet.get("values", []):
                        if v.get("value") == "Home":
                            odd = float(v.get("odd", 0))
                            pre_odds[fixture_id] = odd
                            print(f"    [ODDS⚽] ✅ Home odd={odd}")
                            return odd
            print(f"    [ODDS⚽] ❌ 'Match Winner'/'Home' не знайдено")
    except Exception as e:
        print(f"    [ODDS⚽ ERROR] {e}")
    return None

async def fetch_live_odds_football(session, fixture_id):
    url = f"https://v3.football.api-sports.io/odds/live?fixture={fixture_id}"
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(url, headers={"x-apisports-key": API_KEY}, timeout=timeout) as r:
            track_request("football")
            raw    = await r.json()
            data   = raw.get("response", [])
            errors = raw.get("errors", {})
            print(f"    [LIVE ODDS⚽] fixture={fixture_id} response={len(data)} errors={errors}")
            if errors:
                print(f"    [LIVE ODDS⚽] ⚠️ errors: {errors}")
            if not data:
                print(f"    [LIVE ODDS⚽] ❌ порожня відповідь. RAW: {raw}")
                return None
            all_bets = []
            for bookmaker in data[0].get("odds", []):
                bk_name = bookmaker.get("name", "")
                for bet in bookmaker.get("bets", []):
                    bet_name = bet.get("name", "")
                    all_bets.append(f"{bk_name}/{bet_name}")
                    if bet_name in ["Match Winner", "1X2"]:
                        for v in bet.get("values", []):
                            if v.get("value") in ["Home", "1"]:
                                odd = float(v.get("odd", 0))
                                print(f"    [LIVE ODDS⚽] ✅ {bk_name}/{bet_name} live_odd={odd}")
                                return odd
            print(f"    [LIVE ODDS⚽] ❌ не знайдено. Доступні: {all_bets}")
    except Exception as e:
        print(f"    [LIVE ODDS⚽ ERROR] {e}")
    return None

# ── СИЛА СИГНАЛУ ──────────────────────────────────────────────────────────
def strength(rise, strong_rise=60, good_rise=40):
    if rise >= strong_rise:
        return "🔥 СИЛЬНИЙ"
    if rise >= good_rise:
        return "✅ ХОРОШИЙ"
    return "⚠️ СЛАБКИЙ"

# ── СКАНУВАННЯ ────────────────────────────────────────────────────────────
async def scan_football(session):
    if not sports_enabled["football"]:
        print("  [⚽] вимкнено, пропускаємо")
        return
    if not await check_quota(session, "football", "⚽ Футбол"):
        return

    if all_leagues_mode["football"]:
        fixtures = await fetch_football_live(session)
        await asyncio.sleep(1)
        league_map = {
            fix.get("league", {}).get("id"): fix.get("league", {}).get("name", "")
            for fix in fixtures
        }
        await _process_football_fixtures(session, fixtures, league_map)
    else:
        for league_id, league_name in FOOTBALL_LEAGUES.items():
            if not leagues_enabled["football"][league_id]:
                continue
            fixtures = await fetch_football_live(session, league_id)
            await asyncio.sleep(1)
            await _process_football_fixtures(session, fixtures, {league_id: league_name})

async def _process_football_fixtures(session, fixtures, league_map):
    total = len(fixtures)
    cnt_minute = cnt_odds = cnt_score = cnt_live = cnt_signals = 0
    print(f"  [⚽ СКАН] Матчів отримано: {total}")

    for fix in fixtures:
        fid          = fix["fixture"]["id"]
        league_id    = fix.get("league", {}).get("id")
        league_name  = league_map.get(league_id, fix.get("league", {}).get("name", ""))
        minute       = fix["fixture"]["status"].get("elapsed") or 0
        status_short = fix["fixture"]["status"].get("short", "")
        home         = fix["teams"]["home"]["name"]
        away         = fix["teams"]["away"]["name"]
        score_h      = fix["goals"].get("home") or 0
        score_a      = fix["goals"].get("away") or 0

        print(f"  [⚽] {home} {score_h}:{score_a} {away} | хв={minute} | статус={status_short}")

        # Фільтр 1: статус і хвилина
        if status_short not in ["1H", "2H", "ET"]:
            cnt_minute += 1
            print(f"    → пропуск: статус '{status_short}' (потрібно 1H/2H/ET)")
            continue
        if minute > MAX_MINUTE_FOOT:
            cnt_minute += 1
            print(f"    → пропуск: хвилина {minute} > {MAX_MINUTE_FOOT}")
            continue

        # Фільтр 2: pre-match odds
        pre_odd = await fetch_prematch_odds_football(session, fid)
        if not pre_odd:
            cnt_odds += 1
            print(f"    → пропуск: pre-match odds не знайдено")
            continue
        if pre_odd >= FAV_THRESHOLD_FOOT:
            cnt_odds += 1
            print(f"    → пропуск: pre_odd={pre_odd} >= {FAV_THRESHOLD_FOOT} (не фаворит)")
            continue

        # Фільтр 3: рахунок — фаворит (home) програє
        home_losing      = score_h < score_a
        is_00_second_half = (score_h == 0 and score_a == 0 and minute >= 46)

        if not home_losing and not is_00_second_half:
            cnt_score += 1
            print(f"    → пропуск: рахунок {score_h}:{score_a} не підходить")
            continue

        # Фільтр 4: live odds (запит тільки для матчів що пройшли всі фільтри)
        live_odd = await fetch_live_odds_football(session, fid)
        if live_odd is None:
            cnt_live += 1
            print(f"    → пропуск: live odds недоступні, сигнал не генеруємо")
            continue

        rise = round(((live_odd - pre_odd) / pre_odd) * 100)
        print(f"    → pre_odd={pre_odd} live_odd={live_odd} rise={rise}% (мін={MIN_ODDS_RISE_FOOT}%)")

        if rise < MIN_ODDS_RISE_FOOT:
            cnt_live += 1
            print(f"    → пропуск: ріст {rise}% < {MIN_ODDS_RISE_FOOT}%")
            continue

        # ── Сигнал ────────────────────────────────────────────────────────
        if is_00_second_half and not home_losing:
            key = f"foot_{fid}_00_2h"
            if key in notified:
                print(f"    → вже надсилали: {key}")
                continue
            notified[key] = now_kyiv().timestamp()
            add_signal(f"{home} 0:0 {away} 2-й тайм")
            cnt_signals += 1
            await send_msg(session,
                f"🚨 *СИГНАЛ: ФАВОРИТ НЕ ЗАБИВАЄ*\n\n"
                f"⚽ {league_name}\n"
                f"*{home}* 0:0 *{away}*\n"
                f"⏱ Хвилина: {minute}' (2-й тайм)\n"
                f"📉 Коеф до матчу: `{pre_odd}`\n"
                f"📈 Коеф зараз: `{live_odd}` \\(+{rise}%\\)\n"
                f"💡 Фаворит без голів у другому таймі\n"
                f"💪 {strength(rise)}"
            )
            print(f"  ⚽ СИГНАЛ 0:0: {home} vs {away} {minute}' +{rise}%")
        else:
            key = f"foot_{fid}_{score_h}_{score_a}"
            if key in notified:
                print(f"    → вже надсилали: {key}")
                continue
            notified[key] = now_kyiv().timestamp()
            add_signal(f"{home} {score_h}:{score_a} {away}")
            cnt_signals += 1
            await send_msg(session,
                f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                f"⚽ {league_name}\n"
                f"*{home}* {score_h}:{score_a} *{away}*\n"
                f"⏱ Хвилина: {minute}'\n"
                f"📉 Коеф до матчу: `{pre_odd}`\n"
                f"📈 Коеф зараз: `{live_odd}` \\(+{rise}%\\)\n"
                f"💪 {strength(rise)}"
            )
            print(f"  ⚽ СИГНАЛ: {home} {score_h}:{score_a} {away} +{rise}%")

    print(
        f"  [⚽ ПІДСУМОК] всього={total} | "
        f"статус/хв={cnt_minute} | odds={cnt_odds} | "
        f"рахунок={cnt_score} | live_odds={cnt_live} | "
        f"сигналів={cnt_signals}"
    )

# ── ГОЛОВНИЙ СКАН ─────────────────────────────────────────────────────────
async def scan(session):
    if not is_running:
        return
    stats["scans_total"] += 1
    print(f"[{now_kyiv().strftime('%H:%M:%S')}] Скан #{stats['scans_total']}...")
    if stats["scans_total"] % 10 == 0:
        cleanup_old_data()
    await scan_football(session)
    print(f"  Сигналів всього: {stats['signals_total']}")

# ── MAIN ──────────────────────────────────────────────────────────────────
async def main():
    stats["started_at"] = now_kyiv().strftime("%H:%M %d.%m.%Y")
    print("=" * 50)
    print("  FavTracker Bot — Футбол")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        await send_msg(session,
            "✅ *FavTracker запущено\\!*\n\n"
            "Відстежую:\n"
            "⚽ Футбол \\(MLS, Бразилія, Аргентина та ін\\.\\)\n\n"
            f"⏱ Скан кожні {POLL_INTERVAL // 60} хвилин\n"
            f"📡 API ліміт: {api_requests['football']['limit']} запитів/день"
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
                    reset_counters_if_needed()
                    newly_resumed = _prev_paused - _quota_paused
                    if newly_resumed:
                        labels = ", ".join(
                            {"football": "⚽ Футбол"}.get(s, s)
                            for s in newly_resumed
                        )
                        await send_msg(session,
                            f"✅ *Ліміт запитів поновлено!*\n\n"
                            f"Новий день — {api_requests['football']['limit']} запитів.\n"
                            f"Скан поновлено: {labels}"
                        )
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
