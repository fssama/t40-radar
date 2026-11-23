# T40 Radar Agent 部署工作流

部署器 `sudo bash tools/deploy_ubuntu.sh` 面向 systemd Ubuntu，可重复运行。它安装本仓库的 Python 监视器、为指定 Ubuntu 内核编译并选择 AR1021x 模块、安装设备热插拔 AP 服务，并可选启动 UDP 飞控客户端。下表决定入口。

| 目标 | 入口 | 验收 |
|---|---|---|
| 无设备演练 | `monitor sim` | HTTP 200、source=sim、frames 增长 |
| 已有 40204 数据源 | `monitor udp` | HTTP 200、source=udp。收到有效帧时 data_status=live |
| Ubuntu 直连 AR1021x | `ar1021x …` | 补丁模块选中并实际加载、AP 启用、雷达关联。收到有效 1E61 时 data_status=live |

## 1. 确定部署输入

- 取得本仓库的明确提交，在 **目标 Ubuntu** 上执行。VMware 环境用宿主可达的管理网卡连接 Ubuntu，雷达 AP 使用单独直通的 AR1021x。Windows 只作为浏览端时无需再启动一套监视器。
- 真机配置需要 USB ID 为 0cf3:1022 的网卡、一份可信且经验证的 ath6kl 补丁源码、匹配的 **AR6004 hw3.0** 固件目录和雷达 **STA MAC**。MAC 来自该雷达的标签、既有配置或已知关联记录。不要把 AP 网卡的 MAC 当作雷达 MAC。
- 固件目录应有 bdata.bin，以及 fw-5.bin 或 fw.ram.bin。DRIVER_DIR 指向包含 Makefile 和 ath6kl/ 的 ar1021x_driver 目录。使用当前内核 uname -r 的头文件。
- COUNTRY 使用设备所在地的两位大写监管域代码。部署器保留既有的 radar_wifi、5 GHz 信道 48 和 192.168.8.1/24 配置。目标环境需能使用该信道。AP 只允许配置中的雷达 MAC 关联，部署在隔离网段。MAC 允许列表不能代替网络隔离。
- 可选 FC_CLIENT 指向经审查的独立 radar_min_client.py。启用时客户端使用 UDP 50007 和 40205，监视器独占 UDP 40204。需要已有飞控数据面时可省略该输入。

上游驱动基于 Linux 6.17。旧实验环境 Ubuntu 25.10 的兼容记录仅对应当时的内核，它已于 2026-07-09 结束维护。新部署选仍受支持的 Ubuntu，先用目标内核的头文件验证补丁能编译。上游的六处修改与本项目实验中用过的额外 AP 单播兼容补丁并不等价，选择已经在自己的台架验证过的补丁源码。见 [上游驱动说明](https://github.com/leommxj/dji-agras-radar/tree/main/ar1021x_driver)、[Ubuntu 25.10 结束维护公告](https://lists.ubuntu.com/archives/ubuntu-announce/2026-July/000325.html) 和 [历史工作流](WORKFLOW.md#53-驱动补丁作者-6-处--本地-2-处)。

## 2. 执行部署

在仓库根目录执行离线检查，再运行与目标匹配的一个部署命令。工具只对安装操作要求 root。Ubuntu 检查并安装缺少的依赖。若包仓库或当前内核头文件不可用，保留实际失败输出，修正目标机的软件源或头文件后重试。

```bash
python3 -m unittest discover -s tests
bash -n tools/deploy_ubuntu.sh
bash -n tools/t40_ap.sh
bash tools/deploy_ubuntu.sh --help

# 任选其一
sudo bash tools/deploy_ubuntu.sh monitor sim
sudo bash tools/deploy_ubuntu.sh monitor udp
sudo bash tools/deploy_ubuntu.sh ar1021x "$DRIVER_DIR" "$FIRMWARE_DIR" "$COUNTRY" "$RADAR_MAC" "$FC_CLIENT"
```

不启用 UDP 飞控时，省略最后一个参数。自动服务使用 --flight-mode 2 --v2-local-port 40205，部署流程不启动 CAN、电机桥或电机启动选项。任何需要机械动作的验证单独安排。

Secure Boot 启用时，先按 Ubuntu 的 MOK 流程登记签名证书。已有受信任的私钥和 DER 证书时，可交给部署器为两个模块签名。把文件路径放在目标机环境变量中，私钥留在目标机上。系统要求在启动菜单确认 MOK 登记时，将其记录为控制台待办。见 [Ubuntu Secure Boot 文档](https://documentation.ubuntu.com/security/security-features/platform-protections/secure-boot/)。

```bash
export T40_SIGN_KEY=/secure/t40/module-signing.key
export T40_SIGN_CERT=/secure/t40/module-signing.der
sudo --preserve-env=T40_SIGN_KEY,T40_SIGN_CERT \
  bash tools/deploy_ubuntu.sh ar1021x "$DRIVER_DIR" "$FIRMWARE_DIR" "$COUNTRY" "$RADAR_MAC" "$FC_CLIENT"
```

Ubuntu 服务开机自启。AR1021x 网口出现时，udev 启动对应的 AP 服务。安装时 USB 尚未直通，脚本会先完成软件安装并输出 USB=waiting。设备接入后核对状态，不需要再安装一次。

浏览器通过管理网卡上的 SSH 隧道访问。若另设 T40_HTTP_PORT，隧道远端端口也要改成对应值。此命令在浏览器所在机器运行。

```bash
ssh -N -L 8010:127.0.0.1:8000 "ubuntu@$UBUNTU_MANAGEMENT_IP"
```

打开 http://127.0.0.1:8010/。Ubuntu 直连模式下，由同一台 Ubuntu 消费 UDP 40204。已有 Windows 监视器的分离部署仍可用 [历史转发拓扑](WORKFLOW.md#81-完整链路)，但一个监听地址和端口只交给一个接收程序，避免转发器与监视器争用 40204。

## 3. 分层验收与故障处理

```bash
bash tools/deploy_ubuntu.sh status
sudo bash tools/deploy_ubuntu.sh status ar1021x
```

| 观察 | Agent 的下一步 |
|---|---|
| Ubuntu 下 lsusb -d 0cf3:1022 没有结果 | 核对网卡是否枚举在 Windows、是否被另一台 VM 占用，以及 VMware USB Arbitration Service 状态。让目标 VM 单独持有该 USB 设备，等 Ubuntu 枚举后继续。此阶段不靠重装 Linux 模块排查。 |
| USB 有，modinfo 选到 kernel/ 或其他 updates/ | 对照下方模块命令和安装日志。修复当前内核的路径和 depmod 优先级，让 core 与 USB 两个模块都选中同一套补丁。 |
| modinfo 路径正确，DRIVER_LOADED 缺失或版本不同 | 查看内核日志的 vermagic、签名拒绝、固件加载及 USB bind 信息。补丁已安装但旧模块驻留时，在维护窗口安排重启，然后复验。部署器不会强制卸载正在用的模块。 |
| 模块已加载，仍无 AR1021x 网口 | 查看 journalctl -k -b 的 USB 重置、固件缺失和驱动 probe 日志。对照 bdata.bin 与固件目录，区分未绑定、反复断连和固件报错。 |
| 网口有，AP 失败 | 查 systemctl list-units 't40-radar-ap@*' 和对应 journalctl。核对监管域及信道、192.168.8.1/24 是否冲突、该专用接口是否被网络管理器接管。 |
| AP ENABLED，RADAR_STA=waiting | 核对 radar.accept 中的 STA MAC、设备供电和关联日志。 |
| RADAR_STA=authorized，UDP 仍等待 | 核对 AP 地址、ping 192.168.8.2 的单播连通性以及已加载模块是否来自验证过的 AP 补丁。检查可选飞控服务和 40204 的实际监听。 |
| data_status=live 但没有点 | 接收、帧校验与链路已通。有效空帧可维持 live。记录帧数和记录数，单独判断雷达工作条件。 |

```bash
uname -r
lsusb -d 0cf3:1022
for module in ath6kl_core ath6kl_usb; do
  modinfo -n "$module"
  modinfo -F vermagic "$module"
  modinfo -F srcversion "$module"
  cat "/sys/module/$module/srcversion"
done
journalctl -k -b --no-pager | grep -Ei 'ath6kl|firmware|lockdown|module verification|usb'
systemctl list-units 't40-radar*'
ss -lunp | grep ':40204'
```

成功时，两条 modinfo -n 都位于 /lib/modules/当前内核/updates/t40-radar/，且各自的 srcversion 与 /sys/module 同名模块一致。以前记录的 srcversion 只对对应模块构建有效。安装器用 depmod override 保证本项目候选优先，并在已有 initramfs 时重建当前内核镜像。升级内核后为**新内核**重新执行安装和验收。见 [kmod 的 depmod.d 手册](https://www.man7.org/linux/man-pages/man5/depmod.d.5.html)。

## 4. 重跑与卸载

应用更新后，在新提交重跑原入口。若只更新监视器，用原 source 重跑 monitor，不需要重编网卡驱动。已有真机部署需要切换成纯模拟时，先停用 AP 和可选飞控服务及其 udev 规则。AR1021x 维护完成后，保留运行内核、DRIVER_SELECTED、DRIVER_LOADED、AP 状态、STA 状态和 /api/state 结果。分别报告软件部署通过、USB 待直通、AP 待关联、UDP 等待数据或 UDP 有效收帧；没有实测的数据标为待验证。

需要停用部署时，依次停止可选飞控、AP 和监视器。要还原模块选择，删除本工作流写入的覆盖配置和当前内核 updates/t40-radar 下的两个模块，重建索引和已有的 initramfs。正在驻留的模块在维护重启后才会更换。

```bash
sudo systemctl stop 't40-radar-fc@*.service' 't40-radar-ap@*.service'
sudo systemctl disable --now t40-radar.service
sudo rm -f /etc/udev/rules.d/81-t40-radar.rules
sudo udevadm control --reload-rules
kernel=$(uname -r)
sudo rm -f /etc/depmod.d/t40-ar1021x.conf \
  "/lib/modules/$kernel/updates/t40-radar/ath6kl_core.ko" \
  "/lib/modules/$kernel/updates/t40-radar/ath6kl_usb.ko"
sudo depmod -a "$kernel"
if [ -f "/boot/initrd.img-$kernel" ]; then
  sudo update-initramfs -u -k "$kernel"
fi
```
