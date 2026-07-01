"""Apple Mail flag-color mapping. `flagIndex` 0-6 are Mail's seven colored
flags; -1 means unflagged. The order below is Mail's standard Flag-submenu
order — index 0=red confirmed empirically (a real rule's markFlagIndex=0
paired with colorMessage="red"), index 3=green confirmed by a live
set/cross-process-read round-trip against a real message.
"""

from __future__ import annotations

# Tuple index == flagIndex; both write (name->index) and read (index->name)
# sides import this so there is one source of truth for the mapping.
FLAG_COLOR_NAMES: tuple[str, ...] = (
    "red",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "gray",
)

_NAME_TO_INDEX = {name: i for i, name in enumerate(FLAG_COLOR_NAMES)}


def color_to_index(name: str) -> int:
    key = name.strip().lower()
    if key not in _NAME_TO_INDEX:
        raise ValueError(
            f"unknown flag color {name!r}; expected one of {', '.join(FLAG_COLOR_NAMES)}"
        )
    return _NAME_TO_INDEX[key]


def index_to_color(index: int | None) -> str | None:
    """Map a stored flagIndex back to a color name. Out-of-range / None / -1
    (unflagged) return None rather than raising — the index column is a
    best-effort supplementary read (see Apple-Mail-on-disk-format)."""
    if index is None or index < 0 or index >= len(FLAG_COLOR_NAMES):
        return None
    return FLAG_COLOR_NAMES[index]
