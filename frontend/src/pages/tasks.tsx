import { Link, Navigate, useParams } from "react-router-dom";
import { useRead } from "../api/client";
import {
  Badge,
  Command,
  Copy,
  Detail,
  Empty,
  List,
  Notice,
  Pairs,
  ReadState,
  SafeLink,
  arr,
  obj,
  statusName,
  str,
  when,
} from "../components";
import { ProjectPicker } from "./projects";

export function TasksPage({ review = false }: { review?: boolean }) {
  const { projectId = "", runId = "" } = useParams();
  const projects = useRead("projects");
  const project = projects.data?.projects.find((p) => p.id === projectId);
  const accessible = Boolean(project?.policy.task_access);
  const listing = useRead(
    "tasks",
    { project_id: projectId },
    accessible && !runId,
  );
  const task = useRead(
    "task",
    { project_id: projectId, run_id: runId },
    accessible && Boolean(runId) && !review,
  );
  const evidence = useRead(
    "review",
    { project_id: projectId, run_id: runId },
    accessible && Boolean(runId) && review,
  );
  if (!projectId && projects.data?.projects.length)
    return (
      <Navigate
        replace
        to={`/projects/${projects.data.projects[0].id}/tasks`}
      />
    );
  return (
    <>
      <header className="page-heading">
        <h1>{review ? "审阅依据" : "开发任务"}</h1>
        <p>
          {review
            ? "核对历史验证、当前适用性与交付轨迹。"
            : "找回目标、已有决定与尚未完成的工作。"}
        </p>
      </header>
      <ReadState query={projects} />
      {projects.data && (
        <ProjectPicker
          projects={projects.data.projects}
          value={projectId}
          target="/tasks"
        />
      )}
      {!projects.data?.projects.length && !projects.isPending && (
        <Empty title="还没有项目">
          先关联一个本地项目，再查看任务与检查点。
        </Empty>
      )}
      {project && !accessible && (
        <Empty title="项目策略未启用">
          请在 CLI 中配置仓库角色与贡献策略。
        </Empty>
      )}
      {accessible && !runId && (
        <>
          <ReadState query={listing} />
          <section className="panel">
            <h2>最近任务尝试</h2>
            <p className="muted">每次尝试保留自己的上下文和验证记录。</p>
            {listing.data?.tasks.map((item) => (
              <article className="attention" key={item.id}>
                <div>
                  <Link to={`/projects/${projectId}/tasks/${item.id}`}>
                    {item.title || `Issue #${item.issue_number}`}
                  </Link>
                  <small>
                    #{item.issue_number} ·{" "}
                    {item.stage === "external" ? "外部 Agent" : "仓库维护"} ·{" "}
                    {when(item.updated_at)}
                  </small>
                </div>
                <Badge value={item.status} />
                <Link to={`/projects/${projectId}/tasks/${item.id}/review`}>
                  审阅依据
                </Link>
              </article>
            ))}
            {listing.data && !listing.data.tasks.length && (
              <Empty title="这里还没有任务">
                从已审阅 Issue 开始一次任务后，上下文和检查点会出现在这里。
              </Empty>
            )}
            {!!listing.data?.omitted && (
              <Notice>另有 {listing.data.omitted} 次尝试未展示。</Notice>
            )}
          </section>
        </>
      )}
      {accessible && runId && (
        <>
          <div className="tabs">
            <Link to={`/projects/${projectId}/tasks`}>返回任务列表</Link>
            <Link
              aria-current={!review ? "page" : undefined}
              to={`/projects/${projectId}/tasks/${runId}`}
            >
              任务上下文
            </Link>
            <Link
              aria-current={review ? "page" : undefined}
              to={`/projects/${projectId}/tasks/${runId}/review`}
            >
              审阅依据
            </Link>
          </div>
          <ReadState query={review ? evidence : task} />
          {task.data && !review && <ContextView result={task.data} />}
          {evidence.data && review && <ReviewView result={evidence.data} />}
        </>
      )}
    </>
  );
}

function ContextView({
  result,
}: {
  result: NonNullable<ReturnType<typeof useRead<"task">>["data"]>;
}) {
  if (!result.context)
    return (
      <Empty title="缺少接续上下文">
        这次尝试尚未保存上下文或检查点，可先查看审阅轨迹。
      </Empty>
    );
  const context = result.context;
  const external = result.kind === "external";
  const pack = external ? context : obj(context.context_pack);
  const checkpoint = external ? context : obj(context.checkpoint);
  const task = obj(pack.task);
  const contract = obj(external ? context.contract : pack.task_contract);
  return (
    <>
      <section className="panel">
        <h2>
          {str(task.title) || str(obj(context.work_item).title) || "任务目标"}
        </h2>
        {Boolean(task.url) && (
          <SafeLink href={str(task.url)}>查看原始 Issue</SafeLink>
        )}
        <p className="muted">
          {external
            ? `检查点版本 ${str(context.revision)} · ${statusName(str(context.remote_freshness))}`
            : `上下文保存于 ${when(obj(context.context_metadata).created_at)}`}
        </p>
      </section>
      {!external && (
        <Notice>这是已保存的维护上下文，当前工作区适用性尚未重新核对。</Notice>
      )}
      {external && arr(context.validity).length > 0 && (
        <Notice>
          当前基线存在变化：
          <List items={context.validity} />
        </Notice>
      )}
      {!external && !context.checkpoint && (
        <Empty title="尚无检查点">仍可阅读任务目标与原始上下文。</Empty>
      )}
      <div className="split">
        <div>
          <section className="panel">
            <h2>未完成工作</h2>
            <List items={external ? context.open_work : checkpoint.remaining} />
          </section>
          <section className="panel">
            <h2>已经做出的决定</h2>
            <List items={checkpoint.decisions} />
          </section>
        </div>
        <div>
          <section className="panel">
            <h2>下一步</h2>
            <p>{str(checkpoint.next_action) || "尚未记录"}</p>
            <h3>阻塞</h3>
            <List items={checkpoint.blockers} empty="尚未记录阻塞。" />
          </section>
          <section className="panel">
            <h2>接续入口</h2>
            <Command value={result.command} />
          </section>
        </div>
      </div>
      {Object.keys(contract).length > 0 && (
        <section className="panel">
          <h2>目标与验收约束</h2>
          <p>{str(contract.goal)}</p>
          <h3>验收条件</h3>
          <List items={contract.acceptance_criteria} />
          <h3>范围约束</h3>
          <List items={contract.scope_boundaries} />
          {Boolean(contract.source_requirements) && (
            <p>{str(contract.source_requirements)}</p>
          )}
          <Detail title="契约来源与版本" value={contract} />
        </section>
      )}
      <Detail
        title="Agent 自述（尚未独立核验）"
        value={
          external
            ? context.agent_claims
            : {
                completed: checkpoint.completed,
                tests_observed: checkpoint.tests_observed,
              }
        }
      />
      <Detail title="完整接续数据、来源与省略说明" value={context} />
      <Copy label="复制接续数据" value={JSON.stringify(context, null, 2)} />
    </>
  );
}

function ReviewView({
  result,
}: {
  result: NonNullable<ReturnType<typeof useRead<"review">>["data"]>;
}) {
  const trace = result.trace,
    current = obj(trace.current),
    stats = obj(trace.stats);
  return (
    <>
      <Notice>
        以下为已有证据与审计记录。历史验证通过不等于当前可以提交或合并。
      </Notice>
      <section className="panel">
        <h2>Issue 最新记录摘要</h2>
        <p className="muted">
          #{str(trace.issue_number)} · 包含该 Issue 的多次尝试
        </p>
        <Pairs
          values={[
            ["后续动作", trace.next_action],
            ["记录状态", statusName(str(current.status))],
            ["合并结果", statusName(str(current.merge_outcome))],
            ["证据 HEAD", current.head_sha],
            ["覆盖", trace.complete ? "范围内完整" : "有省略或未知"],
          ]}
        />
      </section>
      {arr(result.verification?.evidence).map((item, index) => {
        const evidence = obj(item);
        return (
          <section className="panel" key={index}>
            <div className="row">
              <Badge value={str(evidence.outcome)} />
              <Badge value={str(evidence.current_applicability)} />
            </div>
            <p>
              {str(evidence.profile)} · {when(evidence.updated_at)}
            </p>
            <List items={evidence.validity} empty="未观察到已检查项的差异。" />
            <Detail title="验证证据" value={evidence} />
          </section>
        );
      })}
      <section className="panel">
        <h2>任务轨迹</h2>
        {arr(trace.events).map((item, i) => {
          const event = obj(item);
          return (
            <article className="event" key={i}>
              <div className="row">
                <Badge value={str(event.source || event.kind)} />
                <small>{when(event.occurred_at)}</small>
              </div>
              <p>
                {str(event.summary || event.event || event.kind || event.id)}
              </p>
              <Detail title="事件依据" value={event} />
            </article>
          );
        })}
        {Number(stats.events_omitted) > 0 && (
          <Notice>另有 {str(stats.events_omitted)} 条事件未展示。</Notice>
        )}
      </section>
      <Detail title="完整范围与来源" value={result} />
    </>
  );
}
