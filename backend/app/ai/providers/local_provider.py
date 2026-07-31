"""Local (no-op) AI provider used when no external provider is configured."""

import logging

logger = logging.getLogger(__name__)


class LocalProvider:
    """Stub provider that echoes the prompt — useful for development."""

    def generate(self, prompt: str) -> str:
        """Return the prompt prefixed with a local-provider marker.

        Args:
            prompt: The input prompt.

        Returns:
            Echoed prompt string.
        """
        logger.debug("LocalProvider echoing prompt: %s", prompt[:80])
        return f"[LOCAL AI]\n\n{prompt}"
