import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { command, useRead, type ReadModels } from "../api/client";
import {
  arr,
  Badge,
  Command,
  Detail,
  Empty,
  Notice,
  obj,
  ReadState,
  SafeLink,
  str,
  when,
} from "../components";


function sourceName(raw: unknown): string {
  const name = str(raw);
  if (name.startsWith("pull:")) return "PR #" + name.slice(5);
  if (name.startsWith("checks:"))
    return "PR #" + name.slice(7) + " 的 CI / 评审";
  return (
    (
      {
        pulls: "开放 PR",
        issues: "开放 Issue",
        activity: "最近更新",
        summary: "操作结果",
        scan_started: "开始扫描",
        index_published: "索引已更新",
        inspected: "项目已识别",
        authorized: "计划已核对",
        staging: "克隆暂存目录已建立",
        clone_prepared: "克隆内容已核对",
        published: "克隆目录已创建",
        registered: "项目已登记",
        completed: "导入已完成",
        failure: "失败原因",
      } as Record<string, string>
    )[name] || name
  );
}
function errorText(raw: unknown): string {
  return (
    (
      {
        workspace_changed: "工作区已变化，请重新核对计划",
        workspace_scan_failed: "扫描未完成，请核对目录或重建索引",
        partial_sync: "部分来源同步失败，可重试",
        network_unavailable: "暂时无法连接 GitHub",
        permission_or_missing: "权限不足或远程记录不存在",
        rate_limited: "GitHub 暂时限制请求频率",
        remote_unavailable: "GitHub 服务暂不可用",
        interrupted: "同步已停止，可重试",
        account_changed: "登录账号与配置不一致，请检查 GitHub 授权",
        invalid_response: "GitHub 返回的数据无法确认",
        request_budget: "本次同步已达到请求范围上限",
        local_sync_failed: "本地同步未完成，请查看设置诊断",
        not_synced: "尚未同步",
        head_unknown: "尚未确认当前提交",
        response_limit: "来源数据超出读取范围",
      } as Record<string, string>
    )[str(raw)] || str(raw)
  );
}
function eventName(raw: unknown): string {
  return (
    (
      {
        enqueued: "已登记",
        claimed: "开始执行",
        taken_over: "进程恢复后继续执行",
        completed: "已完成",
        failed: "执行失败",
        cancelled: "已取消",
        requeued: "重新排队",
        attempts_exhausted: "达到重试次数，等待人工处理",
      } as Record<string, string>
    )[str(raw)] || str(raw)
  );
}

function Sync({ project }: { project: string }) {
  const navigate = useNavigate();
  const session = useRead("session");
  const mutation = useMutation({
    mutationFn: () => command("github/sync", { project_id: project }),
    onSuccess: (result) => navigate(`/operations/${result.id}`),
  });
  return (
    <div className="sync-control">
      <button
        disabled={
          mutation.isPending ||
          !session.data?.capabilities.includes("manage_local")
        }
        onClick={() => mutation.mutate()}
      >
        {mutation.isPending ? "正在登记…" : "同步 GitHub"}
      </button>
      {mutation.error && <Notice>{mutation.error.message}</Notice>}
      {!session.data?.capabilities.includes("manage_local") && (
        <small>当前会话仅允许读取。</small>
      )}
    </div>
  );
}

function PullDetail({
  item: summary,
}: {
  item: ReadModels["github"]["items"][number];
}) {
  const { projectId = "" } = useParams();
  const [open, setOpen] = useState(false);
  const query = useRead(
    "github",
    { project_id: projectId, number: String(summary.number) },
    open,
  );
  const item = query.data?.items[0] || summary;
  const checks = obj(item.checks);
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>查看 PR 详情与 CI / 评审</summary>
      <ReadState query={query} />
      {Boolean(item.body) && <p className="preserve">{str(item.body)}</p>}
      <p>
        当前观测提交 <code>{item.head_sha.slice(0, 12) || "未知"}</code> ·
        读取于 {when(item.observed_at)}
      </p>
      {item.checks ? (
        <>
          <p>
            状态检查：{str(checks.status)} · 读取于{" "}
            {when(item.checks_fetched_at)}
          </p>
          <ul>
            {arr(checks.checks).map((raw, i) => {
              const check = obj(raw);
              return (
                <li key={i}>
                  {str(check.name)}：
                  {str(check.conclusion) || str(check.status)}
                </li>
              );
            })}
          </ul>
          <p>评审记录（提交不同的评审保留为历史）：</p>
          <ul>
            {arr(checks.reviews).map((raw, i) => {
              const review = obj(raw);
              return (
                <li key={i}>
                  {str(review.author)} · {str(review.state)} ·{" "}
                  {when(review.submitted_at)} ·{" "}
                  <code>{str(review.commit_id).slice(0, 12)}</code>
                </li>
              );
            })}
          </ul>
        </>
      ) : (
        <p>尚无匹配当前提交的 CI / 评审观测。</p>
      )}
      {arr(item.native_runs).length > 0 && (
        <div>
          <p>关联的 RepoSteward 任务记录：</p>
          {arr(item.native_runs).map((raw) => {
            const run = obj(raw);
            return (
              <div key={str(run.id)}>
                <p>
                  Issue #{str(run.issue_number)} ·{" "}
                  <Badge value={str(run.status)} /> · {str(run.id).slice(0, 12)}
                </p>
                <Command value={str(run.trace_command)} />
              </div>
            );
          })}
        </div>
      )}
      {Boolean(item.checks_error) && (
        <p className="muted">CI / 评审来源：{errorText(item.checks_error)}</p>
      )}
    </details>
  );
}

export function GitHubPage() {
  const { projectId = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const kind = params.get("kind") || "pulls";
  const cursor = params.get("cursor") || "";
  const query = useRead("github", { project_id: projectId, kind, cursor });
  const data = query.data;
  return (
    <>
      <header className="page-heading">
        <Link to={`/projects/${projectId}`}>返回项目</Link>
        <h1>GitHub 维护</h1>
        <p>{data?.repository || "项目线上状态"}</p>
      </header>
      <div className="row">
        <Sync project={projectId} />
        <Link to="/operations">查看本地操作</Link>
      </div>
      <nav className="tabs" aria-label="GitHub 视图">
        {[
          ["pulls", "PR 状态"],
          ["issues", "开放 Issue"],
          ["activity", "最近更新"],
        ].map(([key, label]) => (
          <button
            aria-pressed={kind === key}
            key={key}
            onClick={() => setParams({ kind: key })}
          >
            {label}
          </button>
        ))}
      </nav>
      <ReadState query={query} />
      {query.error && cursor && (
        <button onClick={() => setParams({ kind })}>重新读取第一页</button>
      )}
      {data?.never_synced && (
        <Empty title="尚未同步 GitHub">
          点击“同步
          GitHub”，将线上记录保存到本地。页面重新读取和刷新不会自动发起远程同步。
        </Empty>
      )}
      {data && !data.never_synced && (
        <>
          <p className="muted">
            {data.coverage} 已观测 {data.observed_count} 条。
            {data.has_more_remote && "远程仍有更多记录，当前窗口未覆盖全部。"}
          </p>
          <div className="github-items">
            {data.items.map((item) => (
              <article className="card" key={item.number}>
                <div className="row">
                  <Badge
                    value={item.state}
                    tone={item.state === "merged" ? "good" : ""}
                  />
                  {Boolean(item.draft) && <Badge value="草稿" />}
                  <SafeLink href={item.url}>
                    #{item.number} {item.title}
                  </SafeLink>
                </div>
                <p className="muted">
                  {item.author} · 更新于 {when(item.updated_at)}
                </p>
                {kind !== "issues" && <PullDetail item={item} />}
              </article>
            ))}
          </div>
          {!data.items.length && (
            <Empty title="此窗口暂无记录">
              来源失败时会保留上次成功结果；空列表不证明其他 PR 已关闭。
            </Empty>
          )}
          <div className="row">
            {cursor && (
              <button onClick={() => setParams({ kind })}>第一页</button>
            )}
            {data.next_cursor && (
              <button
                onClick={() => setParams({ kind, cursor: data.next_cursor })}
              >
                下一页
              </button>
            )}
          </div>
          <details>
            <summary>同步来源与覆盖范围</summary>
            {Boolean(data.sources_omitted) && (
              <p>另有 {str(data.sources_omitted)} 个较早来源未展示。</p>
            )}
            <ul>
              {data.sources.map((source) => (
                <li key={str(source.source)}>
                  <strong>{sourceName(source.source)}</strong> ·{" "}
                  {source.stale ? "已过期" : "15 分钟内观测"} · 最近成功{" "}
                  {when(source.last_success)} · 最近尝试{" "}
                  {when(source.last_attempt)} ·{" "}
                  {source.complete ? "窗口完整" : "窗口未完整"}
                  {Boolean(source.error_code) &&
                    ` · ${errorText(source.error_code)}`}
                  {Boolean(source.retry_at) &&
                    ` · 可重试时间 ${when(source.retry_at)}`}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </>
  );
}

function actionName(action: string) {
  return (
    (
      {
        "github.sync": "GitHub 同步",
        "project.inspect": "识别项目",
        "project.apply": "导入项目",
        "workspace.scan": "扫描工作区",
        "assistance.verification": "验证操作",
        "assistance.understanding": "项目理解报告",
      } as Record<string, string>
    )[action] || action
  );
}

function OperationDetail({ id }: { id: string }) {
  const query = useRead("operation", { operation_id: id });
  const session = useRead("session");
  const cache = useQueryClient();
  const data = query.data;
  useEffect(() => {
    if (data && ["completed", "failed", "cancelled"].includes(data.state)) {
      void cache.invalidateQueries({ queryKey: ["github"] });
      void cache.invalidateQueries({ queryKey: ["overview"] });
      void cache.invalidateQueries({ queryKey: ["workspace"] });
    }
  }, [data?.state, cache]);
  const mutation = useMutation({
    mutationFn: (action: "cancel" | "retry") =>
      command(`operations/${action}`, {
        operation_id: id,
        expected_revision: data!.revision,
      }),
    onSuccess: () => cache.invalidateQueries(),
  });
  return (
    <>
      <ReadState query={query} />
      {data && (
        <>
          <header className="page-heading">
            <h1>{actionName(data.action)}</h1>
            <p>{data.repository}</p>
          </header>
          <div className="row">
            <Badge value={data.state} />
            <span>
              尝试 {data.attempt_count} / {data.max_attempts}
            </span>
            {data.import_id ? (
              <Link to={`/imports/${data.import_id}`}>查看导入与恢复</Link>
            ) : (
              <Link
                to={`/projects/${data.project_id}${data.action === "workspace.scan" ? `/workspaces/${data.binding_id}` : "/github"}`}
              >
                {data.action === "workspace.scan"
                  ? "查看工作区导览"
                  : "查看 GitHub 观测"}
              </Link>
            )}
          </div>
          <p>
            登记于 {when(data.created_at)} · 更新于 {when(data.updated_at)}
          </p>
          {data.action.startsWith("assistance.") && <Notice>此操作完成仅表示报告或验证结束，开发任务的交付状态仍需单独核对。</Notice>}
          {data.cancel_requested && data.state === "running" && <Notice>已请求取消，正在等待执行器确认停止。</Notice>}
          {data.last_error_code && (
            <Notice>
              操作结果：{errorText(data.last_error_code)}
              。已成功的来源保留在本地。
              {data.last_error_code === "rate_limited" && (
                <span>最早重试时间：{when(data.available_at)}</span>
              )}
            </Notice>
          )}
          <div className="row">
            {data.can_cancel && (
              <button
                disabled={
                  mutation.isPending ||
                  !session.data?.capabilities.includes("manage_local")
                }
                onClick={() => mutation.mutate("cancel")}
              >
                {data.state === "running" ? "请求取消" : "取消排队"}
              </button>
            )}
            {data.can_retry && (
              <button
                disabled={
                  mutation.isPending ||
                  !session.data?.capabilities.includes("manage_local")
                }
                onClick={() => mutation.mutate("retry")}
              >
                重试操作
              </button>
            )}
          </div>
          {mutation.error && <Notice>{mutation.error.message}</Notice>}
          <h2>执行记录</h2>
          <ol>
            {data.stages.map((stage, i) => {
              const result = obj(stage.result);
              return (
                <li key={i}>
                  {sourceName(stage.stage)} ·{" "}
                  <Badge value={str(result.outcome || result.status)} /> ·{" "}
                  {when(stage.created_at)}
                  {Boolean(result.error_code) &&
                    ` · ${errorText(result.error_code)}`}
                  {Boolean(result.message) && <p>{str(result.message)}</p>}
                  {Boolean(result.project_id) && (
                    <Link to={`/projects/${str(result.project_id)}`}>
                      查看已登记项目
                    </Link>
                  )}
                  {stage.stage === "result" && <Detail title="结果与来源依据" value={result} />}
                  {stage.stage === "summary" &&
                    data.action === "workspace.scan" && (
                      <span>
                        {" "}
                        · 已扫描 {str(obj(result.coverage).indexed_files)}{" "}
                        个文件
                      </span>
                    )}
                  {stage.stage === "summary" &&
                    data.action === "github.sync" && (
                      <span>
                        {" "}
                        · 成功 {str(result.sources_ok)} 项，失败{" "}
                        {str(result.sources_failed)} 项，已知 PR 未覆盖{" "}
                        {str(result.known_omitted)} 项，CI / 评审未覆盖{" "}
                        {str(result.checks_omitted)} 项
                      </span>
                    )}
                </li>
              );
            })}
          </ol>
          {!!data.stages_omitted && <Notice>仅展示最近结果；另有 {data.stages_omitted} 条历史结果保留在本地审计中。</Notice>}
          {!data.stages.length && <p>操作已持久保存，等待工作进程执行。</p>}
          <details>
            <summary>排队、领取与恢复历史</summary>
            <ol>
              {data.attempts.map((attempt, i) => (
                <li key={i}>
                  {eventName(attempt.event)} · {when(attempt.created_at)}
                </li>
              ))}
            </ol>
          </details>
        </>
      )}
    </>
  );
}

export function OperationsPage() {
  const { operationId = "" } = useParams();
  const [before, setBefore] = useState("0");
  const query = useRead("operations", { before }, !operationId);
  if (operationId)
    return (
      <>
        <Link to="/operations">全部本地操作</Link>
        <OperationDetail id={operationId} />
      </>
    );
  return (
    <>
      <header className="page-heading">
        <h1>本地操作</h1>
        <p>显式发起的同步、导入、扫描、验证与报告记录，刷新页面后可以继续查看。</p>
      </header>
      <ReadState query={query} />
      <div className="github-items">
        {query.data?.items.map((item) => (
          <article className="card" key={item.id}>
            <Badge value={item.state} />{" "}
            <Link to={`/operations/${item.id}`}>
              {item.repository || "新项目"} · {actionName(item.action)}
            </Link>
            <p>{when(item.created_at)}</p>
          </article>
        ))}
      </div>
      {query.data && !query.data.items.length && (
        <Empty title="还没有本地操作">
          打开项目的 GitHub 维护页，即可显式同步。
        </Empty>
      )}
      <div className="row">
        {before !== "0" && (
          <button onClick={() => setBefore("0")}>最新记录</button>
        )}
        {!!query.data?.next_before && (
          <button onClick={() => setBefore(String(query.data?.next_before))}>
            更早记录
          </button>
        )}
      </div>
    </>
  );
}
