"""Small, durable worker metadata independent of transport/config rollback."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile

log = logging.getLogger(__name__)
MAX_DESCRIPTION = 280


def validate_description(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError('Description must be text.')
    value = ' '.join(value.split())
    if len(value) > MAX_DESCRIPTION:
        raise ValueError(f'Description must be at most {MAX_DESCRIPTION} characters.')
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Description must not contain control characters.')
    return value


class WorkerMetadata:
    def __init__(self, path: Path):
        self.path = path
        self.description = ''
        try:
            self.description = validate_description(json.loads(path.read_text(encoding='utf-8')).get('description', ''))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            log.warning('Could not load worker description from %s', path)

    def set_description(self, value: str) -> str:
        value = validate_description(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.metadata-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump({'description': value}, stream, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        self.description = value
        return value
