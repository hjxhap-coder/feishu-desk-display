/* Mac 端不需要内嵌字库（用 .bin 文件），但链接器要能找到这两个符号。 */
#include "todo_font.h"

const unsigned char TODO_FONT_BIN[] = { 0 };
const unsigned int  TODO_FONT_BIN_LEN = 0;
