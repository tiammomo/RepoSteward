import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { command, useRead } from "../api/client";
import { Detail, Notice, ReadState } from "../components";

export function ScanPanel({
  projectId,
  bindingId,
}: {
  projectId: string;
  bindingId: string;
}) {
  const [open, setOpen] = useState(false);
  const [rebuild, setRebuild] = useState(false);
  const session = useRead("session");
  const navigate = useNavigate();
  const query = useRead(
    "scan-plan",
    { project_id: projectId, binding_id: bindingId, rebuild: String(rebuild) },
    open,
  );
  const data = query.data;
  const mutation = useMutation({
    mutationFn: () =>
      command("workspaces/scan", {
        project_id: projectId,
        binding_id: bindingId,
        rebuild,
        expected_revision: data!.revision,
      }),
    onSuccess: (result) => navigate(`/operations/${result.id}`),
  });
  return (
    <section className="panel">
      <div className="row">
        <h2>更新代码理解</h2>
        <button onClick={() => setOpen(!open)}>
          {open ? "收起扫描范围" : "预览扫描范围"}
        </button>
      </div>
      <p className="muted">
        扫描所选工作区的静态源码，更新阅读路线与代码依据。
      </p>
      {open && (
        <>
          <label className="row">
            <input
              type="checkbox"
              checked={rebuild}
              onChange={(e) => setRebuild(e.target.checked)}
            />
            重新构建索引（已有索引损坏时使用）
          </label>
          <ReadState query={query} />
          {data && !query.isFetching && (
            <>
              <p>{data.root}</p>
              <p>
                预计读取 {String(data.coverage.indexed_files)} 个文本文件，
                {Math.ceil(Number(data.coverage.read_bytes) / 1024)} KiB。
                {data.state.dirty ? "包含当前未提交的改动。" : "工作区干净。"}
              </p>
              <p className="muted">
                最多 {String(data.limits.files)} 个文件、单文件{" "}
                {Math.ceil(Number(data.limits.file_bytes) / 1024)} KiB、总计{" "}
                {Math.ceil(Number(data.limits.total_bytes) / 1024 / 1024)}{" "}
                MiB；敏感内容、符号链接和超范围文件会省略。
              </p>
              <Detail title="范围与省略依据" value={data.coverage} />
              <button
                disabled={
                  mutation.isPending ||
                  !session.data?.capabilities.includes("manage_local")
                }
                onClick={() => mutation.mutate()}
              >
                确认扫描工作区
              </button>
            </>
          )}
          {mutation.error && (
            <Notice>
              {mutation.error.message}
              <button onClick={() => query.refetch()}>重新预览</button>
            </Notice>
          )}
        </>
      )}
    </section>
  );
}
