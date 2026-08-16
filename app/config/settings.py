"""Application Settings and Configuration"""
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


class Settings:
    """Application Settings"""

    # API Configuration
    API_TITLE: str = "MedicaAI MVP"
    API_VERSION: str = "0.1.0"
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"

    # Server Configuration
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # LLM Configuration
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    GOOGLE_API_KEY: Optional[str] = os.getenv("GOOGLE_API_KEY")

    # Tavily Search Configuration
    TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")

    # Twilio Configuration
    TWILIO_ACCOUNT_SID: str = os.getenv("TWILIO_ACCOUNT_SID", "")
    TWILIO_AUTH_TOKEN: str = os.getenv("TWILIO_AUTH_TOKEN", "")
    TWILIO_PHONE_NUMBER: str = os.getenv("TWILIO_PHONE_NUMBER", "")

    # Database Configuration (if needed)
    DATABASE_URL: Optional[str] = os.getenv("DATABASE_URL")

    # Environment
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")

    class Config:
        env_file = ".env"


settings = Settings()
