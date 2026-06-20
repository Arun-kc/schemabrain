"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { Icon } from "@/components/Icon";

export interface MobileNavLink {
  label: string;
  href: string;
  /** Internal routes use next/link; section anchors / external use a plain <a>. */
  internal?: boolean;
}

export interface MobileNavAction {
  label: string;
  href: string;
  icon: string;
  primary?: boolean;
  internal?: boolean;
  external?: boolean;
}

interface MobileMenuProps {
  links: MobileNavLink[];
  actions: MobileNavAction[];
}

/**
 * Mobile nav: a hamburger button (shown ≤880px via `.ld-menu`) that toggles a
 * dropdown panel carrying the same links + actions the desktop bar shows.
 *
 * The desktop nav is left untouched — this is purely additive and `display:none`
 * above 880px, so the desktop layout (and its smoke tests) are unchanged. The
 * panel's links are hidden from the a11y tree when closed, so a `getByRole`
 * locator at desktop width still resolves to the single desktop link.
 *
 * A11y: `aria-expanded` / `aria-controls`, Escape + outside-click close, body
 * scroll lock while open, and focus returns to the toggle on close.
 */
export function MobileMenu({ links, actions }: MobileMenuProps) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const wrapRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    const onPointer = (e: PointerEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };

    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointer);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointer);
      document.body.style.overflow = prevOverflow;
    };
  }, [open]);

  const close = () => {
    setOpen(false);
    buttonRef.current?.focus();
  };

  return (
    <div className="ld-menu" ref={wrapRef}>
      <button
        ref={buttonRef}
        type="button"
        className="sb-iconbtn ld-burger"
        aria-label={open ? "Close menu" : "Open menu"}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name={open ? "x" : "menu"} size={18} />
      </button>

      <div className="ld-mobile" id={panelId} data-open={open}>
        <div className="ld-mobile-in">
          {links.map((link) =>
            link.internal ? (
              <Link key={link.href} className="ld-mobile-link" href={link.href} onClick={close}>
                {link.label}
              </Link>
            ) : (
              <a key={link.href} className="ld-mobile-link" href={link.href} onClick={close}>
                {link.label}
              </a>
            ),
          )}
          <div className="ld-mobile-actions">
            {actions.map((action) =>
              action.internal ? (
                <Link
                  key={action.href}
                  className={"sb-btn" + (action.primary ? " primary" : "")}
                  href={action.href}
                  onClick={close}
                >
                  <Icon name={action.icon} size={14} /> {action.label}
                </Link>
              ) : (
                <a
                  key={action.href}
                  className={"sb-btn" + (action.primary ? " primary" : "")}
                  href={action.href}
                  onClick={close}
                  {...(action.external ? { target: "_blank", rel: "noreferrer" } : {})}
                >
                  <Icon name={action.icon} size={14} /> {action.label}
                </a>
              ),
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
