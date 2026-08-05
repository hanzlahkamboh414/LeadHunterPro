"""Tests for crawler configuration."""

from __future__ import annotations

import pytest

from app.crawlers.config import CrawlerConfig


class TestCrawlerConfig:
    """Test CrawlerConfig dataclass."""

    def test_default_config_creates_instance(self):
        """CrawlerConfig can be instantiated with defaults."""
        config = CrawlerConfig()
        assert config.default_timeout == 30
        assert config.max_retries == 3
        assert config.retry_backoff_base == 2.0
        assert config.retry_backoff_max == 30.0
        assert config.rate_limit_per_host_seconds == 0.5
        assert config.rate_limit_global_seconds == 0.1
        assert config.respect_robots_txt is True
        assert config.robots_cache_ttl == 3600
        assert config.session_timeout == 35

    def test_default_user_agent_pool_has_entries(self):
        """Default user agent pool contains at least one agent."""
        config = CrawlerConfig()
        assert len(config.user_agent_pool) >= 1
        assert isinstance(config.user_agent_pool[0], str)
        assert "Mozilla" in config.user_agent_pool[0]

    def test_custom_config_override(self):
        """CrawlerConfig accepts custom values."""
        config = CrawlerConfig(
            default_timeout=60,
            max_retries=5,
            rate_limit_per_host_seconds=1.0,
            respect_robots_txt=False,
        )
        assert config.default_timeout == 60
        assert config.max_retries == 5
        assert config.rate_limit_per_host_seconds == 1.0
        assert config.respect_robots_txt is False

    def test_config_is_frozen(self):
        """CrawlerConfig is immutable (frozen dataclass)."""
        config = CrawlerConfig()
        with pytest.raises(AttributeError):
            config.default_timeout = 60  # type: ignore[misc]

    def test_config_equality(self):
        """Two configs with same values are equal."""
        config1 = CrawlerConfig()
        config2 = CrawlerConfig()
        assert config1 == config2

    def test_config_inequality(self):
        """Two configs with different values are not equal."""
        config1 = CrawlerConfig(default_timeout=30)
        config2 = CrawlerConfig(default_timeout=60)
        assert config1 != config2
