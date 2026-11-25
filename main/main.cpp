#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <stdlib.h>
#include <time.h>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/queue.h>
#include <freertos/event_groups.h>

#include <esp_log.h>
#include <esp_heap_caps.h>
#include "esp_timer.h"
#include <esp_timer.h>
#include <esp_task_wdt.h>
#include <esp_event.h>
#include <esp_netif.h>
#include <esp_wifi.h>
#include <esp_http_client.h>
#include <esp_vfs_console.h>
#include <nvs_flash.h>
#include <nvs.h>

#include <lwip/sockets.h>
#include <lwip/inet.h>
#include "esp_sntp.h"

#include "display_bsp.h"
#include "user_config.h"
#include "todo_config.h"

/* 共享层：和 Mac 端预览用的是同一份 .c */
#include "todo_font.h"
#include "todo_fb.h"
#include "todo_state.h"
#include "todo_layout.h"

/* 板子自己拉飞书 */
#include "lark_api.h"
/* #include "remote_ctl.h" */

// 0 = vendor LVGL demo UI
// 1 = todo screen: artwork on screen at boot, then 400x300 1-bit frames
//     rendered on this board (Chinese text via the bundled bitmap font).
#define TODO_MODE 1

static const char *TAG = "todo";
static const int FRAME_BYTES = TODO_FB_BYTES;   // 15000

DisplayPort RlcdPort(12,11,5,40,41,LCD_WIDTH,LCD_HEIGHT);

/* 刚渲染出来的帧。放 PSRAM —— 内部堆必须留出一块 16KB 的**连续**内存
 * 给 mbedtls 握手（MBEDTLS_SSL_IN_CONTENT_LEN=16384）。
 * 静态数组把内部堆切碎后，这块连续内存申请不到：
 * 实测 free=44KB 但最大连续块只剩 13KB，第二次 HTTPS 请求就死。
 * 教训：内部堆要看「最大连续块」，不是看「还剩多少」。 */
static uint8_t *s_frame;

/* 屏上当前的帧（只用于和 s_frame 比对，不参与 DMA）—— 放 PSRAM。 */
static uint8_t *s_shown;

/* ---------------------------------------------------------------------------
 * modes
 * --------------------------------------------------------------------------- */
static const char *MODE_NAME[] = { "standby", "todo", "schedule" };

/* 屏幕上的第几页。整个序列是：
 *   0 = 待机，1..N = 待办的各页，N+1.. = 日程的各页
 * 板子只有一个键，一次按键前进一步，翻完自动回到待机 ——
 * 不区分长短按，长按翻页那个手感很差。 */
static volatile int s_screen = 0;

/* TodoState 有 8KB，放栈上会直接爆 fetch_task 的栈 —— 放堆上。
 * 而且放 PSRAM：内部堆要留给 TLS 的 16KB 连续块。
 * 解析器 todo_state_parse_json 的所有写入都有边界检查
 * （strncpy + 显式补 '\0'），踩不到邻居。 */
static TodoState *g_state;

/* 开机自检图：图像在 app_main 早期生成，刷屏挪到网络起来之后 */
static uint8_t *s_selftest = nullptr;

/* 当前故障状态 —— 必须让屏上看得见。
 * 之前拉不到数据就「保持上一帧」，结果屏上一直停在开机美术图
 * （吹笛子少年），看上去一切正常，实际上早就死了。 */
static TodoErr s_err = TODO_ERR_NONE;

/* 按键事件：一次按键 = 前进一格。 */
typedef struct {
	int value;
} BtnEvent;

static QueueHandle_t s_btn_q;

/* ---------------------------------------------------------------------------
 * server discovery: the Mac announces itself over UDP broadcast.
 *
 * Why not mDNS: this ESP-IDF tree has no `mdns` component, and DNS-SD also
 * needs the Mac and the board on the same subnet. A plain UDP broadcast is
 * three lines of lwip and works on any home router.
 *
 * Wire format (both directions):
 *   board -> "WHO"                                  (broadcast)
 *   mac   -> "TODO-SCREEN <ip>:<port>"              (unicast reply)
 * --------------------------------------------------------------------------- */
#define DISCOVERY_PORT  9999
static char     s_server_ip[16]  = TODO_HOST_IP;
static uint16_t s_server_port    = TODO_PORT;
static int      s_udp_fd = -1;

static void send_who(void)
{
	if (s_udp_fd < 0) return;
	struct sockaddr_in dst = {};
	dst.sin_family      = AF_INET;
	dst.sin_port        = htons(DISCOVERY_PORT);
	dst.sin_addr.s_addr = INADDR_BROADCAST;
	const char *msg = "WHO";
	sendto(s_udp_fd, msg, strlen(msg), 0, (struct sockaddr *)&dst, sizeof(dst));
}

static void udp_task(void *arg)
{
	uint8_t buf[128];
	while (true) {
		struct sockaddr_in src = {};
		socklen_t sl = sizeof(src);
		int n = recvfrom(s_udp_fd, buf, sizeof(buf) - 1, 0,
						 (struct sockaddr *)&src, &sl);
		if (n <= 0) continue;
		buf[n] = '\0';

		const char *p = strstr((const char *)buf, "TODO-SCREEN ");
		if (p == NULL) continue;
		p += strlen("TODO-SCREEN ");
		char *colon = strchr(p, ':');
		if (colon == NULL) continue;
		*colon = '\0';
		uint16_t port = (uint16_t)atoi(colon + 1);
		if (port == 0) continue;
		if (strcmp(p, s_server_ip) != 0 || port != s_server_port) {
			ESP_LOGI(TAG, "server discovered: %s:%u", p, port);
		}
		strlcpy(s_server_ip, p, sizeof(s_server_ip));
		s_server_port = port;
	}
}

// ---------------------------------------------------------------------------
// WiFi (station, auto-reconnect). Credentials come from NVS if 填WiFi.command
// has written them, otherwise from todo_config.h.
// ---------------------------------------------------------------------------
static EventGroupHandle_t s_net_ev;
#define NET_UP BIT0

static bool load_wifi_nvs(char *ssid, size_t slen, char *pass, size_t plen)
{
	nvs_handle_t h;
	if (nvs_open("wifi", NVS_READONLY, &h) != ESP_OK) return false;
	// nvs_get_str takes a size_t* for the buffer length, not the length itself.
	size_t sl = slen, pl = plen;
	esp_err_t e1 = nvs_get_str(h, "wifi_ssid", ssid, &sl);
	esp_err_t e2 = nvs_get_str(h, "wifi_pass", pass, &pl);
	nvs_close(h);
	if (e1 != ESP_OK || e2 != ESP_OK || ssid[0] == '\0') return false;
	ssid[slen - 1] = '\0';
	pass[plen - 1] = '\0';
	return true;
}

// The Mac's address may be stored in NVS too ("ip" or "ip:port"). If it is,
// it wins over the compile-time default; the UDP discovery can still override
// it later.
static void load_host_nvs(void)
{
	char host[64] = {0};
	nvs_handle_t h;
	if (nvs_open("wifi", NVS_READONLY, &h) != ESP_OK) return;
	size_t hl = sizeof(host);
	esp_err_t e = nvs_get_str(h, "wifi_host", host, &hl);
	nvs_close(h);
	if (e != ESP_OK || host[0] == '\0') return;
	host[sizeof(host) - 1] = '\0';

	char *colon = strchr(host, ':');
	if (colon != NULL) {
		*colon = '\0';
		uint16_t p = (uint16_t)atoi(colon + 1);
		if (p > 0) s_server_port = p;
	}
	if (host[0] != '\0') {
		strlcpy(s_server_ip, host, sizeof(s_server_ip));
		ESP_LOGI(TAG, "host from NVS: %s:%u", s_server_ip, s_server_port);
	}
}

static void NetEvent(void *arg, esp_event_base_t base, int32_t id, void *data)
{
	if (base == WIFI_EVENT) {
		if (id == WIFI_EVENT_STA_START) {
			esp_wifi_connect();
		} else if (id == WIFI_EVENT_STA_DISCONNECTED) {
			xEventGroupClearBits(s_net_ev, NET_UP);
			ESP_LOGW(TAG, "wifi dropped, retrying");
			esp_wifi_connect();
		}
	} else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
		ip_event_got_ip_t *e = (ip_event_got_ip_t *)data;
		ESP_LOGI(TAG, "wifi up, ip " IPSTR, IP2STR(&e->ip_info.ip));
		xEventGroupSetBits(s_net_ev, NET_UP);

		/* 校时。不校的话系统时间停在 1970 年，
		 * 日程拉的是 1970 年的空档，屏上时钟也是错的。
		 * SNTP 给的是 UTC，本地时区靠 TZ 环境变量（app_main 里设的）。 */
		esp_sntp_setoperatingmode(SNTP_OPMODE_POLL);
		esp_sntp_setservername(0, "ntp.aliyun.com");
		esp_sntp_setservername(1, "cn.pool.ntp.org");
		esp_sntp_init();
		ESP_LOGI(TAG, "SNTP 已启动");

		/* 远程控制通道（暂时摘掉 —— 它的组件依赖会改链接/IRAM 布局，
		 * 正在定位 WiFi 那边 StoreProhibited 的根因） */
#if 0
		remote_ctl_set_inject_fn(lark_auth_set_refresh_token);
		remote_ctl_start();
#endif
	}
}

static void NetStart(void)
{
	char ssid[33] = {0};
	char pass[65] = {0};

	s_net_ev = xEventGroupCreate();

	/* NVS 可能被擦空/损坏（我手工 erase 过）。
	 * 任何错误都先擦再初始化，**不要** ESP_ERROR_CHECK —— 那会直接
	 * abort 变成重启循环，日志都来不及打印。 */
	esp_err_t err = nvs_flash_init();
	if (err != ESP_OK) {
		ESP_LOGW(TAG, "nvs_flash_init 失败 (%s)，擦除重建", esp_err_to_name(err));
		esp_err_t e2 = nvs_flash_erase();
		if (e2 != ESP_OK) {
			ESP_LOGE(TAG, "nvs_flash_erase 也失败: %s", esp_err_to_name(e2));
		}
		err = nvs_flash_init();
		if (err != ESP_OK) {
			ESP_LOGE(TAG, "nvs_flash_init 重建失败: %s", esp_err_to_name(err));
		}
	}

	load_host_nvs();

	bool from_nvs = load_wifi_nvs(ssid, sizeof(ssid), pass, sizeof(pass));
	if (!from_nvs) {
		strlcpy(ssid, TODO_WIFI_SSID, sizeof(ssid));
		strlcpy(pass, TODO_WIFI_PASS, sizeof(pass));
	}
	ESP_LOGI(TAG, "wifi ssid=\"%s\" (%s)", ssid, from_nvs ? "from NVS" : "from config.h");

	/* 编译期默认值留空（公开仓库不填真实凭据）+ NVS 里也没有 =
	 * 根本没配网。这时候**不要**拿着空 SSID 去连 —— 连不上只会刷
	 * 一屏 "wifi dropped"，看不出是「没配网」还是「路由器不在线」。
	 * 直接把话说明白，让用户去跑 配网.command 或串口 `wifi <ssid> [pass]`。 */
	if (ssid[0] == '\0') {
		ESP_LOGE(TAG, "没有 WiFi 凭据：NVS 里没有，编译期默认值也是空的");
		ESP_LOGE(TAG, "请先配网：跑 填WiFi.command，或串口输入 `wifi <ssid> [pass]`");
	}

	ESP_ERROR_CHECK(esp_netif_init());
	esp_err_t loop = esp_event_loop_create_default();   // tolerate a pre-existing loop
	if (loop != ESP_OK && loop != ESP_ERR_INVALID_STATE) {
		ESP_ERROR_CHECK(loop);
	}
	esp_netif_create_default_wifi_sta();

	wifi_init_config_t ic = WIFI_INIT_CONFIG_DEFAULT();
	ESP_ERROR_CHECK(esp_wifi_init(&ic));
	ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, NetEvent, NULL));
	ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, NetEvent, NULL));

	wifi_config_t wc = {};
	strlcpy((char *)wc.sta.ssid, ssid, sizeof(wc.sta.ssid) - 1);
	strlcpy((char *)wc.sta.password, pass, sizeof(wc.sta.password) - 1);

	ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
	ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
	ESP_ERROR_CHECK(esp_wifi_start());
	ESP_LOGI(TAG, "connecting to \"%s\" ...", ssid);
}

// ---------------------------------------------------------------------------
// Blit a packed 1-bit frame (MSB first, 1 = WHITE) and remember it.
// ---------------------------------------------------------------------------
static void BlitFrame(const uint8_t *img)
{
	const int rowbytes = LCD_WIDTH / 8;
	RlcdPort.RLCD_ColorClear(ColorWhite);
	for (int y = 0; y < LCD_HEIGHT; y++) {
		for (int x = 0; x < LCD_WIDTH; x++) {
			uint8_t bit = (img[y * rowbytes + (x >> 3)] >> (7 - (x & 7))) & 1;
			RlcdPort.RLCD_SetPixel(x, y, bit ? ColorWhite : ColorBlack);
		}
	}
	RlcdPort.RLCD_Display();
	memcpy(s_shown, img, FRAME_BYTES);
}

// ---------------------------------------------------------------------------
// fetch the state document from the Mac
// ---------------------------------------------------------------------------
/* state.json 的缓冲。11 条未完成 + 今天完成的 + 日程，
 * 加上个别很长的 summary，实测 ~4KB；但飞书哪天多几条就可能顶到上限，
 * 而 lark_build_state 放不下时是**明确失败**（不再静默截断），
 * 所以给足余量。 */
#define STATE_BUF_SIZE 32768

static bool FetchState(char *buf, size_t cap)
{
	/* 编译期兜底 IP 现在是空的（公开仓库不写死别人的内网 IP）。
	 * 正常靠 UDP 广播发现（send_who -> Mac 回 TODO-SCREEN ip:port），
	 * 或者 NVS 里的 wifi_host。两边都没拿到就别去请求一个
	 * "http://:8000/..." 的残缺 URL —— 直接说没找到服务器。 */
	if (s_server_ip[0] == '\0') {
		ESP_LOGW(TAG, "还没找到 Mac 的地址（UDP 发现没回、NVS 里也没有）");
		return false;
	}

	char url[160];
	snprintf(url, sizeof(url), "%s%s:%u/state.json",
			 TODO_URL_PREFIX, s_server_ip, s_server_port);

	esp_http_client_config_t cfg = {};
	cfg.url = url;
	cfg.timeout_ms = 8000;

	esp_http_client_handle_t c = esp_http_client_init(&cfg);
	if (c == NULL) {
		ESP_LOGE(TAG, "http client init failed for %s", url);
		return false;
	}

	bool ok = false;
	if (esp_http_client_open(c, 0) == ESP_OK) {
		int len = esp_http_client_fetch_headers(c);
		int status = esp_http_client_get_status_code(c);
		if (status == 200 && len > 0 && len < (int)cap) {
			int got = esp_http_client_read(c, buf, len);
			if (got == len) {
				buf[got] = '\0';
				ok = true;
			} else {
				ESP_LOGW(TAG, "short read %d/%d", got, len);
			}
		} else {
			ESP_LOGW(TAG, "http %d, content-length %d", status, len);
		}
		esp_http_client_close(c);
	}
	esp_http_client_cleanup(c);
	return ok;
}

// ---------------------------------------------------------------------------
// fetch a frame the Mac already rendered (15000 bytes, packed 1-bit)
// ---------------------------------------------------------------------------
static bool FetchFrame(const char *mode)
{
	if (s_server_ip[0] == '\0') {
		ESP_LOGW(TAG, "还没找到 Mac 的地址，跳过拉帧");
		return false;
	}

	char url[160];
	snprintf(url, sizeof(url), "%s%s:%u/s/%s.bin",
			 TODO_URL_PREFIX, s_server_ip, s_server_port, mode);

	esp_http_client_config_t cfg = {};
	cfg.url = url;
	cfg.timeout_ms = 8000;

	esp_http_client_handle_t c = esp_http_client_init(&cfg);
	if (c == NULL) {
		ESP_LOGE(TAG, "http client init failed for %s", url);
		return false;
	}

	bool ok = false;
	if (esp_http_client_open(c, 0) == ESP_OK) {
		int len = esp_http_client_fetch_headers(c);
		int status = esp_http_client_get_status_code(c);
		if (status == 200 && len == FRAME_BYTES) {
			int got = esp_http_client_read(c, (char *)s_frame, FRAME_BYTES);
			ok = (got == FRAME_BYTES);
			if (!ok) {
				ESP_LOGW(TAG, "short read %d/%d", got, FRAME_BYTES);
			}
		} else {
			ESP_LOGW(TAG, "http %d, content-length %d (want %d)",
					 status, len, FRAME_BYTES);
		}
		esp_http_client_close(c);
	}
	esp_http_client_cleanup(c);
	return ok;
}

// ---------------------------------------------------------------------------
// render the current mode and push it to the panel
// ---------------------------------------------------------------------------

/* 比较两份 state.json 时，跳过 updated_at 字段。
 * 它是「什么时候拉的」，每轮都变；但待办/日程本身可能一点没变。
 * 带着它比较 = 每分钟都重刷一遍串口日志。 */
static const char *skip_updated_at(const char *json)
{
	const char *p = strstr(json, "\"updated_at\"");
	if (p == NULL) return json;
	const char *q = strchr(p, ',');
	return (q != NULL) ? q + 1 : json;
}

/* 把飞书侧的故障状态映射成屏上的故障类型。
 * 分三档：写盘坏了 > RT 作废 > 一般网络问题。 */
static TodoErr lark_error_state(void)
{
	if (lark_auth_broken()) return TODO_ERR_NVS;   /* 凭据没存住，重启就废 */
	if (lark_auth_dead())   return TODO_ERR_AUTH;   /* RT 被作废，要重新授权 */
	return TODO_ERR_NET;
}

/* 串口导出一帧时的分块大小和间隔。
 * 见 dump_frame() 里那段注释：512/2ms 不够，会丢最前面的字节。 */
#define FRAME_CHUNK    128
#define FRAME_CHUNK_MS 10

/* 渲染这一屏时用的时刻 —— 导出帧时一并报出去，
 * Mac 侧要用**同一个时刻**渲染才能逐像素比对。 */
static time_t s_last_render_now = 0;

/* 把当前帧导出到串口（给 Mac 侧做逐像素比对）。
 *
 * 【为什么只在按键换屏时导出】一帧 15000 字节，串口扛不住每分钟一次
 * （待机屏的时钟每分钟都变 → 每分钟 15KB，日志很快撑爆）。
 * 按键换屏才是真正要验收的时刻。
 *
 * 【格式】头 + 固定 15000 字节二进制 + 尾。
 * 中间是定长的，所以 Mac 侧按字节数解析，**不怕二进制里碰巧
 * 出现标记字符串**。 */
static void dump_frame(int screen, const char *mode, int page, time_t now,
                       const char *json)
{
	/* 头里带上 state.json 的**字节数** —— Mac 侧才能精确定位帧的起点。
	 * 【踩过的坑】原来靠「跳过连续换行」找起点，结果帧的头几个字节
	 * 正好是 0x0A/0x0D，被当成换行吃掉了 —— 整幅图水平错位 2 个字节，
	 * 比对出来「几千个像素不同」，其实只是位移。
	 * 二进制数据**不能靠猜分隔符定位**，必须靠长度。 */
	printf("\n===FRAME_BEGIN screen=%d mode=%s page=%d now=%ld state=%d bytes=%d===\n",
	       screen, mode, page, (long)now, (int)strlen(json), FRAME_BYTES);
	/* 【顺序：先帧、后 state】两个长度都在头里，
	 * 所以 Mac 侧只要「跳过头的行尾（1~2 字节），再按长度取」——
	 * **一个字节都不用猜**。
	 *
	 * 【踩过的坑】二进制数据**不能靠猜分隔符定位**：
	 *  - 靠「跳过所有连续换行」→ 帧的头几个字节正好是 0x0A/0x0D，被吃掉了，
	 *    整幅图水平错位 2 个字节，比对出来「几千像素不同」其实只是位移；
	 *  - state 放在帧前面 → state 的长度里混进了头行尾的 \r\n，
	 *    JSON 尾部少一个字节 → 解析失败。
	 *  → 结论：**头里给长度，帧在前 state 在后**。
	 *
	 * 帧块里带着这一帧**用的** state.json —— 否则 Mac 侧可能拿的是
	 * **上一份**数据，比出来的差异全是假的
	 * （踩过：帧是 4 条待办，Mac 拿到的 state 是 2 条已完成的旧数据）。 */
	for (size_t j = 0; j < (size_t)FRAME_BYTES; j += FRAME_CHUNK) {
		size_t n = ((size_t)FRAME_BYTES - j > FRAME_CHUNK) ? FRAME_CHUNK
		                                                     : ((size_t)FRAME_BYTES - j);
		fwrite(s_frame + j, 1, n, stdout);
		fflush(stdout);
		/* 分块让出：一次 printf 15KB 会把 USB TX 缓冲冲爆。
		 * 【踩过的坑】512 字节/块 + 2ms 间隔**远远不够** ——
		 * 15KB 一口气灌，USB 环形缓冲溢出时丢的是**最旧的**字节，
		 * 于是帧的前几千字节没了、后面是对的，看起来像「上半屏乱码」。
		 * （环形缓冲溢出丢最旧，不是丢最新 —— 这是最反直觉的地方。）
		 * 128 字节/块 + 10ms 间隔 ≈ 12.8KB/s，接收端跟得上。 */
		vTaskDelay(pdMS_TO_TICKS(FRAME_CHUNK_MS));
	}
	{
		size_t jn = strlen(json);
		for (size_t j = 0; j < jn; j += FRAME_CHUNK) {
			size_t kn = (jn - j > FRAME_CHUNK) ? FRAME_CHUNK : (jn - j);
			fwrite(json + j, 1, kn, stdout);
			fflush(stdout);
			vTaskDelay(pdMS_TO_TICKS(FRAME_CHUNK_MS));
		}
	}
	printf("\n===FRAME_END===\n");
	fflush(stdout);
}

static void RenderAndBlit(const char *json)
{
	/* 同步故障状态给排版层（待机屏的故障条靠它） */
	todo_layout_set_error(s_err);

	/* TodoState 有 8KB 上下（32 个任务 + 16 个日程），
	 * 放栈上会直接爆 fetch_task 的栈 —— 放静态区。 */
	bool parsed = todo_state_parse_json(json, g_state);
	if (!parsed) {
		/* 【重要】解析失败**不能直接 return**。
		 * 原来这里是「解析失败 -> 屏上保持上一帧」，于是
		 * 「从来没有过数据」这种**最该报警**的情况，
		 * 屏上反而什么都没有（还是开机美术图）——
		 * 故障可见性恰好在最关键的地方失效了。
		 * 现在：解析失败也画（内容屏整屏故障 / 待机屏故障条）。 */
		ESP_LOGE(TAG, "state.json 解析失败，屏上显示故障提示");
		if (s_err == TODO_ERR_NONE) s_err = TODO_ERR_NET;
		todo_layout_set_error(s_err);
	}

	TodoFb fb = { s_frame, TODO_FB_W, TODO_FB_H };

	time_t t = time(NULL);
	struct tm now;
	localtime_r(&t, &now);
	s_last_render_now = t;

	/* 「进行中 / 下一个」按**当前时刻**重算 —— 数据是 60 秒前拉的，
	 * 标记定死在拉取那一刻会��时间轴上挂着一块几小时前的黑块。 */
	todo_state_refresh_flags(g_state, &now);

	TodoMode mode;
	int page;
	todo_layout_screen_unpack(s_screen, g_state, &mode, &page);

	/* 故障时：待办/日程屏整屏提示；待机屏照常显示时钟
	 * （摆件的基本功能不能因为断网就停），故障条由 layout 内部画。 */
	if (mode != TODO_MODE_STANDBY && s_err != TODO_ERR_NONE) {
		todo_layout_render_error(&fb, &now, s_err);
	} else {
		todo_layout_render(mode, g_state, &fb, &now, page);
	}

	/* 总格数是**算出来的**，不是写死的：
	 *   1（待机）+ 待办页数（按实际占高切）+ 日程页数。
	 * 待办数量变了，格数跟着变。 */
	static int s_last_total = -1;
	int total = todo_layout_screen_count(g_state);
	if (total != s_last_total) {
		s_last_total = total;
		ESP_LOGI(TAG, "页面序列共 %d 格（待办 %d 页 / 日程 %d 页）",
		         total, todo_layout_page_count(TODO_MODE_TODO, g_state),
		         todo_layout_page_count(TODO_MODE_SCHEDULE, g_state));
	}

	if (memcmp(s_frame, s_shown, FRAME_BYTES) != 0) {
		BlitFrame(s_frame);
		ESP_LOGI(TAG, "第%d页 %s 帧已更新", s_screen, MODE_NAME[mode]);
	} else {
		ESP_LOGI(TAG, "第%d页 %s 帧没变", s_screen, MODE_NAME[mode]);
	}
}

static volatile int  s_btn_pin[3] = { -1, -1, -1 };
static volatile bool s_btn_low[3] = { true, true, true };  /* true = 按下拉低 */
static volatile int  s_btn_bound = 0;

// ---------------------------------------------------------------------------
// buttons
static int btn_pressed(gpio_num_t pin, bool low_active)
{
	int lv = gpio_get_level(pin);
	return low_active ? (lv == 0) : (lv != 0);
}

static void btn_task(void *arg)
{
	int last[3]   = {0, 0, 0};
	int stable[3] = {0, 0, 0};
	int count[3]  = {0, 0, 0};

	while (true) {
		/* ---- 已确认的按键 ---- */
		for (int i = 0; i < 3; i++) {
			int pin = s_btn_pin[i];
			if (pin < 0) continue;
			int raw = btn_pressed((gpio_num_t)pin, s_btn_low[i]);
			if (raw != last[i]) {
				last[i] = raw;
				count[i] = 0;
			} else if (++count[i] >= 3 && stable[i] != raw) {
				stable[i] = raw;
				if (raw) {                       /* 检测到"按下"边沿 */
					/* 按住 TODO_BTN_HOLD_MS 就触发。
					 * 浮空脚的电平抖动是瞬时的（板上 100nF 去抖电容，
					 * 噪声脉冲只有几毫秒），按住几十毫秒才松开的是真按键。
					 * 不区分长短按 —— 长按翻页那个手感很差。 */
					int hold = 0;
					bool confirmed = false;
					while (btn_pressed((gpio_num_t)pin, s_btn_low[i])) {
						vTaskDelay(pdMS_TO_TICKS(10));
						hold += 10;
						if (hold >= TODO_BTN_HOLD_MS) {
							confirmed = true;
							break;
						}
					}
					if (!confirmed) {
						ESP_LOGW(TAG, "GPIO %d 是抖动不是按键，忽略", pin);
						continue;
					}

					BtnEvent ev;
					ev.value = i;
					ESP_LOGI(TAG, "按键 GPIO %d", pin);
					xQueueSend(s_btn_q, &ev, 0);
				}
			}
		}

		vTaskDelay(pdMS_TO_TICKS(20));
	}
}

// ---------------------------------------------------------------------------
// serial console: change wifi / host without recompiling
// ---------------------------------------------------------------------------
static void save_nvs_str(const char *key, const char *val)
{
	nvs_handle_t h;
	ESP_ERROR_CHECK(nvs_open("wifi", NVS_READWRITE, &h));
	ESP_ERROR_CHECK(nvs_set_str(h, key, val));
	ESP_ERROR_CHECK(nvs_commit(h));
	nvs_close(h);
}

static void console_task(void *arg)
{
	/* refresh_token 有 4800+ 字符，注入命令一行要装得下。
	 * 放堆上（PSRAM）—— console_task 的栈只有几 KB，放不下；
	 * 也不能放 .bss —— 内部堆的连续块要留给 TLS。 */
	char *line = (char *)heap_caps_malloc(7200, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
	if (line == NULL) {
		line = (char *)malloc(7200);
	}
	/* 注意：line 是**指针**，sizeof(line) 是指针大小（Xtensa 上 4 字节），
	 * 不是 5300！之前写成 sizeof(line) -> fgets 每次只读 3 个字符，
	 * "status" 被切成 "sta"+"tus" 两段，命令全都匹配不上。 */
	const size_t LINE_CAP = 7200;
	if (line == NULL) {
		ESP_LOGE(TAG, "console 行缓冲分配失败");
		while (true) vTaskDelay(pdMS_TO_TICKS(1000));
	}
	while (true) {
		/* 注意：FreeRTOS 任务绝不能 return —— 返回会直接 abort 整个固件。
		 * fgets 在没有输入源时返回 NULL，这里必须重试而不是退出循环。 */
		if (fgets(line, LINE_CAP, stdin) == NULL) {
			clearerr(stdin);
			vTaskDelay(pdMS_TO_TICKS(500));
			continue;
		}
		char *nl = strpbrk(line, "\r\n");
		if (nl) *nl = '\0';
		if (line[0] == '\0') continue;

		if (strncmp(line, "wifi ", 5) == 0) {
			char *ssid = line + 5;
			char *sp = strchr(ssid, ' ');
			if (!sp) {
				printf("用法: wifi <ssid> [pass]\n");
				continue;
			}
			*sp = '\0';
			save_nvs_str("wifi_ssid", ssid);
			save_nvs_str("wifi_pass", sp + 1);
			printf("已保存 ssid=\"%s\" - 重启中\n", ssid);
			vTaskDelay(pdMS_TO_TICKS(200));
			esp_restart();
		} else if (strncmp(line, "host ", 5) == 0) {
			save_nvs_str("wifi_host", line + 5);
			printf("已保存 host=%s（重启后生效）\n", line + 5);
		} else if (strcmp(line, "lark rtout") == 0) {
			/* 把当前 refresh_token 打到串口（排查用：
			 * Mac 上要重写 NVS 时需要拿到它） */
			/* 这三个数组原本全在栈上（5300+2401+2464 ≈ 10KB），
			 * console_task 栈只有 4096 字节 -> 必爆栈。
			 * 症状：panic 报 IllegalInstruction / A15=0xa5a5a5a5（栈填充图案）。 */
			char *rt = (char *)heap_caps_malloc(7200, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
			char *p1 = (char *)heap_caps_malloc(3600, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
			char *p2 = (char *)heap_caps_malloc(3600, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
			if (!rt || !p1 || !p2) {
				printf("内存不足\n");
				free(rt); free(p1); free(p2);
				continue;
			}
			rt[0] = p1[0] = p2[0] = '\0';
			size_t l1 = 3500, l2 = 3500;
			nvs_handle_t h;
			if (nvs_open("lark", NVS_READONLY, &h) == ESP_OK) {
				if (nvs_get_str(h, "lark_rt1", p1, &l1) == ESP_OK &&
				    nvs_get_str(h, "lark_rt2", p2, &l2) == ESP_OK) {
					snprintf(rt, 7200, "%s%s", p1, p2);
					printf("RTBEGIN%sRTEND\n", rt);
				} else {
					printf("读取失败\n");
				}
				nvs_close(h);
			}
			free(rt); free(p1); free(p2);
		} else if (strcmp(line, "lark refresh") == 0) {
			/* 手动强制走一遍 token 刷新。
			 * 用途：验证/排查刷新链路，以及 AT 出怪问题时应急
			 *（不用重新授权）。一次性 RT 用掉就没了，所以只手动触发。 */
			lark_request_refresh();
			printf("已标记，下一轮拉数据时强制刷新（最多等 %d 秒）\n", TODO_REFRESH_SEC);
		} else if (strncmp(line, "lark rt ", 8) == 0) {
			/* 注入新的 refresh_token。飞书那个是一次性的，
			 * 响应丢了就废了 —— 有这条命令就不用拆板子烧 NVS。 */
			const char *rt = line + 8;
			if (lark_auth_set_refresh_token(rt)) {
				printf("✓ refresh_token 已写入 NVS（回读校验通过），3 秒后重启生效\n");
				vTaskDelay(pdMS_TO_TICKS(3000));
				esp_restart();
			} else {
				printf("✗ 写入失败\n");
			}
		} else if (strcmp(line, "next") == 0) {
			/* 模拟按一次 KEY（前进一格）—— 不用走到板子跟前就能翻页，
			 * 也方便量「按键 -> 上屏」的延迟。 */
			BtnEvent bev;
			bev.value = 0;
			xQueueSend(s_btn_q, &bev, 0);
			printf("已发送一次翻页事件\n");
		} else if (strcmp(line, "status") == 0) {
			TodoMode cm;
			int cp;
			todo_layout_screen_unpack(s_screen, g_state, &cm, &cp);
			printf("screen    : %d (%s 第%d页)\n", s_screen, MODE_NAME[cm], cp + 1);
			printf("server    : %s:%u\n", s_server_ip, s_server_port);
			printf("net       : %s\n",
				   (xEventGroupGetBits(s_net_ev) & NET_UP) ? "up" : "down");
			printf("buttons   : ");
			for (int i = 0; i < 3; i++) {
				printf("%sGPIO%d ", MODE_NAME[i],
				       s_btn_pin[i] >= 0 ? s_btn_pin[i] : -1);
			}
			printf("\n");
		} else {
			printf("命令: wifi <ssid> [pass] | host <ip> | status | next\n"
			       "      lark refresh   强制走一遍 token 刷新\n"
			       "      lark rtout     打印当前 refresh_token\n"
			       "      lark rt <rt>   注入新的 refresh_token\n");
		}
	}
}

// ---------------------------------------------------------------------------
// main loop: refresh every TODO_REFRESH_SEC, and immediately on a keypress
// ---------------------------------------------------------------------------
static void fetch_task(void *arg)
{
	static bool s_time_warned = false;
	static bool s_time_printed = false;
	/* 这块 16KB 放 PSRAM：内部堆要留给 TLS（esp_tls_t 需要一大块连续内存，
	 * 碎片化后会 mbedtls_ssl_setup -0x7F00）。 */
#if TODO_FONT_IN_PARTITION
	/* 加载点阵字库（独立 flash 分区）。
	 * 分块读 + 每块让出 CPU：一次读 1MB 会让 flash 连续忙几百毫秒，
	 * WiFi 正在跑的话收包/定时器处理被拖住 -> 崩在 sta_input（JTAG 抓到过）。
	 * 16KB 一块，每块只阻塞 flash 几毫秒，WiFi 扛得住。
	 *
	 * 也**必须**放在这个任务里：app_main 的栈只有 3584 字节，
	 * 在那里读 1MB 分区 + 解析的调用链会把栈撑爆
	 * （JTAG 抓到 PC 跳进 flash 里的固件数据 = 栈被写坏）。 */
	{
		static bool s_font_done = false;
		if (!s_font_done) {
			s_font_done = true;
			const esp_partition_t *fp2 = esp_partition_find_first(
				ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "font");
			if (fp2) {
				const size_t CH = 16 * 1024;
				uint8_t *fb2 = (uint8_t *)heap_caps_malloc(fp2->size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
				if (fb2) {
					bool ok = true;
					for (size_t off = 0; off < fp2->size; off += CH) {
						size_t n = fp2->size - off;
						if (n > CH) n = CH;
						if (esp_partition_read(fp2, off, fb2 + off, n) != ESP_OK) { ok = false; break; }
						vTaskDelay(1);
					}
					if (ok && todo_font_load_mem(fb2, fp2->size)) {
						ESP_LOGI(TAG, "字库就绪（分区 %s, %d 字节）", fp2->label, (int)fp2->size);
					} else {
						ESP_LOGE(TAG, "字库加载失败");
						free(fb2);
					}
				} else {
					ESP_LOGE(TAG, "字库缓冲分配失败");
				}
			} else {
				ESP_LOGE(TAG, "找不到 font 分区（字库没烧？）");
			}
		}
	}
#endif
	char *json = (char *)heap_caps_malloc(STATE_BUF_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
	if (json == NULL) {
		ESP_LOGW(TAG, "PSRAM 分配失败，退回内部 RAM");
		json = (char *)malloc(STATE_BUF_SIZE);
	}
	if (json == NULL) {
		ESP_LOGE(TAG, "内存不够，拉不动了");
		/* 不能 return —— FreeRTOS 任务返回会 abort 整个固件 */
		while (true) vTaskDelay(pdMS_TO_TICKS(1000));
	}

	/* json 里装的是哪一路的数据：飞书真数据 还是 Mac 的兜底。
	 * 兜底只在**从来没拿到过**飞书数据时用；一旦拿到过，
	 * 后面拉取失败就保留上一份真数据，不拿兜底去覆盖它。 */
	bool have_real = false;

	/* RTC 慢存：软复位后还在，用来记住「上次因为网络不通而重启」的时刻，
	 * 免得重启成死循环。掉电会清零 —— 无所谓，最多多重启一次。 */
	RTC_DATA_ATTR static int64_t s_last_restart_us = 0;

	int64_t t_cycle = esp_timer_get_time();
	/* 还没成功拉到过内容之前，不等按键 —— 网络没通的时候干等一个
	 * 刷新周期才去试，启动后会白屏很久。
	 *
	 * 【坑】got_any==false 时 wait 原本是 0 —— 队列里没按键就立刻返回，
	 * 外层 while 又立刻重试，整圈变成**热循环**：每轮发两次 HTTPS，
	 * 实测 2 分钟打了 50 次 token 请求（refresh_token 失效时更是纯打转）。
	 * 现在按连续失败次数退避：0 -> 5s -> 15s -> 60s。 */
	bool got_any = false;
	int fail_streak = 0;

	/* 【即时响应】下次该拉数据的**绝对时刻**（us）。
	 *
	 * 以前按键和拉数据是**串在同一个循环**里的：按一次键 -> 换屏 ->
	 * 拉一遍飞书（3 个 HTTPS 请求）-> 才画出来 -> 屏幕才翻过去。
	 * 实测一次要 4 秒多，手感像卡住了。
	 *
	 * 现在拆开：
	 *   - 按键：**只**换屏 + 用**已经拉到的**数据立刻重绘（几十毫秒）
	 *   - 拉数据：按 next_fetch_us 自己的节奏走，**按键不会推迟它**
	 *
	 * 代价：翻页时看到的是上次拉到的数据（最多 60 秒前的）。
	 * 这个取舍值得 —— 摆件的交互必须是即时的，数据晚一分钟没人会在意。 */
	int64_t next_fetch_us = esp_timer_get_time();

	while (true) {
		BtnEvent ev;
		TickType_t wait;
		if (!got_any) {
			if (fail_streak == 0) {
				wait = 0;                 /* 第一次不等按键，立刻试一次 */
			} else {
				int backoff;
				if (lark_auth_dead()) {
					/* refresh_token 已被作废 —— 重试多少次都不会成功，
					 * 每 60 秒打一次飞书纯属浪费。退到 5 分钟。 */
					backoff = 300;
					if (fail_streak == 1) {
						ESP_LOGE(TAG, "凭据已失效：refresh_token 被作废，需要重新授权");
					}
				} else {
					backoff = (fail_streak <= 2) ? 5 : (fail_streak <= 5 ? 15 : 60);
				}
				wait = pdMS_TO_TICKS(backoff * 1000);
			}
		} else {
			/* 最多等到「下次该拉数据」的时刻。
			 * 期间按键随时能打断 —— 按完会回到这里重算。 */
			int64_t due_us = next_fetch_us - esp_timer_get_time();
			if (due_us <= 0) {
				wait = 0;                 /* 到点了 */
			} else {
				int64_t ms = (due_us + 999) / 1000;
				if (ms > (int64_t)TODO_REFRESH_SEC * 1000) {
					ms = (int64_t)TODO_REFRESH_SEC * 1000;
				}
				wait = pdMS_TO_TICKS(ms);
			}
		}
		// wait for a button press; time out into a normal refresh cycle
		if (xQueueReceive(s_btn_q, &ev, wait) == pdTRUE) {
			/* 一次按键 = 前进一格。
			 * 序列是「待机 -> 待办各页 -> 日程各页 -> 待机」，
			 * 待办有多少页就占多少格，翻完自动回到待机。
			 * 切屏和翻页合成同一个动作，不用区分长短按。 */
			TodoMode m = TODO_MODE_STANDBY;
			int pg = 0;
			if (todo_state_parse_json(json, g_state)) {
				int total = todo_layout_screen_count(g_state);
				s_screen = (s_screen + 1) % total;
				todo_layout_screen_unpack(s_screen, g_state, &m, &pg);
				ESP_LOGI(TAG, "第 %d/%d 页 -> %s", s_screen + 1, total, MODE_NAME[m]);
			} else {
				s_screen = (s_screen + 1) % 3;
				todo_layout_screen_unpack(s_screen, g_state, &m, &pg);
			}
			/* ★ 立刻重绘 —— 用**上次拉到的**数据，不碰网络。
			 * 这是「按键即时响应」的关键：画完马上上屏，
			 * 不用等 3 个 HTTPS 请求跑完。 */
			RenderAndBlit(json);
			/* 导出这一屏的帧 —— Mac 侧拿它和本地预览**逐像素**比对，
			 * 验收就不用人盯着屏看了。 */
			dump_frame(s_screen, MODE_NAME[m], pg, s_last_render_now, json);
			continue;   /* 拉数据的时间表不受按键影响 */
		}

		if (!(xEventGroupGetBits(s_net_ev) & NET_UP)) {
			next_fetch_us = esp_timer_get_time() + 500 * 1000;
			vTaskDelay(pdMS_TO_TICKS(500));
			continue;
		}

		/* 等 SNTP 校时。没校准就拉数据，拉回来的是 1970 年的空档。 */
		if (time(NULL) < 1600000000) {
			if (!s_time_warned) {
				ESP_LOGW(TAG, "还没校时，先等等");
				s_time_warned = true;
			}
			next_fetch_us = esp_timer_get_time() + 1000 * 1000;
			vTaskDelay(pdMS_TO_TICKS(1000));
			continue;
		}
		if (s_time_warned) {
			s_time_warned = false;
		}
		if (!s_time_printed) {
			/* 第一次拉到数据时，把板子的时间三种格式都打出来，
			 * 一眼就能看出时区/偏移对不对。 */
			s_time_printed = true;
			time_t t = time(NULL);
			struct tm lt, gt;
			localtime_r(&t, &lt);
			gmtime_r(&t, &gt);
			ESP_LOGI(TAG, "板子时间: epoch=%lld  本地=%04d-%02d-%02d %02d:%02d:%02d  UTC=%02d:%02d:%02d",
			         (long long)t, lt.tm_year + 1900, lt.tm_mon + 1, lt.tm_mday,
			         lt.tm_hour, lt.tm_min, lt.tm_sec, gt.tm_hour, gt.tm_min, gt.tm_sec);
		}

		send_who();          // ask the Mac where it is (cheap, every cycle)

#if TODO_SOURCE == TODO_SOURCE_MAC_BITMAP
		/* 画面是 Mac 渲好的位帧，直接贴。改设计只要重启服务端。 */
		TodoMode bm;
		int bp;
		todo_layout_screen_unpack(s_screen, g_state, &bm, &bp);
		const char *mode = MODE_NAME[bm];
		if (FetchFrame(mode)) {
			if (memcmp(s_frame, s_shown, FRAME_BYTES) != 0) {
				BlitFrame(s_frame);
				ESP_LOGI(TAG, "%s 帧已更新", mode);
			} else {
				ESP_LOGI(TAG, "%s 帧没变", mode);
			}
		} else {
			ESP_LOGW(TAG, "拉 %s 帧失败，屏上保持上一帧", mode);
		}
#else
		/* 板子自己拉飞书、自己渲染。拉不到才退回 Mac 的 state.json ——
		 * 这样飞书没授权/断网时屏上还是有点东西，不至于黑屏。 */
		bool got = false;
		/* 失败时立刻重试一次：偶发的 TLS 缓冲分配失败是概率性的，
		 * 等 10 秒下一个周期才重试的话，屏上会明显顿一下。 */
		for (int attempt = 0; attempt < 2 && !got; attempt++) {
			if (attempt > 0) {
				ESP_LOGW(TAG, "重试拉取");
				vTaskDelay(pdMS_TO_TICKS(1500));
			}
		if (lark_build_state(json, STATE_BUF_SIZE)) {
			got = true;
			have_real = true;
			s_err = TODO_ERR_NONE;
		} else if (!have_real && FetchState(json, STATE_BUF_SIZE)) {
			/* 只有从来没拿到过飞书数据时才用 Mac 的数据顶上。
			 * 已经拿到过真数据的话，拉取失败就保留上一份 ——
			 * 拿兜底去覆盖真数据会让屏上出现"看起来正常"的假内容。 */
			ESP_LOGW(TAG, "飞书拉不到，用 Mac 的数据顶着");
			got = true;
			/* 用的其实是 Mac 的兜底数据，飞书是坏的 —— 要让人知道 */
			s_err = lark_error_state();
		} else if (have_real) {
			ESP_LOGW(TAG, "飞书这次没拉到，保留上一次的据");
			got = true;   /* json 里还是上次的内容，直接重绘 */
			s_err = lark_error_state();
		}
		}
		/* 资源占用实测：一个周期里 CPU 实际忙了多久 + 还剩多少内存 */
		{
			int64_t busy_us = esp_timer_get_time() - t_cycle;
			int cjm = 0, cjf = 0;
			lark_api_mem_stats(&cjm, &cjf);
			ESP_LOGI(TAG, "资源: 内部RAM余%d PSRAM余%d 堆余%d/最小%d 本轮忙%dms (周期%dms) cJSON活跃=%d",
			         (int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
			         (int)heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT),
			         (int)esp_get_free_heap_size(),
			         (int)esp_get_minimum_free_heap_size(),
			         (int)(busy_us / 1000), TODO_REFRESH_SEC * 1000,
			         cjm - cjf);
			t_cycle = esp_timer_get_time();
		}

		if (got) {
			got_any = true;
			fail_streak = 0;
			/* 数据**变了**才把 state.json 打到串口。
			 *
			 * Mac 端拿这份输出做排版预览 —— 和屏上**像素级一致**，
			 * 因为它就是屏上那份数据。而且 Mac 端**不需要自己的飞书 token**：
			 * 一组 refresh_token 只能用一次，Mac 拿板子的那组去刷新，
			 * 等于把板子的凭据打死。两边各签一组又是两份会各自过期的东西。
			 *
			 * updated_at 每分钟都变，但数据可能没变 —— 比较时跳过它，
			 * 否则每轮都会重刷一遍。 */
			static char *s_last_dump = NULL;
			if (s_last_dump == NULL) {
				s_last_dump = (char *)heap_caps_malloc(STATE_BUF_SIZE,
				                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
				if (s_last_dump != NULL) s_last_dump[0] = '\0';
			}
			if (s_last_dump != NULL) {
				const char *cur  = skip_updated_at(json);
				const char *prev = skip_updated_at(s_last_dump);
				/* 诊断：这个判断本身出过错（该吐没吐），
				 * 所以把判断依据打出来，别再靠猜。 */
				ESP_LOGI(TAG, "dump 判断: cur=%d字节 prev=%d字节 same=%d",
				         (int)strlen(cur), (int)strlen(prev),
				         (int)(strcmp(cur, prev) == 0));
				if (strcmp(cur, prev) != 0) {
					strlcpy(s_last_dump, json, STATE_BUF_SIZE);
					/* 分块打印：一次 printf 17KB 会把 USB TX 缓冲冲爆，
					 * 日志缺字节（UTF-8 截断 -> 中文乱码）。每块 512 字节、
					 * 块间让出 2ms，USB 侧来得及取走。 */
					printf("\n===STATE_JSON_BEGIN===\n");
					{
						size_t jn = strlen(json);
						for (size_t j = 0; j < jn; j += 512) {
							size_t kn = (jn - j > 512) ? 512 : (jn - j);
							fwrite(json + j, 1, kn, stdout);
							fflush(stdout);
							vTaskDelay(pdMS_TO_TICKS(2));
						}
					}
					printf("\n===STATE_JSON_END===\n");
					fflush(stdout);
				}
			}
			RenderAndBlit(json);
		} else {
			fail_streak++;
			s_err = lark_error_state();
			ESP_LOGW(TAG, "数据也拉不到（连续第 %d 次失败）：%s", fail_streak,
			         s_err == TODO_ERR_AUTH ? "凭据失效" :
			         s_err == TODO_ERR_NVS  ? "凭据写盘失败" : "网络/服务器问题");
			/* 也重绘一次 —— 否则屏上永远停在开机美术图，
			 * 看上去一切正常，实际上早就死了。 */
			RenderAndBlit(json);

			/* ---- 网络自愈 ----
			 *
			 * 【为什么需要】2026-11-23 实测遇到一次：
			 * 板子的 WiFi **二层是活的**（ARP 有应答、IP 还在、status 说 net: up），
			 * 但 **TCP 一个都建不起来** —— 每个请求 45 秒超时。
			 * 复位一下就全好了，是 WiFi 站端驱动进了坏状态（多半是 DHCP 租约/ARP 卡住）。
			 * 不自愈的话，这台摆件就静静地停在那儿，只有屏上写着「连不上服务器」。
			 *
			 * 判据：**连着（NET_UP）但连不通** —— 这和「根本没连上 WiFi」是两回事，
			 * 后者 WiFi 驱动自己会重连。 */
			/* 【阈值】第 1 轮就动手。
			 * 一轮失败 = 3 个请求 × 45 秒超时 = 已经两分多钟连不上了，
			 * 这不是「网络抖一下」，是**真的不通**了。
			 * 而且这个故障的特征是**卡在某一层**：
			 *   - 卡在 DNS：getaddrinfo returns 202（解析不了域名）
			 *   - 卡在 TCP：select() timeout（域名解析了，连不上）
			 * 两种都是「板子的 IP/ARP 状态僵死」，重连 WiFi（重新 DHCP）
			 * 都能治好 —— 所以第 1 轮就该动手，别等 3 轮。
			 * 代价：重连一次要 5 秒左右，可以接受。 */
			if (fail_streak == 1) {
				/* 重连 WiFi：会重新走一遍 DHCP，僵死的 IP/ARP 状态一并清掉。
				 * 【注意】这里**不能**拿 NET_UP 当条件 ——
				 * NET_UP 正是「WiFi 掉了」时被清掉的那个标志，
				 * 拿它当条件 = 最常见的那种故障永远不触发自愈。 */
				ESP_LOGW(TAG, "连不上（第 %d 次）—— 重连 WiFi", fail_streak);
				esp_wifi_disconnect();
				vTaskDelay(pdMS_TO_TICKS(500));
				esp_wifi_connect();
			} else if (fail_streak == 4) {
				/* 驱动自己重连也不行（连不上 AP，一直刷 wifi dropped）
				 * → 整个 WiFi 驱动重启 */
				ESP_LOGW(TAG, "重连 WiFi 没用（第 %d 次）—— 重启 WiFi 驱动", fail_streak);
				esp_wifi_stop();
				vTaskDelay(pdMS_TO_TICKS(1000));
				esp_wifi_start();
				vTaskDelay(pdMS_TO_TICKS(500));
				esp_wifi_connect();
			}
			/* 还是不行就整机重启。凭据在 NVS 里，重启后能自己回来。
			 * 用 RTC 慢存记住上次重启时间 —— 30 分钟内不重复重启，
			 * 免得「网络不通 -> 重启 -> 还不通 -> 再重启」变成死循环。 */
			if (fail_streak == 8) {
				int64_t now_us = esp_timer_get_time();
				if (now_us - s_last_restart_us > 30LL * 60 * 1000 * 1000) {
					s_last_restart_us = now_us;
					ESP_LOGE(TAG, "重启 WiFi 驱动也没用 —— 整机重启");
					vTaskDelay(pdMS_TO_TICKS(500));
					esp_restart();
				}
			}
		}
#endif
		/* 设定**下次**拉数据的绝对时刻。
		 * 成功 -> 一个刷新周期后；失败 -> 按连续失败次数退避。
		 * 【关键】用绝对时刻而不是「再等 N 秒」——
		 * 这样按键插进来也不会把刷新节奏推后。 */
		{
			int64_t now_us = esp_timer_get_time();
			int secs;
			if (got_any) {
				secs = TODO_REFRESH_SEC;
			} else {
				secs = lark_auth_dead() ? 300
				     : (fail_streak <= 2 ? 5 : (fail_streak <= 5 ? 15 : 60));
			}
			next_fetch_us = now_us + (int64_t)secs * 1000 * 1000;
		}
	}
}

extern "C" void app_main(void)
{
	/* 中国时区。飞书给的时间戳是 UTC 秒，日程/倒计时都要本地时间。 */
	setenv("TZ", "CST-8", 1);
	tzset();

	/* 大块内存统一走这里：优先 PSRAM。
	 * 内部堆是稀缺资源 —— mbedtls 每次 TLS 握手要一块 16KB 的**连续**
	 * 内部内存（IN_CONTENT_LEN=16384）。静态数组和应用缓冲全挤在内部堆里，
	 * 会把「最大连续块」切到 16KB 以下，握手就申请不到，板子直接挂。
	 * 所以：所有大缓冲进 PSRAM，内部堆只留给 TLS 和 WiFi 驱动。 */
	auto alloc_big = [](size_t n, const char *what) -> void * {
		void *p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
		if (p == NULL) {
			ESP_LOGW(TAG, "PSRAM 分配 %s (%d 字节) 失败，退回内部 RAM", what, (int)n);
			p = heap_caps_malloc(n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
		}
		if (p == NULL) {
			ESP_LOGE(TAG, "分配 %s (%d 字节) 失败", what, (int)n);
		}
		return p;
	};

	s_frame = (uint8_t *)alloc_big(FRAME_BYTES, "s_frame");
	s_shown = (uint8_t *)alloc_big(FRAME_BYTES, "s_shown");
	g_state  = (TodoState *)alloc_big(sizeof(TodoState), "g_state");
	if (!s_frame || !s_shown || !g_state) {
		ESP_LOGE(TAG, "关键缓冲分配失败");
		while (true) vTaskDelay(pdMS_TO_TICKS(1000));
	}
	memset(g_state, 0, sizeof(*g_state));

	/* 打印一下内部堆的「最大连续块」—— 这个数比「还剩多少」更能说明
	 * TLS 还能不能握手成功。低于 20KB 就要警惕。 */
	/* WiFi 驱动每隔几秒打一条 [ADDBA]RX DELBA 刷屏。USB-Serial/JTAG 的
	 * TX 缓冲一满就**丢字节**，日志被丢得缺字节（UTF-8 从中间断开 ->
	 * 中文变成乱码），后面的诊断信息全看不清。压掉它。 */
	esp_log_level_set("wifi", ESP_LOG_ERROR);
	esp_log_level_set("net80211", ESP_LOG_ERROR);

	lark_api_install_mem_hooks();   /* 泄漏定位用 */

	ESP_LOGI(TAG, "内部堆: 余%d 最大连续块%d | PSRAM 余%d",
	         (int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
	         (int)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
	         (int)heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));

	/* 自愈：立刻把 GPIO0 配回上拉。
	 * GPIO0 是 strapping 脚，复位瞬间被采样来决定进不进 download 模式。
	 * 如果之前的固件（或别的原因）把它配成了下拉，软件复位不会清 GPIO
	 * 寄存器 -> 板子会永久卡在 download 模式，app 永远起不来。
	 * 这里第一时间纠正，app 一跑起来就恢复正常复位行为。 */
	{
		/* GPIO 0 是 strapping 脚（BOOT），复位瞬间被采样来决定进不进
		 * download 模式。如果是低的，app 永远起不来。
		 *
		 * 这里把它配成**推挽输出高**（不只是输入+内部上拉）——
		 * 推挽输出能 overpower 板子上可能存在的弱下拉，
		 * 让后续任何复位都能正常启动。
		 * GPIO 3/45/46 同理，全部拉高。 */
		gpio_config_t fix = {};
		fix.pin_bit_mask = (1ULL << 0) | (1ULL << 3) | (1ULL << 45) | (1ULL << 46);
		fix.mode = GPIO_MODE_INPUT;
		fix.pull_up_en = GPIO_PULLUP_ENABLE;
		fix.pull_down_en = GPIO_PULLDOWN_DISABLE;
		fix.intr_type = GPIO_INTR_DISABLE;
		gpio_config(&fix);

		/* GPIO0 推挽拉高 */
		gpio_config_t out = {};
		out.pin_bit_mask = 1ULL << 0;
		out.mode = GPIO_MODE_OUTPUT;
		out.intr_type = GPIO_INTR_DISABLE;
		gpio_config(&out);
		gpio_set_level((gpio_num_t)0, 1);
		gpio_hold_en((gpio_num_t)0);

		ESP_LOGI(TAG, "GPIO0 已推挽拉高并 hold，strapping 脚全部拉高");
	}

	/* UserApp_AppInit() 属于 LVGL demo 那套（已摘掉），不再调用。
	 * 显示初始化由 port_bsp 的 RlcdPort 完成。 */
	RlcdPort.RLCD_Init();
#if TODO_MODE
	/* 开机自检图：全黑底 + 中间一个白块。
	 * 屏一亮就能分辨「固件根本没跑到画图」还是「跑了但后面出问题」——
	 * 上一版白屏时没法区分，这一条能一眼看出来。 */
	{
		/* 15KB 数组必须放堆 —— app_main 的栈只有 3584 字节，
		 * 放栈上直接栈溢出，固件启动即崩（屏白 + 无 console 输出）。
		 * 而且必须放 PSRAM：15KB 静态数组会把内部堆切成碎片，
		 * WiFi 的 TX 帧池申请不到连续块（崩在 ieee80211_mgmt_output）。 */
		uint8_t *selftest = (uint8_t *)alloc_big(FRAME_BYTES, "selftest");
		if (selftest == NULL) {
			ESP_LOGE(TAG, "自检图缓冲分配失败");
			while (true) vTaskDelay(pdMS_TO_TICKS(1000));
		}
		memset(selftest, 0x00, FRAME_BYTES);   /* 全黑 = 墨 */
		for (int y = 120; y < 180; y++) {
			for (int x = 160; x < 240; x++) {
				int idx = y * (LCD_WIDTH / 8) + (x >> 3);
				selftest[idx] |= (uint8_t)(0x80 >> (x & 7));   /* 白块 */
			}
		}
		/* 刷屏挪到 NetStart 之后 —— WiFi 启动期间别抢 SPI/flash 总线 */
		s_selftest = selftest;
	}

	ESP_LOGI(TAG, "启动：先起网络，再显示自检图");
	/* 自检图和字库读取都挪到 NetStart 之后 ——
	 * WiFi 启动期间不要碰 SPI 和大块 flash：
	 * SPI 刷屏(15000B)和读 1MB 字库都会抢 flash 总线，
	 * 让 WiFi 扫描超时处理去 disarm 定时器时撞上 esp_timer 的锁，
	 * 在 ISR 上下文里死锁（xt_utils_compare_and_set 自旋到看门狗复位）。 */

#if TODO_SOURCE != TODO_SOURCE_MAC_BITMAP
#if TODO_FONT_IN_PARTITION
	/* 字库实际读取挪到 fetch_task 里做了 —— app_main 的栈太小（3584 字节），
	 * 在这里读 1MB 分区 + 解析的调用链会把栈撑爆。 */
#endif
#else
	/* 画面由 Mac 渲染，板子用不到字库 */
	ESP_LOGI(TAG, "画面来源：Mac 渲染的位帧");
#endif

	// buttons —— 只配输入，不配内部上下拉。
	// 板子自带的上下拉才是真实的；配了内部的会和它打架，电平乱跳。
	// 另外：绝对不要碰 strapping 脚（0/3/45/46），
	// 配了会改变启动模式，soft reset 又不会清 GPIO 配置 -> 板子卡在 download 模式。
	{
		gpio_config_t bc = {};
		bc.pin_bit_mask = 1ULL << TODO_BTN_KEY;
		bc.mode = GPIO_MODE_INPUT;
		bc.pull_up_en = GPIO_PULLUP_DISABLE;
		bc.pull_down_en = GPIO_PULLDOWN_DISABLE;
		bc.intr_type = GPIO_INTR_DISABLE;
		ESP_ERROR_CHECK(gpio_config(&bc));
	}

	s_btn_q = xQueueCreate(8, sizeof(BtnEvent));

	/* 网络栈要先起来（NetStart 里会 esp_netif_init），
	 * 之后才能 socket() —— 早一步 lwIP 的 tcpip 线程还没启动，
	 * 会直接 assert "Invalid mbox" 重启循环。 */
	NetStart();

	/* 网络起来之后再做会碰 flash/SPI 的事（见上面那段注释） */
	if (s_selftest) {
		BlitFrame(s_selftest);
		ESP_LOGI(TAG, "自检图已上屏");
	}
#if TODO_SOURCE != TODO_SOURCE_MAC_BITMAP
#if TODO_FONT_IN_PARTITION
	/* 字库实际读取挪到 fetch_task 里了 —— app_main 的栈太小（3584 字节），
	 * 在这里读 1MB 分区 + 解析的调用链会把栈撑爆。 */
#endif
#endif

	// UDP discovery socket
	s_udp_fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (s_udp_fd >= 0) {
		int one = 1;
		setsockopt(s_udp_fd, SOL_SOCKET, SO_BROADCAST, &one, sizeof(one));
		setsockopt(s_udp_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
		struct sockaddr_in me = {};
		me.sin_family      = AF_INET;
		me.sin_port        = htons(DISCOVERY_PORT);
		me.sin_addr.s_addr = htonl(INADDR_ANY);
		if (bind(s_udp_fd, (struct sockaddr *)&me, sizeof(me)) < 0) {
			ESP_LOGW(TAG, "bind %d 失败，关掉自动发现", DISCOVERY_PORT);
			close(s_udp_fd);
			s_udp_fd = -1;
		}
	}

	/* 板子上只有 KEY 一个用户按键：
	 *   KEY  = GPIO18，低有效，板上已有 10K 外部上拉 + 100nF 去抖电容
	 *   BOOT = GPIO0（下载模式键，别用）
	 *   PWR  = 不是 GPIO（电源锁存 IC，长按关机）
	 * 所以 KEY 做「循环切换」：待机 -> 待办 -> 日程 -> 待机。
	 * 只配成输入，**不配内部上下拉** —— 板子自带的 10K 上拉才是真实的，
	 * 配了内部的会和它打架，电平乱跳 -> 无限误触发。 */
	s_btn_pin[0] = TODO_BTN_KEY;
	s_btn_low[0] = true;              /* KEY 是低有效 */
	s_btn_bound = 1;
	ESP_LOGI(TAG, "按键: GPIO%d (KEY, 低有效)", TODO_BTN_KEY);
	{
		gpio_config_t bc3 = {};
		bc3.pin_bit_mask = 1ULL << TODO_BTN_KEY;
		bc3.mode = GPIO_MODE_INPUT;
		bc3.pull_up_en = GPIO_PULLUP_DISABLE;
		bc3.pull_down_en = GPIO_PULLDOWN_DISABLE;
		bc3.intr_type = GPIO_INTR_DISABLE;
		ESP_ERROR_CHECK(gpio_config(&bc3));
	}

	xTaskCreate(btn_task, "btn", 3072, NULL, 5, NULL);
	xTaskCreate(console_task, "con", 6144, NULL, 3, NULL);
	if (s_udp_fd >= 0) {
		xTaskCreate(udp_task, "udp", 3072, NULL, 4, NULL);
	}
	xTaskCreate(fetch_task, "fetch", 20480, NULL, 6, NULL);

	while (true) {
		vTaskDelay(pdMS_TO_TICKS(1000));
	}
#else
	Lvgl_PortInit(400,300,Lvgl_FlushCallback);
	if(Lvgl_lock(-1)) {
		UserApp_UiInit();
  	  	Lvgl_unlock();
  	}
	UserApp_TaskInit();
#endif
}
