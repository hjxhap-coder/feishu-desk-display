idf_component_register(
    SRCS "main.cpp"

    INCLUDE_DIRS "./")
    # 注意：这里不要手写 REQUIRES / PRIV_REQUIRES。
    # ESP-IDF 在 project.cmake 里对 main 有个特殊待遇：只要用户没手动声明依赖，
    # main 就会自动依赖构建里的每一个组件（display_bsp / user_app / esp_timer /
    # esp_vfs_console ... 全都算）。
    # 一旦手写 REQUIRES，这个便利机制就没了，main 会突然找不到 display_bsp.h
    # 和 driver/spi_master.h。
