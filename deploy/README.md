# 续页 Linux 部署

## 私人试用

若服务只绑定在服务器的 `127.0.0.1:8767`，可通过 SSH 从开发机访问。把 `user@host` 换成自己的 SSH 登录地址，保留终端窗口。

```sh
ssh -N -L 18767:127.0.0.1:8767 user@host
```

[打开转发后的页面](http://localhost:18767/)

更新时记录当前镜像和数据挂载，先用独立书架验证候选版本的登录、问答、引文和回退。更新后核对前端构建及相同书签的在线结果。保留更新前的镜像，供服务启动或问答异常时恢复。私用实例的数据与测试书架分开保存。

## 将来使用域名和 HTTPS

使用 Ubuntu 24.04 LTS。将域名的 A 记录指向服务器公网 IPv4。阿里云防火墙开放 80、443，SSH 端口限制为管理用来源 IP。应用容器只在 Docker 内网接收流量。

安装 Docker Engine 及 Compose 插件后，把项目传至服务器。进入项目目录，复制环境文件，并把真实域名填入对应文件。

```sh
cd deploy
cp private.env.example private.env
cp demo.env.example demo.env
cp proxy.env.example proxy.env
chmod 600 private.env demo.env proxy.env
docker run --rm -it caddy:2 caddy hash-password
```

将 Caddy 输出的 bcrypt 哈希填进 proxy.env 的单引号内。首次进入网站时，浏览器先要求输入这一层的账号和密码。应用的私人账号由访客在站内注册并使用 private.env 配置的邀请码。在 private.env 配置服务器专用的 `RELAYCORE_API_KEY`。问答与追踪会通过 RelayCore 将所需原文发送给该密钥分组支持的模型。更新环境文件后重新创建相应容器。

```sh
docker compose --env-file proxy.env up -d --build reader caddy
docker compose --env-file proxy.env ps
docker compose --env-file proxy.env logs --tail 50 reader caddy
```

先用示范书验证 HTTPS、注册、登录、两端书签、旧引文和服务重启，再导入私人资料。用应用登录名与反向代理密码时应分别设置密码。安装和 HTTPS 行为可对照 [Docker 安装说明](https://docs.docker.com/engine/install/ubuntu/) 与 [Caddy 自动 HTTPS 文档](https://caddyserver.com/docs/automatic-https)。

## 数据备份与恢复

数据在 Compose 的 xudu_private-data 卷，包括 SQLite 数据库与 PDF。定期保留副本，存到与服务器及部署目录分离的位置。以下命令以 Bash 执行，先停私人阅读容器，备份结束后启动。

```sh
mkdir -m 700 -p backups
docker compose --env-file proxy.env stop reader
docker run --rm -v xudu_private-data:/source:ro -v "$PWD/backups:/backup" python:3.12-slim-bookworm \
  tar -C /source -czf "/backup/xudu-$(date +%F-%H%M).tar.gz" .
docker compose --env-file proxy.env start reader
```

恢复演练先创建独立空卷。选择备份后，用下面的命令解包并运行 SQLite 完整性检查。之后可启动独立阅读实例，实际打开一条备份中的引文。迁移本机书架时，也采用停止写入后复制整个数据目录的方式。Cookie 与域名绑定，远程首次访问时需要用已注册账号登录。

```sh
docker volume create xudu_restore
docker run --rm -v xudu_restore:/target -v "$PWD/backups:/backup:ro" python:3.12-slim-bookworm \
  sh -c 'tar -xzf "/backup/$1" -C /target && chown -R 10001:10001 /target && python -c "import sqlite3; print(sqlite3.connect(\"/target/reader.sqlite3\").execute(\"PRAGMA integrity_check\").fetchone())"' \
  restore xudu-YYYY-MM-DD-HHMM.tar.gz
```

当前删书会立即删除当前书架及关联记录。服务端同一份 PDF 由内容摘要复用，磁盘中的孤立原件、生成缓存和备份仍需按实际运营设定保留与清理期限。

## 公开演示

私人验收通过后，可另外解析演示子域名。将 demo.caddy.example 复制到 sites/demo.caddy，换成实际域名，并在 demo.env 配置该域名及独立的 `RELAYCORE_API_KEY`。`RELAYCORE_MODEL` 使用该密钥分组支持的模型 ID。

```sh
docker compose --env-file proxy.env --profile demo up -d --build demo
docker compose --env-file proxy.env exec caddy caddy reload --config /etc/caddy/Caddyfile
```

公开实例使用独立数据卷，仅开放示范书。默认按 UTC 日限制全站与每个访客的模型调用数，问答、追踪和卡片归并共用限额。记录响应时间和输入输出 token，不记录提问及原文。公开前以两个浏览器验收隔离，实际核对当天预算与 DeepSeek 账单。样书版本保留原 PDF 的来源页。[Bookstacks 版本](https://bookstacks.org/zh-Hans/authors/wu-chengen/journey-to-the-west) · [底本许可说明](https://www.gutenberg.org/ebooks/23962)
