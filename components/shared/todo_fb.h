/*
 * todo_fb.h —— 1-bit 帧缓冲原语
 *
 * 帧格式：400x300，行优先，每行 50 字节，MSB 在前。
 *   1 = 白（纸面），0 = 黑（墨）。
 * 这跟板子上 RLCD_SetPixel(x, y, bit ? ColorWhite : ColorBlack) 完全对应。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TODO_FB_W 400
#define TODO_FB_H 300
#define TODO_FB_BYTES (TODO_FB_W * TODO_FB_H / 8)

typedef struct {
	uint8_t *buf;
	int      w;
	int      h;
} TodoFb;

/* ink = true 表示落墨（黑），false 表示留白（纸） */
void fb_clear(TodoFb *fb, bool white);
void fb_px(TodoFb *fb, int x, int y, bool ink);
void fb_hline(TodoFb *fb, int x0, int x1, int y, bool ink);
void fb_vline(TodoFb *fb, int x, int y0, int y1, bool ink);
void fb_rect_fill(TodoFb *fb, int x0, int y0, int x1, int y1, bool ink);
void fb_rect_outline(TodoFb *fb, int x0, int y0, int x1, int y1, bool ink);
/* 实心方点（待办优先级用） */
void fb_dot(TodoFb *fb, int x, int y, int size, bool ink);

/* 画一行文字。y 是文字**顶**边（不是基线）。返回结束处的 x。 */
int  fb_text(TodoFb *fb, int x, int y, const char *utf8, bool ink);
/* 伪粗体：每个墨点向右扩 1 像素。点阵字库只有一种字号，层级靠这个。 */
int  fb_text_bold(TodoFb *fb, int x, int y, const char *utf8, bool ink);
/* 画一行文字，y 是**基线**行。 */
int  fb_text_baseline(TodoFb *fb, int x, int baseline, const char *utf8, bool ink);
/* 画一行文字，自动截断到不超 max_px 宽 */
int  fb_text_clipped(TodoFb *fb, int x, int y, const char *utf8, int max_px, bool ink);
int  fb_text_clipped_bold(TodoFb *fb, int x, int y, const char *utf8, int max_px, bool ink);

#ifdef __cplusplus
}
#endif
