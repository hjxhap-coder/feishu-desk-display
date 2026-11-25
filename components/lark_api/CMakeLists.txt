idf_component_register(
    SRCS "lark_api.c"

    INCLUDE_DIRS "."
    REQUIRES esp_http_client nvs_flash esp-tls shared)
    # 依赖 shared 是为了用它的 cJSON。
    # shared 是项目组件，main 自动依赖它，但组件之间的依赖还是要自己写。
