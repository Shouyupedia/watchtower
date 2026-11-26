# 自动维护设计（全依赖自动升级、受控自动合并、定期发布、去除上游残留）

日期：2026-11-17（修订：2026-11-25）
状态：已修订，待批准

## 1. 意图

这个仓库是 containrrr/watchtower 的个人 fork，由一个人维护。维护者希望仓库能自己保持健康，同时把人工精力只留给真正高风险或非自动化的改动：

- Go 工具链、Go 业务/工具模块、Docker 基础镜像、Python 文档依赖与 GitHub Actions 全部自动跟踪最新稳定版并自动提 PR。
- 符合低风险白名单与冷却期的自动升级 PR 在测试通过后自动合并；**其他普通 PR 默认需要维护者审批，仓库管理员按方法 B 保留仅限 PR 的审批绕过权（平台层面该绕过对任何 PR 有效，"仅用于自提 PR"属于维护者操作约定，且所有合并均受零豁免检查规则集约束）**。
- 定期自动重建并发布，让二进制和镜像拿到最新的依赖、Go 工具链和根证书。
- 本次一并将老旧的 GoReleaser v0.155.0 迁移到 v2 并纳入自动升级，去掉指向上游 containrrr 的残留配置、链接和文件。
- 整条自动化链路要防止供应链投毒与权限绕过。

### 为什么"防投毒"是硬约束

watchtower 镜像运行时挂载 `docker.sock`，权限等同宿主机 root。而且 watchtower 默认会自动更新自己，用户普遍跑 `:latest`。一个被投毒的依赖或恶意 PR 如果未经把关就被合并、发布，所有运行该镜像的主机都会在下一个轮询周期里自行拉取它。所以"全自动"的每一环都必须有硬闸门；任何一环拿不准时，结果必须停在"不自动合并（转人工审核）/ 不发布"一侧。

## 2. 目标、非目标与反目标

**目标**

1. 全生态自动追新：
   - Dependabot 每周检查 Go 模块（主模块与 `/tools` 工具模块，含迁移到 v2 后的 GoReleaser、`staticcheck`、`govulncheck`）、Docker 基础镜像、Python 文档依赖（`docs-requirements.txt`）与 GitHub Actions。
   - 仓库内置定时工作流 `go-toolchain-sync.yml` 每周检查官方最新稳定版 Go 工具链，同步更新精确版本文件 `.go-version` 与 `Dockerfile*` 中的 `golang` 基础镜像标签及 `@sha256:` digest（跨次版本升级时同步更新 `go.mod` 的 `go` 指令）。
2. 受控自动合并与强制人工审核：
   - Go 模块补丁版/次版本升级、Python 文档依赖补丁版/次版本升级、Docker 基础镜像纯 digest 升级，以及 Go 工具链同次版本内的补丁升级，在满足冷却期、CI 全绿且通过 `main` 分支独立校验后自动合并。
   - **除上述合格自动 PR 外的普通 PR 默认需要维护者本人（CODEOWNER）审批；仓库管理员按方法 B 保留仅限 PR 的审批绕过权（仅对审阅后的自提 PR 使用属操作约定，非管理员渠道无法绕过审批，且无人可绕过状态检查）。**
3. 本次一次性将 `goreleaser.yml` 从 `v0.155.0` 迁移至 GoReleaser v2，用 snapshot 构建核对目标平台、压缩包命名与校验和契约一致后，纳入 `tools/go.mod` 自动维护。
4. 每周自动检查上次发布是否完整；若上次发布已完整，再检查 `main` 自上次发版以来是否有影响发布产物的新提交。如果有，先在该固定提交上重跑完整检查，通过后再打下一个补丁版本 tag，并发布 GitHub Release 二进制与 GHCR 多架构镜像。
5. 所有发布产物附带可校验的构建来源证明（build provenance attestation）及镜像 SBOM。
6. 清除上游残留与已知构建隐患，并将 `.github/CODEOWNERS` 收归维护者本人。

**非目标**

- 不改 Go 模块路径 `github.com/containrrr/watchtower`。改它要动全部 Go 文件，用户感知不到，适合以后单独做。
- 不改容器标签 `com.centurylinklabs.watchtower*`。这是运行时协议，改了会让现有部署失效。
- 不改测试夹具（`pkg/container/mocks/data/*.json`）里出现的上游镜像名。它们只是测试数据。
- 不改 mkdocs 主题里的配色方案名 `containrrr`。这是 CSS 内部标识符，用户看不到。

**反目标**（即使"能用"也算失败的情况）

- 自动合并在缺少某项必需检查结果时仍然合并。
- 自动合并了一个大版本升级、跨次版本 Go 升级、安全更新、未满冷却期的依赖（含新增传递依赖）、非白名单文件/指令改动，或一个 GitHub Actions 升级。
- 非自动白名单内的非管理员 PR（外部贡献者 PR、未获豁免的机器人 PR、无 admin 权限 token 发起的 PR）在未经维护者审核批准的情况下被合入 `main`。
- 合并的提交组合不是基于最新 `main` 且经过完整测试的那个提交组合。
- 已发布的版本号被不同内容覆盖，或 `:latest` 被指回旧版本。同一版本号必须永远对应同一份产物。
- 有写权限、合并权限或发布权限的任务里运行了来自 PR 的不可信代码，或信任了 PR 工作流产出的构建产物/元数据。

## 3. 约束

- **仓库设置与私有 GitHub App 需由维护者创建配置。** 本设计不假设会话工具可以直接修改 GitHub 远端配置。2026-11-24 已通过 `gh api` 只读核实远端现状，需要维护者手动完成的创建与配置步骤列在第 8 节。
- **fork 仓库上 Dependabot 默认关闭。** 2026-11-24 基线通过 `gh api` 实测：`dependabot_security_updates` 与 `dependabot/alerts` 均为关闭，`allow_auto_merge` 为 `false`，`rulesets` 为空，`has_issues: false`（2026-11-25 M1/M2 已完成，详见 `d3484b7` 与 §7：`dependabot_security_updates`、`vulnerability-alerts` 及 `main-checks`/`main-review` Ruleset 已启用，`allow_auto_merge: false` 与 `has_issues: false` 保持不变）。
- **公开仓库的定时工作流会在 60 天无活动后自动停用。** `gh api` 实测 `CodeQL` 定时任务已在每周正常运行（最近一次 2026-11-19），但这仅证明 `CodeQL` 本身处于启用状态；新建的 `go-toolchain-sync.yml` 与 `scheduled-release.yml` 合入后仍需单独确认处于 `active` 状态，且所有定时工作流都受 60 天无活动自动禁用限制。
- **Dependabot 的能力边界与特殊限制：**
  - `pull_request` 事件下读不到 Actions secrets，`CODECOV_TOKEN` 为空，覆盖率上传要能容忍失败。
  - **Dependabot 不会主动升级 `go.mod` 中的 Go 工具链版本**（上游 `dependabot-core#13520` 尚未实现），需通过独立定时工作流跟踪官方稳定版。
  - 冷却期（`cooldown`）支持范围有限：`gomod` 支持按 semver 级别设天数；`github-actions`、`docker`、`pip` 只支持 `default-days`。冷却期只对 version updates 生效，**不对 security updates 生效**；Dependabot 自身的 Docker 冷却期目前仅依赖 Docker Hub 的 `tag_last_pushed`，无可用日期时会 fail-open（因此由 §5.1 的独立核验做 fail-closed 兜底）；`gomod` 更新直接依赖时连带引入的新传递依赖也不受直接依赖 `update-type` 的约束。
- **可复用工作流的权限静态校验与上下文归属：**
  - 嵌套任务若请求超出调用方授权的权限，整个工作流在启动阶段即报无效失败（`if` 跳过也无效），因此只读验证与可信发布必须拆分为独立工作流。
  - 可复用工作流的 `github` 上下文属于调用方（如定时发布调用 `release.yml` 时 `github.ref_name` 是 `main`），被调用方必须显式接收并校验 `tag` 与 `sha` 输入。
- **`GITHUB_TOKEN` 与 GitHub App 令牌的事件触发差异：**
  - 用内置 `GITHUB_TOKEN` 推送的提交和 tag **不会**触发 `push` / `pull_request` 工作流。`scheduled-release.yml` 用 `GITHUB_TOKEN` 推送版本 tag 不会触发 `release.yml` 的 tag `push` 事件，随后由工作流显式 `workflow_call` 调用一次 `release.yml`，天然避免重复触发。
  - 用**专用 GitHub App 安装令牌**创建/更新分支 PR 或执行合并 **会正常触发** `pull_request`、`push` 与 `CodeQL` 工作流。因此更新 App 提的 Go 升级 PR 会自动运行 PR CI，合并 App 合入 `main` 后也会自动触发 `push-main.yml` 更新 `:latest-dev` 镜像。
- **GitHub Rulesets 叠加、绕过与自审限制：**
  - 多条规则集针对同一分支时**叠加生效、取最严限制**；某身份在规则集 X 的 Bypass 列表上仅能跳过规则集 X，**无法跳过 Bypass 列表为空的规则集 Y**。
  - 当规则集要求 `Required approvals >= 1` 时，GitHub 原生 `gh pr merge --auto`（GraphQL `enablePullRequestAutoMerge`）与普通 `gh pr merge`（GraphQL `mergePullRequest`）不会应用 GitHub App 的 PR 级绕过，会报分支策略禁止或无限等待人工审批；调用 REST 合并接口 `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`（携带预期 `sha`）则会由服务端按令牌身份正确执行规则集绕过判定。
  - GitHub 不允许 PR 作者 Approve 自己的 PR。采用**方法 B**（在审批规则集中同时给 `Repository admin` 配置 `For pull requests only` 绕过）时，**该管理员绕过在 GitHub 平台层面适用于任何 PR，且无法由平台限定为"仅限自提 PR"或"仅限网页端手动点击"**；任何具备本仓库管理员权限的 token（PAT / CLI / OAuth）均拥有相同的绕过审批能力（但仍受零豁免检查规则集约束）。仅在自提 PR 审阅后使用该绕过属于维护者操作约定。
- **镜像构建不需要 QEMU。** 现有 Dockerfile 用 `--platform=$BUILDPLATFORM` 交叉编译，最终阶段是 `scratch`，只做 COPY。

## 4. 方案比较（如何同时做到"合格自动 PR 自动合"与"其他普通 PR 默认需本人审"）

| 方案 | 做法 | 优点 | 缺点 |
| --- | --- | --- | --- |
| A（PR 工作流内直合，无强制审批规则） | `pull-request.yml` 末尾合入，规则集不设 `Required approvals` | 配置少 | `eligible=false` 只能让机器人自己停手，平台不拦截其他写权限渠道；且同仓分支 PR 若能触碰工作流，合并权限离不可信上下文太近 |
| B（单条规则集开启审批 + 豁免 App） | 单一规则集同时管检查和审批，把合并 App 加入 Bypass | 只有一条规则集 | **单条规则集的 Bypass 会跳过该规则集内的全部规则**，合并 App 一旦绕过审批就会连状态检查和"分支必须最新"一起绕过 |
| **双规则集叠加 + `main` 侧 `workflow_run` 专用 App 合并（选定）** | `main-checks`（零豁免）管必需检查与分支最新；`main-review` 管默认 CODEOWNER 审批，仅给专用合并 App 与管理员（方法 B）仅限 PR 的绕过权；`pull-request.yml` 纯只读，由 `main` 上的 `automerge.yml` 独立核验后调用 REST 合并接口 | `main-checks` 零豁免保证任何人都跳不过最新组合的完整 CI；`main-review` 使所有其他普通 PR 默认需要维护者审批，管理员保留仅限 PR 的审批绕过权；合并逻辑与凭据完全隔离在 `main` 上 | 需要创建 2 个仅装本仓的私有 GitHub App 与 2 套规则集；方法 B 下"仅对自提 PR 使用管理员绕过"需靠维护者约定与凭据管控保证 |

**选定方案的核心机制**：

1. **规则集拆分为两层叠加**：
   - **`main-checks`（质量硬底线，Bypass 列表完全留空 = 零豁免）**：要求所有改动必须走 PR（`Required approvals = 0`）、禁止强推和删分支、启用 **Require status checks to pass** 与 **Require branches to be up to date before merging**。无论是合并 App 还是仓库管理员，都绝无可能跳过 CI 全绿或合入落后的分支组合。
   - **`main-review`（强制维护者审核）**：配合 `.github/CODEOWNERS`（`* @Shouyupedia`），启用 **`Required approvals = 1`**、**`Require review from Code Owners`**、**`Dismiss stale pull request approvals when new commits are pushed`** 与 **`Require approval of the most recent reviewable push`**。Bypass 列表仅授予**专用合并 App（`watchtower-automerge`）**与 **`Repository admin`**（方法 B），模式均为 **`For pull requests only`**。
2. **PR 工作流纯只读，合并决策与凭据固定在 `main`**：
   - `pull-request.yml` 仅声明 `contents: read`，只跑验证，不持有任何写权限或 App 凭据。
   - `automerge.yml` 驻留在 `main` 分支，由 `workflow_run` 在 `Pull Request` 或 `CodeQL` 工作流完成时触发；它**不检出 PR 代码、不下载或信任任何 PR artifact**，而是直接查询 GitHub API 与上游注册表做独立核验，全部通过后才在仅限 `main` 访问的 `automerge` Environment 中签发合并 App 令牌，调用 `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`（携带 `"sha": "<head_sha>"` 与 `"merge_method": "merge"`，对齐 M2 生效的 `main-checks` `allowed_merge_methods: ["merge"]`）执行单次合并；HTTP 405/409 视为预期非重试结果正常退出。
3. **方法 B 的准确安全边界**：
   - 外部贡献者 PR、未进白名单的 Dependabot PR（Actions、大版本、安全更新）与跨次版本 Go 升级 PR，`automerge.yml` 均判定 `eligible=false` 不予合并，默认停在 `main-review` 等待维护者本人点 Approve。
   - `Repository admin` 在 `main-review` 上的 `For pull requests only` 绕过权在 GitHub 平台层面**对所有 PR 均有效**（GitHub 无法将其限制为仅自提 PR 或仅网页端操作）；维护者约定仅在本人自提 PR（因 GitHub 禁止自审）经审阅且 `main-checks` 全绿后使用该绕过，同时**绝不将带有仓库 admin 权限的个人 token 交给外部工具或自动化 Agent**（管理员 token 同样拥有绕过审批的能力）。

## 5. 设计

### 5.1 防投毒与权限隔离的分层措施

1. **两个最小权限私有 GitHub App 与 Environment 隔离。**
   - **`watchtower-updater`（更新专用 App）**：仅安装在本仓库，权限仅开 `Contents: Read & write` 与 `Pull requests: Read & write`，**不加入任何规则集的 Bypass 列表**（无法直接推 `main`，也无法绕过审批或检查）。App ID 与私钥存放在仅允许 `main` 分支部署的 `updater` Environment 中，仅供 `go-toolchain-sync.yml` 通过 GraphQL `createCommitOnBranch`（由 GitHub 自动签名，生成的提交 `verification.verified == true`）创建/更新 Go 升级分支和 PR。
   - **`watchtower-automerge`（合并专用 App）**：仅安装在本仓库，权限仅开 `Contents: Read & write` 与 `Pull requests: Read & write`，仅在 `main-review` 规则集中拥有 `For pull requests only` 绕过权限。App ID 与私钥存放在仅允许 `main` 分支部署的 `automerge` Environment 中，仅供 `automerge.yml` 的合并任务使用。
   - 仓库保持 `default_workflow_permissions: "read"` 与 `can_approve_pull_request_reviews: false`。
2. **延迟入场（冷却期）与正向公开可见时间核验。**
   - 配置层冷却期：`gomod`（`/` 与 `/tools`）补丁版 3 天、次版本 7 天、大版本 30 天；`docker` 7 天；`pip` 7 天；`github-actions` 7 天；`go-toolchain-sync.yml` 7 天。
   - **Dependabot security updates 全部转人工，永不自动合并**（安全更新不受冷却期保护）。
   - **合并前正向核验可信来源的"首次公开可见时间"（不依赖 PR 正文缺少告警，也不把可伪造的提交时间当发布时间）**：
     - `go_modules`：`proxy.golang.org` 的 `.info` 接口返回的 `Time` 是上游 VCS 提交时间（可被回填伪造），不能单独证明公开发布时长，因此只作为前向扫描起点提示（`since = Time - 60s`）。校验任务在固定 10 次请求预算内（1 次 `.info` + 最多 9 页 `index.golang.org/index`、每页上限 2000 条）定位精确 `(Path, Version)` 的首次入库 `Timestamp`，核实补丁版已满 3 天、次版本及连带新增传递依赖已满 7 天；实测样本中约 6/12 个 Go 模块命中，超出预算或缺少可信首次可见时间记录、或未满天数时一律 fail-closed 输出 `eligible=false` 转人工审核。
     - `docker`：向 Docker Hub API 正向查询目标镜像标签及 normalized digest 对应的注册表推送时间 `tag_last_pushed`，核实已满 7 天；查不到、digest 不一致或未满即 fail-closed 输出 `eligible=false`。
     - `pip`：向 PyPI JSON API（`https://pypi.org/pypi/<pkg>/<version>/json`）查询锁文件中该包该版本**所有允许的 `--hash=sha256:...`** 对应文件的注册表上传时间（`upload_time_iso_8601`），取全部允许文件里的**最晚**上传时间核实已满 7 天；任一允许 hash 找不到对应文件、缺少时间戳或未满 7 天即 fail-closed 输出 `eligible=false`。
     - Go 工具链同步：同样不依赖 `.info` 的提交时间，`go_toolchain_sync.py` 与 `automerge_verify.py` 均在 10 次请求预算内查询 `index.golang.org` 中 `golang.org/toolchain` 对应条目的入库 `Timestamp`（未实现“更新流程首次观察起算”回退），且核实 Docker Hub `golang:<version>-alpine` 对应 digest 的 `tag_last_pushed` 已满 7 天。PR 4 实测中 `golang.org/toolchain` 样本在 10 次请求预算内未命中；按 fail-closed 原则，凡在预算内未命中的版本均会阻止该次 `go-toolchain-sync.yml` 创建/更新工具链 PR（`needs_sync=False`），同时 `automerge.yml` 亦对该版本返回 `eligible=false`。
3. **自动合并白名单（`main` 侧独立 API 核验、fail-closed）。**
   - `automerge.yml` 的只读任务 `verify` 直接调用 GitHub API 独立获取数据，**任何字段缺失、状态未就绪、API 异常或超出白名单的情况一律正常结束并输出 `eligible=false`**（留给人工审批）：
     - **`workflow_run` 事件过滤、PR 绑定与双 SHA 区分**：由于 `CodeQL` 同时也会被 `schedule` 和 `push` 触发，`verify` 任务首先要求 `workflow_run.event == "pull_request"` 且 `workflow_run.head_repository.full_name == 本仓库`；随后通过 GitHub API 查询本仓库当前 `open`、`draft == false`、`base.ref == "main"`、`head.repo.full_name == 本仓库` 的唯一目标 PR，确认实时 `pr.head.sha == workflow_run.head_sha`。**严格区分两类 SHA**：① **PR `head_sha`**（分支顶端提交，用于核对提交签名链、验证工作流 `head_sha` 归属，并作为预期 `sha` 传给 REST 合并接口）；② **CI 临时合并提交 SHA**（`refs/pull/<n>/merge`，`pull_request` 工作流默认检出测试的合并预览提交；配合零豁免 `main-checks` 的 `Require branches to be up to date before merging`，确保 `main` 若在测试后前进，旧的临时合并结果失效并拒绝合入）。
     - **必需检查全绿**：查询本仓库关联到该 PR 同一个 `head_sha` 的 `Pull Request` 工作流与 `CodeQL`（`event == "pull_request"`）工作流运行记录，确认两者均已完成且结论为 `success`，同时查询 `head_sha` 上的必需 check-runs 状态全部通过（若另一工作流仍在运行则本次静默退出，待最后完成的工作流触发时再合入）。
     - **可信机器人身份绑定**：`pr.user.login` 仅允许 `dependabot[bot]` 或 `watchtower-updater[bot]`，**绝不凭分支名、PR 标签或通用 `github-actions[bot]` 放行**。
     - **完整提交列表与签名校验**：分页拉取 PR 的全部提交并核对条数等于 `pr.commits`；要求每一个提交都满足 `commit.verification.verified == true`、`author.login` 等于该 PR 作者机器人，且 `committer.login` 仅限 `web-flow` 或该机器人本身。**禁止调用 `update-branch` 向自动 PR 注入合并提交**（否则会引入非机器人提交破坏签名链）。
     - **拒绝安全更新（含校准边界）**：核查 PR 不关联 Dependabot 安全告警，也不带 security 相关标签或安全更新元数据，且必须命中经过真实样本校准的普通更新正向信号。4.1a 真实样本回放表明，两类更新均能命中 `dependabot-body-v1`。在只读校验器能将普通更新的来源绑定到具体 PR 和提交前，`CALIBRATED_NORMAL_UPDATE_SIGNALS` 保持为空，Dependabot PR 转人工审核。
     - **敏感路径硬禁区**：PR 修改的文件列表中绝对禁止出现 `.github/workflows/**`、`.github/actions/**`、`.github/scripts/**`、`.github/CODEOWNERS`、`.github/dependabot.yml`（及任意路径下的 `CODEOWNERS`、`dependabot.yml`、`dependabot.yaml`）。
     - **按机器人与生态执行精确文件及行级 diff 白名单**：
       - **当作者为 `dependabot[bot]` 时**，从 `head.ref` 前缀解析生态，仅允许 `go_modules`、`docker`、`pip`（`github_actions` 永远返回 `eligible=false`）：
         - `go_modules`：改动文件仅限 `go.mod`/`go.sum` 或 `tools/go.mod`/`tools/go.sum`；解析新旧 `go.mod`，**禁止新增、删除或修改 `go`、`toolchain`、`replace`、`exclude`、`retract`、`tool` 指令**，仅允许 `require` 条目的补丁版或次版本升级（拒绝任何主版本变更），且所有变更/新增模块通过可信首次公开可见时间冷却期核验。
         - `docker`：改动文件仅限 `dockerfiles/Dockerfile*`；仅允许既有 `FROM` 行的 `@sha256:...` digest 变化，镜像名与标签保持一字不差，不允许改动其他行，且通过 Docker Hub 7 天冷却期核验。
         - `pip`：改动文件仅限 `docs-requirements.txt`；仅允许既有/传递包的补丁版或次版本升级，每个包条目必须附带 `--hash=sha256:...`，不允许引入额外安装源参数（如 `--index-url`、`--extra-index-url`、git URL），且通过 PyPI 7 天冷却期核验。
       - **当作者为 `watchtower-updater[bot]` 时**（Go 工具链同步 PR）：
         - **同次版本补丁升级（允许自动合并）**：改动文件**仅限** `.go-version` 与 `dockerfiles/Dockerfile*`（`go.mod` 不得变化）；对比 `.go-version` 新旧值，主版本与次版本必须完全相同且补丁号递增（如 `1.26.a → 1.26.b`）；`Dockerfile*` 中仅允许 `FROM` 行同步更新为 `golang:<new_ver>-alpine@sha256:<digest>`；且工具链与镜像均满 7 天冷却期。
         - **跨次版本升级（自动转人工审核）**：如 `1.26.x → 1.27.y`（会同时改动 `.go-version`、`go.mod` / `tools/go.mod` 的 `go` 指令与 `Dockerfile*`），在此处自动判定 `eligible=false`，由 `main-review` 拦下交维护者审核兼容性后合并。
4. **全部钉死、独立 Go 版本文件与 GoReleaser v2 迁移。**
   - 所有工作流的 `uses:` 钉到完整 commit SHA 并附版本注释，由 Dependabot 维护（人工审核合并）。
   - **新增 `.go-version` 作为精确 Go 补丁版本的唯一来源**：所有工作流中的 `actions/setup-go` 统一使用 `go-version-file: .go-version`（不再写浮动的 `1.26.x`，也无需在升级 Go 时改动 `.github/workflows/**`）。`go.mod` 与 `tools/go.mod` 的 `go` 指令代表模块要求的**最低 Go 语言版本**，日常补丁升级保持不变，仅在跨次版本升级 PR 中由 `go-toolchain-sync.yml` 同步调整并交维护者审核。
   - 三个 Dockerfile 的 `golang:<ver>-alpine` 全部钉 `@sha256:` digest。
   - **迁移 `goreleaser.yml` 至 GoReleaser v2 并纳入 `tools/go.mod`**：本次将 `goreleaser.yml` 升级为 v2 配置语法，并用 snapshot 构建严格核对输出平台（共 8 个归档：`linux` 的 `amd64`/`386`/`arm`(v6)/`arm64`、`windows` 的 `amd64`/`386`、`darwin` 的 `amd64`/`arm64`；排除 `windows/arm` 与 `windows/arm64`）、归档格式与架构别名（`armhf`、仅 Linux 使用的 `arm64v8`、Windows `.zip`、macOS `.tar.gz`、附带 `LICENSE.md`）、`ldflags` 版本号注入，以及发布资产契约（`v1.0.0` 的 6 个归档名不变，另加 2 个 `darwin` 归档）一致。随后将 `github.com/goreleaser/goreleaser/v2`、`github.com/rhysd/actionlint/cmd/actionlint`、`staticcheck`、`govulncheck` 全部作为 `tool` 依赖写入独立模块 `tools/go.mod`，由 `tools/go.sum` 和 Go checksum database 校验并由 Dependabot 自动升级（移除写死 `v0.155.0` 的 `goreleaser/goreleaser-action`）。
   - `docs-requirements.txt` 锁定全部直接与传递依赖的精确版本和 `--hash=sha256:...`（启用 `pip install --require-hashes`），文档工作流的 Python 版本钉死。
5. **合并前检查（零豁免硬闸门 `main-checks`）。** 所有合入 `main` 的 PR 都必须通过下列必需检查：
   - `staticcheck`（lint，通过 `go tool -modfile=tools/go.mod staticcheck ./...` 运行）
   - `go test` 三平台矩阵（Linux / macOS / Windows）
   - `go mod verify` 与 `go mod tidy -diff`（同时检查根目录模块与 `tools/go.mod`）
   - `version-consistency`：校验 `.go-version`、三个 `Dockerfile*` 与 `go.mod` / `tools/go.mod` 的 Go 版本一致性
   - `govulncheck`：已知漏洞检查（`scripts/run-govulncheck.sh` 按 `.go-version` 钉死工具链并以 `go build -modfile=tools/go.mod` 为宿主机构建一次扫描器后覆盖各发布目标平台）。**基线与豁免规则**：实现时先在 `main` 上运行 `govulncheck` 并升级清理当前可修复的基线漏洞（如旧版 `golang.org/x/net`）；对上游尚无修复或临时分属不同 PR 的漏洞，在仓库中维护带明确原因和到期日的限时豁免清单，再开启 PR 硬阻断。
   - `script-tests`：运行 `scripts/` 与 `.github/scripts/` 中的 Python 标准库单元测试（覆盖自动合并与工具链同步判定逻辑）
   - GoReleaser v2 snapshot 构建（`go tool -modfile=tools/go.mod goreleaser release --snapshot --clean`）
   - Docker 多架构只读构建（不推送、不请求写权限）
   - **文档只读严格构建**（`pip install --require-hashes -r docs-requirements.txt` + `scripts/build-tplprev.sh` + `mkdocs build --strict`，确保 `docs-requirements.txt` 自动升级与文档链接改动在合并前得到完整验证）
   - `actions/dependency-review-action`：PR 新引入或升级到的依赖如有已知漏洞即阻断
   - `workflow-lint`：`actionlint`（`go tool -modfile=tools/go.mod actionlint`）+ `zizmor`（固定版本与 hash 锁在 `.github/zizmor-requirements.txt`）+ `check-jsonschema` 校验 `dependabot.yml`，发现危险写法或配置错误即阻断
   - `CodeQL`：代码静态安全分析
6. **最小权限与缓存隔离。**
   - 每个工作流顶层 `permissions: {}`，各任务只声明自身最小权限；调用可复用工作流时按需精确授权。
   - `actions/checkout` 一律 `persist-credentials: false`。
   - 删除所有 `pull_request_target` 工作流（`greetings.yml`、`dependabot-approve.yml`）。
   - **只读验证与写操作彻底拆分**：`pull-request.yml` 与 `ci.yml` 全程仅 `contents: read`；`automerge.yml` 拆分为无凭据只读 `verify` 任务与绑定 `automerge` Environment 的 `merge` 任务，全程不检出 PR 代码；镜像推送独立为 `docker-publish.yml`；删除 `publish-docs.yml`（不发布 GitHub Pages）。
   - **发布任务禁用缓存**：正式发布（`release.yml`、`docker-publish.yml`）显式关闭 Go 缓存与 Buildx GHA 缓存，防止缓存投毒影响正式产物。
7. **可校验的产物。** 发布的二进制压缩包、`checksums.txt` 和 GHCR 镜像都用 `actions/attest-build-provenance` 生成 GitHub 签名的 SLSA 构建来源证明，镜像证明推送到仓库并附 SBOM。`id-token: write` 和 `attestations: write` 仅授予发布任务。README 写明 `gh attestation verify` 校验命令。
8. **删除仓库根目录的 `oryxBuildBinary`。** 这是一个 20MB、未剥离的可执行文件，在上游提交 `650acde`（PR **#1754**，`feat: add a label take precedence argument`）里被误提交，来源无法验证。删除并在 `.gitignore` 里屏蔽它。

### 5.2 工作流结构

| 文件 | 触发 | 权限、环境与内容 |
| --- | --- | --- |
| `.github/actions/docker-build`（新，内部 composite action） | 被工作流复用 | 封装统一的 Dockerfile 路径、目标架构列表、`WATCHTOWER_VERSION` 与 OCI 标签参数，供只读验证与可信发布共享，不携带权限声明。 |
| `ci.yml`（新，可复用） | `workflow_call`（输入 `ref`、`upload_coverage`） | 仅需 `contents: read`。包含 `staticcheck`、三平台 `go test`、根目录与 `/tools` 的 `go mod verify` / `go mod tidy -diff`、`version-consistency`、`govulncheck`（`scripts/run-govulncheck.sh` 通过 `go build -modfile=tools/go.mod` 扫描各发布目标）、`script-tests`（`scripts/` 与 `.github/scripts/` 单元测试）、GoReleaser v2 snapshot、Docker 只读多架构构建、文档只读严格构建（`mkdocs build --strict`）、`workflow-lint`（`actionlint` + `zizmor` + `dependabot.yml` schema 校验）。确保 PR、`main` 推送与发版前共用唯一质量门槛。 |
| `pull-request.yml`（改） | `pull_request` → `main` | 顶层 `permissions: {}`，**全程只读（`contents: read`）**。调用 `ci.yml` 与 `dependency-review`，不含任何合并或写权限任务。按 PR 设 `concurrency`，新提交取消旧运行。 |
| `automerge.yml`（新） | `workflow_run`（监听 `["Pull Request", "CodeQL"]`，`types: [completed]`） | 固定取自 `main`，仅处理本仓库 `event == "pull_request"` 的运行（忽略 `CodeQL` 的 `push`/`schedule` 运行），按目标 PR 设串行 `concurrency`。<br>1. **`verify`（只读：`contents: read`、`pull-requests: read`、`checks: read`、`actions: read`）**：检出 `main` 上的 `.github/scripts/automerge_verify.py`（Python 标准库，不检出 PR 代码、不读取 PR artifact）；独立通过 API 解析本仓库唯一关联的开放目标 PR，区分 PR `head_sha` 与 CI 测试的临时合并 SHA，核实实时 `pr.head.sha == head_sha`、本仓 `Pull Request` 与 `CodeQL`（`pull_request` 事件）在该 `head_sha` 上均已 `success`、允许的机器人身份（`dependabot[bot]` 或 `watchtower-updater[bot]`）、完整已签名提交列表、非安全更新（4.1a 已排除正文信号，当前 Dependabot PR 转人工）、敏感路径禁区（含 `.github/scripts/**`）、精确 diff 白名单与可信首次公开可见时间冷却期；任一未满足均正常结束并输出 `eligible=false`。<br>2. **`merge`（`needs: verify`，`if: needs.verify.outputs.eligible == 'true'`，`environment: automerge`）**：签发 `watchtower-automerge` App 短效令牌，执行单次带预期 `sha` 保护的 REST 请求 `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`（`sha="$HEAD_SHA"`、`merge_method=merge`，对齐 M2 已生效的 `main-checks` `allowed_merge_methods: ["merge"]`）。不执行 `update-branch` 且不重试；HTTP 405（当前不可合并，如分支落后于 `main` 被 `main-checks` 拒绝）或 HTTP 409（`head_sha` 已变更）视为预期结果正常退出，交由 Dependabot 自动 rebase 或 `go-toolchain-sync.yml` 重建分支后重跑 CI 再合。 |
| `go-toolchain-sync.yml`（新） | 每周一 02:17 UTC；`push` → `main`；`workflow_dispatch` | 1. **检查任务（只读）**：运行 `.github/scripts/go_toolchain_sync.py check`，查询 `go.dev/dl/?mode=json` 最新稳定版、`index.golang.org` 中 `golang.org/toolchain` 的首次入库 `Timestamp`（10 次请求预算，fail-closed，未实现“首次观察起算”回退；预算内未命中时会阻止该版本的同步尝试，不创建/更新 PR）以及 Docker Hub `golang:<ver>-alpine` 最新 manifest digest 的 `tag_last_pushed`；仅采用同时满 7 天冷却期的目标版本。若与当前 `main` 一致且无待 rebase 的开放同步 PR，直接结束（`push` → `main` 触发时，仅在存在落后的开放 Go 同步分支时继续重放单提交 rebase）。<br>2. **提 PR 任务（`environment: updater`）**：签发 `watchtower-updater` App 短效令牌。同次版本补丁升级仅更新 `.go-version` 与 `dockerfiles/Dockerfile*` 的标签及 digest；跨次版本升级同步更新 `.go-version`、`go.mod` / `tools/go.mod` 的 `go` 指令（含必要的 `go mod tidy`）与 `Dockerfile*` 标签及 digest。通过 GraphQL `createCommitOnBranch` 生成 GitHub 自动签名的单提交并创建/更新 PR（触发 PR CI；补丁版走自动合并，跨次版本留待维护者审批）。 |
| `push-main.yml`（取代 `release-dev.yaml`） | `push` → `main`，`workflow_dispatch` | 调用 `ci.yml`；通过后调用 `docker-publish.yml` 推送 `:latest-dev`。（由于 `automerge.yml` 使用合并 App 令牌合入，自动合并后的提交也会自动触发本工作流更新 `:latest-dev`。） |
| `docker-publish.yml`（取代原 `docker.yml`，可复用） | 仅 `workflow_call`（输入 `ref`、`tag`、`push_latest`、`disable_cache`） | 权限：`contents: read`、`packages: write`、`id-token: write`、`attestations: write`。仅用于可信发布（`push-main.yml` 与 `release.yml`）。发布版本 tag 前先检查 GHCR 中目标 tag 是否已存在：绝不覆盖已有版本 tag；若已存在，需核对与预期 `(tag, sha, digest)` 记录一致才跳过，不一致或无法核实时立即失败（相同 OCI `revision` 标签不足以单独证明内容一致）。仅在串行发布锁内再次核实 `tag` 仍为 `main` 上最高正式版本，且版本镜像与证明就绪后，才推进 `:latest` 并生成证明。取消原 `docker.yml` 可覆写旧 tag 的手动入口。 |
| `release-tag.yml`（手动入口） | 推送正式版本 tag；在 `main` 上用 `workflow_dispatch` 重放已有 tag | 验证 tag 与提交，检查发布状态，未完成的草稿在固定 SHA 上通过完整 `ci.yml` 后进入共用发布锁。已完成的发布直接结束；正式 Release 已发布而 `:latest` 尚待推进时只运行镜像核验与推进阶段。 |
| `release.yml`（可复用） | `workflow_call`（输入 `tag`、`sha`、`mode`、`skip_ci`） | **入口工作流持有 `release-publish` 锁**。`release.yml` 与 `docker-publish.yml` 均不声明并发组。`skip_ci=false` 时先在指定 SHA 上调用 `ci.yml`。入口调用时已在锁外完成 CI，写入任务取锁后重新判定候选。GoReleaser v2 以无缓存方式生成版本化归档，发布脚本按草稿记录只补缺失文件与证明。全部版本产物及证明就绪后才发布草稿，再核验最高正式版本并推进 `:latest`。 |
| `scheduled-release.yml`（新） | 每周二 03:23 UTC；`workflow_dispatch` | 默认手动彩排以 `contents: read` 构建并校验 GoReleaser snapshot，同时构建多架构 Docker 镜像。正式运行从 `main` 固定 `head_sha`，通过 `.github/scripts/release_paths.py` 判断是否有影响发布的路径变更，或选取可恢复的发布；计划任务需要 `contents: write` 权限以读取草稿列表，实际只执行查询。对待发布候选在锁外完成 `ci.yml`，含 macOS；取锁后重新核对 `head_sha`、目标 tag 与恢复状态。随后 `release.yml` 以 `GITHUB_TOKEN` 建立带标记的附注 tag，由同一调用继续发布。 |
| `codeql-analysis.yml`（改） | 不变 | 顶层 `permissions: {}`，任务级 `actions: read`、`contents: read`、`security-events: write`，钉 SHA，删除已过时的 `git checkout HEAD^2` 步骤。 |
| 删除 | — | `greetings.yml`、`dependabot-approve.yml`、`release-dev.yaml`、`publish-docs.yml`（不发布 GitHub Pages）、原 `docker.yml` |

**发布语义**

- **先验证，再打 tag，最后发布**：新版本候选固定到具体 `head_sha`，完整 `ci.yml` 通过前绝不创建 tag，避免留下无产物的死 tag。
- **迁移边界与中断恢复**：历史版本（如现有 `v1.0.0`）早于新证明机制，绝不因缺少新格式证明而重建或重签。仅恢复由新流程记录的未完成发布，绑定 `tag`、`sha` 与原始产物摘要/校验和，只补缺失项、绝不覆盖已有产物，冲突即失败交由人工介入。
- **证明绑定源提交**：GitHub OIDC 证书的源提交取自调用方运行的 `GITHUB_SHA`。构建并签发归档或镜像证明前，要求该值等于预期提交。复用已有证明时，用 `gh attestation verify --source-digest <sha>` 校验源提交，同时校验签发工作流。
- **串行发布与 `:latest` 只进不退**：手动推 tag 与定时发布共用同一串行写入锁（`release-publish`）；在所有版本产物与证明就绪后，于锁内再次核实该 `tag` 仍为 `main` 上最高正式版本，最后才更新 `:latest`。
- **自动发布只发补丁版本**；次版本和大版本由维护者手动推 tag，走同一个 `release.yml`。
- **纯文档提交不触发发版**：只有影响二进制或镜像产物的路径变更才计入发版条件，避免文档修改导致所有运行 Watchtower 的宿主机无意义自更新；Go 工具链、依赖模块和基础镜像 digest 更新合入 `main` 后会自动纳入下一次发版。

### 5.3 自动升级配置（Dependabot + Go 工具链同步）

- `gomod`，目录 `/`（Dependabot）：每周一次；冷却期 3/7/30 天；次版本和补丁版归入一组（`go-minor-patch`），大版本单独开 PR。
- `gomod`，目录 `/tools`（Dependabot）：每周一次；冷却期 3/7/30 天；管理 `staticcheck`、`govulncheck` 与 `github.com/goreleaser/goreleaser/v2`；次版本和补丁版归入一组（`tools-minor-patch`），显式配置 `allow: [{ dependency-type: "all" }]`（覆盖 Go `tool` 依赖在 `go.mod` 中标为 `// indirect` 的情况）。
- `docker`，目录 `/dockerfiles`（Dependabot）：每周一次；冷却期 7 天；`golang` 镜像配置 `ignore` 忽略 `semver-major`、`semver-minor` 与 `semver-patch` 标签变更（仅负责当前标签下的基础镜像安全/根证书 `@sha256:` digest 刷新；标签版本升级统一交由 `go-toolchain-sync.yml` 与 `.go-version` 原子化同步）。
- `pip`，目录 `/`（Dependabot）：每周一次；冷却期 7 天；次版本和补丁版归入一组（`pip-minor-patch`），同步更新 `docs-requirements.txt` 中的版本号与 `--hash`。
- `github-actions`，目录 `/`（Dependabot）：每周一次；冷却期 7 天；全部归入一组（仅自动开 PR，由维护者人工审核合并）。
- Go 工具链与基础镜像标签同步（`go-toolchain-sync.yml`）：每周一次检查 `go.dev/dl/?mode=json` 最新稳定版及对应 `golang:<ver>-alpine` 镜像 digest（均需满 7 天冷却期）；同次版本补丁升级自动合并，跨次版本升级自动开 PR 交维护者审核。

### 5.4 上游残留清理、配置迁移与脚本修复

| 对象 | 处理 |
| --- | --- |
| `.github/CODEOWNERS` | **保留并重写为 `* @Shouyupedia`**（移除所有上游维护者账号，作为 `main-review` 规则集强制维护者本人审批的依据） |
| `.go-version` | 新增，记录单一锁定的精确 Go 稳定版号，供所有工作流 `actions/setup-go` 读取并由 `go-toolchain-sync.yml` 自动维护 |
| `goreleaser.yml` 与 `tools/go.mod` | 将 `goreleaser.yml` 从 `v0.155.0` 迁移至 GoReleaser v2 配置语法并验证产物契约一致；新建 `tools/go.mod` 统一管理 `goreleaser/v2`、`actionlint`、`staticcheck`、`govulncheck` |
| `.gitattributes` | 新增最小范围行尾约束（仅 `*.sh`、`go.mod`、`go.sum`、`*.yml`、`Dockerfile*` 强制 `eol=lf`，避免 Windows CRLF 干扰 `go mod tidy -diff` 与 Linux shell 脚本） |
| `.github/scripts/` | 新增自动合并核验、冷却期查询与 Go 工具链同步 Python 标准库脚本及离线单元测试（纳入 `CI / script-tests` 与敏感路径硬禁区） |
| `oryxBuildBinary` | 删除（上游 #1754 误提交），并加入 `.gitignore` |
| `scripts/codecov.sh` | 删除（包含 `bash <(curl -s https://codecov.io/bash)` 远程脚本执行，已废弃） |
| `scripts/build-tplprev.sh` | 将 `misc/wasm/wasm_exec.js` 修正为 Go 1.24+ 路径 `lib/wasm/wasm_exec.js`，并增加 `set -euo pipefail` |
| `docs-requirements.txt` | 锁定全部直接与传递依赖的精确版本及 `--hash=sha256:...`，供 `pip install --require-hashes` 使用并纳入 Dependabot `pip` 维护 |
| `.github/stale.yml`（Probot）、`.devbots/`、`.codacy.yml`、`.all-contributorsrc` | 删除。对应的第三方 App 没有安装，配置不生效。README 里保留对上游的致谢 |
| `greetings.yml`、`dependabot-approve.yml`、`publish-docs.yml` | 删除（不发布 GitHub Pages） |
| Issue 模板 | 镜像名改为 `ghcr.io/shouyupedia/watchtower:latest`；去掉"不支持其他 fork"的说法；联系链接改为本仓库（注：当前远端 `has_issues: false`，详见第 8 节） |
| PR 模板 | 去掉面向社区贡献者的措辞，保留检查项 |
| `SECURITY.md` | 上游维护者的邮箱改为本仓库的 GitHub 私密漏洞报告入口 |
| `code_of_conduct.md` | 不再引用 Containrrr 社区准则，改为引用 Contributor Covenant 2.1 |
| `CONTRIBUTING.md` | 示例镜像名改为本仓库；更正 `Dockerfile.self-contained` 的说明（它现在构建本地源码） |
| `mkdocs.yml` | 移除 `site_url`（不发布 GitHub Pages），`repo_url`、`user` 指向本仓库；`exclude_docs` 排除 `superpowers/` |
| `docs/*.md` | 用户可见的镜像名 `containrrr/watchtower` 改为 `ghcr.io/shouyupedia/watchtower`；`docs/updating.md` 中的 "Docker Hub" 同步改为 GHCR；指向 `containrrr.dev/watchtower` 和 `containrrr.github.io/watchtower` 的站内链接改为相对链接；`index.md` 里上游的 CI/Codecov/GoDoc/Docker Hub 徽章删除或改为本仓库。指向 shoutrrr（真实依赖）的链接保留 |
| `scripts/dependency-test.sh` | 镜像名改为本仓库 |
| 保留 | Go 模块路径、`com.centurylinklabs.watchtower*` 标签、`containrrr/shoutrrr` 依赖、测试夹具、LICENSE、README 致谢 |

## 6. 留给构建者决定的

- 具体 GitHub Actions 的版本：选各自最新的稳定版，钉完整 commit SHA。
- 初始 `.go-version` 钉死的具体补丁版本号，以及首次合入后是否由 `go-toolchain-sync.yml` 自动发起首个 `1.26 → 1.27` 跨次版本升级 PR 供维护者审核。
- `tools/go.mod` 中 `goreleaser/v2`、`staticcheck`、`govulncheck` 的具体最新稳定版本，以及 `govulncheck` 基线漏洞清理与限时豁免清单文件的具体格式。
- `zizmor` 的接入方式（固定版本二进制或容器/pip）和严重级别阈值。当前全部工作流需要在新配置下零告警，必要的豁免逐条写理由。
- 发布白名单包含 Go 源码、Go 模块与工具链、Docker 和 GoReleaser 配置，以及直接参与构建的工作流与许可证。脚本检查上一正式版本到候选提交的净变更，重命名按旧路径删除和新路径新增计算。
- 草稿 Release 的正文日志先保留 `(tag, sha)`，后写入全部资产哈希；正式创建 tag 前上传一次不可覆盖的 `release-record.json`。已标记的 tag 可恢复缺失的草稿。镜像以 digest 上传并证明，写入草稿日志后才绑定版本 tag。正式发布后若 `:latest` 推进中断，重放先核验已记录的版本 digest 和证明；同一 digest 已在 `:latest` 时只补记完成状态。
- 两个入口在 CI 之后共用 `release-publish` 并发组。GitHub 默认只保留一个待运行任务，可能顶掉手动 tag 的发布任务。若仍需构建，使用 GitHub Actions 的 **Re-run jobs** 重跑原定时运行或 tag 推送运行，以保留匹配的 `GITHUB_SHA`。`release-tag.yml` 从 `main` 手动重放旧 tag 时，可复用已记录并通过校验的归档和镜像，也可补做 `:latest` 推进。若原运行已无法重跑且仍需构建，保留预留记录并由维护者核查。`push-main.yml` 独立持有 `publish-latest-dev` 串行锁。
- tag 推送事件从被标记的提交加载工作流。必须将正式 tag 的创建和更新权限限定给可信发布者，发布者只标记经审核的 `main` 提交。运行时祖先核验只约束发布的提交。并发锁协调这些入口，GHCR 的 tag 更新与 GitHub 的 tag 创建之间没有跨服务原子操作，外部写入者必须遵守相同发布规则。
- 重试只补齐能与日志对应的产物。被中断的无标签镜像推送可能留下孤立 digest；日志写入前可重新构建。GitHub 若留下 `starter` 资产或构建重现的哈希发生变化，发布停止，由维护者核查后恢复。彩排只验证构建，证明、GHCR 和并发行为需要在授权的彩排仓库验证。首次正式版本计划为 `v1.0.1`，在 `main-checks` 生效后从 `main` 手动取消勾选 `dry_run`。
- 覆盖率上传保留在 `ci.yml` 里，用输入开关控制，失败不阻断。
- `docs/index.md` 徽章的具体替换。

## 7. 验证方式与当前基线

**当前实测基线（2026-11-24）**

- **本机 Go 基线（Windows，Go 1.27.4）**：
  - `go build -o /dev/null .`：通过。
  - `go vet ./...`：通过。
  - `go test ./...`：全部通过。
  - `go mod verify`：通过（`all modules verified`）。
  - `go mod tidy -diff`：在默认 Windows `core.autocrlf=true`（CRLF）工作区下因 `go.sum` 行尾差异返回退出码 1；在 Windows 临时 LF 副本上复测返回退出码 0（Linux CI 实际表现待推送后在 GitHub Actions 上验证）。
- **本机尚未验证**：`staticcheck`、`govulncheck`、GoReleaser v2、`actionlint`、`zizmor` 当前未安装在本机，待实现阶段通过 `tools/go.mod` 或本地工具运行验证（包括确认 `govulncheck` 当前依赖基线漏洞情况）。
- **远端仓库初始基线（2026-11-24 `gh api` 实测；其中 Dependabot 安全设置与 Rulesets 已于 2026-11-25 被 M1/M2 更新，见下方 PR 4 实测与 `d3484b7`）**：
  - 个人公开 fork（`owner_type: "User"`），默认分支 `main`，已有 release tag `v1.0.0`（2026-10-29，7 个资产）。
  - Actions 默认权限已为只读（`default_workflow_permissions: "read"`，`can_approve_pull_request_reviews: false`）。
  - 2026-11-24 基线：`allow_auto_merge: false`，`rulesets` 为空（2026-11-25 M2 已启用 `main-checks` 与 `main-review`，`allow_auto_merge` 保持 `false`）。
  - 2026-11-24 基线：`dependabot_security_updates` 与 `dependabot/alerts` 均为关闭（2026-11-25 M1 已开启 `automated-security-fixes` 与 `vulnerability-alerts`）。
  - `has_issues: false`（2026-11-25 M1 核实保持关闭）。
  - `CodeQL` 定时工作流已在每周运行（最近一次 2026-11-19）；这仅证明 `CodeQL` 已启用，新建的 `go-toolchain-sync.yml` 与 `scheduled-release.yml` 需在合入后单独确认处于启用状态。

**实现阶段验证清单**

- 根目录与 `/tools` 模块的 `go build ./...`、`go vet ./...`、`go test ./...`、`go mod verify`、`go mod tidy -diff`（LF 环境下）全部通过。
- `go tool -modfile=tools/go.mod staticcheck ./...`、`go tool -modfile=tools/go.mod actionlint` 与 `go tool -modfile=tools/go.mod goreleaser ...` 已验证可用；`govulncheck` 由 `scripts/run-govulncheck.sh` 按 `.go-version` 以 `go build -modfile=tools/go.mod` 构建一次扫描器后遍历发布目标运行。
- `go tool -modfile=tools/go.mod goreleaser check` 与 `go tool -modfile=tools/go.mod goreleaser release --snapshot --clean` 在本地运行通过，且核对生成的 8 个归档文件名（含 `armhf`、仅 Linux 使用的 `arm64v8`、Windows `.zip`、`darwin_amd64`/`darwin_arm64`）、归档内 `LICENSE.md`、`checksums.txt`（8 行）及二进制内嵌版本字符串与发布契约一致：`v1.0.0` 的 6 个归档名不变，只新增 2 个 `darwin` 归档。
- macOS 发布目标在 `test (macos-latest)` 中按发布参数（`CGO_ENABLED=0` 与相同 `ldflags`）构建 `darwin/arm64` 与 `darwin/amd64`，用 `file` 核对架构、用 `codesign -v` 核对 `arm64` 的 ad-hoc 签名，并运行本机架构二进制的 `--help`（不连接 Docker）。
- `pip install --require-hashes -r docs-requirements.txt`、`scripts/build-tplprev.sh` 与 `mkdocs build --strict` 本地构建通过。
- 所有工作流通过 `actionlint` 和 `zizmor`；`dependabot.yml` 通过 JSON Schema 校验；`scripts/` 与 `.github/scripts/` 的 Python 单元测试全部通过。
- 所有 `uses:` 的 SHA 用 `git ls-remote` 与版本 tag 逐一核对。

**PR 4 实测结论与校准边界（2026-11-25）**

- **Ruleset 与合并方式（M2，`d3484b7`）**：`main-checks`（ID `28051307`，`active`，零豁免）将 `allowed_merge_methods` 限定为 `["merge"]`，`main-review`（ID `28051631`，`active`）将 `watchtower-automerge`（Integration `5915895`）与 `Repository admin` 列为 `pull_request` 绕过项。`automerge.yml` 相应使用单次 `sha` 保护的 `PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge`（`merge_method=merge`），保持 HTTP 405/409 非重试正常退出语义不变。
- **Go 冷却期索引实测（4.2）**：`cooldown.go_first_seen` 在 10 次请求预算内（1 次 `proxy.golang.org` `.info` 提示 + 最多 9 页 `index.golang.org/index`、每页 2000 条）实测约命中 6/12 个 Go 模块样本，且 `golang.org/toolchain` 样本未命中。按 fail-closed 原则，凡在预算内未命中的版本会使对应依赖更新 PR 留在人工审核，并阻止该次 `go-toolchain-sync.yml` 创建/更新工具链同步 PR（亦使 `automerge.yml` 对该版本返回 `eligible=false`）。
- **Dependabot 普通与安全更新校准（2026-11-26）**：离线复核真实样本后确认，普通更新与安全更新可以使用完全相同的 PR 正文结构与标签，正文信号不能区分更新类型。生产白名单保持为空，Dependabot PR 转人工审核；样本来源不在公开文档中记录。
- **PyPI 与 Docker 冷却期**：PyPI 核验覆盖锁文件中该包该版本所有允许的 `--hash=sha256:...` 并取最晚 `upload_time_iso_8601`（任一缺失即 fail-closed）；Docker 核验对缺失 `tag_last_pushed` 或 digest 不匹配一律 fail-closed。
- **相对初版设计的 0.1 实施偏离**：`go tool -modfile=tools/go.mod` 用于 `staticcheck`/`goreleaser`/`actionlint`，`govulncheck` 改由 `scripts/run-govulncheck.sh` 经 `go build -modfile=tools/go.mod` 扫描各目标；新增最小范围 `.gitattributes`；`tools/go.mod` 增补 `actionlint`；校验与同步逻辑落地在 `.github/scripts/`（Python 标准库 + 单元测试）并将 `.github/scripts/**` 列入敏感路径硬禁区。

## 8. 需要维护者在 GitHub 上手动完成的设置

**必需前提（启用受控自动合并与强制本人审核前完成）**

1. **创建并安装 2 个仅限本仓库的私有 GitHub App**（在个人 **Settings → Developer settings → GitHub Apps → New GitHub App**）：
   - **`watchtower-updater`（更新专用 App）**：关闭 Webhook；Repository permissions 仅设 `Contents: Read and write`、`Pull requests: Read and write`（`Metadata: Read-only` 默认）；Where can this GitHub App be installed 选 **Only on this account**。创建后生成 Private Key，记录 App ID，并将其 **仅安装到本 `watchtower` 仓库**。
   - **`watchtower-automerge`（合并专用 App）**：权限、可见性与安装范围同上，单独生成 Private Key 并记录 App ID，**仅安装到本 `watchtower` 仓库**。
2. **仓库 Settings → Environments**：创建两个受保护环境，均将 **Deployment branches** 设为 **Selected branches** 并只添加精准匹配的 `main`：
   - 环境 `updater`：添加 Environment secrets `UPDATER_APP_ID` 与 `UPDATER_APP_PRIVATE_KEY`。
   - 环境 `automerge`：添加 Environment secrets `AUTOMERGE_APP_ID` 与 `AUTOMERGE_APP_PRIVATE_KEY`。
3. **仓库 Settings → Rules → Rulesets**：为默认分支 `main` 创建两套 **Enforcement status = Active** 的分支规则集，并用 `gh api repos/{owner}/{repo}/rules/branches/main` 核实已生效：
   - **规则集 1：`main-checks`（质量硬底线，Bypass list 保持完全空白）**：
     - 勾选 **Restrict deletions**、**Block force pushes**；
     - 勾选 **Require a pull request before merging**，其中 `Required approvals` 设为 **`0`**；
     - 勾选 **Require status checks to pass** 并同时勾选 **Require branches to be up to date before merging**，把 `ci.yml` 各检查任务（lint、三平台 test、mod checks、govulncheck、goreleaser snapshot、docker build、docs build、zizmor）、`dependency-review` 与 `CodeQL` 全部添加为必需检查。
   - **规则集 2：`main-review`（强制本人审核，方法 B）**：
     - 勾选 **Require a pull request before merging**，将 `Required approvals` 设为 **`1`**，并勾选 **`Require review from Code Owners`**、**`Dismiss stale pull request approvals when new commits are pushed`**、**`Require approval of the most recent reviewable push`**；
     - **Bypass list** 仅添加以下两项，模式均选 **`For pull requests only`**：
       1. `watchtower-automerge`（GitHub App）
       2. `Repository admin`（Role）
     - **方法 B 操作约定与风险边界**：GitHub 的 `Repository admin`（`For pull requests only`）豁免对**任何 PR** 均有效，平台无法将其限制为"仅限自提 PR"或"仅限网页端手动点击"；任何持有本仓库管理员权限的 token（PAT / CLI / OAuth）都具备相同的绕过 `main-review` 审批能力（但仍无法绕过零豁免的 `main-checks`）。请严格遵守两点约定：① 仅在本人自提 PR 审阅无误后使用管理员绕过合并；② 绝不将具备本仓 admin 权限的个人凭据提供给外部工具或第三方 Agent（外部工具如需访问，仅授予无 admin 权限的细粒度只读/分支级 token，使其 PR 同样受 `main-review` 强制本人审批约束）。
4. **仓库 Settings → Code security（或 Advanced Security）**：打开 Dependency graph、Dependabot alerts、Dependabot security updates 与 Private vulnerability reporting。合入后在 Insights → Dependency graph → Dependabot 中确认 version updates 已启用。

**已核实到位与上线验收清单**

5. **仓库 Settings → Actions → General → Workflow permissions**：已通过 `gh api` 确认当前为只读（`read`）且 `can_approve_pull_request_reviews: false`，**保持现状不变**。
6. **仓库 Settings → General → Features → Issues**：当前实测 `has_issues: false`。若希望接收外部反馈，请启用 Issues；若打算保持关闭，可在实现时决定是否精简 Issue 模板，仅保留 `SECURITY.md` 的私密漏洞报告入口。
7. **定时工作流启用确认与 60 天无活动巡检**：合入后在 Actions 页确认新建的 `go-toolchain-sync.yml` 与 `scheduled-release.yml` 均处于 `active` 状态；公开仓库连续 60 天无活动时 GitHub 会自动停用定时工作流，需关注通知并定期巡检。
8. **合入后端到端验收**：
   - 手动触发一次 `go-toolchain-sync.yml`（`workflow_dispatch`），验证更新 App 自动签名提交并创建 Go 升级 PR（如 `1.26 → 1.27` 跨次版本 PR），确认 `automerge.yml` 输出 `eligible=false` 且被 `main-review` 拦下等待本人审批；
   - 观察首批 Dependabot PR：确认低风险补丁/次版本 PR 在 CI + `CodeQL` 全绿后由 `watchtower-automerge` 自动合入，而 GitHub Actions PR 被拦下等待本人 Approve；
   - 手动触发一次 `scheduled-release.yml`（`workflow_dispatch`），验证固定 SHA → CI 验证 → 打 tag → GoReleaser v2 发布 → GHCR 镜像与 SLSA 证明全链路可用。

## 9. 后续工作（本次不做）

- 评估 Go 模块路径 `github.com/containrrr/watchtower` 改名。
- 若未来引入独立日常开发账号或多人协作，可将 `main-review` 规则集中的 `Repository admin` 绕过移除（收紧为方法 A），实现连管理员自提 PR 也必须经另一独立身份审批。
