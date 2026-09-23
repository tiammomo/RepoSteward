import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { TaskActions } from "./task-actions";

const mocks = vi.hoisted(() => ({ command: vi.fn(), read: vi.fn(), fixtures: {} as Record<string, unknown> }));
vi.mock("../api/client", () => ({
  command: mocks.command,
  read: mocks.read,
  useRead: (name: string) => ({ data: mocks.fixtures[name], isPending: false, isFetching: false, error: null, refetch: vi.fn() }),
}));
const item = { id: "package", project_id: "project", run_id: "run", client: "codex", digest: "digest", created_at: "2026-09-23T01:00:00Z", current_applicability: "unchanged", acknowledgement: null };
function show() {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><TaskActions projectId="project" runId="run" /></MemoryRouter></QueryClientProvider>);
}
beforeEach(() => {
  mocks.command.mockReset().mockResolvedValue(item);
  mocks.read.mockReset().mockResolvedValue({ ...item, content: { context: { open_work: ["retain requirement"] } } });
  mocks.fixtures = {
    session: { capabilities: ["read_local", "manage_local"] },
    "task-preview": { plan_digest: "plan", budget: 24000, estimated_tokens: 1000, validity: [], coverage: [], context: { open_work: ["retain requirement"] }, export_available: true, verification_available: true, profiles: [{ name: "unit", commands: ["python -m unittest"] }] },
    handoffs: { items: [item], has_more: false },
  };
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
describe("explicit task handoff", () => {
  it("uses the reviewed preview and selected client when saving", async () => {
    show();
    fireEvent.change(screen.getByLabelText("目标客户端"), { target: { value: "claude-code" } });
    fireEvent.click(screen.getByText("生成并保存接续包"));
    await waitFor(() => expect(mocks.command).toHaveBeenCalledWith("tasks/handoff", { project_id: "project", run_id: "run", budget: 24000, expected_plan: "plan", client: "claude-code" }));
    expect(screen.getByText(/尚未确认客户端接收/)).toBeTruthy();
  });
  it("downloads only the stored scoped artifact and does not claim receipt", async () => {
    const create = vi.fn().mockReturnValue("blob:test");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    show();
    fireEvent.click(screen.getByText("下载已保存的接续包"));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(mocks.read).toHaveBeenCalledWith("handoff", { project_id: "project", run_id: "run", handoff_id: "package" });
    expect(mocks.command).not.toHaveBeenCalled();
  });
  it("records explicit human confirmation without executing a client or verification", async () => {
    show();
    fireEvent.click(screen.getByText("我已将此包交给客户端"));
    await waitFor(() => expect(mocks.command).toHaveBeenCalledWith("tasks/acknowledge", { project_id: "project", run_id: "run", handoff_id: "package", expected_digest: "digest" }));
    expect(mocks.command).toHaveBeenCalledTimes(1);
  });
  it("disables every mutation in a read-only session", () => {
    mocks.fixtures.session = { capabilities: ["read_local"] };
    show();
    for (const label of ["生成并保存接续包", "我已将此包交给客户端", "排队执行此验证"])
      expect(screen.getByText(label).hasAttribute("disabled")).toBe(true);
    expect(mocks.command).not.toHaveBeenCalled();
  });
});
