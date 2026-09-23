import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Copy, Detail, SafeLink } from "./components";

afterEach(cleanup);
describe("repository data and task handoff", () => {
  it("renders remote HTML as inert text and rejects executable URLs", () => {
    render(
      <>
        <SafeLink href="javascript:alert(1)">unsafe</SafeLink>
        <Detail
          title="来源"
          value={{ title: "<img src=x onerror=alert(1)>" }}
        />
      </>,
    );
    expect(document.querySelector("a")).toBeNull();
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText(/onerror/).textContent).toContain("<img");
  });
  it("copies the exact handoff and reports completion", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <Copy
        value='{"next_action":"inspect current changes"}'
        label="复制接续数据"
      />,
    );
    fireEvent.click(screen.getByText("复制接续数据"));
    expect(await screen.findByText("已复制")).toBeTruthy();
    expect(writeText).toHaveBeenCalledWith(
      '{"next_action":"inspect current changes"}',
    );
  });
  it("preserves a manual fallback if clipboard access is denied", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    render(<Copy value="reposteward task context example" />);
    fireEvent.click(screen.getByText("复制"));
    expect(await screen.findByText("请手动选择文本复制")).toBeTruthy();
  });
});
