# HTKnow 端到端测试 Web 控制台 · Docker 部署

把 `scripts/e2e_web.py` 控制台打包成镜像部署在本机：浏览器输入 `HOST:PORT`
（如 `192.168.0.46:11000`）即可发起全链路测试，运行产物持久化在 Docker 卷里。

## 构建

```shell
./deploy/e2e-web/build.sh                 # 默认产出 htknow-e2e-web:latest
./deploy/e2e-web/build.sh myreg/e2e-web:v1  # 自定义标签
```

- `build.sh` 会先组装最小构建上下文（控制台、测试与评分脚本、题集和 entrypoint），避免把
  `target/` 等大目录送进 build context
- 若本机已有语料缓存（`scripts/.corpus/` 或 `scripts/corpus/`），会烘进镜像，容器可**完全离线**测试；
  没有则镜像不含语料，首次联网运行自动下载并做 SHA-256 校验
- 镜像基于 `python:3.12-slim`，运行时零第三方依赖；国内拉不动 Docker Hub 时可先
  `docker pull docker.m.daocloud.io/library/python:3.12-slim && docker tag ... python:3.12-slim`

## 部署（本机）

```shell
docker compose -f deploy/e2e-web/docker-compose.yml up -d
```

- 默认 **host 网络**：控制台监听宿主机 `8099` 端口，能直接测局域网与宿主机自身的任意地址；
  打开 `http://<本机IP>:8099` 即可使用
- 运行产物（日志 / HTML / JSON 报告 / 历史元数据）在命名卷 `e2e-web_e2e-web-data` 的 `/data/runs`，
  容器重建、镜像升级后历史仍保留
- `restart: unless-stopped`：宿主机重启后自动拉起

常用运维命令：

```shell
docker logs -f htknow-e2e-web                                  # 看服务日志
docker compose -f deploy/e2e-web/docker-compose.yml down       # 停止（保留数据卷）
docker compose -f deploy/e2e-web/docker-compose.yml up -d --force-recreate   # 升级镜像后重建
docker volume rm e2e-web_e2e-web-data                          # 彻底清空历史与报告（慎用）
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `E2E_WEB_PORT` | `8099` | 控制台监听端口（host 网络下即宿主机端口） |
| `E2E_RUNS_DIR` | `/data/runs` | 运行产物目录，建议保持在 `/data` 卷内 |
| `E2E_MAX_CONCURRENT` | `2` | 同时运行的测试数量上限 |
| `E2E_MAX_HISTORY` | `50` | 保留的历史运行条数，超出自动清理 |
| `E2E_JUDGE_URL` | 空 | 独立答案评审接口，例如 `https://provider.example/v1/chat/completions` |
| `E2E_JUDGE_MODEL` | 空 | 独立评审模型名称 |
| `E2E_JUDGE_API_KEY` | 空 | 评审鉴权密钥，仅通过容器环境传入，不写入报告或命令行 |

Compose 会从启动环境传入以上三项评审配置。配置后选择「对话答案」或「完整链路」即可运行语义评估。
未配置时可以使用「仅规则检查」，但语义门禁会保持未通过；仅检索模式不需要评审模型。
修改环境后重新创建容器，使新配置生效。

## 不用 host 网络（可选）

编辑 `docker-compose.yml`：注释 `network_mode: host`，启用 `ports: ["8099:8099"]` 与
`extra_hosts: ["host.docker.internal:host-gateway"]`。此时测**宿主机自身**的服务要填
`host.docker.internal:<port>` 而不是 `127.0.0.1:<port>`；测局域网其他机器不受影响。

## 安全提示

控制台**没有鉴权**：host 网络 + `0.0.0.0` 监听意味着同网段任何人都能发起测试。
暴露到不可信网络前请自行加反向代理认证 / VPN / 防火墙白名单。
