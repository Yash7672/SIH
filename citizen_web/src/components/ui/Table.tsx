import type { ReactNode } from "react";

/**
 * Table shell: sticky header, zebra rows, horizontal scroll contained in the
 * wrapper so a wide table never pushes the page sideways on a phone.
 */
export function Table({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={["scrollbar-thin overflow-x-auto rounded-xl border border-surface-border", className].join(" ")}>
      <table className="w-full min-w-[640px] border-collapse text-sm">{children}</table>
    </div>
  );
}

export function THead({ children }: { children: ReactNode }) {
  return (
    <thead className="sticky top-0 z-10 bg-surface-raised text-left">
      <tr className="border-b border-surface-border">{children}</tr>
    </thead>
  );
}

export function TH({
  children,
  className = "",
  ...rest
}: { children?: ReactNode; className?: string } & React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={[
        "px-4 py-3 text-xs font-semibold uppercase tracking-wider text-surface-muted",
        className,
      ].join(" ")}
      {...rest}
    >
      {children}
    </th>
  );
}

export function TBody({ children }: { children: ReactNode }) {
  return <tbody className="divide-y divide-surface-border">{children}</tbody>;
}

export function TR({
  children,
  className = "",
  ...rest
}: { children: ReactNode; className?: string } & React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={["odd:row-zebra-odd even:row-zebra-even row-hover", className].join(" ")}
      {...rest}
    >
      {children}
    </tr>
  );
}

export function TD({
  children,
  className = "",
  ...rest
}: { children?: ReactNode; className?: string } & React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={["px-4 py-3 align-middle text-surface-text", className].join(" ")} {...rest}>
      {children}
    </td>
  );
}

export default Table;