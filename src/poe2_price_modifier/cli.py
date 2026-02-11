import argparse
import logging
from pathlib import Path

from .config import PipelineConfig
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze expensive POE jewel modifiers and export Excel report")
    parser.add_argument("--league", default="Fate of the Vaal")
    parser.add_argument("--item-name", default=None)
    parser.add_argument("--item-type", default="Emerald")
    parser.add_argument("--rarity", default="rare", choices=["magic", "rare", "unique"])
    parser.add_argument("--indexed", default="12hours")
    parser.add_argument("--final-min-div", type=float, default=10)
    parser.add_argument("--max-div", type=float, default=None)
    parser.add_argument("--price-currency", default="divine", choices=["divine", "exalted"])
    parser.add_argument("--stats-path", type=Path, default=Path("stats.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("."))
    parser.add_argument("--price-window", action="append", default=["200:300"], help="Format min:max. Can repeat.")
    parser.add_argument("--verbose", action="store_true")
    return parser


def parse_windows(raw_values: list[str]) -> list[tuple[float, float]]:
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
    config = PipelineConfig(
        league=args.league,
        item_name=args.item_name,
        item_type=args.item_type,
        jewel_rarity=args.rarity,
        indexed=args.indexed,
        final_min_div=args.final_min_div,
        max_div=args.max_div,
        price_currency=args.price_currency,
        stats_path=args.stats_path,
        output_dir=args.output_dir,
        price_windows=parse_windows(args.price_window),
        count_min_match=count_min_match,
    )

    result = run_pipeline(config)
    print(f"DONE: {result.output_file} | candidates={result.candidates_count} | final={result.final_count}")


if __name__ == "__main__":
    main()
