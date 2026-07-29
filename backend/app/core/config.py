from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "LeadHunter Pro AI"
    APP_VERSION: str = "0.1.0"

    DATABASE_URL: str

    API_PREFIX: str = "/api"

    LOG_LEVEL: str = "INFO"

    OLLAMA_URL: str = "http://localhost:11434"

    class Config:
        env_file = ".env"


settings = Settings()