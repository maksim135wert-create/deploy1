"""
Расчётный слой витрины: метрики продаж и остатков, рекомендации запаса,
ABC/XYZ, ликвидность, метки (hot/дозаказ/залежь/неликвид).

Логика отделена от веб-слоя. Когда подключишь ML — заменяется только
функция forecast_next_month(), остальное не трогается.
"""
from __future__ import annotations
import statistics
from datetime import date
from collections import defaultdict

# ------- бизнес-правила (раньше были ползунками, теперь фикс. конфиг) -------
LEAD_DAYS = 14          # срок поставки
COVER_MONTHS = 1.0      # целевое покрытие спросом
SERVICE = 95            # уровень сервиса, %
Z_TABLE = {80: 0.84, 85: 1.04, 90: 1.28, 95: 1.65, 97: 1.88, 99: 2.33}


def z_score(svc: int) -> float:
    best = min(Z_TABLE.keys(), key=lambda k: abs(k - svc))
    return Z_TABLE[best]


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    xs = [x for x in xs if x is not None]
    return statistics.pstdev(xs) if len(xs) > 1 else 0.0


def add_month(d: date) -> int:
    """номер следующего календарного месяца (1..12) после даты d"""
    return (d.month % 12) + 1


# --------------------------- сезонность из данных ---------------------------
def seasonal_index(sales_rows):
    """
    Глобальный сезонный индекс по номеру месяца (1..12), посчитанный
    из фактических продаж всех товаров. index[m] = доля месяца / средняя доля.
    Это место можно заменить на сезонность per-SKU или на ML.
    """
    by_month = defaultdict(float)
    for r in sales_rows:
        by_month[r["mon"].month] += (r["qty"] or 0)
    if not by_month:
        return {m: 1.0 for m in range(1, 13)}
    mean = sum(by_month.values()) / len(by_month)
    if mean <= 0:
        return {m: 1.0 for m in range(1, 13)}
    idx = {m: 1.0 for m in range(1, 13)}
    for m, v in by_month.items():
        idx[m] = v / mean
    return idx


# --------------------------- прогноз спроса ---------------------------
def forecast_next_month(monthly_qty, season_idx, last_3_month_nums, next_month_num):
    """
    Деестонализированное среднее за 3 мес × сезонный коэффициент след. месяца.
    monthly_qty       — список количеств по месяцам (без None)
    last_3_month_nums — номера календарных месяцев последних 3 точек
    >>> ML-ХУК: заменить тело на предсказание модели по фичам.
    """
    last3 = [q for q in monthly_qty[-3:] if q is not None]
    if not last3:
        return 0.0
    base_seasons = [season_idx.get(m, 1.0) for m in last_3_month_nums[-3:]]
    seas_avg = _avg(base_seasons) or 1.0
    base = _avg(last3) / seas_avg
    return max(0.0, base * season_idx.get(next_month_num, 1.0))


# --------------------------- метрики по строке ---------------------------
def compute_row_metrics(monthly_qty, month_nums, available, season_idx, next_month_num):
    sold_seq = [q for q in monthly_qty if q is not None]
    last3 = sold_seq[-3:]
    monthly = _avg(last3)
    vel_day = monthly / 30.0
    dos = (available / vel_day) if vel_day > 0 else (9999.0 if available > 0 else 0.0)

    forecast = forecast_next_month(monthly_qty, season_idx, month_nums, next_month_num)

    sigma = _std(sold_seq[-6:])
    safety = z_score(SERVICE) * sigma * (max(COVER_MONTHS, LEAD_DAYS / 30.0) ** 0.5)
    recommended = max(0, round(forecast * COVER_MONTHS + safety))
    gap = recommended - available

    cv = (_std(sold_seq) / _avg(sold_seq)) if _avg(sold_seq) > 0 else 1.0
    xyz = "X" if cv < 0.25 else ("Y" if cv < 0.5 else "Z")

    return {
        "vel_day": vel_day,
        "dos": dos,
        "forecast": round(forecast),
        "recommended": recommended,
        "gap": gap,
        "cv": cv,
        "xyz": xyz,
        "sigma": sigma,
    }


def classify_status(vel_day, dos, available, is_hot):
    if is_hot:
        return "hot"
    if vel_day <= 0.3 and available > 0 and dos > 200:
        return "dead"
    if dos < LEAD_DAYS:
        return "low"
    if dos > 120:
        return "slow"
    return "ok"


def liquidity_score(vel_day, ref_vel_day, cv, dos):
    vel_s = min(1.0, vel_day / ref_vel_day) if ref_vel_day > 0 else 0.0
    turn_s = max(0.0, min(1.0, 60.0 / (dos if dos > 0 else 60.0)))
    stab_s = max(0.0, 1.0 - cv)
    return round((0.45 * vel_s + 0.25 * stab_s + 0.20 * turn_s + 0.10 * 0.5) * 100)