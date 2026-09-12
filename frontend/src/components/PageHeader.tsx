/**
 * PageHeader — the shared screen header (the frontend1 design idea adopted
 * 2026-09-12): a small letterspaced EYEBROW line above a bold title, so every
 * screen opens with the same branded hierarchy instead of a bare 26px heading.
 *
 * ``large`` (the Dashboard hero variant) goes bigger and tighter — the visual
 * anchor of the app; standard screens keep the working size they always had.
 */

export function PageHeader({
  eyebrow,
  title,
  subtitle,
  large = false,
}: {
  /** Small uppercase kicker above the title — e.g. "The Best Estimator LLC". */
  eyebrow: string;
  title: string;
  subtitle?: string;
  large?: boolean;
}) {
  return (
    <div>
      <p className="text-indigo-400 text-[11.5px] font-bold tracking-[0.18em] uppercase">
        {eyebrow}
      </p>
      <h1
        className={
          large
            ? "text-[34px] sm:text-[40px] font-semibold text-white tracking-tight mt-1.5"
            : "text-[26px] font-semibold text-white tracking-tight mt-1"
        }
      >
        {title}
      </h1>
      {subtitle && (
        <p className="text-slate-500 text-[13.5px] mt-1.5">{subtitle}</p>
      )}
    </div>
  );
}
