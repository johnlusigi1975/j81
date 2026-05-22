from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import RawDocument, SourcePlatform


class SourceAdapter(ABC):
    """A pluggable research source. Each platform implements `search`,
    returning normalized RawDocuments. Failures should raise; the pipeline
    isolates per-source errors so one bad source never kills the run.
    """

    platform: SourcePlatform

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[RawDocument]:
        """Find content matching `query` and return up to `max_results` docs."""
        raise NotImplementedError
