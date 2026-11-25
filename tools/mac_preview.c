/*
 * mac_preview.c —— 在 Mac 上跑**同一份**排版代码，输出 15000 字节的帧。
 *
 * 这个程序不是"模仿"板子，它用的就是板子上那几份 .c 文件。
 * 所以预览和真机在像素层面是同一套逻辑，不会长歪。
 *
 *   mac_preview <state.json> <mode> <font.bin> <out.bin> [page] [now_epoch]
 *
 * mode 可以是 standby / todo / schedule，也可以是 screen（此时 [page]
 * 是整条页面序列里的第几格）。[now_epoch] 用来指定渲染时刻，
 * 和板子比对时传板子那一帧的时间戳。
 */
#define _POSIX_C_SOURCE 200809L

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "todo_font.h"
#include "todo_fb.h"
#include "todo_state.h"
#include "todo_layout.h"

static unsigned char *slurp(const char *path, long *len)
{
	FILE *f = fopen(path, "rb");
	if (!f) { fprintf(stderr, "打不开 %s\n", path); return NULL; }
	fseek(f, 0, SEEK_END);
	long n = ftell(f);
	fseek(f, 0, SEEK_SET);
	unsigned char *buf = (unsigned char *)malloc((size_t)n + 1);
	if (!buf) { fclose(f); return NULL; }
	if (fread(buf, 1, (size_t)n, f) != (size_t)n) {
		fprintf(stderr, "读不全 %s\n", path);
		free(buf); fclose(f); return NULL;
	}
	fclose(f);
	*len = n;
	return buf;
}

int main(int argc, char **argv)
{
	if (argc < 5) {
		fprintf(stderr, "用法: mac_preview <state.json> <mode> <font.bin> <out.bin> [page]\n");
		return 1;
	}
	int page = 0;

	long flen = 0;
	unsigned char *font = slurp(argv[3], &flen);
	if (font == NULL) return 1;
	if (!todo_font_load_mem(font, (uint32_t)flen)) {
		fprintf(stderr, "字库解析失败: %s\n", argv[3]);
		return 1;
	}

	long slen = 0;
	unsigned char *json = slurp(argv[1], &slen);
	if (json == NULL) return 1;
	json[slen] = '\0';

	TodoState st;
	if (!todo_state_parse_json((const char *)json, &st)) {
		fprintf(stderr, "state.json 解析失败\n");
		return 1;
	}

	TodoMode mode = TODO_MODE_STANDBY;
	if (strcmp(argv[2], "todo") == 0)         mode = TODO_MODE_TODO;
	else if (strcmp(argv[2], "schedule") == 0) mode = TODO_MODE_SCHEDULE;
	else {
		/* "screen" 模式：按整条页面序列（第几格）来渲染，
		 * 和板子上单键前进的序列完全一致。 */
		int screen = (argc >= 6) ? atoi(argv[5]) : 0;
		todo_layout_screen_unpack(screen, &st, &mode, &page);
	}

	static uint8_t frame[TODO_FB_BYTES];
	TodoFb fb = { frame, TODO_FB_W, TODO_FB_H };

	/* 渲染时刻。可以指定 —— 用来和板子**逐像素**比对时，
	 * 必须用板子那一帧的同一个时刻（待机屏的时钟、日程页的
	 * 「现在」指针都跟时刻有关）。 */
	time_t t = time(NULL);
	if (argc >= 7 && atoi(argv[6]) > 0) {
		t = (time_t)atoi(argv[6]);
	}
	struct tm now;
	localtime_r(&t, &now);
	todo_state_refresh_flags(&st, &now);   /* 和板子一样：按当前时刻重算进行中/下一个 */

	if (strcmp(argv[2], "todo") == 0 || strcmp(argv[2], "schedule") == 0) {
		page = (argc >= 6) ? atoi(argv[5]) : 0;
	}
	todo_layout_render(mode, &st, &fb, &now, page);

	FILE *o = fopen(argv[4], "wb");
	if (!o) { fprintf(stderr, "写不了 %s\n", argv[4]); return 1; }
	fwrite(frame, 1, sizeof(frame), o);
	fclose(o);

	fprintf(stderr, "mode=%s 第%d/%d页  总序列%d格  %d 字节  任务 %d  日程 %d  下一项 %d\n",
	        argv[2], page + 1, todo_layout_page_count(mode, &st),
	        todo_layout_screen_count(&st),
	        (int)sizeof(frame), st.n_tasks, st.n_events, st.next_idx);
	return 0;
}
