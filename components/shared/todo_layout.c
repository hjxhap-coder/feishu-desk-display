#include "todo_layout.h"
#include "todo_font.h"

#include <stdio.h>
#include <string.h>

/* ------------------------------------------------------------------ */
/* 版面常量                                                            */
/* ------------------------------------------------------------------ */
#define M          8                    /* 边距 */
#define LH         TODO_FONT_CELL_H     /* 行高 18 */
#define BASE       TODO_FONT_ASCENT     /* 顶边到基线 14 */

#define HEAD_TITLE_Y   4
#define HEAD_RULE_Y    32
#define BODY_Y         42
#define FOOT_RULE_Y    272
#define FOOT_TEXT_Y    278

/* 日程屏下半屏的时间轴 */
#define TL_TITLE_Y     204
#define TL_BAR_Y       228
#define TL_BAR_H       20
#define TL_TICK_Y      250

/* ------------------------------------------------------------------ */
/* 7 段数码管                                                          */
/* ------------------------------------------------------------------ */
#define SEG_A 0x01
#define SEG_B 0x02
#define SEG_C 0x04
#define SEG_D 0x08
#define SEG_E 0x10
#define SEG_F 0x20
#define SEG_G 0x40

static const char SEG_MAP[10] = {
	0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F,
};

static void draw_digit(TodoFb *fb, int x, int y, int w, int h, int t, char ch,
                       bool ink)
{
	if (ch < '0' || ch > '9') return;
	int mask = SEG_MAP[ch - '0'];

	/* 窄 1：只画一根竖线，放在数字框中间偏右，视觉间隙才均匀 */
	if (ch == '1') {
		fb_rect_fill(fb, x + w / 2, y, x + w / 2 + t - 1, y + h - 1, ink);
		return;
	}

	int mid = (h - t) / 2;
	int upper_bot = mid - t;
	int lower_top = mid + t;

	if (mask & SEG_A) fb_rect_fill(fb, x,            y,            x + w - 1, y + t - 1, ink);
	if (mask & SEG_F) fb_rect_fill(fb, x,            y,            x + t - 1, y + upper_bot, ink);
	if (mask & SEG_B) fb_rect_fill(fb, x + w - t,    y,            x + w - 1, y + upper_bot, ink);
	if (mask & SEG_G) fb_rect_fill(fb, x,            y + mid,      x + w - 1, y + mid + t - 1, ink);
	if (mask & SEG_E) fb_rect_fill(fb, x,            y + lower_top, x + t - 1, y + h - 1, ink);
	if (mask & SEG_C) fb_rect_fill(fb, x + w - t,    y + lower_top, x + w - 1, y + h - 1, ink);
	if (mask & SEG_D) fb_rect_fill(fb, x,            y + h - t,    x + w - 1, y + h - 1, ink);
}

static void draw_colon(TodoFb *fb, int x, int y, int h, bool ink)
{
	int t = 4;
	fb_rect_fill(fb, x, y + h / 4 - t,     x + t - 1, y + h / 4, ink);
	fb_rect_fill(fb, x, y + h * 3 / 4 - t, x + t - 1, y + h * 3 / 4, ink);
}

/* ------------------------------------------------------------------ */
/* 小零件                                                              */
/* ------------------------------------------------------------------ */
static void draw_segbar(TodoFb *fb, int x, int y, int w, int h, int done, int total)
{
	if (total <= 0) return;
	if (total > 24) total = 24;
	int gap = 3;
	int seg = (w - gap * (total - 1)) / total;
	if (seg < 2) { seg = 2; gap = 1; }
	for (int i = 0; i < total; i++) {
		int x0 = x + i * (seg + gap);
		int x1 = x0 + seg;
		if (x1 > x + w) x1 = x + w;
		if (i < done) fb_rect_fill(fb, x0, y, x1, y + h - 1, true);
		else          fb_rect_outline(fb, x0, y, x1, y + h - 1, true);
	}
}

static void draw_checkbox(TodoFb *fb, int x, int y, int size, bool checked)
{
	if (!checked) {
		fb_rect_outline(fb, x, y, x + size - 1, y + size - 1, true);
		return;
	}
	fb_rect_fill(fb, x, y, x + size - 1, y + size - 1, true);
	/* 白勾：先斜下，再斜上 */
	for (int i = 0; i < size / 3; i++) {
		fb_px(fb, x + size / 4 + i, y + size / 2 + i - 1, false);
		fb_px(fb, x + size / 4 + i, y + size / 2 + i,     false);
	}
	for (int i = 0; i < size * 2 / 5; i++) {
		int px_ = x + size / 4 + size / 3 + i;
		int py_ = y + size / 2 + size / 3 - i;
		fb_px(fb, px_, py_ - 1, false);
		fb_px(fb, px_, py_,     false);
	}
}

static void draw_circle(TodoFb *fb, int cx, int cy, int r, bool ink)
{
	for (int dy = -r; dy <= r; dy++) {
		for (int dx = -r; dx <= r; dx++) {
			if (dx * dx + dy * dy <= r * r) fb_px(fb, cx + dx, cy + dy, ink);
		}
	}
}

static void draw_ring(TodoFb *fb, int cx, int cy, int r, bool ink)
{
	for (int dy = -r; dy <= r; dy++) {
		for (int dx = -r; dx <= r; dx++) {
			int d = dx * dx + dy * dy;
			if (d <= r * r && d >= (r - 1) * (r - 1)) fb_px(fb, cx + dx, cy + dy, ink);
		}
	}
}

/* ------------------------------------------------------------------ */
/* 页头页脚                                                            */
/* ------------------------------------------------------------------ */
static void draw_header(TodoFb *fb, const char *title, const struct tm *now)
{
	char t[8];
	snprintf(t, sizeof(t), "%02d:%02d", now->tm_hour, now->tm_min);

	fb_text_bold(fb, M, HEAD_TITLE_Y, title, true);
	int tw = todo_font_text_width(t);
	fb_text(fb, TODO_FB_W - M - tw, HEAD_TITLE_Y + 2, t, true);

	fb_hline(fb, M, TODO_FB_W - M, HEAD_RULE_Y, true);
	fb_hline(fb, M, TODO_FB_W - M, HEAD_RULE_Y + 1, true);
}

/* "2026-10-22 11:38" -> "11:38 更新" */
static void format_updated(const TodoState *st, char *out, size_t cap)
{
	const char *u = st->updated_at;
	int ul = (int)strlen(u);
	if (ul >= 5)      snprintf(out, cap, "%.5s 更新", u + ul - 5);
	else if (ul > 0)  snprintf(out, cap, "%s 更新", u);
	else              snprintf(out, cap, "更新");
}

static void draw_footer(TodoFb *fb, const char *left, const char *right)
{
	fb_hline(fb, M, TODO_FB_W - M, FOOT_RULE_Y, true);
	if (left && *left) fb_text(fb, M, FOOT_TEXT_Y, left, true);
	if (right && *right) {
		int w = todo_font_text_width(right);
		fb_text(fb, TODO_FB_W - M - w, FOOT_TEXT_Y, right, true);
	}
}

/* ------------------------------------------------------------------ */
/* 待机屏                                                              */
/* ------------------------------------------------------------------ */
/* ------------------------------------------------------------------ */
/* 故障提示                                                            */
/* ------------------------------------------------------------------ */
/* 当前故障状态。渲染时读它 —— 故障必须**看得见**，
 * 不能只是「保持上一帧」（那会让屏上一直停在开机美术图，像一切正常）。 */
static TodoErr g_err = TODO_ERR_NONE;
void todo_layout_set_error(TodoErr e) { g_err = e; }


/* 故障文案。1-bit 点阵屏，16px 一种字号，措辞要短。 */
static const char *err_title(TodoErr e)
{
	switch (e) {
		case TODO_ERR_AUTH:  return "凭据已失效";
		case TODO_ERR_NET:   return "连不上服务器";
		case TODO_ERR_EMPTY: return "暂无数据";
		case TODO_ERR_NVS:   return "凭据写入失败";
		default:             return "";
	}
}

static const char *err_detail(TodoErr e)
{
	switch (e) {
		case TODO_ERR_AUTH:  return "请重新授权";
		case TODO_ERR_NET:   return "检查网络";
		case TODO_ERR_EMPTY: return "稍后再试";
		case TODO_ERR_NVS:   return "重启后会失效";
		default:             return "";
	}
}

/* 待办/日程屏：整屏故障。 */
void todo_layout_render_error(TodoFb *fb, const struct tm *now, TodoErr err)
{
	if (err == TODO_ERR_NONE) return;

	/* 页眉照常（标题 + 时间），让人知道设备是活的 */
	const char *title = (err == TODO_ERR_AUTH) ? "待办" : "日程";
	fb_text_bold(fb, M, 12, title, true);
	char hm[8];
	snprintf(hm, sizeof(hm), "%02d:%02d", now->tm_hour, now->tm_min);
	int hw = todo_font_text_width(hm);
	fb_text(fb, TODO_FB_W - M - hw, 12, hm, true);
	fb_hline(fb, M, TODO_FB_W - M, 40, true);

	/* 中间：一个反白块 + 标题 + 说明 */
	const int by = 110, bh = 76;
	fb_rect_fill(fb, M, by, TODO_FB_W - M - 1, by + bh - 1, true);

	const char *t = err_title(err);
	const char *d = err_detail(err);
	int tw = todo_font_text_width(t);
	fb_text(fb, (TODO_FB_W - tw) / 2, by + 18, t, false);
	int dw = todo_font_text_width(d);
	fb_text(fb, (TODO_FB_W - dw) / 2, by + 46, d, false);

	/* 底部：一条实心规则 + 提示「重新授权后自动恢复」 */
	fb_hline(fb, M, TODO_FB_W - M, FOOT_RULE_Y, true);
	if (err == TODO_ERR_AUTH) {
		const char *hint = "授权后自动恢复";
		int hw2 = todo_font_text_width(hint);
		fb_text(fb, (TODO_FB_W - hw2) / 2, FOOT_RULE_Y + 8, hint, true);
	}
}

/* 待机屏：在下半部分画一条反白故障条（时钟照常显示）。 */
static void draw_err_band(TodoFb *fb, TodoErr err)
{
	if (err == TODO_ERR_NONE) return;
	const int by = 214, bh = 42;
	fb_rect_fill(fb, M, by, TODO_FB_W - M - 1, by + bh - 1, true);
	const char *t = err_title(err);
	int tw = todo_font_text_width(t);
	fb_text(fb, (TODO_FB_W - tw) / 2, by + 14, t, false);
}

static void layout_standby(const TodoState *st, TodoFb *fb, const struct tm *now)
{
	char hm[8];
	snprintf(hm, sizeof(hm), "%02d:%02d", now->tm_hour, now->tm_min);

	const int DW = 26, DH = 44, DT = 4, GAP = 10, CW = 8;
	int total = 4 * DW + CW + 4 * GAP;
	int x = (TODO_FB_W - total) / 2;
	int y = 12;

	for (int i = 0; i < 5; i++) {
		if (hm[i] == ':') {
			draw_colon(fb, x, y, DH, true);
			x += CW + GAP;
		} else {
			draw_digit(fb, x, y, DW, DH, DT, hm[i], true);
			x += DW + GAP;
		}
	}

	/* 日期。注意：星期要用 %s，%c 只会吐多字节 UTF-8 的第一个字节。 */
	static const char *WEEK[7] = { "日", "一", "二", "三", "四", "五", "六" };
	char date[32];
	snprintf(date, sizeof(date), "%d月%d日 星期%s",
	         now->tm_mon + 1, now->tm_mday, WEEK[now->tm_wday % 7]);
	int dw = todo_font_text_width(date);
	fb_text(fb, (TODO_FB_W - dw) / 2, 70, date, true);

	fb_hline(fb, M, TODO_FB_W - M, 98, true);

	fb_text_bold(fb, M, 110, "下一个日程", true);
	if (st->next_idx >= 0 && st->next_idx < st->n_events) {
		const TodoEvent *e = &st->events[st->next_idx];
		char line[TODO_MAX_TEXT + 16];
		snprintf(line, sizeof(line), "%s  %s", e->start_hm, e->title);
		fb_text_clipped(fb, M, 132, line, TODO_FB_W - 2 * M, true);
	} else {
		fb_text(fb, M, 132, "今天没有日程", true);
	}

	int total_tasks = st->total_tasks ? st->total_tasks : st->n_tasks;
	int done = total_tasks - st->open_tasks;
	if (done < 0) done = 0;

	fb_text_bold(fb, M, 172, "待办", true);
	char cnt[24];
	snprintf(cnt, sizeof(cnt), "%d/%d", done, total_tasks);
	fb_text(fb, M + 42, 172, cnt, true);

	draw_segbar(fb, M, 196, TODO_FB_W - 2 * M, 8, done,
	            total_tasks > 24 ? 24 : total_tasks);

	/* 故障条（拉不到数据时）。时钟照常显示 —— 摆件的基本功能
	 * 不能因为断网就停，但故障必须让人看见。 */
	draw_err_band(fb, g_err);

	/* 底部反白带：按键说明，每段在格子里居中 */
	const int band_y = 264;
	fb_rect_fill(fb, 0, band_y, TODO_FB_W - 1, TODO_FB_H - 1, true);
	const char *hints[3] = { "待机", "待办", "日程" };
	int cw = TODO_FB_W / 3;
	for (int i = 0; i < 3; i++) {
		int tw = todo_font_text_width(hints[i]);
		int box = 10;
		int block = box + 5 + tw;
		int cx = cw * i + (cw - block) / 2;
		fb_rect_outline(fb, cx, band_y + 12, cx + box - 1, band_y + 21, false);
		fb_text(fb, cx + box + 5, band_y + 9, hints[i], false);
	}
}

/* ------------------------------------------------------------------ */
/* 待办屏                                                              */
/* ------------------------------------------------------------------ */
/* 待办正文区的上下边界和单条占高 —— 分页和渲染都要用同一套。 */
#define TODO_LIST_TOP   (BODY_Y + 22)
#define TODO_LIST_BOT   (FOOT_RULE_Y - 4)
#define TODO_LIST_H     (TODO_LIST_BOT - TODO_LIST_TOP)
#define TODO_CB         14      /* 复选框宽 */

/* 单条待办占多高（含行间距）。同时把折行结果写进 buf 供渲染复用。 */
static int task_block_h(const TodoTask *t, char *buf, size_t cap, int *nlines_out)
{
	int due_w = 0;
	if (t->due[0] && !t->done) due_w = todo_font_text_width(t->due) + 10;

	int tx = M + TODO_CB + 6;
	int avail = TODO_FB_W - M - tx - due_w;
	if (avail < 60) avail = 60;

	int nlines = todo_font_wrap(t->text, avail, buf, cap, 2);
	if (nlines <= 0) nlines = 1;
	if (nlines_out) *nlines_out = nlines;
	return nlines * LH + 4;
}

/* 未完成条目的下标（保持原顺序）。 */
static int todo_open_indices(const TodoState *st, int *idx)
{
	int n = 0;
	int lim = st->n_tasks;
	if (lim > TODO_MAX_TASKS) lim = TODO_MAX_TASKS;
	for (int i = 0; i < lim; i++) {
		if (!st->tasks[i].done) idx[n++] = i;
	}
	return n;
}

/* 已完成条目的下标（保持原顺序）。 */
static int todo_done_indices(const TodoState *st, int *idx)
{
	int n = 0;
	int lim = st->n_tasks;
	if (lim > TODO_MAX_TASKS) lim = TODO_MAX_TASKS;
	for (int i = 0; i < lim; i++) {
		if (st->tasks[i].done) idx[n++] = i;
	}
	return n;
}

/* 把待办切成若干页：starts[p] 是第 p 页第一条在**未完成列表**里的位置，
 * starts[pages] 是哨兵（= 未完成条目数）。返回页数。
 *
 * 切页依据是**实际占高**，所以一行标题的条目和两行标题的条目
 * 排出来的页数不一样 —— 页数随条目数量和内容长度自动变。 */
static int todo_pages(const TodoState *st, int *starts, int max_starts)
{
	int idx[TODO_MAX_TASKS];
	int n = todo_open_indices(st, idx);

	starts[0] = 0;
	if (n == 0) { starts[1] = 0; return 1; }

	static char tmp[1024];
	int pages = 1;
	int y = 0;
	int i = 0;
	while (i < n) {
		int h = task_block_h(&st->tasks[idx[i]], tmp, sizeof(tmp), NULL);
		/* 一条本身就比一页还高（理论上不会，最多 2 行）时也要放得下，
		 * 不能死循环。 */
		if (y > 0 && y + h > TODO_LIST_H) {
			if (pages >= max_starts) break;
			starts[pages++] = i;
			y = 0;
		}
		y += h;
		i++;
	}
	starts[pages] = n;
	return pages;
}

static void layout_todo(const TodoState *st, TodoFb *fb, const struct tm *now,
                        int page)
{
	draw_header(fb, "待办", now);

	int done = 0;
	for (int i = 0; i < st->n_tasks; i++) {
		if (st->tasks[i].done) done++;
	}
	int total = st->n_tasks;

	int bar_w = TODO_FB_W - 2 * M - 48;
	draw_segbar(fb, M, BODY_Y, bar_w, 8, done, total > 24 ? 24 : total);
	char cnt[24];
	snprintf(cnt, sizeof(cnt), "%d/%d", done, total);
	int cw = todo_font_text_width(cnt);
	fb_text_bold(fb, TODO_FB_W - M - cw, BODY_Y - 3, cnt, true);

	/* 分页：
	 *   未完成的按实际占高正常分页（每页几条由标题长度决定）
	 *   已完成的**只占一页** —— 它们是「当天结束」的记录，
	 *   不该把「还有什么要做」挤到后面好几页去。 */
	int starts[TODO_MAX_TASKS + 2];
	int open_pages = todo_pages(st, starts, TODO_MAX_TASKS + 1);

	int oidx[TODO_MAX_TASKS];
	int n_open = todo_open_indices(st, oidx);
	int didx[TODO_MAX_TASKS];
	int n_done = todo_done_indices(st, didx);

	int pages = open_pages + (n_done > 0 ? 1 : 0);
	if (pages < 1) pages = 1;
	if (page < 0) page = 0;
	if (page >= pages) page = pages - 1;

	int from, to;
	bool done_page = (page >= open_pages);
	if (!done_page) {
		from = starts[page];
		to   = starts[page + 1];
	} else {
		from = 0;
		to   = n_done;      /* 汇总页：能画几条画几条 */
	}

	int y = TODO_LIST_TOP;
	const int cb = TODO_CB;
	static char buf[1024];

	/* 汇总页顶部一条点线，把「当天做完的」和「还要做的」分开。
	 * 点线比页眉那条 2px 实线弱一级，表示同一版块里的另一段。 */
	if (done_page) {
		for (int x = M + 6; x < TODO_FB_W - M - 6; x += 6) {
			fb_px(fb, x,     y - 2, true);
			fb_px(fb, x + 1, y - 2, true);
			fb_px(fb, x + 2, y - 2, true);
		}
		y += 8;
	}

	int drawn = 0;
	for (int pos = from; pos < to; pos++) {
		const TodoTask *t = &st->tasks[done_page ? didx[pos] : oidx[pos]];

		int due_w = 0;
		if (t->due[0] && !t->done) due_w = todo_font_text_width(t->due) + 10;

		int tx = M + cb + 6;
		int avail = TODO_FB_W - M - tx - due_w;
		if (avail < 60) avail = 60;

		int nlines = todo_font_wrap(t->text, avail, buf, sizeof(buf), 2);
		if (nlines <= 0) nlines = 1;
		int block = nlines * LH;

		/* 这一页放不下了 */
		if (y + block > TODO_LIST_BOT) {
			if (done_page) {
				char more[32];
				snprintf(more, sizeof(more), "还有 %d 项已完成", n_done - drawn);
				fb_text(fb, M, y, more, true);
			}
			break;
		}

		draw_checkbox(fb, M, y + 2, cb, t->done);

		int text_x = tx;
		if (t->pri == TODO_PRI_HIGH && !t->done) {
			fb_rect_fill(fb, text_x, y + 6, text_x + 5, y + 11, true);
			text_x += 11;
		}

		/* 逐行画 */
		const char *p = buf;
		for (int li = 0; li < nlines; li++) {
			const char *nl = strchr(p, '\n');
			int len = nl ? (int)(nl - p) : (int)strlen(p);
			static char line[512];
			if (len > (int)sizeof(line) - 1) len = (int)sizeof(line) - 1;
			memcpy(line, p, (size_t)len);
			line[len] = '\0';
			int xe = fb_text(fb, text_x, y + li * LH, line, true);
			if (t->done && li == 0) {
				fb_hline(fb, text_x, xe - 1, y + BASE + 2, true);  /* 删除线 */
			}
			if (!nl) break;
			p = nl + 1;
		}

		if (due_w) {
			int dwid = todo_font_text_width(t->due);
			fb_text(fb, TODO_FB_W - M - dwid, y + 2, t->due, true);
		}
		y += block + 4;
		drawn++;
	}

	/* 一条待办都没有 */
	if (st->n_tasks == 0) {
		const char *msg = "没有待办";
		int w = todo_font_text_width(msg);
		fb_text(fb, (TODO_FB_W - w) / 2, TODO_LIST_TOP + 20, msg, true);
	}

	/* 页脚：只有一页的时候不显示页码，多余的墨点就是噪音。
	 * 多页时左边换成「第 x/y 页」。 */
	char left[48];   /* "第 12/34 页" 这种，%d 最长 11 字节 */
	if (pages > 1) {
		snprintf(left, sizeof(left), "第 %d/%d 页", page + 1, pages);
	} else {
		snprintf(left, sizeof(left), "待办清单");
	}
	char upd[32];
	format_updated(st, upd, sizeof(upd));
	draw_footer(fb, left, upd);
}

/* ------------------------------------------------------------------ */
/* 日程屏                                                              */
/* ------------------------------------------------------------------ */

/* 今日时间轴：把**有用的时段**铺成一条带子，事件按时间比例占位。
 *
 * 【为什么不是一整天】8:00——21:00 之外的时间，人基本不在状态：
 * 一整天 24 小时铺开，一半以上是深夜和清晨，有用的时段被压成一小条，
 * 事件块全挤在一起看不清。现在只画 08:00——21:00（13 小时）。 */
#define TL_START_MIN (8 * 60)          /* 08:00 */
#define TL_END_MIN   (21 * 60)         /* 21:00 */
#define TL_SPAN_MIN  (TL_END_MIN - TL_START_MIN)

/* 分钟 -> 时间轴上的 x 坐标（超出范围夹到边上） */
static int tl_min_to_x(int x0, int w, int min)
{
	if (min < TL_START_MIN) min = TL_START_MIN;
	if (min > TL_END_MIN)   min = TL_END_MIN;
	return x0 + ((min - TL_START_MIN) * w) / TL_SPAN_MIN;
}

static void draw_timeline(TodoFb *fb, const TodoState *st, const struct tm *now)
{
	const int x0 = M, x1 = TODO_FB_W - M;
	const int w = x1 - x0;

	fb_text_bold(fb, x0, TL_TITLE_Y, "今日时间轴", true);

	/* 带子 */
	fb_rect_outline(fb, x0, TL_BAR_Y, x1, TL_BAR_Y + TL_BAR_H - 1, true);

	for (int i = 0; i < st->n_events; i++) {
		const TodoEvent *e = &st->events[i];
		if (e->start_min < 0) continue;
		int s = e->start_min;
		int t = (e->end_min > e->start_min) ? e->end_min : e->start_min + 30;
		/* 完全落在时间轴范围外的（深夜/清晨）就不画了 ——
		 * 硬夹到边上会看起来像「那时候有事」，是假的。 */
		if (t <= TL_START_MIN || s >= TL_END_MIN) continue;
		int px0 = tl_min_to_x(x0, w, s);
		int px1 = tl_min_to_x(x0, w, t);
		if (px1 <= px0) px1 = px0 + 2;
		if (px1 > x1) px1 = x1;
		bool is_now = e->is_now;
		if (is_now) {
			fb_rect_fill(fb, px0, TL_BAR_Y + 1, px1 - 1, TL_BAR_Y + TL_BAR_H - 2, true);
		} else {
			/* 空心块 */
			fb_rect_fill(fb, px0, TL_BAR_Y + 4, px1 - 1, TL_BAR_Y + TL_BAR_H - 5, true);
		}
	}

	/* 当前时刻。**范围外就不画**（比如晚上 10 点，指针钉在右边上
	 * 会让人以为「就是现在」，是假的）。 */
	int nowmin = now->tm_hour * 60 + now->tm_min;
	if (nowmin >= TL_START_MIN && nowmin <= TL_END_MIN) {
		int nx = tl_min_to_x(x0, w, nowmin);
		fb_vline(fb, nx, TL_BAR_Y - 6, TL_BAR_Y + TL_BAR_H, true);
		fb_rect_fill(fb, nx - 3, TL_BAR_Y - 10, nx + 3, TL_BAR_Y - 7, true);
	}

	/* 刻度：08 / 12 / 16 / 21。位置按**实际时间**比例，
	 * 所以最后一个刻度和前面几个不是等距的（16->21 是 5 小时）。 */
	static const int TL_TICKS[] = { 8, 12, 16, 21 };
	for (size_t i = 0; i < sizeof(TL_TICKS) / sizeof(TL_TICKS[0]); i++) {
		int tx = tl_min_to_x(x0, w, TL_TICKS[i] * 60);
		if (tx > x1) tx = x1;
		fb_vline(fb, tx, TL_BAR_Y + TL_BAR_H, TL_BAR_Y + TL_BAR_H + 3, true);
		char lab[8];
		snprintf(lab, sizeof(lab), "%02d", TL_TICKS[i]);
		int lw = todo_font_text_width(lab);
		int lx = tx - lw / 2;
		if (lx < x0) lx = x0;
		if (lx + lw > x1) lx = x1 - lw;
		fb_text(fb, lx, TL_TICK_Y, lab, true);
	}
}

/* 日程正文区的上下边界 */
#define EV_LIST_TOP   BODY_Y
#define EV_LIST_BOT   (TL_TITLE_Y - 6)
#define EV_LIST_H     (EV_LIST_BOT - EV_LIST_TOP)

/* 单条日程占多高 */
static int event_block_h(const TodoEvent *e)
{
	int sub = 0;
	if (e->is_now || e->is_next) sub = 1;
	if (e->end_hm[0] || e->location[0]) sub = 1;
	return LH + (sub ? LH : 0) + 4;
}

static int event_pages(const TodoState *st, int *starts, int max_starts)
{
	int n = st->n_events;
	if (n > TODO_MAX_EVENTS) n = TODO_MAX_EVENTS;

	starts[0] = 0;
	if (n == 0) { starts[1] = 0; return 1; }

	int pages = 1;
	int y = 0;
	int i = 0;
	while (i < n) {
		int h = event_block_h(&st->events[i]);
		if (y > 0 && y + h > EV_LIST_H) {
			if (pages >= max_starts) break;
			starts[pages++] = i;
			y = 0;
		}
		y += h;
		i++;
	}
	starts[pages] = n;
	return pages;
}

static void layout_schedule(const TodoState *st, TodoFb *fb, const struct tm *now,
                            int page)
{
	draw_header(fb, "日程", now);

	if (st->n_events == 0) {
		const char *msg = "今天没有日程";
		int w = todo_font_text_width(msg);
		fb_text(fb, (TODO_FB_W - w) / 2, 110, msg, true);
		draw_timeline(fb, st, now);
		char upd[32];
		format_updated(st, upd, sizeof(upd));
		draw_footer(fb, "今日 0 项", upd);
		return;
	}

	const int AXIS_X = M + 42;
	const int TX = M + 54;

	int estarts[TODO_MAX_EVENTS + 2];
	int epages = event_pages(st, estarts, TODO_MAX_EVENTS + 1);
	if (page < 0) page = 0;
	if (page >= epages) page = epages - 1;
	int from = estarts[page];
	int to   = estarts[page + 1];

	int y = EV_LIST_TOP;
	for (int i = from; i < to; i++) {
		const TodoEvent *e = &st->events[i];

		char sub[TODO_MAX_TEXT];
		sub[0] = '\0';
		if (e->is_now)          strcat(sub, "进行中");
		else if (e->is_next)    strcat(sub, "下一个");
		if (e->end_hm[0]) {
			if (sub[0]) strcat(sub, " · ");
			strcat(sub, "至 ");
			strcat(sub, e->end_hm);
		}
		if (e->location[0]) {
			if (sub[0]) strcat(sub, " · ");
			strcat(sub, e->location);
		}

		int block = LH + (sub[0] ? LH : 0);

		bool is_now = e->is_now;
		int avail = TODO_FB_W - TX - M;

		if (is_now) {
			fb_rect_fill(fb, 0, y - 4, TODO_FB_W - 1, y + block - 2, true);
			fb_text_bold(fb, M, y, e->start_hm, false);
			fb_text_clipped_bold(fb, TX, y, e->title, avail, false);
			if (sub[0]) fb_text_clipped(fb, TX, y + LH, sub, avail, false);
			draw_circle(fb, AXIS_X, y + 8, 4, false);
		} else {
			fb_vline(fb, AXIS_X, y + 2, y + block - 4, true);
			if (e->is_next) draw_ring(fb, AXIS_X, y + 8, 5, true);
			else            draw_circle(fb, AXIS_X, y + 8, 3, true);

			if (e->is_next) fb_text_bold(fb, M, y, e->start_hm, true);
			else            fb_text(fb, M, y, e->start_hm, true);
			fb_text_clipped(fb, TX, y, e->title, avail, true);
			if (sub[0]) fb_text_clipped(fb, TX, y + LH, sub, avail, true);
		}
		y += block + 4;
	}

	draw_timeline(fb, st, now);

	char upd[32];
	format_updated(st, upd, sizeof(upd));
	char left[48];   /* "第 12/34 页" 这种，%d 最长 11 字节 */
	if (epages > 1) {
		snprintf(left, sizeof(left), "第 %d/%d 页", page + 1, epages);
	} else {
		snprintf(left, sizeof(left), "今日 %d 项", st->n_events);
	}
	draw_footer(fb, left, upd);
}

/* ------------------------------------------------------------------ */
int todo_layout_page_count(TodoMode mode, const TodoState *st)
{
	int starts[TODO_MAX_TASKS + 2];
	switch (mode) {
	case TODO_MODE_TODO: {
		/* 未完成的正常分页 + 已完成的固定一页 */
		int op = todo_pages(st, starts, TODO_MAX_TASKS + 1);
		int didx[TODO_MAX_TASKS];
		int nd = todo_done_indices(st, didx);
		return op + (nd > 0 ? 1 : 0);
	}
	case TODO_MODE_SCHEDULE: return event_pages(st, starts, TODO_MAX_EVENTS + 1);
	default:                  return 1;
	}
}

int todo_layout_screen_count(const TodoState *st)
{
	int t = todo_layout_page_count(TODO_MODE_TODO, st);
	int s = todo_layout_page_count(TODO_MODE_SCHEDULE, st);
	if (t < 1) t = 1;
	if (s < 1) s = 1;
	return 1 + t + s;          /* 待机占 1 页 */
}

void todo_layout_screen_unpack(int screen, const TodoState *st,
                                TodoMode *mode, int *page)
{
	int t = todo_layout_page_count(TODO_MODE_TODO, st);
	int s = todo_layout_page_count(TODO_MODE_SCHEDULE, st);
	if (t < 1) t = 1;
	if (s < 1) s = 1;

	if (screen <= 0) {                 /* 待机 */
		*mode = TODO_MODE_STANDBY;
		*page = 0;
	} else if (screen <= t) {         /* 待办的各页 */
		*mode = TODO_MODE_TODO;
		*page = screen - 1;
	} else {                          /* 日程的各页 */
		*mode = TODO_MODE_SCHEDULE;
		*page = screen - t - 1;
		if (*page >= s) *page = s - 1;
		if (*page < 0) *page = 0;
	}
}

void todo_layout_render(TodoMode mode, const TodoState *st, TodoFb *fb,
                        const struct tm *now, int page)
{
	fb_clear(fb, true);
	switch (mode) {
	case TODO_MODE_TODO:     layout_todo(st, fb, now, page); break;
	case TODO_MODE_SCHEDULE: layout_schedule(st, fb, now, page); break;
	case TODO_MODE_STANDBY:
	default:                  layout_standby(st, fb, now); break;
	}
}
