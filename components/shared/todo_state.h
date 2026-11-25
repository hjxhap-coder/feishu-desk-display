/*
 * todo_state.h —— 归一化的摆件状态
 *
 * 关键：Mac 端（lark_pull.py 产的 state.json）和板子端（飞书 API 拉的原始响应）
 * 都先转成**同一种形状**，然后喂给同一套排版代码。
 * 两边不会因为数据结构不一样而长歪。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>
#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TODO_MAX_TASKS   32
#define TODO_MAX_EVENTS  16
#define TODO_MAX_TEXT    128
#define TODO_MAX_SHORT   24

/* 优先级 */
#define TODO_PRI_NONE  0
#define TODO_PRI_LOW   1
#define TODO_PRI_HIGH  2

typedef struct {
	char text[TODO_MAX_TEXT];
	bool done;
	int  pri;                  /* TODO_PRI_* */
	char due[TODO_MAX_SHORT];
} TodoTask;

typedef struct {
	char title[TODO_MAX_TEXT];
	char start_hm[8];          /* "14:00" */
	char end_hm[8];            /* "14:30" */
	char location[TODO_MAX_SHORT];
	bool is_now;               /* 正在进行 */
	bool is_next;              /* 下一个 */
	int  start_min;            /* 从 0 点起的分钟数，-1 = 未知 */
	int  end_min;
} TodoEvent;

typedef struct {
	char       updated_at[24];
	TodoTask   tasks[TODO_MAX_TASKS];
	int        n_tasks;
	TodoEvent  events[TODO_MAX_EVENTS];
	int        n_events;
	int        next_idx;       /* events 里的下一个，-1 = 没有 */
	int        open_tasks;
	int        total_tasks;
} TodoState;

/* 解析 state.json 那种形状的 JSON。失败返回 false。 */
bool todo_state_parse_json(const char *json, TodoState *out);

/* "14:30" -> 870；解析不了返回 -1 */
int  todo_hm_to_minutes(const char *hm);

/* 按**当前时刻**重算每个日程的「进行中 / 下一个」标记。
 *
 * 为什么要在渲染时算，而不是在拉数据时算：
 *   数据是 60 秒拉一次的。如果标记在拉取那一刻定死，那么
 *   ① 拉取失败保留旧数据时，屏上会一直挂着几小时前的「进行中」黑块
 *      —— 看起来像时间轴卡死了；
 *   ② 零长度事件（end == start，飞书里「午餐」就是这样）如果按
 *      「end <= start 就算进行中」判，会变成**从开始时间起永远进行中**。
 *
 * 零长度事件只算它开始后的那一分钟。
 * 必须在 todo_state_parse_json() 之后、todo_layout_render() 之前调用。 */
void todo_state_refresh_flags(TodoState *st, const struct tm *now);

/* 清空成空状态 */
void todo_state_empty(TodoState *st);

#ifdef __cplusplus
}
#endif
