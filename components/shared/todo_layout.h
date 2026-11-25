/*
 * todo_layout.h —— 三屏排版
 *
 * 同一份代码在 ESP32 和 Mac 上跑，所以 Mac 上的预览 = 板子上的真机。
 *
 * 设计约束（1-bit 点阵屏）：
 *   - 只有 16px 一种字号，层级不能靠字号，只能靠
 *     「数码管时钟 / 伪粗体 / 反白块 / 粗细分隔线 / 留白」
 *   - 1 = 白（纸面），0 = 黑（墨）
 */
#pragma once

#include "todo_fb.h"
#include "todo_state.h"

#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
	TODO_MODE_STANDBY = 0,
	TODO_MODE_TODO    = 1,
	TODO_MODE_SCHEDULE = 2,
} TodoMode;

/* 这一屏分几页。待办/日程放不下时自动分页，
 * 页数随条目数量和实际占高变化。 */
int  todo_layout_page_count(TodoMode mode, const TodoState *st);

/* ---- 屏幕页面序列 ----
 * 板子只有一个键，所以不做长短按区分，一次按键前进一步：
 *   待机 -> 待办第1页 -> 待办第2页 -> ... -> 日程第1页 -> ... -> 待机
 *
 * 这套序列放在 shared 层，Mac 预览和板子用同一份，不会分叉。 */
int  todo_layout_screen_count(const TodoState *st);
void todo_layout_screen_unpack(int screen, const TodoState *st,
                                TodoMode *mode, int *page);

/* page 是 0 起的页码；超出范围会被 clamp 到最后一页。 */
void todo_layout_render(TodoMode mode, const TodoState *st, TodoFb *fb,
                        const struct tm *now, int page);

/* ---- 故障提示 ----
 * 为什么需要：板子拉不到数据时如果只是「保持上一帧」，
 * 屏上会一直停在开机美术图（吹笛子少年）——
 * 看上去一切正常，实际上早就死了，没人知道要去重新授权。
 * 故障必须**看得见**。 */
typedef enum {
	TODO_ERR_NONE = 0,
	TODO_ERR_AUTH,    /* refresh_token 被作废，需要重新 device flow 授权 */
	TODO_ERR_NET,     /* 连不上服务器 / 拉取失败 */
	TODO_ERR_EMPTY,   /* 连上了但没有数据 */
	TODO_ERR_NVS,     /* 凭据**写盘失败** —— 内存里还能用，重启后就废了 */
} TodoErr;

/* 待办/日程屏：整屏显示故障。
 * standby 屏：时钟照常显示（摆件的基本功能不能因为断网就停），
 * 在下半部分画一条反白故障条。 */
void todo_layout_render_error(TodoFb *fb, const struct tm *now, TodoErr err);

/* 设置当前故障状态（渲染时读它）。 */
void todo_layout_set_error(TodoErr e);

#ifdef __cplusplus
}
#endif
