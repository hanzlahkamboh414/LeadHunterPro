"""Gateway that routes AI requests to the configured provider."""

import logging

from app.ai.manager import AIManager

logger = logging.getLogger(__name__)


class AIGateway:
    """Route prompts to the active AI provider."""

    def __init__(self) -> None:
        self._manager = AIManager()

    def ask(self, prompt: str) -> str:
        """Send *prompt* to the AI provider and return the response.

        Args:
            prompt: The user prompt.

        Returns:
            The provider's text response.
        """
        return self._manager.generate(prompt)
