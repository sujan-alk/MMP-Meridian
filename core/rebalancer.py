"""
Cross-Exchange Rebalancer — generates rebalance suggestions when
token inventory is unevenly distributed across exchanges.

This is advisory only: it logs suggestions but does NOT execute transfers.
Manual intervention is required to move funds between exchanges.
"""

from __future__ import annotations

from utils.logging import get_logger

log = get_logger("rebalancer")

# Thresholds for triggering rebalance suggestions
EXCESS_THRESHOLD = 0.60   # Exchange has >60% of total tokens → overweight
DEFICIT_THRESHOLD = 0.20  # Exchange has <20% of total tokens → underweight


class Rebalancer:
    """
    Checks token balances across exchanges and suggests rebalancing
    when any single exchange holds a disproportionate share.
    """

    def __init__(
        self,
        excess_threshold: float = EXCESS_THRESHOLD,
        deficit_threshold: float = DEFICIT_THRESHOLD,
    ):
        self.excess_threshold = excess_threshold
        self.deficit_threshold = deficit_threshold

    async def check_imbalance(self, balances: dict[str, float]) -> list[dict]:
        """
        Check for imbalanced token distribution across exchanges.

        Args:
            balances: dict mapping exchange name → token balance
                      e.g. {"kucoin": 500_000, "gate": 300_000, "mexc": 200_000}

        Returns:
            List of rebalance suggestion dicts, each containing:
                - from_exchange: str
                - to_exchange: str
                - suggested_amount: float (tokens)
                - reason: str
        """
        total_tokens = sum(balances.values())
        if total_tokens <= 0:
            return []

        suggestions: list[dict] = []

        # Find overweight and underweight exchanges
        overweight: list[tuple[str, float]] = []
        underweight: list[tuple[str, float]] = []

        for exchange, tokens in balances.items():
            share = tokens / total_tokens
            if share > self.excess_threshold:
                overweight.append((exchange, tokens))
            elif share < self.deficit_threshold:
                underweight.append((exchange, tokens))

        # Generate suggestions: move from overweight to underweight
        for over_ex, over_tokens in overweight:
            over_share = over_tokens / total_tokens
            for under_ex, under_tokens in underweight:
                under_share = under_tokens / total_tokens
                # Suggest moving enough to bring overweight to ~equal distribution
                target_share = 1.0 / len(balances)
                suggested_amount = (over_share - target_share) * total_tokens * 0.5

                if suggested_amount > 0:
                    suggestion = {
                        "from_exchange": over_ex,
                        "to_exchange": under_ex,
                        "suggested_amount": round(suggested_amount, 2),
                        "reason": (
                            f"{over_ex} holds {over_share:.1%} of tokens "
                            f"(>{self.excess_threshold:.0%}), "
                            f"{under_ex} holds {under_share:.1%} "
                            f"(<{self.deficit_threshold:.0%})"
                        ),
                    }
                    suggestions.append(suggestion)
                    log.warning(
                        "rebalance_suggestion",
                        from_exchange=over_ex,
                        to_exchange=under_ex,
                        suggested_amount=suggested_amount,
                        reason=suggestion["reason"],
                    )

        if not suggestions:
            log.debug("rebalance_check_ok", exchanges=list(balances.keys()))

        return suggestions
