"""Email module – discovery, cleaning, and validation."""

from app.email.email_cleaner import clean_emails
from app.email.email_discovery import EmailDiscovery
from app.email.email_validator import is_valid_email

__all__ = ["EmailDiscovery", "clean_emails", "is_valid_email"]
