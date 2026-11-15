"""生成 T40 雷达完整接线示意图（电源 → 24P 引出板 → 雷达本体）。"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1680, 1230
BG = (251, 251, 249)
INK = (26, 26, 26)
DIM = (122, 122, 117)
CARD = (255, 255, 255)
BORDER = (214, 213, 207)

C_PWR = (206, 58, 46)
C_GND = (38, 38, 38)
C_DP = (38, 148, 104)
C_DN = (156, 156, 152)
C_CH = (216, 134, 32)
C_CL = (46, 106, 190)
C_PIN = (240, 240, 234)

F_B = "C:/Windows/Fonts/msyhbd.ttc"
F_R = "C:/Windows/Fonts/msyh.ttc"
f_title = ImageFont.truetype(F_B, 34)
f_sub = ImageFont.truetype(F_R, 19)
f_box = ImageFont.truetype(F_B, 21)
f_lbl = ImageFont.truetype(F_R, 17)
f_tiny = ImageFont.truetype(F_R, 14)
f_mini = ImageFont.truetype(F_R, 12)

im = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(im)


def box(x0, y0, x1, y1, r=12, fill=CARD, outline=BORDER, width=2):
    d.rounded_rectangle([x0, y0, x1, y1], r, fill=fill, outline=outline, width=width)


def txt(x, y, s, font=f_lbl, fill=INK, anchor="la"):
    d.text((x, y), s, font=font, fill=fill, anchor=anchor)


def wire(pts, color, width=4):
    for i in range(len(pts) - 1):
        d.line([pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1]],
               fill=color, width=width, joint="curve")
    for p in (pts[0], pts[-1]):
        r = width // 2
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=color)


def arrow(x, y, direction="right", size=10, color=INK):
    if direction == "right":
        d.polygon([(x, y), (x - size, y - size * 0.6), (x - size, y + size * 0.6)], fill=color)
    elif direction == "left":
        d.polygon([(x, y), (x + size, y - size * 0.6), (x + size, y + size * 0.6)], fill=color)
    elif direction == "down":
        d.polygon([(x, y), (x - size * 0.6, y - size), (x + size * 0.6, y - size)], fill=color)


# ── 标题 ────────────────────────────────────────────────
txt(W / 2, 40, "T40 雷达接线总图", f_title, INK, "mm")
txt(W / 2, 80, "15V 电源  →  DC母转杜邦  →  24P 引出板  →  雷达 C 公头", f_sub, DIM, "mm")

# ── 1. 电源 ─────────────────────────────────────────────
PX0, PY0, PX1, PY1 = 56, 148, 236, 258
box(PX0, PY0, PX1, PY1)
txt((PX0 + PX1) / 2, PY0 + 24, "15V 电源", f_box, INK, "mm")
txt((PX0 + PX1) / 2, PY0 + 56, "3A 适配器", f_bl := f_tiny, DIM, "mm")
txt((PX0 + PX1) / 2, PY0 + 82, "DC 5.5×2.1 公头", f_mini, C_PWR, "mm")

wire([(PX1, 203), (PX1 + 44, 203)], C_PWR, 5)
arrow(PX1 + 50, 203, "right", 10, C_PWR)

# ── 2. DC母转杜邦 ───────────────────────────────────────
DX0, DY0, DX1, DY1 = 300, 160, 500, 246
box(DX0, DY0, DX1, DY1)
txt((DX0 + DX1) / 2, DY0 + 20, "DC母转杜邦线", f_box, INK, "mm")
txt((DX0 + DX1) / 2, DY0 + 50, "（待购 ¥3–8）", f_mini, (188, 96, 30), "mm")
txt(DX0 + 18, DY1 - 16, "红 +15V", f_mini, C_PWR, "lm")
txt(DX1 - 18, DY1 - 16, "黑 GND", f_mini, C_GND, "rm")

# ── 3. 24P 引出板 ───────────────────────────────────────
BX0, BY0, BX1, BY1 = 600, 300, 1000, 700
box(BX0, BY0, BX1, BY1, 14)
txt(BX0 + 18, BY0 + 14, "24P 引出板 · 规格9 公转母座", f_box, INK)
txt(BX0 + 18, BY0 + 44, "顺豹科技 ¥8.50", f_mini, DIM)

# 左：公头（不用）  右：母座（雷达插这里）
GX0, GY0, GX1, GY1 = 618, 388, 762, 456
box(GX0, GY0, GX1, GY1, 9, (247, 245, 240), (206, 202, 192))
txt((GX0 + GX1) / 2, (GY0 + GY1) / 2 - 9, "Type-C 公头", f_lbl, DIM, "mm")
txt((GX0 + GX1) / 2, (GY0 + GY1) / 2 + 12, "本方案不用", f_mini, DIM, "mm")

MX0, MY0, MX1, MY1 = 836, 388, 982, 456
box(MX0, MY0, MX1, MY1, 9, (238, 245, 255), (108, 146, 206))
txt((MX0 + MX1) / 2, (MY0 + MY1) / 2 - 9, "Type-C 母座", f_lbl, INK, "mm")
txt((MX0 + MX1) / 2, (MY0 + MY1) / 2 + 12, "雷达插这里", f_mini, (52, 104, 176), "mm")

# 排针
RX0, RY0, RX1, RY1 = 616, 484, 984, 660
box(RX0, RY0, RX1, RY1, 9, (250, 250, 246), (208, 207, 200))
txt(RX0 + 12, RY0 + 7, "2×12 排针  2.54mm", f_mini, DIM)

STEP = 25
PA_Y, PB_Y = 530, 596
PA_X0, PB_X0 = 706, 706
used = {
    ("A", 1): C_GND, ("A", 4): C_PWR, ("A", 5): C_GND, ("A", 9): C_PWR, ("A", 12): C_GND,
    ("B", 1): C_GND, ("B", 2): C_CH, ("B", 3): C_CL, ("B", 6): C_DP, ("B", 7): C_DN, ("B", 12): C_GND,
}
for i in range(12):
    for row, yy, x0 in (("A", PA_Y, PA_X0), ("B", PB_Y, PB_X0)):
        x = x0 + i * STEP
        col = used.get((row, i + 1))
        r = 6 if col else 4
        d.ellipse([x - r, yy - r, x + r, yy + r],
                  fill=col if col else C_PIN, outline=(168, 167, 161), width=1)

# 针位标注
for i in range(12):
    n = i + 1
    x = PA_X0 + i * STEP
    if ("A", n) in used:
        d.text((x, PA_Y - 15), f"A{n}", font=f_mini, fill=used[("A", n)], anchor="ms")
    if ("B", n) in used:
        d.text((x, PB_Y + 16), f"B{n}", font=f_mini, fill=used[("B", n)], anchor="mt")

txt((RX0 + RX1) / 2, RY1 - 16, "板内同号直通", f_mini, DIM, "ms") if False else None

# ── 4. 雷达 ─────────────────────────────────────────────
RX, RY, RW, RH = 1052, 320, 380, 380
d.rounded_rectangle([RX, RY + 36, RX + RW, RY + RH], 10,
                    fill=(240, 238, 232), outline=(198, 195, 187), width=2)
d.ellipse([RX, RY, RX + RW, RY + 72], fill=(249, 248, 245), outline=(198, 195, 187), width=2)

# 侧面天线阵（4 列 × 6 行）
for i in range(4):
    for j in range(6):
        cx = RX + 44 + i * 30
        cy = RY + 104 + j * 34
        d.rounded_rectangle([cx, cy, cx + 22, cy + 26], 3,
                            fill=(210, 176, 116), outline=(180, 148, 94))

txt(RX + 250, RY + 150, "RD2484R", f_box, INK, "lm")
txt(RX + 250, RY + 182, "雷达本体", f_lbl, DIM, "lm")
txt(RX + 250, RY + 214, "24 GHz", f_mini, DIM, "lm")
txt(RX + 250, RY + 238, "机械扫描 900rpm", f_mini, DIM, "lm")
txt(RX + RW / 2, RY + RH - 18, "上半板线圈取电，无物理连线", f_mini, DIM, "mm")

# 雷达 C 公头
d.rounded_rectangle([RX - 40, 412, RX + 8, 440], 5, fill=(74, 74, 72), outline=(40, 40, 38), width=2)
txt(RX - 62, 458, "C 公头", f_mini, (60, 60, 58))
txt(RX - 62, 476, "(plug)", f_mini, DIM)

# 雷达公头 → 板母座
wire([(RX - 44, 426), (MX1 + 6, 426)], (200, 197, 189), 10)
arrow(MX1 + 10, 426, "left", 11, (158, 156, 150))
txt(MX0 + 30, MY0 - 26, "正插 · A面↔A面", f_mini, (168, 64, 54), "lb")

# ── 5. 电源线 → 排针 ────────────────────────────────────
wire([(DX1, 186), (570, 186), (570, 280), (781, 280), (781, PA_Y)], C_PWR, 5)   # A4
wire([(DX1, 218), (592, 218), (592, 474), (694, 474), (694, PA_Y), (706, PA_Y)], C_GND, 5)   # A1

# ── 6. 数据线：排针 → USB转杜邦 → 电脑 ────────────────
UX0, UY0, UX1, UY1 = 130, 826, 348, 916
box(UX0, UY0, UX1, UY1)
txt((UX0 + UX1) / 2, UY0 + 20, "USB转杜邦线 (AM)", f_lbl, INK, "mm")
txt((UX0 + UX1) / 2, UY0 + 50, "USB-A 公 → 4×杜邦母", f_mini, DIM, "mm")

CX0, CY0, CX1, CY1 = 130, 986, 348, 1076
box(CX0, CY0, CX1, CY1)
txt((CX0 + CX1) / 2, CY0 + 22, "电脑 USB-A", f_lbl, INK, "mm")
txt((CX0 + CX1) / 2, CY0 + 50, "枚举 0cf3:1022 网卡", f_mini, DIM, "mm")

wire([(856, PB_Y), (856, 756), (300, 756), (300, UY0)], C_DP, 4)    # B6 D+
wire([(881, PB_Y), (881, 742), (266, 742), (266, UY0)], C_DN, 4)    # B7 D-
wire([(706, PB_Y), (706, 780), (232, 780), (232, UY0)], C_GND, 4)   # B1 GND

for k, col in enumerate((C_DP, C_DN, C_GND)):
    x = 190 + k * 44
    wire([(x, UY1), (x, CY0)], col, 4)
txt((UX0 + UX1) / 2, UY1 - 22, "绿=D+  白=D−  黑=GND", f_mini, DIM, "ms")

# ── 7. CAN 线：排针 → CANable ──────────────────────────
KX0, KY0, KX1, KY1 = 900, 826, 1200, 926
box(KX0, KY0, KX1, KY1)
txt((KX0 + KX1) / 2, KY0 + 20, "MKS CANable V2.0 S", f_lbl, INK, "mm")
txt((KX0 + KX1) / 2, KY0 + 50, "3P 螺丝端子 + 120Ω 拨码", f_mini, DIM, "mm")
txt((KX0 + KX1) / 2, KY0 + 74, "¥61.49", f_mini, DIM, "mm")

wire([(731, PB_Y), (731, 800), (952, 800), (952, KY0)], C_CH, 4)    # B2 CANH
wire([(756, PB_Y), (756, 812), (980, 812), (980, KY0)], C_CL, 4)    # B3 CANL
wire([(981, PB_Y), (981, 770), (1008, 770), (1008, KY0)], C_GND, 4) # B12 GND

wire([(KX1, 878), (KX1 + 56, 878)], (124, 124, 120), 4)
arrow(KX1 + 62, 878, "right", 9, (124, 124, 120))
txt(KX1 + 70, 862, "CAN 1 Mbit", f_mini, DIM)
txt(KX1 + 70, 884, "→ USB → VM", f_mini, DIM)

# ── 8. 图例 ─────────────────────────────────────────────
LX, LY = 440, 806
box(LX - 14, LY - 14, LX + 268, LY + 196, 10, (253, 253, 251), (226, 225, 219))
txt(LX, LY, "线色对照", f_lbl, INK)
legend = [
    (C_PWR, "红", "+15V  A4 / A9"),
    (C_GND, "黑", "GND   A1 A5 B1 B12"),
    (C_DP, "绿", "D+    B6"),
    (C_DN, "白", "D−    B7"),
    (C_CH, "橙", "CAN_H B2"),
    (C_CL, "蓝", "CAN_L B3"),
]
for i, (col, name, desc) in enumerate(legend):
    y = LY + 30 + i * 27
    d.rounded_rectangle([LX, y, LX + 30, y + 9], 4, fill=col, outline=(182, 182, 176))
    d.text((LX + 42, y - 5), f"{name}   {desc}", font=f_mini, fill=INK)

# ── 9. 注意 ─────────────────────────────────────────────
WX0, WY0, WX1, WY1 = 730, 1030, 1620, 1196
d.rounded_rectangle([WX0, WY0, WX1, WY1], 12, fill=(253, 246, 244), outline=(228, 180, 172), width=2)
txt(WX0 + 20, WY0 + 12, "三条必须注意", f_box, (176, 48, 38))
notes = [
    "1.  必须正插：雷达只在 A 面接 VCC（A4/A9），插反完全没电（但不会烧）。不亮就拔出来转 180° 再插。",
    "2.  禁止 5V 倒灌：USB 转杜邦线的红线（5V+）必须剪断或绝缘，否则 15V 经 VBUS 烧电脑主板。",
    "3.  先验方向：只接 15V 看 LED，确认有电再逐根接数据线；一次接错很难查。",
]
for i, s in enumerate(notes):
    txt(WX0 + 20, WY0 + 48 + i * 34, s, f_lbl, (108, 42, 36))

# ── 10. 左下角说明 ──────────────────────────────────────
txt(60, 1140, "红色箭头 = 供电走向   彩色细线 = 信号线   灰色粗线 = Type-C 插合", f_mini, DIM)

im.save(r"E:\dsh\t40-radar\docs\wiring-diagram.png")
print("saved", im.size)
