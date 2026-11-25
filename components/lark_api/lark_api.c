#include "lark_api.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <esp_log.h>
#include <esp_http_client.h>
#include <esp_crt_bundle.h>
#include <esp_heap_caps.h>
#include <nvs.h>
#include <nvs_flash.h>

#include "cJSON/cJSON.h"

static const char *TAG = "lark";

/* ---- 内存泄漏定位：统计 cJSON 的 malloc/free 次数 ----
 * 每轮打印 (malloc - free)，如果每轮都在涨，就是有节点没释放。 */
static int s_cj_malloc;
static int s_cj_free;
static void *cj_hook_malloc(size_t sz) { s_cj_malloc++; return malloc(sz); }
static void cj_hook_free(void *p) { if (p) s_cj_free++; free(p); }
void lark_api_install_mem_hooks(void)
{
	cJSON_Hooks h = { cj_hook_malloc, cj_hook_free };
	cJSON_InitHooks(&h);
}
void lark_api_mem_stats(int *m, int *f) { *m = s_cj_malloc; *f = s_cj_free; }

/* 凭据里有两段 5300 字符的 token = 10.8KB。放 .bss 会占内部 RAM，
 * 而内部堆必须留出一块 16KB 的**连续**内存给 mbedtls 握手
 * （MBEDTLS_SSL_IN_CONTENT_LEN=16384）。挪到 PSRAM。 */
static LarkAuth *g_auth;

/* ------------------------------------------------------------------ */
/* NVS                                                                 */
/* ------------------------------------------------------------------ */
static bool nvs_get_str2(const char *key, char *dst, size_t cap)
{
	nvs_handle_t h;
	if (nvs_open("lark", NVS_READONLY, &h) != ESP_OK) return false;
	size_t len = cap;
	esp_err_t e = nvs_get_str(h, key, dst, &len);
	nvs_close(h);
	if (e != ESP_OK) { dst[0] = '\0'; return false; }
	dst[cap - 1] = '\0';
	return true;
}

/* 飞书的 access_token / refresh_token 都有 4800~6900 字符，
 * 超过 NVS 单个 string 的 4000 字节上限，所以拆成两段存。
 * buffer 用 static —— 局部数组是 7KB，放在任务栈上太险。
 *
 * 单段最大长度：token 要对半分，而 token 长度随 scope 变 ——
 * scope 少时 ~4900，scope 全开时 ~6900（实测 6899）。
 * 6900 的一半是 3450，所以给到 3600。 */
static char s_sp1[3600], s_sp2[3600];

/* 返回 esp_err_t（不是 bool！）。
 * 之前返回 bool，被 `esp_err_t e_at = nvs_set_split(...)` 接住，
 * 失败时 e_at=1 -> esp_err_to_name(1) = "ERROR"，
 * 真正的错误码被吞掉，排查时只能看到一句没头没脑的 "ERROR"。 */
static esp_err_t nvs_get_split(nvs_handle_t h, const char *k1, const char *k2,
                                char *dst, size_t cap)
{
	size_t l1 = sizeof(s_sp1), l2 = sizeof(s_sp2);
	s_sp1[0] = s_sp2[0] = '\0';
	esp_err_t e = nvs_get_str(h, k1, s_sp1, &l1);
	if (e != ESP_OK) return e;
	e = nvs_get_str(h, k2, s_sp2, &l2);
	if (e != ESP_OK) return e;
	size_t n1 = strlen(s_sp1), n2 = strlen(s_sp2);
	if (n1 + n2 + 1 > cap) return ESP_ERR_INVALID_SIZE;
	memcpy(dst, s_sp1, n1);
	memcpy(dst + n1, s_sp2, n2);
	dst[n1 + n2] = '\0';
	return ESP_OK;
}

/* 存完回读校验 —— 飞书的 refresh_token 是一次性的，
 * 写丢了就永久失效，所以必须确认真的写进去了。 */
static bool nvs_verify_split(nvs_handle_t h, const char *k1, const char *k2,
                             const char *src)
{
	/* static：7100 字节放栈上太大，调用链在 fetch_task 里 */
	static char back[7100];
	if (nvs_get_split(h, k1, k2, back, sizeof(back)) != ESP_OK) return false;
	if (strlen(back) != strlen(src)) return false;
	return memcmp(back, src, strlen(src)) == 0;
}

static esp_err_t nvs_set_split(nvs_handle_t h, const char *k1, const char *k2,
                                const char *src)
{
	size_t total = strlen(src), half = total / 2;
	if (half >= sizeof(s_sp1) || (total - half) >= sizeof(s_sp2))
		return ESP_ERR_INVALID_SIZE;
	memcpy(s_sp1, src, half);
	memcpy(s_sp2, src + half, total - half);
	s_sp1[half] = '\0';
	s_sp2[total - half] = '\0';
	esp_err_t e = nvs_set_str(h, k1, s_sp1);
	if (e != ESP_OK) return e;
	return nvs_set_str(h, k2, s_sp2);
}

bool lark_auth_load(LarkAuth *a)
{
	memset(a, 0, sizeof(*a));
	a->port = LARK_PORT;
	a->use_tls = LARK_USE_TLS;
	strlcpy(a->host, LARK_HOST, sizeof(a->host));

	bool ok = nvs_get_str2("lark_app_id", a->app_id, sizeof(a->app_id));
	ok &= nvs_get_str2("lark_app_secret", a->app_secret, sizeof(a->app_secret));
	/* refresh_token / access_token 都是 4800+ 字符，NVS 里分两段存。 */
	{
		nvs_handle_t h;
		bool g = false;
		if (nvs_open("lark", NVS_READONLY, &h) == ESP_OK) {
			g = (nvs_get_split(h, "lark_rt1", "lark_rt2",
			                    a->refresh_token, sizeof(a->refresh_token)) == ESP_OK);
			/* access_token 也存下来：它有 2 小时有效期，
			 * 板子重启时直接复用，别去碰那个一次性的 refresh_token。 */
			if (nvs_get_split(h, "lark_at1", "lark_at2",
			                  a->access_token, sizeof(a->access_token)) == ESP_OK) {
				char exp[24] = {0};
				size_t le = sizeof(exp);
				if (nvs_get_str(h, "lark_at_exp", exp, &le) == ESP_OK) {
					a->token_expire = (time_t)atol(exp);
				}
			}
			nvs_close(h);
		}
		ESP_LOGI(TAG, "凭据: refresh_token=%d(%d字符) access_token=%d(过期%s)",
		         g, (int)strlen(a->refresh_token),
		         a->access_token[0] != '\0',
		         a->token_expire > time(NULL) ? "未到" : "已到");
	}
	if (!ok) ESP_LOGW(TAG, "NVS 里没有飞书凭据");

	/* 接口地址也能从 NVS 覆盖（联调时指向 Mac 上的假飞书） */
	char tmp[64];
	if (nvs_get_str2("lark_host", tmp, sizeof(tmp)) && tmp[0]) {
		strlcpy(a->host, tmp, sizeof(a->host));
	}
	if (nvs_get_str2("lark_port", tmp, sizeof(tmp)) && tmp[0]) {
		a->port = (uint16_t)atoi(tmp);
	}
	if (nvs_get_str2("lark_tls", tmp, sizeof(tmp)) && tmp[0]) {
		a->use_tls = (tmp[0] == '1');
	}
	ESP_LOGI(TAG, "接口: %s://%s:%u 凭据: %s", a->use_tls ? "https" : "http",
	         a->host, a->port, ok ? "有" : "无");
	return ok;
}

bool lark_auth_set_refresh_token(const char *rt)
{
	if (!rt || strlen(rt) < 100) {
		ESP_LOGE(TAG, "refresh_token 太短，不像是真的");
		return false;
	}
	nvs_handle_t h;
	if (nvs_open("lark", NVS_READWRITE, &h) != ESP_OK) {
		ESP_LOGE(TAG, "nvs_open 失败");
		return false;
	}
	/* 【坑】nvs_set_split 返回的是 esp_err_t（ESP_OK == 0），
	 * 直接接到 bool 上就是**反过来**的：写成功了 ok 反而是 false，
	 * 于是 commit 被跳过 —— 写进去的东西**根本没落盘**。
	 * 必须显式 == ESP_OK。 */
	bool ok = (nvs_set_split(h, "lark_rt1", "lark_rt2", rt) == ESP_OK);
	if (ok) {
		esp_err_t ce = nvs_commit(h);
		if (ce != ESP_OK) {
			ESP_LOGE(TAG, "NVS commit 失败: %s(0x%x) —— 写进去的东西没落盘",
			         esp_err_to_name(ce), (int)ce);
			ok = false;
		} else {
			ok = nvs_verify_split(h, "lark_rt1", "lark_rt2", rt);
		}
	}
	nvs_close(h);
	if (ok) {
		/* 换了凭据，旧的 access_token 不能再用了 */
		nvs_handle_t h2;
		if (nvs_open("lark", NVS_READWRITE, &h2) == ESP_OK) {
			nvs_erase_key(h2, "lark_at1");
			nvs_erase_key(h2, "lark_at2");
			nvs_erase_key(h2, "lark_at_exp");
			nvs_commit(h2);
			nvs_close(h2);
		}
		ESP_LOGI(TAG, "新的 refresh_token 已写入并回读校验通过（%d 字符）",
		         (int)strlen(rt));
	} else {
		ESP_LOGE(TAG, "写入 refresh_token 失败或回读不一致");
	}
	return ok;
}

bool lark_auth_save(const LarkAuth *a)
{
	nvs_handle_t h;
	/* 不能用 ESP_ERROR_CHECK：这里失败就 abort，会变成重启循环，
	 * 而且屏幕根本来不及显示错误。 */
	if (nvs_open("lark", NVS_READWRITE, &h) != ESP_OK) {
		ESP_LOGE(TAG, "nvs_open 失败，凭据没存进去");
		return false;
	}
	esp_err_t e = nvs_set_str(h, "lark_app_id", a->app_id);
	e |= nvs_set_str(h, "lark_app_secret", a->app_secret);

	/* refresh_token 有 4800+ 字符，超过 NVS 单个 string 的 4000 字节上限，
	 * 必须拆两段存（读的时候也是按 lark_rt1/lark_rt2 拼回来的）。 */
	size_t total = strlen(a->refresh_token);
	size_t half = total / 2;
	char p1[2500] = {0}, p2[2500] = {0};
	if (half >= sizeof(p1) || (total - half) >= sizeof(p2)) {
		ESP_LOGE(TAG, "refresh_token 长度 %d 超出拆分缓冲，放弃保存", (int)total);
		nvs_close(h);
		return false;
	}
	memcpy(p1, a->refresh_token, half);
	memcpy(p2, a->refresh_token + half, total - half);
	e |= nvs_set_str(h, "lark_rt1", p1);
	e |= nvs_set_str(h, "lark_rt2", p2);
	e |= nvs_commit(h);
	nvs_close(h);
	if (e != ESP_OK) {
		ESP_LOGE(TAG, "凭据写 NVS 失败: %s", esp_err_to_name(e));
		return false;
	}
	return true;
}

/* ------------------------------------------------------------------ */
/* HTTP                                                                */
/* ------------------------------------------------------------------ */

/* refresh_token 被作废（invalid_grant）时置位。
 * 只在「RT 真的死了」时置位 —— 网络失败、证书失败都不算。
 *
 * 【坑】标志必须在 http_req 里设：飞书对无效 RT 返回 **400**，
 * http_req 走 `status != 200` 分支会把响应体 free 掉，
 * 上层根本看不到 invalid_grant —— 之前就因为这个，
 * 板子每 60 秒打一次飞书，明知 RT 已死却从不退避。
 *
 * 定义放在 http_req 前面 —— 它要用。 */
static bool s_auth_dead = false;
bool lark_auth_dead(void) { return s_auth_dead; }

/* 凭据**写盘失败**。
 *
 * 【为什么单独立一个状态】
 * refresh_token 是**一次性**的：轮换时旧 RT 当场作废，
 * 新 RT 必须落到 NVS 里板子才能活过这次刷新。
 * 写失败时内存里那份还能用（到过期为止），但**重启就废了** ——
 * 而 2026-11-09 就是这么死的：NVS 空间不够，写 lark_rt2 失败，
 * NVS 里只剩半截 RT，板子从此拿不到 token，而日志只有一行没人看的 ESP_LOGE。
 *
 * 所以这个状态必须**上屏**：让人知道要重新授权，而不是看着一个死掉的屏发呆。 */
static bool s_nvs_broken = false;
bool lark_auth_broken(void) { return s_nvs_broken; }

static int http_req(const LarkAuth *a, const char *method, const char *path,
                    const char *body, const char *ctype,
                    const char *bearer, char **out, size_t *out_len)
{
	char url[256];
	snprintf(url, sizeof(url), "%s://%s:%u%s",
	         a->use_tls ? "https" : "http", a->host, a->port, path);

	esp_http_client_config_t cfg = {};
	cfg.url = url;
	/* 超时给到 45 秒 —— 【关键】飞书的 refresh_token 是「收到请求就作废」的：
	 * 响应一旦在路上丢了，客户端以为失败可重试，
	 * 但 RT 其实已经死了 —— 表现为「第一次超时、第二次 invalid_grant」，
	 * 真正的失败原因（网络丢包）被掩盖成「token 被撤销」。
	 * 等久一点，让 TCP 重传把响应送到。 */
	cfg.timeout_ms = 45000;
	/* 无条件挂证书包：协议是运行时从 NVS 决定的（联调走明文、正式走 https），
	 * 挂上不影响 http，只在 https 时才用到。 */
	cfg.crt_bundle_attach = esp_crt_bundle_attach;
	/* 请求头缓冲区默认只有 512 字节（DEFAULT_HTTP_BUF_SIZE），
	 * 而 "Authorization: Bearer <4777 字符>" 就有 4785 字节 ——
	 * 头会被 snprintf 截断，飞书收不到完整的 Content-Length，
	 * 请求体压根发不出去（报 missing grant_type）。
	 * 8192 够装下所有头。 */
	cfg.buffer_size_tx = 8192;
	/* 响应缓冲默认也只有 512 字节，飞书的响应头容易超。
	 * 响应体不走这里（自己 malloc 动态扩容），但头要先装下。 */
	cfg.buffer_size = 2048;

	esp_http_client_handle_t c = esp_http_client_init(&cfg);
	if (c == NULL) {
		ESP_LOGE(TAG, "http init 失败: %s", url);
		return -1;
	}

	esp_http_client_set_method(c, strcmp(method, "POST") == 0
	                                  ? HTTP_METHOD_POST : HTTP_METHOD_GET);
	esp_http_client_set_header(c, "Accept", "application/json");
	if (bearer) {
		/* access_token 有 4700+ 字符，栈上放不下 */
		size_t blen = strlen(bearer) + 16;
		char *h = (char *)malloc(blen);
		if (h == NULL) { esp_http_client_cleanup(c); return -1; }
		snprintf(h, blen, "Bearer %s", bearer);
		esp_http_client_set_header(c, "Authorization", h);
		free(h);      /* esp_http_client_set_header 会 strdup */
	}
	if (body && ctype) {
		esp_http_client_set_header(c, "Content-Type", ctype);
		esp_http_client_set_post_field(c, body, (int)strlen(body));
	}

	/* 响应缓冲区必须能增长 —— 飞书的待办列表可能好几 KB，
	 * 定长 buffer 会被写爆（堆溢出 -> LoadProhibited）。
	 *
	 * 【关键】这块必须放 PSRAM！ESP32-S3 内部堆只有 260KB，
	 * 待办响应 64KB 会把 buffer 撑到 128KB，全在内部 RAM 的话
	 * TLS context 申请不到内存 -> mbedtls_ssl_setup -0x7F00
	 * (MBEDTLS_ERR_SSL_ALLOC_FAILED)，第二次请求就连不上了。
	 * PSRAM 有 8MB，放这儿正合适。 */
	size_t cap = 8192;
	char *buf = (char *)heap_caps_malloc(cap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
	if (!buf) {
		ESP_LOGW(TAG, "PSRAM 分配 %d 失败，退回内部 RAM", (int)cap);
		buf = (char *)malloc(cap);
	}
	if (!buf) { esp_http_client_cleanup(c); return -1; }
	size_t total = 0;
	int status = 0;

	/* 注意：esp_http_client_open 的 write_len 参数会**覆盖** post_len
	 * （IDF 源码：client->post_len = write_len;）。
	 * 传 0 会把 set_post_field 设的长度抹掉 -> Content-Length: 0 -> body 发不出去。 */
	int write_len = (body && ctype) ? (int)strlen(body) : 0;
	ESP_LOGI(TAG, "V4 发请求: method=%s write_len=%d body=%s ctype=%s 内部RAM余=%d",
	         method, write_len, body ? "有" : "NULL", ctype ? "有" : "NULL",
	         (int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
	ESP_LOGI(TAG, "内部RAM: 总量余=%d 最大连续块=%d",
	         (int)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
	         (int)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
	if (esp_http_client_open(c, write_len) != ESP_OK) {
		ESP_LOGE(TAG, "open 失败: %s", url);
		free(buf);
		esp_http_client_cleanup(c);
		return -1;
	}
	/* 【关键】open() 只把请求头发出去，**body 必须自己写**。
	 * IDF 里 esp_http_client_send_post_data() 只在 esp_http_client_perform()
	 * 内部被调用（esp_http_client.c:1299），手动 open+fetch_headers 流程下
	 * 不会自动发 body —— 服务端等不到 body 就不回响应头，最后超时。
	 * 症状：TLS 握手成功、证书验证通过，然后 "Connection timed out"。 */
	if (write_len > 0) {
		int w = esp_http_client_write(c, body, write_len);
		if (w != write_len) {
			ESP_LOGE(TAG, "body 写 incomplete: %d/%d", w, write_len);
			free(buf);
			esp_http_client_cleanup(c);
			return -1;
		}
		ESP_LOGI(TAG, "body 已写出 %d 字节", w);
	}
	esp_http_client_fetch_headers(c);
	status = esp_http_client_get_status_code(c);
	int clen = esp_http_client_get_content_length(c);

	/* 按 Content-Length **一次性**把缓冲撑到够大。
	 * 之前是从 8KB 开始 realloc 翻倍：翻到 65536 时正好撞上
	 * CONFIG_SPIRAM_MALLOC_ALWAYSINTERNAL=65536，
	 * 分配被强制走内部 RAM -> 要一块 64KB 连续内部内存 -> 拿不到就失败。
	 * 一次性按 Content-Length 分配就没有这个问题。 */
	if (clen > 0 && (size_t)clen + 1024 > cap) {
		size_t ncap = (size_t)clen + 1024;
		char *nb = (char *)heap_caps_realloc(buf, ncap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
		if (nb == NULL) {
			ESP_LOGE(TAG, "响应缓冲扩到 %d 字节失败", (int)ncap);
			free(buf); esp_http_client_cleanup(c);
			return -1;
		}
		buf = nb;
		cap = ncap;
	}

	while (1) {
		if (total + 1024 > cap) {
			size_t ncap = cap * 2;
			if (ncap > 1024 * 1024) ncap = 1024 * 1024;
			char *nb = (char *)heap_caps_realloc(buf, ncap, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
			if (!nb) {
				/* PSRAM 扩容失败：退回内部 RAM 再说 */
				nb = (char *)realloc(buf, ncap);
			}
			if (!nb) { free(buf); esp_http_client_cleanup(c); return -1; }
			buf = nb;
			cap = ncap;
		}
		size_t room = cap - total - 1;
		int r = esp_http_client_read(c, buf + total, room);
		if (r < 0) {
			ESP_LOGE(TAG, "read 错误 %d", r);
			free(buf); esp_http_client_cleanup(c);
			return -1;
		}
		total += r;
		if (clen > 0 && total >= (size_t)clen) break;
		if (r == 0) break;
		if (total > 512 * 1024) break;
	}
	buf[total] = '\0';
	esp_http_client_close(c);
	esp_http_client_cleanup(c);

	ESP_LOGI(TAG, "%s %s -> %d (%d 字节)", method, path, status, (int)total);
	if (status != 200) {
		/* 飞书对「refresh_token 已作废」返回 400 + invalid_grant。
		 * 这里就地置位 —— 上层拿不到响应体（下面就 free 了）。 */
		if (strstr(buf, "invalid_grant")) {
			s_auth_dead = true;
			ESP_LOGE(TAG, "!! refresh_token 已被作废 —— 需要重新授权（device flow）");
		}
		ESP_LOGW(TAG, "响应: %.200s", buf);
		free(buf);
		return -2;
	}
	*out = buf;
	*out_len = total;
	return 0;
}

/* ------------------------------------------------------------------ */
/* token                                                               */
/* ------------------------------------------------------------------ */
bool lark_auth_token(LarkAuth *a)
{
	time_t now = time(NULL);
	/* 提前 15 分钟就换 access_token：
	 * 飞书的 refresh_token 是一次性的，刷新请求要是响应丢了，
	 * 那组 token 就永久废了 —— 所以要留足重试的机会，
	 * 别拖到最后一刻才去冒险。 */
#ifdef TODO_FORCE_TOKEN_REFRESH
	/* 调试用：强制忽略存下来的 access_token，走一遍刷新链路。 */
	ESP_LOGW(TAG, "强制刷新模式：忽略已存的 access_token");
	a->access_token[0] = '\0';
	a->token_expire = 0;
#endif
	if (a->access_token[0] && a->token_expire > now + 900) return true;
	if (a->app_id[0] == '\0' || a->app_secret[0] == '\0' || a->refresh_token[0] == '\0') {
		ESP_LOGW(TAG, "没有 app_id/app_secret/refresh_token，拿不到 token");
		return false;
	}

	/* 飞书 OAuth v2：refresh_token 换 access_token。
	 * refresh_token 是**一次性的** —— 每次刷新都返回新的，
	 * 必须立刻存回 NVS，否则下次就用不了了。 */
	/* refresh_token 有 4800+ 字符，栈上/定长 buffer 都放不下，
	 * 用 cJSON 动态构造。 */
	cJSON *jb = cJSON_CreateObject();
	if (jb == NULL) return false;
	cJSON_AddStringToObject(jb, "grant_type", "refresh_token");
	cJSON_AddStringToObject(jb, "client_id", a->app_id);
	cJSON_AddStringToObject(jb, "client_secret", a->app_secret);
	cJSON_AddStringToObject(jb, "refresh_token", a->refresh_token);
	char *body = cJSON_PrintUnformatted(jb);
	cJSON_Delete(jb);
	if (body == NULL) return false;
	/* 只打长度，**不打 body** —— body 里有 client_secret。
	 * 串口日志会被复制走，密钥不能进日志。 */
	ESP_LOGI(TAG, "诊断: rt长度=%d body长度=%d",
	         (int)strlen(a->refresh_token), (int)strlen(body));

	char *resp = NULL;
	size_t rlen = 0;
	int rc = http_req(a, "POST", "/open-apis/authen/v2/oauth/token",
	                  body, "application/json", NULL, &resp, &rlen);
	cJSON_free(body);
	if (rc != 0) return false;



	cJSON *root = cJSON_Parse(resp);
	free(resp);
	if (!root) { ESP_LOGE(TAG, "token 响应解析失败"); return false; }

	cJSON *code = cJSON_GetObjectItemCaseSensitive(root, "code");
	if (cJSON_IsNumber(code) && code->valueint != 0) {
		cJSON *msg = cJSON_GetObjectItemCaseSensitive(root, "msg");
		ESP_LOGE(TAG, "刷新失败 code=%d msg=%s", code->valueint,
		         msg && cJSON_IsString(msg) ? msg->valuestring : "?");
		cJSON_Delete(root);
		return false;
	}

	/* 响应可能是扁平的，也可能在 data 里 */
	cJSON *box = cJSON_GetObjectItemCaseSensitive(root, "data");
	if (!cJSON_IsObject(box)) box = root;

	const char *at = NULL, *rt = NULL;
	int expire = 7200;
	cJSON *v = cJSON_GetObjectItemCaseSensitive(box, "access_token");
	if (cJSON_IsString(v)) at = v->valuestring;
	v = cJSON_GetObjectItemCaseSensitive(box, "expires_in");
	if (cJSON_IsNumber(v)) expire = v->valueint;
	v = cJSON_GetObjectItemCaseSensitive(box, "refresh_token");
	if (cJSON_IsString(v)) rt = v->valuestring;

	if (!at) { cJSON_Delete(root); return false; }
	/* 【诊断】把刷新的响应结构原样记下来。
	 * 之前板子把轮换出来的新 refresh_token **静默丢掉**了
	 * （rt_changed 判定为 false），两小时后必然 invalid_grant ——
	 * 而日志里什么都看不出来。 */
	{
		/* 【不能把响应原样打进日志】—— 里面有完整的 access_token 和
		 * refresh_token。串口日志会被复制走，等于把凭据送出去。
		 * 诊断「有没有带新 RT」只需要**结构和长度**，够了。 */
		ESP_LOGI(TAG, "刷新响应: access_token=%d字符 refresh_token=%d字符 expires_in=%d",
		         (int)strlen(at ? at : ""), (int)strlen(rt ? rt : ""), expire);
	}
	if (!rt) {
		/* 一次性 RT 用掉就没了。如果响应里连新 RT 都没有，
		 * 这次轮换等于「白白消费掉唯一那张票」——
		 * 必须喊出来，不能静默继续。 */
		ESP_LOGE(TAG, "!! 刷新响应里没有 refresh_token —— "
		         "一次性 RT 已被消费且拿不到新的，两小时后必定失效！");
	} else {
		ESP_LOGI(TAG, "刷新响应带新 refresh_token（%d 字符），旧的 %d 字符",
		         (int)strlen(rt), (int)strlen(a->refresh_token));
	}


	strlcpy(a->access_token, at, sizeof(a->access_token));
	a->token_expire = time(NULL) + expire;

	/* 新 refresh_token 存回 NVS（旧的已经失效了） */
	bool rt_changed = (rt && strcmp(rt, a->refresh_token) != 0);
	if (rt_changed) {
		strlcpy(a->refresh_token, rt, sizeof(a->refresh_token));
	}

	/* access_token 也存下来：它 2 小时有效，板子重启时直接用，
	 * 不用再去动那个一次性的 refresh_token —— 少动一次少一次风险。 */
	nvs_handle_t h;
	if (nvs_open("lark", NVS_READWRITE, &h) == ESP_OK) {
		/* 【关键】写之前先把旧条目**删掉并提交**。
		 * NVS 是写时复制：覆盖一个 key 只会把旧 entry 标成 tombstone，
		 * 它占的那 2.4KB 数据区还在那儿等着 —— 页内找不到连续空间，
		 * nvs_set_str 就直接失败（NO_SPACE）。
		 * 实测就是这个坑：轮换时 access_token 写不进去，
		 * 于是新 refresh_token 一起丢了，两小时后必然 invalid_grant。
		 * 先 erase 再写，旧数据区立刻变成 0xFF 可复用。 */
		static const char *const OLD_KEYS[] = {
			"lark_rt1", "lark_rt2", "lark_at1", "lark_at2", "lark_at_exp"
		};
		for (size_t i = 0; i < sizeof(OLD_KEYS) / sizeof(OLD_KEYS[0]); i++) {
			nvs_erase_key(h, OLD_KEYS[i]);
		}
		nvs_commit(h);

		esp_err_t e_at = nvs_set_split(h, "lark_at1", "lark_at2", a->access_token);
		bool sok = (e_at == ESP_OK);
		char exp[24];
		snprintf(exp, sizeof(exp), "%ld", (long)a->token_expire);
		esp_err_t e_exp = nvs_set_str(h, "lark_at_exp", exp);
		if (e_exp != ESP_OK) sok = false;
		ESP_LOGI(TAG, "存 token: access_token=%d字符 ret=%s(0x%x)  at_exp=%s",
		         (int)strlen(a->access_token), esp_err_to_name(e_at), (int)e_at,
		         esp_err_to_name(e_exp));
		{
			/* NVS 分区用量 —— 空间不够时这里能直接看出来 */
			nvs_stats_t st;
			if (nvs_get_stats("lark", &st) == ESP_OK) {
				ESP_LOGI(TAG, "NVS lark: 已用 %u / 空闲 %u / 可用 %u / 共 %u 条",
				         (unsigned)st.used_entries, (unsigned)st.free_entries,
				         (unsigned)st.available_entries, (unsigned)st.total_entries);
			}
		}
		if (rt_changed) {
			/* 【坑】esp_err_t 直接接 bool = 语义反过来
			 * （ESP_OK 是 0，写成功反而被当成失败）。 */
			esp_err_t e_rt = nvs_set_split(h, "lark_rt1", "lark_rt2", a->refresh_token);
			if (e_rt != ESP_OK) {
				ESP_LOGE(TAG, "!! 新 refresh_token 写 NVS 失败: %s(0x%x) —— 这次轮换白费了",
				         esp_err_to_name(e_rt), (int)e_rt);
				sok = false;
				s_nvs_broken = true;   /* 屏上要显示「凭据写入失败」 */
			} else {
				/* 关键：回读确认。refresh_token 是一次性的，
				 * 写丢了这次轮换就白费了 —— 旧 token 已作废，新 token 没存住，
				 * 结果就是永远 invalid_grant。 */
				esp_err_t ce = nvs_commit(h);
				if (ce != ESP_OK) {
					ESP_LOGE(TAG, "!! NVS commit 失败: %s(0x%x) —— 写进去的没落盘",
					         esp_err_to_name(ce), (int)ce);
					sok = false;
					s_nvs_broken = true;
				} else if (!nvs_verify_split(h, "lark_rt1", "lark_rt2", a->refresh_token)) {
					ESP_LOGE(TAG, "!! refresh_token 写进 NVS 后回读不一致 —— 这次轮换可能白费了");
					sok = false;
					s_nvs_broken = true;
				} else {
					ESP_LOGI(TAG, "refresh_token 已轮换并回读校验通过（%d 字符）",
					         (int)strlen(a->refresh_token));
				}
			}
		}
		if (!rt_changed) {
			esp_err_t ce = nvs_commit(h);
			if (ce != ESP_OK) {
				ESP_LOGE(TAG, "!! NVS commit 失败: %s(0x%x)", esp_err_to_name(ce), (int)ce);
				sok = false;
			}
		} else if (!sok) {
			/* AT 至少要保住 —— 但 commit 也可能失败，必须看返回码 */
			esp_err_t ce = nvs_commit(h);
			if (ce != ESP_OK) {
				ESP_LOGE(TAG, "!! NVS commit 失败: %s(0x%x) —— AT 也没存住",
				         esp_err_to_name(ce), (int)ce);
			}
		}
		nvs_close(h);
		if (sok) s_nvs_broken = false;
		ESP_LOGI(TAG, "token 已存 NVS（access=%d refresh轮换=%d）", sok, rt_changed);
	} else {
		ESP_LOGE(TAG, "存 token 失败，NVS 打不开");
	}

	cJSON_Delete(root);
	s_auth_dead = false;
	ESP_LOGI(TAG, "token 拿到，有效期 %d 秒", expire);
	return true;
}

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */
static const char *pick_str(const cJSON *o, const char *const *names, int n)
{
	for (int i = 0; i < n; i++) {
		cJSON *v = cJSON_GetObjectItemCaseSensitive(o, names[i]);
		if (cJSON_IsString(v) && v->valuestring) return v->valuestring;
	}
	return NULL;
}

static bool pick_bool(const cJSON *o, const char *const *names, int n)
{
	for (int i = 0; i < n; i++) {
		cJSON *v = cJSON_GetObjectItemCaseSensitive(o, names[i]);
		if (cJSON_IsBool(v)) return cJSON_IsTrue(v);
		if (cJSON_IsNumber(v)) return v->valueint != 0;
	}
	return false;
}

/* 飞书任务 v2 判断「这条是不是已经做完」。
 *
 * 顶层**没有** completed 布尔字段，完成状态在这两处：
 *   - completed_at：有时间戳就是已完成（毫秒字符串）
 *   - items[]：子任务，completed=true 表示那条做完了
 * 只认 "completed"/"done" 这两个名字的话，全部任务都会被当成未完成。
 */
static bool lark_task_done(cJSON *it)
{
	/* 子任务全部完成 -> 父任务也算完成 */
	cJSON *subs = cJSON_GetObjectItemCaseSensitive(it, "items");
	if (cJSON_IsArray(subs)) {
		int sn = cJSON_GetArraySize(subs);
		int sd = 0;
		for (int k = 0; k < sn; k++) {
			cJSON *s = cJSON_GetArrayItem(subs, k);
			if (!cJSON_IsObject(s)) continue;
			cJSON *cb = cJSON_GetObjectItemCaseSensitive(s, "completed");
			if (cJSON_IsTrue(cb)) sd++;
		}
		if (sn > 0 && sd == sn) return true;
	}

	/* 顶层的 completed_at */
	cJSON *ca = cJSON_GetObjectItemCaseSensitive(it, "completed_at");
	if (cJSON_IsString(ca) && ca->valuestring && ca->valuestring[0]) {
		if (strcmp(ca->valuestring, "0") != 0) return true;
	}

	/* 兜底：万一字段名变了 */
	static const char *const kDone[] = { "completed", "done" };
	return pick_bool(it, kDone, 2);
}

/* completed_at 是**毫秒时间戳字符串**，"0" = 未完成。
 * 取不到（比如只靠子任务判定完成、顶层没写时间）返回 0。 */
static time_t lark_task_completed_at(const cJSON *it)
{
	const cJSON *ca = cJSON_GetObjectItemCaseSensitive(it, "completed_at");
	if (!cJSON_IsString(ca) || !ca->valuestring || !ca->valuestring[0]) return 0;
	if (strcmp(ca->valuestring, "0") == 0) return 0;
	long long ms = atoll(ca->valuestring);
	if (ms <= 0) return 0;
	return (time_t)(ms > 100000000000LL ? ms / 1000 : ms);
}

/* 从时间节点里取时间戳（秒或毫秒都认） */
static time_t pick_time(const cJSON *node)
{
	if (!node) return 0;
	if (cJSON_IsNumber(node)) {
		long long n = (long long)node->valuedouble;
		return (time_t)(n > 100000000000LL ? n / 1000 : n);
	}
	if (!cJSON_IsObject(node)) return 0;

	static const char *const keys[] = { "timestamp", "date", "time", "datetime" };
	for (int i = 0; i < 4; i++) {
		cJSON *v = cJSON_GetObjectItemCaseSensitive(node, keys[i]);
		if (cJSON_IsNumber(v)) {
			long long n = (long long)v->valuedouble;
			return (time_t)(n > 100000000000LL ? n / 1000 : n);
		}
		if (cJSON_IsString(v) && v->valuestring) {
			if (keys[i][0] == 't') {   /* timestamp */
				long long ms = atoll(v->valuestring);
				return (time_t)(ms > 100000000000LL ? ms / 1000 : ms);
			}
			struct tm tm = {0};
			if (sscanf(v->valuestring, "%d-%d-%d", &tm.tm_year, &tm.tm_mon, &tm.tm_mday) == 3) {
				tm.tm_year -= 1900; tm.tm_mon -= 1;
				return mktime(&tm);
			}
		}
	}
	return 0;
}

static void hm_str(time_t t, char *out, size_t cap)
{
	struct tm tm;
	localtime_r(&t, &tm);
	snprintf(out, cap, "%02d:%02d", tm.tm_hour, tm.tm_min);
}

/* ------------------------------------------------------------------ */
/* 拉数据                                                              */
/* ------------------------------------------------------------------ */
/* 取 data.items 数组的**副本**（调用方负责 cJSON_Delete） */
static cJSON *fetch_items(LarkAuth *a, const char *path)
{
	char *resp = NULL;
	size_t rlen = 0;
	if (http_req(a, "GET", path, NULL, NULL, a->access_token, &resp, &rlen) != 0) {
		return NULL;
	}
	cJSON *root = cJSON_Parse(resp);
	free(resp);
	if (!root) return NULL;

	cJSON *code = cJSON_GetObjectItemCaseSensitive(root, "code");
	if (cJSON_IsNumber(code) && code->valueint != 0) {
		cJSON *msg = cJSON_GetObjectItemCaseSensitive(root, "msg");
		ESP_LOGE(TAG, "接口 code=%d msg=%s", code->valueint,
		         msg && cJSON_IsString(msg) ? msg->valuestring : "?");
		cJSON_Delete(root);
		return NULL;
	}
	cJSON *data = cJSON_GetObjectItemCaseSensitive(root, "data");
	if (!cJSON_IsObject(data)) { cJSON_Delete(root); return NULL; }
	cJSON *items = cJSON_GetObjectItemCaseSensitive(data, "items");
	if (!cJSON_IsArray(items)) { cJSON_Delete(root); return NULL; }

	cJSON *copy = cJSON_Duplicate(items, true);
	cJSON_Delete(root);
	return copy;
}

static cJSON *fetch_tasks(LarkAuth *a)
{
	/* 飞书 task v2 支持 completed 参数，分两次拉：
	 *   completed=false -> 只给未完成的（实测 11 条 / 11.5KB）
	 *   completed=true  -> 给已完成的（取前 20 条 / 34.6KB，本地再按"今天"筛）
	 *
	 * 【为什么不能只拉 10 条】飞书按「最近更新」返回，前 10 条里
	 * 只有 3 条未完成（实际 11 条），今天刚完成的那条排在第 12 位根本拉不到 ——
	 * 表现就是「屏上数据是旧的」。
	 *
	 * 【为什么不一次拉 100 条】51 条 = 76KB 响应，没必要；
	 * 分开拉语义清楚，体积也可控。 */
	cJSON *open_items = fetch_items(a,
		"/open-apis/task/v2/tasks?page_size=100&completed=false");
	cJSON *done_items = fetch_items(a,
		"/open-apis/task/v2/tasks?page_size=20&completed=true");
	if (!open_items && !done_items) return NULL;

	cJSON *merged = cJSON_CreateArray();
	if (!merged) {
		if (open_items) cJSON_Delete(open_items);
		if (done_items) cJSON_Delete(done_items);
		return NULL;
	}
	cJSON *sets[2] = { open_items, done_items };
	for (int s = 0; s < 2; s++) {
		if (!sets[s]) continue;
		/* 注意：cJSON_DetachItemViaPointer(parent, item) 的第二个参数是
		 * **item 的指针**，不是索引！传索引会被当成指针解引用 ->
		 * LoadProhibited EXCVADDR=0x5（索引 5 的地址）。
		 * 而且摘一个少一个，所以每次都取第 0 个。 */
		cJSON *it;
		while ((it = cJSON_GetArrayItem(sets[s], 0)) != NULL) {
			cJSON_DetachItemViaPointer(sets[s], it);
			cJSON_AddItemToArray(merged, it);
		}
		cJSON_Delete(sets[s]);
	}
	return merged;
}

static cJSON *fetch_events(LarkAuth *a)
{
	time_t now = time(NULL);
	struct tm tm;
	localtime_r(&now, &tm);
	tm.tm_hour = 0; tm.tm_min = 0; tm.tm_sec = 0;
	time_t start = mktime(&tm);
	tm.tm_hour = 23; tm.tm_min = 59; tm.tm_sec = 59;
	time_t end = mktime(&tm);

	char path[220];
	snprintf(path, sizeof(path),
	         "/open-apis/calendar/v4/calendars/primary/events/instance_view"
	         "?start_time=%lld&end_time=%lld",
	         (long long)start, (long long)end);
	return fetch_items(a, path);
}

/* ------------------------------------------------------------------ */
/* 组装 state.json                                                      */
/* ------------------------------------------------------------------ */

/* 手动要求下一轮强制走一遍 token 刷新（串口命令 "lark refresh" 用）。
 *
 * 【为什么是运行时手动触发，不是编译期开关】
 * 编译期开关（TODO_FORCE_TOKEN_REFRESH）留在固件里的风险太大：
 * 一次刷新就**消费掉一张一次性的票**，开关每轮都开 = 每 60 秒烧掉一个 RT，
 * 而 RT 一旦响应丢失就永久作废。手动触发则完全可控，
 * 而且顺带成了一个应急手段：AT 出了怪问题，不用重新授权，敲一行命令就行。 */
static bool s_force_refresh = false;
void lark_request_refresh(void) { s_force_refresh = true; }

bool lark_build_state(char *out, size_t cap)
{
	/* 第一次进来时把凭据读出来 */
	static bool loaded = false;
	if (!loaded) {
		g_auth = (LarkAuth *)heap_caps_malloc(sizeof(LarkAuth), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
		if (g_auth == NULL) {
			g_auth = (LarkAuth *)malloc(sizeof(LarkAuth));
		}
		if (g_auth == NULL) {
			ESP_LOGE(TAG, "凭据内存分配失败");
			return false;
		}
		memset(g_auth, 0, sizeof(*g_auth));
		lark_auth_load(g_auth);
		loaded = true;
	}
	if (s_force_refresh) {
		s_force_refresh = false;
		ESP_LOGW(TAG, "手动强制刷新 access_token —— 走一遍完整刷新链路");
		g_auth->access_token[0] = '\0';
		g_auth->token_expire = 0;
	}
	if (!lark_auth_token(g_auth)) return false;

	cJSON *tasks = fetch_tasks(g_auth);
	if (!tasks) ESP_LOGW(TAG, "拉不到待办");
	cJSON *events = fetch_events(g_auth);
	if (!events) ESP_LOGW(TAG, "拉不到日程");
	/* 【原则】宁可明确失败，也不能给一份**残缺**的 state.json。
	 * 三个请求里挂一个就算失败 —— 否则屏上会显示「待办 0 条」，
	 * 而其实是有待办的（只是没拉到）。用户会以为活干完了。
	 * 整轮失败 -> 走错误页 + 网络自愈，比假装正常强。
	 *
	 * 【坑】early-return 之前**必须把已经拿到的树释放掉**！
	 * 漏了一棵 ~3400 节点的解析树，每轮漏一次 ->
	 * 内部 RAM 一夜之间从 109KB 掉到 26KB ->
	 * mbedtls 申请 16KB 连续内存失败 -> TLS 读失败 ->
	 * **刷新响应收不到（但请求已发出，RT 被消费）** -> invalid_grant
	 * -> 继续部分失败 -> 继续漏。死循环。
	 * （2026-11-24 实测：cJSON 活跃每轮 +273，内存 26KB，板子彻底死了） */
	if (!tasks || !events) {
		if (tasks)  cJSON_Delete(tasks);
		if (events) cJSON_Delete(events);
		return false;
	}

	int cjm0 = 0, cjf0 = 0;
	lark_api_mem_stats(&cjm0, &cjf0);

	cJSON *out_root = cJSON_CreateObject();
	cJSON *ta = cJSON_CreateArray();
	cJSON *ea = cJSON_CreateArray();

	time_t now = time(NULL);
	struct tm tm;
	localtime_r(&now, &tm);
	char upd[48];
	snprintf(upd, sizeof(upd), "%04d-%02d-%02d %02d:%02d",
	         tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min);

	/* 本地零点。用 mktime，不是 now % 86400 —— 后者是 **UTC 零点**，
	 * 本地是 CST-8，会差 8 小时（晚上 8 点前会被算成"昨天"）。 */
	struct tm tm_day = tm;
	tm_day.tm_hour = 0; tm_day.tm_min = 0; tm_day.tm_sec = 0;
	tm_day.tm_isdst = -1;
	time_t day_start = mktime(&tm_day);
	time_t day_end = day_start + 86400;

	/* ---- 待办 ---- */
	int open = 0, total = 0;
	if (cJSON_IsArray(tasks)) {
		int n = cJSON_GetArraySize(tasks);
		if (n > 64) n = 64;
		for (int i = 0; i < n; i++) {
			cJSON *it = cJSON_GetArrayItem(tasks, i);
			if (!cJSON_IsObject(it)) continue;
			static const char *const kSum[] = { "summary", "title", "name" };
			static const char *const kPri[]  = { "priority", "task_priority" };

			const char *sum = pick_str(it, kSum, 3);
			if (!sum || !*sum) continue;

			bool done = lark_task_done(it);
			/* 已完成只保留「今天完成的」。
			 * 沉底的语义是「今天搞定了什么」，不是「历史任务大全」——
			 * 飞书里存着几年的历史（实测 40 条已完成），
			 * 全堆到沉底页就是一屏几个月前的垃圾。
			 *
			 * completed_at 取不到（顶层没写、只靠子任务判定完成）时**保留**：
			 * 证明不了它是旧的就不该藏。 */
			if (done) {
				time_t ca = lark_task_completed_at(it);
				if (ca > 0 && (ca < day_start || ca >= day_end)) continue;
			}
			/* 【泄漏】t 必须在过滤**之后**创建。
			 * 之前是先 CreateObject 再判断要不要 continue，
			 * 于是每轮漏掉 16 个历史任务的对象+text 节点（32 个节点/轮），
			 * 内部 RAM 每轮掉 5KB —— 跑 20 轮就 OOM。 */
			cJSON *t = cJSON_CreateObject();
			cJSON_AddStringToObject(t, "text", sum);
			/* 诊断：看清完成状态到底在哪个字段（只打一次，别刷屏） */
			static bool s_dumped = false;
			if (i < 3 && !s_dumped) {
				s_dumped = true;
				cJSON *ca = cJSON_GetObjectItemCaseSensitive(it, "completed_at");
				cJSON *subs = cJSON_GetObjectItemCaseSensitive(it, "items");
				int sn = cJSON_IsArray(subs) ? cJSON_GetArraySize(subs) : -1;
				ESP_LOGI(TAG, "任务%d summary=%.20s completed_at=%s 子任务数=%d",
				         i, sum,
				         cJSON_IsString(ca) ? ca->valuestring : "(无)", sn);
				for (int k = 0; k < sn && k < 4; k++) {
					cJSON *s2 = cJSON_GetArrayItem(subs, k);
					if (!cJSON_IsObject(s2)) continue;
					cJSON *cb = cJSON_GetObjectItemCaseSensitive(s2, "completed");
					cJSON *ss = cJSON_GetObjectItemCaseSensitive(s2, "summary");
					ESP_LOGI(TAG, "   子%d completed=%d summary=%.20s", k,
					         cJSON_IsTrue(cb) ? 1 : 0,
					         cJSON_IsString(ss) ? ss->valuestring : "?");
				}
			}
			cJSON_AddBoolToObject(t, "done", done);

			const char *pri = pick_str(it, kPri, 2);
			char pr[8] = "";
			if (pri) {
				if (strncmp(pri, "p0", 2) == 0 || strncmp(pri, "p1", 2) == 0 ||
				    strcmp(pri, "urgent") == 0)      strcpy(pr, "high");
				else if (strncmp(pri, "p3", 2) == 0 || strcmp(pri, "low") == 0) strcpy(pr, "low");
			}
			cJSON_AddStringToObject(t, "pri", pr);

			time_t dt = pick_time(cJSON_GetObjectItemCaseSensitive(it, "due"));
			char dl[32] = "";
			if (dt > 0) {
				time_t today = now - (now % 86400);
				time_t mid = dt - (dt % 86400);
				long diff = (mid - today) / 86400;
				if (diff == 0) strcpy(dl, "今天");
				else if (diff == 1) strcpy(dl, "明天");
				else {
					struct tm d2;
					localtime_r(&dt, &d2);
					snprintf(dl, sizeof(dl), "%02d-%02d", d2.tm_mon + 1, d2.tm_mday);
				}
			}
			cJSON_AddStringToObject(t, "due", dl);

			cJSON_AddItemToArray(ta, t);
			total++;
			if (!done) open++;
		}
	}

	/* ---- 日程 ---- */
	/* 事件的时间戳要留着，才能判断"下一个" */
	time_t ev_start[32];
	bool  ev_now[32];
	int   n_ev = 0;

	if (cJSON_IsArray(events)) {
		int n = cJSON_GetArraySize(events);
		if (n > 32) n = 32;
		for (int i = 0; i < n; i++) {
			cJSON *it = cJSON_GetArrayItem(events, i);
			if (!cJSON_IsObject(it)) continue;
			static const char *const kSum[] = { "summary", "title", "subject" };
			const char *sum = pick_str(it, kSum, 3);
			if (!sum || !*sum) continue;

			time_t s = pick_time(cJSON_GetObjectItemCaseSensitive(it, "start_time"));
			time_t e = pick_time(cJSON_GetObjectItemCaseSensitive(it, "end_time"));
			if (s == 0) continue;

			cJSON *ev = cJSON_CreateObject();
			char hm[24];
			hm_str(s, hm, sizeof(hm));
			cJSON_AddStringToObject(ev, "start_hm", hm);
			if (e > s) {
				hm_str(e, hm, sizeof(hm));
				cJSON_AddStringToObject(ev, "end_hm", hm);
			} else {
				cJSON_AddStringToObject(ev, "end_hm", "");
			}

			char loc[64] = "";
			const char *lv = pick_str(it, (const char *const[]){ "location" }, 1);
			if (lv) {
				strlcpy(loc, lv, sizeof(loc));
			} else {
				cJSON *lobj = cJSON_GetObjectItemCaseSensitive(it, "location");
				if (cJSON_IsObject(lobj)) {
					cJSON *nm = cJSON_GetObjectItemCaseSensitive(lobj, "name");
					if (cJSON_IsString(nm)) strlcpy(loc, nm->valuestring, sizeof(loc));
				}
			}
			cJSON_AddStringToObject(ev, "location", loc);
			cJSON_AddStringToObject(ev, "title", sum);

			/* 零长度事件（end == start —— 飞书里「午餐」就是这样）
			 * 只算开始后的那一分钟。
			 * 原来写的是 (e <= s || now < e)：e<=s 时**从开始时间起永远是进行中**，
			 * 于是下午 6 点时间轴上还挂着 11:30 的黑块，看着像时间轴卡死。 */
			time_t end_eff = (e > s) ? e : s + 60;
			bool is_now = (now >= s && now < end_eff);
			cJSON_AddBoolToObject(ev, "now", is_now);

			ev_start[n_ev] = s;
			ev_now[n_ev] = is_now;
			cJSON_AddItemToArray(ea, ev);
			n_ev++;
		}
	}

	/* 标"下一个"：最近的未来事件 */
	int next_idx = -1;
	time_t best = 0;
	for (int i = 0; i < n_ev; i++) {
		if (ev_now[i]) continue;
		if (ev_start[i] <= now) continue;
		if (next_idx < 0 || ev_start[i] < best) {
			next_idx = i;
			best = ev_start[i];
		}
	}
	if (next_idx >= 0) {
		cJSON *ev = cJSON_GetArrayItem(ea, next_idx);
		cJSON_AddBoolToObject(ev, "next", true);
	}

	cJSON_AddItemToObject(out_root, "updated_at", cJSON_CreateString(upd));
	cJSON_AddItemToObject(out_root, "tasks", ta);
	cJSON_AddItemToObject(out_root, "events", ea);
	cJSON_AddNumberToObject(out_root, "open_tasks", open);
	cJSON_AddNumberToObject(out_root, "total_tasks", total);

	if (next_idx >= 0) {
		cJSON *ev = cJSON_GetArrayItem(ea, next_idx);
		cJSON_AddItemToObject(out_root, "next_event", cJSON_Duplicate(ev, true));
	}

	char *s = cJSON_PrintUnformatted(out_root);
	cJSON_Delete(out_root);
	if (tasks) cJSON_Delete(tasks);
	if (events) cJSON_Delete(events);
	{
		int cjm1 = 0, cjf1 = 0;
		lark_api_mem_stats(&cjm1, &cjf1);
		/* 「活跃」= 分配的次数 - 释放的次数 = 此刻还活着的 cJSON 节点数。
		 *
		 * 【必须用 cJSON_free 释放 print 出来的字符串】——
		 * cJSON_PrintUnformatted 用的是 hook 里的 allocator，
		 * 用裸 free() 释放的话，hook 的**释放计数就少记一次**，
		 * 于是「活跃」每轮虚涨 1，看起来像每轮漏一个节点，
		 * 实际内存是还给堆了的。**一个会漂移的诊断比没有诊断更糟** ——
		 * 上一次就是拿它当「没有泄漏」的证据，结果它一直在说谎。
		 *
		 * 「构建后」应该恒为 0（两棵 API 树和建出来的 state 树都释放了）。 */
		ESP_LOGI(TAG, "cJSON 活跃: 构建前=%d 构建后=%d",
		         cjm0 - cjf0, cjm1 - cjf1);
	}
	if (!s) return false;
	/* 静默截断 = 屏上永远显示上一帧的旧数据（JSON 砍断了 -> 解析失败）。
	 * 宁可明确失败，也不能悄悄给一份残缺的 JSON。 */
	size_t sl = strlen(s);
	if (sl >= cap) {
		ESP_LOGE(TAG, "state.json 放不下：需要 %d 字节，缓冲只有 %d —— "
		         "调大 STATE_BUF_SIZE", (int)sl, (int)cap);
		cJSON_free(s);
		return false;
	}
	memcpy(out, s, sl + 1);
	cJSON_free(s);
	return true;
}
