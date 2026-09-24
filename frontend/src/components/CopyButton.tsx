import { useEffect, useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

/**
 * CopyButton — one-click copy for any value the user can see (email,
 * LinkedIn URL, phone, arbitrary link). Renders nothing when *value* is
 * empty, so it can sit beside optional fields unconditionally.
 *
 * Shows a checkmark for ~1.4s after a successful copy — the only feedback
 * a clipboard write can honestly give (we cannot read the clipboard back).
 */
export function CopyButton({ value, label, onCopied }: { value: string; label?: string; onCopied?: () => void }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  if (!value) return null;

  async function copy() {
    // navigator.clipboard needs a secure context (https or localhost) —
    // the http fallback keeps plain-IP deployments copying too.
    let ok = false;
    try {
      await navigator.clipboard.writeText(value);
      ok = true;
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = value;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand("copy");
        document.body.removeChild(ta);
      } catch {
        ok = false;
      }
    }
    if (ok) {
      onCopied?.();
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1400);
    }
  }

  return (
    <button
      type="button"
      onClick={copy}
      aria-label={label ? `Copy ${label}` : "Copy"}
      title={label ? `Copy ${label}` : "Copy"}
      className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-slate-500 transition-colors hover:text-indigo-300"
    >
      {copied ? (
        <Check className="h-3.5 w-3.5 text-emerald-400" />
      ) : (
        <Copy className="h-3.5 w-3.5" />
      )}
    </button>
  );
}
