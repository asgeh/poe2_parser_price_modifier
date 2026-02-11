from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import difflib

from .config import PipelineConfig
from .utils import expand_bracket_variants, extract_mod_lines, normalize_mod_text, parse_price

LOGGER = logging.getLogger(__name__)

def _pd():
    import pandas as pd  # local import to keep utility functions testable without pandas

    return pd


@dataclass(slots=True)
class PipelineArtifacts:
    output_file: Path
    candidates_count: int
    final_count: int


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

    def trade_search(self, min_div: float, max_div: Optional[float], stat_groups: Optional[list[dict[str, Any]]]) -> dict[str, Any]:
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
        return pd.DataFrame(columns=["k", "combo", "count", "median", "p75", "max", "mean"])
    grouped = df_k.groupby(["k", "combo"])["price_div"]
    ranked = grouped.agg(count="count", median="median", p75=lambda x: x.quantile(0.75), max="max", mean="mean").reset_index()
    ranked = ranked[ranked["count"] >= min_count]
    return ranked.sort_values(["median", "count"], ascending=[False, False])


def analyze_top_mods(df_raw: pd.DataFrame, top_n: int = 30) -> pd.DataFrame:
    pd = _pd()
    rows: list[dict[str, Any]] = []
    for _, row in df_raw.iterrows():
        mods = row.get("mods_norm")
        if not isinstance(mods, list):
            continue
        for mod in set(mods):
            rows.append({"mod": mod, "price_div": float(row["price_div"])})
    if not rows:
        return pd.DataFrame(columns=["mod", "count", "median", "p75", "max", "mean"])

    mods_df = pd.DataFrame(rows)
    grouped = mods_df.groupby("mod")["price_div"]
    ranked = grouped.agg(count="count", median="median", p75=lambda x: x.quantile(0.75), max="max", mean="mean").reset_index()
    return ranked.sort_values(["median", "count"], ascending=[False, False]).head(top_n)


def analyze_top_k(df_raw: pd.DataFrame, k: int, top_n: int, min_count: int) -> pd.DataFrame:
    return rank_k_combos(build_k_combos(df_raw, k), min_count=min_count).head(top_n)


def build_count_stat_group(ranked_combos: pd.DataFrame, config: PipelineConfig, stat_map: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[str]]:
    combos = ranked_combos.head(config.top_pairs_in_pool)["combo"].tolist()
    ordered_mods: list[str] = []
    seen = set()
    for combo in combos:
        for part in combo.split(" | "):
            mod = part.strip()
            if mod and mod not in seen:
                seen.add(mod)
                ordered_mods.append(mod)

    stat_ids: list[str] = []
    missing: list[str] = []
    for mod in ordered_mods:
        ids = resolve_stat_ids(mod, stat_map)
        if ids:
            stat_ids.append(ids[0])
        else:
            missing.append(mod)
        if len(stat_ids) >= config.max_pool_stats:
            break

    stat_ids = list(dict.fromkeys(stat_ids))
    if not stat_ids:
        raise RuntimeError("stat pool is empty")

    return [
        {
            "type": "count",
            "filters": [{"id": stat_id, "value": {"min": 0}} for stat_id in stat_ids],
            "value": {"min": int(config.count_min_match)},
        }
    ], missing


def collect_candidates(client: TradeClient, config: PipelineConfig) -> pd.DataFrame:
    pd = _pd()
    all_rows: list[dict[str, Any]] = []
    seen = set()

    for mn, mx in config.price_windows:
        search = client.trade_search(mn, mx, stat_groups=None)
        ids = [item_id for item_id in (search.get("result") or []) if item_id not in seen][: config.max_fetch_per_search]
        for item_id in ids:
            seen.add(item_id)

        for i in range(0, len(ids), config.fetch_chunk):
            chunk = ids[i : i + config.fetch_chunk]
            fetched = client.trade_fetch(chunk, search["id"])
            for hit in fetched.get("result", []):
                listing = hit.get("listing", {}) or {}
                price = parse_price(listing, config.price_currency)
                if price is None:
                    continue
                item = hit.get("item", {}) or {}
                mod_lines = extract_mod_lines(item)
                all_rows.append(
                    {
                        "price_div": price,
                        "mods_norm": sorted({normalize_mod_text(mod) for mod in mod_lines if mod.strip()}),
                        "explicit_raw": " || ".join(mod_lines),
                        "source_min_div": mn,
                        "source_max_div": mx,
                    }
                )

    return pd.DataFrame(all_rows)


def make_trade_url(base: str, realm: str, league: str, search_id: str) -> str:
    league_encoded = quote(league, safe="")
    return f"{base}/trade2/search/{realm}/{league_encoded}/{search_id}"


def run_pipeline(config: PipelineConfig) -> PipelineArtifacts:
    pd = _pd()
    stat_map = load_stat_map(config.stats_path, groups=["explicit", "implicit", "desecrated"])
    client = TradeClient(config)

    candidates = collect_candidates(client, config)
    if candidates.empty:
        raise RuntimeError("No candidates collected")

    k_targets = [2] if config.jewel_rarity.lower() == "magic" else [3, 4]
    ranked_frames = [analyze_top_k(candidates, k=k, top_n=config.top_pairs_in_pool, min_count=config.min_count_to_rank) for k in k_targets]
    ranked_frames = [frame for frame in ranked_frames if not frame.empty]
    if not ranked_frames:
        raise RuntimeError("Unable to build ranked combinations")

    ranked_combos = pd.concat(ranked_frames, ignore_index=True)
    top_mods = analyze_top_mods(candidates, top_n=config.top_mods_in_pool)
    stat_groups, missing_mods = build_count_stat_group(ranked_combos, config, stat_map)

    final_search = client.trade_search(config.final_min_div, config.max_div, stat_groups=stat_groups)
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
    output_path = config.output_dir / f"{config.output_name()}_meta_pipeline_{timestamp}.xlsx"

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        candidates.to_excel(writer, index=False, sheet_name="candidates_raw")
        ranked_combos.to_excel(writer, index=False, sheet_name="pool_top_combos")
        top_mods.to_excel(writer, index=False, sheet_name="pool_top_mods")

        final_df.to_excel(writer, index=False, sheet_name="final_raw")
        final_top_mods.to_excel(writer, index=False, sheet_name="final_top_mods")
        if not final_top_k2.empty:
            final_top_k2.to_excel(writer, index=False, sheet_name="final_top_k2")
        if not final_top_k3.empty:
            final_top_k3.to_excel(writer, index=False, sheet_name="final_top_k3")
        if not final_top_k4.empty:
            final_top_k4.to_excel(writer, index=False, sheet_name="final_top_k4")

        pd.DataFrame(
            [
                {
                    "trade_search_url": make_trade_url(config.base_url, config.realm, config.league, final_search["id"]),
                    "final_min_div": config.final_min_div,
                    "count_min_match": config.count_min_match,
                    "missing_mods": len(missing_mods),
                    "candidates_rows": len(candidates),
                    "final_rows": len(final_df),
                }
            ]
        ).to_excel(writer, index=False, sheet_name="meta")

    return PipelineArtifacts(output_file=output_path, candidates_count=len(candidates), final_count=len(final_df))
