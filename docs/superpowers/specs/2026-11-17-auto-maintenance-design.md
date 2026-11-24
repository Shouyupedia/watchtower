# 自动维护设计（依赖自动合并、定期发布、去除上游残留）

日期：2026-11-17
状态：待批准

## 1. 意图

这个仓库是 containrrr/watchtower 的个人 fork，由一个人维护。维护者希望仓库能自己保持健康：

- 依赖升级在测试通过后自动合并，不需要人每周去点。
- 定期自动重建并发布，让二进制和镜像拿到最新的依赖、Go 工具链和根证书。
- 去掉指向上游 containrrr 的残留配置、链接和文件。
- 整条自动化链路要防止供应链投毒。

### 为什么"防投毒"是硬约束

watchtower 镜像运行时挂载 `docker.sock`，权限等同宿主机 root。而且 watchtower 默认会自动更新自己，用户普遍跑 `:latest`。一个被投毒的依赖如果被自动合并、自动发布，所有运行该镜像的主机都会在下一个轮询周期里自行拉取它。所以"全自动"的每一环都必须有闸门；任何一环拿不准时，结果要停在"不合并 / 不发布"一侧。

## 2. 目标、非目标与反目标

**目标**

1. Dependabot 对 Go 模块、Docker 基础镜像、GitHub Actions 三类依赖每周检查升级。
2. Go 模块和 Docker 基础镜像的补丁版、次版本升级，以及 digest 升级，在全部检查通过后自动合并。
3. 每周自动检查 main 自上次发版以来是否有新提交。如果有，就在该提交上重跑完整检查，通过后自动发布下一个补丁版本：GitHub Release 二进制，加 GHCR 多架构镜像。
4. 所有发布产物附带可校验的构建来源证明（build provenance attestation）。
5. 清除上游残留。

**非目标**

- 不改 Go 模块路径 `github.com/containrrr/watchtower`。改它要动全部 Go 文件，用户感知不到，适合以后单独做。
- 不改容器标签 `com.centurylinklabs.watchtower*`。这是运行时协议，改了会让现有部署失效。
- 不改测试夹具（`pkg/container/mocks/data/*.json`）里出现的上游镜像名。它们只是测试数据。
- 不升级 goreleaser 主版本（v0.155.0 → v2 需要迁移配置）。本次只把它钉死，并记为后续工作。
- 不改 mkdocs 主题里的配色方案名 `containrrr`。这是 CSS 内部标识符，用户看不到。
- 不自动升级 Go 次版本（如 1.26 → 1.27），这仍需人工一次性改 go.mod、CI 和 Dockerfile。

**反目标**（即使"能用"也算失败的情况）

- 自动合并在缺少某项检查结果时仍然合并。
- 自动合并了一个大版本升级，或一个 GitHub Actions 升级。
- 合并的提交不是被测试过的那个提交。
- 已发布的版本号被不同内容覆盖。同一版本号必须永远对应同一份产物。
- 有写权限或发布权限的任务里运行了来自 PR 的不可信代码。

## 3. 约束

- **仓库设置只能由维护者修改。** 本会话的工具改不了 GitHub 仓库设置，设计不能依赖"我能替他开开关"。需要维护者手动打开的开关列在第 8 节。
- **fork 仓库上 Dependabot 默认关闭。** GitHub 文档写明 fork 会自动关闭 Dependabot security updates。仓库至今没有出现过 Dependabot PR，也说明 version updates 没在运行。
- **Dependabot 触发的工作流有特殊限制。** `pull_request` 等事件下 `GITHUB_TOKEN` 默认只读，可以用 `permissions:` 显式提权（GitHub 官方的自动合并示例就是如此）。读不到 Actions secrets，所以 `CODECOV_TOKEN` 为空，覆盖率上传要能容忍失败。
- **冷却期（`cooldown`）支持范围不同。** gomod 支持按 semver 级别分别设天数；github-actions 和 docker 只支持 `default-days`。冷却期只对 version updates 生效，不对 security updates 生效。
- **用 `GITHUB_TOKEN` 推送的提交和 tag 不会触发 `push` 类工作流**（`workflow_dispatch`、`repository_dispatch` 例外）。所以自动合并后 main 上的 `push` 工作流不会运行，自动打的 tag 也不会触发 tag 工作流。发布流程不能依赖这些触发。
- **镜像构建不需要 QEMU。** 现有 Dockerfile 用 `--platform=$BUILDPLATFORM` 交叉编译，最终阶段是 `scratch`，只做 COPY。

## 4. 方案比较（自动合并的闸门放在哪里）

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| **A（选定）** | 合并是 PR 工作流里的最后一个任务，`needs` 所有检查任务，用 `--match-head-commit` 只合并被测过的提交 | 不依赖仓库设置；任一检查失败或缺席就不合并，出错时停在安全一侧 | CodeQL 是独立工作流，不在闸门内 |
| B | 工作流只为合格 PR 开启 GitHub 原生 auto-merge，门槛靠 main 分支规则集 | 平台层面强制，人手动合并也受约束 | 规则集只能维护者手动配置，本会话无法验证；漏配时会直接合并，出错时停在放行一侧 |
| C | 独立工作流用 `workflow_run` 在 CI 结束后合并 | 合并逻辑永远取自 main | 要自己汇总多个工作流的结论、核对 SHA，零件多；对 Dependabot PR 的额外收益很小 |

维护者描述的是想要的结果，把实现方式交给了构建者，所以由构建者选 A，理由如上。B 的规则集作为可选的第二道防线写进第 8 节。实现上，合并任务先尝试直接合并；如果被分支规则拒绝（说明维护者配置了 B），就退回 `gh pr merge --auto`，交给 GitHub 在规则满足后合并。两条路径都带 `--match-head-commit`。

## 5. 设计

### 5.1 防投毒的分层措施

1. **延迟入场（冷却期）。** gomod：补丁版 3 天，次版本 7 天，大版本 30 天。docker：7 天。github-actions：7 天。被投毒的版本通常在几小时到几天内被发现并撤回，冷却期让 Dependabot 不去碰刚发布的版本。
2. **自动合并白名单。**
   - 只合并本仓库分支上（`head.repo == 本仓库`）、作者是 `dependabot[bot]` 的 PR。
   - 生态必须是 `go_modules` 或 `docker`。生态从事件负载里的 `head_ref` 解析（如 `dependabot/go_modules/...`），不从可被篡改的内容推断。
   - gomod 的 update-type 必须是 `semver-patch` 或 `semver-minor`。docker 只允许 digest 升级（update-type 为空）和补丁版。
   - **GitHub Actions 升级永不自动合并。** Actions 在发布任务里拿着发布权限运行，测试通过说明不了它没被投毒。而且已有攻击是改写旧 tag 指向（tj-actions 事件），这会让基于发布日期的冷却期失效。Actions 升级被分成一个组，每周最多一个 PR，由维护者审阅后合并。
3. **全部钉死。**
   - 所有 `uses:` 钉到完整 commit SHA，后面加版本注释，由 Dependabot 维护。
   - 三个 Dockerfile 的 `golang:1.26-alpine` 加 `@sha256:` digest，由 Dependabot 维护。golang 镜像忽略次版本和大版本标签升级，只跟 digest 和补丁版。
   - staticcheck、govulncheck 改为独立的 `tools/go.mod` 模块里的 `tool` 依赖。版本由 go.sum 和 Go checksum database 校验，并由 Dependabot（带冷却期）维护，替代现在写死在 `with: version:` 里、Dependabot 看不到的版本号。
   - goreleaser 保持 `v0.155.0` 不变，钉死。
4. **合并前检查（闸门）。** 自动合并任务 `needs` 下列全部任务：
   - staticcheck（lint）
   - `go test` 三平台矩阵
   - `go mod verify`：校验模块缓存与 go.sum 一致
   - `go mod tidy -diff`：go.mod/go.sum 必须是 tidy 的，防止夹带无关依赖
   - govulncheck：已知漏洞，失败即阻断
   - goreleaser snapshot 构建
   - Docker 多架构构建（不推送）
   - `actions/dependency-review-action`：本次 PR 新引入或升级到的依赖如有已知漏洞即阻断
   - zizmor：GitHub Actions 工作流静态审计，发现危险写法即阻断
5. **最小权限。**
   - 每个工作流顶层 `permissions: {}`，各任务只声明自己需要的权限。
   - `actions/checkout` 一律 `persist-credentials: false`。
   - 删除所有 `pull_request_target` 工作流（greetings、dependabot-approve）。
   - 合并任务不检出代码，只运行 runner 自带的 `gh`。
   - 读取 Dependabot 元数据的 `dependabot/fetch-metadata` 放在单独的只读任务里，把结果作为输出交给合并任务；第三方 action 不和写权限待在同一个任务里。
6. **可校验的产物。** 发布的二进制压缩包、`checksums.txt` 和 GHCR 镜像都用 `actions/attest-build-provenance` 生成 GitHub 签名的 SLSA 构建来源证明，镜像证明推送到仓库，同时附 SBOM。用户可以用 `gh attestation verify` 校验。README 写明校验命令。
7. **删除仓库根目录的 `oryxBuildBinary`。** 这是一个 20MB、未剥离的可执行文件，在上游 #1781（一个 gomega 升级 PR）里被误提交，来源无法验证。同时在 `.gitignore` 里屏蔽它。

### 5.2 工作流结构

| 文件 | 触发 | 内容 |
| --- | --- | --- |
| `ci.yml`（新，可复用） | `workflow_call`（输入 `ref`） | lint、test 矩阵、mod verify/tidy、govulncheck、goreleaser snapshot、docker 构建（不推送）、zizmor。PR、main 推送、发布前都调用它，确保门槛只有一份定义。 |
| `pull-request.yml`（改） | `pull_request` → main | 调用 `ci.yml`；dependency-review；Dependabot 元数据（只读任务）；auto-merge（`needs` 前面全部任务，仅对 Dependabot PR 运行）。按 PR 设 `concurrency`，新提交取消旧运行。 |
| `push-main.yml`（取代 `release-dev.yaml`） | `push` → main，`workflow_dispatch` | 调用 `ci.yml`；通过后发布 `:latest-dev` 镜像。 |
| `release.yml`（改，可复用） | 推送 `v*.*.*` tag（手动发版）；`workflow_call`（输入 `tag`） | 在该 tag 上调用 `ci.yml` → goreleaser 发布 GitHub Release 并为产物生成证明 → 构建推送 `X.Y.Z` 与 `latest` 镜像并生成证明。 |
| `scheduled-release.yml`（新） | 每周二 03:23 UTC；`workflow_dispatch` | 判定任务（只读）：找最高的 `vX.Y.Z` tag，如果 main 上没有新提交就结束；否则计算下一个补丁版本号。打 tag 任务（`contents: write`）：创建带注释的 tag 并推送，这个推送不会触发 tag 工作流。然后调用 `release.yml`。 |
| `docker.yml`（改为可复用） | `workflow_call`（输入 `ref`、`tags`、`push`）；`workflow_dispatch`（重新发布已有 tag） | 唯一的镜像构建/推送实现，被 ci、push-main、release 复用。 |
| `codeql-analysis.yml`（改） | 不变 | 钉 SHA，顶层只读，删除已过时的 `git checkout HEAD^2` 步骤。 |
| 删除 | — | `greetings.yml`、`dependabot-approve.yml`、`release-dev.yaml`、`publish-docs.yml`（不发布 GitHub Pages） |

**发布语义**

- 版本号只增不覆盖。
- 自动发布只发补丁版本；次版本和大版本由维护者手动推 tag，走同一个 `release.yml`。
- main 没有变化时不发布，也不重建。基础镜像和 Go 工具链的更新会通过 Dependabot 的 digest PR 进入 main，进而触发下一次发布。
- 自动发布前在被发布的提交上重跑完整的 `ci.yml`，不依赖 main 上是否有过 CI 运行，因为 `GITHUB_TOKEN` 合并的提交不会触发 push 工作流。

### 5.3 Dependabot 配置

- `gomod`，目录 `/`：每周一次；冷却期 3/7/30 天；次版本和补丁版归入一组（`go-minor-patch`），大版本单独开 PR。
- `gomod`，目录 `/tools`：同上，单独成组。
- `docker`，目录 `/dockerfiles`：每周一次；冷却期 7 天；golang 镜像忽略 semver-major 和 semver-minor。
- `github-actions`，目录 `/`：每周一次；冷却期 7 天；全部归入一组。

### 5.4 上游残留清理

| 对象 | 处理 |
| --- | --- |
| `oryxBuildBinary` | 删除，并加入 `.gitignore` |
| `.github/CODEOWNERS`（上游维护者） | 删除。单人维护，没有用处 |
| `.github/stale.yml`（Probot）、`.devbots/`、`.codacy.yml`、`.all-contributorsrc` | 删除。对应的第三方 App 没有安装，配置不生效。README 里保留对上游的致谢 |
| `greetings.yml`（链接上游行为准则和贡献指南） | 删除 |
| Issue 模板 | 镜像名改为 `ghcr.io/shouyupedia/watchtower:latest`；去掉"不支持其他 fork"的说法；联系链接改为本仓库 |
| PR 模板 | 去掉面向社区贡献者的措辞，保留检查项 |
| `SECURITY.md` | 上游维护者的邮箱改为本仓库的 GitHub 私密漏洞报告入口 |
| `code_of_conduct.md` | 不再引用 Containrrr 社区准则，改为引用 Contributor Covenant 2.1 |
| `CONTRIBUTING.md` | 示例镜像名改为本仓库；更正 `Dockerfile.self-contained` 的说明（它现在构建本地源码） |
| `mkdocs.yml` | 移除 `site_url`（不发布 GitHub Pages），`repo_url`、`user` 指向本仓库；`exclude_docs` 排除 `superpowers/` |
| `docs/*.md` | 用户可见的镜像名 `containrrr/watchtower` 改为 `ghcr.io/shouyupedia/watchtower`；指向 `containrrr.dev/watchtower` 和 `containrrr.github.io/watchtower` 的站内链接改为相对链接；`index.md` 里上游的 CI/Codecov/GoDoc/Docker Hub 徽章删除或改为本仓库。指向 shoutrrr（真实依赖）的链接保留 |
| `scripts/dependency-test.sh` | 镜像名改为本仓库 |
| 保留 | Go 模块路径、`com.centurylinklabs.watchtower*` 标签、`containrrr/shoutrrr` 依赖、测试夹具、LICENSE、README 致谢 |

## 6. 留给构建者决定的

- 具体 action 的版本：选各自最新的稳定版，钉 SHA。
- staticcheck 版本号与 `honnef.co/go/tools` 模块版本的对应关系。
- zizmor 的接入方式（action 还是 pip 固定版本）和严重级别阈值。当前全部工作流需要在新配置下零告警，必要的豁免逐条写理由。
- 覆盖率上传保留在 `ci.yml` 里，用输入开关控制，失败不阻断。
- `docs/index.md` 徽章的具体替换。

## 7. 验证方式（本会话内能做的）

- `go build ./...`、`go vet ./...`、`go test ./...`、`go mod verify`、`go mod tidy -diff` 通过。
- `go tool -modfile=tools/go.mod staticcheck ./...` 和 `govulncheck ./...` 在本地运行。
- 所有工作流通过 actionlint 和 zizmor；`dependabot.yml` 通过 JSON Schema 校验。
- 所有 `uses:` 的 SHA 用 `git ls-remote` 与版本 tag 逐一核对。
- 本会话无法运行 GitHub Actions、无法创建 Dependabot PR，也无法发布。这些要在推送后由真实运行验证，见第 8 节。

## 8. 需要维护者在 GitHub 上手动完成的设置

1. **Settings → Advanced Security（或 Code security）**：打开 Dependency graph、Dependabot alerts、Dependabot security updates。Dependabot version updates 在 `dependabot.yml` 合入 main 后生效；如果 fork 上仍不运行，到 Insights → Dependency graph → Dependabot 里启用。
2. **Settings → Actions → General → Workflow permissions**：选 "Read repository contents and packages permissions"（默认只读）。工作流会自行声明需要的权限。
3. **Settings → Advanced Security → Private vulnerability reporting**：启用，`SECURITY.md` 指向这里。
4. 可选（第二道防线）：**Settings → Rules → Rulesets**，给 `main` 加规则，要求 `Pull Request` 工作流中的检查通过才能合并。不要设置"需要审批"，否则自动合并会被卡住。
5. 合入后到 Actions 页手动运行一次 `Scheduled release`（`workflow_dispatch`），确认端到端可用。

## 9. 后续工作（本次不做）

- goreleaser v0.155 → v2 迁移。
- Go 1.28 发布后（预计 2027 年 2 月），Go 1.26 将停止安全支持，需要人工升级 go.mod、CI 和 Dockerfile 的 Go 次版本。
- 评估 Go 模块路径改名。
