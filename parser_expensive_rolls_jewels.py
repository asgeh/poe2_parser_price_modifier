import re
import time
import json
from typing import Dict, List, Optional, Tuple
from itertools import combinations
import requests
import pandas as pd
from datetime import datetime
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError


BASE = "https://www.pathofexile.com"
REALM = "poe2"
#LEAGUE = "Standard"  # поменяй на свою лигу
LEAGUE = "Fate of the Vaal"  # поменяй на свою лигу

UA = "Mozilla/5.0 (X11; Linux x86_64) PoE2JewelComboStats/1.0"
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}

# ---- настройки ----
MIN_DIV = 10              # твой порог: "дороже N div"
MAX_DIV = 30           # можно поставить верхнюю границу для батчинга, например 10
SLEEP = 2.0              # пауза между запросами (важно)
FETCH_CHUNK = 5         # сколько item ids в одном fetch
MAX_FETCH_PER_RANGE = 3000  # чтобы не умереть по времени/лимитам
MIN_COUNT_TO_RANK = 1    # не ранжировать комбинации с 1-2 лотами (шум)
JEWEL_TYPE = "Emerald"   # или "Sapphire", что угодно
JEWEL_RARITY = "rare"   # "magic" или "rare"
K_LIST = [2, 3, 4]
TOP_N = 100
MIN_COUNT_TO_SHOW = 1  # поставь 1 если хочешь видеть даже редкие
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# --- утилиты ---
num_re = re.compile(r"[-+]?\d+(\.\d+)?")
#brackets_re = re.compile(r"\[[^\]]+\]")  # убираем [Mace|Maces] и т.п.

def normalize_mod(mod: str) -> str:
    s = mod.strip()
    s = s.lstrip("#").strip()                # убрать ведущие ###
    #s = brackets_re.sub("", s)               # убрать [..]
    s = num_re.sub("#", s)                   # числа -> #
    s = re.sub(r"\s+", " ", s).strip()       # пробелы
    return s

def canonical_combo(explicit_mods: List[str]) -> str:
    mods = [normalize_mod(m) for m in explicit_mods if m and m.strip()]
    mods.sort()
    return " | ".join(mods)

def trade_search(min_div: float, max_div: Optional[float], indexed_opt: Optional[str] = None) -> dict:
    url = f"{BASE}/api/trade2/search/{REALM}/{LEAGUE}"

    price_filter = {"min": min_div, "option": "divine"}
    if max_div is not None:
        price_filter["max"] = max_div

    trade_f = {"filters": {"price": price_filter}}
    if indexed_opt is not None:
        trade_f["filters"]["indexed"] = {"option": indexed_opt}

    query_obj = {
        "query": {
            "status": {"option": "securable"},
            "type": JEWEL_TYPE,
            "stats": [{"type": "and", "filters": []}],
            "filters": {
                "type_filters": {"filters": {"category": {"option": "jewel"}, "rarity": {"option": JEWEL_RARITY}}},
                "misc_filters": {"filters": {"identified": {"option": "true"}}},
                "trade_filters": trade_f,
            },
        },
        "sort": {"price": "desc"},
    }

    return request_json("POST", url, json_body=query_obj, timeout=60)


def request_json(method: str, url: str, *, json_body=None, timeout=60, max_attempts=8):
    backoff = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            if method.upper() == "POST":
                r = SESSION.post(url, json=json_body, timeout=timeout)
            else:
                r = SESSION.get(url, timeout=timeout)

            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                try:
                    wait_s = float(ra) if ra is not None else backoff
                except:
                    wait_s = backoff
                print(f"[429] rate-limited, sleep {wait_s:.1f}s (attempt {attempt}/{max_attempts})")
                time.sleep(wait_s)
                backoff = min(backoff * 2, 60)
                continue

            r.raise_for_status()
            return r.json()

        except (ReadTimeout, ConnectionError) as e:
            # временные сетевые проблемы
            print(f"[NET] {type(e).__name__}: retry in {backoff:.1f}s (attempt {attempt}/{max_attempts})")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        except HTTPError as e:
            # другие 4xx/5xx кроме 429 — это обычно ошибка запроса
            body = ""
            try:
                body = e.response.text[:500]
            except:
                pass
            raise RuntimeError(f"HTTP error {e.response.status_code if e.response else '?'}: {body}") from e

    raise RuntimeError("Request failed too many times (rate limit / network).")



def trade_fetch(ids: List[str], search_id: str) -> dict:
    url = f"{BASE}/api/trade2/fetch/{','.join(ids)}?query={search_id}"
    return request_json("GET", url, timeout=60)



def parse_price_divine(listing: dict) -> Optional[float]:
    # тут считаем только лоты, выставленные В DIVINE (чтобы не считать курс)
    price = (listing or {}).get("price")
    if not price:
        return None
    cur = str(price.get("currency", "")).lower()
    amt = price.get("amount")
    if amt is None:
        return None
    if cur not in ("divine", "divine-orb"):
        return None
    try:
        return float(amt)
    except:
        return None

def fetch_rows_for_search(search_id: str, ids: List[str]) -> List[dict]:
    out_rows: List[dict] = []
    ids = ids[:MAX_FETCH_PER_RANGE]

    for i in range(0, len(ids), FETCH_CHUNK):
        chunk = ids[i:i + FETCH_CHUNK]
        time.sleep(SLEEP)
        f = trade_fetch(chunk, search_id)

        for hit in f.get("result", []):
            item = hit.get("item", {}) or {}
            listing = hit.get("listing", {}) or {}

            price_div = parse_price_divine(listing)
            if price_div is None:
                continue

            explicit = item.get("explicitMods", []) or []
            mods_norm = sorted({normalize_mod(m) for m in explicit if m and m.strip()})
            item_id = hit.get("id") or (item.get("id") if isinstance(item, dict) else None)

            out_rows.append({
                "item_id": item_id,
                "price_div": price_div,
                "explicit_count": len(explicit),
                "mods_norm": mods_norm,
                "explicit_raw": " || ".join(explicit),
            })

    return out_rows


INDEXED_LAST_12H = "12hours"

def collect_last_12h(min_div: float, max_div: Optional[float]) -> List[dict]:
    time.sleep(SLEEP)
    s = trade_search(min_div, max_div, indexed_opt=INDEXED_LAST_12H)

    print("TOTAL:", s.get("total"), "IDS:", len(s.get("result", [])))

    search_id = s.get("id")
    ids = s.get("result", []) or []
    if not search_id or not ids:
        return []

    return fetch_rows_for_search(search_id, ids)


# INDEXED_WINDOWS = ["1hour","3hours","12hours","1day","3days","1week","2weeks","1month","2months"]

# def collect_all_by_indexed(min_div, max_div):
#     all_rows = []
#     seen_ids = set()

#     for w in INDEXED_WINDOWS:
#         time.sleep(SLEEP)
#         s = trade_search(min_div, max_div, indexed_opt=w)
#         print("INDEXED", w, "TOTAL", s.get("total"), "IDS", len(s.get("result", [])))

#         search_id = s.get("id")
#         ids = s.get("result", []) or []
#         if not search_id or not ids:
#             continue

#         # дедуп по id (на всякий случай)
#         ids = [x for x in ids if x not in seen_ids]
#         for x in ids:
#             seen_ids.add(x)

#         # fetch как у тебя
#         all_rows.extend(fetch_rows_for_search(search_id, ids))

#     return all_rows


def build_k_combos_df(df_raw: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, r in df_raw.iterrows():
        mods = r["mods_norm"]
        if not isinstance(mods, list):
            continue

        mods = sorted(mods)
        price = r["price_div"]
        item_id = r.get("item_id")
        explicit_raw = r.get("explicit_raw")

        for k in K_LIST:
            if len(mods) < k:
                continue
            for comb in combinations(mods, k):
                key = " | ".join(comb)
                rows.append({
                    "k": k,
                    "combo": key,
                    "price_div": price,
                    "item_id": item_id,
                    "explicit_raw": explicit_raw,
                })

    return pd.DataFrame(rows)


def summarize_k(df_k: pd.DataFrame) -> pd.DataFrame:
    g = df_k.groupby(["k", "combo"])["price_div"]
    summary = g.agg(
        count="count",
        median="median",
        p75=lambda x: x.quantile(0.75),
        mean="mean",
    ).reset_index()
    return summary

def add_roll_examples(top_df: pd.DataFrame, df_k: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    # берем top-n по цене внутри каждой (k, combo)
    df_sorted = df_k.sort_values("price_div", ascending=False)

    topn = df_sorted.groupby(["k", "combo"], as_index=False).head(n).copy()
    topn["example_line"] = topn.apply(
        lambda r: f'{r["price_div"]:.0f}div: {r["explicit_raw"]}', axis=1
    )

    ex = (
        topn.groupby(["k", "combo"])["example_line"]
        .agg(lambda s: " ||| ".join(s.tolist()))
        .reset_index()
        .rename(columns={"example_line": "roll_examples_top"})
    )

    return top_df.merge(ex, on=["k", "combo"], how="left")



def main():
    rows = collect_last_12h(MIN_DIV, MAX_DIV)
    df = pd.DataFrame(rows)
    if df.empty or "price_div" not in df.columns:
        print("No data")
        return

    print("PARSED ROWS:", len(df))
    print("PRICE MIN/MAX:", df["price_div"].min(), df["price_div"].max())

    df_k = build_k_combos_df(df)
    summary_k = summarize_k(df_k)

    # Топы по медиане для каждого k
    tops = {}
    for k in K_LIST:
        s = summary_k[summary_k["k"] == k].copy()
        s = s[s["count"] >= MIN_COUNT_TO_SHOW]
        top = s.sort_values(["median", "count"], ascending=[False, False]).head(TOP_N)

        # <-- добавили примеры роллов
        top = add_roll_examples(top, df_k[df_k["k"] == k], n=3)

        tops[k] = top

    safe_type = re.sub(r'[^A-Za-z0-9_-]+', '_', JEWEL_TYPE).strip('_').lower()
    out_path = f"poe2_{safe_type}_{JEWEL_RARITY}_combo_stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"

    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="raw")
        df_k.to_excel(w, index=False, sheet_name="combos_k_raw")
        summary_k.to_excel(w, index=False, sheet_name="summary_k_all")

        for k in K_LIST:
            tops[k].to_excel(w, index=False, sheet_name=f"top_k{k}_median")

if __name__ == "__main__":
    main()
