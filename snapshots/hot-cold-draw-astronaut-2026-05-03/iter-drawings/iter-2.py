"""Iter 2 probe: 4 distinct elements in canonical positions.

Top  -> sun (sky element)
Bottom -> ground line (ground element)
Center -> stick figure person (central figure)
Right  -> tree (side feature)

Goal: give the judge enough discrete, easy-to-name objects that the
truncated hint must enumerate which are warm and which are cold.
"""

from __future__ import annotations


_SCENE = r"""
        \ | /
       -- O --
        / | \




                            O
                           /|\               &&&
                           / \              &&&&&
                                             |||
                                             |||
__________________________________________________
"""


def draw() -> str:
    return _SCENE
