# ---- 字库放哪 --------------------------------------------------------
# 三种模式：
#   TODO_FONT_IN_PARTITION  字库在独立 flash 分区（app 瘦身到 ~1.1MB，
#                           烧录擦除量减半，写入稳定得多）★ 推荐
#   TODO_SOURCE_MAC_BITMAP  画面由 Mac 渲染，不带字库
#   默认                    字库编进 app 的 .rodata（+1MB）
file(READ "${CMAKE_CURRENT_LIST_DIR}/../../main/todo_config.h" _todo_cfg)
set(_srcs "todo_font.c" "todo_fb.c" "todo_state.c" "todo_layout.c" "cJSON/cJSON.c")
if(_todo_cfg MATCHES "define[ \t]+TODO_FONT_IN_PARTITION")
    message(STATUS "shared: 字库放独立 flash 分区（app 瘦身）")
    list(APPEND _srcs "")
    set(_font_defs TODO_FONT_EMBED=0 TODO_FONT_IN_PARTITION=1)
elseif(_todo_cfg MATCHES "define[ \t]+TODO_SOURCE[ \t]+TODO_SOURCE_MAC_BITMAP")
    message(STATUS "shared: 画面由 Mac 渲染，不内嵌字库")
    set(_font_defs TODO_FONT_EMBED=0)
else()
    message(STATUS "shared: 板子自己渲染，内嵌字库")
    list(APPEND _srcs "todo_font_data.c")
    set(_font_defs TODO_FONT_EMBED=1)
endif()

idf_component_register(
    SRCS ${_srcs}
    INCLUDE_DIRS "." "cJSON")
target_compile_definitions(${COMPONENT_LIB} PRIVATE ${_font_defs})
    # 这个组件被 main 自动依赖（IDF 对 main 有「依赖所有项目组件」的待遇），
    # 所以 main 那边不用手写 REQUIRES —— 手写反而会把那个便利机制关掉。
