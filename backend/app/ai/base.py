"""Abstract base class for AI providers."""

from abc import ABC, abstractmethod


class BaseAIProvider(ABC):
    """Base class all AI provider implementations must extend."""

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """Generate a text response from the given prompt.

        Args:
            prompt: The input prompt string.

        Returns:
            The generated text response.
        """
        ...
