import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { command, read, useRead, type ReadModels } from "../api/client";
import { Detail, Notice, ReadState, str, when } from "../components";

function HandoffCard({ item, writable }: { item: ReadModels["handoff"]; writable: boolean }) {
  const cache = useQueryClient();
  const download = useMutation({
    mutationFn: async () => {
      const result = await read("handoff", { project_id: item.project_id, run_id: item.run_id, handoff_id: item.id });
      const url = URL.createObjectURL(new Blob([JSON.stringify(result.content, null, 2) + "\n"], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = `reposteward-handoff-${result.id}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    },
  });
  const receipt = useMutation({
    mutationFn: () => command("tasks/acknowledge", { project_id: item.project_id, run_id: item.run_id, handoff_id: item.id, expected_digest: item.digest }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["handoffs"] }),
  });
  return <article className="card">
    <strong>{item.client} · 已生成接续包</strong>
    <p>{when(item.created_at)} · {item.current_applicability === "unchanged" ? "来源未变化" : "来源已变化，请重新预览"}</p>
    <p>{item.acknowledgement ? "使用人已确认交给客户端（人工记录）" : "尚未确认客户端接收"} · 未观测客户端执行</p>
    <div className="row">
      <button disabled={download.isPending} onClick={() => download.mutate()}>下载已保存的接续包</button>
      {!item.acknowledgement && <button disabled={!writable || receipt.isPending} onClick={() => receipt.mutate()}>我已将此包交给客户端</button>}
    </div>
    {download.error && <Notice>{download.error.message}</Notice>}
    {receipt.error && <Notice>{receipt.error.message}</Notice>}
    <details><summary>内容摘要与身份</summary><code>{item.digest}</code><p>{item.id}</p></details>
  </article>;
}

export function TaskActions({ projectId, runId }: { projectId: string; runId: string }) {
  const [budget, setBudget] = useState("24000");
  const [client, setClient] = useState<"codex" | "claude-code" | "copilot-vscode">("codex");
  const cache = useQueryClient();
  const session = useRead("session");
  const writable = Boolean(session.data?.capabilities.includes("manage_local"));
  const preview = useRead("task-preview", { project_id: projectId, run_id: runId, budget });
  const handoffs = useRead("handoffs", { project_id: projectId, run_id: runId });
  const data = preview.data;
  const request = { project_id: projectId, run_id: runId, budget: Number(budget), expected_plan: data?.plan_digest || "" };
  const exportPackage = useMutation({
    mutationFn: () => command("tasks/handoff", { ...request, client }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["handoffs"] }),
  });
  const verify = useMutation({
    mutationFn: (profile: string) => command("tasks/verify", { ...request, profile }),
    onSuccess: () => cache.invalidateQueries({ queryKey: ["operations"] }),
  });
  return <section className="panel">
    <h2>接续与验证</h2>
    <p>先预览当前任务信息，再交给你正在使用的开发客户端。生成和下载不会启动客户端。</p>
    <label>上下文预算（估算 tokens） <select value={budget} onChange={event => setBudget(event.target.value)}>
      {[4000, 8000, 16000, 24000, 48000, 100000].map(value => <option key={value} value={value}>{value}</option>)}
    </select></label>
    <button onClick={() => void preview.refetch()}>重新预览</button>
    <ReadState query={preview} />
    {data && <>
      <p>当前上下文约 {data.estimated_tokens} tokens；预算 {data.budget}。此估算不代表模型实际账单。</p>
      {!!data.validity.length && <Notice>适用性：{data.validity.join("、")}</Notice>}
      <Detail title="预览接续上下文与保留的待办" value={data.context} />
      {!!data.coverage.length && <Detail title="内容取舍与原始证据位置" value={data.coverage} />}
      <div className="row">
        <label>目标客户端 <select value={client} onChange={event => setClient(event.target.value as typeof client)}>
          <option value="codex">Codex</option><option value="claude-code">Claude Code</option><option value="copilot-vscode">Copilot</option>
        </select></label>
        <button disabled={!writable || !data.export_available || exportPackage.isPending || preview.isFetching} onClick={() => exportPackage.mutate()}>生成并保存接续包</button>
      </div>
      {!data.export_available && <Notice>此历史任务没有已关联的可用工作区；可以读取上下文，关联对应工作区后才能生成快照接续包。</Notice>}
      {exportPackage.error && <Notice>{exportPackage.error.message}</Notice>}
      <h3>隔离验证</h3>
      <p>验证绑定当前工作区快照、检查点和配置。Agent 自述的测试结果仍单独保留在任务上下文中。</p>
      {data.profiles.map(profile => <div className="card" key={str(profile.name)}>
        <strong>{str(profile.name)}</strong>
        <Detail title="查看已配置的验证命令" value={profile} />
        <button disabled={!writable || !data.verification_available || verify.isPending || preview.isFetching} onClick={() => verify.mutate(str(profile.name))}>排队执行此验证</button>
      </div>)}
      {!data.profiles.length && <p>{data.kind === "managed" ? "原生维护任务继续通过 CLI 的既有验证流程执行。" : "尚无受信任的验证配置，请先在用户配置中登记。"}</p>}
      {verify.error && <Notice>{verify.error.message}</Notice>}
      {verify.data && <Link to={`/operations/${verify.data.id}`}>查看验证进度、取消与结果</Link>}
    </>}
    {!writable && <Notice>当前为只读会话。使用 reposteward web 开启本地操作。</Notice>}
    <h3>最近保存的接续包</h3>
    <ReadState query={handoffs} />
    {handoffs.data?.items.map(item => <HandoffCard key={item.id} item={item} writable={writable} />)}
    {handoffs.data?.has_more && <Notice>仅展示最近 10 个接续包，较早记录仍保存在本地。</Notice>}
  </section>;
}
