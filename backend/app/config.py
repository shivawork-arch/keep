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
    github_webhook_secret: str
    n8n_router_url: str = "http://localhost:5678/webhook/github"

    # Claude access. One setting switches: "subscription" (claude -p) or "api" (API key).
    claude_mode: str = "subscription"
    claude_cli: str = "claude"
    anthropic_api_key: str | None = None
    claude_timeout_seconds: int = 300
    daily_call_cap: int = 200
    daily_token_cap: int = 2_000_000

    # PR Review
    github_token: str | None = None
    review_k: int = 5             # max related decisions sent to Claude
    review_min_score: float = 0.0  # vector score floor; tuned once real decisions exist
    duplicate_score: float = 0.97  # extractor skips a new decision this close to an existing one
