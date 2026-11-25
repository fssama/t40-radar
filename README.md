# RD2484R 雷达监控系统（T40 / DJI Agras）

大疆 T40 农业无人机 **RD2484R 24 GHz 有源相控阵雷达**的前后端监控系统。
使用 Python 标准库和原生 JavaScript，接收实机上报的 1E61 点云与 1E62 避障结果。

> 协议逆向参考 [leommxj/dji-agras-radar](https://github.com/leommxj/dji-agras-radar)（作者公开了
> CAN/DUML、UDP V1/V2 帧格式与内存导出脚本）。本工程是独立实现，未复制其代码。
>
> 📘 **完整工作流（购物清单 / 接线教程 / 部署 / 协议 / 排错）见 [docs/WORKFLOW.md](docs/WORKFLOW.md)。**

---

## 1. 监控页面

全向点云支持自动量程、固定量程和地面、线缆、障碍图层开关。右栏显示最近障碍物、16 个扇区、融合高度、航迹和原始帧。电机控制折叠在读数下方，填入目标转速并提交后才下发。

航迹由 1E61 障碍点云生成。每条航迹需要三次独立观测才能确认，预测中的航迹会保留原 ID 并标注状态。在 1.25 秒内没有新观测时清除航迹。

## 2. 快速开始

```bash
python run.py                 # 本机浏览器 http://127.0.0.1:8000
python run.py --open          # 启动后打开本机页面
python run.py --host 0.0.0.0  # 允许同一局域网访问
python run.py --port 8010     # 更改网页端口
```

使用 Python 3.10 或更新版本。默认监听 `0.0.0.0:40204`，可用 `--udp-host` 和 `--udp-port` 调整。未收到实机数据时页面显示等待状态。

## 3. 真雷达到了之后

雷达上电后跑的是它自己的 Linux（Zynq），**它不会向你发送任何数据** ——
它原本是发给飞控的。所以需要一边扮演飞控（CAN + UDP 服务端），一边收数据。

完整链路（与原作者博文一致）：

```
雷达(Zynq) --WiFi radar_wifi--> 你的 Linux(ath6kl 驱动 + hostapd, 192.168.8.1)
    ^                                    |
    |--- CAN 1 Mbit (0x0A2 / 0x0A3) ------|  假飞控：发心跳/电机指令
    |--- UDP 50007 (V1 控制面) -----------|
    |--- UDP 40204 (V2 上行 1E61) ------->|  VM 转发到监控程序的 UDP 40204
```

要点：

1. **必须**用能跑 Linux 的机器（虚拟机可以，但 USB 要直通），Windows 原生不行 ——
   AR1021x 网卡全球只有 Linux `ath6kl` 驱动，CAN 也需要 SocketCAN。
2. 网卡驱动用 `ar1021x_driver/`（作者的 out-of-tree ath6kl，6 处修改），固件放
   `/lib/firmware/ath6k/AR6004/hw3.0/`。
3. 起 AP：`setup/start_ap.sh`，`hostapd.conf` 里 `ignore_broadcast_ssid` **必须注释掉**。
4. 侧阵点云依赖电机和编码器。1E62 到包时也可能只有占位扇区。若需调速，由现场通过 `duml_can_tool.py` 或电机控制表单操作。
5. 宿主机启动 `python run.py`。在 VM 里运行 `python3 tools/forward_40204.py` 转发 1E61，并用以下命令替代原先的 `radar_min_client.py`，以转发 1E62。两个客户端不能同时绑定 UDP 50007：

   ```bash
   python3 tools/radar_avoidance_relay.py ~/work/dji-agras-radar/radar_client 192.168.59.1
   ```

   1E62 转发器从第一帧起就使用 `flight_mode=2`，不接入 CAN。页面分别显示网页连接和雷达收包状态。1E61 点云与 1E62 避障结果独立判断时效，某一路 5 秒没收到有效帧时，页面会清除该路的旧结果。收到 1E62 但扇区全部为占位值时，「最近障碍物」显示「无有效距离」。

> ⚠️ 900–1800 rpm 的旋转体必须先固定，作者实测 1800 rpm 时雷达会"飞出去"。

排查固定方位缺点时，可以从雷达已运行的 `radar_streamer side`（UDP 9877）读取侧阵 OMNI v2 数据。以下命令在能直达雷达的主机上运行。

```bash
python3 tools/read_omni_sectors.py --host 192.168.8.2 --seconds 5 --capture side.omni --csv side.csv
python3 tools/read_omni_sectors.py --input side.omni
```

工具按机体方位统计原始 CFAR 回波，CSV 保留编码器槽位、方位、斜距和 SNR。默认把编码器槽位 0 对准 180°，可通过 `--offset` 校准。表中每 22.5° 一格，统计的是采样时的原始回波，和 `1E62` 的 16 个结果槽位各有用途。现有 `radar_streamer` 每个槽位最多导出 7 条回波，连续快照可能重复包含同一条记录。

雷达运行 `radar_streamer side` 时，在 Ubuntu 虚拟机运行以下命令即可把原始帧转发到 Windows 的监控程序。

```bash
python3 tools/read_omni_sectors.py --host 192.168.8.2 --forward-host 192.168.59.1
```

页面收到有效 OMNI 帧时会在极坐标图上显示紫色空心点。图例中的「原始回波」可切换这一图层。原始回波使用斜距，`1E61` 图层使用水平距离，两者可按方位对照。航迹和 `1E62` 避障卡片继续使用各自的雷达业务报文。

---

## 4. 协议实现

线格式（`backend/protocol/`）：

**V2 帧**（UDP 40204，雷达 → 飞控，27 B 头 + payload + 2 B CRC16）

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

**0x1E61 perception payload** = 69 B 头 + N × 16 B 记录

```
头  +3..4   记录数 N (u16)
头  +65..66 subtype：0=ground / 1=wire / 2=pcla
记录 <H f f f H> = id(u16), x(f32), y(f32), z(f32), metric(u16)
                  body 坐标系：前 x / 右 y / 上 z，原始单位 dm
                  metric = 置信度 × 65535
```

**0x1E62 避障结果**（UDP 50007，V1，116 B 载荷）：`+8` 为扇区头 `0x10`，`+9..104` 为 16 项连续的 `<HHH>`（方位百分度、仰角百分度、距离厘米）。`10000 cm` 是占位距离；`+109` 为单字节高度置信度，计数器在 `+115`。首扇区的 `40.96 m` 是旧解码器读错距离字段产生的假值。

**CRC**：CRC8 反射 poly `0x8C` init `0x77`；CRC16 反射 poly `0x8408` init `0x3692`。
查表位于 `backend/protocol/crc_tables.py`。`GET /api/protocol` 提供最近接收的两种帧的校验结果和前 40 字节，尚未收到的类型返回 `null`。

---

## 5. 目录

```
run.py                       启动入口
backend/
  app.py                     UDP 接收、航迹与 SSE 广播
  server.py                  ThreadingHTTPServer + SSE + 控制 API
  protocol/                  V1/V2 帧、1E61/1E62、CRC
frontend/                    index.html / style.css / app.js（无框架、无构建）
tools/forward_40204.py       VM 侧 1E61 转发
tools/radar_avoidance_relay.py VM 侧 1E62 转发
```

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 页面 |
| GET | `/events` | SSE 帧流 |
| GET | `/api/state` | `radar` 分别报告 1E61 和 1E62 的 `waiting` / `live` / `stale`。`running` 表示近期收到有效 1E61。1E62 未收到或已过期时 `avoidance` 为 `null` |
| GET | `/api/protocol` | 最近一次通过校验的帧及 hex，含各链路当前状态 |
| POST | `/api/control` | `{"action":"motor","value":转速}`，经 VM 桥下发 |

---

## 6. 已知限制

- 只实现了**接收**（1E61/1E62）。假飞控的 V1 控制面（50007）与 CAN DUML 不在本工程内，
  真机联调时仍需作者的 `radar_min_client.py` 或等价实现。
- 不带录制/回放（可作为下一步：把 `/events` 落 JSONL 再回放）。
- 顶面 4×4 阵的原始热图不在网络协议里，只能从雷达进程内存读，本工程未涉及。

---

## 7. 排错

| 现象 | 原因 |
|---|---|
| 页面打开全空 | 端口被占 / 后端崩了，看终端 traceback |
| SSE 一直"连接中" | 反向代理缓冲了 `text/event-stream`，或防火墙拦了 |
| 收不到雷达数据 | 发送端没发到 `40204`，或被 Windows 防火墙拦（首次会弹窗） |
| 手机连不上电脑 | `run.py` 要用 `--host 0.0.0.0`，且同一局域网 |
