from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_path: str = "kairos.db"

    # Anthropic
    anthropic_api_key: str = ""
    model_fast: str = "claude-haiku-4-5-20251001"   # staypoint disambiguation
    model_smart: str = "claude-sonnet-4-6"          # plan rationale, enrichment

    # External enrichment
    openweather_api_key: str = ""
    locationiq_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    public_base_url: str = ""        # https URL the bot webhook points at, e.g. https://kairos.example.com

    # Staypoint detection
    staypoint_radius_m: float = 150.0
    staypoint_min_dwell_s: float = 720.0     # 12 minutes

    # Scoring weights
    w_pref: float = 1.0
    w_fit: float = 1.2
    w_prog: float = 0.6
    w_expl: float = 0.4
    explore_ratio: float = 0.2               # share of plan slots filled by exploration

    # Kairos-window nudge
    kairos_min_score: float = 2.2
    kairos_min_days_since: int = 7

    tz: str = "Europe/Prague"

    # Debug
    debug_gps: bool = False   # set DEBUG_GPS=true to send a Telegram message on every ping


settings = Settings()
