"""A procedural, deterministic cell culture in stage coordinates.

Cells are generated per 400 µm tile from a seed derived from (seed, tile x, tile y), so any
field of view anywhere on the stage is reproducible without storing millions of cells.
"""

from __future__ import annotations

import math
import threading
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

TILE_UM = 400.0
_TILE_OFFSET = 1 << 20  # keeps RNG seed words non-negative for tiles left of / above the origin


@dataclass(frozen=True)
class CellBatch:
    cx: np.ndarray  # µm
    cy: np.ndarray
    rx: np.ndarray  # semi-axes, µm
    ry: np.ndarray
    cos: np.ndarray
    sin: np.ndarray
    nuc_scale: np.ndarray  # nucleus size relative to the cell
    nuc_dx: np.ndarray  # nucleus offset along the cell's own axes, µm
    nuc_dy: np.ndarray
    dapi: np.ndarray  # label abundance per cell (0 = not expressed)
    gfp: np.ndarray
    rfp: np.ndarray
    cy5: np.ndarray
    lobe_k: np.ndarray  # outline irregularity: number of lobes ...
    lobe_a: np.ndarray  # ... their amplitude (0 = ellipse) ...
    lobe_phase: np.ndarray  # ... and orientation

    def __len__(self) -> int:
        return int(self.cx.size)

    def select(self, mask: np.ndarray) -> CellBatch:
        return CellBatch(**{k: getattr(self, k)[mask] for k in self.__dataclass_fields__})

    @staticmethod
    def concat(batches: list[CellBatch]) -> CellBatch:
        if not batches:
            empty = np.zeros(0)
            return CellBatch(*([empty] * len(CellBatch.__dataclass_fields__)))
        return CellBatch(**{k: np.concatenate([getattr(b, k) for b in batches])
                            for k in CellBatch.__dataclass_fields__})


class Specimen:
    MAX_RADIUS_UM = 24.0

    def __init__(self, seed: int, density_per_mm2: float, max_tiles: int = 512):
        self.seed = int(seed)
        self.density_per_um2 = float(density_per_mm2) / 1e6
        self._tiles: OrderedDict[tuple[int, int], CellBatch] = OrderedDict()
        self._max_tiles = max_tiles
        self._lock = threading.Lock()

    def _tile(self, tx: int, ty: int) -> CellBatch:
        key = (tx, ty)
        with self._lock:
            hit = self._tiles.get(key)
            if hit is not None:
                self._tiles.move_to_end(key)
                return hit
        rng = np.random.default_rng([self.seed & 0xFFFFFFFF, tx + _TILE_OFFSET, ty + _TILE_OFFSET])
        n = int(rng.poisson(self.density_per_um2 * TILE_UM * TILE_UM))
        r = rng.uniform(9.0, 20.0, n)
        rounded = rng.random(n) < 0.08  # mitotic / rounded-up cells: small, round, bright
        r = np.where(rounded, r * 0.45, r)
        aspect = np.where(rounded, rng.uniform(0.9, 1.0, n), rng.uniform(0.3, 0.85, n))
        ang = rng.uniform(0, math.pi, n)
        batch = CellBatch(
            cx=(tx + rng.random(n)) * TILE_UM,
            cy=(ty + rng.random(n)) * TILE_UM,
            rx=r, ry=r * aspect, cos=np.cos(ang), sin=np.sin(ang),
            nuc_scale=np.where(rounded, 0.7, rng.uniform(0.28, 0.42, n)),
            nuc_dx=rng.normal(0, 0.12, n) * r, nuc_dy=rng.normal(0, 0.08, n) * r,
            dapi=rng.uniform(0.75, 1.25, n) * np.where(rounded, 1.6, 1.0),
            gfp=(rng.random(n) < 0.65) * rng.uniform(0.45, 1.2, n),
            rfp=(rng.random(n) < 0.35) * rng.uniform(0.4, 1.1, n),
            cy5=(rng.random(n) < 0.22) * rng.uniform(0.35, 1.0, n),
            lobe_k=rng.integers(2, 4, n).astype(float),
            lobe_a=np.where(rounded, 0.0, rng.uniform(0.04, 0.2, n)),
            lobe_phase=rng.uniform(0, 2 * math.pi, n),
        )
        with self._lock:
            self._tiles[key] = batch
            while len(self._tiles) > self._max_tiles:
                self._tiles.popitem(last=False)
        return batch

    def cells_in(self, x0: float, x1: float, y0: float, y1: float) -> CellBatch:
        m = self.MAX_RADIUS_UM
        tx0, tx1 = math.floor((x0 - m) / TILE_UM), math.floor((x1 + m) / TILE_UM)
        ty0, ty1 = math.floor((y0 - m) / TILE_UM), math.floor((y1 + m) / TILE_UM)
        batch = CellBatch.concat([self._tile(tx, ty) for tx in range(tx0, tx1 + 1)
                                  for ty in range(ty0, ty1 + 1)])
        if len(batch) == 0:
            return batch
        mask = ((batch.cx + m >= x0) & (batch.cx - m <= x1) & (batch.cy + m >= y0)
                & (batch.cy - m <= y1))
        return batch.select(mask)
