import { Matrix } from "@/components/pii/Matrix";
import { PolicyView } from "@/components/pii/PolicyView";

/**
 * PII surface, mounted inside the app shell (the shell owns the chrome:
 * top bar, nav rail, source selector, theme — see (app)/layout.tsx).
 *
 * The Matrix + PolicyView bodies still use the legacy token palette;
 * reskinning them onto the kit/tokens is the next PR. This PR only
 * removes the per-surface HeaderStrip + full-page wrapper so the surface
 * flows in the shell's scroll region.
 */
export default function PiiPage() {
  return (
    <>
      <Matrix />
      <PolicyView />
    </>
  );
}
