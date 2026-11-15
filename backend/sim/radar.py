"""模拟雷达数据源：几何射线投射 + 地面回波环 + 电机/高度状态机。

生成的点云走真实 1E61 语义（body 坐标系前 x / 右 y / 上 z，米），
所以前端与协议栈完全按真机路径工作，接真机时只需换数据源。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

# 侧阵：12 元垂直阵 DBF，俯仰 FOV 约 ±10°
ELEV_FOV_DEG = 10.0
# 地面回波：range bin 0.73 m（顶阵参数块 0x177578 的实测值）
RANGE_BIN = 0.73
# 侧阵最大量程（文档：0xC0 = 0.5 m 栅格，20/30 m 两种模式）
MAX_RANGE = 30.0
# 高度门限：flight_mode==2 且融合高度 < 1.6 m 时避障快照被清零
HEIGHT_GATE_M = 1.6


@dataclass
class SceneObject:
    """2D 场景基元（xy 平面），带高度。"""
    kind: str          # "segment" | "circle"
    pts: tuple = ()    # segment: (x1,y1,x2,y2)
    center: tuple = (0.0, 0.0)
    radius: float = 0.0
    height: float = 1.0     # 物体高度（m）
    snr: float = 1.0        # 相对回波强度


def _ray_segment(ox, oy, dx, dy, x1, y1, x2, y2):
    """射线与线段求交，返回 t>0 的距离或 None。"""
    ex, ey = x2 - x1, y2 - y1
    den = dx * ey - dy * ex
    if abs(den) < 1e-12:
        return None
    t = ((x1 - ox) * ey - (y1 - oy) * ex) / den
    u = ((x1 - ox) * dy - (y1 - oy) * dx) / den
    if t > 1e-6 and 0.0 <= u <= 1.0:
        return t
    return None


def _ray_circle(ox, oy, dx, dy, cx, cy, r):
    fx, fy = ox - cx, oy - cy
    b = fx * dx + fy * dy
    c = fx * fx + fy * fy - r * r
    disc = b * b - c
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    t1, t2 = -b - sq, -b + sq
    for t in (t1, t2):
        if t > 1e-6:
            return t
    return None


class Scene:
    """一个可射线投射的 2D 场景。"""

    def __init__(self, kind: str = "room"):
        self.kind = kind
        self.objects: list[SceneObject] = []
        self.walker_obj: SceneObject | None = None
        self._build(kind)

    def _build(self, kind: str):
        if kind == "room":
            # 10 x 8 m 的车间/室内空间
            self.objects += [
                SceneObject("segment", (-5, -4, 5, -4), height=3.0, snr=1.0),
                SceneObject("segment", (5, -4, 5, 4), height=3.0, snr=1.0),
                SceneObject("segment", (5, 4, -5, 4), height=3.0, snr=1.0),
                SceneObject("segment", (-5, 4, -5, -4), height=3.0, snr=1.0),
                # 桌子 / 椅子 / 箱子 / 立柱
                SceneObject("circle", center=(2.6, 1.4), radius=0.55, height=0.78, snr=0.9),
                SceneObject("circle", center=(-1.9, 2.3), radius=0.38, height=0.9, snr=0.8),
                SceneObject("segment", (-3.4, -1.5, -1.6, -1.5), height=0.55, snr=0.7),
                SceneObject("circle", center=(3.9, -2.4), radius=0.30, height=2.4, snr=0.85),
                SceneObject("segment", (0.8, 3.1, 3.2, 3.1), height=1.1, snr=0.65),
            ]
            # 一个来回走动的人：默认场景也能看到航迹跟踪（不然房间是静止的）。
            # 走 x=-0.6 的直线，y ∈ [1.4, 3.4]：避开雷达近场盲区(r<1.5) 和
            # 桌子(2.6,1.4)/椅子(-1.9,2.3)/矮墙(y=3.1 且在 x>0.8 处，不冲突)。
            self.walker = {"y": 1.4, "dir": 1.0}
            self.walker_obj = SceneObject("circle", center=(-0.6, 1.4), radius=0.28,
                                          height=1.72, snr=0.95)
            self.objects.append(self.walker_obj)
        elif kind == "field":
            # 开阔场地：远处几根杆 + 一段矮墙
            for i in range(6):
                a = i * 60.0 + 12.0
                d = 9.0 + i * 1.7
                self.objects.append(SceneObject(
                    "circle", center=(d * math.cos(math.radians(a)),
                                      d * math.sin(math.radians(a))),
                    radius=0.22, height=4.0, snr=0.75))
            self.objects.append(SceneObject("segment", (6, -8, 14, -8), height=1.2, snr=0.6))
        elif kind == "wire":
            # 线缆：横跨的电线（z 固定 2.2 m，靠 1E61 subtype=1 上报）
            self.wires = [
                ((-12, -2.5), (12, -2.5), 2.2),
                ((-12, 1.8), (12, 1.8), 2.6),
                ((-12, 5.0), (12, 5.0), 1.9),
            ]
            self.objects.append(SceneObject("circle", center=(5.0, 0.5), radius=0.3,
                                            height=2.0, snr=0.8))
        else:  # "obstacle"：走廊 + 一个来回走动的人
            self.objects += [
                SceneObject("segment", (-2.0, -6, -2.0, 6), height=2.6, snr=0.9),
                SceneObject("segment", (2.0, -6, 2.0, 6), height=2.6, snr=0.9),
            ]
            self.walker = {"x": 0.0, "y": -4.0, "dir": 1.0}

    def raycast(self, az_deg: float, rng: random.Random) -> list[tuple[float, float, float, int]]:
        """返回该方位上的回波 [(range_m, z_m, snr, subtype)]。"""
        a = math.radians(az_deg)
        dx, dy = math.cos(a), math.sin(a)
        hits: list[tuple[float, float, float, int]] = []

        best = None
        for o in self.objects:
            if o.kind == "segment":
                t = _ray_segment(0.0, 0.0, dx, dy, *o.pts)
            else:
                t = _ray_circle(0.0, 0.0, dx, dy, o.center[0], o.center[1], o.radius)
            if t is not None and t <= MAX_RANGE and (best is None or t < best[0]):
                best = (t, o)
        if best:
            t, o = best
            # 命中点高度在 0..o.height 之间随机（面回波）
            z = rng.uniform(0.05, o.height) * (0.55 + 0.45 * o.snr)
            hits.append((t, z, o.snr * rng.uniform(0.7, 1.0), 2))

        # 线缆场景：方位接近线缆方位时给 subtype=1 的点
        if self.kind == "wire":
            for (p1, p2, hz) in getattr(self, "wires", []):
                t = _ray_segment(0.0, 0.0, dx, dy, p1[0], p1[1], p2[0], p2[1])
                if t is not None and t <= MAX_RANGE:
                    hits.append((t, hz, rng.uniform(0.5, 0.95), 1))
        return hits

    def update(self, dt: float):
        if self.kind == "room" and self.walker_obj is not None:
            w = self.walker
            w["y"] += w["dir"] * dt * 1.2          # 1.2 m/s，接近步行速度
            if w["y"] > 3.4:
                w["y"], w["dir"] = 3.4, -1.0
            elif w["y"] < 1.4:
                w["y"], w["dir"] = 1.4, 1.0
            self.walker_obj.center = (-0.6, w["y"])
            return
        if self.kind == "obstacle" and hasattr(self, "walker"):
            w = self.walker
            w["y"] += w["dir"] * dt * 1.1
            if abs(w["y"]) > 4.5:
                w["dir"] *= -1.0
            # 用圆形基元表示"人"
            self.objects = [o for o in self.objects if o.kind == "segment"]
            self.objects.append(SceneObject("circle", center=(w["x"], w["y"]),
                                            radius=0.3, height=1.75, snr=0.95))


@dataclass
class RadarState:
    running: bool = False
    rpm: float = 900.0
    angle: float = 0.0
    height_cm: int = 180
    flight_mode: int = 2
    init_state: int = 6          # 电机初始化状态机终态 = 6
    frame_seq: int = 0
    point_total: int = 0
    cfar_error: int = 0
    sectors_decoded: int = 360


class RadarSimulator:
    """模拟整机：场景 + 电机 + 高度融合 + 1E61/1E62 数据生成。"""

    def __init__(self, scene: str = "room", seed: int | None = None):
        self.rng = random.Random(seed)
        self.scene = Scene(scene)
        self.scenes = ["room", "field", "wire", "obstacle"]
        self.state = RadarState()
        self._height_target = 180.0
        self._ground_phase = 0.0
        self.view_range = MAX_RANGE   # 显示量程，由 server 的 /api/control 同步

    # ---------------- 控制面 ----------------
    def start(self):
        self.state.running = True
        self.state.init_state = 6

    def stop(self):
        self.state.running = False

    def set_rpm(self, rpm: float):
        self.state.rpm = max(0.0, min(1800.0, float(rpm)))

    def set_scene(self, kind: str):
        if kind in self.scenes:
            self.scene = Scene(kind)

    def set_height(self, cm: float):
        self._height_target = max(0.0, min(500.0, float(cm)))

    # ---------------- 数据面 ----------------
    def tick(self, dt: float = 0.1) -> dict:
        st = self.state
        self.scene.update(dt)

        # 每帧扫过的扇区宽度：900 rpm @10 fps 约 60°（60 rpm 时 4°，1800 rpm 时 90°）。
        # 注意这里**不能**直接用 rpm*6*dt 推进角度 —— 900 rpm 下那是 540°/帧，
        # 帧与帧之间跳 180°，方位采样会退化成 0°/180° 两个值。
        sweep = max(6.0, min(90.0, st.rpm * 6.0 * dt * 0.35)) if st.running else 30.0
        if st.running:
            st.angle = (st.angle + sweep) % 360.0

        # 高度随机游走（模拟 Kalman 输出）
        drift = self.rng.gauss(0, 1.2)
        st.height_cm = int(max(0, min(500, st.height_cm + (self._height_target - st.height_cm) * 0.08 + drift)))

        points: list[tuple[float, float, float, float, int]] = []
        h = st.height_cm / 100.0

        # 1) 地面回波：以雷达为原点，地面恒在 z = -h。
        #    只有俯仰角 ≤ ELEV_FOV 的那一段地面才落在波束内，
        #    对应近端 r_min = h / tan(FOV)。
        n_ground = 60 if st.running else 12
        r_min = max(1.5, h / math.tan(math.radians(ELEV_FOV_DEG)))
        # 采样半径上限跟着量程走：远场按 1/R² 衰减，30 m 处的点在 10 m 量程里
        # 又小又淡，不如把预算花在看得见的近场。
        r_max = min(MAX_RANGE, RANGE_BIN * int(self.view_range / RANGE_BIN))
        for _ in range(n_ground):
            r = self.rng.uniform(r_min, r_max)
            az = self.rng.uniform(0, 360)
            z = -h + self.rng.gauss(0, 0.03)
            snr = max(0.05, 0.92 * math.exp(-r / 16.0)) * self.rng.uniform(0.55, 1.0)
            points.append((az, r, z, snr, 0))

        # 2) 场景回波：每帧输出**完整 360° 一圈**。
        #    这是真雷达的行为 —— 机械转一圈，每个扇区一条 CFAR 记录，业务层
        #    处理完才聚合成一帧 1E61。之前按 sweep 只扫一个扇区，会让单帧点云
        #    变成一条弧，同一个目标只有 1–2 个点，根本没法聚类跟踪。
        #    st.angle 只驱动扫描线动画，不决定采样范围。
        if st.running:
            n_rays = 300                      # 1.2° 步进
            for i in range(n_rays):
                az = (i * 360.0 / n_rays + self.rng.gauss(0, 0.25)) % 360.0
                for (r, z, snr, sub) in self.scene.raycast(az, self.rng):
                    points.append((az, r + self.rng.gauss(0, 0.015), z - h, snr, sub))

        st.frame_seq += 1
        st.point_total += len(points)

        # 3) 16 扇区避障结果（1E62）：取最近的回波
        sectors = []
        for i in range(16):
            sectors.append((int(i * 22.5 * 100), 0, 10000))
        if points and st.running:
            for i in range(16):
                a0, a1 = i * 22.5, (i + 1) * 22.5
                cand = [p for p in points if a0 <= p[0] < a1 and 2.0 < p[1] < 20.0]
                if cand:
                    p = min(cand, key=lambda q: q[1])
                    sectors[i] = (int(a0 * 100), 0, int(p[1] * 100))

        height_gate = st.flight_mode == 2 and st.height_cm < HEIGHT_GATE_M * 100

        return {
            "points": points,
            "sectors": sectors,
            "height_cm": st.height_cm,
            "gated": height_gate,
            "state": {
                "running": st.running,
                "rpm": st.rpm,
                "angle": round(st.angle, 2),
                "flight_mode": st.flight_mode,
                "init_state": st.init_state,
                "seq": st.frame_seq,
            },
        }
