"""Explicit absence requirements shared by otherwise independent contracts."""

from enum import Enum


class Absent(Enum):
    """An explicit absence requirement, distinct from an unchecked option."""

    ABSENT = "absent"


ABSENT = Absent.ABSENT
