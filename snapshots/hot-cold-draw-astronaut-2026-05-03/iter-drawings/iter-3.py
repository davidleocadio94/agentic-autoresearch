"""Iter 3: scene with figure, pole+rectangle, disc, curved ground, circular pits, dots."""

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
                  |                     '.          .'
                  |==========              '------'
                  |==========
                  |==========
                  |
                  |
                  |
              O   |
             /|\__|                                *
             / \                          .
                                                  *
       _____           __________
      /     \_________/          \________
     /                                    \____           ____
 ___/    .---.            .---.                \         /    \___
        (     )          (     )                \_______/         \
         '---'            '---'        .---.                 .---.
                                      (     )               (     )
                                       '---'                 '---'
"""


def draw() -> str:
    return _SCENE
