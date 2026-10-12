# 第三方代码与资源说明

## RhineLabUI

本插件的内嵌 WebUI 使用并改编了 [LBEILC / RhineLabUI](https://github.com/LBEILC/RhineLabUI) 的部分原创界面代码。

- 上游版本：`12cc5e4013acb9408f753ff71de6b8492299b7c8`。
- 版权：**Copyright (c) 2026 LBEILC**。
- 许可证：**MIT**，全文保存在 [`views/schedule/vendor/rhinelab/LICENSE`](views/schedule/vendor/rhinelab/LICENSE)。分发这些代码或其重要部分时，应保留该版权声明及许可证全文。
- `views/schedule/vendor/rhinelab/content-transition.js`：从上游 `src/ui-transitions.ts` 中的 `ContentTransition` 类改编，仅去除 TypeScript 类型标注，以浏览器原生 JavaScript 模块运行。使用 Web Animations API 控制页签内容淡入，支持减少动态效果。
- `views/schedule/style.css`：基于上游 `src/workbench.css` 的细线分隔、编号导航和工作台排版，以及 `src/theme-ui.ts` / `src/theme.css` 的纸色、墨色、铜色和深色主题配色进行改编；为 AstrBot 插件面板、日程编辑和窄屏重新布局。

本插件未打包 RhineLabUI 的字体文件、三维模型、配乐、音频采样、原作档案文本或《明日方舟》及莱茵生命相关标志。上游 MIT 许可不授予第三方商标、设定或素材的权利；这些内容不属于本次代码复用范围。本插件也不依赖上游的 Three.js、Vite、Rolling Number 或 PWA 运行环境。

## Python 依赖

`requirements.txt` 中的 `holidays`、`tzdata` 与 `aiohttp` 由安装环境获取，各自遵循其原有许可证。本插件发布包不内嵌这些依赖的源码或本机安装副本。
