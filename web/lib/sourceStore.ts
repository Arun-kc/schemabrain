"use client";

import { create } from "zustand";

/**
 * The active-source selection — a single piece of app-level client state
 * shared by the shell source selector and every surface that scopes its
 * data to one source.
 *
 * `selectedSourceId` is null until the operator explicitly picks a source
 * in the shell. While null, consumers fall back to the meta
 * `default_source_connection_id` (see useSourceId / useSources), so the
 * default-source behaviour is unchanged for surfaces that never touch the
 * selector. This is deliberately NOT the resolved source — it is only the
 * override — so the resolver stays the single source of truth.
 */
interface SourceState {
  selectedSourceId: string | null;
  setSelectedSource: (id: string | null) => void;
}

export const useSourceStore = create<SourceState>((set) => ({
  selectedSourceId: null,
  setSelectedSource: (id) => set({ selectedSourceId: id }),
}));
