"""ServeCompact: an OpenAI-compatible compaction proxy (deterministic v1).

Public surface:
  compact_and_verify(messages) -> (messages_to_send, compacted, report)
  Meter: per-request and aggregate token/fallback accounting (estimates
  labelled as such; see tokens.py).
"""

from .pipeline import compact_and_verify
from .meter import Meter

__all__ = ["compact_and_verify", "Meter"]
__version__ = "0.1.0"
