import re
import time
from itertools import combinations
from typing import List, Optional, Dict, Any, Tuple

import requests
from requests.exceptions import ReadTimeout, ConnectionError, HTTPError
import pandas as pd
from datetime import datetime
import json
import difflib

from urllib.parse import quote

BASE = "https://www.pathofexile.com"
REALM = "poe2"
LEAGUE = "Fate of the Vaal"

UA = "Mozilla/5.0 PoE2JewelComboStats/1.0"
HEADERS = {"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ---- настройки ----
#JEWEL_TYPE = "Heart of the Well Diamond"          # "Ruby" / "Sapphire" / и т.д.


ITEM_NAME = "Heart of the Well" #например: "Heart of the Well"
ITEM_TYPE = None        # например: "Diamond" (base type) или None
JEWEL_RARITY = "Unique"       # "magic" или "rare"

INDEXED = "12hours"          # окно
START_MIN_DIV = 1
MAX_DIV = None               # можно None

FETCH_CHUNK = 5
SLEEP_SEARCH = 1.5
SLEEP_FETCH = 2.0
MAX_FETCH_PER_SEARCH = 100   # важно: мы сознательно берём только топ-100

TOP_PAIRS_TO_SHOW = 20
MIN_COUNT_TO_RANK = 1

ITER_STEPS = 5               # сколько итераций
AUTO_RAISE_MIN = False       # если True: поднимем min_div до p75 выбранной пары

PRICE_WINDOWS = [
    #(1, 2),
    #(2, 5),
    (10, 50),
    (50, 300),
]

LADDER_MINS = [1, 2, 5, 10]     # “сбор кандидатов”
FINAL_MIN_DIV = 10              # “финальный поиск самых дорогих” (можно 5, 20, etc)
POOL_TOP_PAIRS = 30             # сколько топ-пар брать в пул
POOL_TOP_MODS = 30              # если хочешь добавлять ещё и одиночные моды

COUNT_MIN_MATCH = 2 if JEWEL_RARITY.lower() in ("magic", "unique") else 3

LINK_SEARCH_SLEEP = 4.0      # 3-6 обычно ок
LINK_FAIL_SLEEP = 15.0       # если словил 429/ошибку — длиннее пауза


# файл stats (скачай и положи рядом)
STATS_PATH = "stats.json"    # <-- путь к /api/trade2/data/stats

# --- нормализация модов ---
num_re = re.compile(r"[-+]?\d+(\.\d+)?")
bracket_choice_re = re.compile(r"\[([^\|\]]+)\|([^\]]+)\]")

def expand_bracket_variants(s: str) -> List[str]:
    """
    Делает варианты текста:
    - как есть
    - заменить [a|b] -> a
    - заменить [a|b] -> b
    Если несколько таких блоков — делаем последовательные замены.
    """
    variants = {s}
    for _ in range(4):  # обычно хватает, чтобы “раскрыть” несколько блоков
        new_vars = set()
        for v in variants:
            m = bracket_choice_re.search(v)
            if not m:
                new_vars.add(v)
                continue
            a, b = m.group(1), m.group(2)
            new_vars.add(bracket_choice_re.sub(a, v, count=1))
            new_vars.add(bracket_choice_re.sub(b, v, count=1))
            new_vars.add(v)
        variants = new_vars
    return sorted(variants)


def normalize_mod_text(s: str) -> str:
    """
    Нормализация и для explicitMods, и для stats.text:
    - числа -> #
    - пробелы
    - lower
    """
    s = s.strip()
    s = num_re.sub("#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.lower()

def safe_name(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s).strip("_").lower()
    return s or "x"

# --- универсальный запрос с backoff ---
def request_json(method: str, url: str, *, json_body: Optional[dict] = None, timeout: int = 60,
                 max_attempts: int = 7) -> dict:
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
                    wait_s = float(ra) if ra else backoff
                except:
                    wait_s = backoff
                print(f"[429] rate-limited, sleep {wait_s:.1f}s (attempt {attempt}/{max_attempts})")
                time.sleep(wait_s + 0.25)
                backoff = min(backoff * 2, 60)
                continue

            r.raise_for_status()
            return r.json()

        except (ReadTimeout, ConnectionError) as e:
            print(f"[NET] {type(e).__name__}, sleep {backoff:.1f}s (attempt {attempt}/{max_attempts})")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        except HTTPError as e:
            body = ""
            try:
                body = e.response.text[:1200]
            except:
                pass
            raise RuntimeError(f"HTTP error: {body}") from e

    raise RuntimeError("Request failed too many times.")

# --- загрузка stats и маппинг текст -> ids ---
def load_explicit_stat_map(stats_path: str) -> Dict[str, List[str]]:
    data = json.load(open(stats_path, "r", encoding="utf-8"))
    explicit_block = next(x for x in data["result"] if x["id"] == "explicit")

    m: Dict[str, List[str]] = {}

    for e in explicit_block["entries"]:
        raw_text = e["text"]

        # сохраняем несколько ключей (как есть + раскрытые [a|b])
        for v in expand_bracket_variants(raw_text):
            key = normalize_mod_text(v)
            m.setdefault(key, []).append(e["id"])

    return m


def resolve_stat_ids(mod_norm: str, stat_map: Dict[str, List[str]]) -> List[str]:
    # пробуем как есть + раскрыть [a|b]
    for v in expand_bracket_variants(mod_norm):
        key = normalize_mod_text(v)
        if key in stat_map:
            return stat_map[key]

    # дальше твой fuzzy (если хочешь)
    key = normalize_mod_text(mod_norm)
    candidates = difflib.get_close_matches(key, stat_map.keys(), n=1, cutoff=0.92)
    if candidates:
        return stat_map[candidates[0]]
    return []


def make_trade_url(search_id: str) -> str:
    # URL как в браузере
    league_q = quote(LEAGUE, safe="")
    return f"{BASE}/trade2/search/{REALM}/{league_q}/{search_id}"

def make_stat_groups_for_exact_combo(combo: str, stat_map: Dict[str, List[str]]) -> List[dict]:
    """
    Превращает строку 'mod1 | mod2 | mod3' в query.stats, где каждый mod = OR по его stat_id,
    а между модами AND (т.е. должны быть все моды).
    """
    parts = [p.strip() for p in combo.split("|") if p.strip()]
    groups: List[dict] = []

    for mod in parts:
        ids = resolve_stat_ids(mod, stat_map)
        if not ids:
            raise RuntimeError(f"Не нашёл stat_id для мода: {mod}")

        groups.append({
            "type": "or",
            "filters": [{"id": sid, "value": {"min": 0}} for sid in ids]
        })

    return groups

def add_trade_links_for_topk(
    topk_df: pd.DataFrame,
    k: int,
    stat_map: Dict[str, List[str]],
    min_div: float,
    max_div: Optional[float],
    top_n_links: int = 25,
) -> pd.DataFrame:
    """
    Делает search под каждое combo (только первые top_n_links строк),
    добавляет столбец trade_url справа.
    """

    # --- настройки троттлинга именно для линков ---
    LINK_SEARCH_SLEEP = 4.0   # пауза перед КАЖДЫМ search (3–6 обычно ок)
    LINK_FAIL_SLEEP = 15.0    # доп. пауза если словили ошибку/429

    if topk_df is None or topk_df.empty:
        return topk_df

    topk_df = topk_df.copy()
    topk_df["trade_url"] = ""

    cache: Dict[str, str] = {}  # combo -> url, чтобы не спамить одинаковыми запросами

    limit = min(len(topk_df), top_n_links)
    print(f"[LINKS] build links for k={k} rows={limit}/{len(topk_df)} min_div={min_div}")

    for i in range(limit):
        combo = str(topk_df.loc[topk_df.index[i], "combo"])

        if combo in cache:
            topk_df.loc[topk_df.index[i], "trade_url"] = cache[combo]
            continue

        # ✅ ВОТ СЮДА: пауза перед каждым поиском
        time.sleep(LINK_SEARCH_SLEEP)

        try:
            stat_groups = make_stat_groups_for_exact_combo(combo, stat_map)
            s = trade_search(min_div, max_div, stat_groups=stat_groups, sort_order="desc")
            url = make_trade_url(s["id"])
            cache[combo] = url
            topk_df.loc[topk_df.index[i], "trade_url"] = url
            print(f"[LINKS] k={k} row={i+1} ok")
        except Exception as e:
            print(f"[LINKS] k={k} row={i+1} fail: {e}")

            # ✅ ВОТ СЮДА: доп. пауза если получили ошибку (особенно 429)
            time.sleep(LINK_FAIL_SLEEP)
            continue

    return topk_df


def trade_search(min_div: float, max_div: Optional[float], stat_groups: Optional[List[dict]], sort_order: str = "desc") -> dict:
    url = f"{BASE}/api/trade2/search/{REALM}/{LEAGUE}"

    price_filter = {"min": min_div, "option": "divine"}
    if max_div is not None:
        price_filter["max"] = max_div

    trade_filters = {"filters": {"price": price_filter, "indexed": {"option": INDEXED}}}

    # ---- ВОТ ТУТ ГЛАВНОЕ ИЗМЕНЕНИЕ: собираем query отдельно ----
    q = {
        "status": {"option": "securable"},
        "stats": stat_groups if stat_groups is not None else [{"type": "and", "filters": []}],
        "filters": {
            "type_filters": {"filters": {
                "category": {"option": "jewel"},
                "rarity": {"option": JEWEL_RARITY},
            }},
            "misc_filters": {"filters": {"identified": {"option": "true"}}},
            "trade_filters": trade_filters,
        },
    }

    # добавляем name/type ТОЛЬКО если они заданы
    if ITEM_NAME:
        q["name"] = ITEM_NAME
    if ITEM_TYPE:
        q["type"] = ITEM_TYPE

    query_obj = {
        "query": q,
        "sort": {"price": sort_order},
    }

    print(f"[SEARCH] min={min_div} max={max_div} sort={sort_order} indexed={INDEXED} "
          f"name={ITEM_NAME} type={ITEM_TYPE} rarity={JEWEL_RARITY} "
          f"stats={'none' if stat_groups is None else stat_groups[0].get('type','?')}")

    time.sleep(SLEEP_SEARCH)
    out = request_json("POST", url, json_body=query_obj, timeout=60)

    print(f"[SEARCH] OK | id={out.get('id')} total={out.get('total')} result_ids={len(out.get('result') or [])}")
    return out



def trade_fetch(ids: List[str], search_id: str) -> dict:
    url = f"{BASE}/api/trade2/fetch/{','.join(ids)}?query={search_id}"
    time.sleep(SLEEP_FETCH)
    return request_json("GET", url, timeout=60)

def parse_price_divine(listing: dict) -> Optional[float]:
    price = (listing or {}).get("price")
    if not price:
        return None
    cur = str(price.get("currency", "")).lower()
    amt = price.get("amount")
    if amt is None:
        return None
    if cur not in ("divine", "divine-orb"):
        return None
    return float(amt)

def fetch_rows(search_id: str, ids: List[str]) -> List[dict]:
    ids = ids[:MAX_FETCH_PER_SEARCH]
    out = []

    print(f"[FETCH] start | search_id={search_id} | ids={len(ids)} | chunk={FETCH_CHUNK}")

    for i in range(0, len(ids), FETCH_CHUNK):
        chunk = ids[i:i+FETCH_CHUNK]
        print(f"[FETCH] chunk {i//FETCH_CHUNK + 1} | taking={len(chunk)}")

        f = trade_fetch(chunk, search_id)

        got = len(f.get("result") or [])
        print(f"[FETCH] chunk {i//FETCH_CHUNK + 1} | api_result={got}")

        for hit in f.get("result", []):
            item = hit.get("item", {}) or {}
            listing = hit.get("listing", {}) or {}

            p = parse_price_divine(listing)
            if p is None:
                continue

            explicit = item.get("explicitMods", []) or []
            mods_norm = sorted({normalize_mod_text(x) for x in explicit if x and x.strip()})

            out.append({
                "price_div": p,
                "mods_norm": mods_norm,
                "explicit_raw": " || ".join(explicit),
            })
    return out

def collect_candidates_from_windows():
    all_rows = []
    seen = set()

    print(f"[CAND] start | windows={PRICE_WINDOWS} | max_fetch_per_search={MAX_FETCH_PER_SEARCH}")

    for (mn, mx) in PRICE_WINDOWS:
        print(f"\n[CAND] window {mn}-{mx} -> trade_search(...)")

        s = trade_search(mn, mx, stat_groups=None, sort_order="desc")

        total = s.get("total")
        search_id = s.get("id")
        ids_all = s.get("result") or []

        print(f"[CAND] window {mn}-{mx} | search_id={search_id} | total={total} | ids_all={len(ids_all)}")

        ids_new = [i for i in ids_all if i not in seen]
        ids = ids_new[:MAX_FETCH_PER_SEARCH]

        print(f"[CAND] window {mn}-{mx} | new_ids={len(ids_new)} | taking={len(ids)} | seen_before={len(seen)}")

        for i in ids:
            seen.add(i)

        if not ids:
            print(f"[CAND] window {mn}-{mx} | no new ids -> skip fetch")
            continue

        print(f"[CAND] window {mn}-{mx} -> fetch_rows(ids={len(ids)})")

        rows = fetch_rows(s["id"], ids)

        print(f"[CAND] window {mn}-{mx} | fetched_rows={len(rows)} | seen_after={len(seen)}")

        for r in rows:
            r["source_min_div"] = mn
            r["source_max_div"] = mx

        all_rows.extend(rows)

    print(f"\n[CAND] done | total_rows={len(all_rows)} | unique_items={len(seen)}")
    return pd.DataFrame(all_rows)




def analyze_top_mods(df_raw: pd.DataFrame, top_n: int = 30) -> pd.DataFrame:
    # df_raw: price_div, mods_norm (list), explicit_raw
    rows = []
    for _, r in df_raw.iterrows():
        mods = r.get("mods_norm")
        if not isinstance(mods, list):
            continue
        p = float(r["price_div"])
        for m in set(mods):
            rows.append({"mod": m, "price_div": p})
    if not rows:
        return pd.DataFrame(columns=["mod","count","median","p75","max","mean"])

    dfm = pd.DataFrame(rows)
    g = dfm.groupby("mod")["price_div"]
    out = g.agg(
        count="count",
        median="median",
        p75=lambda x: x.quantile(0.75),
        max="max",
        mean="mean",
    ).reset_index()
    out = out.sort_values(["median","count"], ascending=[False, False]).head(top_n)
    return out

def analyze_top_k(df_raw: pd.DataFrame, k: int, top_n: int = 30, min_count: int = 2) -> pd.DataFrame:
    df_k = build_k_combos(df_raw, k)
    ranked = rank_k_combos(df_k, min_count=min_count)
    return ranked.head(top_n)



# --- пары/ранжирование ---
def build_k_combos(df_raw: pd.DataFrame, k: int) -> pd.DataFrame:
    rows = []
    for _, r in df_raw.iterrows():
        mods = r.get("mods_norm")
        if not isinstance(mods, list) or len(mods) < k:
            continue
        price = float(r["price_div"])
        for comb in combinations(sorted(mods), k):
            rows.append({"k": k, "combo": " | ".join(comb), "price_div": price})
    return pd.DataFrame(rows)

def rank_k_combos(df_k: pd.DataFrame, min_count: int) -> pd.DataFrame:
    if df_k.empty:
        return pd.DataFrame(columns=["k","combo","count","median","p75","max","mean"])

    g = df_k.groupby(["k", "combo"])["price_div"]
    s = g.agg(
        count="count",
        median="median",
        p75=lambda x: x.quantile(0.75),
        max="max",
        mean="mean",
    ).reset_index()
    s = s[s["count"] >= min_count]
    return s.sort_values(["median", "count"], ascending=[False, False])

from urllib.parse import quote

def combo_to_mod_texts(combo: str) -> List[str]:
    # ВАЖНО: делим только по " | " (с пробелами), чтобы не ломать [a|b]
    return [x.strip() for x in combo.split(" | ") if x.strip()]

def make_and_stat_groups_for_mod_texts(mod_texts: List[str], stat_map: Dict[str, List[str]]) -> List[dict]:
    filters = []
    for mt in mod_texts:
        ids = resolve_stat_ids(mt, stat_map)
        if not ids:
            raise RuntimeError(f"Не нашёл stat_id для мода: {mt}")
        # PoE2 trade2 не принимает type=or -> берём первый id
        filters.append({"id": ids[0], "value": {"min": 0}})
    return [{"type": "and", "filters": filters}]

def trade_link_from_search_id(search_id: str) -> str:
    # trade2 URL (league надо URL-encode)
    return f"{BASE}/trade2/search/{REALM}/{quote(LEAGUE)}/{search_id}"

def add_trade_links_for_topk(df_top: pd.DataFrame, k: int, stat_map: Dict[str, List[str]],
                             min_div: float, max_div: Optional[float], top_n_links: int = 25) -> pd.DataFrame:
    if df_top.empty:
        return df_top

    df = df_top.copy()
    df["trade_url"] = ""

    limit = min(top_n_links, len(df))
    print(f"[LINKS] build links for k={k} rows={limit}/{len(df)} min_div={min_div}")

    for i in range(limit):
        combo = df.iloc[i]["combo"]
        try:
            mod_texts = combo_to_mod_texts(combo)
            if len(mod_texts) != k:
                raise RuntimeError(f"combo split != k (got {len(mod_texts)}): {mod_texts}")

            stat_groups = make_and_stat_groups_for_mod_texts(mod_texts, stat_map)
            s = trade_search(min_div, max_div, stat_groups, sort_order="desc")
            df.at[df.index[i], "trade_url"] = trade_link_from_search_id(s["id"])
            print(f"[LINKS] k={k} row={i+1} ok")

        except Exception as e:
            print(f"[LINKS] k={k} row={i+1} fail: {e}")

    return df


def make_stat_groups_count_from_top_combos(
    ranked_combos: pd.DataFrame,
    stat_map: Dict[str, List[str]],
    top_n_combos: int = 30,
    min_match: int = 2,
) -> Tuple[List[dict], List[str]]:

    combos = ranked_combos.head(top_n_combos)["combo"].tolist()

    pool_mods = set()
    for c in combos:
        for part in c.split("|"):
            pool_mods.add(part.strip())

    pool_stat_ids = []
    missing = []
    for mod in sorted(pool_mods):
        ids = resolve_stat_ids(mod, stat_map)
        if ids:
            pool_stat_ids.extend(ids)
        else:
            missing.append(mod)

    pool_stat_ids = sorted(set(pool_stat_ids))
    if not pool_stat_ids:
        raise RuntimeError("Пул stat_id пустой (ничего не сматчилось со stats.json).")

    stat_groups = [{
        "type": "count",
        "filters": [{"id": sid, "value": {"min": 0}} for sid in pool_stat_ids],
        "value": {"min": int(min_match)},
    }]
    return stat_groups, missing


def build_pool_count_groups(df_candidates: pd.DataFrame, stat_map: Dict[str, List[str]]):
    # magic -> k=2, rare -> k=3 и k=4
    rar = JEWEL_RARITY.lower()

    if rar == "magic":
        k_targets = [2]
    elif rar == "rare":
        k_targets = [3, 4]
    elif rar == "unique":
        k_targets = [2]          # <-- ключевая правка
    else:
        k_targets = [2]


    ranked_all = []
    for k in k_targets:
        topk = analyze_top_k(df_candidates, k=k, top_n=POOL_TOP_PAIRS, min_count=MIN_COUNT_TO_RANK)
        if not topk.empty:
            ranked_all.append(topk)

    if not ranked_all:
        raise RuntimeError("Не получилось собрать топ-комбо (проверь что df_candidates не пустой и min_count).")

    ranked_combos = pd.concat(ranked_all, ignore_index=True)

    # Пул одиночных модов (опционально) — можно оставить
    top_mods = analyze_top_mods(df_candidates, top_n=POOL_TOP_MODS)

    # Счётчик сколько модов нужно (COUNT)
    count_min_match = 2 if JEWEL_RARITY == "magic" else COUNT_MIN_MATCH

    # COUNT-фильтр из топ-комбо (k=2/3/4)
    stat_groups, missing = make_stat_groups_count_from_top_combos(
        ranked_combos,
        stat_map,
        top_n_combos=POOL_TOP_PAIRS,
        min_match=count_min_match,
    )

    return stat_groups, ranked_combos, top_mods, missing

def final_hunt(stat_groups):
    s = trade_search(FINAL_MIN_DIV, MAX_DIV, stat_groups=stat_groups, sort_order="desc")
    ids = (s.get("result") or [])[:MAX_FETCH_PER_SEARCH]
    print(f"[FINAL] min={FINAL_MIN_DIV} total={s.get('total')} ids={len(ids)}")

    if not ids:
        return pd.DataFrame()

    rows = fetch_rows(s["id"], ids)
    return pd.DataFrame(rows)


# --- итератор ---
def run_pipeline():
    stat_map = load_explicit_stat_map(STATS_PATH)

    print(f"[CONFIG] type={ITEM_TYPE} rarity={JEWEL_RARITY} indexed={INDEXED} ladder_mins={LADDER_MINS} final_min={FINAL_MIN_DIV} count_min={COUNT_MIN_MATCH}")

    df_candidates = collect_candidates_from_windows()
    if df_candidates.empty:
        print("[ERROR] No candidates collected")
        return

    print(f"[CANDIDATES] rows={len(df_candidates)} price_min={df_candidates['price_div'].min():.2f} price_max={df_candidates['price_div'].max():.2f}")
    
    # --- DEBUG: сколько модов в каждом item (чтобы понять, возможны ли k=3/4) ---
    lens = df_candidates["mods_norm"].apply(lambda x: len(x) if isinstance(x, list) else 0)
    print("[DEBUG] mods count distribution:", lens.value_counts().head(10).to_dict())
   

    stat_groups, pool_combos, pool_mods, missing = build_pool_count_groups(df_candidates, stat_map)
    print(f"[POOL] combos_rows={len(pool_combos)} mods_rows={len(pool_mods)} missing_mods={len(missing)} count_min={COUNT_MIN_MATCH}")

    df_final = final_hunt(stat_groups)
    if df_final.empty:
        print("[ERROR] Final search returned 0 items. Попробуй COUNT_MIN_MATCH (rare: 3->2) или увеличь POOL_TOP_PAIRS/POOL_TOP_MODS.")
        return

    print(f"[FINAL] rows={len(df_final)} price_min={df_final['price_div'].min():.2f} price_max={df_final['price_div'].max():.2f}")

    # --- финальные топы (ОДИН РАЗ) ---
    final_top_k2 = pd.DataFrame()
    final_top_k3 = pd.DataFrame()
    final_top_k4 = pd.DataFrame()

    if JEWEL_RARITY == "magic":
        final_top_k2 = analyze_top_k(df_final, k=2, top_n=50, min_count=MIN_COUNT_TO_RANK)
    else:
        final_top_k3 = analyze_top_k(df_final, k=3, top_n=50, min_count=MIN_COUNT_TO_RANK)
        final_top_k4 = analyze_top_k(df_final, k=4, top_n=50, min_count=MIN_COUNT_TO_RANK)

    # --- добавим ссылки ТОЛЬКО если df не пустой ---
    if not final_top_k2.empty:
        final_top_k2 = add_trade_links_for_topk(final_top_k2, 2, stat_map, FINAL_MIN_DIV, MAX_DIV, top_n_links=25)
    if not final_top_k3.empty:
        final_top_k3 = add_trade_links_for_topk(final_top_k3, 3, stat_map, FINAL_MIN_DIV, MAX_DIV, top_n_links=25)
    if not final_top_k4.empty:
        final_top_k4 = add_trade_links_for_topk(final_top_k4, 4, stat_map, FINAL_MIN_DIV, MAX_DIV, top_n_links=25)

    top_mods_final = analyze_top_mods(df_final, top_n=50)

    out_path = f"poe2_{safe_name(ITEM_TYPE)}_{JEWEL_RARITY}_{INDEXED}_meta_pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    with pd.ExcelWriter(out_path, engine="openpyxl") as w:
        df_candidates.to_excel(w, index=False, sheet_name="candidates_raw")
        pool_combos.to_excel(w, index=False, sheet_name="pool_top_combos")
        pool_mods.to_excel(w, index=False, sheet_name="pool_top_mods")

        df_final.to_excel(w, index=False, sheet_name="final_raw")
        top_mods_final.to_excel(w, index=False, sheet_name="final_top_mods")

        # ВАЖНО: пишем именно финальные таблицы СО ССЫЛКАМИ
        if not final_top_k2.empty:
            final_top_k2.to_excel(w, index=False, sheet_name="final_top_k2")
        if not final_top_k3.empty:
            final_top_k3.to_excel(w, index=False, sheet_name="final_top_k3")
        if not final_top_k4.empty:
            final_top_k4.to_excel(w, index=False, sheet_name="final_top_k4")

        pd.DataFrame([{
            "ladder_mins": str(LADDER_MINS),
            "final_min_div": FINAL_MIN_DIV,
            "count_min_match": COUNT_MIN_MATCH,
            "missing_mods": len(missing),
            "candidates_rows": len(df_candidates),
            "final_rows": len(df_final),
        }]).to_excel(w, index=False, sheet_name="meta")

    print("SAVED:", out_path)


if __name__ == "__main__":
    run_pipeline()


