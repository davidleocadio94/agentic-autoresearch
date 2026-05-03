"""ASCII drawing for the hot-and-cold target game.

Iter 1 hypothesis: broad, legible scene with multiple distinct anchors
(sky+sun, horizon, sailboat, water) so the judge's hint can point at
which region is hot or cold.
"""

from __future__ import annotations


def draw() -> str:
    scene = r"""
                                  .  *  .         .       *
                          *    .       .   *    .      .
                     .              *           .    *
                          .    *         .      .         *
                                            .
                            _____
                          /       \
                         |  *   *  |        .       *
                          \  ___  /     .
                            \___/                  .
                       .                                  .
   _________________________________________________________________
                       |\
                       | \
                       |  \
                       |   \
                       |____\
                       |    /
                       |   /
                       |  /
                       | /
                       |/
                  _____|______
                 /            \
                /______________\
   ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
   ~~~~  ~~~~~~  ~~~~~  ~~~~~~~  ~~~~~~~  ~~~~~~  ~~~~~  ~~~~~~  ~~~
   ~~~~~~~  ~~~~~~~  ~~~~~~~  ~~~~  ~~~~~~~  ~~~~~~~  ~~~~~~~  ~~~~~
   ~~~  ~~~~~~~  ~~~~~~  ~~~~~~~  ~~~~~~~  ~~~~~~  ~~~~~~~  ~~~~  ~~
   ~~~~~~~  ~~~~  ~~~~~~~  ~~~~~~~  ~~~~~~~  ~~~~~~~  ~~~~  ~~~~~~~~
"""
    return scene
