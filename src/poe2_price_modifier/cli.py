import argparse
import logging
import os
from pathlib import Path

from .config import PipelineConfig
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(description="Analyze expensive POE jewel modifiers and export Excel report")
    parser.add_argument("--league", default=defaults.league)
    parser.add_argument("--item-category", default=defaults.item_category, help="Trade API category, e.g. jewel, amulet, ring, armour.chest")
    parser.add_argument("--item-name", default=defaults.item_name)
    parser.add_argument("--item-type", default=defaults.item_type)
    parser.add_argument("--rarity", default=defaults.jewel_rarity, choices=["magic", "rare", "unique"])
    parser.add_argument("--indexed", default=defaults.indexed)
    parser.add_argument("--final-min-div", type=float, default=defaults.final_min_div)
    parser.add_argument("--max-div", type=float, default=defaults.max_div)
    parser.add_argument("--price-currency", default=defaults.price_currency, choices=["divine", "exalted"])
    parser.add_argument("--stats-path", type=Path, default=defaults.stats_path)
    parser.add_argument("--output-dir", type=Path, default=defaults.output_dir)
    parser.add_argument(
        "--price-window",
        action="append",
        default=None,
        help="Format min:max. Can repeat. If omitted, uses config default windows.",
    )
    parser.add_argument("--sleep-search", type=float, default=defaults.sleep_search)
    parser.add_argument("--sleep-fetch", type=float, default=defaults.sleep_fetch)
    parser.add_argument("--fetch-chunk", type=int, default=defaults.fetch_chunk)
    parser.add_argument("--max-fetch-per-search", type=int, default=defaults.max_fetch_per_search)
    parser.add_argument(
        "--candidate-passes",
        type=int,
        default=defaults.candidate_passes,
        help="How many repeated searches to run for collecting candidates",
    )
    parser.add_argument(
        "--stop-when-no-new",
        action="store_true",
        help="Stop passes early if no new items appear",
    )
    cache_group = parser.add_mutually_exclusive_group()
    cache_group.add_argument(
        "--use-cache",
        dest="use_cache",
        action="store_true",
        help="Skip items already seen in the local cache",
    )
    cache_group.add_argument(
        "--no-cache",
        dest="use_cache",
        action="store_false",
        help="Disable cache usage",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Clear cache before running and rebuild it from fresh results",
    )
    parser.add_argument(
        "--cache-path",
        type=Path,
        default=defaults.cache_path,
        help="Path to JSON cache file with ids and item data",
    )
    parser.add_argument(
        "--poesessid",
        default=None,
        help="POESESSID cookie value. If omitted, reads POESESSID from environment.",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.set_defaults(use_cache=True)
    return parser


def parse_windows(
    raw_values: list[str] | None,
    default_windows: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    if not raw_values:
        return list(default_windows)
    windows = []
    for raw in raw_values:
        min_s, max_s = raw.split(":", maxsplit=1)
        windows.append((float(min_s), float(max_s)))
    return windows


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    count_min_match = 2 if args.rarity == "magic" else 3
    defaults = PipelineConfig()
    config = PipelineConfig(
        league=args.league,
        item_category=args.item_category,
        item_name=args.item_name,
        item_type=args.item_type,
        jewel_rarity=args.rarity,
        indexed=args.indexed,
        final_min_div=args.final_min_div,
        max_div=args.max_div,
        price_currency=args.price_currency,
        stats_path=args.stats_path,
        output_dir=args.output_dir,
        price_windows=parse_windows(args.price_window, defaults.price_windows),
        sleep_search=args.sleep_search,
        sleep_fetch=args.sleep_fetch,
        fetch_chunk=args.fetch_chunk,
        max_fetch_per_search=args.max_fetch_per_search,
        count_min_match=count_min_match,
        candidate_passes=args.candidate_passes,
        stop_when_no_new=args.stop_when_no_new,
        use_cache=args.use_cache,
        cache_path=args.cache_path,
        refresh_cache=args.refresh_cache,
        poesessid=args.poesessid or os.getenv("POESESSID"),
    )

    result = run_pipeline(config)
    print(f"DONE: {result.output_file} | candidates={result.candidates_count} | final={result.final_count}")
    if result.final_trade_url:
        print(f"LAST_QUERY_URL: {result.final_trade_url}")


if __name__ == "__main__":
    main()
