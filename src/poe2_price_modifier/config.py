from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(slots=True)
class PipelineConfig:
    base_url: str = "https://www.pathofexile.com"
    realm: str = "poe1"
    league: str = "Phrecia 2.0"

    item_name: Optional[str] = None
    item_type: str = "Emerald"
    jewel_rarity: str = "rare"

    indexed: str = "12hours"
    final_min_div: float = 10
    max_div: Optional[float] = None
    price_currency: str = "divine"

    fetch_chunk: int = 10
    max_fetch_per_search: int = 100
    sleep_search: float = 1.5
    sleep_fetch: float = 2.0

    top_pairs_in_pool: int = 30
    top_mods_in_pool: int = 30
    min_count_to_rank: int = 2
    count_min_match: int = 3
    max_pool_stats: int = 60

    price_windows: list[tuple[float, float]] = field(default_factory=lambda: [(200, 300)])

    stats_path: Path = Path("stats.json")
    output_dir: Path = Path(".")

    user_agent: str = "Mozilla/5.0 PoE2JewelComboStats/2.0"

    def output_name(self) -> str:
        base_name = self.item_name or self.item_type
        safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in base_name).strip("_") or "x"
        return f"poe2_{safe}_{self.jewel_rarity}_{self.indexed}"
