import os
import re
import asyncio
import aiohttp
import sqlite3
from datetime import datetime, timezone, timedelta

# ── КОНФІГ ────────────────────────────────────────────────────────────────
TG_TOKEN   = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API_KEY    = os.environ.get("API_KEY", "")

POLL_INTERVAL = 300

# ── TEST_MODE: новий фільтр перевіряється паралельно зі старим,    ────────
#    сигнали нового фільтра йдуть ТІЛЬКИ в тестовий чат, не змінюючи
#    і не дублюючи основний потік. Старий код нижче ніяк не змінений.
TEST_MODE     = os.environ.get("TEST_MODE", "false").lower() == "true"
TEST_CHAT_ID  = os.environ.get("TEST_CHAT_ID", "")

KYIV_TZ = timezone(timedelta(hours=3))

def now_kyiv():
    return datetime.now(timezone.utc).astimezone(KYIV_TZ)

# ── СТАТИСТИКА СИГНАЛІВ (SQLite на Railway Volume) ──────────────────────
DB_PATH = os.environ.get("STATS_DB_PATH", "/data/stats.db")
RESULTS_CHECK_INTERVAL = 600  # перевірка результатів раз на 10 хв

SIGNAL_TYPE_LABELS = {
    "fav_losing":     "Фаворит програє",
    "no_goals":       "Фаворит без голів",
    "strong_cant_win": "Сильний фаворит не може виграти",
    "not_winning":    "Фаворит не виграє",
    "sot_total_high": "Багато ударів у ворота",
}

FINISHED_STATUSES = {"FT", "AET", "PEN"}
VOID_STATUSES     = {"CANC", "ABD", "PST", "SUSP", "AWD", "WO"}

def init_db():
    try:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id INTEGER NOT NULL,
                signal_type TEXT NOT NULL,
                league TEXT,
                fav_team TEXT,
                und_team TEXT,
                fav_side TEXT,
                pre_odd REAL,
                live_odd REAL,
                rise_pct REAL,
                score_at_signal TEXT,
                minute_at_signal INTEGER,
                signal_time TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                final_score TEXT,
                result TEXT,
                checked_time TEXT,
                edge_side TEXT,
                edge_result TEXT
            )
        """)
        # ── МІГРАЦІЯ: додаємо edge_side/edge_result, якщо таблиця вже існувала
        # на Railway без цих колонок (старіший деплой). Для sot_total_high:
        # - result      = "win"/"loss" по тому самому критерію, що й у решти
        #                 типів (фаворит не програв) — fav_outcome з обговорення.
        # - edge_side   = хто бив більше в площину на момент сигналу: fav/underdog/equal
        # - edge_result = чи саме сторона з перевагою по ударах не програла (edge_outcome)
        # Для всіх інших типів сигналів edge_side/edge_result лишаються NULL.
        for col, col_type in [("edge_side", "TEXT"), ("edge_result", "TEXT")]:
            try:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # колонка вже існує
        # ── ТЕСТОВА ТАБЛИЦЯ: окремо від основної "signals" ────────────────
        # Сюди пишемо і ті сигнали, що пройшли новий фільтр (passed_filter=1),
        # і ті, що НЕ пройшли (passed_filter=0) — щоб потім порівняти
        # винрейт нового фільтра на свіжих даних.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fixture_id INTEGER NOT NULL,
                signal_type TEXT NOT NULL,
                league TEXT,
                fav_team TEXT,
                und_team TEXT,
                fav_side TEXT,
                pre_odd REAL,
                live_odd REAL,
                rise_pct REAL,
                score_at_signal TEXT,
                minute_at_signal INTEGER,
                shots_on_target_fav INTEGER,
                shots_on_target_und INTEGER,
                red_card_und INTEGER,
                passed_filter INTEGER NOT NULL,
                skip_reason TEXT,
                signal_time TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                final_score TEXT,
                result TEXT,
                checked_time TEXT
            )
        """)
        conn.commit()
        conn.close()
        print(f"[DB] Базу статистики ініціалізовано: {DB_PATH}")
    except Exception as e:
        print(f"[DB ERROR] init_db: {e}")

def save_signal(fixture_id, signal_type, league, fav_team, und_team, fav_side,
                 pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                 edge_side=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO signals (
                fixture_id, signal_type, league, fav_team, und_team, fav_side,
                pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                signal_time, status, edge_side
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            fixture_id, signal_type, league, fav_team, und_team, fav_side,
            pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
            now_kyiv().strftime("%Y-%m-%d %H:%M:%S"), edge_side,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] save_signal fid={fixture_id}: {e}")

def save_test_signal(fixture_id, signal_type, league, fav_team, und_team, fav_side,
                      pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                      shots_on_target_fav=None, shots_on_target_und=None,
                      red_card_und=0, passed_filter=0, skip_reason=None):
    """
    Записує сигнал у тестову таблицю test_signals — НЕЗАЛЕЖНО від основної
    таблиці signals. Пишемо сюди як ті сигнали, що пройшли новий фільтр,
    так і ті, що не пройшли (з причиною в skip_reason) — щоб через 1-2
    тижні порівняти, як новий фільтр відсіює сигнали і який у нього винрейт.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO test_signals (
                fixture_id, signal_type, league, fav_team, und_team, fav_side,
                pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                shots_on_target_fav, shots_on_target_und, red_card_und,
                passed_filter, skip_reason, signal_time, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            fixture_id, signal_type, league, fav_team, und_team, fav_side,
            pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
            shots_on_target_fav, shots_on_target_und, int(red_card_und),
            int(passed_filter), skip_reason,
            now_kyiv().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] save_test_signal fid={fixture_id}: {e}")

def _signal_is_success(signal_type, fav_score, und_score):
    """
    Критерій успіху сигналу:
    - 'no_goals' (ФАВОРИТ БЕЗ ГОЛІВ) — успіх ТІЛЬКИ якщо фаворит виграв
    - всі інші типи — успіх якщо фаворит не програв (виграв або зіграв нічию)
    """
    if signal_type == "no_goals":
        return fav_score > und_score
    return fav_score >= und_score

async def check_pending_results(session):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, fixture_id, signal_type, fav_side, edge_side FROM signals WHERE status='pending'"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] check_pending_results read: {e}")
        return

    if not rows:
        return

    fixture_ids = sorted(set(r[1] for r in rows))
    print(f"[РЕЗУЛЬТАТИ] Перевіряю {len(rows)} сигнал(ів) по {len(fixture_ids)} матч(ах)...")

    fixture_data = {}
    for i in range(0, len(fixture_ids), 20):
        chunk     = fixture_ids[i:i + 20]
        ids_param = "-".join(str(x) for x in chunk)
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with session.get(
                f"https://v3.football.api-sports.io/fixtures?ids={ids_param}",
                headers={"x-apisports-key": API_KEY},
                timeout=timeout
            ) as r:
                track_request("football")
                data = await r.json()
                for fix in data.get("response", []):
                    fid = fix.get("fixture", {}).get("id")
                    if fid is not None:
                        fixture_data[fid] = fix
        except Exception as e:
            print(f"[РЕЗУЛЬТАТИ] помилка запиту по чанку {ids_param}: {e}")
            continue

    try:
        conn = sqlite3.connect(DB_PATH)
        updated = 0
        for row_id, fixture_id, signal_type, fav_side, edge_side in rows:
            fix = fixture_data.get(fixture_id)
            if not fix:
                continue

            status_short = fix.get("fixture", {}).get("status", {}).get("short", "")

            if status_short in VOID_STATUSES:
                conn.execute(
                    "UPDATE signals SET status='voided', checked_time=? WHERE id=?",
                    (now_kyiv().strftime("%Y-%m-%d %H:%M:%S"), row_id)
                )
                updated += 1
                print(f"  [РЕЗУЛЬТАТИ] fid={fixture_id} анульовано (статус {status_short})")
                continue

            if status_short not in FINISHED_STATUSES:
                continue  # матч ще не закінчився

            goals   = fix.get("goals") or {}
            score_h = goals.get("home")
            score_a = goals.get("away")
            if score_h is None or score_a is None:
                continue

            fav_score = score_h if fav_side == "home" else score_a
            und_score = score_a if fav_side == "home" else score_h
            success     = _signal_is_success(signal_type, fav_score, und_score)
            result      = "win" if success else "loss"
            final_score = f"{score_h}:{score_a}"

            # ── edge_result (тільки для sot_total_high, де edge_side не NULL) ─
            # "win" якщо сторона з перевагою по ударах (edge_side) не програла.
            # Якщо edge_side="equal" (рівні удари) — edge_result лишаємо NULL,
            # бо тут нема кого перевіряти на перевагу.
            edge_result = None
            if edge_side == "fav":
                edge_result = "win" if fav_score >= und_score else "loss"
            elif edge_side == "underdog":
                edge_result = "win" if und_score >= fav_score else "loss"

            conn.execute(
                "UPDATE signals SET status='completed', final_score=?, result=?, checked_time=?, edge_result=? WHERE id=?",
                (final_score, result, now_kyiv().strftime("%Y-%m-%d %H:%M:%S"), edge_result, row_id)
            )
            updated += 1
            print(f"  [РЕЗУЛЬТАТИ] fid={fixture_id} {signal_type} → {final_score} → {result}" + (f" (edge={edge_side}→{edge_result})" if edge_side else ""))

        conn.commit()
        conn.close()
        if updated:
            print(f"[РЕЗУЛЬТАТИ] Оновлено {updated} запис(ів)")
    except Exception as e:
        print(f"[DB ERROR] check_pending_results write: {e}")

async def send_results_stat(session):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT signal_type, league, result FROM signals WHERE status='completed'"
        ).fetchall()
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE status='pending'"
        ).fetchone()[0]
        conn.close()
    except Exception as e:
        await send_msg(session, f"❌ Помилка читання статистики: {escape_md(str(e))}")
        return

    if not rows:
        await send_msg(session,
            f"📈 *Статистика результатів*\n\n"
            f"Поки немає завершених сигналів для аналізу.\n"
            f"В очікуванні результату: {pending_count}"
        )
        return

    total = len(rows)
    wins  = sum(1 for _, _, res in rows if res == "win")
    pct   = round(wins / total * 100, 1) if total else 0

    by_type = {}
    for stype, _, res in rows:
        d = by_type.setdefault(stype, {"win": 0, "total": 0})
        d["total"] += 1
        if res == "win":
            d["win"] += 1

    by_league = {}
    for _, league, res in rows:
        key = league or "Невідома"
        d = by_league.setdefault(key, {"win": 0, "total": 0})
        d["total"] += 1
        if res == "win":
            d["win"] += 1

    lines = [
        "📈 *Статистика результатів сигналів*\n",
        f"Всього завершено: *{total}*",
        f"Успішних: *{wins}* ({pct}%)",
        f"В очікуванні результату: {pending_count}\n",
        "*По типах сигналів:*",
    ]
    for stype, d in sorted(by_type.items(), key=lambda x: -x[1]["total"]):
        label = SIGNAL_TYPE_LABELS.get(stype, stype)
        t_pct = round(d["win"] / d["total"] * 100, 1) if d["total"] else 0
        lines.append(f"  - {escape_md(label)}: {d['win']}/{d['total']} ({t_pct}%)")

    lines.append("\n*По лігах (топ-10):*")
    top_leagues = sorted(by_league.items(), key=lambda x: -x[1]["total"])[:10]
    for league, d in top_leagues:
        l_pct = round(d["win"] / d["total"] * 100, 1) if d["total"] else 0
        lines.append(f"  - {escape_md(league)}: {d['win']}/{d['total']} ({l_pct}%)")

    await send_msg(session, "\n".join(lines))

async def send_export_csv(session):
    """Вивантажує всі записи з таблиці signals у CSV-файл і відправляє в Telegram."""
    import csv
    import io

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("""
            SELECT id, fixture_id, signal_type, league, fav_team, und_team, fav_side,
                   pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                   signal_time, status, final_score, result, checked_time
            FROM signals
            ORDER BY id ASC
        """)
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        await send_msg(session, f"❌ Помилка читання БД: {escape_md(str(e))}")
        return

    if not rows:
        await send_msg(session, "📭 У базі ще немає жодного запису.")
        return

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    writer.writerows(rows)
    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM, щоб Excel коректно показав кирилицю

    filename = f"signals_export_{now_kyiv().strftime('%Y%m%d_%H%M')}.csv"
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"

    try:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(TG_CHAT_ID))
        form.add_field("caption", f"📦 Експорт статистики: {len(rows)} записів")
        form.add_field("document", csv_bytes, filename=filename, content_type="text/csv")
        async with session.post(url, data=form) as r:
            resp = await r.json()
            if not resp.get("ok"):
                await send_msg(session, f"❌ Не вдалось відправити файл: {escape_md(str(resp))}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка відправки файлу: {escape_md(str(e))}")

# ── ФУТБОЛ ────────────────────────────────────────────────────────────────
FAV_THRESHOLD_FOOT = 1.80
MIN_ODDS_RISE_FOOT = 30
MAX_MINUTE_FOOT    = 90

NOT_WINNING_MIN_MINUTE  = 60
NOT_WINNING_FAV_ODD_MAX = 1.50

MIN_ODDS_GAP_RATIO = 3.0

DRAW_SCORES_ALLOWED = {0, 1, 2, 3}

# ── ФІЛЬТР "ФАВОРИТ ПРОГРАЄ" (Варіант A, перевірено на 41 завершеному сигналі) ──
# Раніше fav_losing спрацьовував без обмеження по коефіцієнту і хвилині —
# тільки на FAV_THRESHOLD_FOOT=1.80 + MIN_ODDS_RISE_FOOT=30%. На зібраній
# статистиці без цього додаткового фільтра winrate ≈61%, з ним — ≈83%
# (n=12, +114% ROI на наявних даних). Це той самий фільтр, що раніше жив
# тільки в TEST_MODE — переносимо його в основний продакшн-сигнал.
LOSING_FAV_ODD_MAX       = 1.65
LOSING_MAX_SIGNAL_MINUTE = 40

# ── ТЕСТОВИЙ ФІЛЬТР (TEST_MODE) ────────────────────────────────────────────
# Це окремі, БІЛЬШ СУВОРІ пороги. Вони НЕ замінюють пороги вище і
# НЕ впливають на основний сигнал — використовуються лише для
# паралельної перевірки в тестовому чаті, коли TEST_MODE=true.
TEST_FAV_THRESHOLD_FOOT = 1.65   # фаворит має бути сильнішим (було 1.80)
TEST_MAX_SIGNAL_MINUTE  = 40     # сигнал fav_losing тільки до 40-ї хвилини (було без обмеження)

# Пороги по статистиці матчу (удари в створ, червоні картки) для
# тестового фільтра. Початкові значення підібрані "на око" — після
# накопичення статистики за 1-2 тижні їх можна перерахувати так само,
# як ми це робили з хвилиною і коефіцієнтом.
TEST_MIN_SHOTS_ON_TARGET_DIFF = 2   # фаворит має бити в створ мінімум на 2 більше за суперника
TEST_RED_CARD_BONUS = True          # якщо у андердога червона картка — це підсилює сигнал (бонус-умова, не обов'язкова)

# ── НОВИЙ ТИП СИГНАЛУ: "БАГАТО УДАРІВ У ВОРОТА" (sot_total_high) ───────────
# Незалежний від конкретного боку тип сигналу: спрацьовує коли СУМА ударів
# у площину воріт обох команд висока (гра відкрита, багато моментів),
# незалежно від того, хто саме б'є більше. У повідомленні просто вказуємо,
# на чию користь перевага по ударах — це інформація, не умова спрацювання.
# Перевіряється лише серед матчів, де фаворит вже програє/не виграє
# (той самий пул кандидатів, що й fav_losing/no_goals/strong_cant_win) —
# без додаткового сканування всіх live-матчів і без зайвих запитів до API.
SOT_TOTAL_MIN_SHOTS     = 8     # сума ударів у площину (фаворит + андердог) >= цього
SOT_TOTAL_MIN_MINUTE    = 30    # не раніше 30-ї хвилини, щоб сума встигла накопичитись

LIVE_CACHE_TTL = 60
live_odds_cache = {}

live_odds_retry_candidates = set()

LIVE_STATUSES = {"1H", "2H"}

ODDS_MARKETS = {
    "Match Winner",
    "1X2",
    "Winner",
    "Full Time Result",
    "Fulltime Result",
    "Match Winner 1X2",
    "Result",
    "3Way Result",
}

PRE_ODDS_TTL = 6 * 3600

# ── СТАН ──────────────────────────────────────────────────────────────────
pre_odds   = {}
notified   = {}
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

# ── MARKDOWN ЗАХИСТ ───────────────────────────────────────────────────────
def escape_md(text: str) -> str:
    """Екранує спецсимволи Telegram Markdown v1."""
    for ch in ["_", "*", "`", "["]:
        text = text.replace(ch, f"\\{ch}")
    return text

# ── КЛАВІАТУРИ ────────────────────────────────────────────────────────────
def main_keyboard():
    f = "✅" if football_enabled else "❌"
    return {
        "keyboard": [
            [{"text": "▶️ Старт"}, {"text": "⏹ Стоп"}],
            [{"text": "📊 Статистика"}, {"text": "📈 Результати"}],
            [{"text": "📦 Експорт"}],
            [{"text": "🔍 Діагностика"}],
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

async def send_test_msg(session, text):
    """
    Шле повідомлення ТІЛЬКИ в тестовий чат (TEST_CHAT_ID), без клавіатури.
    Якщо TEST_MODE вимкнено або TEST_CHAT_ID не задано — нічого не робить.
    Це гарантує, що тестовий фільтр ніяк не зачіпає основний чат TG_CHAT_ID.
    """
    if not TEST_MODE or not TEST_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id":    TEST_CHAT_ID,
        "text":       text,
        "parse_mode": "Markdown",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.post(url, json=payload, timeout=timeout) as r:
            resp = await r.json()
            if not resp.get("ok"):
                print(f"[TEST TG ERROR] {resp}")
            return resp
    except Exception as e:
        print(f"[TEST TG ERROR] {e}")

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
                f"Зараз: {now_kyiv().strftime('%H:%M')}"
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

        if menu == "set_interval":
            if text == "🔙 назад":
                user_state["menu"] = None
                await send_msg(session, "Головне меню")
                continue
            interval_map = {"1 хв": 1, "2 хв": 2, "3 хв": 3, "5 хв": 5, "10 хв": 10}
            if text in interval_map:
                POLL_INTERVAL = interval_map[text] * 60
                user_state["menu"] = None
                await send_msg(session, f"✅ *Інтервал змінено на {interval_map[text]} хв*")
            else:
                await send_msg(session, "Натисни одну з кнопок", kb=interval_keyboard())
            continue

        if text in ["/start", "▶️ старт"]:
            is_running = True
            await send_msg(session, "▶️ *Сканування запущено!*")

        elif text in ["/stop", "⏹ стоп"]:
            is_running = False
            await send_msg(session, "⏹ *Сканування зупинено.*")

        elif text in ["/stat", "📊 статистика"]:
            await send_stat(session)

        elif text in ["/results", "📈 результати"]:
            await send_results_stat(session)

        elif text in ["/export", "📦 експорт"]:
            await send_export_csv(session)

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
                print(f"[РОЗКЛАД] HTTP {r.status}")
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
                print(f"[ДІАГНОСТИКА] HTTP {r.status}: {data}")
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
            lines.append(f"  - {escape_md(lg)}: {cnt}")
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
        f"Запущено: {stats['started_at']}",
        f"Сканів: {stats['scans_total']}",
        f"Сигналів: {stats['signals_total']}",
        f"Статус: {'▶️ Активний' if is_running else '⏹ Зупинений'}",
        f"Інтервал: {POLL_INTERVAL // 60} хв",
        f"⚽ Футбол: {'✅' if football_enabled else '❌'} (всі ліги API)\n",
        "📡 *Запити сьогодні:*",
        f"  використано: {used}/{limit}",
        f"  залишок: {left} {'⚠️' if left < max(20, int(limit * 0.05)) else ''}",
    ]
    if stats["last_signal"]:
        lines.append(f"\nОстанній сигнал: {escape_md(stats['last_signal'])}")
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
                print(f"  [API⚽] HTTP {r.status}: {raw}")
            if errors:
                print(f"  [API⚽] errors: {errors}")
            return results
    except Exception as e:
        print(f"  [API⚽ ERROR] {e}")
        return []

def _extract_home_away_odds(data, fixture_id=None):
    """
    Витягує коефіцієнти Home та Away з відповіді /odds або /odds/live.
    Підтримує будь-якого букмекера і всі ринки з ODDS_MARKETS.

    ВАЖЛИВО: /odds (pre-match) і /odds/live мають РІЗНІ схеми відповіді:
      - /odds:      entry["bookmakers"] = [ { "bets": [ {name, values}, ... ] }, ... ]
      - /odds/live: entry["odds"]       = [ {name, values}, ... ]  (без рівня "bookmakers"/"bets" взагалі)

    Стара версія цієї функції намагалась читати entry["odds"] так само,
    як bookmakers (.get("bets")), через що live-коефіцієнти НІКОЛИ не парсились
    (bets завжди був порожній список) — це і була причина "home=None away=None"
    для всіх live-матчів.
    """
    seen_bet_names = set()

    def scan_bets(bets):
        for bet in bets:
            bet_name = bet.get("name")
            if bet_name:
                seen_bet_names.add(bet_name)
            if bet_name in ODDS_MARKETS:
                values = bet.get("values", [])
                home_odd = away_odd = None
                for v in values:
                    val = v.get("value", "")
                    odd = v.get("odd")
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
        return None, None

    for entry in data:
        # Схема pre-match: bookmakers -> bets -> values
        for bk in entry.get("bookmakers", []):
            home_odd, away_odd = scan_bets(bk.get("bets", []))
            if home_odd is not None:
                return home_odd, away_odd

        # Схема live: odds — це вже сам список бетів {name, values}
        odds_field = entry.get("odds", [])
        if odds_field and isinstance(odds_field[0], dict) and "values" in odds_field[0]:
            home_odd, away_odd = scan_bets(odds_field)
            if home_odd is not None:
                return home_odd, away_odd
        else:
            # запасний варіант: якщо колись API повернe odds як список букмекерів
            for bk in odds_field:
                home_odd, away_odd = scan_bets(bk.get("bets", []))
                if home_odd is not None:
                    return home_odd, away_odd

    if seen_bet_names:
        fid_label = f"fixture={fixture_id} " if fixture_id is not None else ""
        print(f"    [ODDS MARKET] {fid_label}доступні ринки без Home/Away:")
        for name in sorted(seen_bet_names):
            print(f"      - {name}")

    return None, None

async def fetch_prematch_odds_football(session, fixture_id):
    """
    Повертає dict {"home": float, "away": float, "fav_side": str, "fav_odd": float}
    або None якщо odds недоступні. Кешує результат з TTL.
    """
    now_ts = now_kyiv().timestamp()

    if fixture_id in pre_odds:
        cached = pre_odds[fixture_id]
        if now_ts - cached.get("ts", 0) < PRE_ODDS_TTL:
            return cached
        else:
            del pre_odds[fixture_id]

    try:
        timeout = aiohttp.ClientTimeout(total=10)
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
                print(f"    [ODDS⚽] HTTP {r.status}: {raw}")
                return None
            if errors:
                print(f"    [ODDS⚽] errors: {errors}")
            if not data:
                print(f"    [ODDS⚽] ❌ порожня відповідь")
                return None

            home_odd, away_odd = _extract_home_away_odds(data, fixture_id=fixture_id)
            print(f"    [ODDS⚽] fixture={fixture_id} home={home_odd} away={away_odd}")

            if home_odd is None or away_odd is None:
                print(f"    [ODDS⚽] ❌ fixture={fixture_id} не вдалось витягти Home/Away odds")
                return None

            if home_odd <= away_odd:
                fav_side = "home"
                fav_odd  = home_odd
            else:
                fav_side = "away"
                fav_odd  = away_odd

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
    Кешує результат на LIVE_CACHE_TTL секунд.
    При порожній відповіді позначає матч як кандидат на повтор.
    """
    now_ts = now_kyiv().timestamp()

    cached = live_odds_cache.get(fixture_id)
    if cached and cached.get("fav_side") == fav_side and now_ts - cached.get("ts", 0) < LIVE_CACHE_TTL:
        print(f"    [LIVE ODDS⚽] fixture={fixture_id} з кешу: {cached.get('odd')}")
        return cached.get("odd")

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
                print(f"    [LIVE ODDS⚽] HTTP {r.status}: {raw}")
                live_odds_retry_candidates.add(fixture_id)
                return None
            if errors:
                print(f"    [LIVE ODDS⚽] errors: {errors}")
            if not data:
                print(f"    [LIVE ODDS⚽] ❌ порожня відповідь, кандидат на повтор")
                live_odds_retry_candidates.add(fixture_id)
                return None

            home_odd, away_odd = _extract_home_away_odds(data, fixture_id=fixture_id)
            print(f"    [LIVE ODDS⚽] fixture={fixture_id} home={home_odd} away={away_odd}")

            if home_odd is None or away_odd is None:
                print(f"    [LIVE ODDS⚽] ❌ fixture={fixture_id} не вдалось витягти odds (див. ODDS MARKET вище, якщо є)")
                live_odds_retry_candidates.add(fixture_id)
                return None
    
            live_odds_retry_candidates.discard(fixture_id)
            result_odd = home_odd if fav_side == "home" else away_odd
            live_odds_cache[fixture_id] = {
                "odd": result_odd,
                "fav_side": fav_side,
                "ts": now_ts,
            }
            return result_odd

    except Exception as e:
        print(f"    [LIVE ODDS⚽ ERROR] fixture={fixture_id} {e}")
        live_odds_retry_candidates.add(fixture_id)
    return None

# ── СИЛА СИГНАЛУ ──────────────────────────────────────────────────────────
def strength(rise, strong_rise=60, good_rise=40):
    if rise >= strong_rise:
        return "🔥 СИЛЬНИЙ"
    if rise >= good_rise:
        return "✅ ХОРОШИЙ"
    return "⚠️ СЛАБКИЙ"

# ── СТАТИСТИКА МАТЧУ (для тестового фільтра) ───────────────────────────────
# Окремий запит до /fixtures/statistics. Викликається ТІЛЬКИ для кандидатів,
# що вже пройшли базовий фільтр (хвилина+коефіцієнт) — щоб не витрачати
# квоту на всі live-матчі підряд. З квотою 7500/день це безпечно.
async def fetch_fixture_statistics(session, fixture_id):
    """
    Повертає dict {
        "shots_on_target_home": int, "shots_on_target_away": int,
        "red_cards_home": int, "red_cards_away": int
    } або None, якщо статистика недоступна.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(
            f"https://v3.football.api-sports.io/fixtures/statistics?fixture={fixture_id}",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            raw  = await r.json()
            data = raw.get("response", [])
            print(f"    [STATS⚽] fixture={fixture_id} status={r.status} teams={len(data)}")

            if r.status != 200 or len(data) < 2:
                print(f"    [STATS⚽] ❌ недостатньо даних (teams={len(data)})")
                return None

            def extract(team_block):
                shots_on_target = 0
                red_cards = 0
                for stat in team_block.get("statistics", []):
                    stype = (stat.get("type") or "").strip().lower()
                    val   = stat.get("value")
                    if stype == "shots on goal" and val is not None:
                        shots_on_target = int(val) if str(val).isdigit() else 0
                    if stype == "red cards" and val is not None:
                        red_cards = int(val) if str(val).isdigit() else 0
                return shots_on_target, red_cards

            # API зазвичай повертає [0]=home, [1]=away, але перевіряємо team.id
            # не завжди доступний у відповіді — припускаємо порядок home/away
            home_shots, home_red = extract(data[0])
            away_shots, away_red = extract(data[1])

            return {
                "shots_on_target_home": home_shots,
                "shots_on_target_away": away_shots,
                "red_cards_home": home_red,
                "red_cards_away": away_red,
            }
    except Exception as e:
        print(f"    [STATS⚽ ERROR] fixture={fixture_id} {e}")
    return None

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

    # ВИПРАВЛЕННЯ #4: розділено cnt_odds на два окремих лічильника
    cnt_status   = 0
    cnt_no_odds  = 0   # немає pre-match odds
    cnt_odd_high = 0   # odd занадто високий
    cnt_score    = 0
    cnt_live     = 0
    cnt_signals  = 0

    reasons = {
        "no_prematch_odds":  0,
        "no_live_odds":      0,
        "fav_odd_too_high":  0,
        "fav_not_losing":    0,
        "minute_skipped":    0,
        "low_rise":          0,
        "status_filtered":   0,
    }

    print(f"  [⚽ СКАН] Матчів отримано: {total}")

    for fix in fixtures:
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

            # ВИПРАВЛЕННЯ #5: захист від None у fix["goals"]
            goals   = fix.get("goals") or {}
            score_h = goals.get("home") or 0
            score_a = goals.get("away") or 0

            print(f"  [⚽] fid={fid} | {home} {score_h}:{score_a} {away} | хв={minute} | статус={status_short} | {league_name}")

            if status_short not in LIVE_STATUSES:
                cnt_status += 1
                reasons["status_filtered"] += 1
                print(f"    → fid={fid} пропуск: статус '{status_short}' не в LIVE_STATUSES")
                continue

            if minute > MAX_MINUTE_FOOT:
                cnt_status += 1
                reasons["minute_skipped"] += 1
                print(f"    → fid={fid} пропуск: хвилина {minute} > {MAX_MINUTE_FOOT}")
                continue

            odds_data = await fetch_prematch_odds_football(session, fid)
            if odds_data is None:
                cnt_no_odds += 1
                reasons["no_prematch_odds"] += 1
                print(f"    → fid={fid} пропуск: pre-match odds не знайдено")
                continue

            if odds_data.get("fixture_id") != fid:
                print(f"    → fid={fid} ПОМИЛКА УЗГОДЖЕНОСТІ: odds належать fixture={odds_data.get('fixture_id')}, пропуск")
                cnt_no_odds += 1
                reasons["no_prematch_odds"] += 1
                continue

            fav_side = odds_data["fav_side"]
            fav_odd  = odds_data["fav_odd"]
            fav_team = home if fav_side == "home" else away
            und_team = away if fav_side == "home" else home

            print(f"    → fid={fid} фаворит={fav_team} ({fav_side}) odd={fav_odd}")

            passes_threshold = fav_odd < FAV_THRESHOLD_FOOT
            passes_gap       = odds_data.get("fav_by_gap", False)

            if not passes_threshold and not passes_gap:
                # ВИПРАВЛЕННЯ #4: окремий лічильник для "odd занадто високий"
                cnt_odd_high += 1
                reasons["fav_odd_too_high"] += 1
                print(f"    → fid={fid} пропуск: fav_odd={fav_odd} >= {FAV_THRESHOLD_FOOT} і gap_ratio={round(odds_data.get('gap_ratio',0),2)} < {MIN_ODDS_GAP_RATIO}")
                continue
            elif not passes_threshold and passes_gap:
                print(f"    → fid={fid} фаворит підтверджено через gap_ratio={round(odds_data.get('gap_ratio',0),2)} (odd={fav_odd} >= порогу)")

            if fav_side == "home":
                fav_losing  = score_h < score_a
                fav_drawing = score_h == score_a
                fav_score   = score_h
                und_score   = score_a
            else:
                fav_losing  = score_a < score_h
                fav_drawing = score_h == score_a
                fav_score   = score_a
                und_score   = score_h

            is_00_second_half = (score_h == 0 and score_a == 0 and minute >= 55)

            is_allowed_draw_score = fav_drawing and score_h in DRAW_SCORES_ALLOWED

            not_winning_candidate = (
                is_allowed_draw_score
                and minute >= NOT_WINNING_MIN_MINUTE
                and fav_odd <= NOT_WINNING_FAV_ODD_MAX
            )

            # ВИПРАВЛЕННЯ #6: прибрано зайву умову minute >= NOT_WINNING_MIN_MINUTE
            # з is_no_goals_case (is_00_second_half вже вимагає minute >= 55,
            # а NOT_WINNING_MIN_MINUTE=60 — надлишково)
            strong_fav_cant_win_candidate = (
                fav_odd <= NOT_WINNING_FAV_ODD_MAX
                and is_allowed_draw_score
                and minute >= NOT_WINNING_MIN_MINUTE
            )

            if not fav_losing and not is_00_second_half and not not_winning_candidate and not strong_fav_cant_win_candidate:
                cnt_score += 1
                reasons["fav_not_losing"] += 1
                print(f"    → fid={fid} пропуск: фаворит не програє (рахунок {score_h}:{score_a}, side={fav_side})")
                continue

            key_losing          = f"foot_{fid}_fav_losing"
            key_not_winning     = f"foot_{fid}_not_winning"
            key_no_goals        = f"foot_{fid}_no_goals"
            key_strong_cant_win = f"foot_{fid}_strong_cant_win"
            key_sot_total       = f"foot_{fid}_sot_total_high"

            # ── Варіант A: суворіший фільтр для fav_losing ──────────────────
            # fav_losing спрацьовує лише якщо фаворит реально сильний
            # (odd <= LOSING_FAV_ODD_MAX) і сигнал стався не пізніше
            # LOSING_MAX_SIGNAL_MINUTE. Якщо рахунок підходить (fav_losing=True),
            # але ці умови не виконані — сигнал просто не йде (а не "перетікає"
            # в інший тип), щоб не плутати статистику різних типів сигналів.
            losing_passes_filter = (
                fav_losing
                and fav_odd <= LOSING_FAV_ODD_MAX
                and minute <= LOSING_MAX_SIGNAL_MINUTE
            )
            losing_rejected_by_filter = fav_losing and not losing_passes_filter

            is_losing_case      = losing_passes_filter
            # ВИПРАВЛЕННЯ #6: прибрано зайву перевірку minute >= NOT_WINNING_MIN_MINUTE
            is_no_goals_case    = (not fav_losing) and is_00_second_half
            is_strong_cant_win  = (not fav_losing) and (not is_no_goals_case) and strong_fav_cant_win_candidate
            is_not_winning_case = (not fav_losing) and (not is_no_goals_case) and (not is_strong_cant_win) and not_winning_candidate

            if losing_rejected_by_filter:
                cnt_score += 1
                reasons["fav_losing_filtered_out"] = reasons.get("fav_losing_filtered_out", 0) + 1
                print(f"    → fid={fid} fav_losing відсіяно фільтром: odd={fav_odd} (макс {LOSING_FAV_ODD_MAX}), хв={minute} (макс {LOSING_MAX_SIGNAL_MINUTE})")
                continue

            # ── НОВИЙ ТИП: sot_total_high ────────────────────────────────────
            # Перевіряється серед тих самих кандидатів (фаворит вже
            # програє/не виграє), якщо жоден з "основних" кейсів не активний,
            # АБО навіть якщо основний кейс активний — sot_total_high має
            # окремий ключ антидублювання, тож може зловити цей же матч ще
            # раз пізніше, коли сума ударів виросте. Тут лише перевіряємо,
            # чи варто йти за статистикою — сама перевірка ударів нижче.
            sot_candidate = (
                is_losing_case or is_no_goals_case or is_strong_cant_win or is_not_winning_case
            ) and minute >= SOT_TOTAL_MIN_MINUTE

            if is_losing_case:
                active_key = key_losing
            elif is_no_goals_case:
                active_key = key_no_goals
            elif is_strong_cant_win:
                active_key = key_strong_cant_win
            elif is_not_winning_case:
                active_key = key_not_winning
            else:
                # Сюди потрапляти не повинні, бо вище вже відфільтровано
                # все, що не fav_losing/no_goals/strong_cant_win/not_winning.
                cnt_score += 1
                print(f"    → fid={fid} пропуск: жоден тип сигналу не підійшов")
                continue

            if active_key in notified:
                print(f"    → fid={fid} вже надсилали: {active_key}")
                continue

            live_odd = await fetch_live_odds_football(session, fid, fav_side)
            if live_odd is None:
                cnt_live += 1
                reasons["no_live_odds"] += 1
                print(f"    → fid={fid} пропуск: live odds недоступні (кандидат на повтор наступного скану)")
                continue

            rise = round(((live_odd - fav_odd) / fav_odd) * 100)
            print(f"    → fid={fid} fav_odd={fav_odd} live_odd={live_odd} rise={rise}% (мін={MIN_ODDS_RISE_FOOT}%)")

            if rise < MIN_ODDS_RISE_FOOT:
                cnt_live += 1
                reasons["low_rise"] += 1
                print(f"    → fid={fid} пропуск: ріст {rise}% < {MIN_ODDS_RISE_FOOT}%")
                continue

            notified[active_key] = now_kyiv().timestamp()
            cnt_signals += 1

            # ── ТЕСТОВИЙ ФІЛЬТР (TEST_MODE) ────────────────────────────────
            # Працює ПАРАЛЕЛЬНО зі старою логікою нижче, нічого в ній не
            # змінює. Перевіряється тільки для типу "fav_losing" — це той
            # тип, для якого ми знайшли залежність від хвилини й коефіцієнта.
            # Результат (пройшов чи ні) пишеться в окрему таблицю test_signals.
            if TEST_MODE and is_losing_case:
                test_skip_reason = None
                if fav_odd > TEST_FAV_THRESHOLD_FOOT:
                    test_skip_reason = f"odd {fav_odd} > {TEST_FAV_THRESHOLD_FOOT}"
                elif minute > TEST_MAX_SIGNAL_MINUTE:
                    test_skip_reason = f"хвилина {minute} > {TEST_MAX_SIGNAL_MINUTE}"

                if test_skip_reason:
                    print(f"    [TEST] fid={fid} НЕ пройшов базовий тест-фільтр: {test_skip_reason}")
                    save_test_signal(fid, "fav_losing", league_name, fav_team, und_team, fav_side,
                                      fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                                      passed_filter=0, skip_reason=test_skip_reason)
                else:
                    # Базовий фільтр (коеф+хвилина) пройдено — тепер дивимось
                    # статистику матчу (удари в створ, червоні картки).
                    stats_data = await fetch_fixture_statistics(session, fid)
                    shots_fav = shots_und = None
                    red_und = 0
                    stats_skip_reason = None

                    if stats_data is None:
                        stats_skip_reason = "статистика недоступна"
                    else:
                        if fav_side == "home":
                            shots_fav = stats_data["shots_on_target_home"]
                            shots_und = stats_data["shots_on_target_away"]
                            red_und   = stats_data["red_cards_away"]
                        else:
                            shots_fav = stats_data["shots_on_target_away"]
                            shots_und = stats_data["shots_on_target_home"]
                            red_und   = stats_data["red_cards_home"]

                        shots_diff = shots_fav - shots_und
                        has_red_bonus = TEST_RED_CARD_BONUS and red_und > 0

                        print(f"    [TEST] fid={fid} удари в створ фаворит={shots_fav} андердог={shots_und} (різниця={shots_diff}), червона у андердога={red_und}")

                        if shots_diff < TEST_MIN_SHOTS_ON_TARGET_DIFF and not has_red_bonus:
                            stats_skip_reason = f"удари в створ: різниця {shots_diff} < {TEST_MIN_SHOTS_ON_TARGET_DIFF} і немає червоної картки у андердога"

                    if stats_skip_reason:
                        print(f"    [TEST] fid={fid} НЕ пройшов фільтр статистики: {stats_skip_reason}")
                        save_test_signal(fid, "fav_losing", league_name, fav_team, und_team, fav_side,
                                          fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                                          shots_on_target_fav=shots_fav, shots_on_target_und=shots_und,
                                          red_card_und=red_und, passed_filter=0, skip_reason=stats_skip_reason)
                    else:
                        print(f"    [TEST] ✅ fid={fid} пройшов ПОВНИЙ тест-фільтр (коеф+хвилина+статистика)")
                        save_test_signal(fid, "fav_losing", league_name, fav_team, und_team, fav_side,
                                          fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                                          shots_on_target_fav=shots_fav, shots_on_target_und=shots_und,
                                          red_card_und=red_und, passed_filter=1, skip_reason=None)

                        test_lg_safe   = escape_md(league_name)
                        test_fav_safe  = escape_md(fav_team)
                        test_und_safe  = escape_md(und_team)
                        test_home_safe = escape_md(home)
                        test_away_safe = escape_md(away)
                        shots_line = ""
                        if shots_fav is not None:
                            shots_line = f"Удари в створ: {test_fav_safe} {shots_fav} — {test_und_safe} {shots_und}\n"
                        red_line = "🟥 У андердога червона картка\n" if red_und > 0 else ""

                        await send_test_msg(session,
                            f"🧪 *[TEST] СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                            f"⚽ {test_lg_safe}\n"
                            f"*{test_home_safe}* {score_h}:{score_a} *{test_away_safe}*\n"
                            f"Хвилина: {minute}'\n"
                            f"Фаворит: *{test_fav_safe}* (коеф {fav_odd})\n"
                            f"Коеф зараз: {live_odd} (+{rise}%)\n"
                            f"{shots_line}"
                            f"{red_line}"
                            f"💪 {strength(rise)}"
                        )

            lg_safe   = escape_md(league_name)
            fav_safe  = escape_md(fav_team)
            und_safe  = escape_md(und_team)
            home_safe = escape_md(home)
            away_safe = escape_md(away)

            if is_losing_case:
                add_signal(f"{fav_team} програє {fav_score}:{und_score}")
                save_signal(fid, "fav_losing", league_name, fav_team, und_team, fav_side,
                            fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute)
                await send_msg(session,
                    f"🚨 *СИГНАЛ: ФАВОРИТ ПРОГРАЄ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* {score_h}:{score_a} *{away_safe}*\n"
                    f"Хвилина: {minute}'\n"
                    f"Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"Коеф до матчу: {fav_odd}\n"
                    f"Коеф зараз: {live_odd} (+{rise}%)\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid}: {fav_team} програє {fav_score}:{und_score} +{rise}%")

            elif is_no_goals_case:
                add_signal(f"{fav_team} 0:0 {und_team} 2-й тайм")
                save_signal(fid, "no_goals", league_name, fav_team, und_team, fav_side,
                            fav_odd, live_odd, rise, "0:0", minute)
                await send_msg(session,
                    f"🚨 *СИГНАЛ: ФАВОРИТ БЕЗ ГОЛІВ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* 0:0 *{away_safe}*\n"
                    f"Хвилина: {minute}' (2-й тайм)\n"
                    f"Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"Коеф до матчу: {fav_odd}\n"
                    f"Коеф зараз: {live_odd} (+{rise}%)\n"
                    f"Фаворит без голів у другому таймі\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid} 0:0: {fav_team} vs {und_team} {minute}' +{rise}%")

            elif is_strong_cant_win:
                add_signal(f"{fav_team} не може виграти {score_h}:{score_a}")
                save_signal(fid, "strong_cant_win", league_name, fav_team, und_team, fav_side,
                            fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute)
                await send_msg(session,
                    f"🚨 *СИГНАЛ: СИЛЬНИЙ ФАВОРИТ НЕ МОЖЕ ВИГРАТИ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* {score_h}:{score_a} *{away_safe}*\n"
                    f"Хвилина: {minute}'\n"
                    f"Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"Коеф до матчу: {fav_odd}\n"
                    f"Коеф зараз: {live_odd} (+{rise}%)\n"
                    f"Сильний фаворит (менше {NOT_WINNING_FAV_ODD_MAX}) тримає нічию\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid} сильний фаворит не виграє: {fav_team} {score_h}:{score_a} +{rise}%")

            elif is_not_winning_case:
                add_signal(f"{fav_team} не виграє {score_h}:{score_a}")
                save_signal(fid, "not_winning", league_name, fav_team, und_team, fav_side,
                            fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute)
                await send_msg(session,
                    f"🚨 *СИГНАЛ: ФАВОРИТ НЕ ВИГРАЄ*\n\n"
                    f"⚽ {lg_safe}\n"
                    f"*{home_safe}* {score_h}:{score_a} *{away_safe}*\n"
                    f"Хвилина: {minute}'\n"
                    f"Фаворит: *{fav_safe}* (коеф {fav_odd})\n"
                    f"Коеф до матчу: {fav_odd}\n"
                    f"Коеф зараз: {live_odd} (+{rise}%)\n"
                    f"Фаворит не веде в рахунку\n"
                    f"💪 {strength(rise)}"
                )
                print(f"  ⚽ СИГНАЛ fid={fid} не виграє: {fav_team} {score_h}:{score_a} +{rise}%")

            # ── НОВИЙ ТИП: sot_total_high ────────────────────────────────────
            # Перевіряється ДОДАТКОВО, незалежно від того, який з основних
            # кейсів вище спрацював — тому власний ключ антидублювання
            # (key_sot_total), окреме повідомлення, окремий рядок у БД.
            # Не блокує і не замінює основний сигнал.
            if sot_candidate and key_sot_total not in notified:
                stats_data = await fetch_fixture_statistics(session, fid)
                if stats_data is not None:
                    if fav_side == "home":
                        shots_fav = stats_data["shots_on_target_home"]
                        shots_und = stats_data["shots_on_target_away"]
                    else:
                        shots_fav = stats_data["shots_on_target_away"]
                        shots_und = stats_data["shots_on_target_home"]

                    shots_total = shots_fav + shots_und
                    print(f"    → fid={fid} [SOT] удари в створ: фаворит={shots_fav} андердог={shots_und} сума={shots_total} (мін={SOT_TOTAL_MIN_SHOTS})")

                    if shots_total >= SOT_TOTAL_MIN_SHOTS:
                        if shots_fav > shots_und:
                            edge_side = "fav"
                            edge_line = f"Переважає фаворит: {fav_team} {shots_fav} — {und_team} {shots_und}"
                        elif shots_und > shots_fav:
                            edge_side = "underdog"
                            edge_line = f"Переважає андердог: {und_team} {shots_und} — {fav_team} {shots_fav}"
                        else:
                            edge_side = "equal"
                            edge_line = f"Удари в створ рівні: {shots_fav} — {shots_fav}"

                        notified[key_sot_total] = now_kyiv().timestamp()
                        add_signal(f"Багато ударів {fav_team} vs {und_team} ({shots_total})")
                        save_signal(fid, "sot_total_high", league_name, fav_team, und_team, fav_side,
                                    fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                                    edge_side=edge_side)
                        await send_msg(session,
                            f"🚨 *СИГНАЛ: БАГАТО УДАРІВ У ВОРОТА*\n\n"
                            f"⚽ {lg_safe}\n"
                            f"*{home_safe}* {score_h}:{score_a} *{away_safe}*\n"
                            f"Хвилина: {minute}'\n"
                            f"Сума ударів у створ: {shots_total} (≥{SOT_TOTAL_MIN_SHOTS})\n"
                            f"{edge_line}\n"
                            f"Фаворит: *{fav_safe}* (коеф {fav_odd})"
                        )
                        print(f"  ⚽ СИГНАЛ fid={fid} sot_total_high: сума ударів={shots_total}")

        except Exception as e:
            print(f"  [⚽ ПОМИЛКА МАТЧУ fid={fix.get('fixture',{}).get('id','?')}] {e}")
            continue

    print(
        f"  [⚽ ПІДСУМОК] всього={total} | "
        f"статус={cnt_status} | no_odds={cnt_no_odds} | odd_high={cnt_odd_high} | "
        f"рахунок={cnt_score} | live_odds={cnt_live} | "
        f"сигналів={cnt_signals}"
    )
    print(
        f"  [ПІДСУМОК] "
        f"no_prematch_odds={reasons['no_prematch_odds']} | "
        f"no_live_odds={reasons['no_live_odds']} | "
        f"fav_odd_too_high={reasons['fav_odd_too_high']} | "
        f"fav_not_losing={reasons['fav_not_losing']} | "
        f"low_rise={reasons['low_rise']} | "
        f"minute_skipped={reasons['minute_skipped']} | "
        f"status_filtered={reasons['status_filtered']}"
    )
    if live_odds_retry_candidates:
        print(f"  [RETRY] кандидатів на повторну перевірку live odds: {len(live_odds_retry_candidates)}")

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

    init_db()

    async with aiohttp.ClientSession() as session:
        # ВИПРАВЛЕННЯ #1: прибрано \\! та небезпечні спецсимволи Markdown v1
        await send_msg(session,
            "✅ *FavTracker запущено!*\n\n"
            "⚽ Моніторинг футболу по *всіх лігах* API\n\n"
            f"Скан кожні {POLL_INTERVAL // 60} хвилин\n"
            f"API ліміт: {api_requests['football']['limit']} запитів/день"
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

        async def results_loop():
            while True:
                await asyncio.sleep(RESULTS_CHECK_INTERVAL)
                try:
                    await asyncio.wait_for(check_pending_results(session), timeout=60)
                except asyncio.TimeoutError:
                    print("[РЕЗУЛЬТАТИ TIMEOUT] пропускаємо")
                except Exception as e:
                    print(f"[РЕЗУЛЬТАТИ ERROR] {e}")

        await asyncio.gather(
            command_loop(),
            scan_loop(),
            results_loop(),
        )

if __name__ == "__main__":
    asyncio.run(main())
