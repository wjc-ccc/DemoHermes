-- 感知模块统一命名；只迁移轮次信封的顶层字段，保留 Trace 与用户原文。
UPDATE turns
SET turn_json = json_remove(
    CASE
        WHEN json_type(turn_json, '$.perception_trace') IS NULL
        THEN json_set(turn_json, '$.perception_trace', json_extract(turn_json, '$.precision_trace'))
        ELSE turn_json
    END,
    '$.precision_trace'
)
WHERE json_type(turn_json, '$.precision_trace') IS NOT NULL;
