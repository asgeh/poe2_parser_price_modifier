from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import quote

import difflib

from .config import PipelineConfig
from .utils import expand_bracket_variants, extract_mod_lines, normalize_mod_text, parse_price

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    import pandas as pd
else:
    pd = None  # type: ignore[assignment]

def _pd():
    import pandas as pd  # local import to keep utility functions testable without pandas

    return pd


def sort_by_count_desc(df: "pd.DataFrame") -> "pd.DataFrame":
    if "count" not in df.columns or df.empty:
        return df
    return df.sort_values("count", ascending=False, kind="mergesort")


def analyze_top_sellers(df_raw: pd.DataFrame) -> pd.DataFrame:
    pd = _pd()
    if "seller_account" not in df_raw.columns:
        return pd.DataFrame(columns=["seller_account", "count", "max"])
    sellers = df_raw.dropna(subset=["seller_account"])
    if sellers.empty:
        return pd.DataFrame(columns=["seller_account", "count", "max"])
    grouped = sellers.groupby("seller_account")["price_div"]
    ranked = grouped.agg(count="count", max="max").reset_index()
    return sort_by_count_desc(ranked)


def analyze_mods_by_pass(df_raw: pd.DataFrame) -> pd.DataFrame:
    pd = _pd()
    if "pass_index" not in df_raw.columns:
        return pd.DataFrame(columns=["pass_index", "mod", "count", "max", "share_pass"])

    rows: list[dict[str, Any]] = []
    for _, row in df_raw.iterrows():
        mods = row.get("mods_norm")
        if not isinstance(mods, list):
            continue
        pass_index = row.get("pass_index")
        for mod in set(mods):
            rows.append({"pass_index": pass_index, "mod": mod, "price_div": float(row["price_div"])})

    if not rows:
        return pd.DataFrame(columns=["pass_index", "mod", "count", "max", "share_pass"])

    mods_df = pd.DataFrame(rows)
    pass_totals = df_raw.groupby("pass_index").size()
    grouped = mods_df.groupby(["pass_index", "mod"])["price_div"]
    ranked = grouped.agg(count="count", max="max").reset_index()
    ranked["share_pass"] = ranked.apply(
        lambda r: r["count"] / pass_totals.get(r["pass_index"], 1),
        axis=1,
    )
    return ranked.sort_values(["pass_index", "count"], ascending=[True, False], kind="mergesort")


def analyze_top_sellers_by_mod(df_raw: pd.DataFrame) -> pd.DataFrame:
    pd = _pd()
    if "seller_account" not in df_raw.columns:
        return pd.DataFrame(columns=["mod", "seller_account", "count", "share_mod"])
    rows: list[dict[str, Any]] = []
    for _, row in df_raw.iterrows():
        mods = row.get("mods_norm")
        seller = row.get("seller_account")
        if not seller or not isinstance(mods, list):
            continue
        for mod in set(mods):
            rows.append({"mod": mod, "seller_account": seller})
    if not rows:
        return pd.DataFrame(columns=["mod", "seller_account", "count", "share_mod"])
    df = pd.DataFrame(rows)
    grouped = df.groupby(["mod", "seller_account"]).size().reset_index(name="count")
    totals = grouped.groupby("mod")["count"].transform("sum")
    grouped["share_mod"] = grouped["count"] / totals
    return grouped.sort_values(["mod", "count"], ascending=[True, False], kind="mergesort")


@dataclass(slots=True)
class PipelineArtifacts:
    output_file: Path
    candidates_count: int
    final_count: int
    final_trade_url: Optional[str] = None


class TradeClient:
    def __init__(self, config: PipelineConfig):
        self.config = config
        import requests

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.user_agent,
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        if config.poesessid:
            host = config.base_url.replace("https://", "").replace("http://", "").strip("/")
            self.session.cookies.set("POESESSID", config.poesessid, domain=host, path="/")

    def request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[dict[str, Any]] = None,
        timeout: int = 60,
        max_attempts: int = 7,
    ) -> dict[str, Any]:
        backoff = 2.0
        for attempt in range(1, max_attempts + 1):
            try:
                if method.upper() == "POST":
                    response = self.session.post(url, json=json_body, timeout=timeout)
                else:
                    response = self.session.get(url, timeout=timeout)

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait_seconds = float(retry_after) if retry_after else backoff
                    LOGGER.warning("429 rate-limited, sleeping %.1fs", wait_seconds)
                    time.sleep(wait_seconds + 0.25)
                    backoff = min(backoff * 2, 60)
                    continue

                response.raise_for_status()
                return response.json()
            except Exception as exc:
                name = exc.__class__.__name__
                if name in {"ReadTimeout", "ConnectionError"}:
                    LOGGER.warning("Network timeout, retrying in %.1fs", backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                if name == "HTTPError":
                    body = exc.response.text[:1200] if getattr(exc, "response", None) is not None else ""
                    raise RuntimeError(f"HTTP error from trade API: {body}") from exc
                raise

        raise RuntimeError("Request failed too many times")

    def trade_search(
        self,
        min_div: float,
        max_div: Optional[float],
        stat_groups: Optional[list[dict[str, Any]]],
    ) -> dict[str, Any]:
        url = f"{self.config.base_url}/api/trade2/search/{self.config.realm}/{self.config.league}"

        price_filter: dict[str, Any] = {"min": min_div, "option": self.config.price_currency}
        if max_div is not None:
            price_filter["max"] = max_div

        query: dict[str, Any] = {
            "status": {"option": "securable"},
            "stats": stat_groups if stat_groups else [{"type": "and", "filters": []}],
            "filters": {
                "type_filters": {
                    "filters": {
                        "category": {"option": "jewel"},
                        "rarity": {"option": self.config.jewel_rarity},
                    }
                },
                "misc_filters": {"filters": {"identified": {"option": "true"}}},
                "trade_filters": {"filters": {"price": price_filter, "indexed": {"option": self.config.indexed}}},
            },
        }

        if self.config.item_name:
            query["name"] = self.config.item_name
        if self.config.item_type:
            query["type"] = self.config.item_type

        body = {"query": query, "sort": {"price": "desc"}}
        time.sleep(self.config.sleep_search)
        return self.request_json("POST", url, json_body=body)

    def trade_fetch(self, ids: list[str], search_id: str) -> dict[str, Any]:
        url = f"{self.config.base_url}/api/trade2/fetch/{','.join(ids)}?query={search_id}"
        time.sleep(self.config.sleep_fetch)
        return self.request_json("GET", url)


def load_stat_map(stats_path: Path, groups: list[str]) -> dict[str, list[str]]:
    data = json.loads(stats_path.read_text(encoding="utf-8"))
    mapping: dict[str, list[str]] = {}
    for group in groups:
        block = next((x for x in data["result"] if x["id"] == group), None)
        if not block:
            LOGGER.warning("stats group '%s' not found", group)
            continue
        for entry in block.get("entries", []):
            key = normalize_mod_text(entry.get("text", ""))
            if key:
                mapping.setdefault(key, []).append(entry["id"])
    return mapping


def normalize_cache_item(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    mods_norm = row.get("mods_norm")
    if not isinstance(mods_norm, list):
        return None
    return {
        "item_id": row.get("item_id"),
        "price_div": row.get("price_div"),
        "mods_norm": mods_norm,
        "explicit_raw": row.get("explicit_raw"),
        "seller_account": row.get("seller_account"),
        "indexed": row.get("indexed"),
        "source_min_div": row.get("source_min_div"),
        "source_max_div": row.get("source_max_div"),
    }


def load_cache(path: Path) -> tuple[set[str], list[dict[str, Any]]]:
    if not path.exists():
        return set(), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read cache file %s: %s", path, exc)
        return set(), []

    if isinstance(raw, list):
        # Legacy format: list of ids
        return {str(item) for item in raw if item}, []

    if not isinstance(raw, dict):
        LOGGER.warning("Unexpected cache format in %s (expected dict)", path)
        return set(), []

    ids_raw = raw.get("ids", [])
    items_raw = raw.get("items", [])
    ids = {str(item) for item in ids_raw if item}
    items: list[dict[str, Any]] = []
    if isinstance(items_raw, list):
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            normalized = normalize_cache_item(item)
            if not normalized:
                continue
            row = dict(normalized)
            row["pass_index"] = 0
            items.append(row)
            if row.get("item_id"):
                ids.add(str(row["item_id"]))
    return ids, items


def save_cache(path: Path, ids: set[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_items = []
    for row in rows:
        normalized = normalize_cache_item(row)
        if not normalized:
            continue
        payload_items.append(normalized)
    payload = {"ids": sorted(ids), "items": payload_items}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_legacy_items(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        LOGGER.warning("Failed to read legacy items cache file %s: %s", path, exc)
        return []
    if not isinstance(raw, list):
        LOGGER.warning("Unexpected legacy items cache format in %s (expected list)", path)
        return []
    rows: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        normalized = normalize_cache_item(item)
        if not normalized:
            continue
        row = dict(normalized)
        row["pass_index"] = 0
        rows.append(row)
    return rows


def delete_file_safely(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
            LOGGER.info("Deleted legacy cache file %s", path)
    except Exception as exc:
        LOGGER.warning("Failed to delete legacy cache file %s: %s", path, exc)


def load_cache_with_legacy(path: Path) -> tuple[set[str], list[dict[str, Any]], bool]:
    migrated = False
    ids: set[str] = set()
    items: list[dict[str, Any]] = []

    if path.exists():
        ids, items = load_cache(path)
    else:
        legacy_ids = path.with_name("cache_ids.json")
        if legacy_ids.exists():
            ids, items = load_cache(legacy_ids)
            migrated = True

    legacy_items = path.with_name("cache_items.json")
    legacy_rows = load_legacy_items(legacy_items)
    if legacy_rows:
        for row in legacy_rows:
            item_id = row.get("item_id")
            if item_id:
                ids.add(str(item_id))
        items.extend(legacy_rows)
        migrated = True

    if migrated:
        save_cache(path, ids, items)

    # Always try to delete legacy cache_items.json to keep a single file
    delete_file_safely(path.with_name("cache_items.json"))
    # If we migrated from legacy ids file, remove it as well
    if path.name != "cache_ids.json":
        delete_file_safely(path.with_name("cache_ids.json"))

    return ids, items, migrated


def clear_cache(path: Path) -> None:
    delete_file_safely(path)
    delete_file_safely(path.with_name("cache_ids.json"))
    delete_file_safely(path.with_name("cache_items.json"))


def resolve_stat_ids(mod_norm: str, stat_map: dict[str, list[str]]) -> list[str]:
    for variant in expand_bracket_variants(mod_norm):
        key = normalize_mod_text(variant)
        if key in stat_map:
            return stat_map[key]
    key = normalize_mod_text(mod_norm)
    closest = difflib.get_close_matches(key, stat_map.keys(), n=1, cutoff=0.92)
    return stat_map[closest[0]] if closest else []


def build_k_combos(df_raw: pd.DataFrame, k: int) -> pd.DataFrame:
    pd = _pd()
    rows: list[dict[str, Any]] = []
    for _, row in df_raw.iterrows():
        mods = row.get("mods_norm")
        if not isinstance(mods, list) or len(mods) < k:
            continue
        for combo in combinations(sorted(mods), k):
            rows.append({"k": k, "combo": " | ".join(combo), "price_div": float(row["price_div"])})
    return pd.DataFrame(rows)


def rank_k_combos(df_k: pd.DataFrame, min_count: int) -> pd.DataFrame:
    pd = _pd()
    if df_k.empty:
        return pd.DataFrame(columns=["k", "combo", "count", "max"])
    grouped = df_k.groupby(["k", "combo"])["price_div"]
    ranked = grouped.agg(count="count", max="max").reset_index()
    ranked = ranked[ranked["count"] >= min_count]
    return ranked.sort_values(["count", "max"], ascending=[False, False])


def analyze_top_mods(df_raw: pd.DataFrame, top_n: int = 30) -> pd.DataFrame:
    pd = _pd()
    total_items = len(df_raw)
    rows: list[dict[str, Any]] = []
    for _, row in df_raw.iterrows():
        mods = row.get("mods_norm")
        if not isinstance(mods, list):
            continue
        for mod in set(mods):
            rows.append({"mod": mod, "price_div": float(row["price_div"])})
    if not rows:
        return pd.DataFrame(columns=["mod", "count", "max", "share_total"])

    mods_df = pd.DataFrame(rows)
    grouped = mods_df.groupby("mod")["price_div"]
    ranked = grouped.agg(count="count", max="max").reset_index()
    ranked["share_total"] = ranked["count"] / max(total_items, 1)
    return ranked.sort_values(["count", "max"], ascending=[False, False]).head(top_n)


def analyze_top_k(df_raw: pd.DataFrame, k: int, top_n: int, min_count: int) -> pd.DataFrame:
    return rank_k_combos(build_k_combos(df_raw, k), min_count=min_count).head(top_n)


def build_count_stat_group(
    mods: list[str], config: PipelineConfig, stat_map: dict[str, list[str]]
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    ordered_mods: list[str] = []
    seen = set()
    for mod in mods:
        clean = mod.strip()
        if clean and clean not in seen:
            seen.add(clean)
            ordered_mods.append(clean)

    stat_to_mod: dict[str, str] = {}
    missing: list[str] = []
    for mod in ordered_mods:
        ids = resolve_stat_ids(mod, stat_map)
        if ids:
            stat_id = ids[0]
            if stat_id not in stat_to_mod:
                stat_to_mod[stat_id] = mod
        else:
            missing.append(mod)
        if len(stat_to_mod) >= config.max_pool_stats:
            break

    stat_ids = list(stat_to_mod.keys())
    if not stat_ids:
        raise RuntimeError("stat pool is empty")

    used_mods = [{"mod": mod, "stat_id": stat_id} for stat_id, mod in stat_to_mod.items()]

    return (
        [
            {
                "type": "count",
                "filters": [{"id": stat_id, "value": {"min": 0}} for stat_id in stat_ids],
                "value": {"min": int(config.count_min_match)},
            }
        ],
        missing,
        used_mods,
    )


def collect_candidates(client: TradeClient, config: PipelineConfig, known_ids: Optional[set[str]] = None) -> tuple[pd.DataFrame, set[str]]:
    pd = _pd()
    all_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set(known_ids or set())
    max_passes = max(1, int(config.candidate_passes))
    global_pass_index = 0
    LOGGER.info("collect start (windows=%s passes_per_window=%s)", len(config.price_windows), max_passes)

    for window_index, (mn, mx) in enumerate(config.price_windows, start=1):
        LOGGER.info("collect window %s start min=%s max=%s", window_index, mn, mx)
        for window_pass in range(1, max_passes + 1):
            global_pass_index += 1
            window_new_rows = 0
            window_new_ids = 0

            LOGGER.info(
                "collect window %s pass %s (global_pass=%s) start",
                window_index,
                window_pass,
                global_pass_index,
            )
            search = client.trade_search(mn, mx, stat_groups=None)
            LOGGER.info(
                "search window min=%s max=%s total=%s result=%s",
                mn,
                mx,
                search.get("total"),
                len(search.get("result") or []),
            )
            ids = [item_id for item_id in (search.get("result") or []) if item_id not in seen_ids][: config.max_fetch_per_search]
            window_new_ids += len(ids)
            for item_id in ids:
                seen_ids.add(item_id)
            LOGGER.info(
                "collect window %s pass %s ids: new_ids=%s unique_ids=%s",
                window_index,
                window_pass,
                len(ids),
                len(seen_ids),
            )

            for i in range(0, len(ids), config.fetch_chunk):
                chunk = ids[i : i + config.fetch_chunk]
                fetched = client.trade_fetch(chunk, search["id"])
                for hit in fetched.get("result", []):
                    listing = hit.get("listing", {}) or {}
                    account = (listing.get("account") or {}).get("name")

                    price = parse_price(listing, config.price_currency)
                    if price is None:
                        continue

                    item = hit.get("item", {}) or {}
                    mod_lines = extract_mod_lines(item)
                    all_rows.append(
                        {
                            "item_id": hit.get("id"),
                            "price_div": price,
                            "mods_norm": sorted({normalize_mod_text(mod) for mod in mod_lines if mod.strip()}),
                            "explicit_raw": " || ".join(mod_lines),
                            "seller_account": account,
                            "indexed": listing.get("indexed"),
                            "pass_index": global_pass_index,
                            "source_min_div": mn,
                            "source_max_div": mx,
                        }
                    )
                    window_new_rows += 1
            LOGGER.info(
                "collect window %s pass %s done: rows=%s new_ids=%s unique_ids=%s",
                window_index,
                window_pass,
                window_new_rows,
                window_new_ids,
                len(seen_ids),
            )

            if config.stop_when_no_new and window_new_ids == 0:
                LOGGER.info(
                    "collect window %s stop: no new ids on pass %s",
                    window_index,
                    window_pass,
                )
                break

    return pd.DataFrame(all_rows), seen_ids


def make_trade_url(base: str, realm: str, league: str, search_id: str) -> str:
    league_encoded = quote(league, safe="")
    return f"{base}/trade2/search/{realm}/{league_encoded}/{search_id}"


def run_pipeline(config: PipelineConfig) -> PipelineArtifacts:
    pd = _pd()
    stat_map = load_stat_map(config.stats_path, groups=["explicit", "implicit", "desecrated"])
    client = TradeClient(config)

    cache_before = 0
    cache_after = 0
    cache_items_before = 0
    cache_items_after = 0
    cache_used = False
    cache_used_fallback = False
    cache_migrated = False
    cache_ids: set[str] = set()
    cache_items: list[dict[str, Any]] = []
    if config.use_cache and config.refresh_cache:
        clear_cache(config.cache_path)
        LOGGER.info("Cache cleared: %s", config.cache_path)

    if config.use_cache:
        cache_ids, cache_items, cache_migrated = load_cache_with_legacy(config.cache_path)
        cache_before = len(cache_ids)
        cache_items_before = len(cache_items)
        cache_used = True
        if cache_ids and not cache_items:
            # Id-only cache cannot power analytics; rebuild items by re-fetching.
            LOGGER.warning("Cache contains ids but no items; rebuilding item cache from fresh fetches.")
            cache_ids = set()

    candidates, cache_ids = collect_candidates(client, config, known_ids=cache_ids)
    cache_after = len(cache_ids)

    if config.use_cache:
        if not candidates.empty:
            items_by_id: dict[str, dict[str, Any]] = {}
            extra_items: list[dict[str, Any]] = []
            for row in cache_items:
                item_id = row.get("item_id")
                if item_id:
                    items_by_id[str(item_id)] = row
                else:
                    extra_items.append(row)
            for row in candidates.to_dict(orient="records"):
                item_id = row.get("item_id")
                if item_id:
                    items_by_id[str(item_id)] = row
                else:
                    extra_items.append(row)
            cache_items = list(items_by_id.values()) + extra_items
        cache_items_after = len(cache_items)
        save_cache(config.cache_path, cache_ids, cache_items)

    analysis_candidates = pd.DataFrame(cache_items) if (config.use_cache and cache_items) else candidates
    cache_used_fallback = bool(config.use_cache and cache_items and candidates.empty)

    if analysis_candidates.empty:
        LOGGER.warning(
            "No candidates collected (cache empty=%s). Writing empty report.",
            not bool(cache_items),
        )
        pd = _pd()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = config.output_dir / f"{config.output_name()}_meta_pipeline_{timestamp}.xlsx"
        empty_candidates = pd.DataFrame(
            columns=[
                "item_id",
                "price_div",
                "mods_norm",
                "explicit_raw",
                "seller_account",
                "indexed",
                "pass_index",
                "source_min_div",
                "source_max_div",
            ]
        )
        empty_ranked = pd.DataFrame(columns=["k", "combo", "count", "max"])
        empty_mods = pd.DataFrame(columns=["mod", "count", "max", "share_total"])
        empty_sellers = pd.DataFrame(columns=["seller_account", "count", "max"])
        empty_mods_by_pass = pd.DataFrame(columns=["pass_index", "mod", "count", "max", "share_pass"])
        empty_sellers_by_mod = pd.DataFrame(columns=["mod", "seller_account", "count", "share_mod"])
        empty_final = pd.DataFrame(columns=["price_div", "mods_norm", "explicit_raw"])

        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            empty_candidates.to_excel(writer, index=False, sheet_name="candidates_raw")
            empty_ranked.to_excel(writer, index=False, sheet_name="pool_top_combos")
            empty_mods.to_excel(writer, index=False, sheet_name="pool_top_mods")
            empty_sellers.to_excel(writer, index=False, sheet_name="top_sellers")
            empty_mods_by_pass.to_excel(writer, index=False, sheet_name="mods_by_pass")
            empty_sellers_by_mod.to_excel(writer, index=False, sheet_name="top_sellers_by_mod")
            pd.DataFrame(columns=["mod", "stat_id"]).to_excel(writer, index=False, sheet_name="final_query_mods")
            empty_final.to_excel(writer, index=False, sheet_name="final_raw")
            empty_mods.to_excel(writer, index=False, sheet_name="final_top_mods")
            empty_ranked.to_excel(writer, index=False, sheet_name="final_top_k2")
            empty_ranked.to_excel(writer, index=False, sheet_name="final_top_k3")
            empty_ranked.to_excel(writer, index=False, sheet_name="final_top_k4")
            pd.DataFrame(
                [
                    {
                        "error": "No candidates collected",
                        "cache_used": cache_used,
                        "cache_path": str(config.cache_path) if cache_used else None,
                        "cache_ids_before": cache_before if cache_used else None,
                        "cache_ids_after": cache_after if cache_used else None,
                        "cache_items_before": cache_items_before if cache_used else None,
                        "cache_items_after": cache_items_after if cache_used else None,
                        "cache_used_fallback": cache_used_fallback if cache_used else None,
                        "cache_migrated": cache_migrated if cache_used else None,
                        "trade_search_url": None,
                        "last_query_url": None,
                        "final_max_div": None,
                        "final_query_stats_count": 0,
                    }
                ]
            ).to_excel(writer, index=False, sheet_name="meta")

        return PipelineArtifacts(output_file=output_path, candidates_count=0, final_count=0, final_trade_url=None)

    k_targets = [2] if config.jewel_rarity.lower() == "magic" else [3, 4]
    ranked_frames = [
        analyze_top_k(analysis_candidates, k=k, top_n=config.top_pairs_in_pool, min_count=config.min_count_to_rank)
        for k in k_targets
    ]
    ranked_frames = [frame for frame in ranked_frames if not frame.empty]
    if ranked_frames:
        ranked_combos = pd.concat(ranked_frames, ignore_index=True)
    else:
        LOGGER.warning("No ranked combinations built (insufficient candidates).")
        ranked_combos = pd.DataFrame(columns=["k", "combo", "count", "max"])
    top_mods = analyze_top_mods(analysis_candidates, top_n=config.top_mods_in_pool)
    top_sellers = analyze_top_sellers(analysis_candidates)
    top_sellers_by_mod = analyze_top_sellers_by_mod(analysis_candidates)
    mods_by_pass = analyze_mods_by_pass(analysis_candidates)
    stat_groups, missing_mods, final_query_mods = build_count_stat_group(top_mods["mod"].tolist(), config, stat_map)

    final_search = client.trade_search(config.final_min_div, config.max_div, stat_groups=stat_groups)
    final_trade_url = make_trade_url(config.base_url, config.realm, config.league, final_search["id"])
    final_ids = (final_search.get("result") or [])[: config.max_fetch_per_search]
    if not final_ids:
        raise RuntimeError("Final search returned 0 items")

    final_rows: list[dict[str, Any]] = []
    for i in range(0, len(final_ids), config.fetch_chunk):
        chunk = final_ids[i : i + config.fetch_chunk]
        fetched = client.trade_fetch(chunk, final_search["id"])
        for hit in fetched.get("result", []):
            listing = hit.get("listing", {}) or {}
            price = parse_price(listing, config.price_currency)
            if price is None:
                continue
            item = hit.get("item", {}) or {}
            mod_lines = extract_mod_lines(item)
            final_rows.append(
                {
                    "price_div": price,
                    "mods_norm": sorted({normalize_mod_text(mod) for mod in mod_lines if mod.strip()}),
                    "explicit_raw": " || ".join(mod_lines),
                }
            )

    final_df = pd.DataFrame(final_rows)
    if final_df.empty:
        raise RuntimeError("Final fetch returned no rows")

    final_top_mods = analyze_top_mods(final_df, top_n=50)
    final_top_k2 = analyze_top_k(final_df, k=2, top_n=50, min_count=config.min_count_to_rank)
    final_top_k3 = analyze_top_k(final_df, k=3, top_n=50, min_count=config.min_count_to_rank)
    final_top_k4 = analyze_top_k(final_df, k=4, top_n=50, min_count=config.min_count_to_rank)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = config.output_dir / f"{config.output_name()}_meta_pipeline_{timestamp}.xlsx"

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        analysis_candidates.to_excel(writer, index=False, sheet_name="candidates_raw")
        if config.use_cache:
            candidates.to_excel(writer, index=False, sheet_name="candidates_new")
        sort_by_count_desc(ranked_combos).to_excel(writer, index=False, sheet_name="pool_top_combos")
        sort_by_count_desc(top_mods).to_excel(writer, index=False, sheet_name="pool_top_mods")
        sort_by_count_desc(top_sellers).to_excel(writer, index=False, sheet_name="top_sellers")
        top_sellers_by_mod.to_excel(writer, index=False, sheet_name="top_sellers_by_mod")
        mods_by_pass.to_excel(writer, index=False, sheet_name="mods_by_pass")
        pd.DataFrame(final_query_mods).to_excel(writer, index=False, sheet_name="final_query_mods")

        final_df.to_excel(writer, index=False, sheet_name="final_raw")
        sort_by_count_desc(final_top_mods).to_excel(writer, index=False, sheet_name="final_top_mods")
        if not final_top_k2.empty:
            sort_by_count_desc(final_top_k2).to_excel(writer, index=False, sheet_name="final_top_k2")
        if not final_top_k3.empty:
            sort_by_count_desc(final_top_k3).to_excel(writer, index=False, sheet_name="final_top_k3")
        if not final_top_k4.empty:
            sort_by_count_desc(final_top_k4).to_excel(writer, index=False, sheet_name="final_top_k4")

        meta_df = pd.DataFrame(
            [
                {
                    "trade_search_url": final_trade_url,
                    "last_query_url": final_trade_url,
                    "final_min_div": config.final_min_div,
                    "final_max_div": config.max_div,
                    "count_min_match": config.count_min_match,
                    "final_query_stats_count": len(final_query_mods),
                    "missing_mods": len(missing_mods),
                    "candidates_rows": len(analysis_candidates),
                    "new_candidates_rows": len(candidates),
                    "final_rows": len(final_df),
                    "unique_sellers": analysis_candidates["seller_account"].nunique() if "seller_account" in analysis_candidates else None,
                    "cache_used": cache_used,
                    "cache_path": str(config.cache_path) if cache_used else None,
                    "cache_ids_before": cache_before if cache_used else None,
                    "cache_ids_after": cache_after if cache_used else None,
                    "cache_items_before": cache_items_before if cache_used else None,
                    "cache_items_after": cache_items_after if cache_used else None,
                    "cache_used_fallback": cache_used_fallback if cache_used else None,
                    "cache_migrated": cache_migrated if cache_used else None,
                }
            ]
        )
        meta_df.to_excel(writer, index=False, sheet_name="meta")

        ws_meta = writer.sheets.get("meta")
        if ws_meta is not None:
            def _set_url_link(column_name: str) -> None:
                if column_name not in meta_df.columns:
                    return
                col_idx = meta_df.columns.get_loc(column_name) + 1
                cell = ws_meta.cell(row=2, column=col_idx)
                if isinstance(cell.value, str) and cell.value.startswith("http"):
                    cell.hyperlink = cell.value
                    cell.style = "Hyperlink"

            _set_url_link("trade_search_url")
            _set_url_link("last_query_url")

    return PipelineArtifacts(
        output_file=output_path,
        candidates_count=len(analysis_candidates),
        final_count=len(final_df),
        final_trade_url=final_trade_url,
    )
