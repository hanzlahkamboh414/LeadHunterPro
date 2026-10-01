"""Named phone-only accounts; all other regular accounts use email tools."""

from fastapi import Depends, HTTPException

from app.auth.dependencies import get_current_user
from app.auth.models import User


PHONE_ONLY_USERNAMES = frozenset({"king", "david", "smwqureshi", "quagmire"})


def is_phone_only(user: User) -> bool:
    return not user.is_admin and user.username.casefold() in PHONE_ONLY_USERNAMES


def require_phone_access(user: User = Depends(get_current_user)) -> User:
    if user.is_admin or is_phone_only(user):
        return user
    raise HTTPException(status_code=403, detail="Phones access is not enabled for this account")


def require_email_access(user: User = Depends(get_current_user)) -> User:
    if not is_phone_only(user):
        return user
    raise HTTPException(status_code=403, detail="This account has Phones access only")
