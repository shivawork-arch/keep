from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Backend settings, read from the repo-root .env. Missing fields raise at startup."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    mongo_uri: str
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    ollama_url: str
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768  # must match the Neo4j vector index
