import { PolicyEditor } from "@/components/policy/PolicyEditor";

/**
 * Policy surface (/policy), mounted inside the app shell (the shell owns
 * the chrome — top bar, nav rail, source selector, theme). The editable
 * policy editor supersedes the read-only PolicyView that previously sat
 * on /pii (ADR 0007); /pii is now the PII matrix alone.
 */
export default function PolicyPage() {
  return <PolicyEditor />;
}
