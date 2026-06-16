import pandas as pd
import psycopg2
import json
from pathlib import Path

DB = {
    "dbname": "Ostatki",
    "user": "postgres",
    "password": "123",
    "host": "localhost",
    "port": "5432"
}

def get_conn():
    return psycopg2.connect(**DB)

def safe_numeric(val):
    if pd.isna(val) or val in [None, "", "-", "—", "nan"]:
        return None
    try:
        return float(str(val).replace(" ", "").replace(",", "."))
    except:
        return None

def import_nomenclature():
    file = Path(r"C:\Users\prost\OneDrive\Desktop\Center_Krasok\Номенклатура_итог (1).xlsx")
    
    df = pd.read_excel(file, sheet_name="Справочник_корр", header=0)
    print(f"Загружено строк: {len(df)}")

    conn = get_conn()
    cur = conn.cursor()
    
    cur.execute("""
        CREATE TABLE IF NOT EXISTS "Номенклатура" (
            "Артикул" TEXT PRIMARY KEY,
            "Наименование" TEXT,
            "Группа" TEXT,
            "Вид" TEXT,
            "Бренд" TEXT,
            "Ед_изм" TEXT,
            "Объем" NUMERIC,
            "Поставщик" TEXT,
            "Категория" TEXT,
            raw_data JSONB
        );
    """)
    
    inserted = 0
    for _, row in df.iterrows():
        art = str(row.get("Артикул", "")).strip()
        if not art or art.lower() == 'nan' or len(art) < 2:
            continue
            
        raw_data = {str(k): v for k, v in row.to_dict().items() if pd.notna(v)}
        
        cur.execute("""
            INSERT INTO "Номенклатура" 
            ("Артикул", "Наименование", "Группа", "Вид", "Бренд", "Ед_изм", "Объем", "Поставщик", "Категория", raw_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ("Артикул") DO UPDATE SET raw_data = EXCLUDED.raw_data
        """, (
            art,
            row.get("Наименование"),
            row.get("Группа видов номеналтуры"),
            row.get("Вид номенклатуры"),
            row.get("Бренд"),
            row.get("Единица хранения"),
            safe_numeric(row.get("Объем")),
            row.get("Поставщик"),
            row.get("Товарная категория"),
            json.dumps(raw_data)
        ))
        inserted += 1
    
    conn.commit()
    cur.close()
    conn.close()
    print(f"✅ Успешно импортировано {inserted} товаров из Excel")


if __name__ == "__main__":
    print("Запуск импорта номенклатуры...")
    import_nomenclature()