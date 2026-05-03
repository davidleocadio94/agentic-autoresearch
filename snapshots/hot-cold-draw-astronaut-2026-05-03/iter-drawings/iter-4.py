"""Iter 4: scene refinements — bigger head circle, feet on ground curve, striped flag."""

from __future__ import annotations


_SCENE = r"""
    .      *       .         *        .      *      .
         *               .        *           .         *
    .         *       .       *        .          *
                                          .--------.
                                        .'          '.
       *        .          *           /    .----.    \
                                      |    /      \    |
   .       *           .              |    \------/    |
                                       \              /
                                        '.          .'
                  |                       '--------'
                  |//////////
                  |//////////
                  |//////////
                  |//////////
                ( O )
                 /|\                                  *
                / | \                          .
       _________/_\_____________                      *
      /                         \________      __________
 ____/    .---.       .---.              \____/          \___
         (     )     (     )      .---.         .---.
          '---'       '---'      (     )       (     )
                                  '---'         '---'
"""


def draw() -> str:
    return _SCENE
