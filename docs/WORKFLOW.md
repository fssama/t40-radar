# RD2484R 雷达完整工作流

> 大疆 T40 农业无人机 24 GHz 有源相控阵雷达（RD2484R）的**逆向、接线、部署、数据接入**全流程。
> 本文档基于 2026-11 的实测记录，所有「实测」标记的数值都是这台机器上跑出来的，不是推测。
>
> 适用对象：拿到一台拆机 RD2484R，想让它在本机输出可看的点云。

---

## 目录

1. [它是什么 / 能做什么](#1-它是什么--能做什么)
2. [引用源与致谢](#2-引用源与致谢)
3. [购物清单](#3-购物清单)
4. [接线教程](#4-接线教程)
5. [软件环境部署（Ubuntu）](#5-软件环境部署ubuntu)
6. [每次上电后的启动流程](#6-每次上电后的启动流程)
7. [前后端代码结构](#7-前后端代码结构)
8. [数据流与 HTTP API](#8-数据流与-http-api)
9. [协议要点（含单位修正）](#9-协议要点含单位修正)
10. [故障排查](#10-故障排查)
11. [已知限制与后续方向](#11-已知限制与后续方向)

---

## 1. 它是什么 / 能做什么

### 1.1 硬件构成

RD2484R 是 DJI Agras T40 的避障雷达，装在机身下方，由两半组成：

| 部位 | 内容 |
|---|---|
| **上板** | Zynq XC7Z020（ARM + FPGA）、AFE5401 四通道 ADC、**AR1021x USB WiFi 网卡**、24 GHz 射频前端 |
| **下板** | GD32F305 MCU、电机驱动、Type-C 接口、CAN 收发器 |
| **顶面天线** | 4×4 阵，2T1R，FOV 45°，1.5–30 m —— 测高度/近距地面 |
| **侧面天线** | 12×6 阵，机械旋转 900 rpm，360° 扇区扫描 —— 测侧面障碍 |
| **连接** | 上下板**没有电气连线**：下板线圈取电给上板，数据走 WiFi 回传 |

关键参数：

| 项 | 值 |
|---|---|
| 频段 | 24.05–24.25 GHz |
| 固件 | `ag601 V01.03.1005`（雷达内 Linux 3.17.0-dji，armv7l） |
| 供电 | **15 V**，12–18 W（Type-C 的 A4/A9/B4/B9） |
| 通信 | USB 2.0（WiFi 网卡）+ CAN 1 Mbit（电机/飞控）+ UDP（数据） |

### 1.2 两条数据通道

雷达原本是发给飞控的，**不会主动给电脑发数据**。要拿到数据，必须：

1. **扮演飞控**：CAN 发心跳/电机指令、UDP 50007 发 V1 控制面（飞行模式等）
2. **收雷达的数据**：UDP 40204 收 V2 上行帧（1E61 点云 / 1E62 避障结果）

```
雷达(Zynq) ──WiFi radar_wifi──▶ 你的 Linux (ath6kl 驱动 + hostapd, 192.168.8.1)
    ▲                                  │
    │── CAN 1 Mbit (0x0A2/0x0A3) ──────│  假飞控：心跳 + 转速
    │── UDP 50007 (V1 控制面) ──────────│  flight_mode=2 激活侧阵
    │── UDP 40204 (V2 上行) ───────────▶│  1E61 点云 → 本工程
```

### 1.3 当前验证状态（2026-11-15 实测）

| 能力 | 状态 | 说明 |
|---|---|---|
| 顶阵热图（32×64 float32） | ✅ 通 | `radar_streamer top` 读内存 → UDP 9876 |
| 顶阵 AGL 高度 / 峰值点 | ✅ 通 | 实测 `AGL dist=1.40m`、峰值 `r=1.33/3.58m` |
| 侧面点云（1E61） | ✅ 通 | 900 rpm 下 **~1250–1479 pts/s**，每帧 84–98 点 |
| 侧面避障结果（1E62） | ✅ 通 | `btm=1.41m active_sectors=1/16` |
| 转速控制（前端→CAN） | ✅ 通 | 前端滑块 → 后端 → VM 桥 → `duml_can_tool` → ESC |
| 原始 CFAR ring（0x1C100000） | ⬜ 未接 | 作者有 `radar_streamer side`，帧 60632 B |
| FFT 复数输出（0x1C900000） | ⬜ 未做 | 作者也没进展，格式未知 |
| 任意波形注入 | ❌ 不可行 | FPGA 固件闭源，无注入路径 |

---

## 2. 引用源与致谢

本工程的协议知识来自下面这位作者的公开逆向，**所有协议字段、内存地址、驱动修改都来自他**。
本仓库是独立实现（前后端代码为自己写），但**没有他的资料这台雷达根本跑不起来**。

### 2.1 原始逆向仓库

**https://github.com/leommxj/dji-agras-radar**

仓库里对本次工作最关键的目录：

| 路径 | 内容 |
|---|---|
| `ar1021x_driver/` | 6.x 内核的 ath6kl out-of-tree 驱动（6 处修改，含 AP 模式关键 patch） |
| `setup/` | `hostapd.conf` / `start_ap.sh` / `start_can.sh` |
| `radar_client/radar_min_client.py` | 3029 行，假飞控 + 协议解析 + matplotlib 可视化 |
| `radar_client/radar_streamer.c` | 328 行，读雷达进程内存 → UDP 打包（顶阵/侧面） |
| `radar_client/radar_mem_poke.py` | 335 行，内存读写 + 灵敏度预设 |
| `radar_client/duml_can_tool.py` | CAN DUML 工具（`send motor-speed --speed N`） |
| `radar_client/radar_link_protocols.md` | 协议文档（1E61/1E62 字段、subtype 含义） |
| `radar_client/re_notes.md` | app.elf 逆向笔记（内存地址、门控逻辑） |

### 2.2 作者博客（逆向过程记录）

**https://www.leommxj.com/2026/04/20/dji-t40-RD2484R-radar-reverse-engineering/**

里面记录了 AP 模式故障的根因（`wmi_connect_event` 结构体字段顺序）、USB 不稳定、飞车事故等。

### 2.3 其他

| 来源 | 用途 |
|---|---|
| `github.com/normaldotcom/canable2` | Canable V2.0 的 CDC ACM 固件（`16d0:117e`，免驱） |
| `qca/ath6kl-firmware` | AR6004 hw3.0 固件（md5 `c138131e61de924ce0f7f98fe6aefbd8`，与原厂一致） |
| Linux 内核 `ath6kl` | 上游驱动，作者在此基础上改 |

### 2.4 本工程与作者仓库的差异

| 项 | 作者仓库 | 本工程 |
|---|---|---|
| 可视化 | matplotlib 三面板（需桌面） | **Web 前端**（无桌面也能看） |
| 数据接入 | 直接读 UDP 9876/9877（同一台 Linux） | VM 里转发 → Windows 后端解析 40204 |
| 驱动 patch | 6 处 | 6 处 + **2 处本地 hack**（见 §5.3） |
| 1E61 单位 | 字段名写作 `_m`（米） | **实测为分米，乘 0.1**（见 §9.2） |
| 转速控制 | 命令行 `duml_can_tool` | 前端滑块 → HTTP → UDP 桥 |

---

## 3. 购物清单

### 3.1 必需件

| 商品 | 价格 | 用途 | 备注 |
|---|---|---|---|
| **RD2484R 雷达整机** | 千元级 | 本体 | 二手拆机，注意验货（§3.3） |
| **15V 3A 电源适配器**（DC 5.5×2.1 头） | ¥22.72 | 供电 | 雷达要 12–18 W，别用 12V/1A |
| **DC 母 5.5 转 Type-C 公头线** 22 cm | ¥2.30 | 把电源送进雷达 | 淘宝搜「DC5.5 母转 Type-C 公」 |
| **Type-C 公转母座 双排竖针 24P 测试板** | ¥8.50 | **核心件** | 顺豹「规格 9：TYPE C 公转母座 双排竖针 24P」 |
| **USB-A 公转 4×杜邦母线** 0.30 m | ¥5.45 | USB 数据 | 必须是 **A 型公头**（插电脑那种方口） |
| **杜邦线 母对母** 21 cm（40 根） | ¥3.36 | 排针接线 | 一包够用 |
| **Makerbase CANable V2.0 S** | ¥61.49 | 侧面点云必需 | 要 **V2.0 S 版**（3P 螺丝端子 + 120Ω 拨码 + 金属壳） |

**合计约 ¥81.80**（不含雷达本体和电源）。

### 3.2 为什么必须要那块 24P 测试板

Type-C 口同一时刻**只能插一个东西**：插电源就没数据线，插数据线就没 15V。
测试板把 **Type-C 公头 + Type-C 母座 + 2×12 排针** 三者并在一起，让电和数据同时进出。
没有它，雷达到了也是干瞪眼。

### 3.3 到货验货（10 分钟，先别上电）

1. **看外壳**：天线罩有没有裂、圆柱有没有变形
2. **手转电机**：手指轻推上半板，应该能顺滑转动、无卡顿异响（有滑环感阻力正常）
3. **看 Type-C 座**：手电筒照，座子有没有歪、引脚有没有脱焊
4. **看背面 BTB**：24 pin 小连接器是不是空的（空着才对）
5. **看有没有 `ATHEROS` 字样的模块**：那是 WiFi 网卡，**别拆别碰**
6. **称重**：整机约 300–400 g

然后**只接供电**（先别接数据）：

- LED 亮不亮
- **上半板是否自己慢慢转**（默认 60 rpm，约 1 秒 1 圈）
- 转动时有没有异响

✅ 转了 = 电机、线圈取电、下板 MCU 都是好的。
❌ 不转 = 先别接数据，直接找卖家。

---

## 4. 接线教程

### 4.1 Type-C 引脚表（来自作者实测）

雷达的 Type-C 母座里，有用的只有这几根：

| 针脚 | 信号 | 接到哪 |
|---|---|---|
| **A4 / A9 / B4 / B9** | VBUS **15V** | 电源正极 |
| **A1 / A12 / B1 / B12** | GND | 电源负极 + 数据地 |
| **B6** | USB D+ | 电脑 USB **绿**线 |
| **B7** | USB D− | 电脑 USB **白**线 |
| **B2** | CAN_H | Canable **CANH** |
| **B3** | CAN_L | Canable **CANL** |
| A2 / A3 | RS485 A/B | 实测无数据，不用 |
| A5 | CC1 | ⚠️ **不要拿它当地**（见 §4.5） |

### 4.2 测试板丝印识别

24P 测试板两面丝印（以顺豹规格 9 为例）：

```
A 面： GND | A2 | A3 | VCC | A5 | ... | VCC | A11 | ...
B 面： GND | B11 | B10 | VCC | B8 | D- | D+ | B5 | VCC | B3 | B2 | GND | SGND
```

⚠️ **两排 D+/D− 不是同一个东西**：
- 一排是「公头侧」引脚（插进测试板 Type-C 公头的那排），**不通到雷达**
- 另一排是「母座侧」引脚（真正连到雷达母座的），**只有这排能通信**

**实测教训**：白绿先插在公头侧那排 → 电脑零枚举；换到母座侧（丝印 `D-`/`D+` 与 `B3`/`B2` 同排）→ 立刻枚举成功。

### 4.3 完整接线表

| 线 | 从 | 到 | 说明 |
|---|---|---|---|
| 15V 正极 | 电源 DC 头 | 测试板 **VCC**（A4/A9） | 见 §4.4 正负极判别 |
| 15V 负极 | 电源 DC 头 | 测试板 **GND**（A1/A12） | **不要接 A5** |
| USB 白 | USB-A 线 | 测试板 **D−**（B7） | 差分负 |
| USB 绿 | USB-A 线 | 测试板 **D+**（B6） | 差分正 |
| USB 黑 | USB-A 线 | 测试板 **GND** | 必须接，差分要共地 |
| USB 红 | USB-A 线 | ❌ **剪断包死** | 5V 会倒灌，见下 |
| CAN_H | Canable CANH | 测试板 **B2** | 拧端子螺丝 |
| CAN_L | Canable CANL | 测试板 **B3** | 拧端子螺丝 |
| Canable GND | — | 空着 | 雷达和电脑已共地 |

> 🔥 **红色那根是唯一会烧东西的线。** 测试板上的 VBUS 针和雷达的 15V 是**直通**的，
> 把 USB 红线插上去 = 把 15V 倒灌进电脑 USB 口。轻则烧口，重则烧主板。**先剪了它再动别的。**

### 4.4 正负极怎么区分（没有万用表时）

DC 电源线通常是双芯：

- **线身有白色条纹 / 白色虚线的 = 正极（+）**
- **纯色那根 = 负极（−）**

不要靠「橙红是正、深灰是负」这种颜色猜——不同厂家配色不一样。

**不需要万用表的验证法**：只接电源（不接 USB/CAN），看雷达转不转。转了 = 极性对。

### 4.5 两个致命细节

**① 15V 负极不要走 A5**
A5 是 CC1 触点，只能过几百 mA。让 12–18 W 的电流从 CC 触点回流，接触电阻会发热、压降、偶发断连。
**负极一律走 A1 / A12 / B1 / B12。**

**② 上电顺序**
```
断电状态接线 → USB-A 先插电脑 → 最后插 15V 电源
```
反过来的话，USB 数据线插上瞬间 15V 已经在板上，某些主板的 USB 控制芯片会先看到异常电平。

### 4.6 常见接线故障对照

| 现象 | 原因 |
|---|---|
| 电脑完全没反应（无叮咚、无 PnP 事件） | D+/D− 插到「公头侧」那排了（§4.2） |
| 同上 | USB 黑线没接（没共地） |
| 同上 | USB 口坏了 / 杜邦头没插到底 |
| 枚举出来但反复掉线 | 杜邦线接触不良——作者最大的坑是「不扣金属盖时 USB 极不稳定」 |
| 枚举出来但 ping 不通 | 驱动补丁没生效（§5.3） |
| 雷达不转 | 15V 没到、或极性反（正极识别见 §4.4） |
| USB 口烧了 | **插了红色那根** |

---

## 5. 软件环境部署（Ubuntu）

### 5.1 为什么必须是 Linux

- AR1021x 网卡**全球只有 Linux `ath6kl` 驱动**
- CAN 需要 SocketCAN + `slcand`
- Windows 原生两个都不行

历史实测使用 VMware 内的 Ubuntu 25.10 和 `6.17.0-41-generic`。当前部署入口见 [Agent 部署工作流](AGENT-DEPLOY.md)，按目标内核编译并验证驱动。Windows 可以作为监视器或浏览器宿主。

### 5.2 基础环境清单

| 组件 | 位置 / 命令 | 说明 |
|---|---|---|
| 仓库 | `~/work/dji-agras-radar/` | 作者仓库 clone |
| Python venv | `~/work/venv/` | 含 `python-can 4.6.1`、`numpy`、`paramiko`、`matplotlib` |
| 驱动源码 | `~/work/dji-agras-radar/ar1021x_driver/` | 编译产物 `ath6kl/ath6kl_core.ko` |
| 固件 | `/lib/firmware/ath6k/AR6004/hw3.0/` | `fw-5.bin` md5 `c138131e61de924ce0f7f98fe6aefbd8` |
| hostapd | `~/work/dji-agras-radar/setup/hostapd.conf` | `ssid=radar_wifi`、`channel=48` |
| CAN 工具 | `slcand` / `candump` / `cansend` | `apt install can-utils` |
| 串口 | `Canable` → `/dev/ttyACM0`（重插会变 ACM1） | CDC ACM 免驱 |

### 5.3 驱动补丁（作者 6 处 + 本地 2 处）

作者的 `ar1021x_driver` 已包含 6 处修改，其中最关键的是 **`wmi.h` 里 `wmi_connect_event` 字段顺序**：

```c
// ath6kl/wmi.h:1520 —— ap 分支必须是 phymode 在前、aid 在后
struct wmi_connect_event {
    union {
        struct { __le16 ch; u8 bssid[ETH_ALEN]; ... } sta;
        struct { u8 phymode; u8 aid; u8 mac_addr[ETH_ALEN]; ... } ap;   // ← 顺序关键
        ...
    }
};
```

**但仅靠这个还不够**——实测发现 AP 模式能连上、`aid=1` 也通过了，可 **AP→STA 单播仍然全丢**。

根因在 `cfg80211.c` 的 `ath6kl_change_station()`：hostapd 关联后传的 `flags_set=0x0`（清除授权位），
驱动走 else 分支发 `WMI_AP_MLME_UNAUTHORIZE`，固件把 STA 标记未授权 → **只放广播、丢单播**。

**本地两处 hack**（已打在 `~/work/dji-agras-radar/ar1021x_driver/` 里）：

1. `ath6kl/main.c` 的 `ath6kl_connect_ap_mode_sta()`，在 `netif_wake_queue()` 之后补一行：
   ```c
   ath6kl_wmi_ap_set_mlme(ath6kl_priv(vif->ndev)->wmi, vif->fw_vif_idx,
                          WMI_AP_MLME_AUTHORIZE, mac_addr, 0);
   pr_info("ATH6KL_AUTHORIZE aid=%u mac=%pM\n", aid, mac_addr);
   ```
2. `ath6kl/cfg80211.c` 的 `ath6kl_change_station()`，把 UNAUTHORIZE 分支改成跳过：
   ```c
   pr_info("ATH6KL_UNAUTHZ_SKIP mac=%pM\n", mac);
   return 0;
   ```

**持久选择补丁版**见 [Agent 部署工作流](AGENT-DEPLOY.md#3-分层验收与故障处理)。驱动装入当前内核的 `updates/t40-radar/`，分别验证 `modinfo -n` 解析路径和两个已加载模块的 `srcversion`。前者是下次加载的候选，后者才代表当前正在运行的构建。旧环境曾记录补丁版 core 为 `F2F6840BB20AAEAE7361468`、旧版为 `6D77789D37DFD186B50EF1E`，这些历史值不可用于判定其他内核的构建。

### 5.4 起 AP 并等雷达连上

```bash
# 1) 网卡起来 + 补 IP
sudo ip link set wlx00037f2044d2 up
sudo ip addr add 192.168.8.1/24 dev wlx00037f2044d2

# 2) 起 AP
cd ~/work/dji-agras-radar/setup
sudo ./start_ap.sh          # 内部 airmon-ng 未安装会报错，可忽略

# 3) 等 45–60 秒
ping -c 3 192.168.8.2       # 期望 3/3 通，13–31 ms

# 4) 看客户端
sudo hostapd_cli -p /run/hostapd_radar -i wlx00037f2044d2 all_sta
# 期望出现 60:60:1f:df:2f:b1  flags=[AUTH][ASSOC][AUTHORIZED] aid=1
```

> ⚠️ `192.168.8.1/24` 经常会在 hostapd 重启或 USB 重枚举后从接口上消失，
> 表现为 `ping` 100% 丢。**每次都要检查一遍 `ip -br addr`**，丢了就 `ip addr add` 补回。

### 5.5 CAN 起来

```bash
sudo slcand -o -c -s8 /dev/ttyACM0 can0     # 设备号可能是 ttyACM1
sudo ip link set can0 up
candump -t d can0                            # 期望：0A3 帧持续 8–10 fps
```

**CAN 通了就能控制电机**：

```bash
cd ~/work/dji-agras-radar/radar_client
sudo ~/work/venv/bin/python duml_can_tool.py send motor-speed --speed 900 --seq 1 --iface can0
# 回显 pay=84 03（0x0384 = 900）
```

### 5.6 假飞控（关键，决定侧阵是否工作）

```bash
setsid nohup ~/work/venv/bin/python ~/work/dji-agras-radar/radar_client/radar_min_client.py \
    --flight-mode 2 --v2-local-port 40205 > /tmp/rmc.log 2>&1 &
```

> 🔴 **绝对不要加 `--can-bridge can0`。**
> 雷达 app 自己每秒会发 `motor_speed` 命令（目标 900 rpm），`--can-bridge` 会把它转发到 CAN，
> 电机立刻冲 900 —— 这就是「雷达飞出去」的真正机制。
> `--flight-mode 2` 走 UDP 50007，不需要 CAN 桥。

日志里应该能看到：

```
[rx/v1] msg=0x1E62(avoidance_result_fc) len=116 ...
type=6 ver=0 flags=0x0005 btm=1.41m up=100.00m height=0.00m(kalman) conf=0 active_sectors=1/16
[rx/v1] msg=0x1E32(avoidance_result_alt) len=116 ...
```

---

## 6. 每次上电后的启动流程

### 6.1 顺序总表

| # | 在哪 | 命令 | 期望 |
|---|---|---|---|
| 1 | Windows | 雷达 USB + Canable 挂进 VM（虚拟机→可移动设备） | `lsusb` 见 `0cf3:1022` 和 `16d0:117e` |
| 2 | VM | `make load` | srcversion = `F2F68...` |
| 3 | VM | `ip link set` + `ip addr add 192.168.8.1/24` | `ip -br addr` 有 192.168.8.1 |
| 4 | VM | `sudo ./start_ap.sh` | hostapd `state=ENABLED` |
| 5 | VM | 等 45–60 s，`ping 192.168.8.2` | 3/3 |
| 6 | VM | `slcand` + `ip link set can0 up` | `can0 UP` |
| 7 | VM | `radar_min_client.py --flight-mode 2` | 日志有 1E62/1E32 |
| 8 | VM | `duml_can_tool.py send motor-speed --speed 900` | `cur_speed` 收敛 900 |
| 9 | VM | `python3 /tmp/fwd.py`（40204 转发） | 日志 `fwd #1 ... from 192.168.8.2:40204` |
| 10 | Windows | `python run.py --port 8010 --source udp` | 页面出现点云 |

### 6.2 Agent 自动部署

新部署使用 [Agent 部署工作流](AGENT-DEPLOY.md)。它处理应用服务、当前内核的 AR1021x 驱动、USB 热插拔 AP 和可选 UDP 飞控服务。启用 UDP 飞控时，监视器使用 40204，客户端使用 40205。真机部署完成后，由分层检查记录 USB、AP、关联和有效 1E61 的各自状态。


### 6.3 转速门槛（实测，务必知道）

| 转速 | 1E61 每秒点数 | 每帧点数 |
|---|---|---|
| 60 rpm | 48 | 0–1（几乎全空帧） |
| 300 rpm | 48 | 0–1 |
| 600 rpm | 97（统计值，实际仍空帧） | 0 |
| **900 rpm** | **1250–1479** | **84–98** |

**侧面 CFAR 只在 900 rpm 下工作。** 600 rpm 及以下时雷达内部 `sectors active = 0/360`，
1E61 全是 98 字节空帧；一到 900 立刻有数据。

> ⚠️ 900–1800 rpm 的旋转体**必须先固定**。纸箱+缓冲、扎带+木板、C 型夹都可以。
> 作者和本机都出过「雷达从桌面上飞出去」的事故。

---

## 7. 前后端代码结构

### 7.1 后端（`backend/`，纯标准库，零依赖）

| 文件 | 行数 | 职责 |
|---|---|---|
| `run.py` | 76 | 启动入口，解析 `--host/--port/--source/--open` |
| `backend/app.py` | 365 | `RadarApp`：tick 线程、SSE 广播、`UdpSource`、`ingest_udp()`、`set_motor()` |
| `backend/server.py` | 226 | `ThreadingHTTPServer` + SSE + 控制 API |
| `backend/track.py` | 265 | DBSCAN 聚类 + α-β 滤波（吃滑动窗口点云） |
| `backend/protocol/v2.py` | 254 | V2 帧、1E61、1E62 的打包/解析、**`UNIT_DM = 0.1`** |
| `backend/protocol/duml.py` | 121 | V1/DUML 帧 + CAN 分块 |
| `backend/protocol/crc.py` | 23 | CRC8（init 0x77 / poly 0x8C）、CRC16（init 0x3692 / poly 0x8408） |
| `backend/protocol/crc_tables.py` | 55 | 从作者 ROM 表抽取的查表 |
| `backend/sim/radar.py` | 287 | 模拟器：场景几何 + 射线投射 + 扫描 |

**关键代码：1E61 单位修正**（`protocol/v2.py`）

```python
# 1E61 的 x/y/z 原始数值实测为分米（0.1 m）单位：
#   实测帧点 x/y 落在 12–50，除 10 后 1.2–5.0 m，与 5 m 见方的室内场地吻合；
#   ground 点 z 落在 -4.1–0，除 10 后 -0.41–0 m，正好贴地平面 z≈0；
#   wire 点 z=1.545，除 10 后 0.15 m，线缆垂在雷达下方 15 cm。
UNIT_DM = 0.1

@dataclass
class Record1E61:
    id: int
    x: float
    y: float
    z: float
    metric: int

    @property
    def rng(self) -> float:
        return math.sqrt(self.x ** 2 + self.y ** 2) * UNIT_DM
```

**关键代码：转速控制**（`app.py` → `set_motor`）

```python
def set_motor(self, speed: float, dry: bool = False) -> dict:
    addr = os.environ.get("T40_MOTOR_BRIDGE", "192.168.59.140:50009")
    host, _, port = addr.partition(":")
    payload = json.dumps({"speed": max(0, min(1800, int(round(speed)))), "dry": dry}).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5.0)
    try:
        sock.sendto(payload, (host, int(port or 50009)))
        data, _ = sock.recvfrom(8192)
        res = json.loads(data.decode("utf-8", "replace"))
        if res.get("ok"):
            self.last_motor = speed
            self._save_motor(speed)      # 持久化到 .motor_state，重启不丢
        return res
    except socket.timeout:
        return {"ok": False, "error": f"motor bridge timeout ({addr})"}
    finally:
        sock.close()
```

### 7.2 前端（`frontend/`，无框架、无构建）

| 文件 | 行数 | 职责 |
|---|---|---|
| `frontend/index.html` | 135 | DOM 结构（暗色监控风） |
| `frontend/style.css` | 456 | 主题变量、grid 布局、面板样式 |
| `frontend/app.js` | 488 | Canvas PPI 渲染、SSE 消费、控制交互 |

**核心渲染逻辑**（`app.js`）：

```js
const RANGES = [5, 10, 20, 50];
const LIFE_MS = 4000;

// 三种 subtype 分三遍画：地面最底、障碍最上
const SUB_STYLE = [
  { c: 'rgba(120,155,195,0.45)', w: 2.8, name: '地面' },
  { c: 'rgba(240,180,41,0.95)',  w: 3.8, name: '线缆' },
  { c: 'rgba(61,220,132,1.0)',   w: 4.6, name: '障碍' },
];

// 0° 朝上、顺时针；r 是米
const toScreen = (azDeg, rM) => {
  const a = (azDeg - 90) * Math.PI / 180;
  const rr = (rM / S.range) * R;
  return [CX + Math.cos(a) * rr, CY + Math.sin(a) * rr];
};
```

**转速滑块**（`app.js`）：

```js
rpm.onchange = async () => {
  const v = Number(rpm.value);
  const res = await control('motor', v);        // POST /api/control
  $('rpm-sub').textContent = res.ok
    ? `已下发 ${v} rpm${res.dry ? '（dry-run）' : ''}`
    : '下发失败：' + (res.error || '未知错误');
};
// 每 3 秒轮询 /api/state，外部改了速度时同步显示（拖滑块时不覆盖）
setInterval(async () => {
  if (document.activeElement === $('rpm')) return;
  const st = await (await fetch('/api/state')).json();
  ...
}, 3000);
```

### 7.3 辅助脚本（`tools/`）

| 文件 | 行数 | 用途 |
|---|---|---|
| `tools/motor_bridge.py` | 64 | **VM 内以 root 常驻**：UDP 50009 → `duml_can_tool` → CAN |
| `tools/forward_40204.py` | 31 | **VM 内常驻**：40204 转发到 Windows（192.168.59.1） |
| `tools/radar_udp_sender.py` | 263 | 单文件 UDP 上行发送器（手机 Termux 可跑，没雷达时验证链路） |
| `tools/draw_wiring.py` | 231 | 画接线图 |
| `tools/fetch_termux_pkgs.py` | 202 | Termux 包抓取 |

`motor_bridge.py` 核心（全文 64 行，见 `tools/motor_bridge.py`）：

```python
LISTEN = ("0.0.0.0", 50009)
CLIENT_DIR = "/home/ubuntu/work/dji-agras-radar/radar_client"
PY = "/home/ubuntu/work/venv/bin/python"
IFACE = "can0"

def run_motor(speed: int, dry: bool) -> dict:
    global seq
    seq += 1
    if dry:
        return {"ok": True, "dry": True, "speed": speed, "seq": seq}
    cmd = [PY, "duml_can_tool.py", "send", "motor-speed",
           "--speed", str(speed), "--seq", str(seq), "--iface", IFACE]
    r = subprocess.run(cmd, cwd=CLIENT_DIR, capture_output=True, timeout=10)
    out = (r.stdout + r.stderr).decode("utf-8", "replace").strip()
    return {"ok": r.returncode == 0, "speed": speed, "seq": seq,
            "rc": r.returncode, "out": out[-400:]}
```

`forward_40204.py` 全文（31 行）：

```python
LISTEN = ("0.0.0.0", 40204)       # 雷达发到这个（VM 的 192.168.8.1）
DEST = ("192.168.59.1", 40204)    # Windows 宿主侧地址
```

### 7.4 代码外的东西（不在仓库里）

| 内容 | 位置 | 说明 |
|---|---|---|
| 雷达上的 `radar_streamer` | `/tmp/radar_streamer`（45 KB，md5 `10249b3e...`） | 作者仓库编译产物，读内存发 UDP |
| 雷达上的 `radar_min_client.py` | `~/work/dji-agras-radar/radar_client/` | 假飞控 |
| 驱动 `.ko` | `~/work/dji-agras-radar/ar1021x_driver/ath6kl/ath6kl_core.ko` | 8.7 MB |
| `.motor_state` | `t40-radar/.motor_state` | 后端记录的最近转速 |

---

## 8. 数据流与 HTTP API

### 8.1 完整链路

```
雷达 Zynq (192.168.8.2)
  ├── UDP 40204 ──▶ VM 192.168.8.1:40204
  │                    │ tools/forward_40204.py
  │                    ▼
  │              Windows 192.168.59.1:40204
  │                    │ backend/app.py: ingest_udp() → parse_1e61()
  │                    ▼
  │              SSE /events ──▶ 浏览器（frontend/app.js）
  │
  ├── UDP 9876/9877（顶阵/侧面原始，radar_streamer）──▶ radar_viewer.py（可选）
  ├── UDP 50007（V1 控制面）◀── radar_min_client.py（假飞控）
  └── CAN 0x0A2/0x0A3 ◀──▶ duml_can_tool / motor_bridge
```

### 8.2 HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/events` | **SSE 实时帧**（前端唯一数据源） |
| GET | `/api/state` | 状态：`running/frames/crc_ok/source/rpm/motor_speed/height_cm`，UDP 模式另有 `data_status` |
| GET | `/api/protocol` | 示例协议字段与 hex |
| GET | `/api/snapshot` | ⚠️ **sim 合成数据**（离线快照用，不是真机） |
| POST | `/api/control` | `{action, value, dry}` |

`POST /api/control` 支持的 action：

| action | value | 行为 |
|---|---|---|
| `start` / `stop` / `toggle` | — | 启停（sim 模式） |
| `rpm` | 0–1800 | 改 sim 转速（不碰真机） |
| `motor` | 0–1800 | **真机转速**：→ UDP 桥 → CAN → ESC |
| `scene` | room/field/wire/obstacle | 切 sim 场景 |
| `height` | 0–300 | 雷达离地高度（cm） |

### 8.3 SSE 帧结构

```json
{
  "type": "frame",
  "t": 123.456,
  "source": "udp",
  "data_status": "live",
  "data_epoch": 0,
  "angle": null,
  "rpm": null,
  "running": null,
  "height_cm": null,
  "gated": null,
  "points": [[291.28, 4.56, -0.507, 0.0, 0], ...],
  "tracks": [...],
  "sectors": [...],
  "proto": {"msg": "0x1E61", "seq": 29902, "len": 130, "count": 2, "crc_ok": true, "hex": "..."}
}
```

`points` 每项为 `[方位角°, 水平距离m, 相对雷达高度m, 置信度0-1, subtype]`，subtype `0=ground / 1=wire / 2=pcla`。

UDP 模式的 `data_status` 为 `waiting`（尚无有效 1E61）、`live`（有新鲜的有效帧）或 `stale`（2 秒未收到有效帧）。空点云帧通过校验后仍可维持 `live`。`stale` 清空 `points`、`tracks` 和 `proto`。SSE 的传输连接与数据状态独立，客户端只保留最新一条待发帧。每次过期令 `data_epoch` 加一，前端可借此清除上一段数据，即使中间的 `stale` 帧被较新的帧替换。

---

## 9. 协议要点（含单位修正）

### 9.1 线格式

**V2 帧**（UDP 40204，27 B 头 + payload + 2 B CRC16）：

```
+0       0x55 SOF
+1       低 6 位 = 0x18
+2       (byte2 & 0xFC) == 0x08
+3..4    u16 total_len（含头与 CRC）
+7..10   u32 seq
+12..13  endpoint1（perception = 0x0012）
+18..19  endpoint2（perception = 0x0004）
+23      header CRC8（覆盖 +0..+22）
+24..25  msg，大端（0x1E61）
+27..    payload
-2..-1   CRC16
```

**0x1E61 perception** = 69 B 头 + N × 16 B 记录：

```
头 +3..4   记录数 N (u16)
头 +65..66 subtype：0=ground / 1=wire / 2=pcla
记录 <H f f f H> = id(u16), x(f32), y(f32), z(f32), metric(u16)
```

V2 声明总长度必须与 UDP 报文长度一致。接收 1E61 时要求双 CRC 校验通过，payload 长度必须等于 `69 + 16 × N`。模拟几何使用米，线上编码用分米，解码为米后进入跟踪和显示。

**0x1E62 避障结果**（116 B）：16 个扇区项（方位/仰角/距离）+ 卡尔曼高度 + 融合高度 + 置信度。

### 9.2 ⚠️ 单位修正（最重要的一条）

**1E61 的 x/y/z 是分米（0.1 m），不是米。**

实测依据：

| 证据 | 原始值 | ×0.1 |
|---|---|---|
| 平面距离 | 12–50 | **1.2–5.0 m**（5 m 见方房间） |
| ground 点 z | −4.1–0 | **−0.41–0 m**（贴地平面） |
| wire 点 z | 1.545 | **0.15 m**（线缆垂在雷达下方 15 cm） |

作者 `radar_min_client.py` 的字段名虽写作 `x_m`，但他的注释「xy range ±2..6 m」同样指向这个比例。

**不修正的后果**：点云全部落在 10 m 以外，而被 `pcla_build_and_send_perception_msg_1E61`
的「距离 < 2 m 丢弃」规则过滤掉 → 障碍物点永远是 0。

### 9.3 三个关键约束

**① 转速门槛**：侧面 CFAR 要 900 rpm 才工作（见 §6.3）。

**② flight_mode=2**：侧阵天线要飞控状态为 2 才开。靠 `radar_min_client --flight-mode 2` 写进去。

**③ 1.6 m 高度门控**：`re_notes.md` 记录的 `sub_5494C` 逻辑——

```c
if (byte_1724C6 == 2) {
    if (++word_CACDEB2 > 300) word_CACDEB2 = 301;   // 约 50 分钟后永久旁路
    else if (flt_CACBDB0 < 1.6) {                    // 融合高度 < 1.6 m
        memset(avoidance_data, 0, ...);
        memset(sector_data, 0, ...);
    }
}
```

实测本机 `word_CACDEB2 = 0x012D = 301`（已旁路），所以**不是**点云稀疏的原因。

### 9.4 关键内存地址（app.elf 虚拟地址）

| 地址 | 十进制 | 内容 |
|---|---|---|
| `0xC2CF824` | 204273700 | 转角：`A/B/0x32FA/C`，实测 344/360/344 |
| `0xC442F90` | 205795216 | 侧面点计数（u16） |
| `0xCACBDB0` | 212647344 | 融合高度 float（实测 0.0，卡尔曼没工作） |
| `0xCACDEB2` | 212655794 | 高度门控计数器（实测 301） |
| `0x283295` | 2634389 | `g_flight_mode_from_fc`（实测低字节 0x02） |
| `0xCD37CC0` | — | 顶阵峰值表 |
| `0xCD40330` | 215221040 | 顶阵 32×64 热图（float32） |
| `0xCD44330` | — | 顶阵快照（0x224B） |
| `0x43C30000` | — | FPGA 控制块（物理地址） |
| `0x1C100000` | — | CFAR ring 8 MB（物理） |
| `0x1C900000` | — | FFT 复数输出 ~72 MB（物理） |
| `0x18100000` | — | udmabuf0 物理起点（96 MB，app vaddr `0xB0400000`） |

**读法**（`0xCxxxxxxx` 是**进程虚拟地址**，`devmem` 读不到）：

```bash
ssh root@192.168.8.2
dd if=/proc/1076/mem bs=1 skip=204273700 count=8 2>/dev/null | busybox od -x
```

雷达上没有 `devmem` 符号链接（`/sbin` 不在 PATH），busybox 的 `od` 不支持 `-A/-t`。

---

## 10. 故障排查

### 10.1 USB 枚举

| 现象 | 原因 / 处置 |
|---|---|
| 电脑零反应（无叮咚、无 PnP） | D+/D− 插在公头侧那排（§4.2）；换到母座侧 |
| 同上 | USB 黑线没接（没共地） |
| 同上 | USB 口坏 / 杜邦未插到底 |
| 枚举出来但反复掉 | 杜邦接触不良；作者的坑是「不扣金属盖 USB 极不稳定」 |
| `lsusb` 有但没网卡 | `make load` 没跑，或固件不在 `/lib/firmware/ath6k/AR6004/hw3.0/` |

### 10.2 网络

| 现象 | 原因 / 处置 |
|---|---|
| `ping 192.168.8.2` 100% 丢 | `192.168.8.1/24` 从接口消失了 → `ip addr add` 补回 |
| ping 通但 1E61 全空 | 转速不够（< 900 rpm），见 §6.3 |
| ping 通但没 1E62 | `radar_min_client --flight-mode 2` 没跑 |
| 单播不通、广播通 | 驱动补丁没生效（srcversion 是旧值） |

### 10.3 数据

| 现象 | 原因 / 处置 |
|---|---|
| 页面 0 点 | `frames` 还在涨？停掉 VM 的 `fwd.py` 看 `frames` 是否冻结，验证数据源 |
| 点在 10–50 m | 单位没修正（§9.2） |
| 障碍物永远 0 | 同上；修正后近处 pcla 才出得来 |
| 转速显示「尚未下发」 | 后端重启后读 `.motor_state`；文件丢了就重发一次 |
| 前端改了没生效 | 换 `?v=N` 版本号，浏览器缓存很顽固 |

### 10.4 飞车（最危险）

| 现象 | 原因 / 处置 |
|---|---|
| 电机突然冲 900 rpm | `radar_min_client` 带了 `--can-bridge can0` → 把雷达自己发的 motor_speed 转发到 CAN |
| 处置 | 杀掉 rmc：`sudo kill <pid>`；ESC 会保持最后目标，再发 `motor-speed --speed 0` 或断电 |

---

## 11. 已知限制与后续方向

### 11.1 当前限制

| 限制 | 说明 |
|---|---|
| 转速必须 900 | 低转速下侧面完全没数据，无法做低速演示 |
| 每帧只覆盖一个扇区 | 1E61 每帧 84–98 点集中在 22.5° 扇区，360° 需累积多帧 |
| 侧面 pcla 点少 | 需要真实障碍物进入波束 + 距离 ≥ 2 m |
| 顶阵未接入前端 | 数据链路现成（9876），只差解析 + 渲染 |
| 无录制/回放 | 可加：把 `/events` 落 JSONL 再回放 |
| 卡尔曼高度恒 0 | `flt_CACBDB0 = 0.0`，雷达这边没算出来，不影响点云 |

### 11.2 下一步可做

1. **顶阵热图接入 Web**：`radar_streamer top` → UDP 9876 → 转发 → 后端加 OMNI 解析 → 前端加 32×64 热图面板
2. **原始 CFAR 点云**：`radar_streamer side`（60632 B 帧）比 1E61 密得多
3. **记录与回放**：SSE 落盘，支持离线分析
4. **内核升级自动重编**：为已验证的驱动建立 DKMS 包，减少新内核上线时的手动步骤。开机自启目前见 §6.2。
5. **移动端适配**：前端已有 `--host 0.0.0.0`，手机可看

### 11.3 复用与出处

- 协议知识、内存地址、驱动修改：**https://github.com/leommxj/dji-agras-radar**
- 逆向过程记录：**https://www.leommxj.com/2026/04/20/dji-t40-RD2484R-radar-reverse-engineering/**
- 本工程代码：`E:\dsh\t40-radar\`（独立实现，未复制作者代码）
- 本文档写于 2026-11-15，对应代码版本：后端 365 行 / 前端 488 行 / 协议层 254 行

---

## 附录 A：快速自检清单

在任何时候怀疑「链路断了」时，按顺序验证：

```bash
# 1) VM 里
lsusb | grep -c '0cf3:1022'                 # 1 = 网卡在
cat /sys/module/ath6kl_core/srcversion       # F2F68... = 补丁生效
ip -br addr | grep wlx                       # 有 192.168.8.1/24
ping -c 3 192.168.8.2                        # 3/3
ip -br link | grep can0                      # UP,LOWER_UP

# 2) 雷达上（ssh root@192.168.8.2）
ps | grep app.elf                            # PID 记下来
ps | grep radar_streamer                     # top/side 在跑

# 3) VM 转发
ps -ef | grep fwd.py                         # 在跑
tail -3 /tmp/fwd.log                         # 有 fwd #N ... from 192.168.8.2:40204

# 4) Windows
python -c "import urllib.request,json;print(json.load(urllib.request.urlopen('http://127.0.0.1:8010/api/state')))"
# frames 在涨 = 数据在流
```

## 附录 B：关键值速查

| 项 | 值 |
|---|---|
| 雷达 IP / MAC | `192.168.8.2` / `60:60:1f:df:2f:b1` |
| VM 网卡 MAC | `00:03:7f:20:44:d2`（接口名 `wlx00037f2044d2`） |
| VM IP | `192.168.8.1`（AP 侧）、`192.168.59.140`（宿主侧） |
| Windows 宿主 IP | `192.168.59.1` |
| 雷达 SSH | `ssh -p 107 root@192.168.8.2`，密码 `root` |
| UDP 端口 | `40204` 数据上行、`50007` V1 控制、`9876/9877` streamer、`50009` 转速桥 |
| CAN ID | `0x0A2` 发（飞控→ESC）、`0x0A3` 收（ESC→飞控） |
| 雷达上日志 | `/tmp/rs.log`（streamer）、`/tmp/rmc.log`（假飞控） |
| 转速上限 | 1800 rpm（`duml_can_tool` 限制），工作转速 **900** |
