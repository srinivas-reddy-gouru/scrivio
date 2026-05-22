"""Content-hashed stage cache for the article pipeline.

Persists expensive stage outputs (brief, search, planning, verification,
drafting) to disk so that reruns with the same inputs reuse the results
instead of re-spending API quota.

Cache key = sha256(stage_name + serialized inputs). Files live under
`.cache/article_pipeline/` by default.

Disable globally with `ARTICLE_CACHE=0`. Bump `_CACHE_VERSION` to
invalidate all entries when prompts or schemas change in ways that
shouldn't reuse old outputs.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel


# Bump this whenever prompt content or schema shapes change in a way that
# should invalidate all previously-cached outputs.
_CACHE_VERSION = "v1"

_DEFAULT_CACHE_DIR = Path(".cache") / "article_pipeline"


def _normalize_part(part: Any) -> str:
    """Convert a cache-key part into a stable string for hashing."""
    if part is None:
        return "<none>"
    if isinstance(part, BaseModel):
        return part.model_dump_json()
    if isinstance(part, (list, tuple)):
        return json.dumps(
            [_normalize_part(p) for p in part], sort_keys=True
        )
    if isinstance(part, dict):
        return json.dumps(part, sort_keys=True, default=str)
    if isinstance(part, str):
        return part
    return json.dumps(part, sort_keys=True, default=str)


class StageCache:
    """File-based cache keyed by hash of (stage_name, inputs).

    Usage:
        cache = StageCache()
        cached = cache.get("brief", request.topic, request.audience_role)
        if cached is None:
            brief = await run_brief(...)
            cache.set("brief", brief, request.topic, request.audience_role)
        else:
            brief = StoryBrief.model_validate(cached)
    """

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self.enabled = os.environ.get("ARTICLE_CACHE", "1") != "0"
        if self.enabled:
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logging.warning("Could not create cache dir %s: %s", self.cache_dir, exc)
                self.enabled = False

    def _key(self, stage: str, *parts: Any) -> str:
        h = hashlib.sha256()
        h.update(f"{_CACHE_VERSION}\0{stage}\0".encode())
        for part in parts:
            h.update(_normalize_part(part).encode())
            h.update(b"\0")
        return f"{stage}_{h.hexdigest()[:20]}"

    def _path(self, stage: str, *parts: Any) -> Path:
        return self.cache_dir / f"{self._key(stage, *parts)}.json"

    def get(self, stage: str, *parts: Any) -> Any | None:
        """Return the cached JSON value, or None on miss / error / disabled."""
        if not self.enabled:
            return None
        path = self._path(stage, *parts)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            logging.info("Cache HIT  %s", path.name)
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning("Cache read failed for %s: %s", path.name, exc)
            return None

    def set(self, stage: str, value: Any, *parts: Any) -> None:
        """Persist `value` to disk under the key derived from (stage, parts).

        Pydantic models and lists of Pydantic models are serialized via
        `.model_dump(mode='json')`. Other values must be JSON-serializable.
        """
        if not self.enabled:
            return
        path = self._path(stage, *parts)
        try:
            serialized = _serialize_for_cache(value)
            path.write_text(json.dumps(serialized, default=str), encoding="utf-8")
            logging.info("Cache WRITE %s", path.name)
        except (TypeError, OSError) as exc:
            logging.warning("Cache write failed for %s: %s", path.name, exc)


def _serialize_for_cache(value: Any) -> Any:
    """Recursively serialize Pydantic models for JSON storage."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_serialize_for_cache(item) for item in value]
    if isinstance(value, tuple):
        return [_serialize_for_cache(item) for item in value]
    if isinstance(value, dict):
        return {k: _serialize_for_cache(v) for k, v in value.items()}
    return value
