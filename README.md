# 活动全周期运营工作台（演示项目）

面向公益机构活动场景的单体演示应用。把一场活动的四个环节收进一个可访问的服务：

- **报名**：多来源接入（线上表单 / 群接龙 / 代报 / 电话）、异常自动标记、重复识别与身份合并、历史档案复用
- **现场**：签到差异回传、纸质签到表与活动台账导出
- **归档**：八类资料齐备度检查、归档卡点、照片三级索引、三类导出包（对外包脱敏）
- **对外发布前**：儿童照片本地人脸检测与打码，原图不上传

技术栈：Python + Flask + SQLite；前端为静态页面；无外部数据库、缓存或消息队列依赖。

## 快速开始

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate   /   Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt

python seed_data.py     # 生成演示数据，控制台打印演示账号
python app.py           # http://localhost:5000
```

服务监听 `PORT` 环境变量（默认 5000）并绑定 `0.0.0.0`，可直接部署到单端口托管环境。

## 访问路径

| 路径 | 模块 |
|---|---|
| `/` | 统一入口 |
| `/static/index.html` | 报名统一列表、活动管理、历史信息库、现场签到、账号权限 |
| `/archive` | 活动档案管理台 |
| `/tongyan` | 儿童照片隐私处理 |

`/static/login.html` 为登录页。演示账号一键登录默认开启，账号由服务端配置提供；**生产环境请设置 `ENABLE_DEMO_LOGIN=0` 并覆盖所有演示口令**。

## 配置

复制 `config.example.env` 为 `.env`，按需修改后由部署环境注入同名环境变量。完整变量见 `config.example.env` 与 `config.py`。

| 变量 | 说明 | 默认 |
|---|---|---|
| `SECRET_KEY` | 会话签名密钥 | 每次启动随机生成 |
| `SEED_ADMIN_PASSWORD` / `SEED_WORKER_PASSWORD` / `DEFAULT_NEW_USER_PASSWORD` | 演示与新建账号口令 | 演示默认值（必须覆盖） |
| `DEMO_*_PHONE` | 演示手机号（测试号段） | `100000000NN` |
| `ENABLE_DEMO_LOGIN` | 演示一键登录开关 | `1` |
| `ORG_NAME` | 界面上的机构名称 | 示例公益机构 |
| `CORS_ORIGINS` | 跨域白名单 | 空（不开放跨域） |
| `PORT` | 监听端口 | `5000` |

## 目录结构

```
├── app.py                 报名模块主服务与业务接口
├── modules_ext.py         门户、归档台、照片脱敏模块的路由与数据读写
├── config.py              环境变量配置
├── config.example.env     配置模板
├── seed_data.py           演示数据生成
├── static/                报名模块前端页面
├── portal/index.html      统一入口
├── archive/               档案管理台（页面 + CSV 数据表 + 字典）
├── tongyan/               照片脱敏页面（检测运行时与模型从 CDN 加载）
├── data/                  运行时 SQLite（不入库）
└── requirements.txt
```

## 数据说明

`data/rainbow.db` 与 `tongyan/saved/` 下的图片均为运行期产物，已加入 `.gitignore`。归档台的 `archive/data/*.csv` 为业务表与系统字典表，交付态为空库（仅表头与字典）。演示数据全部为构造值，不含任何真实个人信息。

## 脱敏说明

本仓库为公开发布版本，已做以下处理：

- 机构名称统一替换为可配置的「示例公益机构」
- 人名、家长称谓、群内昵称统一替换为角色占位
- 全部手机号替换为 `100000000NN` 测试号段
- 移除硬编码会话密钥；账号口令改由环境变量提供
- 移除部署链接、本地绝对路径、真实账号与内部说明
- 运行期数据库与生成图片不入库

首次部署前请自行确认以上替换是否完整，并覆盖演示口令。

## 第三方资源

照片脱敏模块从 jsDelivr CDN 加载 face-api.js 运行时与模型权重（仓库内不内置二进制文件），因此该模块需要联网。来源与版本见 `THIRD_PARTY_NOTICES.md`。若需完全离线，按该文件中的说明自行把资源放回 `tongyan/vendor/` 并改回本地路径。

Python 依赖见 `requirements.txt`。

## 已知限制

- 演示项目，未做生产级鉴权、审计与数据备份
- 照片检测运行时与模型从 CDN 加载，离线环境下自动识别不可用（手动补框仍可用）
- 归档台与照片脱敏模块不设登录，公网部署需自行加一层访问控制
- 自动年龄判断存在误判，照片对外发布前必须人工复核
- 无外部数据库依赖，数据随应用目录一起备份
