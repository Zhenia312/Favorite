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
        #
        # ── goals_after_signal: хвилини всіх голів (обидві команди), що
        #   відбулись ПІСЛЯ minute_at_signal — заповнюється при завершенні
        #   матчу в check_pending_results(), формат "23' home; 41' away".
        # ── double_chance_odd: кф дабл шансу на фаворита (1X якщо фаворит
        #   вдома, X2 якщо у гостях), забирається в момент спрацювання
        #   сигналу разом з live_odd (з фолбеком на останню відому лінію).
        for col, col_type in [
            ("edge_side", "TEXT"),
            ("edge_result", "TEXT"),
            ("goals_after_signal", "TEXT"),
            ("double_chance_odd", "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {col} {col_type}")
            except sqlite3.OperationalError:
                pass  # колонка вже існує
        # ── ТАБЛИЦЯ-ФЛАГ ОДНОРАЗОВИХ МІГРАЦІЙ ──────────────────────────────
        # Використовується, наприклад, для одноразового перерахунку result
        # по всій історії signals (див. recalc_results_v2 нижче) — щоб при
        # наступних рестартах/деплоях на Railway цей перерахунок більше не
        # запускався повторно.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS migrations_applied (
                name TEXT PRIMARY KEY,
                applied_time TEXT
            )
        """)
        conn.commit()
        conn.close()
        print(f"[DB] Базу статистики ініціалізовано: {DB_PATH}")
    except Exception as e:
        print(f"[DB ERROR] init_db: {e}")

def _migration_applied(conn, name):
    row = conn.execute(
        "SELECT 1 FROM migrations_applied WHERE name=?", (name,)
    ).fetchone()
    return row is not None

def _mark_migration_applied(conn, name):
    conn.execute(
        "INSERT OR REPLACE INTO migrations_applied (name, applied_time) VALUES (?, ?)",
        (name, now_kyiv().strftime("%Y-%m-%d %H:%M:%S"))
    )

MIGRATION_RECALC_RESULT_V2 = "recalc_result_unified_success_logic_v2"

def recalc_results_v2():
    """
    ОДНОРАЗОВИЙ перерахунок колонки result для всіх status='completed'
    записів у signals за новою єдиною логікою _signal_is_success (замість
    старої формули, яка залежала від signal_type — зокрема для no_goals
    рахувала успіхом суму голів ≤1).

    Виконується автоматично при старті бота (main()), але РЕАЛЬНО щось
    перераховує лише один раз: після завершення позначає міграцію як
    застосовану в таблиці migrations_applied, і при всіх наступних
    рестартах/деплоях на Railway одразу виходить, нічого не чіпаючи.
    """
    try:
        conn = sqlite3.connect(DB_PATH)

        if _migration_applied(conn, MIGRATION_RECALC_RESULT_V2):
            conn.close()
            print(f"[MIGRATION] '{MIGRATION_RECALC_RESULT_V2}' вже застосована раніше — пропускаємо")
            return

        rows = conn.execute("""
            SELECT id, fav_side, score_at_signal, final_score
            FROM signals
            WHERE status='completed'
        """).fetchall()

        updated = 0
        skipped = 0
        for row_id, fav_side, score_at_signal, final_score in rows:
            fav_score_sig, und_score_sig = _parse_score_at_signal(score_at_signal, fav_side)
            if fav_score_sig is None or not final_score:
                skipped += 1
                continue
            try:
                fh_str, fa_str = final_score.split(":")
                fh, fa = int(fh_str), int(fa_str)
            except (ValueError, AttributeError):
                skipped += 1
                continue
            fav_score_final = fh if fav_side == "home" else fa
            und_score_final = fa if fav_side == "home" else fh

            success = _signal_is_success(fav_score_sig, und_score_sig, fav_score_final, und_score_final)
            new_result = "win" if success else "loss"
            conn.execute("UPDATE signals SET result=? WHERE id=?", (new_result, row_id))
            updated += 1

        _mark_migration_applied(conn, MIGRATION_RECALC_RESULT_V2)
        conn.commit()
        conn.close()
        print(f"[MIGRATION] '{MIGRATION_RECALC_RESULT_V2}' застосована: оновлено={updated}, пропущено={skipped} (з {len(rows)} завершених)")
    except Exception as e:
        print(f"[MIGRATION ERROR] recalc_results_v2: {e}")

def save_signal(fixture_id, signal_type, league, fav_team, und_team, fav_side,
                 pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                 edge_side=None, double_chance_odd=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            INSERT INTO signals (
                fixture_id, signal_type, league, fav_team, und_team, fav_side,
                pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
                signal_time, status, edge_side, double_chance_odd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            fixture_id, signal_type, league, fav_team, und_team, fav_side,
            pre_odd, live_odd, rise_pct, score_at_signal, minute_at_signal,
            now_kyiv().strftime("%Y-%m-%d %H:%M:%S"), edge_side, double_chance_odd,
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] save_signal fid={fixture_id}: {e}")

def _parse_score_at_signal(score_at_signal, fav_side):
    """
    Парсить score_at_signal (завжди зберігається як 'home:away') у пару
    (fav_score, und_score) на момент сигналу, з урахуванням fav_side.
    Повертає (None, None), якщо розпарсити не вдалось.
    """
    try:
        h_str, a_str = score_at_signal.split(":")
        h, a = int(h_str), int(a_str)
    except (ValueError, AttributeError, TypeError):
        return None, None
    if fav_side == "home":
        return h, a
    return a, h

def _signal_is_success(fav_score_signal, und_score_signal, fav_score_final, und_score_final):
    """
    ЄДИНИЙ критерій успіху сигналу — для ВСІХ типів сигналів без винятку,
    включно з 'no_goals'. Залежить від того, який був рахунок (з точки зору
    фаворита) У МОМЕНТ СИГНАЛУ, а не від типу сигналу:

    - Нічия на сигнал (fav_score_signal == und_score_signal) → успіх ТІЛЬКИ
      якщо фаворит здобув ЧИСТУ перемогу у фіналі.
    - Фаворит програє на сигнал (fav_score_signal < und_score_signal) → успіх,
      якщо у фіналі фаворит НЕ програв (перемога або нічия).
    - Крайній випадок: фаворит вже вів на момент сигналу
      (fav_score_signal > und_score_signal) — у проді такого бути не повинно
      (сигнали генеруються лише коли фаворит не веде), але про всяк випадок
      трактуємо як "нічию": потрібна чиста перемога у фіналі. Це лише щоб
      код не падав, а не змістовна бізнес-логіка.

    Увага: для 'no_goals' це МІНЯЄ сенс сигналу порівняно зі старою формулою
    (сума голів ≤1, яку ми повністю прибрали). Рахунок 0:0 на сигнал — це
    нічия, тож фінальні 0:0 (нічия у фіналі) тепер рахуються як ПРОГРАШ, а не
    успіх, бо це не чиста перемога фаворита.
    """
    if fav_score_signal == und_score_signal:
        return fav_score_final > und_score_final
    elif fav_score_signal < und_score_signal:
        return fav_score_final >= und_score_final
    else:
        # фаворит вже вів на сигнал — крайній випадок, трактуємо як нічию
        return fav_score_final > und_score_final

WEEKDAY_LABELS_UA = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
MONTH_LABELS_UA = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]

def _parse_signal_time(signal_time):
    """Парсить signal_time ('%Y-%m-%d %H:%M:%S') у datetime. None при помилці."""
    if not signal_time:
        return None
    try:
        return datetime.strptime(signal_time, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None

def signal_time_parts(signal_time):
    """
    Обчислює (weekday_label, day, month) з signal_time "на льоту" — нічого
    з цього НЕ зберігається окремо в БД, тільки рахується при потребі
    (для /results та для CSV-експорту).
    Повертає (None, None, None), якщо signal_time відсутній/некоректний.
    """
    dt = _parse_signal_time(signal_time)
    if dt is None:
        return None, None, None
    weekday_label = WEEKDAY_LABELS_UA[dt.weekday()]
    day = dt.strftime("%Y-%m-%d")
    month = f"{dt.year}-{dt.month:02d} ({MONTH_LABELS_UA[dt.month - 1]})"
    return weekday_label, day, month

async def fetch_fixture_goal_events(session, fixture_id, home_team_id):
    """
    Повертає список голів матчу [{"minute": int, "team": "home"/"away"}, ...]
    відсортований за хвилиною (з урахуванням доданого часу), або [] якщо
    події недоступні чи сталась помилка запиту.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(
            f"https://v3.football.api-sports.io/fixtures/events?fixture={fixture_id}",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            raw  = await r.json()
            data = raw.get("response", [])
            if r.status != 200:
                print(f"    [GOALS⚽] fixture={fixture_id} HTTP {r.status}")
                return []

            goals = []
            for ev in data:
                if (ev.get("type") or "").strip().lower() != "goal":
                    continue
                time_block = ev.get("time") or {}
                minute = time_block.get("elapsed")
                if minute is None:
                    continue
                minute += time_block.get("extra") or 0
                team_id = (ev.get("team") or {}).get("id")
                side = "home" if team_id == home_team_id else "away"
                goals.append({"minute": minute, "team": side})

            goals.sort(key=lambda g: g["minute"])
            return goals
    except Exception as e:
        print(f"    [GOALS⚽ ERROR] fixture={fixture_id} {e}")
        return []

def _format_goals_after_signal(goals, minute_at_signal):
    """
    Формує рядок для CSV/БД типу "23' home; 41' away" з голів, забитих
    ПІСЛЯ minute_at_signal (обидві команди). Повертає "" якщо таких немає
    або minute_at_signal невідома.
    """
    if minute_at_signal is None:
        return ""
    after = [g for g in goals if g["minute"] > minute_at_signal]
    if not after:
        return ""
    return "; ".join(f"{g['minute']}' {g['team']}" for g in after)

async def check_pending_results(session):
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT id, fixture_id, signal_type, fav_side, edge_side, score_at_signal, minute_at_signal "
            "FROM signals WHERE status='pending'"
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
        for row_id, fixture_id, signal_type, fav_side, edge_side, score_at_signal, minute_at_signal in rows:
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
            fav_score_sig, und_score_sig = _parse_score_at_signal(score_at_signal, fav_side)
            if fav_score_sig is None:
                # не змогли розпарсити рахунок на момент сигналу — пропускаємо
                # (лишається status='pending', спробуємо наступного разу)
                print(f"  [РЕЗУЛЬТАТИ] fid={fixture_id} пропуск: не вдалось розпарсити score_at_signal={score_at_signal!r}")
                continue
            success     = _signal_is_success(fav_score_sig, und_score_sig, fav_score, und_score)
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

            # ── goals_after_signal: хвилини всіх голів (обидві команди), що
            # сталися ПІСЛЯ minute_at_signal — беремо з /fixtures/events.
            # Якщо запит не вдався, лишаємо порожнім рядком (не блокує
            # завершення сигналу).
            home_team_id = (fix.get("teams", {}).get("home", {}) or {}).get("id")
            goal_events = await fetch_fixture_goal_events(session, fixture_id, home_team_id)
            goals_after_signal = _format_goals_after_signal(goal_events, minute_at_signal)

            conn.execute(
                "UPDATE signals SET status='completed', final_score=?, result=?, checked_time=?, "
                "edge_result=?, goals_after_signal=? WHERE id=?",
                (final_score, result, now_kyiv().strftime("%Y-%m-%d %H:%M:%S"), edge_result,
                 goals_after_signal, row_id)
            )
            updated += 1
            print(f"  [РЕЗУЛЬТАТИ] fid={fixture_id} {signal_type} → {final_score} → {result}" + (f" (edge={edge_side}→{edge_result})" if edge_side else "") + (f" | голи після сигналу: {goals_after_signal}" if goals_after_signal else ""))

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
            "SELECT signal_type, league, result, signal_time FROM signals WHERE status='completed'"
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
    wins  = sum(1 for _, _, res, _ in rows if res == "win")
    pct   = round(wins / total * 100, 1) if total else 0

    by_type = {}
    for stype, _, res, _ in rows:
        d = by_type.setdefault(stype, {"win": 0, "total": 0})
        d["total"] += 1
        if res == "win":
            d["win"] += 1

    by_league = {}
    for _, league, res, _ in rows:
        key = league or "Невідома"
        d = by_league.setdefault(key, {"win": 0, "total": 0})
        d["total"] += 1
        if res == "win":
            d["win"] += 1

    # ── По днях тижня (Пн-Нд) та по місяцях — рахуємо "на льоту" з
    # signal_time, нічого з цього окремо в БД не зберігається.
    by_weekday = {}
    by_month = {}
    for _, _, res, sig_time in rows:
        wd_label, _, month_label = signal_time_parts(sig_time)
        if wd_label is not None:
            d = by_weekday.setdefault(wd_label, {"win": 0, "total": 0})
            d["total"] += 1
            if res == "win":
                d["win"] += 1
        if month_label is not None:
            d = by_month.setdefault(month_label, {"win": 0, "total": 0})
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

    lines.append("\n*По днях тижня:*")
    weekday_order = {label: i for i, label in enumerate(WEEKDAY_LABELS_UA)}
    for wd_label, d in sorted(by_weekday.items(), key=lambda x: weekday_order.get(x[0], 99)):
        w_pct = round(d["win"] / d["total"] * 100, 1) if d["total"] else 0
        lines.append(f"  - {wd_label}: {d['win']}/{d['total']} ({w_pct}% не програв)")

    lines.append("\n*По місяцях:*")
    for month_label, d in sorted(by_month.items()):
        m_pct = round(d["win"] / d["total"] * 100, 1) if d["total"] else 0
        lines.append(f"  - {escape_md(month_label)}: {d['win']}/{d['total']} ({m_pct}% не програв)")

    await send_msg(session, "\n".join(lines))

# ── Фільтр для розбивки CSV-експорту на 2 файли ────────────────────────────
EXPORT_FILTER_PRE_ODD_MAX  = 1.4
EXPORT_FILTER_RISE_PCT_MAX = 80

def _export_passes_filter(pre_odd, rise_pct):
    """
    pre_odd <=1.4 і rise_pct <=80 → потрапляє у "filtered"-файл.
    Якщо pre_odd або rise_pct відсутні (None) — вважаємо, що фільтр НЕ
    пройдено (запис іде в "rest"), бо перевірити умову неможливо.
    """
    if pre_odd is None or rise_pct is None:
        return False
    return pre_odd <= EXPORT_FILTER_PRE_ODD_MAX and rise_pct <= EXPORT_FILTER_RISE_PCT_MAX

async def _send_csv_document(session, csv_bytes, filename, caption):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument"
    try:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(TG_CHAT_ID))
        form.add_field("caption", caption)
        form.add_field("document", csv_bytes, filename=filename, content_type="text/csv")
        async with session.post(url, data=form) as r:
            resp = await r.json()
            if not resp.get("ok"):
                await send_msg(session, f"❌ Не вдалось відправити файл {escape_md(filename)}: {escape_md(str(resp))}")
    except Exception as e:
        await send_msg(session, f"❌ Помилка відправки файлу {escape_md(filename)}: {escape_md(str(e))}")

async def send_export_csv(session):
    """
    Вивантажує всі записи з таблиці signals у ДВА CSV-файли і відправляє їх
    у Telegram окремими документами:
      - signals_filtered_YYYYMMDD_HHMM.csv — pre_odd<=1.4 і rise_pct<=80
      - signals_rest_YYYYMMDD_HHMM.csv     — всі інші записи
    Обидва файли містять всі поточні колонки + weekday/day/month, які
    рахуються "на льоту" з signal_time (в БД окремо не зберігаються).
    """
    import csv
    import io

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.execute("""
            SELECT id, fixture_id, signal_type, league, fav_team, und_team, fav_side,
                   pre_odd, live_odd, rise_pct, double_chance_odd, score_at_signal, minute_at_signal,
                   signal_time, status, final_score, result, checked_time, goals_after_signal
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

    idx_pre_odd  = columns.index("pre_odd")
    idx_rise_pct = columns.index("rise_pct")
    idx_sig_time = columns.index("signal_time")

    out_columns = columns + ["weekday", "day", "month"]

    filtered_rows = []
    rest_rows = []
    for row in rows:
        wd_label, day, month = signal_time_parts(row[idx_sig_time])
        full_row = list(row) + [wd_label, day, month]
        if _export_passes_filter(row[idx_pre_odd], row[idx_rise_pct]):
            filtered_rows.append(full_row)
        else:
            rest_rows.append(full_row)

    def _build_csv_bytes(data_rows):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(out_columns)
        writer.writerows(data_rows)
        return buf.getvalue().encode("utf-8-sig")  # BOM, щоб Excel коректно показав кирилицю

    ts = now_kyiv().strftime("%Y%m%d_%H%M")

    filtered_filename = f"signals_filtered_{ts}.csv"
    rest_filename      = f"signals_rest_{ts}.csv"

    await _send_csv_document(
        session,
        _build_csv_bytes(filtered_rows),
        filtered_filename,
        f"📦 Filtered (pre_odd≤{EXPORT_FILTER_PRE_ODD_MAX}, rise_pct≤{EXPORT_FILTER_RISE_PCT_MAX}): {len(filtered_rows)} записів",
    )
    await _send_csv_document(
        session,
        _build_csv_bytes(rest_rows),
        rest_filename,
        f"📦 Rest (решта записів): {len(rest_rows)} записів",
    )

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
# (n=12, +114% ROI на наявних даних). Це той самий фільтр, що раніше
# перевірявся окремо від основного продакшн-сигналу.
LOSING_FAV_ODD_MAX       = 1.65
LOSING_MAX_SIGNAL_MINUTE = 40

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
SOT_TOTAL_FAV_ODD_MAX   = 2.00  # власний, м'якший поріг кф фаворита саме для цього типу
                                 # (вищий, ніж NOT_WINNING_FAV_ODD_MAX=1.50, бо тут не
                                 # потрібен настільки явний фаворит — рахунок не головне)

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

# ── ДАБЛ ШАНС НА ФАВОРИТА ─────────────────────────────────────────────────
# Ринок "Double Chance": значення "Home/Draw" (1X) і "Draw/Away" (X2).
# Якщо фаворит вдома — беремо 1X, якщо у гостях — X2. Забирається в
# момент спрацювання сигналу, одразу після live_odd. last_double_chance_odd
# зберігає ОСТАННЄ успішно отримане значення по кожному fixture_id БЕЗ TTL —
# якщо на момент сигналу лінія вже недоступна (матч добігає кінця),
# використовуємо цей фолбек замість None.
DOUBLE_CHANCE_MARKETS = {"Double Chance"}
last_double_chance_odd = {}

def _extract_double_chance_odd(data, fav_side, fixture_id=None):
    """
    Витягує кф дабл шансу на фаворита (1X якщо fav_side='home', X2 якщо 'away')
    з відповіді /odds або /odds/live. Підтримує обидві схеми відповіді
    (bookmakers->bets і live-шему odds як список бетів), так само як
    _extract_home_away_odds.
    """
    wanted_values = {"1X", "Home/Draw"} if fav_side == "home" else {"X2", "Draw/Away"}

    def scan_bets(bets):
        for bet in bets:
            if bet.get("name") not in DOUBLE_CHANCE_MARKETS:
                continue
            for v in bet.get("values", []):
                if v.get("value") in wanted_values:
                    try:
                        return float(v.get("odd"))
                    except (TypeError, ValueError):
                        continue
        return None

    for entry in data:
        for bk in entry.get("bookmakers", []):
            odd = scan_bets(bk.get("bets", []))
            if odd is not None:
                return odd
        odds_field = entry.get("odds", [])
        if odds_field and isinstance(odds_field[0], dict) and "values" in odds_field[0]:
            odd = scan_bets(odds_field)
            if odd is not None:
                return odd
        else:
            for bk in odds_field:
                odd = scan_bets(bk.get("bets", []))
                if odd is not None:
                    return odd
    return None

async def fetch_double_chance_odd_football(session, fixture_id, fav_side):
    """
    Повертає кф дабл шансу на фаворита (1X/X2) в момент виклику, або
    останнє відоме значення (фолбек), якщо лінія зараз недоступна.
    Пробує спершу live-odds (актуальніше на момент сигналу), потім
    pre-match odds як додаткове джерело.
    """
    odd = None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with session.get(
            f"https://v3.football.api-sports.io/odds/live?fixture={fixture_id}",
            headers={"x-apisports-key": API_KEY},
            timeout=timeout
        ) as r:
            track_request("football")
            raw  = await r.json()
            data = raw.get("response", [])
            if r.status == 200 and data:
                odd = _extract_double_chance_odd(data, fav_side, fixture_id=fixture_id)
    except Exception as e:
        print(f"    [DC ODDS⚽ ERROR] live fixture={fixture_id} {e}")

    if odd is None:
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
                if r.status == 200 and data:
                    odd = _extract_double_chance_odd(data, fav_side, fixture_id=fixture_id)
        except Exception as e:
            print(f"    [DC ODDS⚽ ERROR] prematch fixture={fixture_id} {e}")

    if odd is not None:
        last_double_chance_odd[fixture_id] = odd
        print(f"    [DC ODDS⚽] fixture={fixture_id} дабл шанс на фаворита ({fav_side})={odd}")
        return odd

    fallback = last_double_chance_odd.get(fixture_id)
    if fallback is not None:
        print(f"    [DC ODDS⚽] fixture={fixture_id} лінія недоступна, фолбек на останню відому: {fallback}")
    else:
        print(f"    [DC ODDS⚽] fixture={fixture_id} дабл шанс недоступний, фолбеку теж немає")
    return fallback

# ── СИЛА СИГНАЛУ ──────────────────────────────────────────────────────────
def strength(rise, strong_rise=60, good_rise=40):
    if rise >= strong_rise:
        return "🔥 СИЛЬНИЙ"
    if rise >= good_rise:
        return "✅ ХОРОШИЙ"
    return "⚠️ СЛАБКИЙ"

# ── СТАТИСТИКА МАТЧУ (для sot_total_high) ──────────────────────────────────
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

            # ── sot_total_high: окремий, м'якший прохідний прапор ───────────
            # Власний поріг кф фаворита (SOT_TOTAL_FAV_ODD_MAX=2.00) — вищий,
            # ніж у not_winning/strong_cant_win (1.50). Дозволяє нічийному
            # рахунку (fav_drawing) дійти до перевірки ударів навіть тоді,
            # коли фаворит не настільки явний, щоб пройти інші типи сигналу.
            # На результат (fav_losing/win) цей прапор НЕ впливає — лише
            # відкриває шлях до перевірки sot_total_high нижче.
            sot_draw_candidate = (
                fav_drawing
                and score_h in DRAW_SCORES_ALLOWED
                and minute >= SOT_TOTAL_MIN_MINUTE
                and fav_odd <= SOT_TOTAL_FAV_ODD_MAX
            )

            if (not fav_losing and not is_00_second_half and not not_winning_candidate
                    and not strong_fav_cant_win_candidate and not sot_draw_candidate):
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
                is_losing_case or is_no_goals_case or is_strong_cant_win
                or is_not_winning_case or sot_draw_candidate
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

            # ── Кф дабл шансу на фаворита ────────────────────────────────────
            # Забирається одразу в момент спрацювання сигналу, разом з
            # live_odd вище. Якщо лінія зараз недоступна — функція сама
            # підставляє останнє відоме значення (фолбек), або None,
            # якщо навіть фолбеку немає.
            double_chance_odd = await fetch_double_chance_odd_football(session, fid, fav_side)

            lg_safe   = escape_md(league_name)
            fav_safe  = escape_md(fav_team)
            und_safe  = escape_md(und_team)
            home_safe = escape_md(home)
            away_safe = escape_md(away)

            if is_losing_case:
                add_signal(f"{fav_team} програє {fav_score}:{und_score}")
                save_signal(fid, "fav_losing", league_name, fav_team, und_team, fav_side,
                            fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                            double_chance_odd=double_chance_odd)
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
                            fav_odd, live_odd, rise, "0:0", minute,
                            double_chance_odd=double_chance_odd)
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
                            fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                            double_chance_odd=double_chance_odd)
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
                            fav_odd, live_odd, rise, f"{score_h}:{score_a}", minute,
                            double_chance_odd=double_chance_odd)
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
                                    edge_side=edge_side, double_chance_odd=double_chance_odd)
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
    recalc_results_v2()

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
