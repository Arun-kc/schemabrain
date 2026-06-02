"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

interface ToastContextValue {
  /** Show a transient confirmation message. */
  showToast: (message: string) => void;
  /**
   * Copy text to the clipboard and surface a confirmation toast. `label` is
   * what the toast displays (e.g. "Copied markdown" or an ON-clause); falls
   * back to the copied text itself.
   */
  copyToClipboard: (text: string, label?: string) => Promise<void>;
}

const ToastContext = createContext<ToastContextValue | null>(null);

const TOAST_DURATION_MS = 1700;

/**
 * Shared copy-confirmation primitive (handoff app.jsx global toast). Provides
 * the single bottom-centre toast consumed across surfaces (entity ON-clause
 * copy, dictionary "Copied markdown", etc.) so there is exactly one toast
 * element in the tree.
 */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [message, setMessage] = useState("");
  const [open, setOpen] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((next: string) => {
    setMessage(next);
    setOpen(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setOpen(false), TOAST_DURATION_MS);
  }, []);

  const copyToClipboard = useCallback(
    async (text: string, label?: string) => {
      try {
        if (typeof navigator !== "undefined" && navigator.clipboard) {
          await navigator.clipboard.writeText(text);
        }
      } catch {
        // Clipboard can reject (insecure context / denied permission). Still
        // toast so the user gets feedback and can copy the selection manually.
      }
      showToast(label ?? text);
    },
    [showToast],
  );

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  return (
    <ToastContext.Provider value={{ showToast, copyToClipboard }}>
      {children}
      <div className="sb-toast" data-open={open} role="status" aria-live="polite">
        copied · {message}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a <ToastProvider>");
  }
  return ctx;
}
