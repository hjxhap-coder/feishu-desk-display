# 上手指南

## 在电脑上预览

这条路径使用模拟数据，只需要 Python、Pillow、C 编译器（命令名为 cc）和[本地生成的点阵字库](../font/README.md)。macOS 可以通过 Xcode Command Line Tools 提供 cc，Linux 可以使用 GCC 或 Clang。

在项目根目录运行：

    python3 -m pip install Pillow
    python3 tools/preview.py --all

终端会列出页面数。到 out/preview/ 打开图片即可。参数 --state 可以传入自有 JSON 数据。仓库内的 [state.example.json](../desktop/state.example.json) 是模拟数据样例。

## 上板

需要 Waveshare ESP32-S3-RLCD-4.2、USB-C 数据线、2.4 GHz Wi-Fi 和一套可在线授权的飞书自建应用。以下命令以 macOS 为例。

### 1. 编译

按 [ESP-IDF v5.3 工具链说明](https://docs.espressif.com/projects/esp-idf/en/v5.3/esp32s3/get-started/linux-macos-setup.html)安装 ESP32-S3 工具链，在当前终端加载安装目录下的 export.sh。然后从本仓库根目录执行：

    idf.py build

产物为 build/esp32_todo_screen.bin。烧录前还需按[字库说明](../font/README.md)生成 font/font16.bin。

### 2. 分块烧录

先确认接的是目标板，并关闭占用串口的终端或采集器。烧录会替换板上的分区表、应用和字库。需要回退时，请先保留现有固件的可恢复备份。

    python desktop/flash_chunked.py

脚本默认读 build/，也可以用 BUILD_DIR 指定其他构建目录。串口默认识别 /dev/cu.usbmodem* 或 /dev/cu.usbserial*，也可以通过 SERIAL_PORT 指定。烧录地址为分区表 0x8000、应用 0x20000、字库 0x310000。此流程沿用板上兼容的 bootloader。遇到空片或 bootloader 与当前项目不匹配时，先检查构建产物中的 flasher_args.json 和[硬件调试笔记](../AGENTS.md)中的烧录说明。

大块写入可能引起 USB 断连，因此脚本以 16 KiB 分块。macOS 若在芯片重启后丢失串口，可先拔插 USB-C 数据线，再确认设备状态。

### 3. 配网并授权

在[飞书开放平台](https://open.feishu.cn/)创建或选用自建应用，确保登录账号能使用该应用。授权脚本请求的权限见 [desktop/mac_auth.py](../desktop/mac_auth.py)，包括待办和日历的读取权限以及 offline_access。应用权限可能需要管理员审核。待办权限参考[飞书任务 API 文档](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/task-v2/overview)。

在项目根目录复制配置：

    cp desktop/config.example.json desktop/config.local.json

编辑 desktop/config.local.json，填写 lark_app_id、lark_app_secret、wifi_ssid 和 wifi_pass。mac_ip 只在使用局域网数据源时填写。关闭可能占用串口的程序，再运行：

    python desktop/device_flow.py --write-nvs

按终端提示打开飞书链接并授权。授权文件仅在本机保存。该命令会用新配置重写板上的 64 KiB NVS 分区，原 Wi-Fi 和飞书配置会被替换。

### 4. 确认上屏

板子连上 Wi-Fi 后会校时并拉取数据。等屏上出现时钟后，按 KEY 查看待办和日程。若需要查串口，可在串口终端输入 status。凭据失效时重新走设备授权流程，并为板子单独使用这组凭据。

- **只有时钟，待办或日程为空**：先检查飞书授权范围、应用是否可用以及当前账号是否有对应数据。
- **中文缺字或画面异常**：确认生成的 font/font16.bin 已写入字库分区。
- **Wi-Fi 或串口异常**：检查 2.4 GHz 网络、USB 数据线和端口占用。更多复盘见[项目记录](../项目总览.md)。
