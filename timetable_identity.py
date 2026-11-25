"""Presentation-only dataset identity; never alters university scheduling rules."""
from __future__ import annotations
from pathlib import Path


def safe_workbook_label(value: str | None) -> str:
    """Display a workbook's leaf filename, not a temporary/full computer path."""
    label = str(value or '').replace('\\', '/').split('/')[-1].strip()
    return label or 'Workbook name unavailable'


def suggested_university_name(filename: str | None) -> str:
    """A convenience label, not proof of the institution in an uploaded Excel.

    The uploader can override this. Unknown institutions are left unspecified.
    """
    name = safe_workbook_label(filename).casefold()
    if 'northcap' in name or 'north_cap' in name:
        return 'The NorthCap University'
    if 'teerthanker' in name or name.startswith('tmu_') or '_tmu_' in name:
        return 'Teerthanker Mahaveer University (TMU)'
    return ''
