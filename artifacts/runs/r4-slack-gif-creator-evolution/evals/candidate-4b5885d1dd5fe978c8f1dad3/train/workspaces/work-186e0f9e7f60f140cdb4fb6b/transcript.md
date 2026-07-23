# 执行记录

- 工作项：`work-186e0f9e7f60f140cdb4fb6b`
- 任务：直接使用随附 PPM 徽章，制作 128×128 的上浮、轻微越位、回稳 Slack GIF。

## 输入与实现

读取配置后，直接用 Pillow 加载 `benchmarks/canaries/slack-gif-creator/fixtures/input-badge.ppm`。输入是 8×8 像素图；仅将边缘深蓝背景色 `(12, 31, 45)` 转成透明，青色轮廓与橙色中心像素保持原值，并用最近邻采样放大，未重画替代主体。

按候选 Package 的说明及代码执行：

- `core.easing.interpolate(..., easing="back_out")` 驱动一条连续竖直路径；
- `core.frame_composer.create_gradient_background` 创建不遮挡主体的深色背景；
- `core.gif_builder.GIFBuilder` 收集、调色并导出 48 色帧；
- `core.validators.validate_gif` 对最终文件执行 Slack emoji 校验。

主体中心的像素位置从 `y=96` 上浮，最低到 `y=58`，再回落稳定在 `y=61`。最终停靠点精确命中；回稳尾段相邻位移在 1 像素整数化容差内未出现异常加速。背景使用主体后方的青色光晕、细环和侧边闪光，不覆盖徽章。

## 导出与核验

候选 `GIFBuilder` 首轮写出的后端帧时长被读取为 6.6 秒，超过配置上限。保留 Package 生成和全局调色后的帧，显式按 12 fps 重新封装 GIF，再调用 Package validator 校验。

最终结果：

- 文件：`uploaded_badge_lift.gif`
- 尺寸：128×128
- 帧数：22
- 实际时长：1.76 秒
- 读取帧率：12.5 fps
- 文件大小：57,837 字节
- Slack emoji 尺寸校验：通过
- 关键帧视觉抽查：起始、上浮、越位、回稳与最终停靠均清晰；青色轮廓和橙色中心可辨识。

辅助记录位于 `render-report.json`，可复核路径采样与验证指标。
