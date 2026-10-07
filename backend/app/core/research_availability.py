"""Persistent administrator switch for starting AI research jobs."""

import os
from pathlib import Path


UNAVAILABLE_MESSAGE = "AI research is temporarily unavailable. Please wait a while and try again."


def _flag(db_path: str) -> Path:
    return Path(db_path).parent / "ai_research_disabled.flag"


def is_research_disabled(db_path: str) -> bool:
    return _flag(db_path).exists()


def set_research_disabled(db_path: str, disabled: bool) -> None:
    flag = _flag(db_path)
    if disabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        temporary = flag.with_name(f"{flag.name}.{os.getpid()}.tmp")
        temporary.write_text("Disabled by admin", encoding="utf-8")
        os.replace(temporary, flag)
    else:
        flag.unlink(missing_ok=True)
