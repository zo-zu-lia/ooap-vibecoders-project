"""Environment-only application configuration. No credentials are committed."""
import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv('DATABASE_URL', 'postgresql://fileflow_app@postgres/fileflow')
    app_origin: str = os.getenv('APP_ORIGIN', 'http://localhost:8080').rstrip('/')
    oidc_issuer: str = os.getenv('OIDC_ISSUER', 'http://localhost:8080/dex').rstrip('/')
    oidc_internal_url: str = os.getenv('OIDC_INTERNAL_URL', 'http://dex:5556/dex').rstrip('/')
    oidc_client_id: str = os.getenv('OIDC_CLIENT_ID', 'fileflow')
    oidc_client_secret: str = os.getenv('OIDC_CLIENT_SECRET', '')
    app_secret: str = os.getenv('APP_SECRET', '')
    cookie_secure: bool = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
    seed_demo: bool = os.getenv('LOCAL_DEMO', 'false').lower() == 'true'
    rate_limit: int = int(os.getenv('RATE_LIMIT_PER_MINUTE', '120'))

    @property
    def cookie_name(self):
        return '__Host-fileflow_session' if self.cookie_secure else 'fileflow_session'

settings = Settings()
