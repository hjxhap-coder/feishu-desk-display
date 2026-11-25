#include "todo_state.h"
#include "cJSON/cJSON.h"

#include <stdio.h>
#include <string.h>

void todo_state_refresh_flags(TodoState *st, const struct tm *now)
{
	if (!st || !now) return;
	int nowmin = now->tm_hour * 60 + now->tm_min;
	int next_idx = -1;
	int best = 0;

	for (int i = 0; i < st->n_events; i++) {
		TodoEvent *e = &st->events[i];
		e->is_now = false;
		e->is_next = false;
		if (e->start_min < 0) continue;

		/* 零长度事件（end == start）只算开始后的那一分钟。
		 * 否则「11:30 的午餐」会在下午 6 点还挂着「进行中」。 */
		int end_min = e->end_min;
		if (end_min <= e->start_min) end_min = e->start_min + 1;

		if (nowmin >= e->start_min && nowmin < end_min) {
			e->is_now = true;
			continue;                 /* 进行中的不参与「下一个」评选 */
		}
		if (e->start_min > nowmin) {
			if (next_idx < 0 || e->start_min < best) {
				next_idx = i;
				best = e->start_min;
			}
		}
	}
	st->next_idx = next_idx;
	if (next_idx >= 0) st->events[next_idx].is_next = true;
}

void todo_state_empty(TodoState *st)
{
	memset(st, 0, sizeof(*st));
	st->next_idx = -1;
}

int todo_hm_to_minutes(const char *hm)
{
	if (hm == NULL) return -1;
	int h = 0, m = 0;
	if (sscanf(hm, "%d:%d", &h, &m) != 2) return -1;
	if (h < 0 || h > 23 || m < 0 || m > 59) return -1;
	return h * 60 + m;
}

static void copy_str(char *dst, size_t cap, const cJSON *item, const char *key)
{
	const cJSON *v = cJSON_GetObjectItemCaseSensitive(item, key);
	if (v == NULL || !cJSON_IsString(v) || v->valuestring == NULL) {
		dst[0] = '\0';
		return;
	}
	strncpy(dst, v->valuestring, cap - 1);
	dst[cap - 1] = '\0';
}

static int pri_from_str(const char *s)
{
	if (s == NULL) return TODO_PRI_NONE;
	if (strcmp(s, "high") == 0) return TODO_PRI_HIGH;
	if (strcmp(s, "low")  == 0) return TODO_PRI_LOW;
	return TODO_PRI_NONE;
}

static void parse_tasks(const cJSON *root, TodoState *st)
{
	const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "tasks");
	if (arr == NULL || !cJSON_IsArray(arr)) return;

	int n = cJSON_GetArraySize(arr);
	if (n > TODO_MAX_TASKS) n = TODO_MAX_TASKS;
	for (int i = 0; i < n; i++) {
		const cJSON *it = cJSON_GetArrayItem(arr, i);
		if (!cJSON_IsObject(it)) continue;
		TodoTask *t = &st->tasks[st->n_tasks];
		copy_str(t->text, sizeof(t->text), it, "text");
		copy_str(t->due,  sizeof(t->due),  it, "due");

		const cJSON *d = cJSON_GetObjectItemCaseSensitive(it, "done");
		t->done = (d != NULL && cJSON_IsBool(d)) ? cJSON_IsTrue(d) : false;

		char pri[16] = {0};
		copy_str(pri, sizeof(pri), it, "pri");
		t->pri = pri_from_str(pri);

		st->n_tasks++;
	}
}

static void parse_event_obj(const cJSON *it, TodoEvent *e)
{
	copy_str(e->title,     sizeof(e->title),     it, "title");
	copy_str(e->start_hm,  sizeof(e->start_hm),  it, "start_hm");
	copy_str(e->end_hm,    sizeof(e->end_hm),    it, "end_hm");
	copy_str(e->location,  sizeof(e->location),  it, "location");

	const cJSON *v = cJSON_GetObjectItemCaseSensitive(it, "now");
	e->is_now  = (v != NULL && cJSON_IsBool(v)) ? cJSON_IsTrue(v) : false;
	v = cJSON_GetObjectItemCaseSensitive(it, "next");
	e->is_next = (v != NULL && cJSON_IsBool(v)) ? cJSON_IsTrue(v) : false;

	e->start_min = todo_hm_to_minutes(e->start_hm);
	e->end_min   = todo_hm_to_minutes(e->end_hm);
}

/* 日程的排序键。没有合法开始时间的排最后。 */
static int event_sort_key(const TodoEvent *e)
{
	return (e->start_min >= 0) ? e->start_min : 24 * 60;
}

/*
 * 日程按开始时间排序（稳定插入排序）。
 *
 * 为什么必须排：飞书 instance_view 返回的顺序**不是时间顺序** ——
 * 实测同一份数据里 "11:30 午餐" 排在 "10:00 团队周会" 前面。
 * 照 API 顺序画，屏上日程就是乱的，而且**不报错**。
 *
 * n <= 16，插入排序足够了。
 */
static void sort_events_by_start(TodoState *st)
{
	for (int i = 1; i < st->n_events; i++) {
		TodoEvent tmp = st->events[i];
		int key = event_sort_key(&tmp);
		int j = i - 1;
		while (j >= 0 && event_sort_key(&st->events[j]) > key) {
			st->events[j + 1] = st->events[j];
			j--;
		}
		st->events[j + 1] = tmp;
	}
}

static void parse_events(const cJSON *root, TodoState *st)
{
	const cJSON *arr = cJSON_GetObjectItemCaseSensitive(root, "events");
	if (arr == NULL || !cJSON_IsArray(arr)) return;

	int n = cJSON_GetArraySize(arr);
	if (n > TODO_MAX_EVENTS) n = TODO_MAX_EVENTS;
	for (int i = 0; i < n; i++) {
		const cJSON *it = cJSON_GetArrayItem(arr, i);
		if (!cJSON_IsObject(it)) continue;
		parse_event_obj(it, &st->events[st->n_events]);
		st->n_events++;
	}

	/* 先排序，再定位「下一个」—— 排序会搬动下标，
	 * 排序前记下的 next_idx 立刻就失效了。 */
	sort_events_by_start(st);
	for (int i = 0; i < st->n_events; i++) {
		if (st->events[i].is_next) {
			st->next_idx = i;
			break;
		}
	}
}

bool todo_state_parse_json(const char *json, TodoState *out)
{
	if (json == NULL || out == NULL) return false;
	todo_state_empty(out);

	cJSON *root = cJSON_Parse(json);
	if (root == NULL) return false;
	if (!cJSON_IsObject(root)) { cJSON_Delete(root); return false; }

	copy_str(out->updated_at, sizeof(out->updated_at), root, "updated_at");

	parse_tasks(root, out);
	parse_events(root, out);

	const cJSON *v = cJSON_GetObjectItemCaseSensitive(root, "open_tasks");
	out->open_tasks  = (v != NULL && cJSON_IsNumber(v)) ? v->valueint : 0;
	v = cJSON_GetObjectItemCaseSensitive(root, "total_tasks");
	out->total_tasks = (v != NULL && cJSON_IsNumber(v)) ? v->valueint : out->n_tasks;

	/* next_event 优先（它比 events[].next 更权威） */
	const cJSON *ne = cJSON_GetObjectItemCaseSensitive(root, "next_event");
	if (ne != NULL && cJSON_IsObject(ne)) {
		const cJSON *nt = cJSON_GetObjectItemCaseSensitive(ne, "title");
		const char *t = (nt != NULL && cJSON_IsString(nt)) ? nt->valuestring : NULL;
		if (t != NULL) {
			for (int i = 0; i < out->n_events; i++) {
				if (strcmp(t, out->events[i].title) == 0) {
					out->next_idx = i;
					out->events[i].is_next = true;
					break;
				}
			}
		}
		if (out->next_idx < 0) {
			/* events 里没有匹配的，就单独存一份 */
			if (out->n_events < TODO_MAX_EVENTS) {
				parse_event_obj(ne, &out->events[out->n_events]);
				out->events[out->n_events].is_next = true;
				out->next_idx = out->n_events;
				out->n_events++;
			}
		}
	}

	cJSON_Delete(root);
	return true;
}
