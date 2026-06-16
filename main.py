"""
Дашборд склада/продаж — бэкенд FastAPI поверх PostgreSQL "Ostatki".

Запуск:
    python -m uvicorn app:app --host 0.0.0.0 --port 8000

Открыть локально:  http://localhost:8000
Показать в сети:   http://<IP-вашего-ПК>:8000   (см. README)
"""
from __future__ import annotations
import os
from collections import defaultdict
from datetime import date

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import metrics as M

# ============================ КОНФИГ ============================
    conn = psycopg2.connect(os.getenv("postgresql://postgres:UYCtrlmeYHlEuiVvgGKWERrMBJFZuaQt@postgres.railway.internal:5432/railway"))


# Четыре основных склада. Имена — как в БД (нормализованные).
# Поменяйте/дополните при необходимости; порядок = порядок вкладок.
MAIN_WAREHOUSES = [
    ("алматы_основной_склад", "Алматы основной"),
    ("астана_основной_склад", "Астана основной"),
    ("актобе_основной_склад", "Актобе основной"),
    ("алматы_3pl",            "Алматы 3PL"),
]
WH_IDS = [w[0] for w in MAIN_WAREHOUSES]
WH_NAME = dict(MAIN_WAREHOUSES)
HERE = os.path.dirname(os.path.abspath(__file__))

# ============================ ДОСТУП К БД ============================
def fetch(sql, params=None):
    conn = psycopg2.connect(**DB)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or [])
            return cur.fetchall()
    finally:
        conn.close()


# ============================ КЭШ ВИТРИНЫ ============================
class Store:
    """Считается один раз при старте (или по /api/refresh). Данные грузятся
    помесячно вручную, поэтому держать в памяти — быстро и достаточно."""
    def __init__(self):
        self.ready = False
        self.error = None
        self.months = []            # ['2025-06', ...]
        self.aw_rows = []           # строки артикул×склад (для вкладок складов)
        self.art_rows = []          # агрегаты по артикулу (для сводки)
        self.by_art = {}            # art -> {nom, unit, rows:[aw...], series_total}
        self.kpis = []

    def load(self):
        try:
            self._load()
            self.ready = True
            self.error = None
        except Exception as e:
            self.ready = False
            self.error = str(e)
            raise

    def _load(self):
        # ---- 1. продажи по 4 складам ----
        sales = fetch(
            'SELECT "Артикул" art, "Склад" wh, "Месяц" mon, '
            '"Количество" qty, "Объём_дм3" vol, "Выручка" rev '
            'FROM "Продажи" WHERE "Склад" = ANY(%s)', (WH_IDS,)
        )
        for r in sales:
            r["qty"] = float(r["qty"] or 0)
            r["rev"] = float(r["rev"] or 0)
            r["vol"] = None if r["vol"] is None else float(r["vol"])

        months = sorted({r["mon"] for r in sales})
        self.months = [m.strftime("%Y-%m") for m in months]
        month_nums = [m.month for m in months]
        next_num = M.add_month(months[-1]) if months else 1
        season = M.seasonal_index(sales)

        # первый активный месяц склада (чтобы линия 3PL начиналась честно)
        wh_first = {}
        for r in sales:
            if r["qty"] and r["qty"] > 0:
                wh_first.setdefault(r["wh"], r["mon"])
                if r["mon"] < wh_first[r["wh"]]:
                    wh_first[r["wh"]] = r["mon"]

        # (art,wh) -> month -> {qty,rev,vol,has_vol}
        cell = defaultdict(lambda: defaultdict(lambda: {"qty": 0.0, "rev": 0.0, "vol": 0.0, "has_vol": False}))
        for r in sales:
            c = cell[(r["art"], r["wh"])][r["mon"]]
            c["qty"] += r["qty"]
            c["rev"] += r["rev"]
            if r["vol"] is not None:
                c["vol"] += r["vol"]
                c["has_vol"] = True

        # ---- 2. остатки по 4 складам (схлопываем характеристики) ----
        stock = fetch(
            'SELECT "Склад" wh, "Артикул" art, '
            'MAX("Номенклатура") nom, MAX("Ед_изм") unit, '
            'SUM(COALESCE("В_наличии",0)) nalichie, '
            'SUM(COALESCE("Отгружается",0)) otgr, '
            'SUM(COALESCE("В_резерве",0)) rezerv, '
            'SUM(COALESCE("Доступно", COALESCE("В_наличии",0))) dostupno, '
            'SUM(COALESCE("Приход",0)) prihod, '
            'SUM(COALESCE("Расход",0)) rashod, '
            'SUM(COALESCE("Остаток",0)) ostatok '
            'FROM "Остатки" WHERE "Склад" = ANY(%s) '
            'GROUP BY "Склад","Артикул"', (WH_IDS,)
        )
        stock_idx = {}
        nom_of = {}
        unit_of = {}
        for s in stock:
            for k in ("nalichie", "otgr", "rezerv", "dostupno", "prihod", "rashod", "ostatok"):
                s[k] = float(s[k] or 0)
            stock_idx[(s["art"], s["wh"])] = s
            if s["art"] not in nom_of and s["nom"]:
                nom_of[s["art"]] = s["nom"]
                unit_of[s["art"]] = s["unit"]

        # ---- 3. собираем строки артикул×склад ----
        keys = set(cell.keys()) | set(stock_idx.keys())
        aw = []
        ref_vel = {}  # для ликвидности: эталон скорости артикула
        for (art, wh) in keys:
            mser = cell.get((art, wh), {})
            series = []
            month_nums_present = []
            for m in months:
                if wh in wh_first and m < wh_first[wh]:
                    series.append(None)          # склад ещё не работал
                else:
                    series.append(mser.get(m, {"qty": 0.0})["qty"])
                month_nums_present.append(m.month)
            total = sum(v["qty"] for v in mser.values())
            revenue = sum(v["rev"] for v in mser.values())
            has_vol = any(v.get("has_vol") for v in mser.values())
            volume = sum(v["vol"] for v in mser.values()) if has_vol else None

            st = stock_idx.get((art, wh))
            available = st["dostupno"] if st else 0.0

            met = M.compute_row_metrics(series, month_nums_present, available, season, next_num)
            ref_vel[art] = max(ref_vel.get(art, 0.0), met["vel_day"])

            aw.append({
                "art": art, "wh": wh,
                "nom": (st["nom"] if st else nom_of.get(art, "")),
                "unit": (st["unit"] if st else unit_of.get(art, "")),
                "nalichie": st["nalichie"] if st else 0.0,
                "otgr": st["otgr"] if st else 0.0,
                "rezerv": st["rezerv"] if st else 0.0,
                "dostupno": available,
                "prihod": st["prihod"] if st else 0.0,
                "rashod": st["rashod"] if st else 0.0,
                "ostatok": st["ostatok"] if st else 0.0,
                "sold": total, "volume": volume, "revenue": revenue,
                "series": series,
                **met,
            })

        # ---- 4. ABC/XYZ и hot на уровне АРТИКУЛА (агрегат по 4 складам) ----
        art_agg = defaultdict(lambda: {
            "sold": 0.0, "revenue": 0.0, "available": 0.0,
            "nalichie": 0.0, "otgr": 0.0, "rezerv": 0.0, "dostupno": 0.0,
            "prihod": 0.0, "rashod": 0.0, "ostatok": 0.0,
            "series": [0.0] * len(months), "vel_day": 0.0, "recommended": 0.0, "gap": 0.0,
            "volume": 0.0, "has_vol": False,
        })
        for r in aw:
            a = art_agg[r["art"]]
            a["sold"] += r["sold"]; a["revenue"] += r["revenue"]
            a["dostupno"] += r["dostupno"]; a["available"] += r["dostupno"]
            for k in ("nalichie", "otgr", "rezerv", "prihod", "rashod", "ostatok"):
                a[k] += r[k]
            a["vel_day"] += r["vel_day"]; a["recommended"] += r["recommended"]; a["gap"] += r["gap"]
            if r["volume"] is not None:
                a["volume"] += r["volume"]; a["has_vol"] = True
            for i, v in enumerate(r["series"]):
                a["series"][i] += (v or 0)

        # ABC по выручке артикула
        order = sorted(art_agg.items(), key=lambda kv: kv[1]["revenue"], reverse=True)
        grand = sum(v["revenue"] for _, v in order) or 1.0
        cum = 0.0
        abc_of, xyz_of, hot_of = {}, {}, {}
        # XYZ по агрегированному ряду
        vel_all = sorted(v["vel_day"] for _, v in art_agg.items())
        p80 = vel_all[int(len(vel_all) * 0.8)] if vel_all else 0
        cur_num = months[-1].month if months else 1
        rising = season.get(next_num, 1.0) >= season.get(cur_num, 1.0)
        for art, v in order:
            cum += v["revenue"]
            abc_of[art] = "A" if cum / grand <= 0.8 else ("B" if cum / grand <= 0.95 else "C")
            ser = [x for x in v["series"]]
            cv = (M._std(ser) / M._avg(ser)) if M._avg(ser) > 0 else 1.0
            xyz_of[art] = "X" if cv < 0.25 else ("Y" if cv < 0.5 else "Z")
            v["dos"] = (v["available"] / v["vel_day"]) if v["vel_day"] > 0 else (9999.0 if v["available"] > 0 else 0.0)
            hot_of[art] = (v["vel_day"] >= p80 and rising and abc_of[art] in ("A", "B")
                           and not (v["vel_day"] <= 0.3 and v["available"] > 0 and v["dos"] > 200))

        # проставляем класс/метку в строки складов
        for r in aw:
            r["abc"] = abc_of.get(r["art"], "C")
            r["xyz"] = xyz_of.get(r["art"], "Z")
            is_hot = hot_of.get(r["art"], False) and r["vel_day"] >= p80 * 0.6
            r["status"] = M.classify_status(r["vel_day"], r["dos"], r["dostupno"], is_hot)
            r["liquidity"] = M.liquidity_score(r["vel_day"], ref_vel.get(r["art"], 0.0), r["cv"], r["dos"])

        # строки сводки (по артикулу)
        art_rows = []
        for art, v in art_agg.items():
            cv = (M._std(v["series"]) / M._avg(v["series"])) if M._avg(v["series"]) > 0 else 1.0
            status = M.classify_status(v["vel_day"], v["dos"], v["available"], hot_of.get(art, False))
            art_rows.append({
                "art": art, "wh": "summary",
                "nom": nom_of.get(art, ""), "unit": unit_of.get(art, ""),
                "nalichie": v["nalichie"], "otgr": v["otgr"], "rezerv": v["rezerv"],
                "dostupno": v["dostupno"], "prihod": v["prihod"], "rashod": v["rashod"], "ostatok": v["ostatok"],
                "sold": v["sold"], "volume": (v["volume"] if v["has_vol"] else None),
                "revenue": v["revenue"], "dos": v["dos"],
                "recommended": v["recommended"], "gap": v["gap"],
                "status": status, "abc": abc_of.get(art, "C"), "xyz": xyz_of.get(art, "Z"),
                "liquidity": M.liquidity_score(v["vel_day"], ref_vel.get(art, 0.0), cv, v["dos"]),
                "series": v["series"],
            })

        # индекс по артикулу для детальной страницы
        by_art = defaultdict(lambda: {"rows": []})
        for r in aw:
            by_art[r["art"]]["rows"].append(r)

        self.aw_rows = aw
        self.art_rows = art_rows
        self.by_art = by_art

        # ---- 5. KPI по складам ----
        kpis = []
        for wid in WH_IDS:
            rows = [r for r in aw if r["wh"] == wid]
            rev = sum(r["revenue"] for r in rows)
            # тренд: последние 2 месяца суммарно
            last2 = [0.0, 0.0]
            for r in rows:
                s = r["series"]
                last2[0] += (s[-2] or 0) if len(s) >= 2 else 0
                last2[1] += (s[-1] or 0) if len(s) >= 1 else 0
            trend = ((last2[1] - last2[0]) / last2[0] * 100) if last2[0] > 0 else 0.0
            kpis.append({"id": wid, "name": WH_NAME[wid], "revenue": rev, "trend": trend})
        self.kpis = kpis


STORE = Store()

# ============================ FASTAPI ============================
app = FastAPI(title="Склад · Продажи")


@app.on_event("startup")
def _startup():
    try:
        STORE.load()
        print(f"[OK] витрина загружена: {len(STORE.art_rows)} артикулов, "
              f"{len(STORE.aw_rows)} строк по складам, {len(STORE.months)} мес.")
    except Exception as e:
        print(f"[ОШИБКА подключения к БД] {e}")
        print("Проверьте параметры DB в app.py и что PostgreSQL запущен.")


@app.get("/api/refresh")
def refresh():
    try:
        STORE.load()
        return {"ok": True, "articles": len(STORE.art_rows), "rows": len(STORE.aw_rows)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/kpis")
def kpis():
    if not STORE.ready:
        raise HTTPException(503, STORE.error or "витрина не загружена")
    return {"warehouses": STORE.kpis, "months": STORE.months,
            "wh_list": [{"id": w[0], "name": w[1]} for w in MAIN_WAREHOUSES]}


SORT_KEYS = {"art", "nom", "revenue", "sold", "dostupno", "dos", "recommended", "gap", "liquidity", "ostatok"}


@app.get("/api/table")
def table(view: str = "summary", page: int = 1, size: int = 100,
          sort: str = "revenue", direction: str = "desc", q: str = ""):
    if not STORE.ready:
        raise HTTPException(503, STORE.error or "витрина не загружена")
    if view == "summary":
        data = list(STORE.art_rows)
    elif view in WH_IDS:
        data = [r for r in STORE.aw_rows if r["wh"] == view and r["sold"] > 0]
    else:
        raise HTTPException(404, "неизвестный склад")

    if q:
        ql = q.lower()
        data = [r for r in data if ql in r["art"].lower() or ql in (r["nom"] or "").lower()]

    sort = sort if sort in SORT_KEYS else "revenue"
    rev = (direction == "desc")
    data.sort(key=lambda r: (r.get(sort) if not isinstance(r.get(sort), str) else r.get(sort).lower()),
              reverse=rev)

    total = len(data)
    size = max(10, min(500, size))
    pages = max(1, -(-total // size))
    page = max(1, min(page, pages))
    chunk = data[(page - 1) * size: page * size]

    def slim(r):
        return {k: r[k] for k in (
            "art", "wh", "nom", "unit",
            "nalichie", "otgr", "rezerv", "dostupno", "prihod", "rashod", "ostatok",
            "sold", "revenue", "dos", "recommended", "gap",
            "volume",
            "status", "abc", "xyz", "liquidity")}

    return {"total": total, "page": page, "pages": pages, "size": size,
            "rows": [slim(r) for r in chunk]}


@app.get("/api/product/{art}")
def product(art: str):
    if not STORE.ready:
        raise HTTPException(503, STORE.error or "витрина не загружена")
    node = STORE.by_art.get(art)
    if not node:
        raise HTTPException(404, "артикул не найден")
    rows = node["rows"]
    nom = rows[0]["nom"]; unit = rows[0]["unit"]
    n = len(STORE.months)
    total_series = []
    for i in range(n):
        vals = [r["series"][i] for r in rows if r["series"][i] is not None]
        total_series.append(sum(vals) if vals else None)

    whs = []
    for wid in WH_IDS:
        r = next((x for x in rows if x["wh"] == wid), None)
        if not r:
            continue
        whs.append({
            "id": wid, "name": WH_NAME[wid],
            "color_idx": WH_IDS.index(wid),
            "sales": r["series"], "revenue": r["revenue"], "sold": r["sold"],
            "volume": r["volume"],
            "available": r["dostupno"], "dos": r["dos"],
            "recommended": r["recommended"], "gap": r["gap"], "status": r["status"],
            "stock": {k: r[k] for k in ("nalichie", "otgr", "rezerv", "dostupno", "prihod", "rashod", "ostatok")},
        })
    return {"art": art, "nom": nom, "unit": unit, "months": STORE.months,
            "warehouses": whs, "total_series": total_series}


# ---- статика / фронт ----
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))
