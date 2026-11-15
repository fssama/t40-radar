# T40 雷达逆向工程 — 会话交接提示词

> 把本文件整篇作为新对话的第一条消息发给 AI，即可无缝续接。
> 生成时间：2026-11-13

---

## 0. 你的角色

你是一个帮助用户做**实体硬件逆向工程**的技术助理。用户是一台 Windows 电脑（`E:\dsh` 为工作目录）+ 一台 Ubuntu 虚拟机 + 一台 Android 手机（iQOO 13，已 root/KernelSU）。

**沟通铁律**：
- 全程**简体中文**，代码/路径/命令保留英文
- 用户明确要求过：**少论证、多实测**。能用 5 分钟验证的，不要写 500 字分析
- 每一步只给**能立刻动手**的指令，不要一次抛一堆

---

## 1. 项目目标

读懂并驱动**大疆 Agras T40 农业无人机的避障雷达 RD2484R**（二手件，已到手）。

**目标分层**（用户当前在 A→B 之间）：
- **A**：让雷达能输出它自己的业务数据（避障点云）
- **B**：拿到更底层的原始数据（CFAR 记录 / FFT 输出）
- **C**：理解 FPGA 与射频前端，实现"任意波形" ← 已判定**不可行**，不再尝试

---

## 2. 硬件事实（已确认）

### 雷达 RD2484R 本体
- 圆柱形，直径 ~10cm，高 ~12cm
- **上下两块板无物理连线**：线圈无线取电 + WiFi 通信
- **下板 ESC**：GD32F305RCT7 + MP6536 电机驱动 + MAX3051 CAN 收发 + TP75176 RS485 + MPU-6600 IMU + **AR1021x USB WiFi 网卡**
- **上板 Digital**：Zynq XC7Z020 + AFE5401 射频前端 + W25Q512JV Flash，跑 Linux，业务主体 `/home/root/app.elf`（ARM no-PIE，base `0x8000`）
- 天线：
  - **顶面 4×4**：SG24TR12 + LMX2491，2R1T 只接 1T+1R → 等效单站，FOV 45°，1.5–30m，**纯电子扫描，不需要转**
  - **侧面 12×6**：A1024 + TW7112，4R2T 只接 1T + 4R → **机械旋转 900rpm**，360 扇区 → 1° 方位分辨，仰角靠 12 元垂直阵 DBF（~10°）
- 频率 24.05–24.25 GHz（FCC），固件 `ag601` `V01.03.1005`

### Type-C 接口（**关键**）
- 雷达上是 **Type-C 公头（插头）**，实测金属壳宽 **8.0mm**（标准 8.25mm）→ 标准件
- 引脚定义（用户实测逆向，插座视角）：
  ```
  A 排：A1 GND | A4 VCC | A5 GND | A9 VCC | A12 GND | A2=B A3=A (RS485)
  B 排：B1 GND | B2 CAN_H | B3 CAN_L | B5 GND | B6 D+ | B7 D− | B12 GND
  ```
- **VCC 只在 A 面（A4/A9）→ 必须正插**，反插完全没电（但不烧）
- **A5 被标成 GND**（标准是 CC1）→ **雷达没有 PD 协议**，不能用 PD 诱骗器，只能用固定 15V 适配器
- 雷达需 **15V 供电**（实测功耗估 12–18W）

---

## 3. 已完成的工作（按时间）

### 3.1 逆向资料研究（完成）
- 已读完作者 leommxj 的博客 + 仓库全部文档
- 仓库本地副本：`E:\dsh\_repos\dji-agras-radar`
- **作者博客**：https://www.leommxj.com/2026/04/20/dji-t40-RD2484R-radar-reverse-engineering/
- **作者 YouTube 视频**（已下载分析）：`E:\qq\xiazai\dji t40 radar client test.mp4`
- 与作者本人联系过：**他用的是同样的 24P 转接板**；**FFT 输出格式他没有进展**

### 3.2 软件栈（100% 就绪，已实测）
- **Ubuntu 虚拟机**：`D:\VMs\ubuntu-25.10\ubuntu-25.10.vmx`，IP `192.168.59.140`，账号/密码均 `ubuntu`
- 内核 `6.17.0-41-generic`（已 `apt-mark hold` 锁定）
- `ar1021x_driver` 已编译安装：`/lib/modules/6.17.0-41-generic/updates/ath6kl_core.ko` + `ath6kl_usb.ko`
- hw3.0 固件已部署：`/lib/firmware/ath6k/AR6004/hw3.0/` 下 `bdata.bin` / `fw-5.bin` / `fw.ram.bin`
- `hostapd` / `iw` / `slcand` / `candump` / `cansend` 全部就位
- Python venv：`~/work/venv`（python-can 4.6.1 + numpy）
- 仓库代码：`~/work/dji-agras-radar/`
- **`radar_min_client.py --sim-esc` 已跑通**（1475 行日志，状态机推进正常）
- ⚠️ 硬约束：`--sim-esc` 与 `--can-bridge` **互斥**

### 3.3 CAN 链路（**实测已通**）
- **CANable 实物**：`Makerbase CANable V2.0`，`lsusb` 显示 `16d0:117e MCS CANable2 b158aa7 github.com/normaldotcom/canable2.git`
- **是 CDC ACM 方案（不是 CH340）**，内核自带 `cdc_acm`，免驱
- 虚拟机内：`/dev/ttyACM0` 出现，`slcand -o -c -s8 /dev/ttyACM0 can0` 成功
- `ip -details link show can0` → `state UP`，`bitrate 1000000`
- **回环测试通过**：`cansend can0 789#AABBCCDD` → `candump can0` 收到 `can0 789 [4] AA BB CC DD`
- 已创建 systemd 服务 `/etc/systemd/system/can0.service`（`enabled`，开机自动拉起 can0）
- CANable 端子丝印（从上到下）：**GND / CANH / CANL**，120Ω 拨码 `OFF|ON`

### 3.4 监控系统（模拟器版，已可用）
- 代码：`E:\dsh\t40-radar\`
- 零依赖（Python 标准库 + 原生 JS），`python run.py --port 8010`
- **数据走真实协议栈**：模拟器 → `Record1E61` → `build_1e61()` → `build_v2()` → `parse_v2()` → JSON
- 前端：浅色纸面风格（仿作者视频），极坐标点云 + 跟踪列表 + 俯仰剖面 + 协议帧 hex
- 支持 `--source sim`（模拟）/ `--source udp`（收真实 UDP 40204）/ `?snapshot=N`（离线截图）
- **跟踪层已实现**：`backend/track.py`，DBSCAN 聚类（eps 0.5m）+ α-β 滤波，实测能跟踪 1.2m/s 走动的人

### 3.5 采购（¥84.74 那单）
| 件 | 价格 | 状态 |
|---|---|---|
| 15V 3A 电源（京东） | ¥22.72 | 已付，已到货 |
| DC母转C公线（淘宝） | ¥2.30 | 已付，**已作废**（雷达是公头，方向反） |
| 24P 公转母座测试板（顺豹 规格9） | ¥8.50 | **已到货** |
| USB转杜邦 AM 款 | ¥5.45+3 | **已到货** |
| 杜邦线母对母 21cm ×40 | ¥3.36 | **已到货** |
| MKS CANable V2.0 S | ¥61.49 | **已到货** |
| **DC 5.5母座 转 杜邦带孔（芯创力）** | **¥2.94** | **明天到** |

---

## 4. 24P 转接板实测丝印（**重要**）

用户实拍的板子（顺豹科技「TYPE-C公母头测试板 规格9 公转母座 双排竖针24P」）：

```
A 面（12 针，俯视从左到右）：
  GND | A2 | A3 | VCC | A5 | ... | VCC | A11 | ...

B 面（12 针，俯视从左到右）：
  GND | B11 | B10 | VCC | B8 | D- | D+ | B5 | VCC | B3 | B2 | GND | SGND
```

**接线规则（已验证方向正确）**：
| 用途 | 板丝印位置 | 接什么 |
|---|---|---|
| 15V+ | A 面第 4 个（`VCC` = A4） | 红线 |
| 15V− | A 面第 1 个（`GND` = A1） | 黑线 |
| USB D− | B 面第 6 个（丝印 `D-`） | USB转杜邦 **白** |
| USB D+ | B 面第 7 个（丝印 `D+`） | USB转杜邦 **绿** |
| USB GND | B 面第 12 个（`GND`，紧挨 SGND） | USB转杜邦 **黑** |
| CAN_H | B 面丝印 `B2` | 橙 |
| CAN_L | B 面丝印 `B3`（在 B2 左边） | 蓝 |
| CAN GND | B 面 `B12` 或另一个空 GND | 黑 |

⚠️ **USB转杜邦线的红线（5V+）必须包死/剪断** —— 板上 VBUS 是雷达的 15V，接上会 15V 倒灌烧电脑主板。

---

## 5. 当前卡点（TASK AS OF THIS BLOCK）

### 已做但失败：**无 15V 时测雷达 USB**
- 接法：白→B6(D−)、绿→B7(D+)、黑→B1(GND)，红线包死
- 雷达 C 公头插入板母座，USB-A 插电脑
- **结果**：Windows **完全没有反应**（无叮咚声、无 PnP 事件、`Get-PnpDevice` 查不到 `0CF3`）
- 精确检查：`Get-WinEvent -LogName "Microsoft-Windows-Kernel-PnP/Configuration"` 最近 8 条里**没有雷达的任何事件**

### 判断
**没 15V 测不出来是正常的**，原因二选一：
1. **15V 控制 USB PHY 供电**（最可能，60%）—— 芯片没上电，没有 D+ 上拉，电脑零反应
2. D+/D− 或 GND 物理断开（30%）—— 需要万用表通断档验证
3. 24P 板 B 面走线问题（10%）

**Grep 全仓库 + 博客：关于 VBUS/供电拓扑零处记载**，只能靠实测。

### 待办（明天 DC 线到货后）
```
① 板 A4 接红、A1 接黑（15V 来自 DC 母转杜邦线）
② 板 B6 接白、B7 接绿、B1 接黑（USB，已接好）
③ USB-A 插电脑
④ 最后插 15V 电源
⑤ 预期：LED 亮 + 上半部分 60rpm 慢转 + 电脑叮咚 + lsusb 出现 0cf3:1022
⑥ VMware 菜单把 USB 设备挂进 VM
⑦ VM 里 `sudo modprobe ath6kl_usb` → `dmesg | grep "hw 3.0"` → `ip link | grep wlx`
⑧ `sudo ./setup/start_ap.sh` 起 radar_wifi 热点 → 等雷达连上 → ping 192.168.8.2
⑨ `ssh -p 107 -oHostKeyAlgorithms=+ssh-dss root@192.168.8.2`（密码 root）
⑩ `radar_viewer.py --deploy` 看顶阵热图（这一步不需要 CAN）
```

---

## 6. 关键地址与技术细节（逆向成果，备用）

### 6.1 UDP 协议
- **50007** = V1 控制面；**40204** = V2 上行（1E61 perception + 0xF000 blackbox）
- `1E61` payload = 69B 头 + N×16B 记录；头 `+65` = subtype（0=ground / 1=wire / 2=pcla）；记录 `<H f f f H>` = id, x, y, z, metric
- `1E62`/`1E32` = 116B 避障结果（16×6B 扇区项 + 高度融合）
- CRC8 init `0x77`，CRC16 init `0x3692`

### 6.2 内存与 FPGA（自研采集用）
- FPGA 控制块 `0x43C30000`（4KB MMIO）
- `udmabuf0` 物理 `0x18100000`，96MB，app vaddr `0xB0400000`
- **CFAR ring** 物理 `0x1C100000`（8MB），记录 0x800B/条
- **FFT 复数输出** 物理 `0x1C900000`（约占 72MB）
- 状态变量：
  - `0xC2CF824` rot_angle + sectors_decoded
  - `0xC442F90` 点计数
  - `0xCACBDB0` 卡尔曼高度
  - `0xCD37CC0` 顶阵峰值表
  - `0xCD40330` 顶阵 32×64 热图（float32）
  - `0xCD44330` 顶阵快照（0x224B）
  - `0x283295` `g_flight_mode_from_fc`（写 2 = 激活侧面天线）

### 6.3 作者工具
- `radar_streamer top <PID> <PORT> <interval_ms>` —— 8908B/帧，顶阵热图+快照+峰值表
- `radar_streamer side <PID> <PORT> <interval_ms>` —— 60632B/帧，360×160B omni_snapshot + 250×12B body_xyz
- `radar_viewer.py --deploy` 会自动 `Found app.elf PID` 并拉起两个 streamer
- `radar_mem_poke.py activate/pm0/pm3` —— ptrace 写内存
- `duml_can_tool.py list/monitor/send/send-can/shell` —— motor-speed 上限 1800rpm

### 6.4 未解的技术问题
- **FFT 输出格式**（`0x1C900000`）：作者没做，需自己试。线索：顶阵表格里 `str=11234 / thr=30978` 是**整数** → 大概率是 **int16/int32**，不是 float32
- **`0x1EFD` 通道**：`radar_link_protocols.md:325` 记载有"单天线单波束 256 bin 距离幅度谱"UDP 包，由 `0x1EFC` 触发，**格式已全部逆向清楚**，但需要主机主动请求，尚未验证是否可用

---

## 7. 环境工具备忘

### 虚拟机操作（vmrun）
```bash
D:/vm/vmrun.exe -T ws start "D:\VMs\ubuntu-25.10\ubuntu-25.10.vmx" nogui
D:/vm/vmrun.exe list
```
- 封装工具：`E:\dsh\_vm\vmctl.py`，用法 `python vmctl.py exec "命令"` / `push 本地 远端` / `pull 远端 本地`
- **坑①**：`runProgramInGuest` **不能用 shell 变量**（`$T` 会空），要 push 脚本文件再 `bash`
- **坑②**：`CopyFileFromHostToGuest` 的宿主机路径**必须正斜杠**，且要 **`MSYS_NO_PATHCONV=1`**
- **坑③**：`sudo` 是 sudo-rs，`-n` 会失败，要用 `echo ubuntu | sudo -S -p ''`
- **坑④**：`vmrun` **不能挂载 USB 设备**，必须在 VMware GUI 里点「虚拟机 → 可移动设备」

### Android 手机（adb）
- `adb` 在 `C:\adb\platform-tools\adb.exe`，设备 `10AEAT261N001DL`
- **有 root**（KernelSU），`su -c 'cmd'`
- **坑**：`MSYS_NO_PATHCONV=1 adb push src '//sdcard/x'`（双斜杠）
- 手机**没有** `iw` / `hostapd` / `ath6kl`，内核 `CONFIG_CFG80211 is not set` → **当不了雷达主机**
- 唯一热点入口：`cmd wifi start-softap radar_wifi open`（隐藏命令，强制 2.4GHz，没法选 5GHz ch48）
- 热点 KernelSU 模块已写在 `E:\dsh\radar-ap-module\`（`start.sh` / `probe.sh` / `stop.sh`）
- **实测**：手机软 AP 能广播出 `radar_wifi`，电脑能扫到，日志有 `num_connected_clients=1`

### 手机端 DSH 联动（interconnect）
- `instanceId="dsh"`，`sessionId="session-0a882c9a-684e-4099-bfdb-59dbe9b64e10"`
- 用 `interconnect_send` 派活给它（它负责电商下单、屏幕操作等我看不到的界面）

---

## 8. 重要经验与教训

1. **用户的钱包状态**：已花 ¥84.74 + ¥25.02，对价格敏感但愿意为该花的钱花
2. **用户没有万用表**（曾说过），所以**别给需要测量的方案**
3. **用户不想焊接**（明确说过"0焊接"），优先给免焊方案
4. **电商平台全部反爬**：京东搜索「访问频繁」、商品页 403 频控；拼多多「系统繁忙」；淘宝弹滑块。**只能让用户或手机端人工搜**
5. **`github.com` 直连不通**，但 `codeload.github.com`、`api.github.com` 可以
6. **`pip install` 无外网索引**（`Could not find a version that satisfies the requirement`），所有 Python 包必须离线装
7. **作者踩过最大的坑**：不扣金属盖时 USB 极不稳定 → 如果将来枚举时有时无，**先怀疑外壳干扰，别怀疑驱动**
8. **作者犯过的 AI 错误**：opus 判定射频滤波器"短路损坏"，让他焊掉 → 结果滤波器是好的。**别轻易建议拆改射频元件**

---

## 9. 下一步该做什么（按优先级）

### P0（明天 DC 线到货后立即做）
1. 接 15V，验证：
   - LED 亮
   - 上半部分 60rpm 慢转
   - 电脑叮咚 + `0cf3:1022` 出现
2. 挂进 VM，`modprobe ath6kl_usb`，`dmesg | grep "hw 3.0"`
3. `start_ap.sh` 起 AP，等雷达连上，`ping 192.168.8.2`
4. SSH 进去，`radar_viewer.py --deploy` 看顶阵热图（**不需要 CAN**）

### P1（顶阵通了之后）
5. `duml_can_tool.py --can-bridge can0` 设 motor-speed 到 900rpm
6. 用 `radar_min_client.py --can-bridge can0` 跑假 FC
7. `candump can0` 应看到 `0x18` 的 DUML 帧

### P2（侧面点云通了之后）
8. 接 `radar_streamer side`，看侧面 360° 点云
9. 把真实数据接进 `E:\dsh\t40-radar\` 的前端（`--source udp`）

### P3（可选，高难度）
10. 试 `0x1EFD` 通道（FFT 结果 UDP 包，格式已知）
11. dump `0x1C900000` 猜 FFT 格式（先试 int16）

---

## 10. 一句话现状

**软件栈 100% 就绪并实测通过（CAN 回环已通），硬件接线方案已完全确定，只差一根 ¥2.94 的 DC 母座转杜邦线（明天到货）。**

到货后按第 5 节的 10 步走，2 小时内应该能看到第一张雷达图。
