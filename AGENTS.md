# ESP32-S3 摆件工程

飞书待办/日程桌面摆件。ESP32-S3 自己联网拉飞书数据、自己渲染，自己贴到屏上。

📖 **项目全貌、前因后果、踩坑复盘、烧录 Playbook → [`项目总览.md`](项目总览.md)**
（这份文档讲"是什么、为什么"；本文讲"硬件约束、必须记住的坑"）

---

## 硬件：Waveshare ESP32-S3-RLCD-4.2（4.2" 反射式 ST7305，400×300，1-bit）

### 三个按键（丝印）

| 丝印 | 引脚 | 说明 |
|---|---|---|
| **KEY** | **GPIO18** | 唯一的用户按键，**低有效**。板上已有 **10K 外部上拉** + 100nF 去抖电容 |
| **BOOT** | **GPIO0** | 下载模式键（strapping 脚）。**别在固件里碰它** |
| **PWR** | 不是 GPIO | 电源锁存 IC（ECJ23001），长按关机 / 短按开机 |

板上还有 **18650 电池底座（Akku BerryBase）**：
- 拔 USB 线**不等于断电**（电池还在供电）→ 复位类问题要先拔电池或按 PWR
- 电池电压 ADC = **GPIO4**

### 引脚表

| GPIO | 功能 | 备注 |
|---|---|---|
| 0 | BOOT 键 | strapping，**固件绝不碰** |
| 4 | 电池 ADC | 读 18650 电压 |
| 5 / 11 / 12 / 40 / 41 | 屏 DC / SCK / MOSI / CS / RST | SPI3_HOST |
| 6 | 屏 TE | 撕裂信号 |
| 8 / 9 / 10 / 16 / 45 | I²S 喇叭+麦克风 | DOUT / BCLK / DIN / MCLK / LRCLK |
| 13 / 14 | I²C SDA / SCL | 共享总线：SHTC3 温湿度、ES8311/ES7210 音频 codec、PCF85063A RTC、WiFi |
| 18 | **KEY 键** | 低有效，10K 外部上拉 |
| 46 | 喇叭功放使能 | 高电平使能 |
| 43 / 44 | UART0 TX / RX | 调试串口 |
| 1 / 2 / 3 / 15 / 17 / 38 / 39 / 42 | 扩展排针 | 3 是 JTAG strap，浮空 |

⚠️ **GPIO 47 / 48 是 USB PHY 的备用引脚** —— 给它们配 input+上下拉会让 USB 外设异常，设备直接掉线。

---

## 固件架构

```
飞书 API ──► lark_api.c ──► state.json 形状的 JSON
                                    │
                            todo_state.c  (JSON → 结构体)
                                    │
                            todo_layout.c → 1-bit 帧
                                    │
                              todo_font.c (点阵字库)
```

**Mac 端和板子编译同一份 `components/shared/*.c`** —— 预览就是真机。

### 六个必须记住的坑

1. **大结构体别放栈上。** `TodoState` 有 8KB，`app_main` 的栈只有 3584 字节。
   放栈上 → 栈溢出 → 固件启动即崩（屏白 + 无 console 输出）。
2. **别给按键脚配内部上下拉。** 板子自带 10K 上拉才是真实的；
   配了内部的会跟它打架，电平乱跳 → 无限误触发。
   只配 `GPIO_MODE_INPUT` 就好。
3. **strapping 脚（0/3/45/46）运行时绝对不能配成低。**
   复位瞬间采样它决定进不进 download 模式；配低 → 板子永久卡在
   `boot:0x0`，app 永不启动，而且 **soft reset 清不掉**（只有断电才行）。

   → 固件里保留了一段自愈代码：app 一启动就把 GPIO0 推挽拉高并 hold。
4. **主 console 必须是 USB-Serial/JTAG，不能是 UART。**
   这块板子**没有 UART0 引脚**。主 console 挂 UART 的话：
   - `stdin` 收不到任何输入 → 串口命令全废
   - `ESP_EARLY_LOGE`（看门狗超时、启动早期日志）只走主 console →
     发到没引脚的 UART0 = **一条都看不见**，查故障时完全瞎
5. **内部 RAM 要看「最大连续块」，不是看「还剩多少」。**
   mbedtls 每次 TLS 握手要 **16KB 连续**内部内存。
   `.bss` 里的静态数组会把堆切碎，碎片化后连续块掉到 13KB → 握手失败 → 板子重启。
   → **应用的大缓冲一律进 PSRAM**，内部堆只留给 TLS 和 WiFi 驱动。
6. **字库缺字形不报错，只会留空白格。**
   飞书正文里的 emoji（`➡️` = U+27A1 + U+FE0F）在 16px 点阵字库里**两个都没有**。
   - 变体选择符（U+FE00–U+FE0F）**本来就是零宽**，画成空格子 = 平白多 16px 缝隙
   - emoji 本体没有字形 → 16px 豆腐块

   → 处理在 **`todo_font.c`**（宽度计算和绘制共用的唯一入口，两者永远一致）：
   零宽字符返回空位图 + `advance=0`；常见 emoji 替换成字库里真有的字形。
   字库生成见 [font/README.md](font/README.md)。
   改替代表后必须跑 `check_glyphs.py` 核对每个目标码位在 `font16.bin` 里真的存在。

### 排序：飞书的返回顺序不能直接用

`instance_view` 返回的日程**既不是按开始时间也不是按结束时间**（实测 11:30 排在 10:00 前面）。
`todo_state.c` 里解析完必须**按 `start_min` 稳定排序**，而且**排序之后再定位 `next_idx`**
—— 排序会搬动下标，排序前记的 `next_idx` 立刻失效。

### 诊断量本身也要被怀疑

`lark_api.c` 里那个「cJSON 活跃」计数（hook 记的分配次数 - 释放次数）
**曾经每轮虚涨 1**，看着像内存泄漏，实际一个节点都没漏：
`cJSON_PrintUnformatted` 用 hook 的 allocator 分配，却用**裸 `free()`** 释放，
释放计数少记一次。已改成 `cJSON_free()`。

**教训**：上一轮把「cJSON 活跃恒定 2」当成没有泄漏的证据，其实它一直在漂
（当时只看了两个采样点）。**单调趋势先怀疑计数器本身准不准**
（谁分配、谁释放，是不是同一个 allocator），再顺着它去改。

### 故障可见性要在「最坏路径」上验证

`RenderAndBlit()` 里原来有一句 `解析失败 -> return`，
于是「**从来没有过任何数据**」（最该报警的情况）屏上停在开机美术图，
而「有数据但 RT 死了」倒是正常显示故障页 —— 测的是后者，看起来是好的。

**每条故障路径都要问：会不会提前 return，让故障提示根本没机会被画出来。**

### macOS + ESP32-S3：复位后 USB CDC 不重新枚举

烧录后 `/dev/cu.usbmodem*` 整个消失，但板子**没死**（ping 通、屏上照常）。
macOS 的 USB Serial/JTAG 驱动在芯片复位后不重新 attach，
**必须物理拔插一次 USB 线**。

```bash
ping <板子IP>          # 通 → 板子活着，问题在 USB 侧
ls /dev/cu.usbmodem*    # 空 → 要拔插，别再反复烧录
```

### 串口日志里不能出现凭据

`client_secret`、access_token、refresh_token **一律不能打进日志**
（串口日志会被复制走）。需要确认「字段在不在、长度多少」，就打结构和长度。

```c
ESP_LOGI(TAG, "刷新响应: access_token=%d字符 refresh_token=%d字符 expires_in=%d", ...);
```

### 按键判定

GPIO18，低有效，**不配内部上下拉**，靠"按下 = 电平变低"判断，
再用"按住 60ms 电平不变"挡住抖动（`TODO_BTN_HOLD_MS`）。

**单键、一次前进一步**（已取消长短按区分）：按一次 = 前进一格，
序列是「待机 → 待办各页 → 已完成汇总页 → 日程各页 → 回待机」，
总格数由 `todo_layout_screen_count()` 算出来，待办变多变少格数跟着变。

### 按键必须**即时**响应（2026-11-23 改）

**按键不触发拉数据** —— 只换屏 + 用**已经拉到的**数据立刻重绘（~110ms）。
以前按键和拉数据串在同一个循环里：按键 → 换屏 → 拉一遍飞书（3 个 HTTPS）
→ 才画，**实测要 4 秒多**，手感像卡了。

拉数据按 `next_fetch_us`（**绝对时刻**）走自己的节奏 —— 用绝对时刻而不是
「再等 N 秒」，按键插进来才不会把刷新节奏推后。

串口命令 `next` 可以模拟按键（不用走到板子跟前）：
```bash
python3 raw_cmd.py "next"
```

---

## 关键文件

| 路径 | 作用 |
|---|---|
| `main/main.cpp` | 启动、WiFi、按键、模式状态机、拉取调度 |
| `main/todo_config.h` | 按键引脚、WiFi、接口地址等编译期配置 |
| `components/shared/` | 跨平台共享 C：字库 / 帧缓冲 / 状态 / 排版 |
| `components/lark_api/` | 飞书 API 客户端（token 刷新 + 待办 + 日程） |
| `font/font16.bin` | 本地生成的 TOF1 点阵字库，烧到 `font` 分区 |
| `tools/make_font_bin.py` | BDF → TOF1 字库生成器 |
| `tools/preview.py` | Mac 端预览（编译同一份 C 代码出图） |
| `components/shared/todo_font_data.c` | 选择内嵌字库时，使用转换工具生成此文件 |

**分区表**（`partitions.csv`，2026-11-13 改过）：

| 名称 | 偏移 | 大小 | 用途 |
|---|---|---|---|
| `nvs` | 0x9000 | **64KB** | WiFi 配置 + PHY 校准 + 飞书凭据（RT/AT 分两段存） |
| `phy_init` | 0x19000 | 4KB | PHY 校准分区（**当前没在用**，PHY 数据实际在 NVS 里） |
| `factory` | 0x20000 | 3008KB | app 固件（1.06MB，余 65%） |
| `font` | 0x310000 | 1MB | 点阵字库 |

⚠️ **NVS 为什么是 64KB**：RT/AT 各 6899 字符 → NVS 单条上限 ~4000 → **必须对半切
→ 各占 2 页**；加上 PHY 校准（1~2 页）+ WiFi 配置（1~2 页）+ 小条目（1 页）
= **7~8 页**。原来的 24KB = **6 页装不下** → 2026-11-09 自动刷新时写 `lark_rt2`
失败（NO_SPACE），NVS 里只剩半截 RT，板子从此拿不到 token。

⚠️ **app 分区必须 64KB 对齐**（`0x1A000` 不合法，要用 `0x20000`）。
⚠️ **`write_nvs.py` 的 `NVS_SIZE` 必须和分区表一致**，否则 nvs_flash_init
会直接格式化分区（连 WiFi 配置一起没）。

⚠️ **改分区表 → app 偏移就变 → `flash_chunked.py` 里的基址要跟着改**，
而且**分区表本身（0x8000）也得烧**（bootloader 靠它找 app）。

---

## 配套的 Mac 端工程

`desktop/`

**运维（日常会用到）**
- `flash_chunked.py` —— **分块烧录**（16KB/块 + 当次补写），大文件写入唯一的可靠方式
- `serial_logger.py` —— 串口采集（自动找端口）→ `/tmp/board_serial.log`
- `serial_cmd.py` —— 和板子串口对话：`python3 serial_cmd.py "status"`
- `write_nvs.py` —— 灌 NVS（WiFi 配置 + 飞书凭据）
- `nvs_dump.py` —— 读板子 NVS 解出凭据（**自动让开串口、读完恢复采集器**）
- `inject_rt.py` / `finish_auth.py` —— 刷新/注入 refresh_token（带 NVS 回读校验，不擦 NVS）

**开发**
- `pull_state.py` —— 从**板子自己的串口输出**取 state.json
  （Mac 端**不需要飞书 token**，见 `项目总览.md` §3.14）
- `pull_state_lark.py` —— 老路子：Mac 自己从飞书拉（`--from-lark`）
- `check_glyphs.py` —— 核对码位在字库里有没有字形 + 核对 emoji 替代表的目标真的存在
- `render.py` / `render_v2.py` / `render_v3.py` —— 三屏渲染器（v3 是当前设计规范）
- `serve_screen.py` —— HTTP 服务（`--renderer v1|v3|c`）+ UDP 广播自动发现
- `mock_state.py` —— 无权限时造假数据
- `mock_lark_server.py` —— **假飞书 API**，让板子在权限批下来之前就能端到端联调
- `sync_lark_creds.py` —— 读写 lark-cli 的加密凭据

**设计文档**
- `需求说明书.md` —— 需求源头（三屏内容、字体约束、还没定的事）
- `设计规范_v3.md` —— 显示设计规范 v3（版面、视觉层次、边界情况）

⚠️ **串口是独占资源**：采集器占着端口时，esptool 抢同一个端口会失败，
而且**板子会被留在下载模式**（`boot:0x22`），应用不跑、屏上停住。
烧录前先 `pkill -f serial_logger.py`，用完再拉起来。

## 烧录

⚠️ **不要用普通 `write_flash` 写 app** —— 写 >100KB 时擦除期间芯片无法取指，
USB-Serial-JTAG 固件跟着停摆 → USB 断连。必须分块写：

```bash
pkill -f serial_logger.py                      # 让开串口

python -m esptool --chip esp32s3 --port /dev/cu.usbmodem1101 \
    erase_region 0x10000 0x10000               # 先让板子停在 bootloader

python3 desktop/flash_chunked.py # 分块烧 app + 字库（失败时补写缺块）

python3 desktop/serial_logger.py # 恢复采集 → /tmp/board_serial.log
```

USB 端口名会变（`usbmodem1101` ↔ `11101`），每次 `ls /dev/cu.usb*` 确认。

烧完如果板子进不了 app：**拔掉 USB 线 + 拔掉电池**（或按 PWR 关机再开机）。

改了 IDF 组件源码后 ninja **不会**自动重编，要手动删对应 `.obj`（见 `项目总览.md` §7）。

---

## VS Code 开发环境（本工程已配好）

工程根目录的 `.vscode/` 和 `.clangd` **是有用的，别删**：

| 文件 | 作用 |
|---|---|
| `.clangd` | `CompileFlags: Remove: [-f*, -m*]` —— 去掉 xtensa gcc 的私有参数（`-mlongcalls` 等），否则 clangd 报 unknown argument |
| `.vscode/settings.json` | clangd `--query-driver` 指向 `xtensa-esp32s3-elf-gcc`，这样 clangd 能问出正确 target triple |
| `.vscode/tasks.json` | 编译 / 分块烧录 / 串口采集 / 读 NVS 等任务 |

⚠️ **clangd 必须配 `--query-driver`**，否则 target 会退化成宿主 macOS，
`esp32s3` 的头文件按 x86 语义解析，跳转和类型全是错的。
配好后实测 `clangd --check` 的 triple 是 `xtensa-esp-unknown-elf`。

⚠️ **本机 GitHub 被墙**，clangd 扩展下不到上游二进制，
所以 `clangd.path` 指向 `/usr/bin/clangd`（Apple 自带 21.0.0，实测可用）。
换机器时这三条要重新验证。

命令面板 → `Tasks: Run Task` 里有：
「编译固件（不碰板子）」「烧录（分块·合规版）」「读板子 NVS」等。
**烧录任务已经按上面的分块流程写好，别再用 `idf.py flash`。**
