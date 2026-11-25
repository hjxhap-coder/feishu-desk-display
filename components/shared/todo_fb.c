#include "todo_fb.h"
#include "todo_font.h"

#include <string.h>

/* 位在帧里的位置：1 = 白，0 = 黑。ink=true 就是把位清 0。 */
static inline void put_bit(TodoFb *fb, int x, int y, bool ink)
{
	if (x < 0 || y < 0 || x >= fb->w || y >= fb->h) return;
	int rowbytes = fb->w >> 3;
	uint8_t *p = fb->buf + y * rowbytes + (x >> 3);
	uint8_t m = (uint8_t)(0x80 >> (x & 7));
	if (ink) *p &= (uint8_t)~m;     /* 落墨 -> 0 */
	else     *p |= m;               /* 留白 -> 1 */
}

void fb_clear(TodoFb *fb, bool white)
{
	memset(fb->buf, white ? 0xFF : 0x00, (size_t)fb->w * fb->h / 8);
}

void fb_px(TodoFb *fb, int x, int y, bool ink)
{
	put_bit(fb, x, y, ink);
}

void fb_hline(TodoFb *fb, int x0, int x1, int y, bool ink)
{
	if (x0 > x1) { int t = x0; x0 = x1; x1 = t; }
	for (int x = x0; x <= x1; x++) put_bit(fb, x, y, ink);
}

void fb_vline(TodoFb *fb, int x, int y0, int y1, bool ink)
{
	if (y0 > y1) { int t = y0; y0 = y1; y1 = t; }
	for (int y = y0; y <= y1; y++) put_bit(fb, x, y, ink);
}

void fb_rect_fill(TodoFb *fb, int x0, int y0, int x1, int y1, bool ink)
{
	if (x0 > x1) { int t = x0; x0 = x1; x1 = t; }
	if (y0 > y1) { int t = y0; y0 = y1; y1 = t; }
	for (int y = y0; y <= y1; y++) {
		for (int x = x0; x <= x1; x++) put_bit(fb, x, y, ink);
	}
}

void fb_rect_outline(TodoFb *fb, int x0, int y0, int x1, int y1, bool ink)
{
	fb_hline(fb, x0, x1, y0, ink);
	fb_hline(fb, x0, x1, y1, ink);
	fb_vline(fb, x0, y0, y1, ink);
	fb_vline(fb, x1, y0, y1, ink);
}

void fb_dot(TodoFb *fb, int x, int y, int size, bool ink)
{
	fb_rect_fill(fb, x, y, x + size - 1, y + size - 1, ink);
}

/* ------------------------------------------------------------------ */
static int draw_glyph(TodoFb *fb, int x, int y, uint32_t cp, bool ink, bool bold)
{
	uint8_t adv = 0;
	const uint8_t *g = todo_font_glyph(cp, &adv);
	if (g == NULL) return x + TODO_FONT_CELL_W;    /* 缺字：空一格 */

	int rowbytes = TODO_FONT_CELL_W / 8;
	for (int r = 0; r < TODO_FONT_CELL_H; r++) {
		const uint8_t *row = g + r * rowbytes;
		for (int c = 0; c < TODO_FONT_CELL_W; c++) {
			if ((row[c >> 3] >> (7 - (c & 7))) & 1) {
				put_bit(fb, x + c, y + r, ink);
				if (bold) put_bit(fb, x + c + 1, y + r, ink);
			}
		}
	}
	return x + adv;
}

int fb_text(TodoFb *fb, int x, int y, const char *utf8, bool ink)
{
	const char *p = utf8;
	while (*p) {
		uint32_t cp;
		int n = todo_utf8_next(p, &cp);
		if (n <= 0) break;
		x = draw_glyph(fb, x, y, cp, ink, false);
		p += n;
	}
	return x;
}

int fb_text_bold(TodoFb *fb, int x, int y, const char *utf8, bool ink)
{
	const char *p = utf8;
	while (*p) {
		uint32_t cp;
		int n = todo_utf8_next(p, &cp);
		if (n <= 0) break;
		x = draw_glyph(fb, x, y, cp, ink, true);
		p += n;
	}
	return x;
}

int fb_text_baseline(TodoFb *fb, int x, int baseline, const char *utf8, bool ink)
{
	return fb_text(fb, x, baseline - TODO_FONT_ASCENT, utf8, ink);
}

int fb_text_clipped(TodoFb *fb, int x, int y, const char *utf8, int max_px, bool ink)
{
	int used = todo_font_fit(utf8, max_px);
	char buf[256];
	if (used >= (int)sizeof(buf)) used = (int)sizeof(buf) - 1;
	memcpy(buf, utf8, (size_t)used);
	buf[used] = '\0';
	return fb_text(fb, x, y, buf, ink);
}

int fb_text_clipped_bold(TodoFb *fb, int x, int y, const char *utf8, int max_px, bool ink)
{
	int used = todo_font_fit(utf8, max_px);
	char buf[256];
	if (used >= (int)sizeof(buf)) used = (int)sizeof(buf) - 1;
	memcpy(buf, utf8, (size_t)used);
	buf[used] = '\0';
	return fb_text_bold(fb, x, y, buf, ink);
}
