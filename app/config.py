"""Application configuration, loaded from environment / .env file."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    wcl_client_id: str = ""
    wcl_client_secret: str = ""

    guild_name: str = ""
    guild_server_slug: str = ""
    guild_region: str = "EU"

    default_timeframe_days: int = 14
    max_reports: int = 40

    # Restrict analysis to raid content by excluding Mythic+ reports (their DPS/
    # deaths aren't comparable to raid). Matches report zone names against the
    # patterns below (case-insensitive substring).
    exclude_mythic_plus: bool = True
    mythic_plus_zone_patterns: tuple[str, ...] = ("mythic+", "mythic plus")

    # Only analyze fights at this raid difficulty. 1=LFR, 3=Normal, 4=Heroic,
    # 5=Mythic. Set to 0 to include all difficulties.
    raid_difficulty: int = 5

    # Core-raider filter: a player is included only if they were present for at
    # least this fraction of the raid NIGHTS in the window. Excludes one-off pugs
    # and socials. Set to 0 to include everyone who appears in the logs.
    min_attendance_pct: float = 0.5

    # Death ranking: only the first N deaths of a pull count as "meaningful".
    # A wipe kills everyone, so later deaths are just the cascade — ranking by
    # early deaths surfaces who actually triggers wipes rather than who was
    # present for them. Lower = stricter (1 = first death only). Also reused as
    # the "live portion" cutoff for avoidable-damage checks (e.g. Glaives).
    early_death_cutoff: int = 3

    # Killing-blow abilities that shouldn't count against the victim in the death
    # checks (Most Early Deaths / Dies First). Some mechanics are unavoidable
    # one-shots that reflect a raid failure (e.g. a missed interrupt on Midnight
    # Falls' Terminate), not the dead player's mistake. Matched case-insensitively
    # on the killing-blow ability name. Editable without code changes.
    non_culpable_death_abilities: tuple[str, ...] = ("Terminate",)

    # Midnight Falls boss-specific tracking. The Glaive ("Heaven's Glaives")
    # tracker fetches damage-taken events for this ability, scoped to this
    # encounter, on the boss panel. Confirmed from live logs; override if Blizzard
    # re-IDs the spell. Set glaive_ability_id to 0 to disable the fetch.
    midnight_falls_encounter_id: int = 3183
    glaive_ability_id: int = 1254076

    # WCL endpoints
    wcl_token_url: str = "https://www.warcraftlogs.com/oauth/token"
    wcl_api_url: str = "https://www.warcraftlogs.com/api/v2/client"

    @property
    def configured(self) -> bool:
        return bool(self.wcl_client_id and self.wcl_client_secret and self.guild_name and self.guild_server_slug)


settings = Settings()
