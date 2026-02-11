import argparse
import logging

from .config import PipelineConfig
from .pipeline import TradeClient, make_trade_url


def build_parser() -> argparse.ArgumentParser:
    defaults = PipelineConfig()
    parser = argparse.ArgumentParser(
        description="Debug POE2 trade search and inspect returned ids/items"
    )
    parser.add_argument("--league", default=defaults.league)
    parser.add_argument("--item-name", default=defaults.item_name)
    parser.add_argument("--item-type", default=defaults.item_type)
    parser.add_argument("--rarity", default=defaults.jewel_rarity, choices=["magic", "rare", "unique"])
    parser.add_argument("--indexed", default=defaults.indexed)
    parser.add_argument("--min-div", type=float, default=200)
    parser.add_argument("--max-div", type=float, default=300)
    parser.add_argument("--price-currency", default=defaults.price_currency, choices=["divine", "exalted"])
    parser.add_argument("--limit", type=int, default=5, help="How many ids/items to show")
    parser.add_argument("--no-fetch", action="store_true", help="Only show search ids, skip fetch")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    config = PipelineConfig(
        league=args.league,
        item_name=args.item_name,
        item_type=args.item_type,
        jewel_rarity=args.rarity,
        indexed=args.indexed,
        price_currency=args.price_currency,
    )

    client = TradeClient(config)
    response = client.trade_search(args.min_div, args.max_div, stat_groups=None)

    search_id = response.get("id")
    ids = response.get("result") or []
    total = response.get("total")

    print("search_id:", search_id)
    print("total:", total)
    print("result_len:", len(ids))
    print("trade_url:", make_trade_url(config.base_url, config.realm, config.league, search_id))
    print("first_ids:", ids[: args.limit])

    if args.no_fetch:
        return

    fetch_ids = ids[: args.limit]
    if not fetch_ids:
        return

    fetched = client.trade_fetch(fetch_ids, search_id)
    for hit in fetched.get("result", []):
        listing = hit.get("listing") or {}
        account = listing.get("account") or {}
        price = listing.get("price") or {}
        item = hit.get("item") or {}

        print("---")
        print("id:", hit.get("id"))
        print("price:", price.get("amount"), price.get("currency"))
        print("account:", account.get("name"))
        print("character:", account.get("lastCharacterName"))
        print("item:", item.get("name"), item.get("typeLine"))
        print("indexed:", listing.get("indexed"))


if __name__ == "__main__":
    main()
