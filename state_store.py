"""Persistence of script playback progress.

Progress is keyed by (session_name, script_name) and stored as a small
JSON file, so restarting the process or losing the connection never
replays or skips turns.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)


@dataclass
class ScriptState:
    turn_index: int = 0
    # Index of the next not-yet-sent message within turns[turn_index].
    # Lets playback resume mid-turn if the process crashes after sending
    # only some of a multi-message turn.
    message_index: int = 0


class StateStore:
    """One JSON file per (session_name, script_name) pair under state_dir."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_name: str, script_name: str) -> Path:
        safe_session = _sanitize(session_name)
        safe_script = _sanitize(script_name)
        return self.state_dir / f"{safe_session}__{safe_script}.json"

    def load(self, session_name: str, script_name: str) -> ScriptState:
        path = self._path(session_name, script_name)
        if not path.exists():
            return ScriptState()
        try:
            data: Dict = json.loads(path.read_text(encoding="utf-8"))
            return ScriptState(
                turn_index=int(data.get("turn_index", 0)),
                message_index=int(data.get("message_index", 0)),
            )
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.warning("Failed to read state file %s (%s); starting from turn 0", path, exc)
            return ScriptState()

    def save(self, session_name: str, script_name: str, state: ScriptState) -> None:
        path = self._path(session_name, script_name)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(asdict(state)), encoding="utf-8")
        tmp_path.replace(path)  # atomic on POSIX
        logger.debug("Saved state for (%s, %s): %s", session_name, script_name, state)

    def reset(self, session_name: str, script_name: str) -> None:
        self.save(session_name, script_name, ScriptState(turn_index=0))
        logger.info("Reset progress for (%s, %s) to turn 0", session_name, script_name)


def _sanitize(name: str) -> str:
    """Keep filenames filesystem-safe."""
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in name)
