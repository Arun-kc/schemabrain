import { beforeEach, describe, expect, it } from "vitest";
import { useSourceStore } from "./sourceStore";

describe("useSourceStore", () => {
  beforeEach(() => {
    useSourceStore.setState({ selectedSourceId: null });
  });

  it("starts with no explicit selection", () => {
    expect(useSourceStore.getState().selectedSourceId).toBeNull();
  });

  it("sets and clears the selected source", () => {
    useSourceStore.getState().setSelectedSource("src_a");
    expect(useSourceStore.getState().selectedSourceId).toBe("src_a");

    useSourceStore.getState().setSelectedSource(null);
    expect(useSourceStore.getState().selectedSourceId).toBeNull();
  });
});
