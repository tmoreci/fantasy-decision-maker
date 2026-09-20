"""Data providers. Each one fails softly and records what it could not supply."""

from fantasy_decision.providers.base import HttpProvider, ProviderError, soft_call
from fantasy_decision.providers.espn import EspnProvider, implied_totals
from fantasy_decision.providers.nflverse import NflverseProvider
from fantasy_decision.providers.sleeper import PRESETS, SleeperProvider

__all__ = [
    "PRESETS",
    "EspnProvider",
    "HttpProvider",
    "NflverseProvider",
    "ProviderError",
    "SleeperProvider",
    "implied_totals",
    "soft_call",
]
