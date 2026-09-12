import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";

export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

/**
 * Select — the dark-themed replacement for native <select>.
 *
 * Native dropdowns open an OS-rendered WHITE list that ignores the page
 * theme entirely; on the dark UI the trade/state/city names were barely
 * readable. This component renders its own popover: dark background,
 * indigo highlight, check mark on the chosen row, and a filter box when
 * the list is long (50 states, many tags). Keyboard works like a native
 * select: arrows navigate, Enter picks, Esc closes.
 */
export function Select({
  value,
  onChange,
  options,
  placeholder = "Select…",
  className = "",
  filterable = false,
  disabled = false,
  ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  /** Width/layout classes for the trigger button (w-full, w-auto, …). */
  className?: string;
  /** Force the in-popover filter box on/off. Default: on when >8 options. */
  filterable?: boolean;
  disabled?: boolean;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0); // highlighted row while open
  const [flipUp, setFlipUp] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const filterRef = useRef<HTMLInputElement>(null);

  const showFilter = filterable || options.length > 8;
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((o) => o.label.toLowerCase().includes(q));
  }, [options, query]);

  const selected = options.find((o) => o.value === value);

  // Close on outside click / Esc — a dropdown that traps focus is a bug.
  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
        rootRef.current?.querySelector("button")?.focus();
      }
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Focus the filter box on open (typing filters immediately) and reset state on close.
  useEffect(() => {
    if (open) {
      setQuery("");
      const i = options.findIndex((o) => o.value === value);
      setActive(i < 0 ? 0 : i);
      // Focus after the popover mounts.
      requestAnimationFrame(() => filterRef.current?.focus());
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  // Flip the popover up when there is more room above than below.
  useLayoutEffect(() => {
    if (!open) return;
    const rect = rootRef.current?.getBoundingClientRect();
    if (!rect) return;
    const below = window.innerHeight - rect.bottom;
    setFlipUp(below < 240 && rect.top > below);
  }, [open]);

  // Keep the highlighted row visible while arrowing through a long list.
  useEffect(() => {
    const el = listRef.current?.children[active] as HTMLElement | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  function choose(opt: SelectOption) {
    if (opt.disabled) return;
    onChange(opt.value);
    setOpen(false);
    rootRef.current?.querySelector("button")?.focus();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (disabled) return;
    if (!open) {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => {
        const next = e.key === "ArrowDown" ? i + 1 : i - 1;
        return Math.max(0, Math.min(filtered.length - 1, next));
      });
    } else if (e.key === "Enter") {
      e.preventDefault();
      const opt = filtered[active];
      if (opt) choose(opt);
    } else if (e.key === "Home" || e.key === "End") {
      e.preventDefault();
      setActive(e.key === "Home" ? 0 : filtered.length - 1);
    }
  }

  return (
    <div ref={rootRef} className={`relative ${className}`} onKeyDown={onKeyDown}>
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => setOpen((v) => !v)}
        className={`flex w-full items-center justify-between gap-2 rounded-lg border bg-white/[0.04] px-3.5 py-2.5 text-left text-[13px] outline-none transition-colors
          ${selected ? "text-slate-200" : "text-slate-500"}
          ${open ? "border-indigo-500/40 ring-2 ring-indigo-500/40" : "border-white/5 hover:border-white/10"}
          disabled:opacity-50 disabled:cursor-not-allowed`}
      >
        <span className="truncate">{selected ? selected.label : placeholder}</span>
        <ChevronDown
          className={`h-4 w-4 shrink-0 text-slate-500 transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>

      {open && (
        <div
          className={`absolute left-0 right-0 z-50 min-w-full rounded-lg border border-white/10 bg-[#0d1117] shadow-2xl shadow-black/60 ${
            flipUp ? "bottom-full mb-1.5" : "top-full mt-1.5"
          }`}
        >
          {showFilter && (
            <div className="flex items-center gap-2 border-b border-white/5 px-3 py-2">
              <Search className="h-3.5 w-3.5 shrink-0 text-slate-500" />
              <input
                ref={filterRef}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(0);
                }}
                placeholder="Filter…"
                className="w-full bg-transparent text-[12.5px] text-slate-200 placeholder:text-slate-600 outline-none"
              />
            </div>
          )}
          <ul ref={listRef} role="listbox" className="max-h-60 overflow-y-auto py-1">
            {filtered.map((o, i) => (
              <li
                key={o.value}
                role="option"
                aria-selected={o.value === value}
                onClick={() => choose(o)}
                onMouseEnter={() => setActive(i)}
                className={`flex cursor-pointer items-center justify-between gap-2 px-3 py-2 text-[13px] ${
                  o.disabled ? "cursor-not-allowed text-slate-600" : "text-slate-300"
                } ${i === active ? "bg-indigo-500/15" : ""} ${o.value === value ? "text-white" : ""}`}
              >
                <span className="truncate">{o.label}</span>
                {o.value === value && <Check className="h-3.5 w-3.5 shrink-0 text-indigo-400" />}
              </li>
            ))}
            {filtered.length === 0 && (
              <li className="px-3 py-3 text-[12.5px] text-slate-500">No matches.</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * Combobox — the dark-themed replacement for <datalist>. A free-text input
 * (type a brand-new folder name) that also offers the existing options in a
 * dark dropdown as you type. Native datalist popups are OS-white, same
 * problem as native selects.
 */
export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  className = "",
}: {
  value: string;
  onChange: (value: string) => void;
  /** Suggestion strings (existing folder/tag names). */
  options: string[];
  placeholder?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  const q = value.trim().toLowerCase();
  const suggestions = useMemo(() => {
    const pool = q ? options.filter((o) => o.toLowerCase().includes(q)) : options;
    return pool.slice(0, 8);
  }, [options, q]);

  useEffect(() => {
    if (!open) return;
    function onDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        placeholder={placeholder}
        className="w-full rounded-lg border border-white/5 bg-white/[0.04] px-3.5 py-2.5 text-[13px] text-slate-300 placeholder:text-slate-500 outline-none focus:border-indigo-500/40 focus:ring-2 focus:ring-indigo-500/40"
      />
      {open && suggestions.length > 0 && (
        <ul className="absolute left-0 right-0 top-full z-50 mt-1.5 max-h-48 overflow-y-auto rounded-lg border border-white/10 bg-[#0d1117] py-1 shadow-2xl shadow-black/60">
          {suggestions.map((s) => (
            <li
              key={s}
              onClick={() => {
                onChange(s);
                setOpen(false);
              }}
              className="cursor-pointer truncate px-3 py-2 text-[13px] text-slate-300 hover:bg-indigo-500/15"
            >
              {s}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
