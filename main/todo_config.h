// todo screen - device config
//
// WiFi credentials and how the device reaches the Mac live here.
// Nothing secret from Feishu ever reaches the device: the Mac renders the whole
// frame and the ESP32 only downloads 15000 bytes of pixels.
//
// WiFi creds are ALSO readable from NVS at runtime (see todo_wifi_nvs), so you
// can change networks without recompiling: run 配网.command instead.
//
// ESP32-S3 WiFi is 2.4GHz only (802.11b/g/n). A 5GHz SSID will not even show
// up in a scan.
#ifndef TODO_CONFIG_H
#define TODO_CONFIG_H

// ---- WiFi ------------------------------------------------------------------
// 编译期兜底值。**公开仓库里不要在这里填真实凭据** ——
// 运行时优先读 NVS（见 配网.command），只有 NVS 里没有时才用这里的值。
// 留空 = 必须先跑一次 配网.command。
#define TODO_WIFI_SSID   ""
#define TODO_WIFI_PASS   ""

// ---- how the device reaches the Mac ---------------------------------------
// mDNS name registered by the Mac (dns-sd -R "todo-screen" _http._tcp).
// The firmware tries this first and falls back to the literal IP.
//
// TODO_HOST_IP 是编译期兜底，同上：默认留空，靠 UDP 广播发现
// （send_who -> Mac 回 "TODO-SCREEN <ip>:<port>"）或 NVS 里的 wifi_host。
#define TODO_HOST_MDNS   "todo-screen.local"
#define TODO_HOST_IP     ""
#define TODO_PORT        8000

// URL prefix; the mode name is appended at runtime -> /s/<mode>.bin
#define TODO_URL_PREFIX  "http://"

// how often to poll the Mac for a new frame.
// The standby screen has a clock, so it wants a short TTL on the Mac side
// (15 s) — polling faster than that buys nothing.
#define TODO_REFRESH_SEC 60

/* 点阵字库放独立 flash 分区（而不是编进 app 的 .rodata）。
 * app 从 2.09MB 缩到 ~1.1MB —— 烧录时擦除量减半，USB 写入稳得多。
 * 字库本体用 tools/make_font_bin.py 生成的 font/font16.bin 烧到该分区。 */
#define TODO_FONT_IN_PARTITION 1

/* 按住这么久才算「真按键」而不是浮空噪声。
 * 板上有 100nF 去抖电容 + 10K 上拉，噪声脉冲只有几毫秒，
 * 120ms 足够区分，而且短按响应快得多。 */
#define TODO_BTN_HOLD_MS 60

// ---- 画面从哪来 -------------------------------------------------------
// 0 = 板子自己渲染：拉 /state.json，用板上的 C 排版引擎出图。
// 1 = 拉 Mac 渲染好的位帧：拉 /s/<mode>.bin，直接贴屏。
// 2 = 板子自己拉飞书，自己渲染；拉不到才退回 Mac 的 /state.json。
//     这是最终形态 —— Mac 关了板子照常工作。
#define TODO_SOURCE_MAC_BITMAP 1
#define TODO_SOURCE_SELF       0
#define TODO_SOURCE_SELF_FALLBACK 2
#define TODO_SOURCE            TODO_SOURCE_SELF_FALLBACK

// ---- the three buttons -----------------------------------------------------
// Must avoid: display 5/11/12/40/41, i2c 13/14, USB 19/20, flash 26-32,
// PSRAM 33-37, UART 43/44, strapping 0/3/45/46.
//
// 板子丝印是 BOOT / PWR / KEY 三个键，但：
//   BOOT = GPIO0 (strapping, 按住会拉低 -> 板子进 download 模式)
//   PWR  = 电源键，不是 GPIO 输入
//   KEY  = 唯一的用户按键
// 所以只有一个可用按键 —— 它做「循环切换」：待机 -> 待办 -> 日程 -> 待机。
// 引脚由固件开机扫描自动探测（按住 300ms 确认），这里是兜底值。
#define TODO_BTN_KEY      18     // KEY 键，低有效（板上有 10K 上拉）
#define TODO_BTN_SCAN     0      // 已确认引脚，不用再扫

// buttons are active-low with an internal pull-up
#define TODO_BTN_ACTIVE_LOW 1
// ---------------------------------------------------------------------------

#define _TODO_STR2(x) #x
#define _TODO_STR(x)  _TODO_STR2(x)

#endif
