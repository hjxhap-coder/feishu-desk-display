/*
 * todo_font.h —— 点阵中文字库（TOF1 格式）
 *
 * 这套代码在 ESP32 和 Mac 上编译同一份，所以 Mac 上的预览就是板子上的真机效果。
 *
 * 字库数据由 tools/make_font_bin.py 从文泉驿点阵宋体 BDF 生成，
 * 板子和 Mac 读的是同一个文件（板子编译进 flash，Mac 直接读 .bin）。
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TODO_FONT_CELL_W 16
#define TODO_FONT_CELL_H 18
#define TODO_FONT_ASCENT 14

typedef struct {
	const uint8_t *bin;
	uint32_t       bin_len;
	uint8_t        cell_w;
	uint8_t        cell_h;
	uint8_t        ascent;
	uint16_t       n_glyph;
	uint32_t       index_off;
	uint32_t       index_len;
	uint32_t       data_off;
} TodoFont;

/* 内嵌数据（编译进固件），定义在 todo_font_data.c */
extern const unsigned char TODO_FONT_BIN[];
extern const unsigned int  TODO_FONT_BIN_LEN;

/* 用内嵌数据初始化 */
bool todo_font_init(void);

/* 从内存加载（Mac 端预览用 .bin 文件） */
bool todo_font_load_mem(const uint8_t *data, uint32_t len);

/* 取一个码位的字形位图（cell_h 行，每行 cell_w/8 字节，1 = 有墨）。
 * 找不到返回 NULL。advance 输出该字的步进宽度。 */
const uint8_t *todo_font_glyph(uint32_t codepoint, uint8_t *advance);

/* UTF-8 解码：取一个字符，返回消耗的字节数（非法序列返回 1） */
int todo_utf8_next(const char *s, uint32_t *codepoint);

/* 整串 UTF-8 的像素宽度 */
int todo_font_text_width(const char *utf8);

/* 截断到不超过 max_px 宽度（不处理 UTF-8 边界，返回实际写入的字节数） */
int todo_font_fit(const char *utf8, int max_px);

/* UTF-8 感知的换行。逐字贪心，超宽就断行（中文可断，英文不拆单词）。
 * 结果写进 out，行间用 '\n' 分隔，每行不带换行符。
 * 返回行数；放不下时截到 max_lines 行。 */
int todo_font_wrap(const char *utf8, int max_px, char *out, int out_cap,
                   int max_lines);

#ifdef __cplusplus
}
#endif
