import os
from pathlib import Path

from pydantic import Field, SecretStr

from autonomous_studio.domain.models import Model
from autonomous_studio.hitl.adapters import TelegramConfig


def load_local_env(path: Path | None = None):
    """Load only known studio variables; process environment takes precedence."""
    path = path or Path.cwd() / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if separator and (
            key in {"OPENAI_API_KEY", "GEMINI_API_KEY"}
            or key.startswith(("STUDIO_", "TELEGRAM_", "YOUTUBE_"))
        ):
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


class Settings(Model):
    api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    research_model: str = "gpt-6-luna"
    director_model: str = "gpt-5.6-sol"
    shorts_research_model: str = "gpt-6-luna"
    shorts_creative_model: str = "gpt-6.1-sol"
    shorts_repair_model: str = "gpt-6-luna"
    story_model: str = "gpt-6.1-sol"
    continuity_model: str = "gpt-6-luna"
    visual_plan_model: str = "gpt-6-luna"
    visual_repair_model: str = "gpt-6-luna"
    semantic_layout_model: str = "gpt-6-luna"
    longform_metadata_model: str = "gpt-6-luna"
    tts_model: str = "gpt-4o-mini-tts"
    voice: str = "coral"
    tts_provider: str = Field(default="gemini", pattern="^(gemini|openai)$")
    longform_tts_provider: str = "gemini"
    longform_tts_model: str = "gemini-3.8-flash-tts"
    longform_tts_voice: str = "Aoede"
    max_output_tokens: int = Field(default=20000, ge=1000, le=20000)
    auto_assets: bool = True
    image_model: str = "gpt-image-2"
    image_quality: str = "medium"
    max_image_requests: int = Field(default=2, ge=1, le=18)
    music_catalog: str = "music/youtube-audio-library.yaml"
    closed_captions: bool = True
    youtube_category_id: str = Field(default="28", pattern=r"^\d{1,3}$")
    rate_card: str | None = None

    @classmethod
    def environment(cls):
        load_local_env()
        return cls(
            api_key=os.getenv("OPENAI_API_KEY"),
            gemini_api_key=os.getenv("GEMINI_API_KEY"),
            research_model=os.getenv("STUDIO_RESEARCH_MODEL", "gpt-6-luna"),
            director_model=os.getenv("STUDIO_DIRECTOR_MODEL", "gpt-5.6-sol"),
            shorts_research_model=os.getenv("STUDIO_SHORTS_RESEARCH_MODEL", "gpt-6-luna"),
            shorts_creative_model=os.getenv("STUDIO_SHORTS_CREATIVE_MODEL", "gpt-6.1-sol"),
            shorts_repair_model=os.getenv("STUDIO_SHORTS_REPAIR_MODEL", "gpt-6-luna"),
            story_model=os.getenv("STUDIO_STORY_MODEL", "gpt-6.1-sol"),
            continuity_model=os.getenv("STUDIO_CONTINUITY_MODEL", "gpt-6-luna"),
            visual_plan_model=os.getenv("STUDIO_VISUAL_PLAN_MODEL", "gpt-6-luna"),
            visual_repair_model=os.getenv("STUDIO_VISUAL_REPAIR_MODEL", "gpt-6-luna"),
            semantic_layout_model=os.getenv("STUDIO_SEMANTIC_LAYOUT_MODEL", "gpt-6-luna"),
            longform_metadata_model=os.getenv("STUDIO_LONGFORM_METADATA_MODEL", "gpt-6-luna"),
            tts_model=os.getenv("STUDIO_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.getenv("STUDIO_VOICE", "coral"),
            tts_provider=os.getenv(
                "STUDIO_TTS_PROVIDER", os.getenv("STUDIO_LONGFORM_TTS_PROVIDER", "gemini")
            ),
            longform_tts_provider=os.getenv("STUDIO_LONGFORM_TTS_PROVIDER", "gemini"),
            longform_tts_model=os.getenv("STUDIO_LONGFORM_TTS_MODEL", "gemini-3.8-flash-tts"),
            longform_tts_voice=os.getenv("STUDIO_LONGFORM_TTS_VOICE", "Aoede"),
            max_output_tokens=int(os.getenv("STUDIO_MAX_OUTPUT_TOKENS", "20000")),
            auto_assets=os.getenv("STUDIO_AUTO_ASSETS", "true").lower() == "true",
            image_model=os.getenv("STUDIO_IMAGE_MODEL", "gpt-image-2"),
            image_quality=os.getenv("STUDIO_IMAGE_QUALITY", "medium"),
            max_image_requests=int(os.getenv("STUDIO_MAX_IMAGE_REQUESTS", "2")),
            music_catalog=os.getenv("STUDIO_MUSIC_CATALOG", "music/youtube-audio-library.yaml"),
            closed_captions=os.getenv("STUDIO_CLOSED_CAPTIONS", "true").lower() == "true",
            youtube_category_id=os.getenv("STUDIO_YOUTUBE_CATEGORY_ID", "28"),
            rate_card=os.getenv("STUDIO_RATE_CARD"),
        )

    def require_key(self):
        if not self.api_key or not self.api_key.get_secret_value().strip():
            raise ValueError("Set OPENAI_API_KEY in your environment to run a live project")
        return self.api_key.get_secret_value()

    def require_gemini_key(self):
        if not self.gemini_api_key or not self.gemini_api_key.get_secret_value().strip():
            raise ValueError("Set GEMINI_API_KEY in the local environment for Gemini narration")
        return self.gemini_api_key.get_secret_value()

    def model_ids(self):
        return {
            key: getattr(self, key)
            for key in (
                "research_model", "director_model", "shorts_research_model",
                "shorts_creative_model", "shorts_repair_model", "story_model", "continuity_model",
                "visual_plan_model", "visual_repair_model", "semantic_layout_model",
                "longform_metadata_model",
                "tts_model", "voice", "longform_tts_model", "longform_tts_provider",
                "longform_tts_voice",
            )
        }

    def asset_policy(self):
        from autonomous_studio.domain.models import AutoAssetPolicy

        return AutoAssetPolicy(
            image_model=self.image_model,
            image_quality=self.image_quality,
            max_image_requests=self.max_image_requests,
            music_catalog=str(Path(self.music_catalog).resolve()),
        )


def telegram_environment():
    load_local_env()
    required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ALLOWED_USER_IDS")
    if any(not os.getenv(key) for key in required):
        raise ValueError("Set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID and TELEGRAM_ALLOWED_USER_IDS")
    # Do not echo token-bearing validation inputs on invalid configuration.
    try:
        return TelegramConfig(
            bot_token=os.environ[required[0]],
            chat_id=int(os.environ[required[1]]),
            allowed_user_ids=[int(x.strip()) for x in os.environ[required[2]].split(",")],
        )
    except ValueError:
        raise ValueError("Invalid Telegram configuration; check token and numeric IDs") from None
