"""How much any one answer may carry.

An agent's context is small and expensive, and a tool that returns a million
cell ids fills it with nothing the agent can use. Every capability bounds its
output with these, and says `truncated: true` when it did -- never silently.
"""

#: Characters in one serialized tool result.
MAX_TOOL_CHARS = 60_000
#: Items in one list (projects, regions, markers).
MAX_LIST = 200
#: Cell ids in one answer.
MAX_IDS = 1_000
#: Vertices of one region's geometry.
MAX_VERTICES = 2_000
#: Histogram bins.
MAX_BINS = 50
#: Pixels in one rendered image, and the default.
MAX_OUTPUT_PIXELS = 2048 * 2048
DEFAULT_OUTPUT_SIDE = 1024
MAX_OUTPUT_SIDE = 2048
#: Channels composited into one render.
MAX_CHANNELS = 6
#: Fields sampled for one gate check, and per class.
MAX_FIELDS = 12
MAX_PER_CLASS = 4
#: Borderline cells listed per field.
MAX_BORDERLINE_ROWS = 50
#: Width of one validation panel.
MAX_PANEL_WIDTH = 1600
#: Bytes of one inline image.
MAX_INLINE_BYTES = 8 * 1024 * 1024
#: Source pixels one agent render may read (a quarter of Figure Builder's
#: budget: an agent render is evidence, not a publication figure).
MAX_SOURCE_PIXELS_AGENT = 30_000_000
#: The artifact store's bounds.
ARTIFACT_BUDGET_BYTES = 512 * 1024 * 1024
ARTIFACT_MAX_AGE_DAYS = 14
#: Cells scored when sampling fields; a larger table is subsampled (seeded).
SAMPLING_CELL_CAP = 250_000


def bounded(items, limit=MAX_LIST):
    """(list, truncated) -- at most `limit` items."""
    items = list(items)
    return items[:limit], len(items) > limit
