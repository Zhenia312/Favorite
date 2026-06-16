import os
import re
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
FAV_THRESHOLD_FOOT = 2.50
MIN_ODDS_RISE_FOOT = 15
MAX_MINUTE_FOOT    = 90

# Розширення №4 / №5: пороги для сигналів "не виграє" / "без голів"
NOT_WINNING_MIN_MINUTE  = 60
NOT_WINNING_FAV_ODD_MAX = 1.80

# Розширення №6: альтернативне визначення фаворита через співвідношення коефіцієнтів
MIN_ODDS_GAP_RATIO = 2.0

# Статуси матчу що вважаються live (п.7 ТЗ)
LIVE_STATUSES = {"1H", "2H", "HT", "ET", "BT", "P"}

# Ринки odds що перевіряємо (п.1, п.6 ТЗ)
ODDS_MARKETS = {"Match Winner", "1X2", "Winner", "Full Time Result"}

# TTL кешу pre-match odds — 6 годин (п.5 ТЗ)
PRE_ODDS_TTL = 6 * 3600

# ── СТАН ──────────────────────────────────────────────────────────────────
# pre_odds: fixture_id -> {"home": float, "away": float, "ts": timestamp}
pre_odds   = {}
notified   = {}   # key -> timestamp
is_running = True
offset     = 0

football_enabled = True

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

# ── MARKDOWN ЗАХИСТ (п.8 ТЗ) ─────────────────────────────────────────────
def escape_md(text: str) -> str:
    """Екранує спецсимволи Telegram Markdown v1."""
    # У Markdown v1 небезпечні: _ * ` [
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, f"\\{ch}")
    return text

# ── КЛАВІАТУРИ ────────────────────────────────────────────────────────────
def main_keyboard():
    f = "✅" if football_enabled else "❌"
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}, {"text": "🔍 Діагностика"}],
            [{"text": f"{f} Футбол (всі ліги)"}],
            [{"text": "📅 Розклад"}],
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
            resp = await r.json()
            if not resp.get("ok"):
                print(f"[TG ERROR] {resp}")
            return resp
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

async def check_quota(session):
    left  = requests_left("football")
    limit = api_requests["football"]["limit"]
    warn_threshold = max(20, int(limit * 0.05))

    if left == 0:
        if "football" not in _quota_paused:
            _quota_paused.add("football")
            await send_msg(session,
                f"🚫 *Запити вичерпано!*\n\n"
                f"Ліміт {limit} запитів на сьогодні витрачено.\n"
                f"Скан призупинено до 00:00 (Київ).\n"
                f"🕐 Зараз: {now_kyiv().strftime('%H:%M')}"
            )
            print(f"[QUOTA] вичерпано, призупинено")
        return False

    if left <= warn_threshold and "football" not in _quota_warned:
        _quota_warned.add("football")
        await send_msg(session,
            f"⚠️ *Залишилось мало запитів!*\n\n"
            f"Залишок: *{left}/{limit}*\n"
            f"Розглянь збільшення інтервалу."
        )
        print(f"[QUOTA] попередження: залишилось {left}")
    return True

# ── ОЧИЩЕННЯ СТАРИХ ДАНИХ ─────────────────────────────────────────────────
def cleanup_old_data():
    now_ts = now_kyiv().timestamp()
    cutoff_notified = 12 * 3600

    old_notified = [k for k, ts in notified.items() if now_ts - ts > cutoff_notified]
    for k in old_notified:
        del notified[k]

    old_odds = [k for k, v in pre_odds.items() if now_ts - v.get("ts", 0) > PRE_ODDS_TTL]
    for k in old_odds:
        del pre_odds[k]

    if old_notified or old_odds:
        print(f"[CLEANUP] notified={len(old_notified)} odds={len(old_odds)}")

# ── ОБРОБКА КОМАНД ────────────────────────────────────────────────────────
async def process_commands(session):
    global is_running, offset, POLL_INTERVAL, football_enabled
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

        elif "розклад" in text:
            await send_schedule(session)

        elif "інтервал" in text or text == "/interval":
            user_state["menu"] = "set_interval"
            await send_msg(session,
                f"⏱ *Поточний інтервал: {POLL_INTERVAL // 60} хв*\n\nВибери новий:",
                kb=interval_keyboard()
            )

        elif "футбол" in text:
            football_enabled = not football_enabled
            icon  = "✅" if football_enabled else "❌"
            state = "увімкнено" if football_enabled else "вимкнено"
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
            data     = await r.json()
            fixtures = data.get("response", [])
            errors   = data.get("errors", {})
            print(f"[РОЗКЛАД] date={today} results={data.get('results',0)} errors={errors}")
            if r.status != 200:
                print(f"[РОЗКЛАД] ⚠️ HTTP {r.status}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка запиту: {escape_md(str(e))}")
        return

    hours = {}
    for fix in fixtures:
        try:
            dt_str = fix.get("fixture", {}).get("date", "")
            if "T" in dt_str:
                kyiv_hour = (int(dt_str[11:13]) + 3) % 24
                hours[kyiv_hour] = hours.get(kyiv_hour, 0) + 1
        except Exception:
            continue

    hour_lines = (
        "\n".join(f"  {h:02d}:00 — {hours[h]} матч(ів)" for h in sorted(hours))
        if hours else "  немає матчів"
    )
    lines = [
        f"📅 *Розклад футболу* ({today}, Київ)\n",
        f"⚽ Всього матчів сьогодні: *{len(fixtures)}*\n",
        hour_lines,
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
            print(f"[ДІАГНОСТИКА] status={r.status} results={data.get('results',0)} errors={errors} len={len(fixtures)}")
            if r.status != 200:
                print(f"[ДІАГНОСТИКА] ⚠️ HTTP {r.status}: {data}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка: {escape_md(str(e))}")
        return

    leagues_live = {}
    for fix in fixtures:
        lg = fix.get("league", {}).get("name", "Невідома")
        leagues_live[lg] = leagues_live.get(lg, 0) + 1

    left  = requests_left("football")
    limit = api_requests["football"]["limit"]
    used  = api_requests["football"]["used"]

    lines = [
        "🔍 *Діагностика — Live матчі зараз*\n",
        f"⚽ Всього в лайві: *{len(fixtures)} матчів*",
    ]
    if leagues_live:
        lines.append("\n*По лігах:*")
        for lg, cnt in sorted(leagues_live.items(), key=lambda x: -x[1])[:10]:
            lines.append(f"  • {escape_md(lg)}: {cnt}")
        if len(leagues_live) > 10:
            lines.append(f"  ...і ще {len(leagues_live) - 10} ліг")

    lines += [
        f"\n📡 Запитів сьогодні: {used}/{limit} (залишок: {left})",
        f"⚠️ Витрачено 1 запит",
    ]
    if errors:
        lines.append(f"\n❌ Помилка API: {errors}")
    await send_msg(session, "\n".join(lines))

# ── СТАТИСТИКА ────────────────────────────────────────────────────────────
async def send_stat(session):
    left  = requests_left("football")
    limit = api_requests["football"]["limit"]
    used  = api_requests["football"]["used"]

    lines = [
        "📊 *Статистика FavTracker*\n",
        f"🕐 Запущено: {stats['started_at']}",
        f"🔍 Сканів: {stats['scans_total']}",
        f"🚨 Сигналів: {stats['signals_total']}",
        f"⚡ Статус: {'▶️ Активний' if is_running else '⏹ Зупинений'}",
        f"⏱ Інтервал: {POLL_INTERVAL // 60} хв",
        f"⚽ Футбол: {'✅' if football_enabled else '❌'} (всі ліги API)\n",
        "📡 *Запити сьогодні:*",
        f"  використано: {used}/{limit}",
        f"  залишок: {left} {'⚠️' if left < max(20, int(limit * 0.05)) else ''}",
    ]
    if stats["last_signal"]:
        lines.append(f"\n📌 Останній сигнал: {escape_md(stats['last_signal'])}")
    await send_msg(session, "\n".join(lines))

def add_signal(description):
    stats["signals_total"]    += 1
    stats["signals_football"] += 1
    stats["last_signal"] = f"{description} ({now_kyiv().strftime('%H:%M')})"

# ── ФУТБОЛ API ────────────────────────────────────────────────────────────
async def fetch_football_live(session):
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with session.get(
            "https://v3.football.api-sports.io/fixtures?live=all",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            raw     = await r.json()
            results = raw.get("response", [])
            errors  = raw.get("errors", {})
            total   = raw.get("results", 0)
            print(f"  [API⚽] status={r.status} results={total} errors={errors} len={len(results)}")
            if r.status != 200:
                print(f"  [API⚽] ⚠️ HTTP {r.status}: {raw}")
            if errors:
                print(f"  [API⚽] ⚠️ errors: {errors}")
            return results
    except Exception as e:
        print(f"  [API⚽ ERROR] {e}")
        return []

def _extract_home_away_odds(data, fixture_id=None):
    """
    Витягує коефіцієнти Home та Away з відповіді /odds або /odds/live.
    Підтримує будь-якого букмекера і всі ринки з ODDS_MARKETS.
    Повертає (home_odd, away_odd) або (None, None).

    Розширення №7: якщо коефіцієнти не знайдено, виводить список доступних
    bet names для діагностики майбутніх ринків.
    """
    seen_bet_names = set()

    for entry in data:
        # Структура /odds: entry має "bookmakers"
        bookmakers = entry.get("bookmakers", [])
        # Структура /odds/live: entry має "odds" (список букмекерів)
        if not bookmakers:
            bookmakers = entry.get("odds", [])

        for bk in bookmakers:
            bets = bk.get("bets", [])
            for bet in bets:
                bet_name = bet.get("name")
                if bet_name:
                    seen_bet_names.add(bet_name)
                if bet_name in ODDS_MARKETS:
                    values = bet.get("values", [])
                    home_odd = away_odd = None
                    for v in values:
                        val  = v.get("value", "")
                        odd  = v.get("odd")
                        try:
                            odd = float(odd)
                        except (TypeError, ValueError):
                            continue
                        if val in ("Home", "1"):
                            home_odd = odd
                        elif val in ("Away", "2"):
                            away_odd = odd
                    if home_odd is not None and away_odd is not None:
                        return home_odd, away_odd

    # Не вдалось знайти Home/Away ні в одному з відомих ринків (п. Розширення №7)
    if seen_bet_names:
        fid_label = f"fixture={fixture_id} " if fixture_id is not None else ""
        print(f"    [ODDS MARKET] {fid_label}доступні ринки без Home/Away:")
        for name in sorted(seen_bet_names):
            print(f"      - {name}")

    return None, None

async def fetch_prematch_odds_football(session, fixture_id):
    """
    Повертає dict {"home": float, "away": float, "fav_side": str, "fav_odd": float}
    або None якщо odds недоступні.
    Кешує результат з TTL.
    """
    now_ts = now_kyiv().timestamp()

    # Перевіряємо кеш (п.5 ТЗ)
    if fixture_id in pre_odds:
        cached = pre_odds[fixture_id]
        if now_ts - cached.get("ts", 0) < PRE_ODDS_TTL:
            return cached
        else:
            del pre_odds[fixture_id]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        # Без bookmaker=6 — беремо будь-якого (п.1 ТЗ)
        async with session.get(
            f"https://v3.football.api-sports.io/odds?fixture={fixture_id}",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            raw  = await r.json()
            data = raw.get("response", [])
            errors = raw.get("errors", {})
            print(f"    [ODDS⚽] fixture={fixture_id} status={r.status} results={len(data)} errors={errors}")

            if r.status != 200:
                print(f"    [ODDS⚽] ⚠️ HTTP {r.status}: {raw}")
                return None
            if errors:
                print(f"    [ODDS⚽] ⚠️ errors: {errors}")
            if not data:
                print(f"    [ODDS⚽] ❌ порожня відповідь")
                return None

            home_odd, away_odd = _extract_home_away_odds(data, fixture_id=fixture_id)
            print(f"    [ODDS⚽] fixture={fixture_id} home={home_odd} away={away_odd}")

            if home_odd is None or away_odd is None:
                print(f"    [ODDS⚽] ❌ fixture={fixture_id} не вдалось витягти Home/Away odds")
                return None

            # Визначаємо фаворита (п.2 ТЗ)
            if home_odd <= away_odd:
                fav_side = "home"
                fav_odd  = home_odd
            else:
                fav_side = "away"
                fav_odd  = away_odd

            # Розширення №6: альтернативне визначення фаворита через gap ratio.
            # Якщо коефіцієнт фаворита трохи перевищує FAV_THRESHOLD_FOOT, але
            # розрив між командами достатньо великий — все одно вважаємо
            # цю сторону фаворитом (позначаємо це окремим прапорцем).
            max_odd = max(home_odd, away_odd)
            min_odd = min(home_odd, away_odd)
            gap_ratio = (max_odd / min_odd) if min_odd > 0 else 0
            fav_by_gap = gap_ratio >= MIN_ODDS_GAP_RATIO

            result = {
                "home": home_odd,
                "away": away_odd,
                "fav_side": fav_side,
                "fav_odd":  fav_odd,
                "gap_ratio": gap_ratio,
                "fav_by_gap": fav_by_gap,
                "ts": now_ts,
                "fixture_id": fixture_id,
            }
            pre_odds[fixture_id] = result
            print(f"    [ODDS⚽] ✅ fixture={fixture_id} фаворит={fav_side} odd={fav_odd} gap_ratio={round(gap_ratio,2)}")
            return result

    except Exception as e:
        print(f"    [ODDS⚽ ERROR] fixture={fixture_id} {e}")
    return None

async def fetch_live_odds_football(session, fixture_id, fav_side):
    """
    Повертає live коефіцієнт для сторони фаворита або None.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(
            f"https://v3.football.api-sports.io/odds/live?fixture={fixture_id}",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            raw    = await r.json()
            data   = raw.get("response", [])
            errors = raw.get("errors", {})
            print(f"    [LIVE ODDS⚽] fixture={fixture_id} status={r.status} response={len(data)} errors={errors}")

            if r.status != 200:
                print(f"    [LIVE ODDS⚽] ⚠️ HTTP {r.status}: {raw}")
                return None
            if errors:
                print(f"    [LIVE ODDS⚽] ⚠️ errors: {errors}")
            if not data:
                print(f"    [LIVE ODDS⚽] ❌ порожня відповідь. RAW: {raw}")
                return None

            home_odd, away_odd = _extract_home_away_odds(data, fixture_id=fixture_id)
            print(f"    [LIVE ODDS⚽] fixture={fixture_id} home={home_odd} away={away_odd}")

            if home_odd is None or away_odd is None:
                print(f"    [LIVE ODDS⚽] ❌ fixture={fixture_id} не вдалось витягти odds")
                return None

            return home_odd if fav_side == "home" else away_odd

    except Exception as e:
        print(f"    [LIVE ODDS⚽ ERROR] fixture={fixture_id} {e}")
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
    if not football_enabled:
        print("  [⚽] вимкнено, пропускаємо")
        return
    if not await check_quota(session):
        return

    fixtures = await fetch_football_live(session)
    await asyncio.sleep(1)
    await _process_football_fixtures(session, fixtures)

async def _process_football_fixtures(session, fixtures):
    total = len(fixtures)
    cnt_status = cnt_odds = cnt_score = cnt_live = cnt_signals = 0

    # Розширення №8: лічильники причин відмов
    reasons = {
        "no_prematch_odds":  0,
        "no_live_odds":      0,
        "fav_odd_too_high":  0,
        "fav_not_losing":    0,
        "ht_skipped":        0,
        "minute_skipped":    0,
    }

    print(f"  [⚽ СКАН] Матчів отримано: {total}")

    for fix in fixtures:
        # ── Усі дані матчу витягуємо у локальні змінні на початку циклу,
        # щоб виключити будь-яке витікання значень з попередньої ітерації
        # (Проблема №1 ТЗ). Жодна змінна нижче не використовується поза
        # межами цієї ітерації for-циклу.
        fid          = None
        odds_data    = None
        fav_side     = None
        fav_odd      = None
        fav_team     = None
        und_team     = None

        try:
            fid          = fix["fixture"]["id"]
            league_name  = fix.get("league", {}).get("name", "")
            minute       = fix["fixture"]["status"].get("elapsed") or 0
            status_short = fix["fixture"]["status"].get("short", "")
            home         = fix["teams"]["home"]["name"]
            away         = fix["teams"]["away"]["name"]
            score_h      = fix["goals"].get("home") if fix["goals"].get("home") is not None else 0
            score_a      = fix["goals"].get("away") if fix["goals"].get("away") is not None else 0

            print(f"  [⚽] fid={fid} | {home} {score_h}:{score_a} {away} | хв={minute} | статус={status_short} | {league_name}")

            # Фільтр 1: статус матчу (п.7 ТЗ)
            if status_short not in LIVE_STATUSES:
                cnt_status += 1
                print(f"    → fid={fid} пропуск: статус '{status_short}' не в LIVE_STATUSES")
                continue

            # Проблема №2: HT — лише моніторинг, без розрахунку сигналів,
            # без перевірки рахунку, без перевірки live odds.
            if status_short == "HT":
                reasons["ht_skipped"] += 1
                print(f"    → fid={fid} пропуск: перерва HT")
                continue

            if minute > MAX_MINUTE_FOOT:
                cnt_status += 1
                reasons["minute_skipped"] += 1
                print(f"    → fid={fid} пропуск: хвилина {minute} > {MAX_MINUTE_FOOT}")
                continue

            # Фільтр 2: pre-match odds + визначення фаворита (п.1, п.2 ТЗ)
            odds_data = await fetch_prematch_odds_football(session, fid)
            if odds_data is None:
                cnt_odds += 1
                reasons["no_prematch_odds"] += 1
                print(f"    → fid={fid} пропуск: pre-match odds не знайдено")
                continue

            # Захист від неузгодженості кешу (Проблема №1): кеш ключується
            # по fixture_id, тож odds_data мають належати саме цьому fid.
            if odds_data.get("fixture_id") != fid:
                print(f"    → fid={fid} ⚠️ ПОМИЛКА УЗГОДЖЕНОСТІ: odds_data належить fixture={odds_data.get('fixture_id')}, пропуск")
                cnt_odds += 1
                reasons["no_prematch_odds"] += 1
                continue

            fav_side = odds_data["fav_side"]
            fav_odd  = odds_data["fav_odd"]
            fav_team = home if fav_side == "home" else away
            und_team = away if fav_side == "home" else home

            print(f"    → fid={fid} фаворит={fav_team} ({fav_side}) odd={fav_odd}")

            # Розширення №6: фаворит проходить, якщо коефіцієнт нижче порогу
            # ИЛИ розрив коефіцієнтів достатньо великий (gap ratio).
            passes_threshold = fav_odd < FAV_THRESHOLD_FOOT
            passes_gap       = odds_data.get("fav_by_gap", False)

            if not passes_threshold and not passes_gap:
                cnt_odds += 1
                reasons["fav_odd_too_high"] += 1
                print(f"    → fid={fid} пропуск: fav_odd={fav_odd} >= {FAV_THRESHOLD_FOOT} і gap_ratio={round(odds_data.get('gap_ratio',0),2)} < {MIN_ODDS_GAP_RATIO}")
                continue
            elif not passes_threshold and passes_gap:
                print(f"    → fid={fid} фаворит підтверджено через gap_ratio={round(odds_data.get('gap_ratio',0),2)} (odd={fav_odd} >= порогу)")

            # Фільтр 3: рахунок — програє саме фаворит (п.3 ТЗ)
            if fav_side == "home":
                fav_losing = score_h < score_a
                fav_drawing = score_h == score_a
                fav_score  = score_h
                und_score  = score_a
            else:
                fav_losing = score_a < score_h
                fav_drawing = score_h == score_a
                fav_score  = score_a
                und_score  = score_h

            is_00_second_half = (score_h == 0 and score_a == 0 and minute >= 46)

            # ── Розширення №4: "ФАВОРИТ НЕ ВИГРАЄ" ──────────────────────
            # хвилина >= 60, рахунок нічийний (фаворит не веде), fav_odd <= 1.80
            not_winning_candidate = (
                fav_drawing
                and minute >= NOT_WINNING_MIN_MINUTE
                and fav_odd <= NOT_WINNING_FAV_ODD_MAX
            )

            if not fav_losing and not is_00_second_half and not not_winning_candidate:
                cnt_score += 1
                reasons["fav_not_losing"] += 1
                print(f"    → fid={fid} пропуск: фаворит не програє (рахунок {score_h}:{score_a}, side={fav_side})")
                continue

            # Фільтр 4: перевіряємо чи вже надсилали сигнал (п.9 ТЗ)
            # Окремі ключі для різних типів сигналів — один сигнал кожного
            # типу на матч (п.4 фінальної перевірки).
            key_losing      = f"foot_{fid}_fav_losing"
            key_not_winning = f"foot_{fid}_not_winning"
            key_no_goals    = f"foot_{fid}_no_goals"

            # Визначаємо, який тип сигналу зараз розглядаємо
            is_no_goals_case     = is_00_second_half and minute >= NOT_WINNING_MIN_MINUTE and fav_odd <= NOT_WINNING_FAV_ODD_MAX
            is_not_winning_case  = not_winning_candidate and not is_00_second_half
            is_losing_case       = fav_losing

            if is_losing_case:
                active_key = key_losing
            elif is_no_goals_case:
                active_key = key_no_goals
            elif is_not_winning_case:
                active_key = key_not_winning
            else:
                # Старий 0:0-у-другому-таймі кейс без виконання нових порогів —
                # зберігаємо стару поведінку (сигнал "ФАВОРИТ НЕ ЗАБИВАЄ")
                active_key = key_no_goals

            if active_key in notified:
                print(f"    → fid={fid} вже надсилали: {active_key}")
                continue

            # Фільтр 5: live odds (тільки для матчів що пройшли всі фільтри)
            live_odd = await fetch_live_odds_football(session, fid, fav_side)
            if live_odd is None:
                cnt_live += 1
                reasons["no_live_odds"] += 1
                print(f"    → fid={fid} пропуск: live odds недоступні")
                continue

            rise = round(((live_odd - fav_odd) / fav_odd) * 100)
            print(f"    → fid={fid} fav_odd={fav_odd} live_odd={live_odd} rise={rise}% (мін={MIN_ODDS_RISE_FOOT}%)")

            if rise < MIN_ODDS_RISE_FOOT:
                cnt_live += 1
                reasons["no_live_odds"] += 1
                print(f"    → fid={fid} пропуск: ріст {rise}% < {MIN_ODDS_RISE_FOOT}%")
                continue

            # ── Генеруємо сигнал ──────────────────────────────────────────
            notified[active_key] = now_kyiv().timestamp()
            cnt_signals += 1

            lg_safe   = escape_md(league_name)
            fav_safe  = escape_md(fav_team)
            und_safe  = escape_md(und_team)
            home_safe = escape_md(home)
            away_safe = escape_md(away)

            if is_losing_case:
                add_signal(f"{fav_team} програє {fav_score}:{und_score}")
                await send_msg(session,
                    f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* {score_h}:{score_a} *{away_safe}*\n"
                    f"⏱ Хвилина: {minute}'\n"
                    f"🎯 Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"📉 Коеф до матчу: `{fav_odd}`\n"
                    f"📈 Коеф зараз: `{live_odd}` (+{rise}%)\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid}: {fav_team} програє {fav_score}:{und_score} +{rise}%")

            elif is_no_goals_case:
                add_signal(f"{fav_team} 0:0 {und_team} 2-й тайм")
                await send_msg(session,
                    f"🚨 *СИГНАЛ: ФАВОРИТ БЕЗ ГОЛІВ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* 0:0 *{away_safe}*\n"
                    f"⏱ Хвилина: {minute}' (2-й тайм)\n"
                    f"🎯 Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"📉 Коеф до матчу: `{fav_odd}`\n"
                    f"📈 Коеф зараз: `{live_odd}` (+{rise}%)\n"
                    f"💡 Фаворит без голів у другому таймі\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid} 0:0: {fav_team} vs {und_team} {minute}' +{rise}%")

            elif is_not_winning_case:
                add_signal(f"{fav_team} не виграє {score_h}:{score_a}")
                await send_msg(session,
                    f"🚨 *СИГНАЛ: ФАВОРИТ НЕ ВИГРАЄ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* {score_h}:{score_a} *{away_safe}*\n"
                    f"⏱ Хвилина: {minute}'\n"
                    f"🎯 Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"📉 Коеф до матчу: `{fav_odd}`\n"
                    f"📈 Коеф зараз: `{live_odd}` (+{rise}%)\n"
                    f"💡 Фаворит не веде в рахунку\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid} не виграє: {fav_team} {score_h}:{score_a} +{rise}%")

        except Exception as e:
            print(f"  [⚽ ПОМИЛКА МАТЧУ fid={fix.get('fixture',{}).get('id','?')}] {e}")
            continue

    print(
        f"  [⚽ ПІДСУМОК] всього={total} | "
        f"статус={cnt_status} | odds={cnt_odds} | "
        f"рахунок={cnt_score} | live_odds={cnt_live} | "
        f"сигналів={cnt_signals}"
    )
    print(
        f"  [ПІДСУМОК] "
        f"no_prematch_odds={reasons['no_prematch_odds']} | "
        f"no_live_odds={reasons['no_live_odds']} | "
        f"fav_odd_too_high={reasons['fav_odd_too_high']} | "
        f"fav_not_losing={reasons['fav_not_losing']} | "
        f"ht_skipped={reasons['ht_skipped']} | "
        f"minute_skipped={reasons['minute_skipped']}"
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
    print("  FavTracker Bot — Футбол (всі ліги)")
    print("=" * 50)

    async with aiohttp.ClientSession() as session:
        await send_msg(session,
            "✅ *FavTracker запущено\\!*\n\n"
            "⚽ Моніторинг футболу по *всіх лігах* API\n\n"
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
                        limit = api_requests["football"]["limit"]
                        await send_msg(session,
                            f"✅ *Ліміт запитів поновлено!*\n\n"
                            f"Новий день — {limit} запитів.\n"
                            f"Сканування поновлено."
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
