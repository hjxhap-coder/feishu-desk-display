# 生成点阵字库

从[文泉驿官方发布页](https://sourceforge.net/projects/wqy/files/wqy-bitmapfont/1.0.0-RC1/)下载 wqy-bitmapsong-bdf-1.0.0-RC1_GPLv2+.tar.gz。解压后，将 wqy-bitmapsong/wenquanyi_12pt.bdf 放到 font/，在项目根目录运行。

    python3 tools/make_font_bin.py font/wenquanyi_12pt.bdf --bin font/font16.bin

生成的字库用于预览和烧录，分区偏移为 0x310000。若使用固件的内嵌字库模式，在生成命令后追加 --c components/shared/todo_font_data.c。

上游的 README 和 COPYING 说明了文泉驿字库的版权及许可，包括 GPL v2 或更高版本及字体嵌入例外。本项目代码使用 MIT 许可。
