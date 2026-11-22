"""
Configuration management for FlareSolverr.
Loads from config.yaml or environment variables (env takes priority).
Singleton pattern - load once, access anywhere.
"""

import os
import yaml
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass, field

# Sentinel error tracking
try:
    from .core.sentinel_errors import ErrCode, emit
except ImportError:
    def emit(*a: object, **kw: object) -> None: pass


@dataclass
class SolverTimeouts:
    """Timeout configuration per solver type (in seconds)."""
    recaptcha_v2: float = 120.0
    recaptcha_v3: float = 60.0
    hcaptcha: float = 120.0
    funcaptcha: float = 120.0
    geetest: float = 120.0
    turnstile: float = 60.0
    image_captcha: float = 30.0
    text_captcha: float = 30.0


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""
    enabled: bool = False
    requests_per_minute: int = 60
    burst_size: int = 10


@dataclass
class SolverConfig:
    """Configuration for an individual solver."""
    enabled: bool = True
    timeout: float = 120.0
    max_retries: int = 3
    cdp_port: int = 9234
    headless: bool = True
    proxy: Optional[str] = None
    model_cache_dir: str = "./model_cache"
    # Extra fields passthrough
    extra: Dict[str, Any] = field(default_factory=dict)

    def dict(self) -> Dict[str, Any]:
        """Convert to dictionary for solver constructors."""
        result = {
            'enabled': self.enabled,
            'timeout': self.timeout,
            'max_retries': self.max_retries,
            'cdp_port': self.cdp_port,
            'headless': self.headless,
            'proxy': self.proxy,
            'model_cache_dir': self.model_cache_dir,
        }
        result.update(self.extra)
        return result


@dataclass
class Config:
    """Main configuration class."""
    # Server settings
    port: int = 8000
    host: str = "127.0.0.1"
    workers: int = 1
    
    # Security
    api_key: str = ""
    
    # Solvers
    enabled_solvers: List[str] = field(default_factory=lambda: [
        "recaptcha_v2", "recaptcha_v2_audio", "recaptcha_v2_grid", "recaptcha_v3", "hcaptcha", 
        "cloudflare", "funcaptcha", "geetest", "turnstile", "image_captcha", "text_captcha",
        "tls", "stealth", "stealth_browser", "pydoll", "drissionpage", "headlessx",
        "curl-impersonate", "camoufox", "datadome",
    ])
    
    # Timeouts per solver
    timeouts: SolverTimeouts = field(default_factory=SolverTimeouts)
    
    # Task queue settings
    task_ttl_seconds: int = 300
    cleanup_interval_seconds: int = 60
    max_concurrent_tasks: int = 10
    
    # External services
    flaresolverr_url: Optional[str] = "http://localhost:8191/v1"
    flaresolverr_container: str = "flaresolverr"
    proxy: Optional[str] = None
    
    # Logging
    log_level: str = "INFO"
    log_dir: str = "logs"
    
    # Model cache
    model_cache_dir: str = "./model_cache"
    
    # Rate limiting
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    
    # 2Captcha compatibility
    twocaptcha_compatible: bool = True
    
    # Per-solver config overrides
    solver_configs: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def get_solver_config(self, solver_type: str) -> SolverConfig:
        """Get configuration for a specific solver."""
        # Start with defaults
        config = SolverConfig(
            enabled=solver_type in self.enabled_solvers,
            timeout=getattr(self.timeouts, solver_type, 120.0),
            proxy=self.proxy,
            model_cache_dir=self.model_cache_dir,
        )
        # Apply per-solver overrides
        overrides = self.solver_configs.get(solver_type, {})
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)
            else:
                config.extra[key] = value
        if solver_type in {"cloudflare", "flaresolverr"} and self.flaresolverr_url:
            config.extra.setdefault("base_url", self.flaresolverr_url)
        return config


class ConfigManager:
    """Singleton configuration manager."""
    _instance: Optional['ConfigManager'] = None
    _config: Optional[Config] = None
    _initialized: bool = False
    
    def __new__(cls) -> 'ConfigManager':
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        if not self._initialized:
            self._load_config()
            self._initialized = True
    
    def _load_config(self) -> None:
        """Load configuration from file and environment."""
        self._config = Config()
        
        # First load from config.yaml if exists
        self._load_from_yaml()
        
        # Environment variables override file config
        self._load_from_env()
        
        # Validate required settings
        self._validate()
    
    def _load_from_yaml(self) -> None:
        """Load configuration from the first matching config.yaml path."""
        config_paths = [
            Path("config.yaml"),
            Path("config.yml"),
            Path.home() / ".flaresolverr" / "config.yaml",
            Path("/etc/flaresolverr/config.yaml"),
        ]
        for config_path in config_paths:
            if config_path.exists():
                self._load_one_yaml_file(config_path)
                return

    def _load_one_yaml_file(self, config_path: Path) -> None:
        """Read + apply a single YAML config file (early-return path)."""
        try:
            with open(config_path, 'r', encoding='utf-8-sig', errors='replace') as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            emit(ErrCode.CONFIG_UNREADABLE, f"Config load failed: {config_path}: {e}")
            raise ConfigError(f"Failed to load config from {config_path}: {e}") from e
        self._apply_yaml_data(data)

    # YAML-key -> (config attribute, coercer) for flat scalar settings.
    _SCALAR_YAML_FIELDS: Dict[str, Any] = {
        'port': ('port', int),
        'host': ('host', str),
        'api_key': ('api_key', str),
        'enabled_solvers': ('enabled_solvers', list),
        'flaresolverr_url': ('flaresolverr_url', lambda v: v),
        'flaresolverr_container': ('flaresolverr_container', str),
        'proxy': ('proxy', lambda v: v),
        'log_level': ('log_level', lambda v: str(v).upper()),
        'log_dir': ('log_dir', str),
        'model_cache_dir': ('model_cache_dir', str),
        'twocaptcha_compatible': ('twocaptcha_compatible', bool),
    }

    def _apply_yaml_data(self, data: Dict[str, Any]) -> None:
        """Apply YAML data to config (delegates by section)."""
        self._apply_scalar_fields(data)
        self._apply_timeouts_section(data.get('timeouts'))
        self._apply_rate_limit_section(data.get('rate_limit'))

    def _apply_scalar_fields(self, data: Dict[str, Any]) -> None:
        """Apply table-driven scalar config keys."""
        for key, (attr, coerce) in self._SCALAR_YAML_FIELDS.items():
            if key in data:
                setattr(self._config, attr, coerce(data[key]))

    def _apply_timeouts_section(self, timeouts: Any) -> None:
        """Apply per-solver timeout overrides from the `timeouts` mapping."""
        if not isinstance(timeouts, dict):
            return
        for key, value in timeouts.items():
            if hasattr(self._config.timeouts, key):
                setattr(self._config.timeouts, key, float(value))

    def _apply_rate_limit_section(self, rate_limit: Any) -> None:
        """Apply rate-limit settings from the `rate_limit` mapping."""
        if not isinstance(rate_limit, dict):
            return
        if 'enabled' in rate_limit:
            self._config.rate_limit.enabled = bool(rate_limit['enabled'])
        if 'requests_per_minute' in rate_limit:
            self._config.rate_limit.requests_per_minute = int(rate_limit['requests_per_minute'])
        if 'burst_size' in rate_limit:
            self._config.rate_limit.burst_size = int(rate_limit['burst_size'])
    
    def _load_from_env(self) -> None:
        """Load configuration from environment variables."""
        env_mappings = {
            'FLARESOLVERR_PORT': ('port', int),
            'FLARESOLVERR_HOST': ('host', str),
            'FLARESOLVERR_API_KEY': ('api_key', str),
            'FLARESOLVERR_URL': ('flaresolverr_url', str),
            'FLARESOLVERR_CONTAINER': ('flaresolverr_container', str),
            'FLARESOLVERR_PROXY': ('proxy', str),
            'FLARESOLVERR_LOG_LEVEL': ('log_level', str),
            'FLARESOLVERR_LOG_DIR': ('log_dir', str),
            'FLARESOLVERR_MODEL_CACHE_DIR': ('model_cache_dir', str),
            'FLARESOLVERR_TWOCAPTCHA_COMPATIBLE': ('twocaptcha_compatible', lambda x: x.lower() == 'true'),
        }
        
        for env_var, (config_key, converter) in env_mappings.items():
            value = os.getenv(env_var)
            if value is not None:
                setattr(self._config, config_key, converter(value))
        
        # Enabled solvers (comma-separated)
        if 'FLARESOLVERR_ENABLED_SOLVERS' in os.environ:
            solvers = os.environ['FLARESOLVERR_ENABLED_SOLVERS'].split(',')
            self._config.enabled_solvers = [s.strip() for s in solvers if s.strip()]
        
        # Timeouts
        timeout_prefix = 'FLARESOLVERR_TIMEOUT_'
        for key in vars(self._config.timeouts):
            env_var = timeout_prefix + key.upper()
            value = os.getenv(env_var)
            if value is not None:
                setattr(self._config.timeouts, key, float(value))
        
        # Rate limiting
        if 'FLARESOLVERR_RATE_LIMIT_ENABLED' in os.environ:
            self._config.rate_limit.enabled = os.environ['FLARESOLVERR_RATE_LIMIT_ENABLED'].lower() == 'true'
        if 'FLARESOLVERR_RATE_LIMIT_RPM' in os.environ:
            self._config.rate_limit.requests_per_minute = int(os.environ['FLARESOLVERR_RATE_LIMIT_RPM'])
        if 'FLARESOLVERR_RATE_LIMIT_BURST' in os.environ:
            self._config.rate_limit.burst_size = int(os.environ['FLARESOLVERR_RATE_LIMIT_BURST'])
    
    def _validate(self) -> None:
        """Validate required configuration settings."""
        errors = []
        
        # API key is optional -- if not set, all requests are allowed
        # (verify_api_key in main.py returns True when api_key is empty)
        
        if not self._config.enabled_solvers:
            errors.append("At least one solver must be enabled in enabled_solvers")
        
        if self._config.port < 1 or self._config.port > 65535:
            errors.append(f"Invalid port: {self._config.port}. Must be 1-65535")
        
        if errors:
            raise ConfigError("Configuration validation failed:\n" + "\n".join(f"  - {e}" for e in errors))
    
    @property
    def config(self) -> Config:
        """Get the loaded configuration."""
        return self._config
    
    def reload(self) -> None:
        """Reload configuration from disk."""
        self._load_config()


class ConfigError(Exception):
    """Configuration error exception."""
    pass


# Global accessor function
def get_config() -> Config:
    """Get the global configuration instance."""
    return ConfigManager().config


def reload_config() -> None:
    """Reload the global configuration."""
    ConfigManager().reload()
