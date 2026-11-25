/*
 * lark_api.h —— 板子端飞书客户端
 *
 * 板子自己拉飞书数据，不依赖 Mac。凭据（app_id / app_secret / refresh_token）
 * 存在 NVS 里，access_token 过期了自己用 refresh_token 换。
 *
 * 数据契约：产出的 JSON 和 Mac 端 state.json **完全同形状**，
 * 然后交给同一个 todo_state_parse_json() 解析 —— 两边结构不会分叉。
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif

/* 飞书接口主机。默认是公网。
 * 也可以写进 NVS 覆盖（联调时指向 Mac 上的假飞书）：
 *   nvs key "host" = "<你的 Mac IP>"
 *   nvs key "port" = "8099"
 *   nvs key "tls"  = "0"          （0 = 明文 http，只在联调时用）
 * 这样联调和切正式都不用重新编译。 */
#ifndef LARK_HOST
#define LARK_HOST "open.feishu.cn"
#endif
#ifndef LARK_PORT
#define LARK_PORT 443
#endif
#ifndef LARK_USE_TLS
#define LARK_USE_TLS 1
#endif

typedef struct {
	char     app_id[80];
	char     app_secret[100];
	/* 飞书的 token 是 4700~4900 字符的 JWT，字段必须按实际长度给，
	 * 否则会被截断成畸形 JSON（表现为飞书报 "missing grant_type"）。 */
	/* 飞书的 user token 长度跟着 scope 走：scope 少时 ~4900 字符，
	 * scope 全开时 ~6900 字符（实测 6899）。容量必须留够，
	 * 不够的话 strlcpy 会截断成畸形 token，飞书报 invalid_grant。 */
	char     refresh_token[7100];
	char     access_token[7100];
	time_t   token_expire;      /* unix 时间；0 = 没有 */
	char     host[64];
	uint16_t port;
	bool     use_tls;
} LarkAuth;

/* 从 NVS 读凭据。没读到也返回 true（可能走编译期默认值）。 */
bool lark_auth_load(LarkAuth *a);
bool lark_auth_save(const LarkAuth *a);

/* 直接塞一个 refresh_token 进去（串口用）。
 * 飞书的 refresh_token 一次性，响应丢了就废了 —— 有了这个，
 * 重新授权后不用拆板子烧 NVS，敲一行命令就行。
 * 写完会回读校验。 */
bool lark_auth_set_refresh_token(const char *rt);

/* 拿到可用的 access_token（必要时用 refresh_token 换新的） */
bool lark_auth_token(LarkAuth *a);

/* 拉全部数据，产出 state.json 形状的 JSON。失败返回 false。 */
bool lark_build_state(char *out, size_t cap);

/* 要求下一轮强制走一遍 token 刷新（忽略存下来的 access_token）。
 * 一次性 RT 用掉就没了，所以这个**只能手动触发**，不能每轮都做。
 * 串口命令 "lark refresh" 调它。 */
void lark_request_refresh(void);

/* 上一次刷新是不是「refresh_token 已被作废」。
 * 这种失败重试多少次都没用（飞书的 RT 只能用一次），
 * 必须退避到分钟级，不能每 60 秒打一次飞书。 */
bool lark_auth_dead(void);

/* 凭据是不是**没写进 NVS**（轮换时写盘失败）。
 * 内存里那份还能用到过期，但重启就废了 —— 必须上屏提醒。 */
bool lark_auth_broken(void);

/* 内存泄漏定位：装 cJSON 分配计数 hook（要在任何 cJSON 分配前调用） */
void lark_api_install_mem_hooks(void);
void lark_api_mem_stats(int *m, int *f);

#ifdef __cplusplus
}
#endif
