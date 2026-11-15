"""点云目标跟踪：聚类 → 关联 → α-β 滤波 → 航迹管理。

纯标准库实现（不依赖 numpy），单帧 2000 点约 10–20 ms，10 Hz 余量充足。

数据流：

    points[]  (az, r, z, snr, subtype)
        ↓ 极坐标转笛卡尔
    grids     (1.2 m 网格 + 并查集合并相邻格)
        ↓
    clusters  (质心 / 点数 / 平均强度)
        ↓ 贪心最近邻关联（门限 GATE）
    tracks    (id / 位置 / 速度 / 存活计数)
        ↓
    confirmed tracks → 前端画框 + 航迹列表

为什么不用完整卡尔曼：目标运动近似匀速、观测噪声主要来自栅格量化
（0.5 m 距离栅格、1° 方位），α-β 滤波在这种量级下与 KF 差别可忽略，
但代码量和调试成本低一个数量级。
"""
from __future__ import annotations

import math

# ---- 调参区（都在米/秒量级，改这里就能调行为）----
DBSCAN_EPS = 0.50    # DBSCAN 邻域半径 (m)：小于它的点视为同一物体
MIN_PTS = 5          # 少于这个点数的簇当噪声丢掉
MAX_PTS = 400        # 多于这个点数的簇当背景（墙/地面）
GATE = 2.5           # 关联门限 (m)：预测位置与观测质心的最大距离
ALPHA = 0.55         # 位置平滑增益（0~1，越大越信观测）
BETA = 0.22          # 速度增益
CONFIRM_HITS = 3     # 连续命中几次才算「确认航迹」
MAX_MISSES = 4       # 连续几帧没关联上就销号
MAX_TRACKS = 24      # 最多同时维护的航迹数（按距离保留最近的）


class _UF:
    """并查集：把相邻网格里的点合并成同一个簇。"""

    __slots__ = ("p",)

    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, a: int) -> int:
        p = self.p
        while p[a] != a:
            p[a] = p[p[a]]      # 路径压缩
            a = p[a]
        return a

    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


class Track:
    __slots__ = ("id", "x", "y", "z", "vx", "vy", "vz",
                 "hits", "misses", "age", "rng", "az", "el", "snr")

    def __init__(self, tid: int, x: float, y: float, z: float, snr: float):
        self.id = tid
        self.x, self.y, self.z = x, y, z
        self.vx = self.vy = self.vz = 0.0
        self.hits = 1
        self.misses = 0
        self.age = 1
        self.rng = math.hypot(x, y)
        self.az = math.degrees(math.atan2(y, x)) % 360.0
        self.el = 0.0
        self.snr = snr

    # --- 预测：匀速模型外推 ---
    def predict(self, dt: float) -> tuple[float, float, float]:
        return (self.x + self.vx * dt,
                self.y + self.vy * dt,
                self.z + self.vz * dt)

    # --- 更新：α-β 滤波 ---
    def update(self, mx: float, my: float, mz: float, snr: float, dt: float):
        px, py, pz = self.predict(dt)
        rx, ry, rz = mx - px, my - py, mz - pz      # 新息
        self.x = px + ALPHA * rx
        self.y = py + ALPHA * ry
        self.z = pz + ALPHA * rz
        if dt > 1e-6:
            self.vx += BETA * rx / dt
            self.vy += BETA * ry / dt
            self.vz += BETA * rz / dt
        self.rng = math.hypot(self.x, self.y)
        self.az = math.degrees(math.atan2(self.y, self.x)) % 360.0
        self.el = math.degrees(math.atan2(self.z, max(1e-6, self.rng)))
        self.snr = snr
        self.hits += 1
        self.misses = 0
        self.age += 1

    def miss(self):
        self.misses += 1
        self.age += 1
        self.hits = max(0, self.hits - 1)   # 漏检会削弱置信度

    @property
    def confirmed(self) -> bool:
        return self.hits >= CONFIRM_HITS and self.misses == 0

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "x": round(self.x, 3), "y": round(self.y, 3), "z": round(self.z, 3),
            "vx": round(self.vx, 3), "vy": round(self.vy, 3),
            "range": round(self.rng, 2),
            "az": round(self.az, 1),
            "el": round(self.el, 1),
            "speed": round(self.speed, 2),
            # 接近为正：径向速度分量
            "radial": round(-(self.vx * self.x + self.vy * self.y) / max(1e-6, self.rng), 2),
            "snr": round(self.snr, 3),
            "hits": self.hits,
            "age": self.age,
            "confirmed": self.confirmed,
        }


class Tracker:
    def __init__(self):
        self.tracks: list[Track] = []
        self._next_id = 1
        self.frames = 0

    # ---------------- 聚类 ----------------
    @staticmethod
    def _cluster(points: list) -> list[dict]:
        """DBSCAN 聚类（网格加速），返回每个簇的质心与统计。

        为什么不用「网格 + 并查集」：那种做法会把所有相连的点连成一个簇，
        场景里的墙、柱子、人只要沿扫描面连续分布就会被并成一大团，质心飘到
        几何中心，跟踪器完全抓不住单个目标。DBSCAN 靠邻域半径 eps 划边界，
        相距 0.75 m 以上的物体天然分开。
        """
        n = len(points)
        if n == 0:
            return []

        # 极坐标 → 笛卡尔（az 度、r 米、z 已是相对高度）
        xs = [0.0] * n
        ys = [0.0] * n
        zs = [0.0] * n
        ws = [0.0] * n
        for i, (az, r, z, snr, _sub) in enumerate(points):
            a = math.radians(az)
            xs[i] = r * math.cos(a)
            ys[i] = r * math.sin(a)
            zs[i] = z
            ws[i] = snr

        inv = 1.0 / DBSCAN_EPS
        grid: dict[tuple[int, int], list[int]] = {}
        for i in range(n):
            grid.setdefault((int(xs[i] * inv), int(ys[i] * inv)), []).append(i)

        eps2 = DBSCAN_EPS * DBSCAN_EPS
        visited = bytearray(n)
        out: list[dict] = []

        for i in range(n):
            if visited[i]:
                continue
            visited[i] = 1
            stack = [i]
            members = []
            while stack:
                j = stack.pop()
                members.append(j)
                gx = int(xs[j] * inv)
                gy = int(ys[j] * inv)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for k in grid.get((gx + dx, gy + dy), ()):
                            if visited[k]:
                                continue
                            ddx = xs[k] - xs[j]
                            ddy = ys[k] - ys[j]
                            if ddx * ddx + ddy * ddy <= eps2:
                                visited[k] = 1
                                stack.append(k)

            k = len(members)
            if k < MIN_PTS or k > MAX_PTS:
                continue
            sx = sy = sz = sw = 0.0
            for j in members:
                sx += xs[j]; sy += ys[j]; sz += zs[j]; sw += ws[j]
            out.append({
                "x": sx / k, "y": sy / k, "z": sz / k,
                "snr": sw / k, "n": k,
            })
        return out

    # ---------------- 主更新 ----------------
    def update(self, points: list, dt: float) -> list[dict]:
        self.frames += 1
        dt = max(1e-3, min(1.0, dt))
        clusters = self._cluster(points)

        # 预测全部航迹
        preds = {t.id: t.predict(dt) for t in self.tracks}

        # 贪心关联：先算所有 (距离, track, cluster)，从小到大匹配
        pairs = []
        for t in self.tracks:
            px, py, pz = preds[t.id]
            for ci, c in enumerate(clusters):
                d = math.hypot(px - c["x"], py - c["y"])
                if d <= GATE:
                    pairs.append((d, t.id, ci))
        pairs.sort(key=lambda p: p[0])

        used_t: set[int] = set()
        used_c: set[int] = set()
        tmap = {t.id: t for t in self.tracks}
        for _d, tid, ci in pairs:
            if tid in used_t or ci in used_c:
                continue
            used_t.add(tid)
            used_c.add(ci)
            c = clusters[ci]
            tmap[tid].update(c["x"], c["y"], c["z"], c["snr"], dt)

        # 未匹配的航迹：miss
        for t in self.tracks:
            if t.id not in used_t:
                t.miss()

        # 未匹配的簇：新建航迹（只给 4 m 内的，远处多半是墙）
        for ci, c in enumerate(clusters):
            if ci in used_c:
                continue
            if math.hypot(c["x"], c["y"]) > 12.0:
                continue
            if len(self.tracks) >= MAX_TRACKS:
                break
            t = Track(self._next_id, c["x"], c["y"], c["z"], c["snr"])
            self._next_id = self._next_id % 100000 + 1
            self.tracks.append(t)

        # 销号
        self.tracks = [t for t in self.tracks if t.misses <= MAX_MISSES]

        # 排序：确认的在前，然后按距离
        self.tracks.sort(key=lambda t: (not t.confirmed, t.rng))
        return [t.to_dict() for t in self.tracks if t.confirmed]

    # ---------------- 只读访问 ----------------
    def snapshot(self) -> list[dict]:
        return [t.to_dict() for t in self.tracks if t.confirmed]

    @property
    def count(self) -> int:
        return sum(1 for t in self.tracks if t.confirmed)
