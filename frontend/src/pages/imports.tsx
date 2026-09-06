import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { command, useRead } from "../api/client";
import { Badge, Detail, Notice, ReadState } from "../components";

const obj = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
const str = (value: unknown) => (typeof value === "string" ? value : "");
const methods = {
  link: "关联已有目录",
  clone: "克隆到新目录",
  watch: "仅关注 GitHub",
};

export function ImportPage() {
  const { importId = "" } = useParams();
  const navigate = useNavigate();
  const cache = useQueryClient();
  const [kind, setKind] = useState<"github_url" | "local_path">("github_url");
  const [value, setValue] = useState("");
  const [method, setMethod] = useState<"link" | "clone" | "watch">("link");
  const [purpose, setPurpose] = useState<"maintain" | "contribute" | "watch">(
    "maintain",
  );
  const [target, setTarget] = useState("");
  const session = useRead("session");
  const query = useRead("import", { import_id: importId }, Boolean(importId));
  const data = query.data;
  const inspection = obj(data?.inspection);
  const remote = obj(inspection.remote);
  const local = obj(inspection.workspace);
  const preview = obj(data?.preview);
  const canManage = Boolean(
    session.data?.capabilities.includes("manage_local"),
  );
  const latest = data?.operations[0];
  const busy = Boolean(
    data?.operations.some((op) => ["pending", "running"].includes(op.state)),
  );
  const complete = data?.operations.find(
    (op) => op.action === "project.apply" && op.state === "completed",
  );
  const result = obj(
    complete?.stages.find((stage) => stage.stage === "summary")?.result,
  );
  useEffect(() => {
    if (complete) {
      void cache.invalidateQueries({ queryKey: ["projects"] });
    }
  }, [complete?.id, cache]);
  const inspect = useMutation({
    mutationFn: () => command("projects/inspect", { kind, value }),
    onSuccess: (op) => navigate(`/imports/${op.import_id}`),
  });
  const plan = useMutation({
    mutationFn: () =>
      command("projects/plan", {
        import_id: importId,
        inspection_id: data!.inspection_id,
        method,
        purpose,
        target: method === "watch" ? "" : target || str(local.root),
      }),
    onSuccess: () => query.refetch(),
  });
  const apply = useMutation({
    mutationFn: () =>
      command("projects/apply", {
        import_id: importId,
        preview_id: data!.preview_id,
        expected_digest: str(data?.preview_digest),
      }),
    onSuccess: () => query.refetch(),
  });
  const error = inspect.error || plan.error || apply.error;
  return (
    <>
      <header className="page-heading">
        <h1>导入项目</h1>
        <p>从 GitHub 链接或本地目录识别项目，选择关联方式后确认导入。</p>
      </header>
      <Link to="/projects">返回项目列表</Link>
      {!canManage && (
        <Notice>当前会话仅允许读取。请以管理模式重新打开本地工作台。</Notice>
      )}
      {!importId ? (
        <form
          className="panel import-form"
          onSubmit={(e) => {
            e.preventDefault();
            inspect.mutate();
          }}
        >
          <label className="field">
            项目来源
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
            >
              <option value="github_url">GitHub 链接</option>
              <option value="local_path">本地目录</option>
            </select>
          </label>
          <label className="field">
            {kind === "github_url" ? "仓库链接" : "本地绝对路径"}
            <input
              required
              value={value}
              onChange={(e) => setValue(e.target.value)}
              placeholder={
                kind === "github_url"
                  ? "https://github.com/owner/repository"
                  : "/home/you/projects/repository"
              }
            />
          </label>
          <button disabled={!canManage || inspect.isPending} type="submit">
            识别项目
          </button>
          <p className="muted">
            识别只读取仓库信息；凭据由宿主 GitHub 连接提供。
          </p>
        </form>
      ) : (
        <>
          <ReadState query={query} />
          <p className="import-source">来源：{data?.source.value}</p>
          {latest && (
            <div className="panel import-panel">
              <span>
                {latest.action === "project.inspect" ? "识别：" : "导入："}
              </span>
              <Badge value={latest.state} />{" "}
              <Link to={`/operations/${latest.id}`}>查看执行记录与重试</Link>
              {busy && <p role="status">操作已保存，刷新页面后可继续查看。</p>}
              {latest.state === "failed" && (
                <Notice>
                  操作未完成。请查看执行记录，修复连接或路径后重试；如果项目或目录已经变化，请重新识别并生成计划。
                </Notice>
              )}
            </div>
          )}
          {data?.inspection && !complete && (
            <>
              <section className="panel import-panel">
                <h2>已识别仓库</h2>
                <p>
                  {str(remote.repository)} ·{" "}
                  {remote.private ? "私有仓库" : "公开仓库"}
                </p>
                <p>
                  {remote.fork
                    ? `Fork，上游：${str(obj(remote.upstream).repository)}`
                    : "独立仓库"}
                </p>
                {Boolean(local.root) && (
                  <p>
                    本地工作区：{str(local.root)} ·{" "}
                    {obj(local.state).dirty ? "有未提交改动" : "工作区干净"}
                  </p>
                )}
                <p className="muted">
                  重复链接会复用项目；改名别名保留，仓库 ID
                  冲突需要处理后重新导入。
                </p>
              </section>
              <form
                className="panel import-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  plan.mutate();
                }}
              >
                <h2>选择关联方式</h2>
                <label className="field">
                  关联方式
                  <select
                    value={method}
                    onChange={(e) => setMethod(e.target.value as typeof method)}
                  >
                    {Object.entries(methods).map(([key, label]) => (
                      <option value={key} key={key}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                {method !== "watch" && (
                  <label className="field">
                    {method === "link" ? "已有 Git 目录" : "新的克隆目录"}
                    <input
                      required={!local.root || method === "clone"}
                      value={target}
                      onChange={(e) => setTarget(e.target.value)}
                      placeholder={
                        method === "link"
                          ? str(local.root) || "/home/you/projects/repository"
                          : "/home/you/projects/new-repository"
                      }
                    />
                  </label>
                )}
                <label className="field">
                  项目用途
                  <select
                    value={purpose}
                    onChange={(e) =>
                      setPurpose(e.target.value as typeof purpose)
                    }
                  >
                    <option value="maintain">长期维护</option>
                    <option value="contribute">参与贡献</option>
                    <option value="watch">关注学习</option>
                  </select>
                </label>
                <p className="muted">
                  用途不授予代码执行或发布权限。执行策略仍由可信配置控制。
                </p>
                <button disabled={!canManage || busy || plan.isPending}>
                  生成导入计划
                </button>
              </form>
              {data.preview && (
                <section className="panel import-panel">
                  <h2>确认这份计划</h2>
                  <p>
                    {methods[str(preview.method) as keyof typeof methods]} ·{" "}
                    {str(obj(preview.remote).repository)}
                  </p>
                  {Boolean(preview.target) && <p>{str(preview.target)}</p>}
                  <p>
                    用途：
                    {
                      (
                        {
                          maintain: "长期维护",
                          contribute: "参与贡献",
                          watch: "关注学习",
                        } as Record<string, string>
                      )[str(preview.purpose)]
                    }
                  </p>
                  <p>
                    {preview.method === "clone"
                      ? "使用宿主 SSH 克隆到新目录，已有目标不会被覆盖。"
                      : preview.method === "link"
                        ? "登记已有目录，保留当前代码、分支与 origin。"
                        : "先建立 GitHub 项目视图，之后可关联本地目录。"}
                  </p>
                  <button
                    disabled={!canManage || busy || apply.isPending}
                    onClick={() => apply.mutate()}
                  >
                    确认并导入
                  </button>
                  <Detail title="计划依据" value={preview} />
                </section>
              )}
            </>
          )}
          {complete && (
            <section className="panel import-panel">
              <h2>项目已导入</h2>
              <p>{str(result.target) || "已建立远程项目记录。"}</p>
              <Link to={`/projects/${str(result.project_id)}`}>
                进入项目空间
              </Link>{" "}
              ·{" "}
              <Link to={`/projects/${str(result.project_id)}/github`}>
                查看 GitHub 维护
              </Link>
            </section>
          )}
          <p>
            <Link
              to="/imports"
              onClick={() => {
                setTarget("");
              }}
            >
              导入另一个项目 / 重新识别
            </Link>
          </p>
        </>
      )}
      {error && <Notice>{error.message}</Notice>}
    </>
  );
}
