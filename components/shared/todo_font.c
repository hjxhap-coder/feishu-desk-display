#include "todo_font.h"

#include <string.h>

static TodoFont s_font;

/* ------------------------------------------------------------------ */
/* 初始化                                                              */
/* ------------------------------------------------------------------ */
static bool parse_header(TodoFont *f, const uint8_t *data, uint32_t len)
{
	if (len < 24) return false;
	if (memcmp(data, "TOF1", 4) != 0) return false;

	f->bin      = data;
	f->bin_len  = len;
	f->cell_h   = data[5];
	f->cell_w   = data[6];
	f->ascent   = data[7];
	f->n_glyph  = (uint16_t)(data[8] | (data[9] << 8));
	f->index_off = (uint32_t)data[12] | ((uint32_t)data[13] << 8) |
	               ((uint32_t)data[14] << 16) | ((uint32_t)data[15] << 24);
	f->index_len = (uint32_t)data[16] | ((uint32_t)data[17] << 8) |
	               ((uint32_t)data[18] << 16) | ((uint32_t)data[19] << 24);
	f->data_off  = (uint32_t)data[20] | ((uint32_t)data[21] << 8) |
	               ((uint32_t)data[22] << 16) | ((uint32_t)data[23] << 24);

	if (f->cell_w != TODO_FONT_CELL_W || f->cell_h != TODO_FONT_CELL_H) {
		return false;
	}
	if (f->index_off + f->index_len > len) return false;
	if (f->data_off > len) return false;
	return true;
}

bool todo_font_init(void)
{
	return parse_header(&s_font, TODO_FONT_BIN, TODO_FONT_BIN_LEN);
}

bool todo_font_load_mem(const uint8_t *data, uint32_t len)
{
	return parse_header(&s_font, data, len);
}

/* ------------------------------------------------------------------ */
/* 零宽字符与替代字形                                                  */
/* ------------------------------------------------------------------ */

/*
 * 飞书里的正文是「人类写的」，里面一定有 emoji。16px 1-bit 点阵屏
 * 没有彩色 emoji，也没有 U+1F300 以后的字形 —— 字库里根本没有。
 *
 * 两个问题，都不报错，只是屏上多一段莫名其妙的空隙：
 *
 * 1) **变体选择符** U+FE0F (VS16) 等本来就是零宽字符，
 *    但查不到字形时代码按 16px 步进画一个空格子。
 *    "➡️" = U+27A1 U+FE0F -> 屏上凭空多出 32px 空白。
 *
 * 2) **emoji 本体**（U+27A1 等）字库里没有。
 *
 * 处理办法（全在字体层，所以 Mac 预览和板子行为一致）：
 *   - 零宽类字符 -> 返回一张全空的 16x18 位图，advance = 0
 *   - 常见 emoji -> 替换成字库里真有的几何/箭头字形
 *   - 其余没有的 -> 保持原样（16px 空白，标准「豆腐块」）
 */

/* 全空位图：零宽字符用它，advance=0，画出来什么都没有。 */
static const uint8_t s_blank_cell[TODO_FONT_CELL_W / 8 * TODO_FONT_CELL_H] = { 0 };

/* 这些字符**本来就不占宽度**。 */
static bool cp_is_zero_width(uint32_t cp)
{
	if (cp >= 0xFE00 && cp <= 0xFE0F) return true;    /* 变体选择符 VS1..VS16 */
	if (cp >= 0xE0100 && cp <= 0xE01EF) return true;  /* 变体选择符补充 */
	if (cp == 0x200B) return true;                    /* 零宽空格 */
	if (cp == 0x200C) return true;                    /* ZWNJ */
	if (cp == 0x200D) return true;                    /* ZWJ */
	if (cp == 0x2060) return true;                    /* word joiner */
	if (cp == 0x00AD) return true;                    /* soft hyphen */
	if (cp == 0xFEFF) return true;                    /* BOM */
	return false;
}

/*
 * emoji -> 字库里真有的字形。
 * 目标字形全部在 font16.bin 里核对过存在（别写没验证过的码位）。
 *   箭头  ← ↑ → ↓        (U+2190..2193)
 *   几何  ■ □ ▢ ▣ ▤ ▲ △ ▼ ▽ ◆ ◇ ○ ◎ ●
 *   其它  × (U+00D7)  · (U+00B7)  ※  ℃ …
 */
static uint32_t cp_substitute(uint32_t cp)
{
	switch (cp) {
	/* 箭头 —— 最常见，"白膜➡️视频" 这种 */
	case 0x27A1: case 0x2794: case 0x27A4: case 0x27F6:
	case 0x2797: case 0x21D2: case 0x27A6:
		return 0x2192;                    /* -> */
	case 0x27F5: case 0x21D0: case 0x2B05:
		return 0x2190;                    /* <- */
	case 0x2B06: case 0x21D1:
		return 0x2191;                    /* ↑ */
	case 0x2B07: case 0x21D3:
		return 0x2193;                    /* ↓ */
	case 0x1F449: case 0x1F448:          /* 指手指 -> 箭头 */
		return 0x2192;

	/* 星 / 圆 */
	case 0x2B50: case 0x2605: case 0x23F8: case 0x1F535:
	case 0x1F534: case 0x1F7E0:
		return 0x25CF;                    /* ● */
	case 0x2606: case 0x1F536: case 0x1F7E1: case 0x1F7E2:
		return 0x25CB;                    /* ○ */

	/* 对勾 / 叉 —— 字库里没有 √ 和 ✓，用实心菱形和 × */
	case 0x2713: case 0x2714: case 0x2705: case 0x1F5F8:
		return 0x25C6;                    /* ◆ */
	case 0x2717: case 0x2718: case 0x274C: case 0x2B55:
		return 0x00D7;                    /* × */

	/* 警告 —— 三角 */
	case 0x26A0: case 0x203C: case 0x2757: case 0x1F6A8:
		return 0x25B2;                    /* ▲ */

	/* 方块 */
	case 0x2B1B: case 0x25AA: case 0x25AB:
		return 0x25A0;                    /* ■ */
	case 0x2B1C:
		return 0x25A1;                    /* □ */

	/* 加减 */
	case 0x2795: case 0x2B95:
		return 0x002B;                    /* + */
	case 0x2796: case 0x2B96:
		return 0x002D;                    /* - */

	/* 项目符号 */
	case 0x2022:                          /* • */
		return 0x00B7;                    /* · */

	/* 心 —— 常用的聊天符号 */
	case 0x2764: case 0x1F495: case 0x1F496:
		return 0x25CF;                    /* ● */
	case 0x1F525:
		return 0x25B2;                    /* ▲ */

	/* 图钉 / 备忘录 / 文件夹 —— 没有近似的符号，用实心菱形占位 */
	case 0x1F4CC: case 0x1F4DD: case 0x1F4C5: case 0x1F4CE:
		return 0x25C6;                    /* ◆ */
	}
	return cp;   /* 没有替换，返回原码位 */
}

/* ------------------------------------------------------------------ */
/* 查表                                                                */
/* ------------------------------------------------------------------ */
const uint8_t *todo_font_glyph(uint32_t cp, uint8_t *advance)
{
	if (s_font.bin == NULL) return NULL;

	/* 零宽字符：返回空位图 + advance 0。
	 * 注意必须返回**非 NULL** —— 绘制层看到 NULL 会空掉一整个 16px 格。 */
	if (cp_is_zero_width(cp)) {
		if (advance) *advance = 0;
		return s_blank_cell;
	}

	/* emoji 替代：换成字库里真有的字形。
	 * 宽度计算和绘制都走这一个函数，所以两者永远一致。 */
	cp = cp_substitute(cp);

	/* 索引按 codepoint 升序，二分 */
	uint32_t lo = 0, hi = s_font.n_glyph;
	while (lo < hi) {
		uint32_t mid = lo + (hi - lo) / 2;
		const uint8_t *e = s_font.bin + s_font.index_off + mid * 12;
		uint32_t k = (uint32_t)e[0] | ((uint32_t)e[1] << 8) |
		             ((uint32_t)e[2] << 16) | ((uint32_t)e[3] << 24);
		if (k == cp) {
			uint32_t off = (uint32_t)e[4] | ((uint32_t)e[5] << 8) |
			               ((uint32_t)e[6] << 16) | ((uint32_t)e[7] << 24);
			if (advance) *advance = e[8];
			return s_font.bin + off;
		}
		if (k < cp) lo = mid + 1;
		else          hi = mid;
	}
	if (advance) *advance = s_font.cell_w;
	return NULL;
}

/* ------------------------------------------------------------------ */
/* UTF-8                                                               */
/* ------------------------------------------------------------------ */
int todo_utf8_next(const char *s, uint32_t *cp)
{
	const unsigned char *p = (const unsigned char *)s;
	if (p[0] == 0) { if (cp) *cp = 0; return 0; }

	if (p[0] < 0x80) {
		if (cp) *cp = p[0];
		return 1;
	}
	if ((p[0] & 0xE0) == 0xC0 && (p[1] & 0xC0) == 0x80) {
		if (cp) *cp = ((uint32_t)(p[0] & 0x1F) << 6) | (p[1] & 0x3F);
		return 2;
	}
	if ((p[0] & 0xF0) == 0xE0 && (p[1] & 0xC0) == 0x80 && (p[2] & 0xC0) == 0x80) {
		if (cp) *cp = ((uint32_t)(p[0] & 0x0F) << 12) |
		              ((uint32_t)(p[1] & 0x3F) << 6) | (p[2] & 0x3F);
		return 3;
	}
	if ((p[0] & 0xF8) == 0xF0 && (p[1] & 0xC0) == 0x80 &&
	    (p[2] & 0xC0) == 0x80 && (p[3] & 0xC0) == 0x80) {
		if (cp) *cp = ((uint32_t)(p[0] & 0x07) << 18) |
		              ((uint32_t)(p[1] & 0x3F) << 12) |
		              ((uint32_t)(p[2] & 0x3F) << 6) | (p[3] & 0x3F);
		return 4;
	}
	/* 非法字节，按单字节跳过 */
	if (cp) *cp = 0xFFFD;
	return 1;
}

int todo_font_text_width(const char *utf8)
{
	int w = 0;
	while (*utf8) {
		uint32_t cp;
		int n = todo_utf8_next(utf8, &cp);
		if (n <= 0) break;
		uint8_t adv;
		const uint8_t *g = todo_font_glyph(cp, &adv);
		w += g ? adv : TODO_FONT_CELL_W;
		utf8 += n;
	}
	return w;
}

int todo_font_fit(const char *utf8, int max_px)
{
	int w = 0;
	const char *p = utf8;
	while (*p) {
		uint32_t cp;
		int n = todo_utf8_next(p, &cp);
		if (n <= 0) break;
		uint8_t adv;
		const uint8_t *g = todo_font_glyph(cp, &adv);
		int a = g ? adv : TODO_FONT_CELL_W;
		if (w + a > max_px) break;
		w += a;
		p += n;
	}
	return (int)(p - utf8);
}

/* 一个词（连续的 ASCII 字母数字）不可拆开 */
static int is_word_char(uint32_t cp)
{
	return (cp >= '0' && cp <= '9') || (cp >= 'a' && cp <= 'z') ||
	       (cp >= 'A' && cp <= 'Z');
}

int todo_font_wrap(const char *utf8, int max_px, char *out, int out_cap,
                   int max_lines)
{
	if (out_cap <= 0) return 0;
	out[0] = '\0';

	const char *p = utf8;
	int lines = 0;
	int out_len = 0;

	while (*p) {
		/* 跳过行首空白 */
		while (*p == ' ') p++;

		int w = 0;
		const char *start = p;
		const char *last_break = NULL;   /* 上一个可断点（空格之后）*/
		int word_start_w = 0;
		int in_word = 0;

		while (*p) {
			uint32_t cp;
			int n = todo_utf8_next(p, &cp);
			if (n <= 0) break;
			uint8_t adv;
			const uint8_t *g = todo_font_glyph(cp, &adv);
			int a = g ? adv : TODO_FONT_CELL_W;

			if (w + a > max_px && w > 0) {
				/* 放不下：在最后一个空格处断，否则硬断（中文可以硬断） */
				if (last_break != NULL && !in_word) {
					p = last_break;
				}
				break;
			}

			if (is_word_char(cp) && !in_word) { in_word = 1; word_start_w = w; }
			else if (!is_word_char(cp) && in_word) { in_word = 0; }

			w += a;
			p += n;
			if (cp == ' ') last_break = p;
		}
		(void)word_start_w;

		int nbytes = (int)(p - start);
		if (nbytes > 0) {
			if (out_len + nbytes + 2 >= out_cap) nbytes = out_cap - out_len - 2;
			if (nbytes < 0) nbytes = 0;
			memcpy(out + out_len, start, (size_t)nbytes);
			out_len += nbytes;
		}
		if (lines < max_lines) {
			out[out_len++] = '\n';
			out[out_len] = '\0';
			lines++;
		} else {
			break;
		}

		/* 吃掉断行处的空格 */
		while (*p == ' ') p++;
		if (*p == '\0') break;
	}
	return lines;
}
