# 第三方资源说明

## face-api.js（照片脱敏模块）

- 版本：npm 包 `@vladmandic/face-api@1.7.15`
- 上游：https://github.com/vladmandic/face-api
- 许可：请以上游仓库的 LICENSE 为准（MIT）；本仓库不内置其构建产物，仅从 CDN 引用
- 运行时从以下地址加载，未做修改：
  - `https://cdn.jsdelivr.net/npm/@vladmandic/face-api@1.7.15/dist/face-api.js`
  - `https://cdn.jsdelivr.net/npm/@vladmandic/face-api@1.7.15/model/tiny_face_detector_model.bin`（及 manifest）
  - `https://cdn.jsdelivr.net/npm/@vladmandic/face-api@1.7.15/model/age_gender_model.bin`（及 manifest）

### 如需完全离线

把上述文件下载到 `tongyan/vendor/face-api.min.js` 与 `tongyan/vendor/models/`，再把 `tongyan/index.html` 中的两处 CDN 地址改回 `/tongyan/vendor/face-api.min.js` 与 `/tongyan/vendor/models` 即可。

## Python 依赖

见 `requirements.txt`：Flask、Werkzeug、flask-cors、openpyxl、reportlab。各依赖许可以其官方发布页为准。
