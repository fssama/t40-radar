"""跟踪 1E61 PCLA 障碍点，向前端输出已确认的实测或预测航迹。"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from itertools import product

TRACK_WINDOW = 0.35       # 新旧点共同建立簇的最长时间跨度，秒
CLUSTER_RADIUS_M = 0.50   # 三维邻域半径
MIN_PTS = 5               # 核心点邻居数，含自身
MAX_PTS = 400             # 过大的连续回波簇通常是背景
MATCH_RADIUS_M = 2.5
INIT_RANGE_M = 12.0
MAX_TRACKS = 24
CONFIRM_HITS = 3          # 同一航迹需要三次独立的新观测
OBSERVATION_S = 0.60     # 未收到下一帧时，暂时保留最近一次实测
TENTATIVE_S = 0.75
COAST_S = 1.25
SEQUENCE_RESET_S = 5.0
ALPHA = 0.55
BETA = 0.22

Position = tuple[float, float, float]
Point = tuple[float, float, float, float]  # body x/y/z in metres, normalized intensity
Cell = tuple[int, int, int]
NEIGHBOR_CELLS = tuple(product((-1, 0, 1), repeat=3))


@dataclass(frozen=True)
class Detection:
    position: Position
    strength: float


def cluster_detections(history: list[Point], fresh: list[Point]) -> list[Detection]:
    """用三维 DBSCAN 聚类，只用簇中的新回波计算匹配所用的质心。"""
    if not fresh:
        return []

    points = history + fresh
    fresh_start = len(history)
    locations = [p[:3] for p in points]
    cells = [tuple(math.floor(v / CLUSTER_RADIUS_M) for v in pos) for pos in locations]
    grid: dict[Cell, list[int]] = {}
    for i, cell in enumerate(cells):
        grid.setdefault(cell, []).append(i)

    # 已归入一个簇的点不再参与扩张扫描，仍参与核心点的邻居计数。
    remaining = {cell: set(indices) for cell, indices in grid.items()}
    grid_neighbors = {
        cell: [neighbor for dx, dy, dz in NEIGHBOR_CELLS
               if (neighbor := (cell[0] + dx, cell[1] + dy, cell[2] + dz)) in grid]
        for cell in grid
    }

    def is_core(i: int) -> bool:
        count = 0
        for cell in grid_neighbors[cells[i]]:
            for j in grid[cell]:
                if math.dist(locations[i], locations[j]) <= CLUSTER_RADIUS_M:
                    count += 1
                    if count == MIN_PTS:
                        return True
        return False

    detections = []
    for seed, seed_cell in enumerate(cells):
        if seed not in remaining[seed_cell] or not is_core(seed):
            continue
        remaining[seed_cell].remove(seed)
        stack = [seed]
        members = [seed]
        while stack:
            core = stack.pop()
            for cell in grid_neighbors[cells[core]]:
                pool = remaining[cell]
                neighbors = [j for j in pool
                             if math.dist(locations[core], locations[j]) <= CLUSTER_RADIUS_M]
                pool.difference_update(neighbors)
                members.extend(neighbors)
                stack.extend(j for j in neighbors if is_core(j))

        measured = [points[i] for i in members if i >= fresh_start]
        if not measured or len(members) > MAX_PTS:
            continue
        xs, ys, zs, strengths = zip(*measured)
        n = len(measured)
        detections.append(Detection((sum(xs) / n, sum(ys) / n, sum(zs) / n),
                                    sum(strengths) / n))
    return detections


def _minimum_assignment(cost: list[list[float]]) -> list[int]:
    """矩形代价矩阵的匈牙利算法。每行分配一列，列数至少等于行数。"""
    rows, cols = len(cost), len(cost[0])
    row_offset = [0.0] * (rows + 1)
    col_offset = [0.0] * (cols + 1)
    owner = [0] * (cols + 1)
    predecessor = [0] * (cols + 1)
    for row in range(1, rows + 1):
        owner[0] = row
        col = 0
        visited = [False] * (cols + 1)
        best = [math.inf] * (cols + 1)
        while True:
            visited[col] = True
            current_row = owner[col]
            delta = math.inf
            next_col = 0
            for j in range(1, cols + 1):
                if visited[j]:
                    continue
                adjusted = cost[current_row - 1][j - 1] - row_offset[current_row] - col_offset[j]
                if adjusted < best[j]:
                    best[j], predecessor[j] = adjusted, col
                if best[j] < delta:
                    delta, next_col = best[j], j
            for j in range(cols + 1):
                if visited[j]:
                    row_offset[owner[j]] += delta
                    col_offset[j] -= delta
                else:
                    best[j] -= delta
            col = next_col
            if owner[col] == 0:
                break
        while col:
            previous = predecessor[col]
            owner[col] = owner[previous]
            col = previous

    columns = [-1] * rows
    for col in range(1, cols + 1):
        if owner[col]:
            columns[owner[col] - 1] = col - 1
    return columns


def associate(predicted: list[Position], detections: list[Detection]) -> dict[int, int]:
    """门限内优先保留最多的航迹，再比较匹配的总距离。"""
    if not predicted or not detections:
        return {}
    distances = [[math.dist(position, det.position) for det in detections]
                 for position in predicted]
    eligible = [j for j in range(len(detections))
                if any(row[j] <= MATCH_RADIUS_M for row in distances)]
    if not eligible:
        return {}
    # 漏配代价超过全部合法距离的可能总和，故多匹配一条总会更优。
    miss = (len(predicted) + 1) * MATCH_RADIUS_M
    cost = [[row[j] if row[j] <= MATCH_RADIUS_M else 2 * miss for j in eligible]
            + [miss] * len(predicted) for row in distances]
    return {
        i: eligible[col] for i, col in enumerate(_minimum_assignment(cost))
        if 0 <= col < len(eligible) and distances[i][eligible[col]] <= MATCH_RADIUS_M
    }


@dataclass
class Track:
    id: int
    position: Position
    strength: float
    observed_at: float
    velocity: Position = (0.0, 0.0, 0.0)
    hits: int = 1
    age: int = 1
    confirmed: bool = False
    missed: bool = False

    def predict(self, timestamp: float) -> Position:
        elapsed = max(0.0, timestamp - self.observed_at)
        x, y, z = self.position
        vx, vy, vz = self.velocity
        return (x + vx * elapsed, y + vy * elapsed, z + vz * elapsed)

    def update(self, detection: Detection, timestamp: float):
        px, py, pz = self.predict(timestamp)
        mx, my, mz = detection.position
        rx, ry, rz = mx - px, my - py, mz - pz
        vx, vy, vz = self.velocity
        scale = BETA / max(0.02, timestamp - self.observed_at)
        self.position = (px + ALPHA * rx, py + ALPHA * ry, pz + ALPHA * rz)
        self.velocity = (vx + scale * rx, vy + scale * ry, vz + scale * rz)
        self.strength = detection.strength
        self.observed_at = timestamp
        self.hits += 1
        self.age += 1
        self.confirmed |= self.hits >= CONFIRM_HITS
        self.missed = False

    def miss(self):
        self.age += 1
        self.missed = True
        if not self.confirmed:
            self.hits = 0

    def expired(self, timestamp: float) -> bool:
        lifetime = COAST_S if self.confirmed else TENTATIVE_S
        return timestamp - self.observed_at >= lifetime

    def to_dict(self, timestamp: float) -> dict:
        elapsed = max(0.0, timestamp - self.observed_at)
        coasting = self.missed or elapsed >= OBSERVATION_S
        x, y, z = self.predict(timestamp) if coasting else self.position
        vx, vy, _ = self.velocity
        distance = math.hypot(x, y)
        return {
            "id": self.id,
            "x": round(x, 3), "y": round(y, 3), "z": round(z, 3),
            "vx": round(vx, 3), "vy": round(vy, 3),
            "range": round(distance, 2),
            "az": round(math.degrees(math.atan2(y, x)) % 360.0, 1) % 360.0,
            "el": round(math.degrees(math.atan2(z, distance)), 1),
            "speed": round(math.hypot(vx, vy), 2),
            "radial": round(-(vx * x + vy * y) / max(1e-6, distance), 2),
            "snr": round(self.strength, 3),
            "hits": self.hits,
            "age": self.age,
            "confirmed": self.confirmed,
            "status": "coasting" if coasting else "observed",
            "observation_age_s": round(elapsed, 3),
        }


class Tracker:
    def __init__(self):
        self.tracks: list[Track] = []
        self._next_id = 1
        self._last_observation: tuple[float, int] | None = None
        self._window: deque[tuple[float, list[Point]]] = deque()

    def observe(self, points: list[Point], timestamp: float, seq: int) -> list[dict]:
        """推进一次 PCLA 观测。同序号和乱序数据不更新航迹。"""
        previous = self._last_observation
        if previous is not None:
            last_at, last_seq = previous
            if timestamp - last_at < SEQUENCE_RESET_S:
                seq_delta = (seq - last_seq) & 0xFFFFFFFF
                if not 0 < seq_delta < 0x80000000:
                    return self.snapshot(timestamp)
            else:
                self._window.clear()
        self._last_observation = (timestamp, seq)
        while self._window and timestamp - self._window[0][0] > TRACK_WINDOW:
            self._window.popleft()
        history = [point for _, batch in self._window for point in batch] if points else []
        detections = cluster_detections(history, points)
        if points:
            self._window.append((timestamp, points))

        self.tracks = [track for track in self.tracks if not track.expired(timestamp)]
        matched = associate([track.predict(timestamp) for track in self.tracks], detections)
        for i, track in enumerate(self.tracks):
            if i in matched:
                track.update(detections[matched[i]], timestamp)
            else:
                track.miss()

        used = set(matched.values())
        candidates = sorted(
            (det for i, det in enumerate(detections) if i not in used),
            key=lambda det: math.hypot(*det.position[:2]),
        )
        for detection in candidates:
            if len(self.tracks) >= MAX_TRACKS or math.hypot(*detection.position[:2]) > INIT_RANGE_M:
                break
            self.tracks.append(Track(self._next_id, detection.position, detection.strength, timestamp))
            self._next_id += 1
        return self.snapshot(timestamp)

    def snapshot(self, timestamp: float) -> list[dict]:
        tracks = [track.to_dict(timestamp) for track in self.tracks
                  if track.confirmed and not track.expired(timestamp)]
        return sorted(tracks, key=lambda track: track["range"])
